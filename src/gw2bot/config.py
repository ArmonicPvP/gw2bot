from __future__ import annotations

import os
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from dotenv import load_dotenv


def normalize_guild_id(value: str | None) -> str | None:
    """One spelling of a Guild Wars 2 guild id, for comparing two of them.

    uuid.UUID accepts upper and lower case, braces, a urn:uuid: prefix and no
    hyphens at all, so the same guild reaches the bot written several ways:
    the API returns uppercase, an operator pastes whatever they copied, and
    the raffle ledger holds whatever an earlier release stored. Comparing the
    raw text would call those different guilds - which, for the ledger, means
    refusing to start.

    Returns None for anything that is not a UUID, so callers can fall back to
    comparing the original strings rather than treating unparsable values as
    equal.
    """
    if value is None:
        return None
    try:
        return str(uuid.UUID(value.strip()))
    except (ValueError, AttributeError):
        return None


def same_guild_id(left: str | None, right: str | None) -> bool:
    """Whether two guild ids name the same guild, however they are written.

    Falls back to exact text when either side is not a UUID, so a ledger some
    earlier release bound to a non-UUID value keeps the behaviour it has.
    """
    if left is None or right is None:
        return left == right
    normalized_left = normalize_guild_id(left)
    normalized_right = normalize_guild_id(right)
    if normalized_left is None or normalized_right is None:
        return left.strip() == right.strip()
    return normalized_left == normalized_right

# The settings a Guild Wars 2 lookup needs, and the one that decides whether
# anything is delivered to the notification channel. Named here rather than
# re-derived, so a message about a disabled feature always names the same
# /settings subcommand the operator has to run.
GW2_API_SETTINGS = ("gw2_api_key", "gw2_guild_id")
NOTIFICATION_CHANNEL_SETTING = "discord_notification_channel_id"

# Variables that stay in the environment because they decide how the container
# itself starts: the credentials the bot needs before it can read a database,
# where that database is, whether a listening socket is opened and on which
# port, and where the encrypted API key is allowed to be sent.
BOOTSTRAP_VARIABLES = (
    "DISCORD_TOKEN",
    "DISCORD_COMMAND_GUILD_ID",
    "DEBUG",
    "RAFFLE_DB_PATH",
    "WEB_ENABLED",
    "WEB_PORT",
    "GW2_API_BASE_URL",
    "SETTINGS_ENCRYPTION_KEY",
)

# Discord role, channel and forum-tag ids. These were literals scattered through
# the feature modules until they became settings; they stay here as the values a
# guild falls back to when the matching setting is unset, so an install that
# never touches /settings behaves exactly as it did before.
DEFAULT_RAFFLE_DRAW_ROLE_ID = 1317124663847157880
DEFAULT_RAFFLE_ADDTICKET_ROLE_ID = 1318357141521825872
DEFAULT_RAFFLE_OFFICER_ROLE_ID = 1317638909735342201
DEFAULT_GUILD_ROSTER_ROLE_ID = 1317202210152513606
DEFAULT_EVENT_CREATE_ROLE_ID = 1318357141521825872
DEFAULT_TRIAL_ROLE_ID = 1450164501696741597
DEFAULT_SUNBORNE_ROLE_ID = 1317140660188352584
DEFAULT_RAFFLE_CONTRIBUTION_CHANNEL_ID = 856343628984746014
DEFAULT_TRIAL_FORUM_CHANNEL_ID = 1317206104727621693
DEFAULT_TRIAL_ACCEPTED_TAG_ID = 1317349209619562587
DEFAULT_TRIAL_IN_REVIEW_TAG_ID = 1317349421821726790


class ConfigurationError(ValueError):
    """Raised when required application configuration is invalid."""


def missing_configuration_message(settings: Sequence[str]) -> str:
    """Explain to a command's caller which settings switch the feature on."""
    commands = ", ".join(f"/settings {name}" for name in settings)
    plural = "s" if len(settings) != 1 else ""
    return (
        "This command is disabled. Run the "
        f"{commands} command{plural} to enable it."
    )


