import tempfile
import logging
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, call, patch

import aiohttp
import discord
import pytest
from discord import app_commands

from gw2bot.config import Config
from gw2bot.guild_members import TrialMemberReportEntry
from gw2bot.main import (
    RAFFLE_ADDTICKET_ROLE_ID,
    RAFFLE_DRAW_ROLE_ID,
    SUNBORNE_ROLE_ID,
    TRIAL_FORUM_CHANNEL_ID,
    TRIAL_ROLE_ID,
    Gw2Bot,
    RedactingFormatter,
    RaffleCommands,
    configure_logging,
    format_addticket_audit,
    main as run_main,
    redact_log_text,
    user_has_role,
)


class TestCommand:
    def test_registers_raffle_command_group(self) -> None:
        group = RaffleCommands(object())  # type: ignore[arg-type]
        commands = {command.name: command for command in group.commands}

        assert group.name == "raffle"
        assert group.guild_only
        assert set(commands) == {"draw", "addticket"}
        assert "win" not in commands
        addticket = commands["addticket"]
        assert isinstance(addticket, app_commands.Command)
        assert [parameter.name for parameter in addticket.parameters] == ["username"]

    def test_checks_required_raffle_roles(self) -> None:
        draw_user = SimpleNamespace(roles=[SimpleNamespace(id=RAFFLE_DRAW_ROLE_ID)])
        add_user = SimpleNamespace(roles=[SimpleNamespace(id=RAFFLE_ADDTICKET_ROLE_ID)])
        no_roles_user = SimpleNamespace()

        assert user_has_role(draw_user, RAFFLE_DRAW_ROLE_ID)
        assert not user_has_role(draw_user, RAFFLE_ADDTICKET_ROLE_ID)
        assert user_has_role(add_user, RAFFLE_ADDTICKET_ROLE_ID)
        assert not user_has_role(no_roles_user, RAFFLE_DRAW_ROLE_ID)

    def test_formats_addticket_audit_with_discord_mention(self) -> None:
        assert (
            format_addticket_audit(123456789, "Username.1234")
            == "<@123456789> added 1 raffle ticket to Username.1234."
        )

    @patch("gw2bot.main.logging.basicConfig")
    def test_configures_application_debug_logging_only(
        self,
        basic_config: MagicMock,
    ) -> None:
        app_logger = logging.getLogger("gw2bot")
        previous_level = app_logger.level
        try:
            configure_logging(True)
            assert app_logger.level == logging.DEBUG

            configure_logging(False)
            assert app_logger.level == logging.INFO
        finally:
            app_logger.setLevel(previous_level)

        assert basic_config.call_args.kwargs["level"] == logging.INFO
        assert basic_config.call_args.kwargs["force"]
        handlers = basic_config.call_args.kwargs["handlers"]
        assert len(handlers) == 1
        assert isinstance(handlers[0].formatter, RedactingFormatter)

    def test_redacts_credentials_from_http_request_and_response_logs(self) -> None:
        message = (
            "GET https://example.test/v2/account?access_token=query-secret "
            "headers={'Authorization': 'Bearer header-secret'} "
            "response={'subtoken': 'response-secret'} configured-secret"
        )

        redacted = redact_log_text(message, ("configured-secret",))

        for secret in (
            "query-secret",
            "header-secret",
            "response-secret",
            "configured-secret",
        ):
            assert secret not in redacted
        assert redacted.count("[REDACTED]") == 4

    def test_strips_complete_url_query_strings_with_unknown_parameters(self) -> None:
        message = (
            "request failed: https://example.test/log?since=42&opaque=mystery-secret "
            "and HTTP://OTHER.TEST/path?custom=another-secret"
        )

        redacted = redact_log_text(message)

        assert redacted == (
            "request failed: https://example.test/log?[REDACTED] "
            "and HTTP://OTHER.TEST/path?[REDACTED]"
        )
        assert "mystery-secret" not in redacted
        assert "another-secret" not in redacted

    def test_redacting_formatter_sanitizes_exception_tracebacks(self) -> None:
        secret = "configured-secret"
        try:
            raise RuntimeError(
                "request failed with Authorization: Bearer configured-secret"
            )
        except RuntimeError:
            record = logging.LogRecord(
                "aiohttp.client",
                logging.ERROR,
                __file__,
                1,
                "HTTP request failed",
                (),
                sys.exc_info(),
            )

        formatted = RedactingFormatter("%(message)s", (secret,)).format(record)

        assert secret not in formatted
        assert "[REDACTED]" in formatted

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


