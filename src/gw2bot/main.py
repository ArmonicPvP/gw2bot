from __future__ import annotations

import logging
import os

from gw2bot.bot import Gw2Bot as Gw2Bot
from gw2bot.config import ConfigurationError, bootstrap_from_env
from gw2bot.logging_setup import (
    SecretRegistry,
    configure_logging as configure_logging,
)
from gw2bot.settings.composition import compose_from_store
from gw2bot.settings.crypto import SettingsCipher
from gw2bot.settings.migration import import_legacy_environment
from gw2bot.settings.store import SettingsStore

LOGGER = logging.getLogger(__name__)


def main() -> None:
    try:
        bootstrap = bootstrap_from_env()
    except ConfigurationError as exc:
        raise SystemExit(f"Configuration error: {exc}") from exc

    # The Discord token is the one credential that exists before the database
    # does, so console redaction is armed with it first and the rest are added
    # as soon as the settings can be read.
    secrets = SecretRegistry((bootstrap.discord_token,))
    configure_logging(bootstrap.debug, secrets)
    LOGGER.debug("Debug logging enabled")

    try:
        cipher = SettingsCipher.for_database(bootstrap.raffle_db_path)
        settings_store = SettingsStore(bootstrap.raffle_db_path, cipher)
    except (ConfigurationError, OSError) as exc:
        raise SystemExit(f"Configuration error: {exc}") from exc

    # This is the last thing that reads the environment for configuration, and
    # it happens before anything is composed so the first run after the upgrade
    # starts with the values the operator already had. It runs exactly once;
    # afterwards the database is what the bot reads.
    import_legacy_environment(settings_store, os.environ)

    config = compose_from_store(bootstrap, settings_store)
    secrets.update(
        (
            config.gw2_api_key,
            config.discord_oauth_client_secret,
            config.web_session_secret,
        )
    )
    bot = Gw2Bot(
        config,
        bootstrap=bootstrap,
        settings_store=settings_store,
        secrets=secrets,
    )
    bot.run(bootstrap.discord_token, log_handler=None)
