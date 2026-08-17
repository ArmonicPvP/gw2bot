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
from gw2bot.raffle.views import (
    RaffleAuditRangesButton,
    RaffleContributionReportButton,
    RaffleLeaderboardButton,
    RaffleTicketsListButton,
)


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
        # Without registration these pagers would only work while their view
        # object stayed alive, so their arrows would start failing on a
        # message that is still on screen.
        add_dynamic_items.assert_any_call(
            RaffleTicketsListButton,
            RaffleLeaderboardButton,
            RaffleContributionReportButton,
        )
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


class TestOptionalConfiguration:
    ALWAYS_RUNNING = {
        "gw2-raffle-contribution-poller",
        "gw2-event-scheduler",
    }
    GW2_POLLERS = {
        "gw2-guild-storage-poller",
        "gw2-guild-log-poller",
    }
    NOTIFICATION_POLLERS = {
        "gw2-overdue-trial-poller",
        "gw2-guild-member-count-topic-poller",
    }

    def _config(self, tmp_path: Path, **values: str) -> Config:
        return Config.from_env(
            {
                "DISCORD_TOKEN": "discord-token",
                "DISCORD_COMMAND_GUILD_ID": "5678",
                "RAFFLE_DB_PATH": str(tmp_path / "raffle.db"),
                **values,
            }
        )

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

    async def _started_poll_task_names(self, config: Config) -> tuple[set[str], Gw2Bot]:
        bot = Gw2Bot(config)
        with self._quiet_bot_patches():
            await bot.setup_hook()
        return {task.get_name() for task in bot._poll_tasks}, bot

    async def _close(self, bot: Gw2Bot) -> None:
        with patch.object(discord.Client, "close", AsyncMock()):
            await bot.close()

    @patch("gw2bot.bot.GuildMemberCache")
    async def test_unset_gw2_credentials_start_the_bot_without_gw2_pollers(
        self,
        member_cache: MagicMock,
        tmp_path: Path,
    ) -> None:
        member_cache.return_value.close = AsyncMock()
        names, bot = await self._started_poll_task_names(
            self._config(tmp_path, DISCORD_NOTIFICATION_CHANNEL_ID="9012")
        )

        assert names == self.ALWAYS_RUNNING
        assert bot._api is None
        assert bot._guild_members is None
        member_cache.assert_not_called()

        await self._close(bot)

    @patch("gw2bot.bot.GuildMemberCache")
    async def test_unset_notification_channel_keeps_the_gw2_pollers(
        self,
        member_cache: MagicMock,
        tmp_path: Path,
    ) -> None:
        # Guild Storage and the guild log feed the raffle ledger and the feast
        # history, so they still earn their keep with nowhere to post.
        member_cache.return_value.close = AsyncMock()
        names, bot = await self._started_poll_task_names(
            self._config(
                tmp_path,
                GW2_API_KEY="gw2-key",
                GW2_GUILD_ID="guild-id",
            )
        )

        assert names == self.ALWAYS_RUNNING | self.GW2_POLLERS
        assert bot._api is not None

        await self._close(bot)

    @patch("gw2bot.bot.GuildMemberCache")
    async def test_full_configuration_starts_every_poller(
        self,
        member_cache: MagicMock,
        tmp_path: Path,
    ) -> None:
        member_cache.return_value.close = AsyncMock()
        names, bot = await self._started_poll_task_names(
            self._config(
                tmp_path,
                DISCORD_NOTIFICATION_CHANNEL_ID="9012",
                GW2_API_KEY="gw2-key",
                GW2_GUILD_ID="guild-id",
            )
        )

        assert names == (
            self.ALWAYS_RUNNING | self.GW2_POLLERS | self.NOTIFICATION_POLLERS
        )

        await self._close(bot)

    def test_disabled_features_are_logged_as_warnings(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        bot = Gw2Bot(self._config(tmp_path))

        with caplog.at_level(logging.WARNING, logger="gw2bot"):
            bot._log_disabled_features()
        bot.event_store.close()
        bot.raffle_store.close()

        warnings = [
            record.getMessage()
            for record in caplog.records
            if record.levelno == logging.WARNING
        ]
        assert len(warnings) == 2
        assert (
            "Guild Wars 2 features are disabled because GW2_API_KEY, "
            "GW2_GUILD_ID are not set" in warnings[0]
        )
        assert "Guild Storage polling" in warnings[0]
        assert (
            "Notification channel delivery is disabled because "
            "DISCORD_NOTIFICATION_CHANNEL_ID is not set" in warnings[1]
        )
        # The raffle contribution channel keeps posting, so the warning must
        # not read as "the bot has gone silent".
        assert "raffle contribution channel is unaffected" in warnings[1]

    def test_no_warning_is_logged_when_everything_is_configured(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        bot = Gw2Bot(
            self._config(
                tmp_path,
                DISCORD_NOTIFICATION_CHANNEL_ID="9012",
                GW2_API_KEY="gw2-key",
                GW2_GUILD_ID="guild-id",
            )
        )

        with caplog.at_level(logging.WARNING, logger="gw2bot"):
            bot._log_disabled_features()
        bot.event_store.close()
        bot.raffle_store.close()

        assert caplog.records == []
        assert bot.gw2_api_enabled

    async def test_disabled_command_reply_names_the_unset_variables(self) -> None:
        bot = SimpleNamespace(
            _config=SimpleNamespace(
                missing_gw2_api_variables=("GW2_API_KEY", "GW2_GUILD_ID"),
            )
        )
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=1234),
            response=SimpleNamespace(
                is_done=MagicMock(return_value=False),
                send_message=AsyncMock(),
            ),
        )

        rejected = await Gw2Bot.reject_without_gw2_api(
            cast(Gw2Bot, bot),
            cast(discord.Interaction, interaction),
            "raffle ticket addition",
        )

        assert rejected
        interaction.response.send_message.assert_awaited_once_with(
            "This command is disabled. Set the GW2_API_KEY, GW2_GUILD_ID "
            "environment variables for the bot and restart it to enable "
            "this command.",
            ephemeral=True,
        )

    async def test_a_command_stays_rejected_when_the_reply_cannot_be_sent(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        bot = SimpleNamespace(
            _config=SimpleNamespace(missing_gw2_api_variables=("GW2_API_KEY",)),
        )
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=1234),
            response=SimpleNamespace(
                is_done=MagicMock(return_value=False),
                send_message=AsyncMock(side_effect=forbidden_error(50013)),
            ),
        )

        with caplog.at_level(logging.DEBUG, logger="gw2bot"):
            rejected = await Gw2Bot.reject_without_gw2_api(
                cast(Gw2Bot, bot),
                cast(discord.Interaction, interaction),
                "raffle ticket addition",
            )

        assert rejected
        assert (
            "Reported disabled command configuration; "
            "action=raffle ticket addition delivered=False" in caplog.text
        )

    async def test_configured_commands_are_not_rejected(self) -> None:
        bot = SimpleNamespace(
            _config=SimpleNamespace(missing_gw2_api_variables=()),
        )
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=1234),
            response=SimpleNamespace(
                is_done=MagicMock(return_value=False),
                send_message=AsyncMock(),
            ),
        )

        rejected = await Gw2Bot.reject_without_gw2_api(
            cast(Gw2Bot, bot),
            cast(discord.Interaction, interaction),
            "raffle ticket addition",
        )

        assert not rejected
        interaction.response.send_message.assert_not_awaited()

    async def test_diag_is_ignored_when_no_notification_channel_is_set(self) -> None:
        bot = SimpleNamespace(
            _config=SimpleNamespace(discord_notification_channel_id=None),
            _send_automated_message_diagnostics=AsyncMock(),
        )
        message = SimpleNamespace(
            author=SimpleNamespace(bot=False),
            channel=SimpleNamespace(id=9012),
            content="diag",
        )

        await Gw2Bot.on_message(cast(Gw2Bot, bot), message)  # type: ignore[arg-type]

        bot._send_automated_message_diagnostics.assert_not_awaited()


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
                gw2_api_enabled=True,
                notifications_enabled=True,
            ),
            _startup_status=lambda: Gw2Bot._startup_status(cast(Gw2Bot, bot)),
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

    def test_startup_status_omits_the_disabled_schedules(self) -> None:
        bot = SimpleNamespace(
            _config=SimpleNamespace(
                poll_interval_seconds=300,
                guild_log_poll_interval_seconds=60,
                gw2_api_enabled=False,
                notifications_enabled=False,
            ),
        )

        assert Gw2Bot._startup_status(cast(Gw2Bot, bot)) == (
            "Raffle contribution reporting every 6 hours UTC."
        )

    def test_startup_status_keeps_gw2_polling_without_a_channel(self) -> None:
        bot = SimpleNamespace(
            _config=SimpleNamespace(
                poll_interval_seconds=300,
                guild_log_poll_interval_seconds=60,
                gw2_api_enabled=True,
                notifications_enabled=False,
            ),
        )

        assert Gw2Bot._startup_status(cast(Gw2Bot, bot)) == (
            "Storage polling every 300 seconds; guild log polling every 60 "
            "seconds; raffle contribution reporting every 6 hours UTC."
        )
