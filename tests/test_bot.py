import logging
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from factories import forbidden_error
from gw2bot.bot import Gw2Bot
from gw2bot.config import Config
from gw2bot.main import main as run_main
from gw2bot.events.views import (
    EventSettingsButton,
    EventSignOutButton,
    EventSignUpButton,
)
from gw2bot.raffle.views import RaffleAuditRangesButton


class TestCommand:
    @patch("gw2bot.main.Gw2Bot")
    @patch("gw2bot.main.configure_logging")
    @patch("gw2bot.main.Config.from_env")
    def test_registers_all_configured_credentials_with_console_redaction(
        self,
        from_env: MagicMock,
        configure: MagicMock,
        bot_class: MagicMock,
    ) -> None:
        config = SimpleNamespace(
            debug=True,
            gw2_api_key="gw2-secret",
            discord_token="discord-secret",
            discord_oauth_client_secret="oauth-secret",
            web_session_secret="session-secret",
        )
        from_env.return_value = config

        run_main()

        configure.assert_called_once_with(
            True,
            (
                "gw2-secret",
                "discord-secret",
                "oauth-secret",
                "session-secret",
            ),
        )
        bot_class.assert_called_once_with(config)
        bot_class.return_value.run.assert_called_once_with(
            "discord-secret",
            log_handler=None,
        )

    @patch("gw2bot.main.Gw2Bot")
    @patch("gw2bot.main.configure_logging")
    @patch("gw2bot.main.Config.from_env")
    def test_registers_blank_placeholders_for_unset_web_secrets(
        self,
        from_env: MagicMock,
        configure: MagicMock,
        bot_class: MagicMock,
    ) -> None:
        config = SimpleNamespace(
            debug=False,
            gw2_api_key="gw2-secret",
            discord_token="discord-secret",
            discord_oauth_client_secret=None,
            web_session_secret=None,
        )
        from_env.return_value = config

        run_main()

        configure.assert_called_once_with(
            False,
            ("gw2-secret", "discord-secret", "", ""),
        )


