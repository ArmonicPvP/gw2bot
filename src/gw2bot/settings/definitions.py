from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from gw2bot.config import (
    DEFAULT_EVENT_CREATE_ROLE_ID,
    DEFAULT_GUILD_ROSTER_ROLE_ID,
    DEFAULT_RAFFLE_ADDTICKET_ROLE_ID,
    DEFAULT_RAFFLE_CONTRIBUTION_CHANNEL_ID,
    DEFAULT_RAFFLE_DRAW_ROLE_ID,
    DEFAULT_RAFFLE_OFFICER_ROLE_ID,
    DEFAULT_SUNBORNE_ROLE_ID,
    DEFAULT_TRIAL_ACCEPTED_TAG_ID,
    DEFAULT_TRIAL_FORUM_CHANNEL_ID,
    DEFAULT_TRIAL_IN_REVIEW_TAG_ID,
    DEFAULT_TRIAL_ROLE_ID,
    ConfigurationError,
    normalize_guild_id,
)

# Discord rejects a command name longer than this, and it rejects the whole
# command tree rather than the one name, so a settings name that overruns it
# takes every other command down with it.
COMMAND_NAME_LIMIT = 32
# Discord's cap on the options one command group may hold. Subcommands and
# nested subgroups both count against it.
GROUP_OPTION_LIMIT = 25

ROLES_GROUP = "roles"
CHANNELS_GROUP = "channels"

SECRET_PLACEHOLDER = "This secret cannot be viewed once set"
UNSET_DISPLAY = "Not set"


class ValidationTarget:
    """What a Discord id has to resolve to before it may be stored."""

    ROLE = "role"
    TEXT_CHANNEL = "text_channel"
    FORUM_CHANNEL = "forum_channel"
    FORUM_TAG = "forum_tag"


@dataclass(frozen=True, slots=True)
class SettingDefinition:
    """One value an officer can change from Discord.

    ``name`` is the slash subcommand, ``field`` the :class:`~gw2bot.config.Config`
    attribute it feeds, and ``legacy_variable`` the environment variable it
    replaces - ``None`` for the Discord ids, which were source literals and so
    have nothing to import or warn about.

    ``encrypted`` marks a value that holds a credential: it is encrypted in the
    row and never rendered back to anybody. It is a flag describing the
    setting, not the value itself.

    ``default`` and ``default_from`` are alternatives: a literal fallback, or
    the name of another setting to follow while this one is unset. Only the
    food page role uses the second, because it has always been whichever role
    draws the raffle.
    """

    name: str
    field: str
    description: str
    parse: Callable[[str], object]
    legacy_variable: str | None = None
    # Whether a leftover legacy_variable earns the "remove this from the
    # environment" warning. False for a variable the container itself uses:
    # the value is still imported once, but telling an operator to delete it
    # would break something the bot does not own.
    warn_when_present: bool = True
    group: str | None = None
    encrypted: bool = False
    default: object = None
    default_from: str | None = None
    validates: str | None = None
    # How a parsed value is written back to the row. Defaults to str(), which
    # round-trips every scalar setting; a setting whose value is a collection
    # has to say how, or str(tuple) would be stored and never parse again.
    serialize: Callable[[object], str] | None = None

    @property
    def command_path(self) -> str:
        """How the setting is written when a message tells someone to set it."""
        if self.group is None:
            return f"/settings {self.name}"
        return f"/settings {self.group} {self.name}"


def _parse_positive_int(name: str) -> Callable[[str], object]:
    def parse(value: str) -> object:
        try:
            result = int(value)
        except ValueError as exc:
            raise ConfigurationError(f"{name} must be an integer") from exc
        if result <= 0:
            raise ConfigurationError(f"{name} must be greater than zero")
        return result

    return parse


def _parse_interval(name: str, minimum: int) -> Callable[[str], object]:
    positive = _parse_positive_int(name)

    def parse(value: str) -> object:
        result = positive(value)
        assert isinstance(result, int)
        if result < minimum:
            raise ConfigurationError(f"{name} must be at least {minimum}")
        return result

    return parse


