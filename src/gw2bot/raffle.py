from __future__ import annotations

import logging
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from gw2bot.database import (
    FeastAlertRecord,
    GuildLeaveRecord,
    RaffleDepositRecord,
    RaffleRunEntryRecord,
    RaffleRunRecord,
    RaffleTotalRecord,
    SettingRecord,
    create_database_engine,
    initialize_database,
)

LOGGER = logging.getLogger(__name__)

COPPER_PER_GOLD = 10_000
MAX_GOLD_RAFFLE_TICKETS = 10
MAX_MANUAL_RAFFLE_TICKETS = 3


@dataclass(frozen=True, slots=True)
class RaffleDeposit:
    event_id: int
    username: str
    coins_deposited: int
    raffle_tickets: int
    event_time: str

    @property
    def message(self) -> str:
        gold = format_gold(self.coins_deposited)
        return (
            f"{self.username} deposited {gold} gold and purchased "
            f"{self.raffle_tickets} raffle tickets"
        )


@dataclass(frozen=True, slots=True)
class RaffleTotal:
    username: str
    coins_deposited: int
    raffle_tickets: int
    gold_raffle_tickets: int
    manual_raffle_tickets: int


@dataclass(frozen=True, slots=True)
class RaffleResult:
    run_id: int
    winner: str
    winning_ticket: int
    total_tickets: int


@dataclass(frozen=True, slots=True)
class GuildLeave:
    event_id: int
    username: str
    event_time: str

    @property
    def message(self) -> str:
        return f"{self.username} has left the guild."


class RaffleStore:
    def __init__(self, database_path: str, guild_id: str):
        LOGGER.debug("Opening raffle store")
        self._engine = create_database_engine(database_path)
        added_columns = initialize_database(self._engine)
        self._sessions = sessionmaker(self._engine, expire_on_commit=False)
        if "gold_raffle_tickets" in added_columns:
            self._migrate_legacy_totals()
        try:
            self._bind_guild(guild_id)
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

    def process_events(self, events: list[dict[str, Any]]) -> None:
        processed = 0
        deposits = 0
        leaves = 0
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
                    self._process_deposit(session, deposit)
                    deposits += 1

                leave = parse_guild_leave(event)
                if (
                    leave is not None
                    and session.get(GuildLeaveRecord, leave.event_id) is None
                ):
                    session.add(
                        GuildLeaveRecord(
                            event_id=leave.event_id,
                            username=leave.username,
                            event_time=leave.event_time,
                        )
                    )
                    leaves += 1

                cursor = event_id
                processed += 1

            cursor_record.value = str(cursor)
        LOGGER.debug(
            "Processed guild log events; fetched=%s new=%s deposits=%s leaves=%s cursor=%s",
            len(events),
            processed,
            deposits,
            leaves,
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

    def get_totals(self) -> list[RaffleTotal]:
        statement = select(RaffleTotalRecord).order_by(RaffleTotalRecord.username)
        with self._sessions() as session:
            results = [
                _to_raffle_total(record)
                for record in session.scalars(statement).all()
            ]
            LOGGER.debug("Loaded %s raffle totals", len(results))
            return results

    def add_manual_ticket(self, username: str) -> RaffleTotal:
        with self._sessions.begin() as session:
            total = session.get(RaffleTotalRecord, username)
            manual_tickets = total.manual_raffle_tickets if total else 0
            if manual_tickets >= MAX_MANUAL_RAFFLE_TICKETS:
                raise ValueError(
                    f"{username} already has the maximum of "
                    f"{MAX_MANUAL_RAFFLE_TICKETS} manually added tickets"
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
            result = _to_raffle_total(total)
        LOGGER.debug(
            "Added manual raffle ticket; current_tickets=%s manual_tickets=%s",
            result.raffle_tickets,
            result.manual_raffle_tickets,
        )
        return result

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

            winning_ticket = randbelow(total_tickets) + 1
            cursor = 0
            winner = ""
            for total in totals:
                cursor += total.raffle_tickets
                if winning_ticket <= cursor:
                    winner = total.username
                    break

            run = RaffleRunRecord(
                winner=winner,
                winning_ticket=winning_ticket,
                total_tickets=total_tickets,
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
            for total in totals:
                total.raffle_tickets = 0
                total.gold_raffle_tickets = 0
                total.manual_raffle_tickets = 0

        result = RaffleResult(
            run_id=run_id,
            winner=winner,
            winning_ticket=winning_ticket,
            total_tickets=total_tickets,
        )
        LOGGER.debug(
            "Created raffle run %s; participants=%s total_tickets=%s",
            run_id,
            len(totals),
            total_tickets,
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
            result = _to_raffle_result(record) if record is not None else None
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

    def mark_leave_notification_sent(self, event_id: int) -> None:
        with self._sessions.begin() as session:
            record = session.get(GuildLeaveRecord, event_id)
            if record is not None:
                record.notification_sent = True
                LOGGER.debug("Marked guild-leave event %s notification sent", event_id)

    def _process_deposit(self, session: Session, deposit: RaffleDeposit) -> None:
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
            session.add(
                RaffleTotalRecord(
                    username=deposit.username,
                    coins_deposited=deposit.coins_deposited,
                    raffle_tickets=tickets_awarded,
                    gold_raffle_tickets=tickets_awarded,
                    manual_raffle_tickets=0,
                )
            )
        else:
            total.coins_deposited += deposit.coins_deposited
            total.raffle_tickets += tickets_awarded
            total.gold_raffle_tickets += tickets_awarded
        LOGGER.debug(
            "Processed raffle deposit event %s; tickets_awarded=%s",
            deposit.event_id,
            tickets_awarded,
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
    )


def _to_raffle_total(record: RaffleTotalRecord) -> RaffleTotal:
    return RaffleTotal(
        username=record.username,
        coins_deposited=record.coins_deposited,
        raffle_tickets=record.raffle_tickets,
        gold_raffle_tickets=record.gold_raffle_tickets,
        manual_raffle_tickets=record.manual_raffle_tickets,
    )


def _to_raffle_result(record: RaffleRunRecord) -> RaffleResult:
    return RaffleResult(
        run_id=record.run_id,
        winner=record.winner,
        winning_ticket=record.winning_ticket,
        total_tickets=record.total_tickets,
    )


def parse_gold_deposit(event: dict[str, Any]) -> RaffleDeposit | None:
    coins = int(event.get("coins", 0))
    if (
        event.get("type") != "stash"
        or event.get("operation") != "deposit"
        or not event.get("user")
        or coins <= 0
    ):
        return None

    return RaffleDeposit(
        event_id=int(event["id"]),
        username=str(event["user"]),
        coins_deposited=coins,
        raffle_tickets=coins // COPPER_PER_GOLD,
        event_time=str(event.get("time", "")),
    )


def parse_guild_leave(event: dict[str, Any]) -> GuildLeave | None:
    if not event.get("user"):
        return None
    # GW2 reports a voluntary departure as a self-kick.
    if (
        event.get("type") == "kick"
        and event.get("kicked_by")
        and event["kicked_by"] != event["user"]
    ):
        return None
    if event.get("type") not in {"kick", "left"}:
        return None
    return GuildLeave(
        event_id=int(event["id"]),
        username=str(event["user"]),
        event_time=str(event.get("time", "")),
    )


def format_gold(coins: int) -> str:
    whole, remainder = divmod(coins, COPPER_PER_GOLD)
    if remainder == 0:
        return str(whole)
    return f"{whole}.{remainder:04d}".rstrip("0")
