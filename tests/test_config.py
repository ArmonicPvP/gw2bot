from unittest.mock import patch

import pytest

from factories import GW2_GUILD_ID, config_from_env
from gw2bot.config import (
    BOOTSTRAP_VARIABLES,
    Config,
    ConfigurationError,
    bootstrap_from_env,
    missing_configuration_message,
)
from gw2bot.settings.definitions import LEGACY_VARIABLES

BOOTSTRAP = {
    "DISCORD_TOKEN": "discord-token",
    "DISCORD_COMMAND_GUILD_ID": "5678",
}


def environment(**overrides: str) -> dict[str, str]:
    return {**BOOTSTRAP, **overrides}


class TestBootstrap:
    def test_reads_required_values_and_defaults(self) -> None:
        bootstrap = bootstrap_from_env(environment())

        assert bootstrap.discord_token == "discord-token"
        assert bootstrap.discord_command_guild_id == 5678
        assert bootstrap.raffle_db_path == "data/gw2bot.db"
        assert bootstrap.gw2_api_base_url == "https://api.guildwars2.com"
        assert not bootstrap.debug
        assert not bootstrap.web_enabled
        assert bootstrap.web_port == 2222

    def test_reports_all_missing_required_values(self) -> None:
        with pytest.raises(
            ConfigurationError,
            match="DISCORD_TOKEN, DISCORD_COMMAND_GUILD_ID",
        ):
            bootstrap_from_env({})

    def test_reads_debug_boolean(self) -> None:
        assert bootstrap_from_env(environment(DEBUG="true")).debug

    def test_rejects_invalid_debug_boolean(self) -> None:
        with pytest.raises(ConfigurationError, match="DEBUG must be true or false"):
            bootstrap_from_env(environment(DEBUG="sometimes"))

    def test_rejects_invalid_command_guild_id(self) -> None:
        with pytest.raises(
            ConfigurationError,
            match="DISCORD_COMMAND_GUILD_ID must be greater than zero",
        ):
            bootstrap_from_env({**BOOTSTRAP, "DISCORD_COMMAND_GUILD_ID": "0"})

    def test_rejects_invalid_port(self) -> None:
        with pytest.raises(
            ConfigurationError,
            match="WEB_PORT must be greater than zero",
        ):
            bootstrap_from_env(environment(WEB_PORT="0"))

    def test_blank_optional_path_and_url_use_defaults(self) -> None:
        bootstrap = bootstrap_from_env(
            environment(RAFFLE_DB_PATH="   ", GW2_API_BASE_URL="\t")
        )

        assert bootstrap.raffle_db_path == "data/gw2bot.db"
        assert bootstrap.gw2_api_base_url == "https://api.guildwars2.com"

    def test_strips_optional_path_and_url_values(self) -> None:
        bootstrap = bootstrap_from_env(
            environment(
                RAFFLE_DB_PATH=" custom.db ",
                GW2_API_BASE_URL=" https://example.test/ ",
            )
        )

        assert bootstrap.raffle_db_path == "custom.db"
        assert bootstrap.gw2_api_base_url == "https://example.test"

    @patch("gw2bot.config.load_dotenv")
    @patch.dict(
        "os.environ",
        {"DISCORD_TOKEN": "runtime-token", "DISCORD_COMMAND_GUILD_ID": "5678"},
        clear=True,
    )
    def test_loads_dotenv_without_overriding_runtime_environment(
        self,
        load_dotenv: object,
    ) -> None:
        bootstrap = bootstrap_from_env()

        assert bootstrap.discord_token == "runtime-token"
        load_dotenv.assert_called_once_with(override=False)  # type: ignore[attr-defined]

    def test_no_variable_is_both_bootstrap_and_setting(self) -> None:
        # A variable in both tiers would be read twice with different rules,
        # and the operator could not tell which one won.
        assert not set(BOOTSTRAP_VARIABLES) & set(LEGACY_VARIABLES)