def _parse_discord_id(name: str) -> Callable[[str], object]:
    positive = _parse_positive_int(name)

    def parse(value: str) -> object:
        # Discord's own copy buttons and mention syntax both end up pasted
        # here, so accept <@&123>, <#123> and #123 as well as a bare id.
        cleaned = value.strip().lstrip("<").rstrip(">").lstrip("@&#")
        return positive(cleaned)

    return parse


def _parse_guild_id(value: str) -> object:
    """A Guild Wars 2 guild id, which is always a GUID.

    Worth checking rather than accepting any text: the raffle ledger records
    the first guild id it is given and refuses a different one afterwards, so
    a typo claims the database for a guild that does not exist and the correct
    id is refused from then on.
    """
    canonical = normalize_guild_id(value)
    if canonical is None:
        raise ConfigurationError(
            "gw2_guild_id must be the guild's id from "
            "/v2/account.guild_leader, which looks like "
            "116E0C0E-0035-44A9-BB22-4AE3E23127E5"
        )
    # Stored in one spelling so the value on screen, the value compared
    # against the API and the value that claims the ledger are the same text.
    return canonical


def _parse_account_list(value: str) -> object:
    """A comma-separated list of Guild Wars 2 account names.

    Kept in the order it was given so /settings reads back the way it was
    typed, de-duplicated case-insensitively because the guild log and the
    roster disagree on capitalisation and one account listed twice would look
    like two exclusions.
    """
    seen: set[str] = set()
    accounts: list[str] = []
    for entry in value.split(","):
        account = entry.strip()
        if not account:
            continue
        key = account.casefold()
        if key in seen:
            continue
        seen.add(key)
        accounts.append(account)
    if not accounts:
        raise ConfigurationError(
            "List one or more account names separated by commas, for example "
            "Someone.1234, Another.5678. Pass a space to clear the list."
        )
    return tuple(accounts)


def _format_account_list(value: object) -> str:
    assert isinstance(value, tuple)
    return ", ".join(str(account) for account in value)


def _parse_text(name: str) -> Callable[[str], object]:
    def parse(value: str) -> object:
        return value.strip()

    return parse


def _parse_timezone(value: str) -> object:
    zone = value.strip()
    try:
        ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigurationError(
            "timezone must be a valid IANA timezone name, for example "
            "America/New_York"
        ) from exc
    return zone


def _parse_base_url(value: str) -> object:
    url = value.strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        raise ConfigurationError(
            "web_base_url must start with http:// or https://"
        )
    return url


def _parse_session_secret(value: str) -> object:
    secret = value.strip()
    if len(secret) < 32:
        raise ConfigurationError(
            "web_session_secret must be at least 32 characters"
        )
    return secret


