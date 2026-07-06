from __future__ import annotations

import logging
import secrets
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from gw2bot.database import (
    FeastAlertRecord,
    GuildInviteRecord,
    GuildJoinRecord,
    GuildLeaveRecord,
    GuildRankChangeRecord,
    RaffleAccountLinkRecord,
    RaffleDepositRecord,
    RaffleManualTicketRecord,
    RaffleMilestoneRecord,
    RaffleRunEntryRecord,
    RaffleRunRecord,
    RaffleRunWinnerRecord,
    RaffleTotalRecord,
    SettingRecord,
    TrackedTrialMemberRecord,
    TrialForumPostRecord,
    create_database_engine,
    initialize_database,
)
from gw2bot.raffle.events import (
    event_in_window,
    parse_gold_deposit,
    parse_guild_invite,
    parse_guild_join,
    parse_guild_leave,
    parse_guild_rank_change,
)
from gw2bot.raffle.models import (
    COPPER_PER_GOLD,
    MAX_GOLD_RAFFLE_TICKETS,
    MAX_MANUAL_RAFFLE_TICKETS,
    OFFICER_MAX_TICKET_DEPOSIT_COINS,
    RAFFLE_DRAW_TIERS,
    RAFFLE_REWARD_TIERS,
    GuildInvite,
    GuildJoin,
    GuildLeave,
    GuildRankChange,
    RaffleContribution,
    RaffleDeposit,
    RaffleDrawTier,
    RaffleMilestone,
    RaffleResult,
    RaffleRewardTier,
    RaffleTotal,
    RaffleWinner,
    TrialForumPost,
)

LOGGER = logging.getLogger(__name__)

MANUAL_TICKET_CAP_MIGRATION_KEY = "manual_ticket_cap_v1"
OFFICER_PURCHASE_EVENT_ID_KEY = "officer_purchase_event_id"
OFFICER_PURCHASE_EVENT_ID_START = -(2**63)
TRIAL_FORUM_INDEX_WATERMARK_KEY = "trial_forum_index_watermark"


