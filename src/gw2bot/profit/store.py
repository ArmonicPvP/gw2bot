from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, sessionmaker

from gw2bot.database import (
    ProfitApiKeyRecord,
    ProfitCacheSyncRecord,
    ProfitItemRecord,
    ProfitTransactionRecord,
    create_database_engine,
    initialize_database,
)
from gw2bot.logging_setup import SecretRegistry
from gw2bot.profit.models import Transaction, parse_gw2_time
from gw2bot.settings.crypto import SettingsCipher

LOGGER = logging.getLogger(__name__)

HISTORY_KINDS = frozenset({"history_buys", "history_sells"})
CURRENT_KINDS = frozenset({"current_sells"})
TRANSACTION_KINDS = HISTORY_KINDS | CURRENT_KINDS
HISTORY_RETENTION_DAYS = 92


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
        return api_key

    def delete_api_key(self, discord_user_id: int) -> bool:
        with self._sessions.begin() as session:
            record = session.get(ProfitApiKeyRecord, discord_user_id)
            removed = record is not None
            if record is not None:
                session.delete(record)
            _clear_member_cache(session, discord_user_id)
        LOGGER.debug(
            "Deleted profit API key; user_id=%s removed=%s "
            "cache_cleared=true",
            discord_user_id,
            removed,
        )
        return removed

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
                    )
                )
            else:
                record.synced_at = synced_at
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
        updated_at = (datetime.now(UTC) if now is None else now).isoformat()
        rows = [
            {
                "discord_user_id": discord_user_id,
                "transaction_kind": transaction_kind,
                "transaction_id": transaction.transaction_id,
                "item_id": transaction.item_id,
                "price": transaction.price,
                "quantity": transaction.quantity,
                "occurred_at": transaction.occurred_at.isoformat(),
                "updated_at": updated_at,
            }
            for transaction in transactions
        ]
        with self._sessions.begin() as session:
            if transaction_kind in CURRENT_KINDS:
                session.execute(
                    delete(ProfitTransactionRecord).where(
                        ProfitTransactionRecord.discord_user_id
                        == discord_user_id,
                        ProfitTransactionRecord.transaction_kind
                        == transaction_kind,
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
                    ),
                    rows,
                )
            if transaction_kind in HISTORY_KINDS:
                cutoff = (
                    datetime.now(UTC) if now is None else now
                ) - timedelta(days=HISTORY_RETENTION_DAYS)
                session.execute(
                    delete(ProfitTransactionRecord).where(
                        ProfitTransactionRecord.discord_user_id
                        == discord_user_id,
                        ProfitTransactionRecord.transaction_kind.in_(
                            HISTORY_KINDS
                        ),
                        ProfitTransactionRecord.occurred_at
                        < cutoff.isoformat(),
                    )
                )
        LOGGER.debug(
            "Stored profit transactions; user_id=%s kind=%s records=%s "
            "replace=%s",
            discord_user_id,
            transaction_kind,
            len(transactions),
            transaction_kind in CURRENT_KINDS,
        )

    def get_transactions(
        self,
        discord_user_id: int,
        transaction_kind: str,
        cutoff: datetime | None = None,
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

    def get_item_names(self, item_ids: set[int]) -> dict[int, str]:
        if not item_ids:
            LOGGER.debug("Read profit item names; requested=0 found=0")
            return {}
        with self._sessions() as session:
            records = list(
                session.scalars(
                    select(ProfitItemRecord).where(
                        ProfitItemRecord.item_id.in_(item_ids)
                    )
                )
            )
        names = {record.item_id: record.name for record in records}
        LOGGER.debug(
            "Read profit item names; requested=%s found=%s",
            len(item_ids),
            len(names),
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