SETTING_DEFINITIONS: tuple[SettingDefinition, ...] = (
    SettingDefinition(
        name="discord_notification_channel_id",
        field="discord_notification_channel_id",
        legacy_variable="DISCORD_NOTIFICATION_CHANNEL_ID",
        description=(
            "Channel that receives guild membership messages, raffle audit "
            "lines, feast stock alerts and Trial member reports, and whose "
            "description carries the guild member count. It must belong to "
            "the command server. Unset it and nothing is posted there."
        ),
        parse=_parse_discord_id("discord_notification_channel_id"),
        validates=ValidationTarget.TEXT_CHANNEL,
    ),
    SettingDefinition(
        name="gw2_api_key",
        field="gw2_api_key",
        legacy_variable="GW2_API_KEY",
        encrypted=True,
        description=(
            "Guild Wars 2 API key with the account and guilds permissions. "
            "Set it together with gw2_guild_id; without both, Guild Storage "
            "and guild log polling, feast alerts, raffle deposit tracking and "
            "the guild member lookups stay switched off."
        ),
        parse=_parse_text("gw2_api_key"),
    ),
    SettingDefinition(
        name="gw2_guild_id",
        field="gw2_guild_id",
        legacy_variable="GW2_GUILD_ID",
        description=(
            "Guild id listed in /v2/account.guild_leader. The API key must "
            "lead this guild. The raffle database records the first guild id "
            "it is given and refuses a different one afterwards, so it is "
            "checked against the API before it is stored."
        ),
        parse=_parse_guild_id,
    ),
    SettingDefinition(
        name="raffle_excluded_accounts",
        field="raffle_excluded_accounts",
        default=(),
        description=(
            "Guild Wars 2 account names barred from the raffle, separated by "
            "commas. They earn no tickets from gold they deposit, and "
            "/raffle addticket refuses them. Gold they deposit is still "
            "recorded as a contribution."
        ),
        parse=_parse_account_list,
        serialize=_format_account_list,
    ),
    SettingDefinition(
        name="feast_notification_user_id",
        field="discord_feast_notification_user_id",
        legacy_variable="DISCORD_FEAST_NOTIFICATION_USER_ID",
        description=(
            "Discord user who also receives feast stock alerts by private "
            "message. Unset it and the alerts only go to the notification "
            "channel."
        ),
        parse=_parse_discord_id("feast_notification_user_id"),
    ),
    SettingDefinition(
        name="gw2_poll_interval_seconds",
        field="poll_interval_seconds",
        legacy_variable="GW2_POLL_INTERVAL_SECONDS",
        default=300,
        description=(
            "How often Guild Storage is polled, in seconds. At least 30."
        ),
        parse=_parse_interval("gw2_poll_interval_seconds", 30),
    ),
    SettingDefinition(
        name="guild_log_poll_interval_seconds",
        field="guild_log_poll_interval_seconds",
        legacy_variable="GW2_GUILD_LOG_POLL_INTERVAL_SECONDS",
        default=60,
        description=(
            "How often the guild log is polled, in seconds. At least 30. "
            "This is what turns gold deposits into raffle tickets, so a "
            "longer interval delays ticket purchases."
        ),
        parse=_parse_interval("guild_log_poll_interval_seconds", 30),
    ),
    SettingDefinition(
        name="gw2_guild_member_cache_seconds",
        field="guild_member_cache_seconds",
        legacy_variable="GW2_GUILD_MEMBER_CACHE_SECONDS",
        default=900,
        description=(
            "How long the in-game guild roster is cached, in seconds. It "
            "backs the account-name autocompletes and the membership checks."
        ),
        parse=_parse_positive_int("gw2_guild_member_cache_seconds"),
    ),
    SettingDefinition(
        name="timezone",
        field="event_timezone",
        legacy_variable="TZ",
        # TZ is read once on upgrade and then never again, but it is also the
        # variable the container uses for its own clock and log timestamps, so
        # it must not be named in the "remove these" warning.
        warn_when_present=False,
        default="UTC",
        description=(
            "IANA timezone name used to read the times typed into /event new, "
            "to name event threads, and to define which day a weekly repeat "
            "falls on. For example America/New_York. Taken from TZ once on "
            "upgrade; leave TZ set, because the container uses it too."
        ),
        parse=_parse_timezone,
    ),
    SettingDefinition(
        name="web_base_url",
        field="web_base_url",
        legacy_variable="WEB_BASE_URL",
        description=(
            "Public base URL of the web calendar, without a trailing slash. "
            "Register <web_base_url>/oauth/callback as a redirect URI on the "
            "bot's Discord application. Needs WEB_ENABLED=true to take "
            "effect."
        ),
        parse=_parse_base_url,
    ),
    SettingDefinition(
        name="discord_oauth_client_id",
        field="discord_oauth_client_id",
        legacy_variable="DISCORD_OAUTH_CLIENT_ID",
        description=(
            "OAuth2 client id of the bot's Discord application, from the "
            "OAuth2 tab of the developer portal. Used to sign members in to "
            "the web calendar."
        ),
        parse=_parse_text("discord_oauth_client_id"),
    ),
    SettingDefinition(
        name="discord_oauth_client_secret",
        field="discord_oauth_client_secret",
        legacy_variable="DISCORD_OAUTH_CLIENT_SECRET",
        encrypted=True,
        description=(
            "OAuth2 client secret of the bot's Discord application, from the "
            "same OAuth2 tab as the client id."
        ),
        parse=_parse_text("discord_oauth_client_secret"),
    ),
    SettingDefinition(
        name="web_session_secret",
        field="web_session_secret",
        legacy_variable="WEB_SESSION_SECRET",
        encrypted=True,
        description=(
            "Random secret of at least 32 characters that signs web calendar "
            "session cookies. Changing it signs every calendar user out. "
            "Generate one with: python -c \"import secrets; "
            "print(secrets.token_urlsafe(48))\""
        ),
        parse=_parse_session_secret,
    ),
    SettingDefinition(
        name="web_session_ttl_seconds",
        field="web_session_ttl_seconds",
        legacy_variable="WEB_SESSION_TTL_SECONDS",
        default=604800,
        description=(
            "How long a web calendar sign-in stays valid, in seconds. Guild "
            "membership is re-checked periodically regardless, so a member "
            "who leaves loses access without waiting for this to expire."
        ),
        parse=_parse_positive_int("web_session_ttl_seconds"),
    ),
    SettingDefinition(
        name="raffle_draw",
        field="raffle_draw_role_id",
        group=ROLES_GROUP,
        default=DEFAULT_RAFFLE_DRAW_ROLE_ID,
        description=(
            "Role allowed to run /raffle draw and /raffle removetickets. The "
            "feast usage dashboard follows this role unless roles food_page "
            "is set apart."
        ),
        parse=_parse_discord_id("raffle_draw"),
        validates=ValidationTarget.ROLE,
    ),
    SettingDefinition(
        name="raffle_addticket",
        field="raffle_addticket_role_id",
        group=ROLES_GROUP,
        default=DEFAULT_RAFFLE_ADDTICKET_ROLE_ID,
        description=(
            "Role allowed to hand out free raffle tickets with /raffle "
            "addticket, /raffle addtickets and /raffle bulkaddtickets."
        ),
        parse=_parse_discord_id("raffle_addticket"),
        validates=ValidationTarget.ROLE,
    ),
    SettingDefinition(
        name="raffle_officer",
        field="raffle_officer_role_id",
        group=ROLES_GROUP,
        default=DEFAULT_RAFFLE_OFFICER_ROLE_ID,
        description=(
            "Role allowed to record a gold purchase on someone's behalf, to "
            "run /check and /track, and to use /settings. The server owner "
            "and anyone with Administrator can always use /settings, so a "
            "mistake here cannot lock the bot out of being reconfigured."
        ),
        parse=_parse_discord_id("raffle_officer"),
        validates=ValidationTarget.ROLE,
    ),
    SettingDefinition(
        name="guild_roster",
        field="guild_roster_role_id",
        group=ROLES_GROUP,
        default=DEFAULT_GUILD_ROSTER_ROLE_ID,
        description=(
            "Role whose members are offered in-game account names by the "
            "raffle command autocompletes."
        ),
        parse=_parse_discord_id("guild_roster"),
        validates=ValidationTarget.ROLE,
    ),
    SettingDefinition(
        name="event_create",
        field="event_create_role_id",
        group=ROLES_GROUP,
        default=DEFAULT_EVENT_CREATE_ROLE_ID,
        description=(
            "Role allowed to create, edit, move, cancel and delete guild "
            "events and to change their rosters."
        ),
        parse=_parse_discord_id("event_create"),
        validates=ValidationTarget.ROLE,
    ),
    SettingDefinition(
        name="trial",
        field="trial_role_id",
        group=ROLES_GROUP,
        default=DEFAULT_TRIAL_ROLE_ID,
        description=(
            "Role that marks a Discord member as a Trial, reported by /check "
            "and the overdue Trial member report."
        ),
        parse=_parse_discord_id("trial"),
        validates=ValidationTarget.ROLE,
    ),
    SettingDefinition(
        name="sunborne",
        field="sunborne_role_id",
        group=ROLES_GROUP,
        default=DEFAULT_SUNBORNE_ROLE_ID,
        description=(
            "Role that marks a Discord member as a full member, reported by "
            "/check and the overdue Trial member report."
        ),
        parse=_parse_discord_id("sunborne"),
        validates=ValidationTarget.ROLE,
    ),
    SettingDefinition(
        name="food_page",
        field="food_page_role_id",
        group=ROLES_GROUP,
        default_from="raffle_draw",
        description=(
            "Role allowed to open the feast usage dashboard on the web "
            "calendar. While it is unset it follows roles raffle_draw, which "
            "is how the page has always been gated."
        ),
        parse=_parse_discord_id("food_page"),
        validates=ValidationTarget.ROLE,
    ),
    SettingDefinition(
        name="raffle_contribution",
        field="raffle_contribution_channel_id",
        group=CHANNELS_GROUP,
        default=DEFAULT_RAFFLE_CONTRIBUTION_CHANNEL_ID,
        description=(
            "Channel that receives ticket purchase embeds, reward-tier "
            "milestones and the six-hourly contribution report. It is "
            "separate from the notification channel and must belong to the "
            "command server."
        ),
        parse=_parse_discord_id("raffle_contribution"),
        validates=ValidationTarget.TEXT_CHANNEL,
    ),
    SettingDefinition(
        name="trial_forum",
        field="trial_forum_channel_id",
        group=CHANNELS_GROUP,
        default=DEFAULT_TRIAL_FORUM_CHANNEL_ID,
        description=(
            "Forum channel holding Trial applications. The bot indexes it to "
            "link applications to Discord members and tags new posts as In "
            "Review. Set this before the two tag settings below, which are "
            "checked against whichever forum is configured."
        ),
        parse=_parse_discord_id("trial_forum"),
        validates=ValidationTarget.FORUM_CHANNEL,
    ),
    SettingDefinition(
        name="trial_accepted_tag",
        field="trial_accepted_tag_id",
        group=CHANNELS_GROUP,
        default=DEFAULT_TRIAL_ACCEPTED_TAG_ID,
        description=(
            "Tag in the Trial application forum that marks an accepted "
            "application. Only tagged posts are indexed."
        ),
        parse=_parse_discord_id("trial_accepted_tag"),
        validates=ValidationTarget.FORUM_TAG,
    ),
    SettingDefinition(
        name="trial_in_review_tag",
        field="trial_in_review_tag_id",
        group=CHANNELS_GROUP,
        default=DEFAULT_TRIAL_IN_REVIEW_TAG_ID,
        description=(
            "Tag the bot applies to a new Trial application post so it shows "
            "as being reviewed."
        ),
        parse=_parse_discord_id("trial_in_review_tag"),
        validates=ValidationTarget.FORUM_TAG,
    ),
)