class RaffleStore:
    def __init__(
        self,
        database_path: str,
        guild_id: str,
        reward_tiers: tuple[RaffleRewardTier, ...] = RAFFLE_REWARD_TIERS,
        draw_tiers: tuple[RaffleDrawTier, ...] = RAFFLE_DRAW_TIERS,
    ):
        LOGGER.debug("Opening raffle store")
        self._reward_tiers = _validate_reward_tiers(reward_tiers)
        self._draw_tiers = _validate_draw_tiers(draw_tiers)
        self._engine = create_database_engine(database_path)
        added_columns = initialize_database(self._engine)
        self._sessions = sessionmaker(self._engine, expire_on_commit=False)
        if "gold_raffle_tickets" in added_columns:
            self._migrate_legacy_totals()
        try:
            self._bind_guild(guild_id)
            self._apply_manual_ticket_cap_once()
        except Exception:
            self.close()
            raise
        LOGGER.debug(
            "Raffle store initialized; migrated_columns=%s",
            sorted(added_columns),
        )

    def close(self) -> None:
        LOGGER.debug("Closing raffle store")
        self._engine.dispose()

    def get_cursor(self) -> int | None:
        with self._sessions() as session:
            record = session.get(SettingRecord, "guild_log_cursor")
            cursor = int(record.value) if record is not None else None
            LOGGER.debug("Loaded guild log cursor %s", cursor)
            return cursor

    def get_feast_alert_times(self) -> dict[int, float]:
        with self._sessions() as session:
            records = session.scalars(select(FeastAlertRecord)).all()
            results = {
                record.guild_storage_id: record.last_notification_time
                for record in records
            }
            LOGGER.debug("Loaded %s persisted feast alert times", len(results))
            return results

    def mark_feast_alert_sent(
        self,
        guild_storage_id: int,
        notification_time: float,
    ) -> None:
        with self._sessions.begin() as session:
            record = session.get(FeastAlertRecord, guild_storage_id)
            if record is None:
                session.add(
                    FeastAlertRecord(
                        guild_storage_id=guild_storage_id,
                        last_notification_time=notification_time,
                    )
                )
            else:
                record.last_notification_time = notification_time
        LOGGER.debug("Marked feast alert %s sent", guild_storage_id)

    def clear_feast_alert(self, guild_storage_id: int) -> None:
        with self._sessions.begin() as session:
            record = session.get(FeastAlertRecord, guild_storage_id)
            if record is not None:
                session.delete(record)
                LOGGER.debug("Cleared feast alert %s", guild_storage_id)

    def initialize_cursor(self, event_id: int) -> None:
        with self._sessions.begin() as session:
            if session.get(SettingRecord, "guild_log_cursor") is None:
                session.add(
                    SettingRecord(key="guild_log_cursor", value=str(event_id))
                )
                LOGGER.debug("Initialized guild log cursor at event %s", event_id)

    def process_events(
        self,
        events: list[dict[str, Any]],
        officer_usernames: set[str] | None = None,
    ) -> None:
        processed = 0
        deposits = 0
        officer_deposits_skipped = 0
        joins = 0
        leaves = 0
        invites = 0
        rank_changes = 0
        officer_keys = {
            username.casefold() for username in officer_usernames or set()
        }
        with self._sessions.begin() as session:
            cursor_record = session.get(SettingRecord, "guild_log_cursor")
            if cursor_record is None:
                raise RuntimeError("Guild log cursor must be initialized first")
            cursor = int(cursor_record.value)

            for event in sorted(events, key=lambda entry: int(entry["id"])):
                event_id = int(event["id"])
                if event_id <= cursor:
                    continue

                deposit = parse_gold_deposit(event)
                if deposit is not None:
                    is_officer = deposit.username.casefold() in officer_keys
                    if (
                        is_officer
                        and deposit.coins_deposited
                        > OFFICER_MAX_TICKET_DEPOSIT_COINS
                    ):
                        LOGGER.debug(
                            "Skipped oversized Officer raffle deposit event %s; "
                            "coins=%s maximum=%s",
                            deposit.event_id,
                            deposit.coins_deposited,
                            OFFICER_MAX_TICKET_DEPOSIT_COINS,
                        )
                        officer_deposits_skipped += 1
                    else:
                        self._process_deposit(session, deposit)
                        deposits += 1

                join = parse_guild_join(event)
                if (
                    join is not None
                    and session.get(GuildJoinRecord, join.event_id) is None
                ):
                    session.add(
                        GuildJoinRecord(
                            event_id=join.event_id,
                            username=join.username,
                            event_time=join.event_time,
                        )
                    )
                    joins += 1

                leave = parse_guild_leave(event)
                if (
                    leave is not None
                    and session.get(GuildLeaveRecord, leave.event_id) is None
                ):
                    session.add(
                        GuildLeaveRecord(
                            event_id=leave.event_id,
                            username=leave.username,
                            kicked_by=leave.kicked_by,
                            event_time=leave.event_time,
                        )
                    )
                    leaves += 1

                invite = parse_guild_invite(event)
                if (
                    invite is not None
                    and session.get(GuildInviteRecord, invite.event_id) is None
                ):
                    session.add(
                        GuildInviteRecord(
                            event_id=invite.event_id,
                            username=invite.username,
                            invited_by=invite.invited_by,
                            event_time=invite.event_time,
                        )
                    )
                    invites += 1

                rank_change = parse_guild_rank_change(event)
                if (
                    rank_change is not None
                    and session.get(GuildRankChangeRecord, rank_change.event_id)
                    is None
                ):
                    session.add(
                        GuildRankChangeRecord(
                            event_id=rank_change.event_id,
                            username=rank_change.username,
                            old_rank=rank_change.old_rank,
                            new_rank=rank_change.new_rank,
                            changed_by=rank_change.changed_by,
                            event_time=rank_change.event_time,
                        )
                    )
                    rank_changes += 1

                cursor = event_id
                processed += 1

            self._create_reached_milestones(session)
            cursor_record.value = str(cursor)
        LOGGER.debug(
            "Processed guild log events; fetched=%s new=%s deposits=%s "
            "officer_deposits_skipped=%s joins=%s leaves=%s invites=%s "
            "rank_changes=%s cursor=%s",
            len(events),
            processed,
            deposits,
            officer_deposits_skipped,
            joins,
            leaves,
            invites,
            rank_changes,
            cursor,
        )

    def get_pending_notifications(self) -> list[RaffleDeposit]:
        statement = (
            select(RaffleDepositRecord)
            .where(RaffleDepositRecord.notification_sent.is_(False))
            .order_by(RaffleDepositRecord.event_id)
        )
        with self._sessions() as session:
            results = [
                _to_raffle_deposit(record)
                for record in session.scalars(statement).all()
            ]
            LOGGER.debug("Loaded %s pending raffle notifications", len(results))
            return results

    def get_pending_deposit_audit_notifications(self) -> list[RaffleDeposit]:
        statement = (
            select(RaffleDepositRecord)
            .where(RaffleDepositRecord.audit_notification_sent.is_(False))
            .order_by(RaffleDepositRecord.event_id)
        )
        with self._sessions() as session:
            results = [
                _to_raffle_deposit(record)
                for record in session.scalars(statement).all()
            ]
            LOGGER.debug(
                "Loaded %s pending raffle deposit audit notifications",
                len(results),
            )
            return results

    def get_pending_leave_notifications(self) -> list[GuildLeave]:
        statement = (
            select(GuildLeaveRecord)
            .where(GuildLeaveRecord.notification_sent.is_(False))
            .order_by(GuildLeaveRecord.event_id)
        )
        with self._sessions() as session:
            results = [
                _to_guild_leave(record)
                for record in session.scalars(statement).all()
            ]
            LOGGER.debug("Loaded %s pending guild-leave notifications", len(results))
            return results

    def get_pending_join_notifications(self) -> list[GuildJoin]:
        statement = (
            select(GuildJoinRecord)
            .where(GuildJoinRecord.notification_sent.is_(False))
            .order_by(GuildJoinRecord.event_id)
        )
        with self._sessions() as session:
            results = [
                _to_guild_join(record)
                for record in session.scalars(statement).all()
            ]
            LOGGER.debug("Loaded %s pending guild-join notifications", len(results))
            return results

    def get_pending_invite_notifications(self) -> list[GuildInvite]:
        statement = (
            select(GuildInviteRecord)
            .where(GuildInviteRecord.notification_sent.is_(False))
            .order_by(GuildInviteRecord.event_id)
        )
        with self._sessions() as session:
            results = [
                _to_guild_invite(record)
                for record in session.scalars(statement).all()
            ]
            LOGGER.debug("Loaded %s pending guild-invite notifications", len(results))
            return results

    def get_pending_rank_change_notifications(self) -> list[GuildRankChange]:
        statement = (
            select(GuildRankChangeRecord)
            .where(GuildRankChangeRecord.notification_sent.is_(False))
            .order_by(GuildRankChangeRecord.event_id)
        )
        with self._sessions() as session:
            results = [
                _to_guild_rank_change(record)
                for record in session.scalars(statement).all()
            ]
            LOGGER.debug(
                "Loaded %s pending guild-rank-change notifications",
                len(results),
            )
            return results

    def get_pending_milestones(self) -> list[RaffleMilestone]:
        statement = (
            select(RaffleMilestoneRecord)
            .where(RaffleMilestoneRecord.notification_sent.is_(False))
            .order_by(RaffleMilestoneRecord.threshold)
        )
        with self._sessions() as session:
            results = [
                _to_raffle_milestone(record)
                for record in session.scalars(statement).all()
            ]
            LOGGER.debug("Loaded %s pending raffle milestones", len(results))
            return results

    def get_totals(self) -> list[RaffleTotal]:
        with self._sessions() as session:
            results = sorted(
                (
                    _to_raffle_total(record)
                    for record in session.scalars(select(RaffleTotalRecord)).all()
                ),
                key=lambda total: (
                    -total.raffle_tickets,
                    total.username.casefold(),
                    total.username,
                ),
            )
            LOGGER.debug("Loaded %s raffle totals", len(results))
            return results

    def get_total(self, username: str) -> RaffleTotal:
        with self._sessions() as session:
            record = session.get(RaffleTotalRecord, username)
            if record is not None:
                return _to_raffle_total(record)
        return RaffleTotal(
            username=username,
            coins_deposited=0,
            raffle_tickets=0,
            gold_raffle_tickets=0,
            manual_raffle_tickets=0,
        )

    def get_linked_username(self, discord_user_id: int) -> str | None:
        with self._sessions() as session:
            record = session.get(RaffleAccountLinkRecord, discord_user_id)
            return record.username if record is not None else None

    def link_account(self, discord_user_id: int, username: str) -> None:
        with self._sessions.begin() as session:
            record = session.get(RaffleAccountLinkRecord, discord_user_id)
            if record is None:
                session.add(
                    RaffleAccountLinkRecord(
                        discord_user_id=discord_user_id,
                        username=username,
                    )
                )
            else:
                record.username = username

    def add_manual_ticket(
        self,
        username: str,
        event_time: datetime | None = None,
    ) -> RaffleTotal:
        with self._sessions.begin() as session:
            total = session.get(RaffleTotalRecord, username)
            manual_tickets = total.manual_raffle_tickets if total else 0
            if manual_tickets >= MAX_MANUAL_RAFFLE_TICKETS:
                raise ValueError(
                    f"{username} already has the maximum of "
                    f"{MAX_MANUAL_RAFFLE_TICKETS} manually added ticket"
                )
            if total is None:
                total = RaffleTotalRecord(
                    username=username,
                    coins_deposited=0,
                    raffle_tickets=1,
                    gold_raffle_tickets=0,
                    manual_raffle_tickets=1,
                )
                session.add(total)
            else:
                total.raffle_tickets += 1
                total.manual_raffle_tickets += 1
            session.add(
                RaffleManualTicketRecord(
                    username=username,
                    event_time=(event_time or datetime.now(UTC)).isoformat(),
                )
            )
            result = _to_raffle_total(total)
        LOGGER.debug(
            "Added manual raffle ticket; current_tickets=%s manual_tickets=%s",
            result.raffle_tickets,
            result.manual_raffle_tickets,
        )
        return result

    def add_officer_purchase(
        self,
        username: str,
        amount: int,
        event_time: datetime | None = None,
    ) -> RaffleTotal:
        if amount <= 0:
            raise ValueError("Ticket purchase amount must be greater than zero")
        with self._sessions.begin() as session:
            total = session.get(RaffleTotalRecord, username)
            purchased = total.gold_raffle_tickets if total is not None else 0
            if purchased + amount > MAX_GOLD_RAFFLE_TICKETS:
                LOGGER.debug(
                    "Officer raffle purchase rejected; amount=%s "
                    "current_purchased=%s maximum=%s",
                    amount,
                    purchased,
                    MAX_GOLD_RAFFLE_TICKETS,
                )
                raise ValueError(
                    f"Adding {amount} purchased raffle "
                    f"{'ticket' if amount == 1 else 'tickets'} would put "
                    f"{username} over the maximum of "
                    f"{MAX_GOLD_RAFFLE_TICKETS}. They currently have "
                    f"{purchased} purchased "
                    f"{'ticket' if purchased == 1 else 'tickets'}."
                )

            event_id_record = session.get(
                SettingRecord,
                OFFICER_PURCHASE_EVENT_ID_KEY,
            )
            event_id = (
                int(event_id_record.value)
                if event_id_record is not None
                else OFFICER_PURCHASE_EVENT_ID_START
            )
            if event_id_record is None:
                session.add(
                    SettingRecord(
                        key=OFFICER_PURCHASE_EVENT_ID_KEY,
                        value=str(event_id + 1),
                    )
                )
            else:
                event_id_record.value = str(event_id + 1)

            deposit = RaffleDeposit(
                event_id=event_id,
                username=username,
                coins_deposited=amount * COPPER_PER_GOLD,
                raffle_tickets=amount,
                event_time=(event_time or datetime.now(UTC)).isoformat(),
            )
            self._process_deposit(session, deposit)
            self._create_reached_milestones(session)
            session.flush()
            updated_total = session.get(RaffleTotalRecord, username)
            if updated_total is None:
                raise RuntimeError("Officer raffle purchase did not create a total")
            result = _to_raffle_total(updated_total)
        LOGGER.debug(
            "Recorded officer raffle purchase; amount=%s current_purchased=%s "
            "event_id=%s",
            amount,
            result.gold_raffle_tickets,
            event_id,
        )
        return result

    def remove_gold_tickets(self, username: str, amount: int = 1) -> RaffleTotal:
        if amount <= 0:
            raise ValueError("Ticket removal amount must be greater than zero")
        with self._sessions.begin() as session:
            total = session.get(RaffleTotalRecord, username)
            purchased = total.gold_raffle_tickets if total is not None else 0
            if total is None or purchased < amount:
                noun = "ticket" if purchased == 1 else "tickets"
                raise ValueError(
                    f"{username} has only {purchased} purchased raffle {noun}"
                )
            total.gold_raffle_tickets -= amount
            total.raffle_tickets -= amount
            result = _to_raffle_total(total)
        LOGGER.info(
            "Removed purchased raffle tickets; amount=%s remaining_purchased=%s",
            amount,
            result.gold_raffle_tickets,
        )
        return result

    def get_contributions(
        self,
        start: datetime,
        end: datetime,
    ) -> list[RaffleContribution]:
        start_utc = start.astimezone(UTC)
        end_utc = end.astimezone(UTC)
        contributions: dict[str, list[int]] = {}
        with self._sessions() as session:
            deposits = session.scalars(
                select(RaffleDepositRecord).where(
                    RaffleDepositRecord.raffle_tickets > 0
                )
            ).all()
            manual_tickets = session.scalars(select(RaffleManualTicketRecord)).all()

        for deposit in deposits:
            if event_in_window(deposit.event_time, start_utc, end_utc):
                counts = contributions.setdefault(deposit.username, [0, 0])
                counts[0] += deposit.raffle_tickets
        for ticket in manual_tickets:
            if event_in_window(ticket.event_time, start_utc, end_utc):
                counts = contributions.setdefault(ticket.username, [0, 0])
                counts[1] += 1

        return [
            RaffleContribution(
                username=username,
                purchased_tickets=counts[0],
                event_tickets=counts[1],
            )
            for username, counts in sorted(
                contributions.items(),
                key=lambda item: (
                    -(item[1][0] + item[1][1]),
                    item[0].casefold(),
                    item[0],
                ),
            )
        ]

    def get_lifetime_contributions(self) -> list[RaffleContribution]:
        contributions: dict[str, list[int]] = {}
        with self._sessions() as session:
            deposits = session.scalars(
                select(RaffleDepositRecord).where(
                    RaffleDepositRecord.raffle_tickets > 0
                )
            ).all()
            manual_tickets = session.scalars(select(RaffleManualTicketRecord)).all()

        for deposit in deposits:
            counts = contributions.setdefault(deposit.username, [0, 0])
            counts[0] += deposit.raffle_tickets
        for ticket in manual_tickets:
            counts = contributions.setdefault(ticket.username, [0, 0])
            counts[1] += 1

        results = [
            RaffleContribution(
                username=username,
                purchased_tickets=counts[0],
                event_tickets=counts[1],
            )
            for username, counts in sorted(
                contributions.items(),
                key=lambda item: (
                    -(item[1][0] + item[1][1]),
                    item[0].casefold(),
                    item[0],
                ),
            )
        ]
        LOGGER.debug("Loaded %s lifetime raffle contributors", len(results))
        return results

    def run_raffle(
        self,
        randbelow: Callable[[int], int] = secrets.randbelow,
    ) -> RaffleResult | None:
        pending = self.get_pending_raffle_result()
        if pending is not None:
            LOGGER.debug("Reusing pending raffle result run_id=%s", pending.run_id)
            return pending

        statement = (
            select(RaffleTotalRecord)
            .where(RaffleTotalRecord.raffle_tickets > 0)
            .order_by(RaffleTotalRecord.username)
        )
        with self._sessions.begin() as session:
            totals = session.scalars(statement).all()
            total_tickets = sum(total.raffle_tickets for total in totals)
            if total_tickets == 0:
                LOGGER.debug("Raffle draw skipped because no tickets are available")
                return None

            purchased_tickets = sum(total.gold_raffle_tickets for total in totals)
            free_tickets = sum(total.manual_raffle_tickets for total in totals)
            draw_count = min(
                _winner_count_for_purchased_tickets(
                    purchased_tickets,
                    self._draw_tiers,
                ),
                total_tickets,
            )
            remaining_tickets = {
                total.username: total.raffle_tickets for total in totals
            }
            winners: list[RaffleWinner] = []
            for _ in range(draw_count):
                tickets_before_draw = sum(remaining_tickets.values())
                winning_ticket = randbelow(tickets_before_draw) + 1
                cursor = 0
                winner = ""
                for username, ticket_count in remaining_tickets.items():
                    cursor += ticket_count
                    if winning_ticket <= cursor:
                        winner = username
                        break
                tickets_held = remaining_tickets[winner]
                remaining_tickets[winner] -= 1
                winners.append(
                    RaffleWinner(
                        username=winner,
                        winning_ticket=winning_ticket,
                        tickets_before_draw=tickets_before_draw,
                        tickets_held=tickets_held,
                    )
                )

            run = RaffleRunRecord(
                winner=winners[0].username,
                winning_ticket=winners[0].winning_ticket,
                total_tickets=total_tickets,
                purchased_tickets=purchased_tickets,
                free_tickets=free_tickets,
            )
            session.add(run)
            session.flush()
            run_id = run.run_id
            session.add_all(
                [
                    RaffleRunEntryRecord(
                        run_id=run_id,
                        username=total.username,
                        raffle_tickets=total.raffle_tickets,
                    )
                    for total in totals
                ]
            )
            session.add_all(
                [
                    RaffleRunWinnerRecord(
                        run_id=run_id,
                        draw_position=position,
                        username=winner.username,
                        winning_ticket=winner.winning_ticket,
                        tickets_before_draw=winner.tickets_before_draw,
                    )
                    for position, winner in enumerate(winners, start=1)
                ]
            )
            for total in totals:
                total.raffle_tickets = 0
                total.gold_raffle_tickets = 0
                total.manual_raffle_tickets = 0
            session.execute(delete(RaffleMilestoneRecord))

        result = RaffleResult(
            run_id=run_id,
            winners=tuple(winners),
            total_tickets=total_tickets,
            purchased_tickets=purchased_tickets,
            free_tickets=free_tickets,
        )
        LOGGER.debug(
            "Created raffle run %s; participants=%s purchased_tickets=%s "
            "free_tickets=%s total_tickets=%s winners=%s",
            run_id,
            len(totals),
            purchased_tickets,
            free_tickets,
            total_tickets,
            len(winners),
        )
        return result

    def get_pending_raffle_result(self) -> RaffleResult | None:
        statement = (
            select(RaffleRunRecord)
            .where(RaffleRunRecord.announcement_sent.is_(False))
            .order_by(RaffleRunRecord.run_id)
        )
        with self._sessions() as session:
            record = session.scalars(statement).first()
            result = (
                _to_raffle_result(session, record) if record is not None else None
            )
            LOGGER.debug("Loaded pending raffle result; found=%s", result is not None)
            return result

    def mark_raffle_announcement_sent(self, run_id: int) -> None:
        with self._sessions.begin() as session:
            record = session.get(RaffleRunRecord, run_id)
            if record is not None:
                record.announcement_sent = True
                LOGGER.debug("Marked raffle run %s announcement sent", run_id)

    def mark_notification_sent(self, event_id: int) -> None:
        with self._sessions.begin() as session:
            record = session.get(RaffleDepositRecord, event_id)
            if record is not None:
                record.notification_sent = True
                LOGGER.debug("Marked raffle deposit event %s notification sent", event_id)

    def mark_deposit_audit_notification_sent(self, event_id: int) -> None:
        with self._sessions.begin() as session:
            record = session.get(RaffleDepositRecord, event_id)
            if record is not None:
                record.audit_notification_sent = True
                LOGGER.debug(
                    "Marked raffle deposit event %s audit notification sent",
                    event_id,
                )

    def mark_leave_notification_sent(self, event_id: int) -> None:
        with self._sessions.begin() as session:
            record = session.get(GuildLeaveRecord, event_id)
            if record is not None:
                record.notification_sent = True
                LOGGER.debug("Marked guild-leave event %s notification sent", event_id)

    def mark_join_notification_sent(self, event_id: int) -> None:
        with self._sessions.begin() as session:
            record = session.get(GuildJoinRecord, event_id)
            if record is not None:
                record.notification_sent = True
                LOGGER.debug("Marked guild-join event %s notification sent", event_id)

    def mark_invite_notification_sent(self, event_id: int) -> None:
        with self._sessions.begin() as session:
            record = session.get(GuildInviteRecord, event_id)
            if record is not None:
                record.notification_sent = True
                LOGGER.debug("Marked guild-invite event %s notification sent", event_id)

    def mark_rank_change_notification_sent(self, event_id: int) -> None:
        with self._sessions.begin() as session:
            record = session.get(GuildRankChangeRecord, event_id)
            if record is not None:
                record.notification_sent = True
                LOGGER.debug(
                    "Marked guild-rank-change event %s notification sent",
                    event_id,
                )

    def get_tracked_trial_members(self) -> set[str]:
        with self._sessions() as session:
            results = {
                record.username
                for record in session.scalars(
                    select(TrackedTrialMemberRecord)
                ).all()
            }
            LOGGER.debug("Loaded %s tracked Trial members", len(results))
            return results

    def get_tracked_trial_member_times(self) -> dict[str, datetime]:
        with self._sessions() as session:
            records = session.scalars(select(TrackedTrialMemberRecord)).all()
            results: dict[str, datetime] = {}
            for record in records:
                try:
                    tracked_at = datetime.fromisoformat(record.tracked_at)
                except ValueError:
                    # Treat an unparsable timestamp as just-tracked so the member
                    # is not prematurely listed as overdue for kicking.
                    tracked_at = datetime.now(UTC)
                if tracked_at.tzinfo is None:
                    tracked_at = tracked_at.replace(tzinfo=UTC)
                results[record.username] = tracked_at.astimezone(UTC)
            LOGGER.debug("Loaded %s tracked Trial member times", len(results))
            return results

    def is_trial_member_tracked(self, username: str) -> bool:
        with self._sessions() as session:
            return session.get(TrackedTrialMemberRecord, username) is not None

    def toggle_trial_member_tracking(
        self,
        username: str,
        tracked_by_discord_user_id: int | None = None,
        event_time: datetime | None = None,
    ) -> bool:
        with self._sessions.begin() as session:
            record = session.get(TrackedTrialMemberRecord, username)
            if record is not None:
                session.delete(record)
                now_tracked = False
            else:
                session.add(
                    TrackedTrialMemberRecord(
                        username=username,
                        tracked_by_discord_user_id=tracked_by_discord_user_id,
                        tracked_at=(event_time or datetime.now(UTC)).isoformat(),
                    )
                )
                now_tracked = True
        LOGGER.debug(
            "Toggled Trial member warning tracking; now_tracked=%s",
            now_tracked,
        )
        return now_tracked

    def untrack_trial_member(self, username: str) -> None:
        with self._sessions.begin() as session:
            record = session.get(TrackedTrialMemberRecord, username)
            if record is not None:
                session.delete(record)
                LOGGER.debug("Removed Trial member warning tracking")

    def get_trial_forum_index(self) -> dict[int, TrialForumPost]:
        with self._sessions() as session:
            records = session.scalars(select(TrialForumPostRecord)).all()
            results = {
                record.thread_id: _to_trial_forum_post(record)
                for record in records
            }
            LOGGER.debug("Loaded %s Trial forum index posts", len(results))
            return results

    def get_trial_forum_watermark(self) -> datetime | None:
        with self._sessions() as session:
            record = session.get(SettingRecord, TRIAL_FORUM_INDEX_WATERMARK_KEY)
            if record is None:
                return None
            try:
                parsed = datetime.fromisoformat(record.value)
            except ValueError:
                LOGGER.warning("Discarding unparsable Trial forum index watermark")
                return None
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed.astimezone(UTC)

    def set_trial_forum_watermark(self, value: datetime) -> None:
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        normalized = value.astimezone(UTC).isoformat()
        with self._sessions.begin() as session:
            record = session.get(SettingRecord, TRIAL_FORUM_INDEX_WATERMARK_KEY)
            if record is None:
                session.add(
                    SettingRecord(
                        key=TRIAL_FORUM_INDEX_WATERMARK_KEY,
                        value=normalized,
                    )
                )
            else:
                record.value = normalized
        LOGGER.debug("Updated Trial forum index watermark")

    def upsert_trial_forum_posts(self, posts: list[TrialForumPost]) -> None:
        if not posts:
            return
        with self._sessions.begin() as session:
            for post in posts:
                record = session.get(TrialForumPostRecord, post.thread_id)
                indexed_at = datetime.now(UTC).isoformat()
                if record is None:
                    session.add(
                        TrialForumPostRecord(
                            thread_id=post.thread_id,
                            owner_id=post.owner_id,
                            normalized_content=post.normalized_content,
                            last_activity=post.last_activity,
                            indexed_at=indexed_at,
                        )
                    )
                else:
                    record.owner_id = post.owner_id
                    record.normalized_content = post.normalized_content
                    record.last_activity = post.last_activity
                    record.indexed_at = indexed_at
        LOGGER.debug("Upserted %s Trial forum index posts", len(posts))

    def delete_trial_forum_posts(self, thread_ids: set[int]) -> None:
        if not thread_ids:
            return
        with self._sessions.begin() as session:
            session.execute(
                delete(TrialForumPostRecord).where(
                    TrialForumPostRecord.thread_id.in_(thread_ids)
                )
            )
        LOGGER.debug("Deleted %s Trial forum index posts", len(thread_ids))

    def clear_trial_forum_index(self) -> None:
        with self._sessions.begin() as session:
            session.execute(delete(TrialForumPostRecord))
            watermark = session.get(SettingRecord, TRIAL_FORUM_INDEX_WATERMARK_KEY)
            if watermark is not None:
                session.delete(watermark)
        LOGGER.debug("Cleared Trial forum index")

    def mark_milestone_notification_sent(self, threshold: int) -> None:
        with self._sessions.begin() as session:
            record = session.get(RaffleMilestoneRecord, threshold)
            if record is not None:
                record.notification_sent = True
                LOGGER.debug("Marked raffle milestone %s notification sent", threshold)

    def _process_deposit(
        self,
        session: Session,
        deposit: RaffleDeposit,
    ) -> None:
        if session.get(RaffleDepositRecord, deposit.event_id) is not None:
            LOGGER.debug("Skipping duplicate raffle deposit event %s", deposit.event_id)
            return

        total = session.get(RaffleTotalRecord, deposit.username)
        gold_tickets = total.gold_raffle_tickets if total else 0
        tickets_awarded = min(
            deposit.raffle_tickets,
            MAX_GOLD_RAFFLE_TICKETS - gold_tickets,
        )
        session.add(
            RaffleDepositRecord(
                event_id=deposit.event_id,
                username=deposit.username,
                coins_deposited=deposit.coins_deposited,
                raffle_tickets=tickets_awarded,
                event_time=deposit.event_time,
            )
        )
        if total is None:
            total = RaffleTotalRecord(
                username=deposit.username,
                coins_deposited=deposit.coins_deposited,
                raffle_tickets=tickets_awarded,
                gold_raffle_tickets=tickets_awarded,
                manual_raffle_tickets=0,
            )
            session.add(total)
        else:
            total.coins_deposited += deposit.coins_deposited
            total.raffle_tickets += tickets_awarded
            total.gold_raffle_tickets += tickets_awarded
        LOGGER.debug(
            "Processed raffle deposit event %s; tickets_awarded=%s",
            deposit.event_id,
            tickets_awarded,
        )

    def _create_reached_milestones(self, session: Session) -> None:
        purchased_tickets = sum(
            session.scalars(select(RaffleTotalRecord.gold_raffle_tickets)).all()
        )
        for tier in self._reward_tiers:
            if purchased_tickets < tier.threshold:
                break
            if session.get(RaffleMilestoneRecord, tier.threshold) is None:
                session.add(
                    RaffleMilestoneRecord(
                        threshold=tier.threshold,
                        tier_name=tier.name,
                    )
                )

    def _migrate_legacy_totals(self) -> None:
        migrated = 0
        with self._sessions.begin() as session:
            for total in session.scalars(select(RaffleTotalRecord)).all():
                capped_tickets = min(
                    total.raffle_tickets,
                    MAX_GOLD_RAFFLE_TICKETS,
                )
                total.raffle_tickets = capped_tickets
                total.gold_raffle_tickets = capped_tickets
                migrated += 1
        LOGGER.debug("Migrated %s legacy raffle totals", migrated)

    def _apply_manual_ticket_cap_once(self) -> None:
        updated = 0
        with self._sessions.begin() as session:
            if session.get(SettingRecord, MANUAL_TICKET_CAP_MIGRATION_KEY) is not None:
                return
            for total in session.scalars(
                select(RaffleTotalRecord).where(
                    RaffleTotalRecord.manual_raffle_tickets
                    > MAX_MANUAL_RAFFLE_TICKETS
                )
            ).all():
                total.manual_raffle_tickets = MAX_MANUAL_RAFFLE_TICKETS
                total.raffle_tickets = (
                    total.gold_raffle_tickets + MAX_MANUAL_RAFFLE_TICKETS
                )
                updated += 1
            session.add(
                SettingRecord(
                    key=MANUAL_TICKET_CAP_MIGRATION_KEY,
                    value="complete",
                )
            )
        LOGGER.info("Applied one-time free-ticket cap; updated_users=%s", updated)

    def _bind_guild(self, guild_id: str) -> None:
        with self._sessions.begin() as session:
            record = session.get(SettingRecord, "guild_id")
            if record is not None and record.value != guild_id:
                raise ValueError(
                    "Raffle database belongs to a different guild; "
                    "use a different RAFFLE_DB_PATH"
                )
            if record is None:
                session.add(SettingRecord(key="guild_id", value=guild_id))
        LOGGER.debug("Validated raffle database guild binding")