class TestRaffleDrawCommand:
    async def test_defers_before_running_raffle_and_uses_followup(self) -> None:
        events: list[str] = []
        bot = SimpleNamespace(
            authorize_raffle_command=AsyncMock(return_value=True),
            get_pending_raffle_result=MagicMock(return_value=None),
            refresh_guild_log=AsyncMock(side_effect=lambda: events.append("refresh")),
            run_raffle=MagicMock(
                side_effect=lambda: (
                    events.append("run"),
                    SimpleNamespace(
                        run_id=7,
                        winner="Winner.1234",
                        total_tickets=10,
                    ),
                )[1]
            ),
            mark_raffle_announcement_sent=MagicMock(),
        )
        interaction = SimpleNamespace(
            response=SimpleNamespace(
                defer=AsyncMock(side_effect=lambda: events.append("defer")),
                send_message=AsyncMock(),
            ),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        group = RaffleCommands(bot)  # type: ignore[arg-type]
        draw = next(command for command in group.commands if command.name == "draw")

        await draw.callback(group, interaction)  # type: ignore[arg-type]

        assert events == ["defer", "refresh", "run"]
        interaction.response.send_message.assert_not_awaited()
        interaction.followup.send.assert_awaited_once_with(
            "Raffle winner: **Winner.1234**! Selected from 10 tickets. "
            "All current raffle tickets have been reset."
        )
        bot.mark_raffle_announcement_sent.assert_called_once_with(7)

    async def test_retries_pending_announcement_without_refreshing_or_redrawing(
        self,
    ) -> None:
        pending = SimpleNamespace(
            run_id=7,
            winner="Winner.1234",
            total_tickets=10,
        )
        bot = SimpleNamespace(
            authorize_raffle_command=AsyncMock(return_value=True),
            get_pending_raffle_result=MagicMock(return_value=pending),
            refresh_guild_log=AsyncMock(),
            run_raffle=MagicMock(),
            mark_raffle_announcement_sent=MagicMock(),
        )
        interaction = SimpleNamespace(
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        group = RaffleCommands(bot)  # type: ignore[arg-type]
        draw = next(command for command in group.commands if command.name == "draw")

        await draw.callback(group, interaction)  # type: ignore[arg-type]

        bot.refresh_guild_log.assert_not_awaited()
        bot.run_raffle.assert_not_called()
        bot.mark_raffle_announcement_sent.assert_called_once_with(7)

    async def test_preserves_pending_announcement_when_discord_send_fails(
        self,
    ) -> None:
        bot = SimpleNamespace(
            authorize_raffle_command=AsyncMock(return_value=True),
            get_pending_raffle_result=MagicMock(return_value=None),
            refresh_guild_log=AsyncMock(),
            run_raffle=MagicMock(
                return_value=SimpleNamespace(
                    run_id=7,
                    winner="Winner.1234",
                    total_tickets=10,
                )
            ),
            mark_raffle_announcement_sent=MagicMock(),
        )
        interaction = SimpleNamespace(
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(
                send=AsyncMock(
                    side_effect=discord.ClientException("Discord unavailable")
                )
            ),
        )
        group = RaffleCommands(bot)  # type: ignore[arg-type]
        draw = next(command for command in group.commands if command.name == "draw")

        with pytest.raises(discord.ClientException):
            await draw.callback(group, interaction)  # type: ignore[arg-type]

        bot.mark_raffle_announcement_sent.assert_not_called()

    async def test_does_not_draw_when_guild_log_refresh_fails(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        bot = SimpleNamespace(
            authorize_raffle_command=AsyncMock(return_value=True),
            get_pending_raffle_result=MagicMock(return_value=None),
            refresh_guild_log=AsyncMock(
                side_effect=TimeoutError("GW2 API unavailable")
            ),
            run_raffle=MagicMock(),
            mark_raffle_announcement_sent=MagicMock(),
        )
        interaction = SimpleNamespace(
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        group = RaffleCommands(bot)  # type: ignore[arg-type]
        draw = next(command for command in group.commands if command.name == "draw")

        with caplog.at_level(logging.ERROR, logger="gw2bot.main"):
            await draw.callback(group, interaction)  # type: ignore[arg-type]

        bot.run_raffle.assert_not_called()
        interaction.followup.send.assert_awaited_once_with(
            "Could not refresh guild deposits. No raffle was drawn.",
            ephemeral=True,
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
        self.tree.sync = AsyncMock(side_effect=_forbidden_error(50001))
        bot = SimpleNamespace(_config=self.config, tree=self.tree)

        with caplog.at_level(logging.ERROR, logger="gw2bot.main"):
            await Gw2Bot._sync_commands(bot)  # type: ignore[arg-type]

        assert "Missing Access" in caplog.text
        assert "Monitoring will continue" in caplog.text
        self.tree.clear_commands.assert_not_called()

    async def test_other_command_sync_permission_errors_are_raised(self) -> None:
        self.tree.sync = AsyncMock(side_effect=_forbidden_error(50013))
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
        assert not bot.intents.members
        assert bot.intents.message_content
        raffle_store.assert_called_once()


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
                "daily at 17:00 UTC." in message
                for message in caplog.messages
            )
            == 1
        )
        assert bot._ready_announced


class TestGuildLogRefresh:
    async def test_processes_new_events_before_returning(self) -> None:
        events = [{"id": 101, "type": "stash"}]
        api = SimpleNamespace(get_guild_log=AsyncMock(return_value=events))
        store = MagicMock()
        store.get_cursor.return_value = 100
        bot = SimpleNamespace(
            _api=api,
            _raffle_store=store,
            _config=SimpleNamespace(gw2_guild_id="guild-id"),
        )

        await Gw2Bot.refresh_guild_log(cast(Gw2Bot, bot))

        api.get_guild_log.assert_awaited_once_with("guild-id", 100)
        store.process_events.assert_called_once_with(events)
        store.initialize_cursor.assert_not_called()


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


class TestTrialMemberNotification:
    async def test_posts_overdue_trial_report_to_notification_channel(self) -> None:
        now = datetime(2026, 6, 7, 17, 0, tzinfo=UTC)
        api = SimpleNamespace(
            get_guild_members=AsyncMock(
                return_value=[
                    {
                        "name": "Overdue.1234",
                        "rank": "Trial",
                        "joined": (now - timedelta(days=14)).isoformat(),
                    },
                    {
                        "name": "Recent.1234",
                        "rank": "Trial",
                        "joined": (now - timedelta(days=1)).isoformat(),
                    },
                ]
            )
        )
        bot = SimpleNamespace(
            _api=api,
            _config=SimpleNamespace(gw2_guild_id="guild-id"),
            _resolve_trial_member_discord_statuses=AsyncMock(
                return_value=["Overdue.1234"]
            ),
            _try_send_notification=AsyncMock(return_value=True),
        )

        delivered = await Gw2Bot._check_overdue_trials(cast(Gw2Bot, bot), now)

        assert delivered
        api.get_guild_members.assert_awaited_once_with("guild-id")
        message = bot._try_send_notification.await_args.args[0]
        assert "Overdue.1234" in message
        assert "Recent.1234" not in message
        assert "ranked up to Sunborne" in message

    async def test_does_not_post_when_no_trials_are_overdue(self) -> None:
        bot = SimpleNamespace(
            _api=SimpleNamespace(get_guild_members=AsyncMock(return_value=[])),
            _config=SimpleNamespace(gw2_guild_id="guild-id"),
            _resolve_trial_member_discord_statuses=AsyncMock(return_value=[]),
            _try_send_notification=AsyncMock(return_value=True),
        )

        delivered = await Gw2Bot._check_overdue_trials(
            cast(Gw2Bot, bot),
            datetime(2026, 6, 7, tzinfo=UTC),
        )

        assert delivered
        bot._try_send_notification.assert_not_awaited()

    async def test_reports_failed_delivery_to_poller(self) -> None:
        now = datetime(2026, 6, 7, 17, 0, tzinfo=UTC)
        bot = SimpleNamespace(
            _api=SimpleNamespace(
                get_guild_members=AsyncMock(
                    return_value=[
                        {
                            "name": "Overdue.1234",
                            "rank": "Trial",
                            "joined": (now - timedelta(days=14)).isoformat(),
                        }
                    ]
                )
            ),
            _config=SimpleNamespace(gw2_guild_id="guild-id"),
            _resolve_trial_member_discord_statuses=AsyncMock(
                return_value=["Overdue.1234"]
            ),
            _try_send_notification=AsyncMock(return_value=False),
        )

        delivered = await Gw2Bot._check_overdue_trials(cast(Gw2Bot, bot), now)

        assert not delivered

    async def test_resolves_statuses_from_cached_forum_message_authors(self) -> None:
        sunborne_author = SimpleNamespace(
            id=101,
            roles=[SimpleNamespace(id=SUNBORNE_ROLE_ID)],
        )
        trial_author = SimpleNamespace(
            id=202,
            roles=[SimpleNamespace(id=TRIAL_ROLE_ID)],
        )
        no_role_author = SimpleNamespace(id=303, roles=[])
        reviewer = SimpleNamespace(id=999, roles=[])

        def messages(*items: tuple[str, Any]) -> Any:
            async def iterate() -> Any:
                for content, author in items:
                    yield SimpleNamespace(content=content, author=author)

            return iterate()

        active_thread = SimpleNamespace(
            id=1,
            parent_id=TRIAL_FORUM_CHANNEL_ID,
            owner_id=101,
            owner=None,
            applied_tags=[SimpleNamespace(name="Accepted")],
            name="Application",
            history=lambda **_: messages(
                ("GW2 account is title.1234", sunborne_author)
            ),
        )
        archived_thread = SimpleNamespace(
            id=2,
            parent_id=TRIAL_FORUM_CHANNEL_ID,
            owner_id=202,
            owner=None,
            applied_tags=[SimpleNamespace(name="accepted")],
            name="Application",
            history=lambda **_: messages(
                ("My account is BODY.2345", trial_author),
                ("Reviewer confirmed comment.3456", reviewer),
            ),
        )
        third_thread = SimpleNamespace(
            id=3,
            parent_id=TRIAL_FORUM_CHANNEL_ID,
            owner_id=303,
            owner=None,
            applied_tags=[SimpleNamespace(name="Accepted")],
            name="NoRole.4567",
            history=lambda **_: messages(("No extra content", no_role_author)),
        )
        guild = SimpleNamespace(
            active_threads=AsyncMock(return_value=[active_thread]),
            fetch_member=AsyncMock(),
        )

        async def archived_threads(**_: Any) -> Any:
            yield archived_thread
            yield third_thread

        forum = SimpleNamespace(
            id=TRIAL_FORUM_CHANNEL_ID,
            guild=guild,
            archived_threads=archived_threads,
        )
        bot = SimpleNamespace(fetch_channel=AsyncMock(return_value=forum))

        entries = await Gw2Bot._resolve_trial_member_discord_statuses(
            cast(Gw2Bot, bot),
            [
                "Title.1234",
                "Body.2345",
                "Comment.3456",
                "NoRole.4567",
                "Missing.5678",
            ],
        )

        assert entries == [
            TrialMemberReportEntry("Title.1234", 101, "Sunborne"),
            TrialMemberReportEntry("Body.2345", 202, "Trial"),
            TrialMemberReportEntry("Comment.3456", 202, "Trial"),
            TrialMemberReportEntry("NoRole.4567", 303),
            TrialMemberReportEntry("Missing.5678"),
        ]
        bot.fetch_channel.assert_awaited_once_with(TRIAL_FORUM_CHANNEL_ID)
        guild.fetch_member.assert_awaited_once_with(303)

    async def test_uses_discord_indexed_search_without_reading_thread_history(
        self,
    ) -> None:
        history = MagicMock()
        guild = SimpleNamespace(
            id=123,
            active_threads=AsyncMock(return_value=[]),
            get_member=MagicMock(
                return_value=SimpleNamespace(
                    roles=[SimpleNamespace(id=SUNBORNE_ROLE_ID)]
                )
            ),
            fetch_member=AsyncMock(),
        )

        async def archived_threads(**_: Any) -> Any:
            if False:
                yield None

        forum = SimpleNamespace(
            id=TRIAL_FORUM_CHANNEL_ID,
            guild=guild,
            available_tags=[SimpleNamespace(id=42, name="Accepted")],
            archived_threads=archived_threads,
        )
        search = AsyncMock(
            return_value={
                "total_results": 1,
                "messages": [
                    [
                        {
                            "content": "GW2 account is Indexed.1234",
                            "channel_id": "900",
                        }
                    ]
                ],
                "threads": [
                    {
                        "id": "900",
                        "parent_id": str(TRIAL_FORUM_CHANNEL_ID),
                        "owner_id": "777",
                        "applied_tags": ["42"],
                    }
                ],
            }
        )
        bot = SimpleNamespace(
            fetch_channel=AsyncMock(return_value=forum),
            http=SimpleNamespace(request=search),
        )

        entries = await Gw2Bot._resolve_trial_member_discord_statuses(
            cast(Gw2Bot, bot),
            ["Indexed.1234"],
        )

        assert entries == [TrialMemberReportEntry("Indexed.1234", 777, "Sunborne")]
        history.assert_not_called()
        guild.fetch_member.assert_not_awaited()
        search.assert_awaited_once()
        search_call = search.await_args
        assert search_call is not None
        route = search_call.args[0]
        assert route.path == "/guilds/{guild_id}/messages/search"
        assert ("channel_id", str(TRIAL_FORUM_CHANNEL_ID)) in search_call.kwargs["params"]

    @patch("gw2bot.main.asyncio.sleep", new_callable=AsyncMock)
    async def test_retries_discord_search_while_index_is_unavailable(
        self,
        sleep: AsyncMock,
    ) -> None:
        guild = SimpleNamespace(
            id=123,
            active_threads=AsyncMock(return_value=[]),
            get_member=MagicMock(
                return_value=SimpleNamespace(roles=[SimpleNamespace(id=TRIAL_ROLE_ID)])
            ),
            fetch_member=AsyncMock(),
        )

        async def archived_threads(**_: Any) -> Any:
            if False:
                yield None

        forum = SimpleNamespace(
            id=TRIAL_FORUM_CHANNEL_ID,
            guild=guild,
            available_tags=[SimpleNamespace(id=42, name="Accepted")],
            archived_threads=archived_threads,
        )
        search = AsyncMock(
            side_effect=[
                {"code": 110000, "retry_after": 0.25},
                {
                    "total_results": 1,
                    "messages": [[{"content": "Retry.1234", "channel_id": "900"}]],
                    "threads": [
                        {
                            "id": "900",
                            "parent_id": str(TRIAL_FORUM_CHANNEL_ID),
                            "owner_id": "777",
                            "applied_tags": ["42"],
                        }
                    ],
                },
            ]
        )
        bot = SimpleNamespace(
            fetch_channel=AsyncMock(return_value=forum),
            http=SimpleNamespace(request=search),
        )

        entries = await Gw2Bot._resolve_trial_member_discord_statuses(
            cast(Gw2Bot, bot),
            ["Retry.1234"],
        )

        assert entries == [TrialMemberReportEntry("Retry.1234", 777, "Trial")]
        assert search.await_count == 2
        sleep.assert_awaited_once_with(0.25)

    @patch("gw2bot.main.asyncio.sleep", new_callable=AsyncMock)
    async def test_indexed_search_checks_members_without_per_member_delay(
        self,
        sleep: AsyncMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        guild = SimpleNamespace(
            id=123,
            active_threads=AsyncMock(return_value=[]),
        )

        async def archived_threads(**_: Any) -> Any:
            if False:
                yield None

        forum = SimpleNamespace(
            id=TRIAL_FORUM_CHANNEL_ID,
            guild=guild,
            available_tags=[SimpleNamespace(id=42, name="Accepted")],
            archived_threads=archived_threads,
        )
        search = AsyncMock(
            return_value={
                "total_results": 0,
                "messages": [],
                "threads": [],
            }
        )
        bot = SimpleNamespace(
            fetch_channel=AsyncMock(return_value=forum),
            http=SimpleNamespace(request=search),
        )

        with caplog.at_level(logging.DEBUG, logger="gw2bot.main"):
            entries = await Gw2Bot._resolve_trial_member_discord_statuses(
                cast(Gw2Bot, bot),
                ["One.1234", "Two.1234", "Three.1234", "Four.1234"],
            )

        assert entries == [
            TrialMemberReportEntry("One.1234"),
            TrialMemberReportEntry("Two.1234"),
            TrialMemberReportEntry("Three.1234"),
            TrialMemberReportEntry("Four.1234"),
        ]
        assert search.await_count == 4
        sleep.assert_not_awaited()
        assert (
            "checking Discord indexed search without a per-member delay"
            in caplog.text
        )
        assert "Trial member One.1234 (1/4; attempt 1/3)" in caplog.text
        assert "Trial member Four.1234 (4/4; attempt 1/3)" in caplog.text

    async def test_indexed_search_uses_title_only_fallback_without_history(
        self,
    ) -> None:
        history = MagicMock()
        owner = SimpleNamespace(
            id=777,
            roles=[SimpleNamespace(id=TRIAL_ROLE_ID)],
        )
        accepted_thread = SimpleNamespace(
            id=900,
            parent_id=TRIAL_FORUM_CHANNEL_ID,
            owner_id=777,
            owner=owner,
            applied_tags=[SimpleNamespace(name="Accepted")],
            name="TitleOnly.1234 application",
            history=history,
        )
        guild = SimpleNamespace(
            id=123,
            active_threads=AsyncMock(return_value=[accepted_thread]),
            fetch_member=AsyncMock(),
        )

        async def archived_threads(**_: Any) -> Any:
            if False:
                yield None

        forum = SimpleNamespace(
            id=TRIAL_FORUM_CHANNEL_ID,
            guild=guild,
            available_tags=[SimpleNamespace(id=42, name="Accepted")],
            archived_threads=archived_threads,
        )
        search = AsyncMock(
            return_value={
                "total_results": 1,
                "messages": [[{"content": "TitleOnly.1234", "channel_id": "901"}]],
                "threads": [
                    {
                        "id": "901",
                        "parent_id": str(TRIAL_FORUM_CHANNEL_ID),
                        "owner_id": "888",
                        "applied_tags": ["99"],
                    }
                ],
            }
        )
        bot = SimpleNamespace(
            fetch_channel=AsyncMock(return_value=forum),
            http=SimpleNamespace(request=search),
        )

        entries = await Gw2Bot._resolve_trial_member_discord_statuses(
            cast(Gw2Bot, bot),
            ["TitleOnly.1234"],
        )

        assert entries == [TrialMemberReportEntry("TitleOnly.1234", 777, "Trial")]
        history.assert_not_called()
        guild.fetch_member.assert_not_awaited()
        search.assert_not_awaited()

    async def test_skips_forum_posts_without_accepted_tag(self) -> None:
        async def empty_messages() -> Any:
            if False:
                yield None

        trial_owner = SimpleNamespace(
            id=777,
            roles=[SimpleNamespace(id=TRIAL_ROLE_ID)],
        )
        rejected_history = MagicMock()
        rejected_thread = SimpleNamespace(
            id=1,
            parent_id=TRIAL_FORUM_CHANNEL_ID,
            owner_id=1,
            owner=trial_owner,
            applied_tags=[SimpleNamespace(name="Rejected")],
            name="Rejected.1234 application",
            history=rejected_history,
        )
        accepted_thread = SimpleNamespace(
            id=777,
            parent_id=TRIAL_FORUM_CHANNEL_ID,
            owner_id=777,
            owner=trial_owner,
            applied_tags=[],
            _applied_tags=[42],
            name="Accepted.1234 application",
            history=lambda **_: empty_messages(),
        )
        guild = SimpleNamespace(active_threads=AsyncMock(return_value=[]))

        async def archived_threads(**_: Any) -> Any:
            yield rejected_thread
            yield accepted_thread

        forum = SimpleNamespace(
            id=TRIAL_FORUM_CHANNEL_ID,
            guild=guild,
            available_tags=[SimpleNamespace(id=42, name="Accepted")],
            archived_threads=archived_threads,
        )
        bot = SimpleNamespace(fetch_channel=AsyncMock(return_value=forum))

        entries = await Gw2Bot._resolve_trial_member_discord_statuses(
            cast(Gw2Bot, bot),
            ["Rejected.1234", "Accepted.1234"],
        )

        assert entries == [
            TrialMemberReportEntry("Rejected.1234"),
            TrialMemberReportEntry("Accepted.1234", 777, "Trial"),
        ]
        rejected_history.assert_not_called()

    async def test_resolves_role_after_accepted_post_match_only(self) -> None:
        async def messages() -> Any:
            yield SimpleNamespace(
                content="Matched.1234",
                author=SimpleNamespace(id=999, roles=[]),
            )

        accepted_thread = SimpleNamespace(
            id=1,
            parent_id=TRIAL_FORUM_CHANNEL_ID,
            owner_id=777,
            owner=None,
            applied_tags=[SimpleNamespace(name="Accepted")],
            name="Application",
            history=lambda **_: messages(),
        )
        guild = SimpleNamespace(
            active_threads=AsyncMock(return_value=[accepted_thread]),
            get_member=MagicMock(return_value=None),
            fetch_member=AsyncMock(
                return_value=SimpleNamespace(roles=[SimpleNamespace(id=TRIAL_ROLE_ID)])
            ),
        )

        async def archived_threads(**_: Any) -> Any:
            if False:
                yield None

        forum = SimpleNamespace(
            id=TRIAL_FORUM_CHANNEL_ID,
            guild=guild,
            available_tags=[],
            archived_threads=archived_threads,
        )
        bot = SimpleNamespace(fetch_channel=AsyncMock(return_value=forum))

        entries = await Gw2Bot._resolve_trial_member_discord_statuses(
            cast(Gw2Bot, bot),
            ["Matched.1234"],
        )

        assert entries == [TrialMemberReportEntry("Matched.1234", 777, "Trial")]
        guild.fetch_member.assert_awaited_once_with(777)

    async def test_preserves_matched_user_id_when_creator_left_guild(self) -> None:
        async def messages() -> Any:
            yield SimpleNamespace(
                content="Former.1234",
                author=SimpleNamespace(id=999, roles=[]),
            )

        accepted_thread = SimpleNamespace(
            id=1,
            parent_id=TRIAL_FORUM_CHANNEL_ID,
            owner_id=777,
            owner=None,
            applied_tags=[SimpleNamespace(name="Accepted")],
            name="Application",
            history=lambda **_: messages(),
        )
        guild = SimpleNamespace(
            active_threads=AsyncMock(return_value=[accepted_thread]),
            get_member=MagicMock(return_value=None),
            fetch_member=AsyncMock(side_effect=_not_found_error()),
        )

        async def archived_threads(**_: Any) -> Any:
            if False:
                yield None

        forum = SimpleNamespace(
            id=TRIAL_FORUM_CHANNEL_ID,
            guild=guild,
            available_tags=[],
            archived_threads=archived_threads,
        )
        bot = SimpleNamespace(fetch_channel=AsyncMock(return_value=forum))

        entries = await Gw2Bot._resolve_trial_member_discord_statuses(
            cast(Gw2Bot, bot),
            ["Former.1234"],
        )

        assert entries == [TrialMemberReportEntry("Former.1234", 777)]

    @patch("gw2bot.main.seconds_until_trial_report", return_value=123)
    @patch("gw2bot.main.asyncio.sleep", new_callable=AsyncMock)
    async def test_poller_checks_immediately_then_waits_for_daily_run(
        self,
        sleep: AsyncMock,
        seconds_until_report: MagicMock,
    ) -> None:
        bot = SimpleNamespace(
            wait_until_ready=AsyncMock(),
            is_closed=MagicMock(side_effect=[False, True]),
            _check_overdue_trials=AsyncMock(return_value=True),
            _handle_poll_error=AsyncMock(),
            _handle_poll_success=AsyncMock(),
            _config=SimpleNamespace(poll_interval_seconds=30),
        )

        await Gw2Bot._poll_overdue_trials(bot)  # type: ignore[arg-type]

        bot.wait_until_ready.assert_awaited_once()
        bot._check_overdue_trials.assert_awaited_once()
        bot._handle_poll_success.assert_awaited_once_with("Trial Members")
        seconds_until_report.assert_called_once()
        sleep.assert_awaited_once_with(123)

    @patch("gw2bot.main.seconds_until_trial_report")
    @patch("gw2bot.main.asyncio.sleep", new_callable=AsyncMock)
    async def test_poller_retries_failed_delivery_after_poll_interval(
        self,
        sleep: AsyncMock,
        seconds_until_report: MagicMock,
    ) -> None:
        bot = SimpleNamespace(
            wait_until_ready=AsyncMock(),
            is_closed=MagicMock(side_effect=[False, True]),
            _check_overdue_trials=AsyncMock(return_value=False),
            _handle_poll_error=AsyncMock(),
            _handle_poll_success=AsyncMock(),
            _config=SimpleNamespace(poll_interval_seconds=30),
        )

        await Gw2Bot._poll_overdue_trials(bot)  # type: ignore[arg-type]

        seconds_until_report.assert_not_called()
        sleep.assert_awaited_once_with(30)


class TestPollStatusNotification:
    async def test_bad_gateway_does_not_leak_api_key(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        api_key = "secret-api-key"
        bot = SimpleNamespace(
            _config=SimpleNamespace(
                gw2_api_key=api_key,
                discord_token="secret-discord-token",
            ),
            _last_errors={},
            _try_send_notification=AsyncMock(return_value=True),
        )
        error = aiohttp.ClientResponseError(
            SimpleNamespace(
                real_url=f"https://example.test/log?access_token={api_key}"
            ),  # type: ignore[arg-type]
            (),
            status=502,
            message="Bad Gateway",
        )

        with caplog.at_level(logging.WARNING, logger="gw2bot.main"):
            await Gw2Bot._handle_poll_error(cast(Gw2Bot, bot), "Guild Log", error)

        bot._try_send_notification.assert_not_awaited()
        assert bot._last_errors == {"Guild Log": "HTTP 502: Bad Gateway"}
        assert api_key not in caplog.text

    async def test_redacts_configured_credentials_from_poll_error(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        api_key = "secret-api-key"
        bot = SimpleNamespace(
            _config=SimpleNamespace(
                gw2_api_key=api_key,
                discord_token="secret-discord-token",
            ),
            _last_errors={},
            _try_send_notification=AsyncMock(return_value=True),
        )

        with caplog.at_level(logging.WARNING, logger="gw2bot.main"):
            await Gw2Bot._handle_poll_error(
                cast(Gw2Bot, bot),
                "Guild Log",
                TimeoutError(f"Request failed with Bearer {api_key}"),
            )

        bot._try_send_notification.assert_not_awaited()
        assert (
            "Guild Log polling failed: Request failed with Bearer [REDACTED]"
            in caplog.text
        )

    async def test_retries_same_poll_error_after_delivery_failure(self) -> None:
        bot = SimpleNamespace(
            _last_errors={},
            _try_send_notification=AsyncMock(side_effect=[False, True]),
        )
        error = TimeoutError("API unavailable")

        await Gw2Bot._handle_poll_error(cast(Gw2Bot, bot), "Guild Storage", error)
        await Gw2Bot._handle_poll_error(cast(Gw2Bot, bot), "Guild Storage", error)

        assert (
            bot._try_send_notification.await_args_list
            == [call("Guild Storage polling failed: API unavailable")] * 2
        )
        assert bot._last_errors == {"Guild Storage": "API unavailable"}

    async def test_retries_recovery_notification_after_delivery_failure(self) -> None:
        bot = SimpleNamespace(
            _last_errors={"Guild Storage": "API unavailable"},
            _try_send_notification=AsyncMock(side_effect=[False, True]),
        )

        await Gw2Bot._handle_poll_success(cast(Gw2Bot, bot), "Guild Storage")
        await Gw2Bot._handle_poll_success(cast(Gw2Bot, bot), "Guild Storage")

        assert (
            bot._try_send_notification.await_args_list
            == [call("Guild Storage polling recovered.")] * 2
        )
        assert bot._last_errors == {}

    async def test_guild_log_recovery_is_console_only(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        bot = SimpleNamespace(
            _last_errors={"Guild Log": "API unavailable"},
            _try_send_notification=AsyncMock(),
        )

        with caplog.at_level(logging.INFO, logger="gw2bot.main"):
            await Gw2Bot._handle_poll_success(cast(Gw2Bot, bot), "Guild Log")

        bot._try_send_notification.assert_not_awaited()
        assert "Guild Log polling recovered." in caplog.text
        assert bot._last_errors == {}


def _forbidden_error(code: int) -> discord.Forbidden:
    response = SimpleNamespace(status=403, reason="Forbidden")
    return discord.Forbidden(
        response,  # type: ignore[arg-type]
        {"code": code, "message": "Missing Access"},
    )


def _not_found_error() -> discord.NotFound:
    response = SimpleNamespace(status=404, reason="Not Found")
    return discord.NotFound(
        response,  # type: ignore[arg-type]
        {"code": 10007, "message": "Unknown Member"},
    )