class TestCommandSync:
    def setup_method(self) -> None:
        self.config = Config.from_env(
            {
                "DISCORD_TOKEN": "discord-token",
                "DISCORD_COMMAND_GUILD_ID": "5678",
                "DISCORD_NOTIFICATION_CHANNEL_ID": "9012",
                "GW2_API_KEY": "gw2-key",
                "GW2_GUILD_ID": "guild-id",
            }
        )
        self.tree = MagicMock()

    async def test_missing_guild_access_does_not_stop_monitoring(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        self.tree.sync = AsyncMock(side_effect=forbidden_error(50001))
        bot = SimpleNamespace(_config=self.config, tree=self.tree)

        with caplog.at_level(logging.ERROR, logger="gw2bot"):
            await Gw2Bot._sync_commands(bot)  # type: ignore[arg-type]

        assert "Missing Access" in caplog.text
        assert "Monitoring will continue" in caplog.text
        self.tree.clear_commands.assert_not_called()

    async def test_other_command_sync_permission_errors_are_raised(self) -> None:
        self.tree.sync = AsyncMock(side_effect=forbidden_error(50013))
        bot = SimpleNamespace(_config=self.config, tree=self.tree)

        with pytest.raises(discord.Forbidden):
            await Gw2Bot._sync_commands(bot)  # type: ignore[arg-type]


class TestBotIntent:
    @patch("gw2bot.bot.RaffleStore")
    def test_enables_guild_intent_to_resolve_interaction_roles(
        self,
        raffle_store: MagicMock,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Config.from_env(
                {
                    "DISCORD_TOKEN": "discord-token",
                    "DISCORD_COMMAND_GUILD_ID": "5678",
                    "DISCORD_NOTIFICATION_CHANNEL_ID": "9012",
                    "GW2_API_KEY": "gw2-key",
                    "GW2_GUILD_ID": "guild-id",
                    "RAFFLE_DB_PATH": str(Path(directory) / "raffle.db"),
                }
            )

            bot = Gw2Bot(config)
            # Release the SQLite file handle so Windows can delete the
            # temporary directory.
            bot.event_store.close()

        assert bot.intents.guilds
        assert bot.intents.guild_messages
        assert not bot.intents.members
        assert bot.intents.message_content
        raffle_store.assert_called_once()

    @patch("gw2bot.bot.RaffleStore")
    def test_registers_persistent_raffle_audit_pager(
        self,
        raffle_store: MagicMock,
    ) -> None:
        # Registration lets Discord dispatch audit pager clicks from any
        # old message, keeping /raffle audit pages reachable after the
        # original interaction ages out or the bot restarts.
        with tempfile.TemporaryDirectory() as directory:
            config = Config.from_env(
                {
                    "DISCORD_TOKEN": "discord-token",
                    "DISCORD_COMMAND_GUILD_ID": "5678",
                    "DISCORD_NOTIFICATION_CHANNEL_ID": "9012",
                    "GW2_API_KEY": "gw2-key",
                    "GW2_GUILD_ID": "guild-id",
                    "RAFFLE_DB_PATH": str(Path(directory) / "raffle.db"),
                }
            )
            with patch.object(Gw2Bot, "add_dynamic_items") as add_dynamic_items:
                bot = Gw2Bot(config)
            # Release the SQLite file handle so Windows can delete the
            # temporary directory.
            bot.event_store.close()

        add_dynamic_items.assert_any_call(RaffleAuditRangesButton)
        add_dynamic_items.assert_any_call(
            EventSignUpButton,
            EventSignOutButton,
            EventSettingsButton,
        )


class TestBotWebServer:
    def _config(self, tmp_path: Path, web_enabled: bool) -> Config:
        values = {
            "DISCORD_TOKEN": "discord-token",
            "DISCORD_COMMAND_GUILD_ID": "5678",
            "DISCORD_NOTIFICATION_CHANNEL_ID": "9012",
            "GW2_API_KEY": "gw2-key",
            "GW2_GUILD_ID": "guild-id",
            "RAFFLE_DB_PATH": str(tmp_path / "raffle.db"),
        }
        if web_enabled:
            values.update(
                {
                    "WEB_ENABLED": "true",
                    "WEB_BASE_URL": "http://localhost:8080",
                    "DISCORD_OAUTH_CLIENT_ID": "client-id",
                    "DISCORD_OAUTH_CLIENT_SECRET": "client-secret",
                    "WEB_SESSION_SECRET": "s" * 32,
                }
            )
        return Config.from_env(values)

    def _quiet_bot_patches(self):
        return patch.multiple(
            Gw2Bot,
            _sync_commands=AsyncMock(),
            _poll_guild_storage=AsyncMock(),
            _poll_guild_log=AsyncMock(),
            _poll_overdue_trials=AsyncMock(),
            _poll_raffle_contributions=AsyncMock(),
            _poll_guild_member_count_topic=AsyncMock(),
            _poll_event_updates=AsyncMock(),
        )

    @patch("gw2bot.bot.GuildMemberCache")
    @patch("gw2bot.bot.RaffleStore")
    async def test_setup_hook_starts_web_server_and_close_stops_it(
        self,
        raffle_store: MagicMock,
        member_cache: MagicMock,
        tmp_path: Path,
    ) -> None:
        member_cache.return_value.close = AsyncMock()
        bot = Gw2Bot(self._config(tmp_path, web_enabled=True))
        with (
            self._quiet_bot_patches(),
            patch("gw2bot.web.server.WebServer") as web_server_class,
        ):
            web_server = web_server_class.return_value
            web_server.start = AsyncMock()
            web_server.stop = AsyncMock()

            await bot.setup_hook()

            assert bot._web_server is web_server
            web_server_class.assert_called_once_with(
                bot,
                bot._config,
                bot._session,
            )
            web_server.start.assert_awaited_once()

            with patch.object(discord.Client, "close", AsyncMock()):
                await bot.close()

            web_server.stop.assert_awaited_once()

    @patch("gw2bot.bot.GuildMemberCache")
    @patch("gw2bot.bot.RaffleStore")
    async def test_setup_hook_survives_a_web_server_that_cannot_bind(
        self,
        raffle_store: MagicMock,
        member_cache: MagicMock,
        tmp_path: Path,
    ) -> None:
        # The calendar is an optional read-only extra. A taken port must not
        # cost the guild its raffles, trials and events.
        member_cache.return_value.close = AsyncMock()
        bot = Gw2Bot(self._config(tmp_path, web_enabled=True))
        with (
            self._quiet_bot_patches(),
            patch("gw2bot.web.server.WebServer") as web_server_class,
        ):
            web_server = web_server_class.return_value
            web_server.start = AsyncMock(
                side_effect=OSError("address already in use")
            )
            web_server.stop = AsyncMock()

            await bot.setup_hook()

            assert bot._web_server is None

            with patch.object(discord.Client, "close", AsyncMock()):
                await bot.close()

            # Nothing was ever started, so there is nothing to stop.
            web_server.stop.assert_not_awaited()

    @patch("gw2bot.bot.GuildMemberCache")
    @patch("gw2bot.bot.RaffleStore")
    async def test_setup_hook_skips_web_server_when_disabled(
        self,
        raffle_store: MagicMock,
        member_cache: MagicMock,
        tmp_path: Path,
    ) -> None:
        member_cache.return_value.close = AsyncMock()
        bot = Gw2Bot(self._config(tmp_path, web_enabled=False))
        with self._quiet_bot_patches():
            await bot.setup_hook()

            assert bot._web_server is None

            with patch.object(discord.Client, "close", AsyncMock()):
                await bot.close()


class TestStartupStatus:
    async def test_startup_status_is_logged_once_without_channel_notification(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        bot = SimpleNamespace(
            user="Test Bot",
            _ready_announced=False,
            _config=SimpleNamespace(
                poll_interval_seconds=300,
                guild_log_poll_interval_seconds=60,
            ),
            _try_send_notification=AsyncMock(),
        )

        with caplog.at_level(logging.INFO, logger="gw2bot"):
            await Gw2Bot.on_ready(cast(Gw2Bot, bot))
            await Gw2Bot.on_ready(cast(Gw2Bot, bot))

        bot._try_send_notification.assert_not_awaited()
        assert (
            sum(
                "GW2 bot connected to Discord. Storage polling every 300 seconds; "
                "guild log polling every 60 seconds; overdue Trial member reporting "
                "daily at 17:00 UTC; raffle contribution reporting every 6 hours "
                "UTC; guild member count topic updates every 60 seconds." in message
                for message in caplog.messages
            )
            == 1
        )
        assert bot._ready_announced