def _by_key() -> Mapping[str, SettingDefinition]:
    keys: dict[str, SettingDefinition] = {}
    for definition in SETTING_DEFINITIONS:
        key = setting_key(definition)
        if key in keys:
            raise ValueError(f"Duplicate setting key: {key}")
        keys[key] = definition
    return keys


def setting_key(definition: SettingDefinition) -> str:
    """Stored key for a setting: unique across the groups, stable over time."""
    if definition.group is None:
        return definition.name
    return f"{definition.group}.{definition.name}"


SETTINGS_BY_KEY: Mapping[str, SettingDefinition] = _by_key()

LEGACY_SETTINGS: tuple[SettingDefinition, ...] = tuple(
    definition
    for definition in SETTING_DEFINITIONS
    if definition.legacy_variable is not None
)

LEGACY_VARIABLES: tuple[str, ...] = tuple(
    definition.legacy_variable
    for definition in LEGACY_SETTINGS
    if definition.legacy_variable is not None
)


def definition_for(name: str, group: str | None = None) -> SettingDefinition:
    key = name if group is None else f"{group}.{name}"
    definition = SETTINGS_BY_KEY.get(key)
    if definition is None:
        raise KeyError(f"Unknown setting: {key}")
    return definition


def definition_for_variable(variable: str) -> SettingDefinition | None:
    for definition in LEGACY_SETTINGS:
        if definition.legacy_variable == variable:
            return definition
    return None


def definitions_in_group(group: str | None) -> tuple[SettingDefinition, ...]:
    return tuple(
        definition
        for definition in SETTING_DEFINITIONS
        if definition.group == group
    )


def setting_text(definition: SettingDefinition, value: object) -> str:
    """The text to store for a parsed value.

    str() is right for every scalar, and wrong for a collection: storing
    str(tuple) would write a value its own parser rejects, so the setting
    would read as unreadable the moment anybody looked at it.
    """
    if definition.serialize is not None:
        return definition.serialize(value)
    return str(value)
