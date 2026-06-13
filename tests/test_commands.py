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
    RAFFLE_CONTRIBUTION_CHANNEL_ID,
    RAFFLE_DRAW_ROLE_ID,
    SUNBORNE_ROLE_ID,
    TRIAL_FORUM_CHANNEL_ID,
    TRIAL_ROLE_ID,
    Gw2Bot,
    RaffleAccountLinkModal,
    RedactingFormatter,
    RaffleCommands,
    RaffleTicketsListView,
    configure_logging,
    format_addticket_audit,
    format_raffle_contribution_report,
    raffle_contribution_report_end,
    raffle_ticket_embed,
    raffle_ticket_list_embed,
    main as run_main,
    redact_log_text,
    seconds_until_raffle_contribution_report,
    user_has_role,
)
from gw2bot.raffle import RaffleContribution, RaffleTotal


class TestCommand:
    def test_registers_raffle_command_group(self) -> None:
        group = RaffleCommands(object())  # type: ignore[arg-type]
        commands = {command.name: command for command in group.commands}

        assert group.name == "raffle"
        assert group.guild_only
        assert set(commands) == {"draw", "addticket", "tickets", "list"}
        assert "tickets-list" not in commands
        assert "win" not in commands
        addticket = commands["addticket"]
        assert isinstance(addticket, app_commands.Command)
        assert [parameter.name for parameter in addticket.parameters] == ["username"]
        tickets = commands["tickets"]
        assert isinstance(tickets, app_commands.Command)
        assert [parameter.name for parameter in tickets.parameters] == ["username"]

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


