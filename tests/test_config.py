from unittest.mock import patch

from gw2bot.config import Config, ConfigurationError
import pytest


class TestConfig:
    def test_reads_required_values_and_defaults(self) -> None:
        config = Config.from_env(
            {
                "DISCORD_TOKEN": "discord-token",
                "DISCORD_COMMAND_GUILD_ID": "5678",
                "DISCORD_NOTIFICATION_CHANNEL_ID": "9012",
                "GW2_API_KEY": "gw2-key",
                "GW2_GUILD_ID": "guild-id",
            }
        )

        assert config.discord_command_guild_id == 5678
        assert config.discord_notification_channel_id == 9012
        assert config.discord_feast_notification_user_id is None
        assert config.gw2_guild_id == "guild-id"
        assert config.poll_interval_seconds == 300
        assert config.guild_log_poll_interval_seconds == 60
        assert config.guild_member_cache_seconds == 900
        assert config.raffle_db_path == "data/gw2bot.db"
        assert config.gw2_api_base_url == "https://api.guildwars2.com"
        assert not config.debug

    def test_reads_debug_boolean(self) -> None:
        config = Config.from_env(
            {
                "DISCORD_TOKEN": "discord-token",
                "DISCORD_COMMAND_GUILD_ID": "5678",
                "DISCORD_NOTIFICATION_CHANNEL_ID": "9012",
                "GW2_API_KEY": "gw2-key",
                "GW2_GUILD_ID": "guild-id",
                "DEBUG": "true",
            }
        )

        assert config.debug

    def test_rejects_invalid_debug_boolean(self) -> None:
        with pytest.raises(ConfigurationError, match="DEBUG must be true or false"):
            Config.from_env(
                {
                    "DISCORD_TOKEN": "discord-token",
                    "DISCORD_COMMAND_GUILD_ID": "5678",
                    "DISCORD_NOTIFICATION_CHANNEL_ID": "9012",
                    "GW2_API_KEY": "gw2-key",
                    "GW2_GUILD_ID": "guild-id",
                    "DEBUG": "sometimes",
                }
            )

    def test_reads_optional_feast_notification_user_id(self) -> None:
        config = Config.from_env(
            {
                "DISCORD_TOKEN": "discord-token",
                "DISCORD_COMMAND_GUILD_ID": "5678",
                "DISCORD_NOTIFICATION_CHANNEL_ID": "9012",
                "DISCORD_FEAST_NOTIFICATION_USER_ID": "3456",
                "GW2_API_KEY": "gw2-key",
                "GW2_GUILD_ID": "guild-id",
            }
        )

        assert config.discord_feast_notification_user_id == 3456

    def test_blank_optional_path_and_url_use_defaults(self) -> None:
        config = Config.from_env(
            {
                "DISCORD_TOKEN": "discord-token",
                "DISCORD_COMMAND_GUILD_ID": "5678",
                "DISCORD_NOTIFICATION_CHANNEL_ID": "9012",
                "GW2_API_KEY": "gw2-key",
                "GW2_GUILD_ID": "guild-id",
                "RAFFLE_DB_PATH": "   ",
                "GW2_API_BASE_URL": "\t",
            }
        )

        assert config.raffle_db_path == "data/gw2bot.db"
        assert config.gw2_api_base_url == "https://api.guildwars2.com"

    def test_strips_optional_path_and_url_values(self) -> None:
        config = Config.from_env(
            {
                "DISCORD_TOKEN": "discord-token",
                "DISCORD_COMMAND_GUILD_ID": "5678",
                "DISCORD_NOTIFICATION_CHANNEL_ID": "9012",
                "GW2_API_KEY": "gw2-key",
                "GW2_GUILD_ID": "guild-id",
                "RAFFLE_DB_PATH": " custom.db ",
                "GW2_API_BASE_URL": " https://example.test/ ",
            }
        )

        assert config.raffle_db_path == "custom.db"
        assert config.gw2_api_base_url == "https://example.test"

    def test_rejects_invalid_feast_notification_user_id(self) -> None:
        with pytest.raises(
            ConfigurationError,
            match="DISCORD_FEAST_NOTIFICATION_USER_ID must be greater than zero",
        ):
            Config.from_env(
                {
                    "DISCORD_TOKEN": "discord-token",
                    "DISCORD_COMMAND_GUILD_ID": "5678",
                    "DISCORD_NOTIFICATION_CHANNEL_ID": "9012",
                    "DISCORD_FEAST_NOTIFICATION_USER_ID": "0",
                    "GW2_API_KEY": "gw2-key",
                    "GW2_GUILD_ID": "guild-id",
                }
            )

    def test_reports_all_missing_required_values(self) -> None:
        with pytest.raises(
            ConfigurationError,
            match=(
                "DISCORD_TOKEN, DISCORD_COMMAND_GUILD_ID, "
                "DISCORD_NOTIFICATION_CHANNEL_ID, GW2_API_KEY, GW2_GUILD_ID"
            ),
        ):
            Config.from_env({})

    def test_rejects_short_poll_interval(self) -> None:
        with pytest.raises(ConfigurationError, match="must be at least 30"):
            Config.from_env(
                {
                    "DISCORD_TOKEN": "discord-token",
                    "DISCORD_COMMAND_GUILD_ID": "5678",
                    "DISCORD_NOTIFICATION_CHANNEL_ID": "9012",
                    "GW2_API_KEY": "gw2-key",
                    "GW2_GUILD_ID": "guild-id",
                    "GW2_POLL_INTERVAL_SECONDS": "10",
                }
            )

    def test_rejects_short_guild_log_poll_interval(self) -> None:
        with pytest.raises(ConfigurationError, match="must be at least 30"):
            Config.from_env(
                {
                    "DISCORD_TOKEN": "discord-token",
                    "DISCORD_COMMAND_GUILD_ID": "5678",
                    "DISCORD_NOTIFICATION_CHANNEL_ID": "9012",
                    "GW2_API_KEY": "gw2-key",
                    "GW2_GUILD_ID": "guild-id",
                    "GW2_GUILD_LOG_POLL_INTERVAL_SECONDS": "10",
                }
            )

    @patch("gw2bot.config.load_dotenv")
    @patch.dict(
        "os.environ",
        {
            "DISCORD_TOKEN": "runtime-token",
            "DISCORD_COMMAND_GUILD_ID": "5678",
            "DISCORD_NOTIFICATION_CHANNEL_ID": "9012",
            "GW2_API_KEY": "runtime-key",
            "GW2_GUILD_ID": "runtime-guild",
        },
        clear=True,
    )
    def test_loads_dotenv_without_overriding_runtime_environment(
        self,
        load_dotenv: object,
    ) -> None:
        config = Config.from_env()

        assert config.discord_token == "runtime-token"
        assert config.gw2_api_key == "runtime-key"
        assert config.gw2_guild_id == "runtime-guild"
        load_dotenv.assert_called_once_with(override=False)  # type: ignore[attr-defined]