def _to_raffle_deposit(record: RaffleDepositRecord) -> RaffleDeposit:
    return RaffleDeposit(
        event_id=record.event_id,
        username=record.username,
        coins_deposited=record.coins_deposited,
        raffle_tickets=record.raffle_tickets,
        event_time=record.event_time,
    )


def _to_guild_leave(record: GuildLeaveRecord) -> GuildLeave:
    return GuildLeave(
        event_id=record.event_id,
        username=record.username,
        event_time=record.event_time,
        kicked_by=record.kicked_by,
    )


def _to_guild_join(record: GuildJoinRecord) -> GuildJoin:
    return GuildJoin(
        event_id=record.event_id,
        username=record.username,
        event_time=record.event_time,
    )


def _to_guild_invite(record: GuildInviteRecord) -> GuildInvite:
    return GuildInvite(
        event_id=record.event_id,
        username=record.username,
        event_time=record.event_time,
        invited_by=record.invited_by,
    )


def _to_trial_forum_post(record: TrialForumPostRecord) -> TrialForumPost:
    return TrialForumPost(
        thread_id=record.thread_id,
        owner_id=record.owner_id,
        normalized_content=record.normalized_content,
        last_activity=record.last_activity,
    )


def _to_guild_rank_change(record: GuildRankChangeRecord) -> GuildRankChange:
    return GuildRankChange(
        event_id=record.event_id,
        username=record.username,
        old_rank=record.old_rank,
        new_rank=record.new_rank,
        event_time=record.event_time,
        changed_by=record.changed_by,
    )


