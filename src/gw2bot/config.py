from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv


class ConfigurationError(ValueError):
    """Raised when required application configuration is invalid."""


@dataclass(frozen=True, slots=True)
class Config:
    discord_token: str
    discord_command_guild_id: int
    discord_notification_channel_id: int
    gw2_api_key: str
    gw2_guild_id: str
    discord_feast_notification_user_id: int | None = None
    poll_interval_seconds: int = 300
    guild_log_poll_interval_seconds: int = 60
    guild_member_cache_seconds: int = 900
    raffle_db_path: str = "data/gw2bot.db"
    gw2_api_base_url: str = "https://api.guildwars2.com"
    event_timezone: str = "UTC"
    debug: bool = False
    web_enabled: bool = False
    web_port: int = 2222
    web_base_url: str | None = None
    discord_oauth_client_id: str | None = None
    discord_oauth_client_secret: str | None = None
    web_session_secret: str | None = None
    web_session_ttl_seconds: int = 604800

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Config:
        if env is None:
            # Existing runtime variables win over local .env values.
            load_dotenv(override=False)
        values = os.environ if env is None else env
        required = (
            "DISCORD_TOKEN",
            "DISCORD_COMMAND_GUILD_ID",
            "DISCORD_NOTIFICATION_CHANNEL_ID",
            "GW2_API_KEY",
            "GW2_GUILD_ID",
        )
        missing = [name for name in required if not values.get(name, "").strip()]
        if missing:
            raise ConfigurationError(
                f"Missing required environment variables: {', '.join(missing)}"
            )

        discord_command_guild_id = _positive_int(
            values["DISCORD_COMMAND_GUILD_ID"],
            "DISCORD_COMMAND_GUILD_ID",
        )
        discord_notification_channel_id = _positive_int(
            values["DISCORD_NOTIFICATION_CHANNEL_ID"],
            "DISCORD_NOTIFICATION_CHANNEL_ID",
        )
        discord_feast_notification_user_id = _optional_positive_int(
            values.get("DISCORD_FEAST_NOTIFICATION_USER_ID"),
            "DISCORD_FEAST_NOTIFICATION_USER_ID",
        )
        poll_interval = _positive_int(
            values.get("GW2_POLL_INTERVAL_SECONDS", "300"),
            "GW2_POLL_INTERVAL_SECONDS",
        )
        if poll_interval < 30:
            raise ConfigurationError("GW2_POLL_INTERVAL_SECONDS must be at least 30")
        guild_log_poll_interval = _positive_int(
            values.get("GW2_GUILD_LOG_POLL_INTERVAL_SECONDS", "60"),
            "GW2_GUILD_LOG_POLL_INTERVAL_SECONDS",
        )
        if guild_log_poll_interval < 30:
            raise ConfigurationError(
                "GW2_GUILD_LOG_POLL_INTERVAL_SECONDS must be at least 30"
            )
        guild_member_cache = _positive_int(
            values.get("GW2_GUILD_MEMBER_CACHE_SECONDS", "900"),
            "GW2_GUILD_MEMBER_CACHE_SECONDS",
        )
        event_timezone = _optional_string(
            values.get("TZ"),
            "UTC",
        )
        try:
            ZoneInfo(event_timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ConfigurationError(
                "TZ must be a valid IANA timezone name"
            ) from exc
        web_enabled = _boolean(values.get("WEB_ENABLED", "false"), "WEB_ENABLED")
        web_port = _positive_int(values.get("WEB_PORT", "2222"), "WEB_PORT")
        web_session_ttl_seconds = _positive_int(
            values.get("WEB_SESSION_TTL_SECONDS", "604800"),
            "WEB_SESSION_TTL_SECONDS",
        )
        web_base_url: str | None = None
        discord_oauth_client_id: str | None = None
        discord_oauth_client_secret: str | None = None
        web_session_secret: str | None = None
        if web_enabled:
            web_required = (
                "WEB_BASE_URL",
                "DISCORD_OAUTH_CLIENT_ID",
                "DISCORD_OAUTH_CLIENT_SECRET",
                "WEB_SESSION_SECRET",
            )
            web_missing = [
                name for name in web_required if not values.get(name, "").strip()
            ]
            if web_missing:
                raise ConfigurationError(
                    "WEB_ENABLED requires environment variables: "
                    f"{', '.join(web_missing)}"
                )
            web_base_url = values["WEB_BASE_URL"].strip().rstrip("/")
            if not web_base_url.startswith(("http://", "https://")):
                raise ConfigurationError(
                    "WEB_BASE_URL must start with http:// or https://"
                )
            discord_oauth_client_id = values["DISCORD_OAUTH_CLIENT_ID"].strip()
            discord_oauth_client_secret = values[
                "DISCORD_OAUTH_CLIENT_SECRET"
            ].strip()
            web_session_secret = values["WEB_SESSION_SECRET"].strip()
            if len(web_session_secret) < 32:
                raise ConfigurationError(
                    "WEB_SESSION_SECRET must be at least 32 characters"
                )
        return cls(
            discord_token=values["DISCORD_TOKEN"].strip(),
            discord_command_guild_id=discord_command_guild_id,
            discord_notification_channel_id=discord_notification_channel_id,
            discord_feast_notification_user_id=discord_feast_notification_user_id,
            gw2_api_key=values["GW2_API_KEY"].strip(),
            gw2_guild_id=values["GW2_GUILD_ID"].strip(),
            poll_interval_seconds=poll_interval,
            guild_log_poll_interval_seconds=guild_log_poll_interval,
            guild_member_cache_seconds=guild_member_cache,
            raffle_db_path=_optional_string(
                values.get("RAFFLE_DB_PATH"),
                "data/gw2bot.db",
            ),
            gw2_api_base_url=_optional_string(
                values.get("GW2_API_BASE_URL"),
                "https://api.guildwars2.com",
            ).rstrip("/"),
            event_timezone=event_timezone,
            debug=_boolean(values.get("DEBUG", "false"), "DEBUG"),
            web_enabled=web_enabled,
            web_port=web_port,
            web_base_url=web_base_url,
            discord_oauth_client_id=discord_oauth_client_id,
            discord_oauth_client_secret=discord_oauth_client_secret,
            web_session_secret=web_session_secret,
            web_session_ttl_seconds=web_session_ttl_seconds,
        )


def _positive_int(value: str, name: str) -> int:
    try:
        result = int(value)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if result <= 0:
        raise ConfigurationError(f"{name} must be greater than zero")
    return result


def _optional_positive_int(value: str | None, name: str) -> int | None:
    if value is None or not value.strip():
        return None
    return _positive_int(value, name)


def _optional_string(value: str | None, default: str) -> str:
    if value is None or not value.strip():
        return default
    return value.strip()


def _boolean(value: str, name: str) -> bool:
    normalized = value.strip().casefold()
    if normalized in {"true", "1", "yes", "on"}:
        return True
    if normalized in {"false", "0", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} must be true or false")
