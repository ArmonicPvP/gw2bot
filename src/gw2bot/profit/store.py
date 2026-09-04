from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import delete, func, insert, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, sessionmaker

from gw2bot.database import (
    ProfitApiKeyRecord,
    ProfitCacheSyncRecord,
    ProfitItemRecord,
    ProfitLotCheckpointIndexRecord,
    ProfitLotCheckpointRecord,
    ProfitOpenLotRecord,
    ProfitOrderExclusionRecord,
    ProfitPreferenceRecord,
    ProfitRollupRecord,
    ProfitRollupStateRecord,
    ProfitTransactionRecord,
    create_database_engine,
    initialize_database,
)
from gw2bot.logging_setup import SecretRegistry
from gw2bot.profit.models import (
    BuyLot,
    ItemDayProfit,
    Transaction,
    parse_gw2_time,
)
from gw2bot.settings.crypto import SettingsCipher

LOGGER = logging.getLogger(__name__)

HISTORY_KINDS = frozenset({"history_buys", "history_sells"})
CURRENT_KINDS = frozenset({"current_sells", "current_buys"})
TRANSACTION_KINDS = HISTORY_KINDS | CURRENT_KINDS
MIN_REPORT_DAYS = 1

# How many month-boundary lot snapshots one member keeps. Two years of them
# is enough to rewind any late arrival the GW2 API can still be reporting,
# and bounds what an inactive account costs to store.
MAX_LOT_CHECKPOINTS = 24

# An item's name is fixed for the life of the game build, so it is cached for
# a month rather than for the five minutes a transaction snapshot lasts. This
# is what keeps a report from asking /v2/items about the same few thousand
# items every single time it is opened.
ITEM_NAME_TTL_SECONDS = 30 * 24 * 60 * 60

# The GW2 history endpoints only reach about ninety days back, so everything
# older than that exists solely because this store kept it. Nothing is dropped
# any more, and the window a member may ask for is bounded only by how long the
# bot has been collecting for them.
MAX_REPORT_DAYS = 3650


@dataclass(frozen=True, slots=True)
class ProfitApiKeySnapshot:
    api_key: str
    generation: str


@dataclass(frozen=True, slots=True)
class RollupState:
    """How far the stored rollups reach, and when they were last computed."""

    computed_through: datetime | None
    computed_at: datetime | None


@dataclass(frozen=True, slots=True)
class SyncState:
    """How far one collection has been read for one member.

    ``synced_through`` is the newest transaction ever stored, and is what an
    incremental refresh stops at. ``backfilled`` says the pages behind it were
    read too, so a member whose store predates the watermark is filled in once
    rather than being left with only the history the old retention kept.
    """

    synced_through: datetime | None
    backfilled: bool