class TestRaffleTicketsCommand:
    async def test_prompts_unlinked_user_for_gw2_account(self) -> None:
        bot = SimpleNamespace(
            get_linked_raffle_username=MagicMock(return_value=None),
        )
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=1234),
            response=SimpleNamespace(send_modal=AsyncMock()),
        )
        group = RaffleCommands(bot)  # type: ignore[arg-type]
        tickets = next(
            command for command in group.commands if command.name == "tickets"
        )

        await tickets.callback(group, interaction, None)  # type: ignore[arg-type]

        modal = interaction.response.send_modal.await_args.args[0]
        assert isinstance(modal, RaffleAccountLinkModal)
        assert modal.username.placeholder == "Username.1234"

    async def test_shows_linked_users_purchased_and_free_tickets(self) -> None:
        total = raffle_total("Linked.1234", purchased=4, free=2)
        bot = SimpleNamespace(
            get_linked_raffle_username=MagicMock(return_value=total.username),
            get_raffle_total=MagicMock(return_value=total),
        )
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=1234),
            response=SimpleNamespace(send_message=AsyncMock()),
        )
        group = RaffleCommands(bot)  # type: ignore[arg-type]
        tickets = next(
            command for command in group.commands if command.name == "tickets"
        )

        await tickets.callback(group, interaction, None)  # type: ignore[arg-type]

        kwargs = interaction.response.send_message.await_args.kwargs
        embed = kwargs["embed"]
        assert isinstance(embed, discord.Embed)
        assert [field.value for field in embed.fields] == ["4", "2", "6"]
        assert kwargs["ephemeral"]

    async def test_link_modal_verifies_and_persists_gw2_account(self) -> None:
        total = raffle_total("Canonical.1234", purchased=2, free=1)
        bot = SimpleNamespace(
            resolve_guild_member=AsyncMock(return_value=total.username),
            link_raffle_account=MagicMock(),
            get_raffle_total=MagicMock(return_value=total),
        )
        modal = RaffleAccountLinkModal(bot)  # type: ignore[arg-type]
        modal.username._value = "canonical.1234"
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=1234),
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

        await modal.on_submit(interaction)  # type: ignore[arg-type]

        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        bot.resolve_guild_member.assert_awaited_once_with(
            "canonical.1234",
            force_refresh=True,
        )
        bot.link_raffle_account.assert_called_once_with(1234, "Canonical.1234")
        assert interaction.followup.send.await_args.kwargs["ephemeral"]

    async def test_link_modal_rejects_account_outside_guild(self) -> None:
        bot = SimpleNamespace(
            resolve_guild_member=AsyncMock(return_value=None),
            link_raffle_account=MagicMock(),
        )
        modal = RaffleAccountLinkModal(bot)  # type: ignore[arg-type]
        modal.username._value = "outsider.1234"
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=1234),
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

        await modal.on_submit(interaction)  # type: ignore[arg-type]

        bot.resolve_guild_member.assert_awaited_once_with(
            "outsider.1234",
            force_refresh=True,
        )
        bot.link_raffle_account.assert_not_called()
        interaction.followup.send.assert_awaited_once_with(
            "`outsider.1234` is not a member of the configured guild.",
            ephemeral=True,
        )

    async def test_any_user_can_search_a_guild_member(self) -> None:
        total = raffle_total("Member.1234", purchased=3, free=1)
        bot = SimpleNamespace(
            resolve_guild_member=AsyncMock(return_value=total.username),
            get_raffle_total=MagicMock(return_value=total),
        )
        interaction = SimpleNamespace(
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        group = RaffleCommands(bot)  # type: ignore[arg-type]
        tickets = next(
            command for command in group.commands if command.name == "tickets"
        )

        await tickets.callback(group, interaction, "member.1234")  # type: ignore[arg-type]

        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        bot.resolve_guild_member.assert_awaited_once_with("member.1234")
        embed = interaction.followup.send.await_args.kwargs["embed"]
        assert embed.title == "Raffle Tickets: Member.1234"

    async def test_lookup_failure_does_not_log_secret_bearing_exception(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        secret = "ticket-lookup-secret"
        bot = SimpleNamespace(
            resolve_guild_member=AsyncMock(
                side_effect=aiohttp.ClientError(
                    f"request failed with access_token={secret}"
                )
            ),
        )
        interaction = SimpleNamespace(
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        group = RaffleCommands(bot)  # type: ignore[arg-type]
        tickets = next(
            command for command in group.commands if command.name == "tickets"
        )

        with caplog.at_level(logging.ERROR, logger="gw2bot.main"):
            await tickets.callback(group, interaction, "member.1234")  # type: ignore[arg-type]

        assert secret not in caplog.text
        assert "Could not refresh the guild member cache" in caplog.text
        interaction.followup.send.assert_awaited_once_with(
            "Could not verify guild membership. Try again later.",
            ephemeral=True,
        )

    async def test_list_only_includes_buttons_for_multiple_pages(self) -> None:
        bot = SimpleNamespace(get_raffle_totals=MagicMock(return_value=[]))
        group = RaffleCommands(bot)  # type: ignore[arg-type]
        tickets_list = next(
            command for command in group.commands if command.name == "list"
        )
        interaction = SimpleNamespace(
            response=SimpleNamespace(send_message=AsyncMock()),
        )

        bot.get_raffle_totals.return_value = [
            raffle_total(f"Member {index:02d}.1234") for index in range(10)
        ]
        await tickets_list.callback(group, interaction)  # type: ignore[arg-type]
        assert "view" not in interaction.response.send_message.await_args.kwargs

        interaction.response.send_message.reset_mock()
        bot.get_raffle_totals.return_value = [
            raffle_total(f"Member {index:02d}.1234") for index in range(11)
        ]
        await tickets_list.callback(group, interaction)  # type: ignore[arg-type]
        view = interaction.response.send_message.await_args.kwargs["view"]
        assert isinstance(view, RaffleTicketsListView)
        assert len(view.children) == 2

    async def test_list_paginates_ten_players_at_a_time(self) -> None:
        totals = [
            raffle_total(f"Member {index:02d}.1234", purchased=index)
            for index in range(11)
        ]
        first_embed = raffle_ticket_list_embed(totals, 0)
        view = RaffleTicketsListView(totals)
        interaction = SimpleNamespace(
            response=SimpleNamespace(edit_message=AsyncMock()),
        )

        assert "Member 09.1234" in (first_embed.description or "")
        assert "Member 10.1234" not in (first_embed.description or "")

        await view.change_page(interaction, 1)  # type: ignore[arg-type]

        second_embed = interaction.response.edit_message.await_args.kwargs["embed"]
        assert "Member 10.1234" in (second_embed.description or "")
        assert "Member 09.1234" not in (second_embed.description or "")

    def test_formats_ticket_embed(self) -> None:
        embed = raffle_ticket_embed(
            raffle_total("Member.1234", purchased=5, free=3)
        )

        assert embed.title == "Raffle Tickets: Member.1234"
        assert [field.name for field in embed.fields] == [
            "Purchased Tickets",
            "Free Tickets",
            "Total Tickets",
        ]


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
                "daily at 17:00 UTC; raffle contribution reporting every 6 hours "
                "UTC." in message
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
    async def test_poller_waits_for_daily_schedule_before_first_check(
        self,
        sleep: AsyncMock,
        seconds_until_report: MagicMock,
    ) -> None:
        bot = SimpleNamespace(
            wait_until_ready=AsyncMock(),
            is_closed=MagicMock(side_effect=[False, False, True]),
            _check_overdue_trials=AsyncMock(return_value=True),
            _handle_poll_error=AsyncMock(),
            _handle_poll_success=AsyncMock(),
        )

        await Gw2Bot._poll_overdue_trials(bot)  # type: ignore[arg-type]

        bot.wait_until_ready.assert_awaited_once()
        bot._check_overdue_trials.assert_awaited_once()
        bot._handle_poll_success.assert_awaited_once_with("Trial Members")
        seconds_until_report.assert_called_once()
        sleep.assert_awaited_once_with(123)

    @patch("gw2bot.main.seconds_until_trial_report", return_value=123)
    @patch("gw2bot.main.asyncio.sleep", new_callable=AsyncMock)
    async def test_poller_does_not_run_if_closed_during_scheduled_wait(
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
        )

        await Gw2Bot._poll_overdue_trials(bot)  # type: ignore[arg-type]

        seconds_until_report.assert_called_once()
        sleep.assert_awaited_once_with(123)
        bot._check_overdue_trials.assert_not_awaited()
        bot._handle_poll_success.assert_not_awaited()

    @patch("gw2bot.main.seconds_until_trial_report", side_effect=[123, 456])
    @patch("gw2bot.main.asyncio.sleep", new_callable=AsyncMock)
    async def test_poller_waits_for_next_daily_schedule_after_failure(
        self,
        sleep: AsyncMock,
        seconds_until_report: MagicMock,
    ) -> None:
        error = aiohttp.ClientError("Guild members unavailable")
        bot = SimpleNamespace(
            wait_until_ready=AsyncMock(),
            is_closed=MagicMock(side_effect=[False, False, False, True]),
            _check_overdue_trials=AsyncMock(side_effect=error),
            _handle_poll_error=AsyncMock(),
            _handle_poll_success=AsyncMock(),
        )

        await Gw2Bot._poll_overdue_trials(bot)  # type: ignore[arg-type]

        assert seconds_until_report.call_count == 2
        assert sleep.await_args_list == [call(123), call(456)]
        bot._check_overdue_trials.assert_awaited_once()
        bot._handle_poll_error.assert_awaited_once_with("Trial Members", error)
        bot._handle_poll_success.assert_not_awaited()


class TestRaffleContributionNotification:
    def test_schedules_fixed_six_hour_utc_boundaries(self) -> None:
        now = datetime(2026, 6, 7, 5, 30, tzinfo=UTC)

        assert raffle_contribution_report_end(now) == datetime(
            2026,
            6,
            7,
            0,
            tzinfo=UTC,
        )
        assert seconds_until_raffle_contribution_report(now) == 30 * 60
        assert seconds_until_raffle_contribution_report(
            datetime(2026, 6, 7, 6, tzinfo=UTC)
        ) == 6 * 60 * 60

    def test_formats_contributors_and_skips_empty_report(self) -> None:
        contributions = [
            RaffleContribution("Alpha.1234", 2, 1),
            RaffleContribution("Beta.1234", 0, 2),
        ]

        messages = format_raffle_contribution_report(contributions)

        assert len(messages) == 1
        assert "**Alpha.1234** - Total: 3 | Purchased: 2 | Free: 1" in messages[0]
        assert "**Beta.1234** - Total: 2 | Free: 2" in messages[0]
        assert format_raffle_contribution_report([]) == []

    async def test_empty_window_does_not_send_message(self) -> None:
        report_end = datetime(2026, 6, 7, 6, tzinfo=UTC)
        bot = SimpleNamespace(
            get_raffle_contributions=MagicMock(return_value=[]),
            _send_raffle_contribution_message=AsyncMock(),
        )

        await Gw2Bot._send_raffle_contribution_report(
            cast(Gw2Bot, bot),
            report_end,
        )

        bot.get_raffle_contributions.assert_called_once_with(
            datetime(2026, 6, 7, 0, tzinfo=UTC),
            report_end,
        )
        bot._send_raffle_contribution_message.assert_not_awaited()

    async def test_free_ticket_only_window_sends_message(self) -> None:
        report_end = datetime(2026, 6, 7, 6, tzinfo=UTC)
        bot = SimpleNamespace(
            get_raffle_contributions=MagicMock(
                return_value=[RaffleContribution("Free Only.1234", 0, 1)]
            ),
            _send_raffle_contribution_message=AsyncMock(),
        )

        await Gw2Bot._send_raffle_contribution_report(
            cast(Gw2Bot, bot),
            report_end,
        )

        bot._send_raffle_contribution_message.assert_awaited_once_with(
            "**Raffle contributions from the last 6 hours**\n"
            "* **Free Only.1234** - Total: 1 | Free: 1"
        )

    async def test_sends_pending_purchase_messages_to_raffle_channel(self) -> None:
        deposit = SimpleNamespace(event_id=101, message="purchase message")
        store = MagicMock()
        store.get_pending_notifications.return_value = [deposit]
        bot = SimpleNamespace(
            _raffle_store=store,
            _try_send_raffle_contribution_message=AsyncMock(return_value=True),
        )

        await Gw2Bot._send_pending_raffle_notifications(cast(Gw2Bot, bot))

        bot._try_send_raffle_contribution_message.assert_awaited_once_with(
            "purchase message"
        )
        store.mark_notification_sent.assert_called_once_with(101)

    async def test_retries_pending_purchase_after_raffle_channel_failure(
        self,
    ) -> None:
        deposit = SimpleNamespace(event_id=101, message="purchase message")
        store = MagicMock()
        store.get_pending_notifications.return_value = [deposit]
        bot = SimpleNamespace(
            _raffle_store=store,
            _try_send_raffle_contribution_message=AsyncMock(return_value=False),
        )

        await Gw2Bot._send_pending_raffle_notifications(cast(Gw2Bot, bot))

        store.mark_notification_sent.assert_not_called()

    async def test_raffle_channel_failure_does_not_log_credentials(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        secret = "raffle-channel-secret"
        bot = SimpleNamespace(
            _send_raffle_contribution_message=AsyncMock(
                side_effect=discord.ClientException(secret)
            ),
        )

        with caplog.at_level(logging.ERROR, logger="gw2bot.main"):
            sent = await Gw2Bot._try_send_raffle_contribution_message(
                cast(Gw2Bot, bot),
                "purchase message",
            )

        assert not sent
        assert secret not in caplog.text
        assert "Could not send raffle contribution message" in caplog.text

    async def test_sends_report_to_configured_gw2_chat_and_caches_channel(
        self,
    ) -> None:
        channel = SimpleNamespace(
            guild=SimpleNamespace(id=5678),
            send=AsyncMock(),
        )
        bot = SimpleNamespace(
            _raffle_contribution_channel=None,
            _config=SimpleNamespace(discord_command_guild_id=5678),
            fetch_channel=AsyncMock(return_value=channel),
        )

        await Gw2Bot._send_raffle_contribution_message(
            cast(Gw2Bot, bot),
            "first",
        )
        await Gw2Bot._send_raffle_contribution_message(
            cast(Gw2Bot, bot),
            "second",
        )

        bot.fetch_channel.assert_awaited_once_with(RAFFLE_CONTRIBUTION_CHANNEL_ID)
        assert channel.send.await_args_list == [call("first"), call("second")]

    @patch("gw2bot.main.raffle_contribution_report_end")
    @patch("gw2bot.main.seconds_until_raffle_contribution_report", return_value=123)
    @patch("gw2bot.main.asyncio.sleep", new_callable=AsyncMock)
    async def test_poller_refreshes_guild_log_at_scheduled_boundary(
        self,
        sleep: AsyncMock,
        seconds_until_report: MagicMock,
        report_end: MagicMock,
    ) -> None:
        boundary = datetime(2026, 6, 7, 6, tzinfo=UTC)
        report_end.return_value = boundary
        bot = SimpleNamespace(
            wait_until_ready=AsyncMock(),
            is_closed=MagicMock(side_effect=[False, False, True]),
            refresh_guild_log=AsyncMock(),
            _send_raffle_contribution_report=AsyncMock(),
            _handle_poll_error=AsyncMock(),
            _handle_poll_success=AsyncMock(),
        )

        await Gw2Bot._poll_raffle_contributions(bot)  # type: ignore[arg-type]

        sleep.assert_awaited_once_with(123)
        bot.refresh_guild_log.assert_awaited_once()
        bot._send_raffle_contribution_report.assert_awaited_once_with(boundary)
        bot._handle_poll_success.assert_awaited_once_with("Raffle Contributions")
        bot._handle_poll_error.assert_not_awaited()

    async def test_report_failure_does_not_log_credentials(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        secret = "raffle-report-secret"
        bot = SimpleNamespace(
            _config=SimpleNamespace(
                gw2_api_key=secret,
                discord_token="discord-secret",
            ),
            _last_errors={},
            _try_send_notification=AsyncMock(return_value=True),
        )

        with caplog.at_level(logging.WARNING, logger="gw2bot.main"):
            await Gw2Bot._handle_poll_error(
                cast(Gw2Bot, bot),
                "Raffle Contributions",
                aiohttp.ClientError(f"request failed with access_token={secret}"),
            )

        assert secret not in caplog.text
        bot._try_send_notification.assert_awaited_once_with(
            "Raffle Contributions polling failed: "
            "request failed with access_token=[REDACTED]"
        )


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


def raffle_total(
    username: str,
    *,
    purchased: int = 0,
    free: int = 0,
) -> RaffleTotal:
    return RaffleTotal(
        username=username,
        coins_deposited=purchased * 10_000,
        raffle_tickets=purchased + free,
        gold_raffle_tickets=purchased,
        manual_raffle_tickets=free,
    )
