from __future__ import annotations

import logging
from collections.abc import Mapping

from gw2bot.config import BootstrapConfig, Config
from gw2bot.settings.definitions import (
    SETTING_DEFINITIONS,
    SettingDefinition,
    setting_key,
)
from gw2bot.settings.store import SettingsStore

LOGGER = logging.getLogger(__name__)


def compose_config(
    bootstrap: BootstrapConfig,
    raw_values: Mapping[str, str],
) -> Config:
    """Build the runtime configuration from the bootstrap plus the settings.

    Stored text is parsed by the definition that owns it. A value that no
    longer parses is logged and dropped rather than raised: the bot has
    already started by the time this runs on a settings change, and one bad
    row must not take the whole configuration down with it. The setting simply
    reads as unset, which is the state /settings can fix.
    """
    fields: dict[str, object] = {
        "discord_token": bootstrap.discord_token,
        "discord_command_guild_id": bootstrap.discord_command_guild_id,
        "debug": bootstrap.debug,
        "raffle_db_path": bootstrap.raffle_db_path,
        "gw2_api_base_url": bootstrap.gw2_api_base_url,
        "web_enabled": bootstrap.web_enabled,
        "web_port": bootstrap.web_port,
    }
    resolved: dict[str, object] = {}
    invalid = 0
    for definition in SETTING_DEFINITIONS:
        value = _resolve(definition, raw_values)
        if value is _INVALID:
            invalid += 1
            value = None
        if value is None:
            continue
        resolved[definition.name] = value
        fields[definition.field] = value

    # The food page role follows whichever role draws the raffle until it is
    # set apart, so it is filled in after every literal value is known.
    for definition in SETTING_DEFINITIONS:
        if definition.default_from is None or definition.name in resolved:
            continue
        source = _definition_named(definition.default_from)
        if source is None:
            continue
        followed = fields.get(source.field, source.default)
        if followed is not None:
            fields[definition.field] = followed

    LOGGER.debug(
        "Composed configuration; stored=%s applied=%s invalid=%s",
        len(raw_values),
        len(resolved),
        invalid,
    )
    return Config(**fields)  # type: ignore[arg-type]


def compose_from_store(
    bootstrap: BootstrapConfig,
    store: SettingsStore,
) -> Config:
    return compose_config(bootstrap, store.raw_values())


class _Invalid:
    """Sentinel telling a caller a stored value could not be parsed."""


_INVALID = _Invalid()


def _resolve(
    definition: SettingDefinition,
    raw_values: Mapping[str, str],
) -> object:
    raw = raw_values.get(setting_key(definition))
    if raw is None:
        return definition.default
    try:
        return definition.parse(raw)
    except Exception as exc:
        LOGGER.error(
            "Ignoring an unusable stored setting; set it again with %s. "
            "setting=%s error_type=%s",
            definition.command_path,
            setting_key(definition),
            type(exc).__name__,
        )
        return _INVALID


def _definition_named(name: str) -> SettingDefinition | None:
    for definition in SETTING_DEFINITIONS:
        if definition.name == name:
            return definition
    return None