class ProfitStore:
    """Per-member encrypted credentials and Trading Post response cache."""

    def __init__(
        self,
        database_path: str,
        cipher: SettingsCipher,
        secrets: SecretRegistry | None = None,
    ) -> None:
        self._cipher = cipher
        self._secrets = SecretRegistry() if secrets is None else secrets
        self._engine = create_database_engine(database_path)
        initialize_database(self._engine)
        # Construction only ensures the shared schema exists. Drop that
        # setup connection now; a bot that has not used /profit yet should
        # not add another Windows file handle for test harnesses or embedders
        # to account for. Sessions reconnect on first real use.
        self._engine.dispose()
        self._sessions = sessionmaker(self._engine, expire_on_commit=False)
        LOGGER.debug("Profit store initialized")

    def close(self) -> None:
        LOGGER.debug("Closing profit store")
        self._engine.dispose()

    def set_api_key(self, discord_user_id: int, api_key: str) -> None:
        # Register before encryption or persistence does anything that might
        # log. Registration is deliberately one-way, like /settings secrets.
        self._secrets.add(api_key)
        encrypted = self._cipher.encrypt(api_key)
        now = datetime.now(UTC).isoformat()
        created = False
        with self._sessions.begin() as session:
            record = session.get(ProfitApiKeyRecord, discord_user_id)
            if record is None:
                session.add(
                    ProfitApiKeyRecord(
                        discord_user_id=discord_user_id,
                        encrypted_api_key=encrypted,
                        created_at=now,
                        updated_at=now,
                    )
                )
                created = True
            else:
                record.encrypted_api_key = encrypted
                record.updated_at = now
            _clear_member_cache(session, discord_user_id)
        LOGGER.debug(
            "Stored profit API key; user_id=%s created=%s "
            "cache_cleared=%s characters=%s",
            discord_user_id,
            created,
            True,
            len(api_key),
        )

    def get_api_key(self, discord_user_id: int) -> str | None:
        snapshot = self.get_api_key_snapshot(discord_user_id)
        return snapshot.api_key if snapshot is not None else None

    def get_api_key_snapshot(
        self,
        discord_user_id: int,
    ) -> ProfitApiKeySnapshot | None:
        with self._sessions() as session:
            record = session.get(ProfitApiKeyRecord, discord_user_id)
        if record is None:
            LOGGER.debug(
                "Read profit API key; user_id=%s present=false",
                discord_user_id,
            )
            return None
        api_key = self._cipher.decrypt(
            record.encrypted_api_key,
            "profit_api_key",
        )
        # A successfully recovered per-user key can now be used in an HTTP
        # request, so arm the shared formatter before returning it.
        self._secrets.add(api_key)
        LOGGER.debug(
            "Read profit API key; user_id=%s present=true decrypted=%s",
            discord_user_id,
            api_key is not None,
        )
        if api_key is None:
            return None
        return ProfitApiKeySnapshot(
            api_key=api_key,
            generation=record.updated_at,
        )

    def delete_api_key(self, discord_user_id: int) -> bool:
        with self._sessions.begin() as session:
            record = session.get(ProfitApiKeyRecord, discord_user_id)
            removed = record is not None
            if record is not None:
                session.delete(record)
            _clear_member_cache(session, discord_user_id)
            # /profit deletekey is how a member takes their data back out of
            # the bot, so the remembered window and excluded items go with the
            # key. Replacing a key with set_api_key deliberately keeps both.
            _clear_member_preferences(session, discord_user_id)
        LOGGER.debug(
            "Deleted profit API key; user_id=%s removed=%s "
            "cache_cleared=true preferences_cleared=true",
            discord_user_id,
            removed,
        )
        return removed

    def get_report_days(self, discord_user_id: int) -> int | None:
        """Return the member's remembered report window, if they set one."""
        with self._sessions() as session:
            record = session.get(ProfitPreferenceRecord, discord_user_id)
        days = None if record is None else record.report_days
        if days is not None and not MIN_REPORT_DAYS <= days <= MAX_REPORT_DAYS:
            # A row written by another release, or edited by hand. Read it as
            # unset rather than serving a window the report would refuse.
            LOGGER.warning(
                "Ignored a stored profit report window; user_id=%s "
                "reason=out-of-range",
                discord_user_id,
            )
            days = None
        LOGGER.debug(
            "Read profit report window; user_id=%s stored=%s",
            discord_user_id,
            days is not None,
        )
        return days

    def set_report_days(
        self,
        discord_user_id: int,
        days: int,
        *,
        now: datetime | None = None,
    ) -> None:
        if not MIN_REPORT_DAYS <= days <= MAX_REPORT_DAYS:
            raise ValueError("Profit report days must be between 1 and 90")
        updated_at = (datetime.now(UTC) if now is None else now).isoformat()
        with self._sessions.begin() as session:
            record = session.get(ProfitPreferenceRecord, discord_user_id)
            if record is None:
                session.add(
                    ProfitPreferenceRecord(
                        discord_user_id=discord_user_id,
                        report_days=days,
                        updated_at=updated_at,
                    )
                )
            else:
                record.report_days = days
                record.updated_at = updated_at
        LOGGER.debug(
            "Stored profit report window; user_id=%s days=%s",
            discord_user_id,
            days,
        )

    def get_excluded_order_items(self, discord_user_id: int) -> frozenset[int]:
        with self._sessions() as session:
            item_ids = frozenset(
                session.scalars(
                    select(ProfitOrderExclusionRecord.item_id).where(
                        ProfitOrderExclusionRecord.discord_user_id
                        == discord_user_id
                    )
                )
            )
        LOGGER.debug(
            "Read profit order exclusions; user_id=%s items=%s",
            discord_user_id,
            len(item_ids),
        )
        return item_ids

    def set_order_exclusion(
        self,
        discord_user_id: int,
        item_id: int,
        excluded: bool,
        *,
        now: datetime | None = None,
    ) -> bool:
        """Exclude or restore one item; return whether the row changed."""
        if item_id <= 0:
            raise ValueError("Profit order exclusions need a positive item id")
        created_at = (datetime.now(UTC) if now is None else now).isoformat()
        with self._sessions.begin() as session:
            record = session.get(
                ProfitOrderExclusionRecord,
                (discord_user_id, item_id),
            )
            if excluded:
                changed = record is None
                if record is None:
                    session.add(
                        ProfitOrderExclusionRecord(
                            discord_user_id=discord_user_id,
                            item_id=item_id,
                            created_at=created_at,
                        )
                    )
            else:
                changed = record is not None
                if record is not None:
                    session.delete(record)
        LOGGER.debug(
            "Stored profit order exclusion; user_id=%s item_id=%s "
            "excluded=%s changed=%s",
            discord_user_id,
            item_id,
            excluded,
            changed,
        )
        return changed

    def is_cache_fresh(
        self,
        discord_user_id: int,
        cache_kind: str,
        ttl_seconds: int,
        *,
        now: datetime | None = None,
    ) -> bool:
        _require_kind(cache_kind)
        with self._sessions() as session:
            record = session.get(
                ProfitCacheSyncRecord,
                (discord_user_id, cache_kind),
            )
        if record is None:
            LOGGER.debug(
                "Checked profit cache; user_id=%s kind=%s fresh=false "
                "reason=missing",
                discord_user_id,
                cache_kind,
            )
            return False
        checked_at = datetime.now(UTC) if now is None else now
        try:
            age = (checked_at - parse_gw2_time(record.synced_at)).total_seconds()
        except (TypeError, ValueError):
            LOGGER.warning(
                "Checked profit cache; user_id=%s kind=%s fresh=false "
                "reason=invalid-timestamp",
                discord_user_id,
                cache_kind,
            )
            return False
        fresh = 0 <= age < ttl_seconds
        LOGGER.debug(
            "Checked profit cache; user_id=%s kind=%s fresh=%s age_seconds=%s",
            discord_user_id,
            cache_kind,
            fresh,
            max(0, int(age)),
        )
        return fresh

    def get_sync_state(
        self,
        discord_user_id: int,
        cache_kind: str,
    ) -> SyncState:
        """Return how far this collection has been read, if at all."""
        _require_kind(cache_kind)
        with self._sessions() as session:
            record = session.get(
                ProfitCacheSyncRecord,
                (discord_user_id, cache_kind),
            )
        if record is None:
            return SyncState(None, False)
        synced_through: datetime | None = None
        if record.synced_through is not None:
            try:
                synced_through = parse_gw2_time(record.synced_through)
            except (TypeError, ValueError):
                LOGGER.warning(
                    "Ignored a profit sync watermark; user_id=%s kind=%s "
                    "reason=invalid-timestamp",
                    discord_user_id,
                    cache_kind,
                )
        state = SyncState(synced_through, record.backfilled)
        LOGGER.debug(
            "Read profit sync state; user_id=%s kind=%s watermark=%s "
            "backfilled=%s",
            discord_user_id,
            cache_kind,
            synced_through is not None,
            state.backfilled,
        )
        return state

    def touch_cache(
        self,
        discord_user_id: int,
        cache_kind: str,
        *,
        now: datetime | None = None,
    ) -> None:
        _require_kind(cache_kind)
        synced_at = (datetime.now(UTC) if now is None else now).isoformat()
        with self._sessions.begin() as session:
            _touch_cache_record(
                session,
                discord_user_id,
                cache_kind,
                synced_at,
            )
        LOGGER.debug(
            "Updated profit cache marker; user_id=%s kind=%s",
            discord_user_id,
            cache_kind,
        )

    def store_transactions(
        self,
        discord_user_id: int,
        transaction_kind: str,
        transactions: list[Transaction],
        *,
        now: datetime | None = None,
    ) -> None:
        _require_kind(transaction_kind)
        stored_at = datetime.now(UTC) if now is None else now
        with self._sessions.begin() as session:
            _store_transaction_records(
                session,
                discord_user_id,
                transaction_kind,
                transactions,
                stored_at,
            )
        LOGGER.debug(
            "Stored profit transactions; user_id=%s kind=%s records=%s "
            "replace=%s",
            discord_user_id,
            transaction_kind,
            len(transactions),
            transaction_kind in CURRENT_KINDS,
        )

    def store_transaction_snapshot(
        self,
        discord_user_id: int,
        key_generation: str,
        collections: list[tuple[str, list[Transaction]]],
        *,
        backfilled: dict[str, bool] | None = None,
        now: datetime | None = None,
    ) -> bool:
        for transaction_kind, _ in collections:
            _require_kind(transaction_kind)
        backfilled = {} if backfilled is None else backfilled
        stored_at = datetime.now(UTC) if now is None else now
        accepted = False
        with self._sessions.begin() as session:
            key_record = session.get(ProfitApiKeyRecord, discord_user_id)
            if (
                key_record is not None
                and key_record.updated_at == key_generation
            ):
                for transaction_kind, transactions in collections:
                    _store_transaction_records(
                        session,
                        discord_user_id,
                        transaction_kind,
                        transactions,
                        stored_at,
                    )
                    _touch_cache_record(
                        session,
                        discord_user_id,
                        transaction_kind,
                        stored_at.isoformat(),
                        newest=(
                            max(
                                transaction.occurred_at
                                for transaction in transactions
                            )
                            if transactions
                            else None
                        ),
                        backfilled=backfilled.get(transaction_kind, False),
                    )
                accepted = True
        LOGGER.debug(
            "Stored profit transaction snapshot; user_id=%s accepted=%s "
            "collections=%s records=%s reason=%s",
            discord_user_id,
            accepted,
            len(collections),
            sum(len(transactions) for _, transactions in collections),
            "current-key" if accepted else "key-generation-changed",
        )
        return accepted

    def get_transactions(
        self,
        discord_user_id: int,
        transaction_kind: str,
        cutoff: datetime | None = None,
        *,
        after: datetime | None = None,
        at_or_after: datetime | None = None,
    ) -> list[Transaction]:
        _require_kind(transaction_kind)
        query = select(ProfitTransactionRecord).where(
            ProfitTransactionRecord.discord_user_id == discord_user_id,
            ProfitTransactionRecord.transaction_kind == transaction_kind,
        )
        if cutoff is not None:
            query = query.where(
                ProfitTransactionRecord.occurred_at >= cutoff.isoformat()
            )
        if after is not None:
            query = query.where(
                ProfitTransactionRecord.occurred_at > after.isoformat()
            )
        if at_or_after is not None:
            query = query.where(
                ProfitTransactionRecord.occurred_at >= at_or_after.isoformat()
            )
        query = query.order_by(ProfitTransactionRecord.occurred_at)
        with self._sessions() as session:
            records = list(session.scalars(query))
        transactions = [
            Transaction(
                transaction_id=record.transaction_id,
                item_id=record.item_id,
                price=record.price,
                quantity=record.quantity,
                occurred_at=parse_gw2_time(record.occurred_at),
            )
            for record in records
        ]
        LOGGER.debug(
            "Read profit transactions; user_id=%s kind=%s records=%s "
            "windowed=%s",
            discord_user_id,
            transaction_kind,
            len(transactions),
            cutoff is not None,
        )
        return transactions

    def get_earliest_transaction_at(
        self,
        discord_user_id: int,
    ) -> datetime | None:
        """Return the oldest trade held, which bounds a useful window."""
        with self._sessions() as session:
            oldest = session.scalar(
                select(func.min(ProfitTransactionRecord.occurred_at)).where(
                    ProfitTransactionRecord.discord_user_id
                    == discord_user_id,
                    ProfitTransactionRecord.transaction_kind.in_(
                        HISTORY_KINDS
                    ),
                )
            )
        if oldest is None:
            LOGGER.debug(
                "Read profit history start; user_id=%s present=false",
                discord_user_id,
            )
            return None
        try:
            earliest = parse_gw2_time(oldest)
        except (TypeError, ValueError):
            LOGGER.warning(
                "Ignored a profit history start; user_id=%s "
                "reason=invalid-timestamp",
                discord_user_id,
            )
            return None
        LOGGER.debug(
            "Read profit history start; user_id=%s present=true",
            discord_user_id,
        )
        return earliest

    def get_members_with_api_key(self) -> list[int]:
        """Every member the background sync has a key to work with."""
        with self._sessions() as session:
            members = sorted(
                session.scalars(select(ProfitApiKeyRecord.discord_user_id))
            )
        LOGGER.debug("Read profit key holders; members=%s", len(members))
        return members

    def get_known_item_ids(
        self,
        ttl_seconds: int,
        *,
        now: datetime | None = None,
    ) -> set[int]:
        """Item ids whose names are stored and still fresh."""
        checked_at = datetime.now(UTC) if now is None else now
        with self._sessions() as session:
            records = list(
                session.execute(
                    select(
                        ProfitItemRecord.item_id,
                        ProfitItemRecord.updated_at,
                    )
                )
            )
        known: set[int] = set()
        for item_id, updated_at in records:
            try:
                age = (
                    checked_at - parse_gw2_time(updated_at)
                ).total_seconds()
            except (TypeError, ValueError):
                continue
            if 0 <= age < ttl_seconds:
                known.add(item_id)
        LOGGER.debug(
            "Read known profit item names; stored=%s fresh=%s",
            len(records),
            len(known),
        )
        return known

    def get_rollup_state(self, discord_user_id: int) -> RollupState:
        """How far the stored rollups reach, and when they were computed."""
        with self._sessions() as session:
            record = session.get(ProfitRollupStateRecord, discord_user_id)
        if record is None:
            return RollupState(None, None)
        computed_through: datetime | None = None
        if record.computed_through is not None:
            try:
                computed_through = parse_gw2_time(record.computed_through)
            except (TypeError, ValueError):
                LOGGER.warning(
                    "Ignored a profit rollup watermark; user_id=%s "
                    "reason=invalid-timestamp",
                    discord_user_id,
                )
        computed_at: datetime | None = None
        try:
            computed_at = parse_gw2_time(record.computed_at)
        except (TypeError, ValueError):
            computed_at = None
        return RollupState(computed_through, computed_at)

    def get_late_arrival(
        self,
        discord_user_id: int,
        computed_through: datetime,
        computed_at: datetime,
    ) -> datetime | None:
        """The oldest trade stored since the last pass that predates it.

        History normally only grows at the newest end, and the watermark
        alone catches that. A trade that lands behind the watermark - a
        backfill reaching further than before, or a correction - would
        otherwise never be matched at all, because the newest transaction
        has not moved. This is what notices.
        """
        with self._sessions() as session:
            oldest = session.scalar(
                select(func.min(ProfitTransactionRecord.occurred_at)).where(
                    ProfitTransactionRecord.discord_user_id
                    == discord_user_id,
                    ProfitTransactionRecord.transaction_kind.in_(
                        HISTORY_KINDS
                    ),
                    ProfitTransactionRecord.occurred_at
                    <= computed_through.isoformat(),
                    ProfitTransactionRecord.updated_at
                    > computed_at.isoformat(),
                )
            )
        if oldest is None:
            return None
        try:
            arrival = parse_gw2_time(oldest)
        except (TypeError, ValueError):
            return None
        LOGGER.info(
            "Found a late Trading Post arrival; user_id=%s",
            discord_user_id,
        )
        return arrival

    def store_lot_checkpoint(
        self,
        discord_user_id: int,
        checkpoint_at: datetime,
        lots: dict[int, tuple[BuyLot, ...]],
    ) -> None:
        """Record what the member held at one boundary, replacing any there."""
        stamp = checkpoint_at.isoformat()
        with self._sessions.begin() as session:
            session.execute(
                delete(ProfitLotCheckpointRecord).where(
                    ProfitLotCheckpointRecord.discord_user_id
                    == discord_user_id,
                    ProfitLotCheckpointRecord.checkpoint_at == stamp,
                )
            )
            # Recorded whether or not there are lots: holding nothing is a
            # state a rematch can resume from just as well as holding
            # something.
            session.execute(
                sqlite_insert(ProfitLotCheckpointIndexRecord)
                .values(
                    discord_user_id=discord_user_id,
                    checkpoint_at=stamp,
                )
                .on_conflict_do_nothing(
                    index_elements=("discord_user_id", "checkpoint_at")
                )
            )
            rows = [
                {
                    "discord_user_id": discord_user_id,
                    "checkpoint_at": stamp,
                    "item_id": item_id,
                    "lot_index": index,
                    "remaining": lot.remaining,
                    "unit_price": lot.unit_price,
                    "occurred_at": lot.occurred_at.isoformat(),
                }
                for item_id, item_lots in lots.items()
                for index, lot in enumerate(item_lots)
            ]
            if rows:
                session.execute(insert(ProfitLotCheckpointRecord), rows)
            _trim_lot_checkpoints(session, discord_user_id)
        LOGGER.debug(
            "Stored a profit lot checkpoint; user_id=%s items=%s",
            discord_user_id,
            len(lots),
        )

    def get_lot_checkpoint_at_or_before(
        self,
        discord_user_id: int,
        moment: datetime,
    ) -> tuple[datetime, dict[int, tuple[BuyLot, ...]]] | None:
        """The latest checkpoint a rematch from ``moment`` can start at."""
        stamp = moment.isoformat()
        with self._sessions() as session:
            chosen = session.scalar(
                select(
                    func.max(ProfitLotCheckpointIndexRecord.checkpoint_at)
                ).where(
                    ProfitLotCheckpointIndexRecord.discord_user_id
                    == discord_user_id,
                    ProfitLotCheckpointIndexRecord.checkpoint_at <= stamp,
                )
            )
            if chosen is None:
                LOGGER.debug(
                    "No profit lot checkpoint reaches back far enough; "
                    "user_id=%s",
                    discord_user_id,
                )
                return None
            records = list(
                session.scalars(
                    select(ProfitLotCheckpointRecord)
                    .where(
                        ProfitLotCheckpointRecord.discord_user_id
                        == discord_user_id,
                        ProfitLotCheckpointRecord.checkpoint_at == chosen,
                    )
                    .order_by(ProfitLotCheckpointRecord.lot_index)
                )
            )
        try:
            checkpoint_at = parse_gw2_time(chosen)
        except (TypeError, ValueError):
            return None
        lots: dict[int, list[BuyLot]] = {}
        for record in records:
            try:
                occurred_at = parse_gw2_time(record.occurred_at)
            except (TypeError, ValueError):
                continue
            lots.setdefault(record.item_id, []).append(
                BuyLot(record.remaining, record.unit_price, occurred_at)
            )
        LOGGER.debug(
            "Read a profit lot checkpoint; user_id=%s items=%s",
            discord_user_id,
            len(lots),
        )
        return checkpoint_at, {
            item_id: tuple(rows) for item_id, rows in lots.items()
        }

    def discard_rollups_from(
        self,
        discord_user_id: int,
        boundary: datetime,
    ) -> None:
        """Drop everything computed at or after ``boundary`` so it can be redone."""
        stamp = boundary.isoformat()
        with self._sessions.begin() as session:
            session.execute(
                delete(ProfitRollupRecord).where(
                    ProfitRollupRecord.discord_user_id == discord_user_id,
                    ProfitRollupRecord.sold_day >= boundary.date().isoformat(),
                )
            )
            for record_type in (
                ProfitLotCheckpointRecord,
                ProfitLotCheckpointIndexRecord,
            ):
                session.execute(
                    delete(record_type).where(
                        record_type.discord_user_id == discord_user_id,
                        record_type.checkpoint_at > stamp,
                    )
                )
        LOGGER.info(
            "Discarded profit rollups for a rematch; user_id=%s",
            discord_user_id,
        )

    def get_newest_transaction_at(
        self,
        discord_user_id: int,
    ) -> datetime | None:
        """The newest trade held, which is what rollups must account for."""
        with self._sessions() as session:
            newest = session.scalar(
                select(func.max(ProfitTransactionRecord.occurred_at)).where(
                    ProfitTransactionRecord.discord_user_id
                    == discord_user_id,
                    ProfitTransactionRecord.transaction_kind.in_(
                        HISTORY_KINDS
                    ),
                )
            )
        if newest is None:
            return None
        try:
            return parse_gw2_time(newest)
        except (TypeError, ValueError):
            return None

    def store_rollups(
        self,
        discord_user_id: int,
        rollups: dict[tuple[int, str], ItemDayProfit],
        open_lots: dict[int, tuple[BuyLot, ...]],
        computed_through: datetime | None,
        *,
        now: datetime | None = None,
    ) -> None:
        """Replace this member's rollups with a freshly matched set.

        Written as one transaction: a half-replaced rollup would report
        profit that never happened, which is worse than none at all.
        """
        computed_at = (datetime.now(UTC) if now is None else now).isoformat()
        with self._sessions.begin() as session:
            session.execute(
                delete(ProfitRollupRecord).where(
                    ProfitRollupRecord.discord_user_id == discord_user_id
                )
            )
            session.execute(
                delete(ProfitOpenLotRecord).where(
                    ProfitOpenLotRecord.discord_user_id == discord_user_id
                )
            )
            if rollups:
                session.execute(
                    insert(ProfitRollupRecord),
                    [
                        {
                            "discord_user_id": discord_user_id,
                            "sold_day": sold_day,
                            "item_id": item_id,
                            "matched_quantity": totals.matched_quantity,
                            "cost": totals.cost,
                            "net_revenue": totals.net_revenue,
                            "profit": totals.profit,
                            "hold_seconds": totals.hold_seconds,
                        }
                        for (item_id, sold_day), totals in rollups.items()
                    ],
                )
            lot_rows = [
                {
                    "discord_user_id": discord_user_id,
                    "item_id": item_id,
                    "lot_index": index,
                    "remaining": lot.remaining,
                    "unit_price": lot.unit_price,
                    "occurred_at": lot.occurred_at.isoformat(),
                }
                for item_id, lots in open_lots.items()
                for index, lot in enumerate(lots)
            ]
            if lot_rows:
                session.execute(insert(ProfitOpenLotRecord), lot_rows)
            record = session.get(ProfitRollupStateRecord, discord_user_id)
            through = (
                None if computed_through is None
                else computed_through.isoformat()
            )
            if record is None:
                session.add(
                    ProfitRollupStateRecord(
                        discord_user_id=discord_user_id,
                        computed_through=through,
                        computed_at=computed_at,
                    )
                )
            else:
                record.computed_through = through
                record.computed_at = computed_at
        LOGGER.debug(
            "Stored profit rollups; user_id=%s rows=%s open_lots=%s",
            discord_user_id,
            len(rollups),
            len(lot_rows),
        )

    def merge_rollups(
        self,
        discord_user_id: int,
        rollups: dict[tuple[int, str], ItemDayProfit],
        open_lots: dict[int, tuple[BuyLot, ...]],
        computed_through: datetime,
        *,
        now: datetime | None = None,
    ) -> None:
        """Add one pass's matches to what is already stored.

        Days the pass touched are added to rather than replaced, because a
        sale today does not change what a sale last week realized. Open lots
        are replaced outright: they are the state carried to the next pass,
        and this pass has just recomputed it.
        """
        computed_at = (datetime.now(UTC) if now is None else now).isoformat()
        with self._sessions.begin() as session:
            if rollups:
                statement = sqlite_insert(ProfitRollupRecord)
                session.execute(
                    statement.on_conflict_do_update(
                        index_elements=(
                            "discord_user_id",
                            "sold_day",
                            "item_id",
                        ),
                        set_={
                            "matched_quantity": (
                                ProfitRollupRecord.matched_quantity
                                + statement.excluded.matched_quantity
                            ),
                            "cost": (
                                ProfitRollupRecord.cost
                                + statement.excluded.cost
                            ),
                            "net_revenue": (
                                ProfitRollupRecord.net_revenue
                                + statement.excluded.net_revenue
                            ),
                            "profit": (
                                ProfitRollupRecord.profit
                                + statement.excluded.profit
                            ),
                            "hold_seconds": (
                                ProfitRollupRecord.hold_seconds
                                + statement.excluded.hold_seconds
                            ),
                        },
                    ),
                    [
                        {
                            "discord_user_id": discord_user_id,
                            "sold_day": sold_day,
                            "item_id": item_id,
                            "matched_quantity": totals.matched_quantity,
                            "cost": totals.cost,
                            "net_revenue": totals.net_revenue,
                            "profit": totals.profit,
                            "hold_seconds": totals.hold_seconds,
                        }
                        for (item_id, sold_day), totals in rollups.items()
                    ],
                )
            session.execute(
                delete(ProfitOpenLotRecord).where(
                    ProfitOpenLotRecord.discord_user_id == discord_user_id
                )
            )
            lot_rows = [
                {
                    "discord_user_id": discord_user_id,
                    "item_id": item_id,
                    "lot_index": index,
                    "remaining": lot.remaining,
                    "unit_price": lot.unit_price,
                    "occurred_at": lot.occurred_at.isoformat(),
                }
                for item_id, lots in open_lots.items()
                for index, lot in enumerate(lots)
            ]
            if lot_rows:
                session.execute(insert(ProfitOpenLotRecord), lot_rows)
            record = session.get(ProfitRollupStateRecord, discord_user_id)
            if record is None:
                session.add(
                    ProfitRollupStateRecord(
                        discord_user_id=discord_user_id,
                        computed_through=computed_through.isoformat(),
                        computed_at=computed_at,
                    )
                )
            else:
                record.computed_through = computed_through.isoformat()
                record.computed_at = computed_at
        LOGGER.debug(
            "Merged profit rollups; user_id=%s rows=%s open_lots=%s",
            discord_user_id,
            len(rollups),
            len(lot_rows),
        )

    def get_rollups(
        self,
        discord_user_id: int,
        cutoff: datetime,
    ) -> list[tuple[int, str, ItemDayProfit]]:
        """Read the precomputed results from ``cutoff`` onwards."""
        with self._sessions() as session:
            records = list(
                session.scalars(
                    select(ProfitRollupRecord)
                    .where(
                        ProfitRollupRecord.discord_user_id
                        == discord_user_id,
                        ProfitRollupRecord.sold_day
                        >= cutoff.date().isoformat(),
                    )
                    .order_by(ProfitRollupRecord.sold_day)
                )
            )
        rows = [
            (
                record.item_id,
                record.sold_day,
                ItemDayProfit(
                    record.matched_quantity,
                    record.cost,
                    record.net_revenue,
                    record.profit,
                    record.hold_seconds,
                ),
            )
            for record in records
        ]
        LOGGER.debug(
            "Read profit rollups; user_id=%s rows=%s",
            discord_user_id,
            len(rows),
        )
        return rows

    def get_open_lots(
        self,
        discord_user_id: int,
    ) -> dict[int, tuple[BuyLot, ...]]:
        """The unmatched purchases the member still holds."""
        with self._sessions() as session:
            records = list(
                session.scalars(
                    select(ProfitOpenLotRecord)
                    .where(
                        ProfitOpenLotRecord.discord_user_id
                        == discord_user_id
                    )
                    .order_by(ProfitOpenLotRecord.lot_index)
                )
            )
        lots: dict[int, list[BuyLot]] = {}
        for record in records:
            try:
                occurred_at = parse_gw2_time(record.occurred_at)
            except (TypeError, ValueError):
                continue
            lots.setdefault(record.item_id, []).append(
                BuyLot(record.remaining, record.unit_price, occurred_at)
            )
        LOGGER.debug(
            "Read profit open lots; user_id=%s items=%s",
            discord_user_id,
            len(lots),
        )
        return {item_id: tuple(rows) for item_id, rows in lots.items()}

    def count_transactions(
        self,
        discord_user_id: int,
        transaction_kind: str,
        cutoff: datetime,
    ) -> int:
        """Count rows in a window without reading them into memory."""
        _require_kind(transaction_kind)
        with self._sessions() as session:
            total = session.scalar(
                select(func.count())
                .select_from(ProfitTransactionRecord)
                .where(
                    ProfitTransactionRecord.discord_user_id
                    == discord_user_id,
                    ProfitTransactionRecord.transaction_kind
                    == transaction_kind,
                    ProfitTransactionRecord.occurred_at
                    >= cutoff.isoformat(),
                )
            )
        return int(total or 0)

    def get_item_names(
        self,
        item_ids: set[int],
        ttl_seconds: int,
        *,
        now: datetime | None = None,
    ) -> dict[int, str]:
        if not item_ids:
            LOGGER.debug(
                "Read profit item names; requested=0 found=0 expired=0 "
                "invalid_timestamps=0"
            )
            return {}
        with self._sessions() as session:
            records = list(
                session.scalars(
                    select(ProfitItemRecord).where(
                        ProfitItemRecord.item_id.in_(item_ids)
                    )
                )
            )
        checked_at = datetime.now(UTC) if now is None else now
        names: dict[int, str] = {}
        expired = 0
        invalid_timestamps = 0
        for record in records:
            try:
                age = (
                    checked_at - parse_gw2_time(record.updated_at)
                ).total_seconds()
            except (TypeError, ValueError):
                invalid_timestamps += 1
                continue
            if 0 <= age < ttl_seconds:
                names[record.item_id] = record.name
            else:
                expired += 1
        LOGGER.debug(
            "Read profit item names; requested=%s found=%s expired=%s "
            "invalid_timestamps=%s",
            len(item_ids),
            len(names),
            expired,
            invalid_timestamps,
        )
        return names

    def store_item_names(
        self,
        names: dict[int, str],
        *,
        now: datetime | None = None,
    ) -> None:
        updated_at = (datetime.now(UTC) if now is None else now).isoformat()
        with self._sessions.begin() as session:
            if names:
                statement = sqlite_insert(ProfitItemRecord)
                session.execute(
                    statement.on_conflict_do_update(
                        index_elements=("item_id",),
                        set_={
                            "name": statement.excluded.name,
                            "updated_at": statement.excluded.updated_at,
                        },
                    ),
                    [
                        {
                            "item_id": item_id,
                            "name": name,
                            "updated_at": updated_at,
                        }
                        for item_id, name in names.items()
                    ],
                )
        LOGGER.debug("Stored profit item names; records=%s", len(names))