class TestComposedConfiguration:
    def test_composes_the_settings_onto_the_bootstrap(self) -> None:
        config = config_from_env(
            environment(
                DISCORD_NOTIFICATION_CHANNEL_ID="9012",
                GW2_API_KEY="gw2-key",
                GW2_GUILD_ID=GW2_GUILD_ID,
            )
        )

        assert config.discord_command_guild_id == 5678
        assert config.discord_notification_channel_id == 9012
        assert config.gw2_guild_id == GW2_GUILD_ID
        assert config.discord_feast_notification_user_id is None
        assert config.poll_interval_seconds == 300
        assert config.guild_log_poll_interval_seconds == 60
        assert config.guild_member_cache_seconds == 900
        assert config.event_timezone == "UTC"

    def test_reads_optional_feast_notification_user_id(self) -> None:
        config = config_from_env(
            environment(DISCORD_FEAST_NOTIFICATION_USER_ID="3456")
        )

        assert config.discord_feast_notification_user_id == 3456

    def test_starts_without_notification_channel_and_gw2_credentials(self) -> None:
        config = config_from_env(environment())

        assert config.discord_notification_channel_id is None
        assert config.gw2_api_key is None
        assert config.gw2_guild_id is None
        assert not config.notifications_enabled
        assert not config.gw2_api_enabled
        assert config.missing_gw2_api_settings == ("gw2_api_key", "gw2_guild_id")

    def test_blank_optional_credentials_are_treated_as_unset(self) -> None:
        config = config_from_env(
            environment(
                DISCORD_NOTIFICATION_CHANNEL_ID="   ",
                GW2_API_KEY="\t",
                GW2_GUILD_ID=" ",
            )
        )

        assert config.discord_notification_channel_id is None
        assert config.gw2_api_key is None
        assert config.gw2_guild_id is None

    def test_reports_only_the_unset_half_of_the_gw2_credentials(self) -> None:
        config = config_from_env(environment(GW2_API_KEY="gw2-key"))

        assert config.missing_gw2_api_settings == ("gw2_guild_id",)
        assert not config.gw2_api_enabled

    def test_all_features_are_enabled_when_every_value_is_set(self) -> None:
        config = config_from_env(
            environment(
                DISCORD_NOTIFICATION_CHANNEL_ID="9012",
                GW2_API_KEY="gw2-key",
                GW2_GUILD_ID=GW2_GUILD_ID,
            )
        )

        assert config.notifications_enabled
        assert config.gw2_api_enabled
        assert config.missing_gw2_api_settings == ()

    def test_ships_the_discord_ids_as_defaults(self) -> None:
        config = config_from_env(environment())

        assert config.raffle_officer_role_id == 1317638909735342201
        assert config.trial_forum_channel_id == 1317206104727621693
        # The feast dashboard has always been gated on the raffle draw role.
        assert config.food_page_role_id == config.raffle_draw_role_id

    @pytest.mark.parametrize(
        ("settings", "expected"),
        (
            (
                ("gw2_api_key", "gw2_guild_id"),
                "This command is disabled. Run the /settings gw2_api_key, "
                "/settings gw2_guild_id commands to enable it.",
            ),
            (
                ("gw2_guild_id",),
                "This command is disabled. Run the /settings gw2_guild_id "
                "command to enable it.",
            ),
        ),
    )
    def test_missing_configuration_message_names_every_unset_setting(
        self,
        settings: tuple[str, ...],
        expected: str,
    ) -> None:
        assert missing_configuration_message(settings) == expected


class TestInvalidStoredValues:
    """A value that no longer parses reads as unset rather than crashing.

    These arrive through the legacy import and through /settings, and by the
    time they are read the bot is already running - a raise here would take
    the whole configuration down over one bad row.
    """

    def test_short_poll_interval_is_ignored(self) -> None:
        config = config_from_env(environment(GW2_POLL_INTERVAL_SECONDS="10"))

        assert config.poll_interval_seconds == 300

    def test_short_guild_log_poll_interval_is_ignored(self) -> None:
        config = config_from_env(
            environment(GW2_GUILD_LOG_POLL_INTERVAL_SECONDS="10")
        )

        assert config.guild_log_poll_interval_seconds == 60

    def test_invalid_notification_channel_id_is_ignored(self) -> None:
        config = config_from_env(environment(DISCORD_NOTIFICATION_CHANNEL_ID="0"))

        assert config.discord_notification_channel_id is None

    def test_invalid_timezone_is_ignored(self) -> None:
        config = config_from_env(environment(TZ="Mars/Olympus_Mons"))

        assert config.event_timezone == "UTC"

    def test_accepts_a_valid_timezone(self) -> None:
        config = config_from_env(environment(TZ="America/New_York"))

        assert config.event_timezone == "America/New_York"


