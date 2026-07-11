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
        )
        from_env.return_value = config

        run_main()

        configure.assert_called_once_with(
            True,
            ("gw2-secret", "discord-secret"),
        )
        bot_class.assert_called_once_with(config)
        bot_class.return_value.run.assert_called_once_with(
            "discord-secret",
            log_handler=None,
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
                Gw2Bot(config)

        add_dynamic_items.assert_any_call(RaffleAuditRangesButton)
        add_dynamic_items.assert_any_call(
            EventSignUpButton,
            EventSignOutButton,
            EventSettingsButton,
        )


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
