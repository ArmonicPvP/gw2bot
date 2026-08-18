from __future__ import annotations

import logging
from collections.abc import Mapping

from sqlalchemy.orm import sessionmaker

from gw2bot.database import (
    BotSettingRecord,
    SettingRecord,
    create_database_engine,
    initialize_database,
)
from gw2bot.settings.crypto import SettingsCipher
from gw2bot.settings.definitions import (
    SETTING_DEFINITIONS,
    SettingDefinition,
    setting_key,
)

LOGGER = logging.getLogger(__name__)

# Marker in the bot's own bookkeeping table recording that the environment has
# already been read once. See gw2bot.settings.migration.
ENV_IMPORT_MARKER_KEY = "settings_env_import_v1"
# The raffle ledger's own record of which guild it belongs to. Owned by
# RaffleStore; read here so the environment import cannot store a guild id
# that would stop the bot from starting.
LEDGER_GUILD_KEY = "guild_id"


class SettingsStore:
    """Reads and writes the values behind /settings.

    Values are stored as text, parsed on the way out by the definition that
    owns them, so a definition whose parser changes cannot be silently
    misread. Secrets are encrypted in the row and only ever decrypted to
    compose the configuration - never to show anybody.
    """

    def __init__(self, database_path: str, cipher: SettingsCipher) -> None:
        LOGGER.debug("Opening settings store")
        self._cipher = cipher
        self._engine = create_database_engine(database_path)
        initialize_database(self._engine)
        self._sessions = sessionmaker(self._engine, expire_on_commit=False)
        LOGGER.debug("Settings store initialized")

    def close(self) -> None:
        LOGGER.debug("Closing settings store")
        self._engine.dispose()

    def get_raw(self, definition: SettingDefinition) -> str | None:
        """Stored text for one setting, decrypted when it is a secret."""
        key = setting_key(definition)
        with self._sessions() as session:
            record = session.get(BotSettingRecord, key)
        if record is None:
            LOGGER.debug("Read setting; setting=%s present=false", key)
            return None
        if not record.is_encrypted:
            LOGGER.debug(
                "Read setting; setting=%s present=true encrypted=false "
                "characters=%s",
                key,
                len(record.value),
            )
            return record.value
        plaintext = self._cipher.decrypt(record.value, key)
        LOGGER.debug(
            "Read setting; setting=%s present=true encrypted=true "
            "decrypted=%s",
            key,
            plaintext is not None,
        )
        return plaintext

    def is_set(self, definition: SettingDefinition) -> bool:
        """Whether a row exists, without decrypting anything.

        A secret whose key has been lost still counts as set here, so
        /settings can say the value is there and unreadable rather than
        claiming nobody ever set it.
        """
        key = setting_key(definition)
        with self._sessions() as session:
            return session.get(BotSettingRecord, key) is not None

    def set_raw(self, definition: SettingDefinition, value: str) -> None:
        key = setting_key(definition)
        stored = self._cipher.encrypt(value) if definition.encrypted else value
        with self._sessions.begin() as session:
            record = session.get(BotSettingRecord, key)
            if record is None:
                session.add(
                    BotSettingRecord(
                        key=key,
                        value=stored,
                        is_encrypted=definition.encrypted,
                    )
                )
            else:
                record.value = stored
                record.is_encrypted = definition.encrypted
        LOGGER.debug(
            "Stored setting; setting=%s encrypted=%s characters=%s",
            key,
            definition.encrypted,
            len(value),
        )

    def unset(self, definition: SettingDefinition) -> bool:
        """Remove a setting, reporting whether there was one to remove."""
        key = setting_key(definition)
        with self._sessions.begin() as session:
            record = session.get(BotSettingRecord, key)
            removed = record is not None
            if record is not None:
                session.delete(record)
        LOGGER.debug("Unset setting; setting=%s removed=%s", key, removed)
        return removed

    def stored_keys(self) -> frozenset[str]:
        """Keys with a row, whether or not their value can be decrypted."""
        with self._sessions() as session:
            records = session.query(BotSettingRecord.key).all()
        return frozenset(row[0] for row in records)

    def raw_values(self) -> Mapping[str, str]:
        """Every readable setting, keyed the way the definitions key them."""
        values: dict[str, str] = {}
        stored = self.stored_keys()
        unreadable = 0
        for definition in SETTING_DEFINITIONS:
            key = setting_key(definition)
            raw = self.get_raw(definition)
            if raw is None:
                # A row that is there but unreadable is a secret this key
                # cannot decrypt. get_raw has already logged which one; count
                # it here so the summary does not read as "never set".
                if key in stored:
                    unreadable += 1
                continue
            if raw == "":
                # An empty stored value would parse as "unset" anyway, and
                # writing one is not something /settings can do.
                continue
            values[key] = raw
        LOGGER.debug(
            "Loaded settings; stored=%s unreadable=%s",
            len(values),
            unreadable,
        )
        return values

    def ledger_guild_id(self) -> str | None:
        """The guild the raffle ledger in this database already belongs to.

        The ledger records it under its own key in the bookkeeping table this
        store already uses for the import marker. Reading it here lets the
        one-time environment import refuse a guild id that would make
        RaffleStore refuse to open, which happens before any command exists to
        report it.
        """
        with self._sessions() as session:
            record = session.get(SettingRecord, LEDGER_GUILD_KEY)
            return record.value if record is not None else None

    def env_import_completed(self) -> bool:
        with self._sessions() as session:
            return session.get(SettingRecord, ENV_IMPORT_MARKER_KEY) is not None

    def mark_env_import_completed(self) -> None:
        with self._sessions.begin() as session:
            if session.get(SettingRecord, ENV_IMPORT_MARKER_KEY) is None:
                session.add(
                    SettingRecord(key=ENV_IMPORT_MARKER_KEY, value="1")
                )
        LOGGER.debug("Recorded the one-time environment import marker")