def _require_kind(transaction_kind: str) -> None:
    if transaction_kind not in TRANSACTION_KINDS:
        raise ValueError(f"Unknown profit transaction kind: {transaction_kind}")


def _touch_cache_record(
    session: Session,
    discord_user_id: int,
    cache_kind: str,
    synced_at: str,
    *,
    newest: datetime | None = None,
    backfilled: bool = False,
) -> None:
    record = session.get(
        ProfitCacheSyncRecord,
        (discord_user_id, cache_kind),
    )
    if record is None:
        session.add(
            ProfitCacheSyncRecord(
                discord_user_id=discord_user_id,
                cache_kind=cache_kind,
                synced_at=synced_at,
                synced_through=None if newest is None else newest.isoformat(),
                backfilled=backfilled,
            )
        )
        return
    record.synced_at = synced_at
    # The watermark only ever moves forward. An empty refresh, or one that
    # raced a older snapshot in, must not walk it back and make the next
    # refresh re-read pages this one already stored.
    if newest is not None:
        current: datetime | None = None
        if record.synced_through is not None:
            try:
                current = parse_gw2_time(record.synced_through)
            except (TypeError, ValueError):
                current = None
        if current is None or newest > current:
            record.synced_through = newest.isoformat()
    if backfilled:
        record.backfilled = True


