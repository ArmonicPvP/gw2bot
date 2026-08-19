import logging
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from cryptography.fernet import Fernet

from factories import (
    GW2_GUILD_ID,
    OTHER_GW2_GUILD_ID,
    config_from_env,
    default_config,
    forbidden_error,
)
from gw2bot.bot import Gw2Bot
from gw2bot.config import Config
from gw2bot.logging_setup import SecretRegistry
from gw2bot.main import main as run_main
from gw2bot.raffle import TrialForumPost
from gw2bot.settings.definitions import definition_for
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
    @staticmethod
    def _environment(tmp_path: Path, **overrides: str) -> dict[str, str]:
        values = {
            "DISCORD_TOKEN": "discord-secret",
            "DISCORD_COMMAND_GUILD_ID": "5678",
            "RAFFLE_DB_PATH": str(tmp_path / "gw2bot.db"),
            "DEBUG": "true",
        }
        values.update(overrides)
        return values

    @patch("gw2bot.main.Gw2Bot")
    @patch("gw2bot.main.configure_logging")
    def test_registers_all_configured_credentials_with_console_redaction(
        self,
        configure: MagicMock,
        bot_class: MagicMock,
        tmp_path: Path,
    ) -> None:
        environment = self._environment(
            tmp_path,
            GW2_API_KEY="gw2-secret",
            GW2_GUILD_ID=GW2_GUILD_ID,
            DISCORD_OAUTH_CLIENT_SECRET="oauth-secret",
            WEB_SESSION_SECRET="s" * 32,
        )

        with patch.dict("os.environ", environment, clear=True):
            run_main()

        debug, secrets = configure.call_args.args
        assert debug
        # The registry is shared with the handler, so a secret set later is
        # redacted too; every credential this configuration carries has to be
        # in it before anything can log one.
        assert set(secrets.current()) == {
            "gw2-secret",
            "discord-secret",
            "oauth-secret",
            "s" * 32,
        }
        config = bot_class.call_args.args[0]
        assert config.gw2_api_key == "gw2-secret"
        assert config.gw2_guild_id == GW2_GUILD_ID
        assert bot_class.call_args.kwargs["secrets"] is secrets
        bot_class.return_value.run.assert_called_once_with(
            "discord-secret",
            log_handler=None,
        )

    @patch("gw2bot.main.Gw2Bot")
    @patch("gw2bot.main.configure_logging")
    def test_registers_no_placeholders_for_unset_web_secrets(
        self,
        configure: MagicMock,
        bot_class: MagicMock,
        tmp_path: Path,
    ) -> None:
        environment = self._environment(
            tmp_path,
            DEBUG="false",
            GW2_API_KEY="gw2-secret",
        )

        with patch.dict("os.environ", environment, clear=True):
            run_main()

        debug, secrets = configure.call_args.args
        assert not debug
        # An unset secret has nothing to redact, and registering a blank would
        # make the formatter replace every empty string it ever formats.
        assert set(secrets.current()) == {"gw2-secret", "discord-secret"}

    @patch("gw2bot.main.Gw2Bot")
    @patch("gw2bot.main.configure_logging")
    def test_registers_the_settings_encryption_key(
        self,
        configure: MagicMock,
        bot_class: MagicMock,
        tmp_path: Path,
    ) -> None:
        # Every credential variable has to reach the redacting formatter, and
        # this one is armed before the store is even opened.
        key = Fernet.generate_key().decode("ascii")
        environment = self._environment(
            tmp_path,
            SETTINGS_ENCRYPTION_KEY=key,
        )

        with patch.dict("os.environ", environment, clear=True):
            run_main()
        bot_class.call_args.kwargs["settings_store"].close()

        _, secrets = configure.call_args.args
        assert key in secrets.current()

    @patch("gw2bot.main.Gw2Bot")
    @patch("gw2bot.main.configure_logging")
    def test_imports_the_legacy_environment_once(
        self,
        configure: MagicMock,
        bot_class: MagicMock,
        tmp_path: Path,
    ) -> None:
        environment = self._environment(
            tmp_path,
            GW2_API_KEY="gw2-secret",
            GW2_GUILD_ID=GW2_GUILD_ID,
            TZ="America/New_York",
        )

        with patch.dict("os.environ", environment, clear=True):
            run_main()
        first = bot_class.call_args.args[0]
        store = bot_class.call_args.kwargs["settings_store"]
        store.close()

        assert first.event_timezone == "America/New_York"
        assert first.gw2_api_key == "gw2-secret"

        # The database is authoritative from here on: a value cleared with
        # /settings must not come back because the variable is still in the
        # environment.
        with patch.dict("os.environ", environment, clear=True):
            run_main()
        reopened = bot_class.call_args.kwargs["settings_store"]
        definition = definition_for("gw2_api_key")
        reopened.unset(definition)
        reopened.close()

        with patch.dict("os.environ", environment, clear=True):
            run_main()
        third = bot_class.call_args.args[0]
        bot_class.call_args.kwargs["settings_store"].close()

        assert third.gw2_api_key is None
        assert third.event_timezone == "America/New_York"

    @patch("gw2bot.main.Gw2Bot")
    @patch("gw2bot.main.configure_logging")
    def test_encrypts_the_secrets_it_imports(
        self,
        configure: MagicMock,
        bot_class: MagicMock,
        tmp_path: Path,
    ) -> None:
        database = tmp_path / "gw2bot.db"
        environment = self._environment(
            tmp_path,
            GW2_API_KEY="gw2-secret",
        )

        with patch.dict("os.environ", environment, clear=True):
            run_main()
        bot_class.call_args.kwargs["settings_store"].close()

        # The whole point of encrypting them: a copied database file is not a
        # copied API key.
        assert b"gw2-secret" not in database.read_bytes()

    def test_reports_a_missing_bootstrap_variable_and_stops(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            with patch("gw2bot.config.load_dotenv"):
                with pytest.raises(SystemExit) as exit_info:
                    run_main()

        assert "DISCORD_TOKEN" in str(exit_info.value)


class TestCommandSync:
    def setup_method(self) -> None:
        self.config = config_from_env(
            {
                "DISCORD_TOKEN": "discord-token",
                "DISCORD_COMMAND_GUILD_ID": "5678",
                "DISCORD_NOTIFICATION_CHANNEL_ID": "9012",
                "GW2_API_KEY": "gw2-key",
                "GW2_GUILD_ID": GW2_GUILD_ID,
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
            config = config_from_env(
                {
                    "DISCORD_TOKEN": "discord-token",
                    "DISCORD_COMMAND_GUILD_ID": "5678",
                    "DISCORD_NOTIFICATION_CHANNEL_ID": "9012",
                    "GW2_API_KEY": "gw2-key",
                    "GW2_GUILD_ID": GW2_GUILD_ID,
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
            config = config_from_env(
                {
                    "DISCORD_TOKEN": "discord-token",
                    "DISCORD_COMMAND_GUILD_ID": "5678",
                    "DISCORD_NOTIFICATION_CHANNEL_ID": "9012",
                    "GW2_API_KEY": "gw2-key",
                    "GW2_GUILD_ID": GW2_GUILD_ID,
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
            "GW2_GUILD_ID": GW2_GUILD_ID,
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
        return config_from_env(values)

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
        return config_from_env(
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
                GW2_GUILD_ID=GW2_GUILD_ID,
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
                GW2_GUILD_ID=GW2_GUILD_ID,
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
            and record.name.startswith("gw2bot")
        ]
        assert len(warnings) == 2
        assert (
            "Guild Wars 2 features are disabled because /settings "
            "gw2_api_key, /settings gw2_guild_id are not set" in warnings[0]
        )
        assert "Guild Storage polling" in warnings[0]
        assert (
            "Notification channel delivery is disabled because /settings "
            "discord_notification_channel_id is not set" in warnings[1]
        )
        # The raffle contribution channel keeps posting, so the warning must
        # not read as "the bot has gone silent".
        assert "raffle contribution channel is unaffected" in warnings[1]

    def test_an_enabled_calendar_missing_its_settings_warns(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        bot = Gw2Bot(
            self._config(
                tmp_path,
                DISCORD_NOTIFICATION_CHANNEL_ID="9012",
                GW2_API_KEY="gw2-key",
                GW2_GUILD_ID=GW2_GUILD_ID,
                WEB_ENABLED="true",
                WEB_BASE_URL="http://localhost:8080",
            )
        )

        with caplog.at_level(logging.WARNING, logger="gw2bot"):
            bot._log_disabled_features()
        bot.event_store.close()
        bot.raffle_store.close()

        warnings = [
            record.getMessage()
            for record in caplog.records
            if record.levelno == logging.WARNING
            and record.name.startswith("gw2bot")
        ]
        assert len(warnings) == 1
        # WEB_ENABLED asked for the calendar, so the operator has to be told
        # which settings are keeping it off rather than left watching a port
        # that never answers.
        assert (
            "The web calendar is disabled because /settings "
            "discord_oauth_client_id, /settings discord_oauth_client_secret, "
            "/settings web_session_secret are not set" in warnings[0]
        )
        assert "WEB_ENABLED is true" in warnings[0]

    def test_a_calendar_nobody_asked_for_stays_quiet(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        bot = Gw2Bot(
            self._config(
                tmp_path,
                DISCORD_NOTIFICATION_CHANNEL_ID="9012",
                GW2_API_KEY="gw2-key",
                GW2_GUILD_ID=GW2_GUILD_ID,
            )
        )

        with caplog.at_level(logging.WARNING, logger="gw2bot"):
            bot._log_disabled_features()
        bot.event_store.close()
        bot.raffle_store.close()

        # WEB_ENABLED is false, so the four settings being unset is the
        # normal state of an install that never wanted the calendar.
        assert caplog.records == []

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
                GW2_GUILD_ID=GW2_GUILD_ID,
            )
        )

        with caplog.at_level(logging.WARNING, logger="gw2bot"):
            bot._log_disabled_features()
        bot.event_store.close()
        bot.raffle_store.close()

        assert caplog.records == []
        assert bot.gw2_api_enabled

    async def test_disabled_command_reply_names_the_unset_settings(self) -> None:
        bot = SimpleNamespace(
            _config=SimpleNamespace(
                missing_gw2_api_settings=("gw2_api_key", "gw2_guild_id"),
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
            "This command is disabled. Run the /settings gw2_api_key, "
            "/settings gw2_guild_id commands to enable it.",
            ephemeral=True,
        )

    async def test_a_command_stays_rejected_when_the_reply_cannot_be_sent(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        bot = SimpleNamespace(
            _config=SimpleNamespace(missing_gw2_api_settings=("gw2_api_key",)),
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
            _config=SimpleNamespace(missing_gw2_api_settings=()),
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
            _announce_legacy_variables=AsyncMock(),
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


class TestSettingsHotApply:
    """A settings change has to reach the parts that captured a value.

    Most of the bot reads bot._config when it needs something, so swapping the
    frozen Config covers them. These are the ones it does not cover: caches,
    long-lived clients, and the poll tasks whose very existence depends on
    whether a feature is configured.
    """

    ALWAYS_RUNNING = {"gw2-raffle-contribution-poller", "gw2-event-scheduler"}
    GW2_POLLERS = {"gw2-guild-storage-poller", "gw2-guild-log-poller"}

    def _config(self, tmp_path: Path, **values: str) -> Config:
        return config_from_env(
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

    async def _started(self, config: Config, **stored: str) -> Gw2Bot:
        """Start a bot whose store already holds the values it was built with.

        apply_settings_change recomposes from the store, which is what startup
        does too, so a value only present in the Config would vanish the first
        time anything changed.
        """
        bot = Gw2Bot(config)
        for name, value in stored.items():
            bot.settings_store.set_raw(definition_for(name), value)
        with self._quiet_bot_patches():
            await bot.setup_hook()
        return bot

    async def _close(self, bot: Gw2Bot) -> None:
        with patch.object(discord.Client, "close", AsyncMock()):
            await bot.close()

    @staticmethod
    def _names(bot: Gw2Bot) -> set[str]:
        return {task.get_name() for task in bot._poll_tasks}

    @patch("gw2bot.bot.GuildMemberCache")
    async def test_setting_the_credentials_starts_the_gw2_pollers(
        self,
        member_cache: MagicMock,
        tmp_path: Path,
    ) -> None:
        # Enabling a feature used to mean editing .env and restarting the
        # container; the whole point is that it no longer does.
        member_cache.return_value.close = AsyncMock()
        bot = await self._started(self._config(tmp_path))
        assert self._names(bot) == self.ALWAYS_RUNNING
        assert bot._api is None

        bot.settings_store.set_raw(definition_for("gw2_api_key"), "gw2-key")
        bot.settings_store.set_raw(definition_for("gw2_guild_id"), GW2_GUILD_ID)
        with self._quiet_bot_patches():
            restarted = await bot.apply_settings_change(
                {"gw2_api_key", "gw2_guild_id"}
            )

        assert self._names(bot) == self.ALWAYS_RUNNING | self.GW2_POLLERS
        assert bot._api is not None
        assert bot._config.gw2_api_key == "gw2-key"
        assert "the Guild Wars 2 API client" in restarted

        await self._close(bot)

    @patch("gw2bot.bot.GuildMemberCache")
    async def test_clearing_the_credentials_stops_the_gw2_pollers(
        self,
        member_cache: MagicMock,
        tmp_path: Path,
    ) -> None:
        member_cache.return_value.close = AsyncMock()
        bot = await self._started(
            self._config(tmp_path, GW2_API_KEY="gw2-key", GW2_GUILD_ID=GW2_GUILD_ID),
            gw2_api_key="gw2-key",
            gw2_guild_id=GW2_GUILD_ID,
        )
        assert self._names(bot) == self.ALWAYS_RUNNING | self.GW2_POLLERS

        bot.settings_store.unset(definition_for("gw2_api_key"))
        with self._quiet_bot_patches():
            await bot.apply_settings_change({"gw2_api_key"})

        assert self._names(bot) == self.ALWAYS_RUNNING
        assert bot._api is None
        assert bot._guild_members is None

        await self._close(bot)

    @patch("gw2bot.bot.GuildMemberCache")
    async def test_a_new_api_key_replaces_the_running_pollers(
        self,
        member_cache: MagicMock,
        tmp_path: Path,
    ) -> None:
        # Both pollers read the guild id once before their loop, so the only
        # way a new one reaches them is a fresh task.
        member_cache.return_value.close = AsyncMock()
        bot = await self._started(
            self._config(tmp_path, GW2_API_KEY="gw2-key", GW2_GUILD_ID=GW2_GUILD_ID),
            gw2_api_key="gw2-key",
            gw2_guild_id=GW2_GUILD_ID,
        )
        before = {
            task.get_name(): task
            for task in bot._poll_tasks
            if task.get_name() in self.GW2_POLLERS
        }

        bot.settings_store.set_raw(definition_for("gw2_api_key"), "new-key")
        with self._quiet_bot_patches():
            await bot.apply_settings_change({"gw2_api_key"})

        after = {
            task.get_name(): task
            for task in bot._poll_tasks
            if task.get_name() in self.GW2_POLLERS
        }
        assert set(after) == self.GW2_POLLERS
        for name, task in before.items():
            assert after[name] is not task

        await self._close(bot)

    @patch("gw2bot.bot.GuildMemberCache")
    async def test_a_new_poll_interval_replaces_its_poller(
        self,
        member_cache: MagicMock,
        tmp_path: Path,
    ) -> None:
        # The loop reads the interval when it next sleeps, so one already
        # sleeping on the old value would not pick up a lower one until the
        # old sleep ran out - hours, for the intervals people actually set.
        member_cache.return_value.close = AsyncMock()
        bot = await self._started(
            self._config(tmp_path, GW2_API_KEY="gw2-key", GW2_GUILD_ID=GW2_GUILD_ID),
            gw2_api_key="gw2-key",
            gw2_guild_id=GW2_GUILD_ID,
        )
        before = {
            task.get_name(): task
            for task in bot._poll_tasks
            if task.get_name() in self.GW2_POLLERS
        }

        bot.settings_store.set_raw(
            definition_for("gw2_poll_interval_seconds"),
            "60",
        )
        with self._quiet_bot_patches():
            await bot.apply_settings_change({"poll_interval_seconds"})

        after = {
            task.get_name(): task
            for task in bot._poll_tasks
            if task.get_name() in self.GW2_POLLERS
        }
        assert after["gw2-guild-storage-poller"] is not before["gw2-guild-storage-poller"]
        # Only the poller whose interval changed is disturbed.
        assert after["gw2-guild-log-poller"] is before["gw2-guild-log-poller"]

        await self._close(bot)

    @patch("gw2bot.bot.GuildMemberCache")
    async def test_an_unrelated_change_leaves_the_pollers_alone(
        self,
        member_cache: MagicMock,
        tmp_path: Path,
    ) -> None:
        member_cache.return_value.close = AsyncMock()
        bot = await self._started(
            self._config(tmp_path, GW2_API_KEY="gw2-key", GW2_GUILD_ID=GW2_GUILD_ID),
            gw2_api_key="gw2-key",
            gw2_guild_id=GW2_GUILD_ID,
        )
        before = {task.get_name(): task for task in bot._poll_tasks}

        bot.settings_store.set_raw(definition_for("timezone"), "America/New_York")
        with self._quiet_bot_patches():
            await bot.apply_settings_change({"event_timezone"})

        assert {task.get_name(): task for task in bot._poll_tasks} == before
        assert str(bot.event_timezone) == "America/New_York"

        await self._close(bot)

    @patch("gw2bot.bot.GuildMemberCache")
    async def test_changing_a_channel_drops_its_cached_handle(
        self,
        member_cache: MagicMock,
        tmp_path: Path,
    ) -> None:
        # Both handles are cached for the life of the process, so without this
        # the bot would keep posting to the channel it was pointed at before.
        member_cache.return_value.close = AsyncMock()
        bot = await self._started(self._config(tmp_path))
        bot._notification_channel = object()
        bot._raffle_contribution_channel = object()
        bot._feast_notification_user = object()

        bot.settings_store.set_raw(
            definition_for("discord_notification_channel_id"),
            "9012",
        )
        with self._quiet_bot_patches():
            await bot.apply_settings_change(
                {
                    "discord_notification_channel_id",
                    "raffle_contribution_channel_id",
                    "discord_feast_notification_user_id",
                }
            )

        assert bot._notification_channel is None
        assert bot._raffle_contribution_channel is None
        assert bot._feast_notification_user is None
        assert bot._config.discord_notification_channel_id == 9012

        await self._close(bot)

    @patch("gw2bot.bot.GuildMemberCache")
    async def test_changing_the_channel_lets_topic_failures_be_reported_again(
        self,
        member_cache: MagicMock,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        member_cache.return_value.close = AsyncMock()
        bot = await self._started(self._config(tmp_path))

        class DiscordFailure(discord.DiscordException):
            status = 403
            code = 50013

        # The old channel already refused a topic edit, so the failure is
        # suppressed until its signature changes.
        bot._notification_channel = SimpleNamespace(
            topic="old",
            edit=AsyncMock(side_effect=DiscordFailure()),
        )
        assert not await bot._try_update_logging_channel_topic("1/500")
        suppressed = bot._last_topic_update_failure
        assert suppressed is not None

        bot.settings_store.set_raw(
            definition_for("discord_notification_channel_id"),
            "9012",
        )
        with self._quiet_bot_patches():
            await bot.apply_settings_change({"discord_notification_channel_id"})

        assert bot._last_topic_update_failure is None

        # A new channel that fails the same way must be reported, not read as
        # the failure the operator was already told about - the only log on
        # record names the channel nobody is using any more.
        bot._notification_channel = SimpleNamespace(
            topic="old",
            edit=AsyncMock(side_effect=DiscordFailure()),
        )
        with caplog.at_level(logging.ERROR, logger="gw2bot"):
            assert not await bot._try_update_logging_channel_topic("1/500")

        assert "channel_id=9012" in caplog.text
        assert bot._last_topic_update_failure == suppressed

        await self._close(bot)

    @patch("gw2bot.bot.GuildMemberCache")
    async def test_changing_the_trial_forum_drops_its_index(
        self,
        member_cache: MagicMock,
        tmp_path: Path,
    ) -> None:
        # The index is refreshed incrementally against a watermark, so rows
        # from the old forum would never be revisited and would keep matching
        # accounts in /check and the overdue report.
        member_cache.return_value.close = AsyncMock()
        bot = await self._started(self._config(tmp_path))
        bot.raffle_store.upsert_trial_forum_posts(
            [TrialForumPost(1, 101, "member.1234", "2026-01-01T00:00:00+00:00")]
        )
        bot.raffle_store.set_trial_forum_watermark(
            datetime(2026, 1, 1, tzinfo=UTC)
        )
        assert bot.raffle_store.get_trial_forum_index()

        bot.settings_store.set_raw(
            definition_for("trial_forum", "channels"),
            "424242",
        )
        with self._quiet_bot_patches():
            restarted = await bot.apply_settings_change(
                {"trial_forum_channel_id"}
            )

        assert bot.raffle_store.get_trial_forum_index() == {}
        assert bot.raffle_store.get_trial_forum_watermark() is None
        assert "the Trial application index" in restarted

        await self._close(bot)

    @patch("gw2bot.bot.GuildMemberCache")
    async def test_an_unrelated_change_keeps_the_trial_index(
        self,
        member_cache: MagicMock,
        tmp_path: Path,
    ) -> None:
        member_cache.return_value.close = AsyncMock()
        bot = await self._started(self._config(tmp_path))
        bot.raffle_store.upsert_trial_forum_posts(
            [TrialForumPost(1, 101, "member.1234", "2026-01-01T00:00:00+00:00")]
        )

        bot.settings_store.set_raw(definition_for("timezone"), "Europe/Berlin")
        with self._quiet_bot_patches():
            await bot.apply_settings_change({"event_timezone"})

        assert bot.raffle_store.get_trial_forum_index()

        await self._close(bot)

    @patch("gw2bot.bot.GuildMemberCache")
    async def test_a_secret_set_at_runtime_is_registered_for_redaction(
        self,
        member_cache: MagicMock,
        tmp_path: Path,
    ) -> None:
        member_cache.return_value.close = AsyncMock()
        bot = await self._started(self._config(tmp_path))
        assert "gw2-secret" not in bot.secrets.current()

        bot.settings_store.set_raw(definition_for("gw2_api_key"), "gw2-secret")
        with self._quiet_bot_patches():
            await bot.apply_settings_change({"gw2_api_key"})

        # Registered before anything can log it, and the registry is the same
        # object the console formatter was built with.
        assert "gw2-secret" in bot.secrets.current()

        await self._close(bot)

    def test_an_empty_registry_it_was_handed_is_the_one_it_keeps(
        self,
        tmp_path: Path,
    ) -> None:
        # configure_logging installs its handler holding the caller's
        # registry, so the bot has to register into that same object. An
        # empty SecretRegistry is falsy, and truthiness-testing it would
        # swap in a second one the formatter never sees.
        registry = SecretRegistry()
        assert not registry

        bot = Gw2Bot(
            self._config(tmp_path, GW2_API_KEY="gw2-secret"),
            secrets=registry,
        )

        assert bot.secrets is registry
        assert bot._poll_status._secrets is registry
        # The caller built the registry for the formatter and need not have
        # known which Config fields hold credentials, so the bot seeds it
        # whether it made the registry or was handed one.
        assert "gw2-secret" in registry.current()
        assert "discord-token" in registry.current()

        registry.add("later-secret")
        assert "later-secret" in bot.secrets.current()

        bot.event_store.close()
        bot.raffle_store.close()

    @patch("gw2bot.bot.GuildMemberCache")
    async def test_a_new_guild_id_rebinds_the_raffle_database(
        self,
        member_cache: MagicMock,
        tmp_path: Path,
    ) -> None:
        member_cache.return_value.close = AsyncMock()
        bot = await self._started(
            self._config(tmp_path, GW2_API_KEY="gw2-key", GW2_GUILD_ID=GW2_GUILD_ID),
            gw2_api_key="gw2-key",
            gw2_guild_id=GW2_GUILD_ID,
        )

        bot.settings_store.set_raw(
            definition_for("gw2_guild_id"),
            OTHER_GW2_GUILD_ID,
        )
        with self._quiet_bot_patches():
            with pytest.raises(ValueError, match="belongs to a different guild"):
                await bot.apply_settings_change({"gw2_guild_id"})

        await self._close(bot)


class TestWebCalendarReconcile:
    """What /settings is told about the calendar has to match reality."""

    def _config(self, tmp_path: Path, **values: str) -> Config:
        return config_from_env(
            {
                "DISCORD_TOKEN": "discord-token",
                "DISCORD_COMMAND_GUILD_ID": "5678",
                "RAFFLE_DB_PATH": str(tmp_path / "raffle.db"),
                "WEB_ENABLED": "true",
                "WEB_BASE_URL": "http://localhost:8080",
                "DISCORD_OAUTH_CLIENT_ID": "client-id",
                "DISCORD_OAUTH_CLIENT_SECRET": "client-secret",
                "WEB_SESSION_SECRET": "s" * 32,
                **values,
            }
        )

    async def _bot(self, config: Config) -> Gw2Bot:
        bot = Gw2Bot(config)
        bot._session = cast(Any, object())
        return bot

    async def _close(self, bot: Gw2Bot) -> None:
        bot._session = None
        with patch.object(discord.Client, "close", AsyncMock()):
            await bot.close()

    async def test_reports_a_started_calendar(self, tmp_path: Path) -> None:
        bot = await self._bot(self._config(tmp_path))

        with patch("gw2bot.web.server.WebServer") as web_server:
            web_server.return_value.start = AsyncMock()
            web_server.return_value.stop = AsyncMock()
            outcome = await bot._reconcile_web_server()

        assert outcome == "the web calendar"
        assert bot._web_server is not None
        await self._close(bot)

    async def test_reports_a_calendar_that_could_not_start(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # A taken port must not be reported as a successful restart: the
        # calendar is offline, and the reply is the only place anyone finds
        # that out without reading the console.
        bot = await self._bot(self._config(tmp_path))

        with patch("gw2bot.web.server.WebServer") as web_server:
            web_server.return_value.start = AsyncMock(
                side_effect=OSError("address already in use")
            )
            with caplog.at_level(logging.ERROR, logger="gw2bot"):
                outcome = await bot._reconcile_web_server()

        assert outcome is not None
        assert "could not be started" in outcome
        assert "offline" in outcome
        # The bot keeps running: the calendar is an optional extra.
        assert bot._web_server is None
        assert "Could not start the web calendar" in caplog.text
        await self._close(bot)

    async def test_reports_nothing_when_the_calendar_is_off(
        self,
        tmp_path: Path,
    ) -> None:
        bot = await self._bot(self._config(tmp_path, WEB_ENABLED="false"))

        assert await bot._reconcile_web_server() is None
        assert bot._web_server is None
        await self._close(bot)

    async def test_reports_nothing_when_an_unrelated_setting_changed(
        self,
        tmp_path: Path,
    ) -> None:
        bot = await self._bot(self._config(tmp_path))
        with patch("gw2bot.web.server.WebServer") as web_server:
            web_server.return_value.start = AsyncMock()
            web_server.return_value.stop = AsyncMock()
            await bot._reconcile_web_server()

            assert await bot._reconcile_web_server() is None

        await self._close(bot)


class TestLegacyEnvironmentWarnings:
    async def test_the_console_warning_names_every_stale_variable(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        bot = SimpleNamespace()

        with caplog.at_level(logging.WARNING, logger="gw2bot"):
            present = Gw2Bot._log_legacy_variables(
                cast(Gw2Bot, bot),
                {"GW2_API_KEY": "gw2-secret", "TZ": "UTC"},
            )

        # TZ is imported like any other legacy variable but never named
        # here: the warning tells the operator to delete what it lists, and
        # the container uses TZ for its own clock and log timestamps.
        assert present == ("GW2_API_KEY",)
        assert "no longer configure the bot" in caplog.text
        assert "GW2_API_KEY" in caplog.text
        assert "TZ" not in caplog.text
        # The variables are named; their values never are.
        assert "gw2-secret" not in caplog.text

    async def test_tz_alone_earns_no_warning(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        bot = SimpleNamespace()

        with caplog.at_level(logging.WARNING, logger="gw2bot"):
            present = Gw2Bot._log_legacy_variables(
                cast(Gw2Bot, bot),
                {"TZ": "America/New_York"},
            )

        assert present == ()
        assert caplog.records == []

    async def test_nothing_is_warned_about_when_the_environment_is_clean(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        bot = SimpleNamespace()

        with caplog.at_level(logging.WARNING, logger="gw2bot"):
            present = Gw2Bot._log_legacy_variables(
                cast(Gw2Bot, bot),
                {"DISCORD_TOKEN": "token", "RAFFLE_DB_PATH": "/app/data/x.db"},
            )

        assert present == ()
        assert caplog.records == []

    async def test_the_channel_notice_is_posted_once_per_startup(self) -> None:
        bot = SimpleNamespace(
            _legacy_variables_announced=False,
            _config=default_config(discord_notification_channel_id=9012),
            _try_send_notification=AsyncMock(return_value=True),
        )

        with patch.dict("os.environ", {"GW2_API_KEY": "gw2-secret"}, clear=True):
            await Gw2Bot._announce_legacy_variables(cast(Gw2Bot, bot))
            await Gw2Bot._announce_legacy_variables(cast(Gw2Bot, bot))

        # It is a migration notice, not a recurring alert; repeating it would
        # bury the messages the channel exists for.
        bot._try_send_notification.assert_awaited_once()
        message = bot._try_send_notification.await_args.args[0]
        assert "GW2_API_KEY" in message
        assert "/settings gw2_api_key" in message
        assert "gw2-secret" not in message

    async def test_a_clean_environment_posts_nothing(self) -> None:
        bot = SimpleNamespace(
            _legacy_variables_announced=False,
            _config=default_config(discord_notification_channel_id=9012),
            _try_send_notification=AsyncMock(return_value=True),
        )

        with patch.dict("os.environ", {"DISCORD_TOKEN": "token"}, clear=True):
            await Gw2Bot._announce_legacy_variables(cast(Gw2Bot, bot))

        bot._try_send_notification.assert_not_awaited()
