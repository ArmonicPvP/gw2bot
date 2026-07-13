from __future__ import annotations

import logging

from gw2bot.bot import Gw2Bot as Gw2Bot
from gw2bot.config import Config, ConfigurationError
from gw2bot.logging_setup import configure_logging as configure_logging

LOGGER = logging.getLogger(__name__)


def main() -> None:
    try:
        config = Config.from_env()
    except ConfigurationError as exc:
        raise SystemExit(f"Configuration error: {exc}") from exc

    configure_logging(
        config.debug,
        (
            config.gw2_api_key,
            config.discord_token,
            config.discord_oauth_client_secret or "",
            config.web_session_secret or "",
        ),
    )
    LOGGER.debug("Debug logging enabled")
    bot = Gw2Bot(config)
    bot.run(config.discord_token, log_handler=None)