def _store_transaction_records(
    session: Session,
    discord_user_id: int,
    transaction_kind: str,
    transactions: list[Transaction],
    stored_at: datetime,
) -> None:
    rows = [
        {
            "discord_user_id": discord_user_id,
            "transaction_kind": transaction_kind,
            "transaction_id": transaction.transaction_id,
            "item_id": transaction.item_id,
            "price": transaction.price,
            "quantity": transaction.quantity,
            "occurred_at": transaction.occurred_at.isoformat(),
            "updated_at": stored_at.isoformat(),
        }
        for transaction in transactions
    ]
    if transaction_kind in CURRENT_KINDS:
        session.execute(
            delete(ProfitTransactionRecord).where(
                ProfitTransactionRecord.discord_user_id == discord_user_id,
                ProfitTransactionRecord.transaction_kind == transaction_kind,
            )
        )
    if rows:
        statement = sqlite_insert(ProfitTransactionRecord)
        session.execute(
            statement.on_conflict_do_update(
                index_elements=(
                    "discord_user_id",
                    "transaction_kind",
                    "transaction_id",
                ),
                set_={
                    "item_id": statement.excluded.item_id,
                    "price": statement.excluded.price,
                    "quantity": statement.excluded.quantity,
                    "occurred_at": statement.excluded.occurred_at,
                    "updated_at": statement.excluded.updated_at,
                },
                # Every incremental fetch deliberately re-reads rows it
                # already has, and a completed transaction never changes.
                # Touching updated_at for those would make each refresh look
                # like history arriving late and send the rollups back to a
                # checkpoint every time. So updated_at means "when this row's
                # content was first seen", and an identical row is left
                # exactly as it was.
                where=(
                    (
                        ProfitTransactionRecord.item_id
                        != statement.excluded.item_id
                    )
                    | (
                        ProfitTransactionRecord.price
                        != statement.excluded.price
                    )
                    | (
                        ProfitTransactionRecord.quantity
                        != statement.excluded.quantity
                    )
                    | (
                        ProfitTransactionRecord.occurred_at
                        != statement.excluded.occurred_at
                    )
                ),
            ),
            rows,
        )


