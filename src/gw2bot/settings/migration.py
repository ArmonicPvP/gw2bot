from __future__ import annotations

import logging
from collections.abc import Mapping

from gw2bot.settings.definitions import (
    LEGACY_SETTINGS,
    SettingDefinition,
    setting_key,
)
from gw2bot.settings.store import SettingsStore

LOGGER = logging.getLogger(__name__)


def import_legacy_environment(
    store: SettingsStore,
    env: Mapping[str, str],
) -> tuple[str, ...]:
    """Copy the legacy environment variables into the settings once.

    Runs on the first startup after the upgrade and never again: after the
    marker is written the database is what the bot reads, and the variables
    only earn a warning. Doing it every startup would mean a value unset with
    /settings came back the next time the container restarted, with nothing on
    screen to explain why.

    Returns the names of the variables it imported.
    """
    if store.env_import_completed():
        LOGGER.debug("Skipped the legacy environment import; already recorded")
        return ()

    ledger_guild_id = store.ledger_guild_id()
    imported: list[str] = []
    skipped_unset = 0
    skipped_stored = 0
    rejected: list[str] = []
    for definition in LEGACY_SETTINGS:
        variable = definition.legacy_variable
        assert variable is not None
        value = env.get(variable, "").strip()
        if not value:
            skipped_unset += 1
            continue
        if store.is_set(definition):
            # Somebody set it with /settings before the import ran. Their
            # choice is the newer one and wins.
            skipped_stored += 1
            continue
        if not _importable(definition, value, variable):
            rejected.append(variable)
            continue
        if _conflicts_with_ledger(definition, value, ledger_guild_id):
            # Storing it would make RaffleStore refuse to open on this and
            # every later startup, and the marker below would stop a corrected
            # variable from ever being read again - so the guild would be left
            # with a bot that cannot start and no command able to fix it.
            LOGGER.warning(
                "Not importing %s: the raffle ledger already belongs to a "
                "different guild. Set /settings gw2_guild_id if this database "
                "should follow the new one, or point RAFFLE_DB_PATH at a "
                "different file.",
                variable,
            )
            rejected.append(variable)
            continue
        store.set_raw(definition, value)
        imported.append(variable)

    store.mark_env_import_completed()
    LOGGER.info(
        "Imported the legacy environment configuration into /settings; "
        "imported=%s already_set=%s unset=%s rejected=%s",
        len(imported),
        skipped_stored,
        skipped_unset,
        len(rejected),
    )
    if rejected:
        # Each rejection has already been logged with its own reason; this
        # is the one line that names them together.
        LOGGER.warning(
            "These environment variables were not imported and are not "
            "configuring anything; set them with /settings: %s",
            ", ".join(rejected),
        )
    return tuple(imported)


def _conflicts_with_ledger(
    definition: SettingDefinition,
    value: str,
    ledger_guild_id: str | None,
) -> bool:
    """Whether importing this value would claim a ledger for another guild."""
    return (
        definition.field == "gw2_guild_id"
        and ledger_guild_id is not None
        and ledger_guild_id != value
    )


def _importable(
    definition: SettingDefinition,
    value: str,
    variable: str,
) -> bool:
    """Whether a legacy value still parses under the setting that replaced it.

    A value the parser rejects must not be stored: it would fail the same way
    on every startup, with no command able to show what is wrong. Rejecting it
    here leaves the setting unset and names the variable in a warning.
    """
    try:
        definition.parse(value)
    except Exception as exc:
        LOGGER.debug(
            "Rejected a legacy environment value; variable=%s setting=%s "
            "error_type=%s",
            variable,
            setting_key(definition),
            type(exc).__name__,
        )
        return False
    return True


def legacy_variables_present(env: Mapping[str, str]) -> tuple[str, ...]:
    """Legacy variables still set in the environment, in definition order."""
    return tuple(
        definition.legacy_variable
        for definition in LEGACY_SETTINGS
        if definition.legacy_variable is not None
        and env.get(definition.legacy_variable, "").strip()
    )