class TestWebCalendarConfiguration:
    WEB = {
        "WEB_BASE_URL": "https://calendar.example.test",
        "DISCORD_OAUTH_CLIENT_ID": "client-id",
        "DISCORD_OAUTH_CLIENT_SECRET": "client-secret",
        "WEB_SESSION_SECRET": "s" * 32,
    }

    def test_web_disabled_by_default_but_still_reads_its_settings(self) -> None:
        config = config_from_env(environment(**self.WEB))

        # WEB_ENABLED opens the listening socket, so it stays a bootstrap
        # variable; the four credentials are settings and are read regardless.
        assert not config.web_enabled
        assert not config.web_calendar_enabled
        assert config.web_base_url == "https://calendar.example.test"
        assert config.discord_oauth_client_id == "client-id"
        assert config.web_session_ttl_seconds == 604800

    def test_web_enabled_reads_all_web_values(self) -> None:
        config = config_from_env(
            environment(
                WEB_ENABLED="true",
                WEB_PORT="9090",
                **{**self.WEB, "WEB_BASE_URL": "https://calendar.example.test/"},
            )
        )

        assert config.web_enabled
        assert config.web_calendar_enabled
        assert config.web_port == 9090
        assert config.web_base_url == "https://calendar.example.test"
        assert config.discord_oauth_client_secret == "client-secret"
        assert config.web_session_secret == "s" * 32
        assert config.missing_web_settings == ()

    def test_web_enabled_without_its_settings_is_not_fatal(self) -> None:
        # The calendar is an optional extra. Switching it on before its
        # credentials are set names them and leaves it off, rather than
        # stopping a bot that also runs the raffle, trials and events.
        config = config_from_env(environment(WEB_ENABLED="true"))

        assert config.web_enabled
        assert not config.web_calendar_enabled
        assert config.missing_web_settings == (
            "web_base_url",
            "discord_oauth_client_id",
            "discord_oauth_client_secret",
            "web_session_secret",
        )

    def test_invalid_base_url_scheme_is_ignored(self) -> None:
        config = config_from_env(
            environment(WEB_ENABLED="true", **{**self.WEB, "WEB_BASE_URL": "x.test"})
        )

        assert config.web_base_url is None
        assert not config.web_calendar_enabled

    def test_short_session_secret_is_ignored(self) -> None:
        config = config_from_env(
            environment(WEB_ENABLED="true", **{**self.WEB, "WEB_SESSION_SECRET": "s"})
        )

        assert config.web_session_secret is None
        assert config.missing_web_settings == ("web_session_secret",)

    def test_web_session_ttl_is_configurable(self) -> None:
        config = config_from_env(environment(WEB_SESSION_TTL_SECONDS="3600"))

        assert config.web_session_ttl_seconds == 3600

    def test_non_positive_session_ttl_is_ignored(self) -> None:
        config = config_from_env(environment(WEB_SESSION_TTL_SECONDS="0"))

        assert config.web_session_ttl_seconds == 604800


class TestMigrationIsANoOp:
    def test_a_fully_populated_environment_composes_to_the_same_values(
        self,
    ) -> None:
        """Upgrading must not change a single value for an existing operator.

        The environment below is what a fully configured install had before
        settings existed; composing it has to land on exactly the Config the
        bot used to build from it.
        """
        config = config_from_env(
            {
                "DISCORD_TOKEN": "discord-token",
                "DISCORD_COMMAND_GUILD_ID": "5678",
                "DISCORD_NOTIFICATION_CHANNEL_ID": "9012",
                "DISCORD_FEAST_NOTIFICATION_USER_ID": "3456",
                "GW2_API_KEY": "gw2-key",
                "GW2_GUILD_ID": GW2_GUILD_ID,
                "GW2_POLL_INTERVAL_SECONDS": "600",
                "GW2_GUILD_LOG_POLL_INTERVAL_SECONDS": "120",
                "GW2_GUILD_MEMBER_CACHE_SECONDS": "1800",
                "GW2_API_BASE_URL": "https://api.example.test",
                "RAFFLE_DB_PATH": "/app/data/gw2bot.db",
                "TZ": "America/New_York",
                "DEBUG": "true",
                "WEB_ENABLED": "true",
                "WEB_PORT": "9090",
                "WEB_BASE_URL": "https://calendar.example.test",
                "DISCORD_OAUTH_CLIENT_ID": "client-id",
                "DISCORD_OAUTH_CLIENT_SECRET": "client-secret",
                "WEB_SESSION_SECRET": "s" * 32,
                "WEB_SESSION_TTL_SECONDS": "3600",
            }
        )

        assert config == Config(
            discord_token="discord-token",
            discord_command_guild_id=5678,
            discord_notification_channel_id=9012,
            discord_feast_notification_user_id=3456,
            gw2_api_key="gw2-key",
            gw2_guild_id=GW2_GUILD_ID,
            poll_interval_seconds=600,
            guild_log_poll_interval_seconds=120,
            guild_member_cache_seconds=1800,
            raffle_db_path="/app/data/gw2bot.db",
            gw2_api_base_url="https://api.example.test",
            event_timezone="America/New_York",
            debug=True,
            web_enabled=True,
            web_port=9090,
            web_base_url="https://calendar.example.test",
            discord_oauth_client_id="client-id",
            discord_oauth_client_secret="client-secret",
            web_session_secret="s" * 32,
            web_session_ttl_seconds=3600,
        )