def _trim_lot_checkpoints(session: Session, discord_user_id: int) -> None:
    """Keep only the newest checkpoints; older ones can never be rewound to."""
    stamps = sorted(
        session.scalars(
            select(ProfitLotCheckpointIndexRecord.checkpoint_at).where(
                ProfitLotCheckpointIndexRecord.discord_user_id
                == discord_user_id
            )
        ),
        reverse=True,
    )
    stale = stamps[MAX_LOT_CHECKPOINTS:]
    if not stale:
        return
    for record_type in (
        ProfitLotCheckpointRecord,
        ProfitLotCheckpointIndexRecord,
    ):
        session.execute(
            delete(record_type).where(
                record_type.discord_user_id == discord_user_id,
                record_type.checkpoint_at.in_(stale),
            )
        )
    LOGGER.debug(
        "Trimmed profit lot checkpoints; user_id=%s dropped=%s",
        discord_user_id,
        len(stale),
    )


def _clear_member_rollups(session: Session, discord_user_id: int) -> None:
    for record_type in (
        ProfitRollupRecord,
        ProfitLotCheckpointIndexRecord,
    ProfitLotCheckpointRecord,
        ProfitOpenLotRecord,
        ProfitRollupStateRecord,
    ):
        session.execute(
            delete(record_type).where(
                record_type.discord_user_id == discord_user_id
            )
        )


def _clear_member_preferences(session: Session, discord_user_id: int) -> None:
    session.execute(
        delete(ProfitPreferenceRecord).where(
            ProfitPreferenceRecord.discord_user_id == discord_user_id
        )
    )
    session.execute(
        delete(ProfitOrderExclusionRecord).where(
            ProfitOrderExclusionRecord.discord_user_id == discord_user_id
        )
    )


def _clear_member_cache(session: Session, discord_user_id: int) -> None:
    session.execute(
        delete(ProfitTransactionRecord).where(
            ProfitTransactionRecord.discord_user_id == discord_user_id
        )
    )
    session.execute(
        delete(ProfitCacheSyncRecord).where(
            ProfitCacheSyncRecord.discord_user_id == discord_user_id
        )
    )
    # The rollups were matched from the rows just dropped, so they go too.
    _clear_member_rollups(session, discord_user_id)