def _to_raffle_milestone(record: RaffleMilestoneRecord) -> RaffleMilestone:
    return RaffleMilestone(
        threshold=record.threshold,
        tier_name=record.tier_name,
    )


def _to_raffle_total(record: RaffleTotalRecord) -> RaffleTotal:
    return RaffleTotal(
        username=record.username,
        coins_deposited=record.coins_deposited,
        raffle_tickets=record.raffle_tickets,
        gold_raffle_tickets=record.gold_raffle_tickets,
        manual_raffle_tickets=record.manual_raffle_tickets,
    )


def _to_raffle_result(session: Session, record: RaffleRunRecord) -> RaffleResult:
    winner_records = session.scalars(
        select(RaffleRunWinnerRecord)
        .where(RaffleRunWinnerRecord.run_id == record.run_id)
        .order_by(RaffleRunWinnerRecord.draw_position)
    ).all()
    # Each win removes one of the winner's tickets, so the tickets held at a
    # given draw are the run entry's tickets minus their earlier wins.
    entry_tickets = {
        entry.username: entry.raffle_tickets
        for entry in session.scalars(
            select(RaffleRunEntryRecord).where(
                RaffleRunEntryRecord.run_id == record.run_id
            )
        ).all()
    }
    prior_wins: dict[str, int] = {}
    winner_models: list[RaffleWinner] = []
    for winner in winner_records:
        initial_tickets = entry_tickets.get(winner.username)
        tickets_held = (
            initial_tickets - prior_wins.get(winner.username, 0)
            if initial_tickets is not None
            else None
        )
        prior_wins[winner.username] = prior_wins.get(winner.username, 0) + 1
        winner_models.append(
            RaffleWinner(
                username=winner.username,
                winning_ticket=winner.winning_ticket,
                tickets_before_draw=winner.tickets_before_draw,
                tickets_held=tickets_held,
            )
        )
    winners = tuple(winner_models)
    if not winners:
        winners = (
            RaffleWinner(
                username=record.winner,
                winning_ticket=record.winning_ticket,
                tickets_before_draw=record.total_tickets,
            ),
        )
    return RaffleResult(
        run_id=record.run_id,
        winners=winners,
        total_tickets=record.total_tickets,
        purchased_tickets=record.purchased_tickets,
        free_tickets=record.free_tickets,
    )