@dataclass(frozen=True, slots=True)
class Config:
    discord_token: str
    discord_command_guild_id: int
    # The three values below are optional: the bot starts without them and
    # disables the features that need them instead of refusing to run.
    discord_notification_channel_id: int | None = None
    gw2_api_key: str | None = None
    gw2_guild_id: str | None = None
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
    # Discord ids. The food page has no default of its own: it has always been
    # whichever role draws the raffle, and composing the settings is what keeps
    # it following that role until someone sets it apart. The roster page
    # starts from that same role as a plain default rather than a follower,
    # because it is a page with its own audience.
    # In-game account names barred from the raffle. Matched case-insensitively,
    # because the guild log and the roster do not agree on capitalisation.
    raffle_excluded_accounts: tuple[str, ...] = ()
    raffle_draw_role_id: int = DEFAULT_RAFFLE_DRAW_ROLE_ID
    raffle_addticket_role_id: int = DEFAULT_RAFFLE_ADDTICKET_ROLE_ID
    raffle_officer_role_id: int = DEFAULT_RAFFLE_OFFICER_ROLE_ID
    guild_roster_role_id: int = DEFAULT_GUILD_ROSTER_ROLE_ID
    event_create_role_id: int = DEFAULT_EVENT_CREATE_ROLE_ID
    trial_role_id: int = DEFAULT_TRIAL_ROLE_ID
    sunborne_role_id: int = DEFAULT_SUNBORNE_ROLE_ID
    food_page_role_id: int = DEFAULT_RAFFLE_DRAW_ROLE_ID
    roster_page_role_id: int = DEFAULT_RAFFLE_DRAW_ROLE_ID
    raffle_contribution_channel_id: int = DEFAULT_RAFFLE_CONTRIBUTION_CHANNEL_ID
    trial_forum_channel_id: int = DEFAULT_TRIAL_FORUM_CHANNEL_ID
    trial_accepted_tag_id: int = DEFAULT_TRIAL_ACCEPTED_TAG_ID
    trial_in_review_tag_id: int = DEFAULT_TRIAL_IN_REVIEW_TAG_ID

    @property
    def missing_gw2_api_settings(self) -> tuple[str, ...]:
        """Names of the unset settings that Guild Wars 2 lookups need."""
        return tuple(
            name
            for name, value in zip(
                GW2_API_SETTINGS,
                (self.gw2_api_key, self.gw2_guild_id),
                strict=True,
            )
            if value is None
        )

    @property
    def gw2_api_enabled(self) -> bool:
        """Whether the bot can call the Guild Wars 2 API for its guild."""
        return not self.missing_gw2_api_settings

    @property
    def notifications_enabled(self) -> bool:
        """Whether a channel is configured for automated notifications."""
        return self.discord_notification_channel_id is not None

    @property
    def missing_web_settings(self) -> tuple[str, ...]:
        """Names of the unset settings the web calendar needs to serve."""
        return tuple(
            name
            for name, value in (
                ("web_base_url", self.web_base_url),
                ("discord_oauth_client_id", self.discord_oauth_client_id),
                (
                    "discord_oauth_client_secret",
                    self.discord_oauth_client_secret,
                ),
                ("web_session_secret", self.web_session_secret),
            )
            if value is None
        )

    @property
    def web_calendar_enabled(self) -> bool:
        """Whether the calendar is both switched on and fully configured.

        WEB_ENABLED opens the listening socket, which is why it stays an
        environment variable, but the four values it needs are settings now.
        Switching it on before they are set warns and leaves the calendar off
        rather than refusing to start, the same way an unset API key disables
        Guild Wars 2 polling.
        """
        return self.web_enabled and not self.missing_web_settings


@dataclass(frozen=True, slots=True)
class BootstrapConfig:
    """The values that have to be known before the database can be opened.

    Everything else lives in the settings table and is composed on top of
    this; see gw2bot.settings.composition.
    """

    discord_token: str
    discord_command_guild_id: int
    debug: bool = False
    raffle_db_path: str = "data/gw2bot.db"
    gw2_api_base_url: str = "https://api.guildwars2.com"
    web_enabled: bool = False
    web_port: int = 2222


def bootstrap_from_env(env: Mapping[str, str] | None = None) -> BootstrapConfig:
    if env is None:
        # Existing runtime variables win over local .env values.
        load_dotenv(override=False)
    values = os.environ if env is None else env
    required = ("DISCORD_TOKEN", "DISCORD_COMMAND_GUILD_ID")
    missing = [name for name in required if not values.get(name, "").strip()]
    if missing:
        raise ConfigurationError(
            f"Missing required environment variables: {', '.join(missing)}"
        )
    return BootstrapConfig(
        discord_token=values["DISCORD_TOKEN"].strip(),
        discord_command_guild_id=_positive_int(
            values["DISCORD_COMMAND_GUILD_ID"],
            "DISCORD_COMMAND_GUILD_ID",
        ),
        debug=_boolean(values.get("DEBUG", "false"), "DEBUG"),
        raffle_db_path=_optional_string(
            values.get("RAFFLE_DB_PATH"),
            "data/gw2bot.db",
        ),
        gw2_api_base_url=_optional_string(
            values.get("GW2_API_BASE_URL"),
            "https://api.guildwars2.com",
        ).rstrip("/"),
        web_enabled=_boolean(values.get("WEB_ENABLED", "false"), "WEB_ENABLED"),
        web_port=_positive_int(values.get("WEB_PORT", "2222"), "WEB_PORT"),
    )


def _positive_int(value: str, name: str) -> int:
    try:
        result = int(value)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if result <= 0:
        raise ConfigurationError(f"{name} must be greater than zero")
    return result


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
