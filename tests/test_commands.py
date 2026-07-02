import tempfile
import logging
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, call, patch

import aiohttp
import discord
import pytest

from gw2bot.config import Config
from factories import forbidden_error
from gw2bot.main import (
    GUILD_MEMBER_COUNT_TOPIC_UPDATE_SECONDS,
    Gw2Bot,
    count_active_guild_members,
    format_automated_message_diagnostics,
    format_guild_member_count_topic,
    format_raffle_milestone_preview,
    main as run_main,
)
from gw2bot.raffle import RaffleContribution, RaffleTotal


class GuildMemberCountTopicBot(SimpleNamespace):
    async def _try_update_logging_channel_topic(self, topic: str) -> bool:
        return await Gw2Bot._try_update_logging_channel_topic(
            cast(Gw2Bot, self),
            topic,
        )

    async def _get_notification_channel(self) -> Any:
        return await Gw2Bot._get_notification_channel(cast(Gw2Bot, self))


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

        with caplog.at_level(logging.ERROR, logger="gw2bot.main"):
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
    @patch("gw2bot.main.RaffleStore")
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


class TestAutomatedMessageDiagnostics:
    def test_formats_all_non_command_automated_message_previews(self) -> None:
        messages = format_automated_message_diagnostics(
            [RaffleContribution("Free Only.1234", 0, 1)],
            purchased_tickets=125,
        )
        output = "\n".join(messages)

        assert (
            "DiagnosticUser.1234 deposited 3 gold and purchased 3 raffle tickets"
            in output
        )
        assert "DiagnosticUser.1234 has joined the guild." in output
        assert "DiagnosticUser.1234 has left the guild." in output
        assert (
            "Officer.5678 invited DiagnosticUser.1234 to the guild." in output
        )
        assert (
            "Officer.5678 changed DiagnosticUser.1234's guild rank "
            "from Trial to Sunborne." in output
        )
        assert (
            "150 total tickets have been purchased for this raffle. "
            "Tier 3 rewards have been reached!"
        ) in output
        assert "Guild Storage is low on **Diagnostic Feast**: 5 left" in output
        assert "Trial members past the 14-day mark" in output
        assert "Trial members past the 7-day warning mark (to be kicked)" in output
        assert (
            "The guild member count has not been retrieved yet, so the "
            "channel description is not set."
        ) in output
        assert "polling failed" not in output
        assert "polling recovered" not in output

    def test_includes_current_guild_member_count_description(self) -> None:
        messages = format_automated_message_diagnostics(
            [],
            purchased_tickets=0,
            member_count=493,
            pending_invite_count=5,
        )
        output = "\n".join(messages)

        assert (
            "**Guild member count channel description (current)**\n"
            "493/500 (5 pending)"
        ) in output

    def test_highest_tier_preview_notes_that_it_is_already_reached(self) -> None:
        assert format_raffle_milestone_preview(200) == (
            "200 total tickets have been purchased for this raffle. "
            "Tier 4 rewards have been reached! "
            "This raffle is already at the highest configured tier."
        )

    async def test_diag_in_notification_channel_sends_read_only_previews(
        self,
    ) -> None:
        channel = SimpleNamespace(id=9012, send=AsyncMock())
        bot = SimpleNamespace(
            _config=SimpleNamespace(discord_notification_channel_id=9012),
            _send_automated_message_diagnostics=AsyncMock(),
        )
        message = SimpleNamespace(
            author=SimpleNamespace(bot=False),
            channel=channel,
            content=" DiAg ",
        )

        await Gw2Bot.on_message(cast(Gw2Bot, bot), message)  # type: ignore[arg-type]

        bot._send_automated_message_diagnostics.assert_awaited_once_with(channel)

    async def test_diag_debug_logging_does_not_include_message_details(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        author_secret = "author-secret"
        channel_secret = "channel-secret"
        channel = SimpleNamespace(
            id=9012,
            name=channel_secret,
            send=AsyncMock(),
        )
        bot = SimpleNamespace(
            _config=SimpleNamespace(discord_notification_channel_id=9012),
            _send_automated_message_diagnostics=AsyncMock(),
        )
        message = SimpleNamespace(
            author=SimpleNamespace(bot=False, name=author_secret),
            channel=channel,
            content="diag",
        )

        with caplog.at_level(logging.DEBUG, logger="gw2bot.main"):
            await Gw2Bot.on_message(cast(Gw2Bot, bot), message)  # type: ignore[arg-type]

        assert author_secret not in caplog.text
        assert channel_secret not in caplog.text
        assert (
            "Discord message received; author_is_bot=False "
            "notification_channel=True characters=4 diag_candidate=True"
            in caplog.text
        )
        assert "Starting automated message diagnostics request" in caplog.text
        assert "Automated message diagnostics request completed" in caplog.text

    async def test_diag_request_failure_logs_only_error_type(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        secret = "diag-request-secret"
        bot = SimpleNamespace(
            _config=SimpleNamespace(discord_notification_channel_id=9012),
            _send_automated_message_diagnostics=AsyncMock(
                side_effect=RuntimeError(secret)
            ),
        )
        message = SimpleNamespace(
            author=SimpleNamespace(bot=False),
            channel=SimpleNamespace(id=9012),
            content="diag",
        )

        with caplog.at_level(logging.DEBUG, logger="gw2bot.main"):
            await Gw2Bot.on_message(cast(Gw2Bot, bot), message)  # type: ignore[arg-type]

        assert secret not in caplog.text
        assert (
            "Automated message diagnostics request failed; error_type=RuntimeError"
            in caplog.text
        )
        assert "Automated message diagnostics request completed" not in caplog.text

    @pytest.mark.parametrize(
        ("author_is_bot", "channel_id", "content"),
        (
            (True, 9012, "diag"),
            (False, 3456, "diag"),
            (False, 9012, "diagnostic"),
        ),
    )
    async def test_ignores_bot_wrong_channel_and_non_exact_diag_messages(
        self,
        author_is_bot: bool,
        channel_id: int,
        content: str,
    ) -> None:
        bot = SimpleNamespace(
            _config=SimpleNamespace(discord_notification_channel_id=9012),
            _send_automated_message_diagnostics=AsyncMock(),
        )
        message = SimpleNamespace(
            author=SimpleNamespace(bot=author_is_bot),
            channel=SimpleNamespace(id=channel_id),
            content=content,
        )

        await Gw2Bot.on_message(cast(Gw2Bot, bot), message)  # type: ignore[arg-type]

        bot._send_automated_message_diagnostics.assert_not_awaited()

    async def test_preview_reads_current_interval_without_changing_schedule(
        self,
    ) -> None:
        now = datetime(2026, 6, 12, 14, 30, tzinfo=UTC)
        channel = SimpleNamespace(send=AsyncMock())
        bot = SimpleNamespace(
            get_raffle_contributions=MagicMock(
                return_value=[RaffleContribution("Free Only.1234", 0, 1)]
            ),
            get_raffle_totals=MagicMock(
                return_value=[
                    RaffleTotal(
                        username="Buyer.1234",
                        coins_deposited=750_000,
                        raffle_tickets=76,
                        gold_raffle_tickets=75,
                        manual_raffle_tickets=1,
                    )
                ]
            ),
            _last_guild_member_count=None,
            _last_pending_guild_invite_count=None,
        )

        await Gw2Bot._send_automated_message_diagnostics(
            cast(Gw2Bot, bot),
            channel,
            now,
        )

        bot.get_raffle_contributions.assert_called_once_with(
            datetime(2026, 6, 12, 12, tzinfo=UTC),
            now,
        )
        bot.get_raffle_totals.assert_called_once_with()
        output = "\n".join(
            call_.args[0]
            for call_ in channel.send.await_args_list
            if call_.args
        )
        report_embed = next(
            call_.kwargs["embed"]
            for call_ in channel.send.await_args_list
            if "embed" in call_.kwargs
        )
        assert (
            report_embed.description
            == "**Free Only.1234**\nPurchased: 0\nFree: 1\nTotal: 1"
        )
        assert (
            "100 total tickets have been purchased for this raffle. "
            "Tier 2 rewards have been reached!"
        ) in output

    async def test_preview_logging_does_not_include_contributor_content(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        secret = "contributor-secret"
        bot = SimpleNamespace(
            get_raffle_contributions=MagicMock(
                return_value=[RaffleContribution(secret, 1, 0)]
            ),
            get_raffle_totals=MagicMock(return_value=[]),
            _last_guild_member_count=None,
            _last_pending_guild_invite_count=None,
        )

        with caplog.at_level(logging.DEBUG, logger="gw2bot.main"):
            await Gw2Bot._send_automated_message_diagnostics(
                cast(Gw2Bot, bot),
                SimpleNamespace(send=AsyncMock()),
                datetime(2026, 6, 12, 14, 30, tzinfo=UTC),
            )

        assert secret not in caplog.text
        assert (
            "Prepared automated message diagnostics; messages=11 contributors=1"
            in caplog.text
        )
        assert caplog.text.count("Attempting automated diagnostic delivery") == 12
        assert caplog.text.count("Automated diagnostic delivery succeeded") == 12
        assert (
            "Automated message diagnostics completed; attempted=12 delivered=12 "
            "failed=0"
            in caplog.text
        )

    async def test_preview_failure_is_logged_and_remaining_previews_continue(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        secret = "diagnostic-failure-secret"
        channel = SimpleNamespace(
            send=AsyncMock(
                side_effect=[
                    None,
                    RuntimeError(secret),
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                ]
            )
        )
        bot = SimpleNamespace(
            get_raffle_contributions=MagicMock(
                return_value=[RaffleContribution("Buyer.1234", 1, 0)]
            ),
            get_raffle_totals=MagicMock(return_value=[]),
            _last_guild_member_count=None,
            _last_pending_guild_invite_count=None,
        )

        with caplog.at_level(logging.DEBUG, logger="gw2bot.main"):
            await Gw2Bot._send_automated_message_diagnostics(
                cast(Gw2Bot, bot),
                channel,
                datetime(2026, 6, 12, 14, 30, tzinfo=UTC),
            )

        assert channel.send.await_count == 12
        assert secret not in caplog.text
        assert (
            "Automated diagnostic delivery failed; kind=contribution-report "
            "error_type=RuntimeError"
            in caplog.text
        )
        assert (
            "Automated message diagnostics completed; attempted=12 delivered=11 "
            "failed=1"
            in caplog.text
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

        with caplog.at_level(logging.INFO, logger="gw2bot.main"):
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


class TestGuildMemberCountTopic:
    def test_formats_guild_member_count_topic(self) -> None:
        assert format_guild_member_count_topic(493, 5) == "493/500 (5 pending)"

    def test_counts_invited_guild_records_as_pending(self) -> None:
        assert count_active_guild_members(
            [
                {"name": "One.1234", "rank": "Member"},
                {"name": "Two.5678", "rank": " invited "},
                {"name": "Three.9012", "rank": "Invited"},
            ]
        ) == (1, 2)

    async def test_updates_logging_channel_description_with_member_count(self) -> None:
        updated_channel = SimpleNamespace(topic="2/500 (1 pending)")
        channel = SimpleNamespace(
            id=9012,
            guild=SimpleNamespace(id=5678),
            topic="old",
            edit=AsyncMock(return_value=updated_channel),
        )
        api = SimpleNamespace(
            get_guild_members=AsyncMock(
                return_value=[
                    {"name": "One.1234", "rank": "Member"},
                    {"name": "Two.5678", "rank": "Officer"},
                    {"name": "Pending.9012", "rank": "invited"},
                ]
            )
        )
        bot = GuildMemberCountTopicBot(
            _api=api,
            _config=SimpleNamespace(
                gw2_guild_id="guild-id",
                discord_notification_channel_id=9012,
                discord_command_guild_id=5678,
            ),
            _notification_channel=channel,
            _last_guild_member_count=None,
            _last_pending_guild_invite_count=None,
            _last_topic_update_failure=None,
        )

        updated = await Gw2Bot._update_guild_member_count_topic(
            cast(Gw2Bot, bot)
        )

        assert updated
        assert bot._last_guild_member_count == 2
        assert bot._last_pending_guild_invite_count == 1
        api.get_guild_members.assert_awaited_once_with("guild-id")
        channel.edit.assert_awaited_once_with(
            topic="2/500 (1 pending)",
            reason="Update GW2 guild member count",
        )
        assert bot._notification_channel is updated_channel

    async def test_skips_logging_channel_update_when_description_is_current(
        self,
    ) -> None:
        channel = SimpleNamespace(
            id=9012,
            guild=SimpleNamespace(id=5678),
            topic="3/500 (1 pending)",
            edit=AsyncMock(),
        )
        api = SimpleNamespace(
            get_guild_members=AsyncMock(
                return_value=[
                    {"name": "One.1234", "rank": "Member"},
                    {"name": "Two.5678", "rank": "Member"},
                    {"name": "Three.9012", "rank": "Officer"},
                    {"name": "Pending.1234", "rank": "invited"},
                ]
            )
        )
        bot = GuildMemberCountTopicBot(
            _api=api,
            _config=SimpleNamespace(
                gw2_guild_id="guild-id",
                discord_notification_channel_id=9012,
                discord_command_guild_id=5678,
            ),
            _notification_channel=channel,
            _last_guild_member_count=None,
            _last_pending_guild_invite_count=None,
            _last_topic_update_failure=None,
        )

        updated = await Gw2Bot._update_guild_member_count_topic(
            cast(Gw2Bot, bot)
        )

        assert updated
        assert bot._last_guild_member_count == 3
        assert bot._last_pending_guild_invite_count == 1
        channel.edit.assert_not_awaited()

    async def test_channel_update_failure_logging_omits_raw_exception_body(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        secret = "raw-topic-update-secret"

        class DiscordFailure(discord.DiscordException):
            status = 403
            code = 50013

            def __str__(self) -> str:
                return secret

        channel = SimpleNamespace(
            id=9012,
            guild=SimpleNamespace(id=5678),
            topic="old",
            edit=AsyncMock(side_effect=DiscordFailure()),
        )
        api = SimpleNamespace(
            get_guild_members=AsyncMock(
                return_value=[
                    {"name": "One.1234", "rank": "Member"},
                    {"name": "Pending.1234", "rank": "invited"},
                ]
            )
        )
        bot = GuildMemberCountTopicBot(
            _api=api,
            _config=SimpleNamespace(
                gw2_guild_id="guild-id",
                discord_notification_channel_id=9012,
                discord_command_guild_id=5678,
            ),
            _notification_channel=channel,
            _last_guild_member_count=None,
            _last_pending_guild_invite_count=None,
            _last_topic_update_failure=None,
        )

        with caplog.at_level(logging.ERROR, logger="gw2bot.main"):
            updated = await Gw2Bot._update_guild_member_count_topic(
                cast(Gw2Bot, bot)
            )

        assert not updated
        assert bot._last_guild_member_count == 1
        assert bot._last_pending_guild_invite_count == 1
        assert secret not in caplog.text
        assert "type=DiscordFailure status=403 code=50013" in caplog.text

    async def test_repeated_topic_update_failures_log_once(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        class DiscordFailure(discord.DiscordException):
            status = 403
            code = 50013

        channel = SimpleNamespace(
            id=9012,
            guild=SimpleNamespace(id=5678),
            topic="old",
            edit=AsyncMock(side_effect=DiscordFailure()),
        )
        bot = GuildMemberCountTopicBot(
            _config=SimpleNamespace(
                discord_notification_channel_id=9012,
                discord_command_guild_id=5678,
            ),
            _notification_channel=channel,
            _last_topic_update_failure=None,
        )

        with caplog.at_level(logging.ERROR, logger="gw2bot.main"):
            assert not await Gw2Bot._try_update_logging_channel_topic(
                cast(Gw2Bot, bot), "1/500 (0 pending)"
            )
            assert not await Gw2Bot._try_update_logging_channel_topic(
                cast(Gw2Bot, bot), "1/500 (0 pending)"
            )

        assert channel.edit.await_count == 2
        assert (
            caplog.text.count("Could not update logging channel description") == 1
        )

    async def test_topic_update_recovery_is_logged_after_failure(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        class DiscordFailure(discord.DiscordException):
            status = 403
            code = 50013

        updated_channel = SimpleNamespace(topic="1/500 (0 pending)")
        channel = SimpleNamespace(
            id=9012,
            guild=SimpleNamespace(id=5678),
            topic="old",
            edit=AsyncMock(side_effect=[DiscordFailure(), updated_channel]),
        )
        bot = GuildMemberCountTopicBot(
            _config=SimpleNamespace(
                discord_notification_channel_id=9012,
                discord_command_guild_id=5678,
            ),
            _notification_channel=channel,
            _last_topic_update_failure=None,
        )

        assert not await Gw2Bot._try_update_logging_channel_topic(
            cast(Gw2Bot, bot), "1/500 (0 pending)"
        )
        assert bot._last_topic_update_failure is not None

        with caplog.at_level(logging.INFO, logger="gw2bot.main"):
            assert await Gw2Bot._try_update_logging_channel_topic(
                cast(Gw2Bot, bot), "1/500 (0 pending)"
            )

        assert bot._last_topic_update_failure is None
        assert "Logging channel description update recovered" in caplog.text

    @patch("gw2bot.main.asyncio.sleep", new_callable=AsyncMock)
    async def test_poller_updates_topic_every_minute(self, sleep: AsyncMock) -> None:
        bot = SimpleNamespace(
            wait_until_ready=AsyncMock(),
            is_closed=MagicMock(side_effect=[False, True]),
            _api=object(),
            _update_guild_member_count_topic=AsyncMock(return_value=True),
            _poll_status=SimpleNamespace(
                record_error=MagicMock(),
                record_success=MagicMock(),
            ),
        )

        await Gw2Bot._poll_guild_member_count_topic(cast(Gw2Bot, bot))

        bot._update_guild_member_count_topic.assert_awaited_once()
        bot._poll_status.record_success.assert_called_once_with("Guild Member Count")
        bot._poll_status.record_error.assert_not_called()
        sleep.assert_awaited_once_with(GUILD_MEMBER_COUNT_TOPIC_UPDATE_SECONDS)

    @patch("gw2bot.main.asyncio.sleep", new_callable=AsyncMock)
    async def test_poller_reports_member_count_api_failure(
        self,
        sleep: AsyncMock,
    ) -> None:
        error = aiohttp.ClientError("GW2 unavailable")
        bot = SimpleNamespace(
            wait_until_ready=AsyncMock(),
            is_closed=MagicMock(side_effect=[False, True]),
            _api=object(),
            _update_guild_member_count_topic=AsyncMock(side_effect=error),
            _poll_status=SimpleNamespace(
                record_error=MagicMock(),
                record_success=MagicMock(),
            ),
        )

        await Gw2Bot._poll_guild_member_count_topic(cast(Gw2Bot, bot))

        bot._poll_status.record_error.assert_called_once_with(
            "Guild Member Count",
            error,
        )
        bot._poll_status.record_success.assert_not_called()
        sleep.assert_awaited_once_with(GUILD_MEMBER_COUNT_TOPIC_UPDATE_SECONDS)


class TestGuildLogRefresh:
    async def test_processes_new_events_before_returning(self) -> None:
        events = [
            {
                "id": 101,
                "type": "stash",
                "operation": "deposit",
                "user": "Officer.1234",
                "coins": 110_000,
            }
        ]
        api = SimpleNamespace(get_guild_log=AsyncMock(return_value=events))
        store = MagicMock()
        store.get_cursor.return_value = 100
        guild_members = SimpleNamespace(
            usernames_with_rank=AsyncMock(return_value={"Officer.1234"})
        )
        bot = SimpleNamespace(
            _api=api,
            _raffle_store=store,
            _guild_members=guild_members,
            _config=SimpleNamespace(gw2_guild_id="guild-id"),
        )

        await Gw2Bot.refresh_guild_log(cast(Gw2Bot, bot))

        api.get_guild_log.assert_awaited_once_with("guild-id", 100)
        guild_members.usernames_with_rank.assert_awaited_once_with(
            "Officer",
            force_refresh=True,
        )
        store.process_events.assert_called_once_with(events, {"Officer.1234"})
        store.initialize_cursor.assert_not_called()

    async def test_does_not_refresh_member_ranks_without_new_deposits(self) -> None:
        events = [{"id": 101, "type": "joined", "user": "Member.1234"}]
        api = SimpleNamespace(get_guild_log=AsyncMock(return_value=events))
        store = MagicMock()
        store.get_cursor.return_value = 100
        guild_members = SimpleNamespace(usernames_with_rank=AsyncMock())
        bot = SimpleNamespace(
            _api=api,
            _raffle_store=store,
            _guild_members=guild_members,
            _config=SimpleNamespace(gw2_guild_id="guild-id"),
        )

        await Gw2Bot.refresh_guild_log(cast(Gw2Bot, bot))

        guild_members.usernames_with_rank.assert_not_awaited()
        store.process_events.assert_called_once_with(events, set())

    @patch("gw2bot.main.asyncio.sleep", new_callable=AsyncMock)
    async def test_guild_log_poller_sends_deposits_to_main_and_audit_channels(
        self,
        sleep: AsyncMock,
    ) -> None:
        bot = SimpleNamespace(
            wait_until_ready=AsyncMock(),
            is_closed=MagicMock(side_effect=[False, True]),
            _session=object(),
            _api=object(),
            refresh_guild_log=AsyncMock(),
            _send_pending_raffle_notifications=AsyncMock(),
            _send_pending_deposit_audit_notifications=AsyncMock(),
            _send_pending_raffle_milestones=AsyncMock(),
            _send_pending_join_notifications=AsyncMock(),
            _send_pending_leave_notifications=AsyncMock(),
            _send_pending_invite_notifications=AsyncMock(),
            _send_pending_rank_change_notifications=AsyncMock(),
            _poll_status=SimpleNamespace(
                record_error=MagicMock(),
                record_success=MagicMock(),
            ),
            _config=SimpleNamespace(guild_log_poll_interval_seconds=60),
        )

        await Gw2Bot._poll_guild_log(cast(Gw2Bot, bot))

        bot._send_pending_raffle_notifications.assert_awaited_once()
        bot._send_pending_deposit_audit_notifications.assert_awaited_once()
        bot._send_pending_invite_notifications.assert_awaited_once()
        bot._send_pending_rank_change_notifications.assert_awaited_once()
        bot._poll_status.record_success.assert_called_once_with("Guild Log")
        sleep.assert_awaited_once_with(60)

class TestFeastNotification:
    async def test_sends_same_feast_message_to_channel_and_private_user(self) -> None:
        message = "Guild Storage is low on **Food**: 5 left"
        private_message = AsyncMock()
        bot = SimpleNamespace(
            _config=SimpleNamespace(discord_feast_notification_user_id=3456),
            _try_send_notification=AsyncMock(return_value=True),
            _send_feast_private_message=private_message,
        )

        sent = await Gw2Bot._try_send_feast_notification(
            cast(Gw2Bot, bot),
            message,
        )

        assert sent
        bot._try_send_notification.assert_awaited_once_with(message)
        private_message.assert_awaited_once_with(message)

    async def test_feast_private_message_fetches_configured_user_once(self) -> None:
        message = "Guild Storage is low on **Food**: 5 left"
        private_user = SimpleNamespace(send=AsyncMock())
        bot = SimpleNamespace(
            _config=SimpleNamespace(discord_feast_notification_user_id=3456),
            _feast_notification_user=None,
            fetch_user=AsyncMock(return_value=private_user),
        )

        await Gw2Bot._send_feast_private_message(cast(Gw2Bot, bot), message)
        await Gw2Bot._send_feast_private_message(cast(Gw2Bot, bot), message)

        bot.fetch_user.assert_awaited_once_with(3456)
        assert private_user.send.await_args_list == [call(message)] * 2

    async def test_skips_private_message_when_not_configured(self) -> None:
        bot = SimpleNamespace(
            _config=SimpleNamespace(discord_feast_notification_user_id=None),
            _try_send_notification=AsyncMock(return_value=True),
        )

        sent = await Gw2Bot._try_send_feast_notification(
            cast(Gw2Bot, bot),
            "food alert",
        )

        assert sent
        bot._try_send_notification.assert_awaited_once_with("food alert")

    async def test_does_not_private_message_when_channel_send_fails(self) -> None:
        private_message = AsyncMock()
        bot = SimpleNamespace(
            _config=SimpleNamespace(discord_feast_notification_user_id=3456),
            _try_send_notification=AsyncMock(return_value=False),
            _send_feast_private_message=private_message,
        )

        sent = await Gw2Bot._try_send_feast_notification(
            cast(Gw2Bot, bot),
            "food alert",
        )

        assert not sent
        private_message.assert_not_awaited()

    async def test_private_message_failure_does_not_repeat_channel_alert(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        bot = SimpleNamespace(
            _config=SimpleNamespace(discord_feast_notification_user_id=3456),
            _try_send_notification=AsyncMock(return_value=True),
            _send_feast_private_message=AsyncMock(
                side_effect=discord.ClientException("DM unavailable")
            ),
        )

        with caplog.at_level(logging.ERROR, logger="gw2bot.main"):
            sent = await Gw2Bot._try_send_feast_notification(
                cast(Gw2Bot, bot),
                "food alert",
            )

        assert sent


class TestDiscordNotificationDelivery:
    async def test_forbidden_logs_actionable_permission_diagnostics(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        bot = SimpleNamespace(
            _config=SimpleNamespace(discord_notification_channel_id=9012),
            _send_notification=AsyncMock(side_effect=forbidden_error(50013)),
        )

        with caplog.at_level(logging.ERROR, logger="gw2bot.main"):
            sent = await Gw2Bot._try_send_notification(
                cast(Gw2Bot, bot),
                "purchase message",
            )

        assert not sent
        assert (
            "Could not send Discord notification; reason=missing_permissions "
            "channel_id=9012 required_permissions=view_channel,send_messages "
            "(type=Forbidden status=403 code=50013)"
            in caplog.text
        )

    async def test_failure_logging_omits_raw_discord_exception_body(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        secret = "discord-raw-response-secret"

        class DiscordFailure(discord.DiscordException):
            status = 403
            code = 50001

            def __str__(self) -> str:
                return secret

        bot = SimpleNamespace(
            _config=SimpleNamespace(discord_notification_channel_id=9012),
            _send_notification=AsyncMock(side_effect=DiscordFailure()),
        )

        with caplog.at_level(logging.ERROR, logger="gw2bot.main"):
            sent = await Gw2Bot._try_send_notification(
                cast(Gw2Bot, bot),
                "purchase message",
            )

        assert not sent
        assert secret not in caplog.text
        assert "reason=missing_access" in caplog.text
        assert "type=DiscordFailure status=403 code=50001" in caplog.text


