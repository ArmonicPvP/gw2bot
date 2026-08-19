"""Runtime configuration an officer changes from Discord with /settings.

The environment keeps only what decides how the container starts. Everything
else lives in the ``gw2_bot_settings`` table beside the raffle ledger, with the
credential-bearing values encrypted at rest, and is composed onto the bootstrap
values to make the :class:`~gw2bot.config.Config` the rest of the bot reads.
"""

from gw2bot.settings.composition import (
    compose_config as compose_config,
    compose_from_store as compose_from_store,
)
from gw2bot.settings.crypto import (
    ENCRYPTION_KEY_VARIABLE as ENCRYPTION_KEY_VARIABLE,
    SettingsCipher as SettingsCipher,
    key_file_path as key_file_path,
)
from gw2bot.settings.definitions import (
    CHANNELS_GROUP as CHANNELS_GROUP,
    COMMAND_NAME_LIMIT as COMMAND_NAME_LIMIT,
    GROUP_OPTION_LIMIT as GROUP_OPTION_LIMIT,
    LEGACY_SETTINGS as LEGACY_SETTINGS,
    LEGACY_VARIABLES as LEGACY_VARIABLES,
    ROLES_GROUP as ROLES_GROUP,
    SECRET_PLACEHOLDER as SECRET_PLACEHOLDER,
    SETTING_DEFINITIONS as SETTING_DEFINITIONS,
    SETTINGS_BY_KEY as SETTINGS_BY_KEY,
    UNSET_DISPLAY as UNSET_DISPLAY,
    SettingDefinition as SettingDefinition,
    ValidationTarget as ValidationTarget,
    definition_for as definition_for,
    definition_for_variable as definition_for_variable,
    definitions_in_group as definitions_in_group,
    setting_key as setting_key,
)
from gw2bot.settings.migration import (
    import_legacy_environment as import_legacy_environment,
    legacy_variables_present as legacy_variables_present,
)
from gw2bot.settings.store import SettingsStore as SettingsStore