def _validate_reward_tiers(
    tiers: tuple[RaffleRewardTier, ...],
) -> tuple[RaffleRewardTier, ...]:
    ordered = tuple(sorted(tiers, key=lambda tier: tier.threshold))
    thresholds = [tier.threshold for tier in ordered]
    if any(threshold <= 0 for threshold in thresholds):
        raise ValueError("Raffle reward tier thresholds must be greater than zero")
    if len(thresholds) != len(set(thresholds)):
        raise ValueError("Raffle reward tier thresholds must be unique")
    if any(not tier.name.strip() for tier in ordered):
        raise ValueError("Raffle reward tier names must not be blank")
    return ordered


def _validate_draw_tiers(
    tiers: tuple[RaffleDrawTier, ...],
) -> tuple[RaffleDrawTier, ...]:
    ordered = tuple(sorted(tiers, key=lambda tier: tier.minimum_purchased_tickets))
    thresholds = [tier.minimum_purchased_tickets for tier in ordered]
    if not thresholds or thresholds[0] != 0:
        raise ValueError("Raffle draw tiers must start at zero purchased tickets")
    if len(thresholds) != len(set(thresholds)):
        raise ValueError("Raffle draw tier thresholds must be unique")
    if any(tier.winner_count <= 0 for tier in ordered):
        raise ValueError("Raffle draw winner counts must be greater than zero")
    return ordered


def _winner_count_for_purchased_tickets(
    purchased_tickets: int,
    draw_tiers: tuple[RaffleDrawTier, ...],
) -> int:
    winner_count = draw_tiers[0].winner_count
    for tier in draw_tiers:
        if purchased_tickets < tier.minimum_purchased_tickets:
            break
        winner_count = tier.winner_count
    return winner_count
