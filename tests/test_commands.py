import tempfile
import logging
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Protocol, cast
from unittest.mock import AsyncMock, MagicMock, call, patch

import aiohttp
import discord
import pytest
from discord import app_commands

from gw2bot.config import Config
from gw2bot.guild_members import TrialMemberReportEntry
from gw2bot.main import (
    GUILD_MEMBER_COUNT_TOPIC_UPDATE_SECONDS,
    RAFFLE_ADDTICKET_ROLE_ID,
    RAFFLE_CONTRIBUTION_CHANNEL_ID,
    RAFFLE_DRAW_ROLE_ID,
    RAFFLE_OFFICER_ROLE_ID,
    SUNBORNE_ROLE_ID,
    TRIAL_FORUM_CHANNEL_ID,
    TRIAL_IN_REVIEW_TAG_ID,
    TRIAL_ROLE_ID,
    Gw2Bot,
    RaffleAccountLinkModal,
    RaffleBulkAddTicketsModal,
    RaffleContributionReportView,
    RedactingFormatter,
    RaffleCommands,
    RaffleTicketsListView,
    configure_logging,
    count_active_guild_members,
    format_addticket_audit,
    format_automated_message_diagnostics,
    format_bulk_addtickets_summary,
    format_guild_member_count_topic,
    format_removetickets_audit,
    format_raffle_milestone_preview,
    format_raffle_result,
    raffle_contribution_report_embed,
    raffle_contribution_report_end,
    raffle_ticket_embed,
    raffle_ticket_list_embed,
    raffle_tier_summary_embed,
    main as run_main,
    parse_squad_attendance_usernames,
    redact_log_text,
    seconds_until_raffle_contribution_report,
    user_has_role,
)
from gw2bot.raffle import RaffleContribution, RaffleTotal


class AddTicketsCallback(Protocol):
    async def __call__(
        self,
        group: RaffleCommands,
        interaction: discord.Interaction,
        username1: str | None = None,
        username2: str | None = None,
        username3: str | None = None,
        username4: str | None = None,
        username5: str | None = None,
        username6: str | None = None,
        username7: str | None = None,
        username8: str | None = None,
        username9: str | None = None,
        username10: str | None = None,
    ) -> None: ...


class TrialForumTaggingBot(SimpleNamespace):
    async def _resolve_trial_forum_tags(
        self,
        thread: discord.Thread,
        tag_ids: set[int],
    ) -> dict[int, discord.ForumTag]:
        return await Gw2Bot._resolve_trial_forum_tags(
            cast(Gw2Bot, self),
            thread,
            tag_ids,
        )


class GuildMemberCountTopicBot(SimpleNamespace):
    async def _try_update_logging_channel_topic(self, topic: str) -> bool:
        return await Gw2Bot._try_update_logging_channel_topic(
            cast(Gw2Bot, self),
            topic,
        )

    async def _get_notification_channel(self) -> Any:
        return await Gw2Bot._get_notification_channel(cast(Gw2Bot, self))


class TestCommand:
    def test_registers_raffle_command_group(self) -> None:
        group = RaffleCommands(object())  # type: ignore[arg-type]
        commands = {command.name: command for command in group.commands}

        assert group.name == "raffle"
        assert group.guild_only
        assert set(commands) == {
            "draw",
            "addticket",
            "addtickets",
            "bulkaddtickets",
            "removetickets",
            "tickets",
            "list",
        }
        assert "tickets-list" not in commands
        assert "win" not in commands
        addticket = commands["addticket"]
        assert isinstance(addticket, app_commands.Command)
        assert [parameter.name for parameter in addticket.parameters] == [
            "username",
            "amount",
        ]
        assert addticket.parameters[0].autocomplete
        assert not addticket.parameters[1].required
        addtickets = commands["addtickets"]
        assert isinstance(addtickets, app_commands.Command)
        assert [parameter.name for parameter in addtickets.parameters] == [
            f"username{index}" for index in range(1, 11)
        ]
        assert all(parameter.autocomplete for parameter in addtickets.parameters)
        assert not any(parameter.required for parameter in addtickets.parameters)
        bulkaddtickets = commands["bulkaddtickets"]
        assert isinstance(bulkaddtickets, app_commands.Command)
        assert not bulkaddtickets.parameters
        tickets = commands["tickets"]
        assert isinstance(tickets, app_commands.Command)
        assert [parameter.name for parameter in tickets.parameters] == ["username"]
        removetickets = commands["removetickets"]
        assert isinstance(removetickets, app_commands.Command)
        assert [parameter.name for parameter in removetickets.parameters] == [
            "username",
            "amount",
        ]

    def test_checks_required_raffle_roles(self) -> None:
        draw_user = SimpleNamespace(roles=[SimpleNamespace(id=RAFFLE_DRAW_ROLE_ID)])
        add_user = SimpleNamespace(roles=[SimpleNamespace(id=RAFFLE_ADDTICKET_ROLE_ID)])
        officer_user = SimpleNamespace(
            roles=[SimpleNamespace(id=RAFFLE_OFFICER_ROLE_ID)]
        )
        no_roles_user = SimpleNamespace()

        assert user_has_role(draw_user, RAFFLE_DRAW_ROLE_ID)
        assert not user_has_role(draw_user, RAFFLE_ADDTICKET_ROLE_ID)
        assert user_has_role(add_user, RAFFLE_ADDTICKET_ROLE_ID)
        assert user_has_role(officer_user, RAFFLE_OFFICER_ROLE_ID)
        assert not user_has_role(no_roles_user, RAFFLE_DRAW_ROLE_ID)

    def test_formats_addticket_audit_with_discord_mention(self) -> None:
        assert (
            format_addticket_audit(123456789, "Username.1234")
            == "<@123456789> added 1 raffle ticket to Username.1234."
        )

    def test_formats_removetickets_audit_with_discord_mention(self) -> None:
        assert (
            format_removetickets_audit(123456789, "Username.1234", 2)
            == "<@123456789> removed 2 purchased raffle tickets "
            "from Username.1234."
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

    async def test_forum_failure_logging_omits_raw_exception_body(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        class DiscordFailure(discord.DiscordException):
            status = 403
            code = 50001

            def __str__(self) -> str:
                return "raw-response-body-secret"

        bot = SimpleNamespace(fetch_channel=AsyncMock(side_effect=DiscordFailure()))
        with caplog.at_level(logging.ERROR, logger="gw2bot.main"):
            entries = await Gw2Bot._resolve_trial_member_discord_statuses(
                cast(Gw2Bot, bot), ["User.1234"]
            )

        assert entries == [TrialMemberReportEntry("User.1234")]
        assert "raw-response-body-secret" not in caplog.text
        assert "type=DiscordFailure status=403 code=50001" in caplog.text

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


class TestRaffleGuildMemberAutocomplete:
    async def test_returns_matching_guild_members_for_authorized_user(self) -> None:
        bot = SimpleNamespace(
            search_guild_members=AsyncMock(
                return_value=["Member One.1234", "Member Two.5678"]
            )
        )
        interaction = SimpleNamespace(
            user=SimpleNamespace(
                roles=[SimpleNamespace(id=RAFFLE_ADDTICKET_ROLE_ID)]
            )
        )
        group = RaffleCommands(bot)  # type: ignore[arg-type]

        choices = await group.guild_member_autocomplete(
            interaction,  # type: ignore[arg-type]
            "member",
        )

        bot.search_guild_members.assert_awaited_once_with("member", limit=25)
        assert [(choice.name, choice.value) for choice in choices] == [
            ("Member One.1234", "Member One.1234"),
            ("Member Two.5678", "Member Two.5678"),
        ]

    async def test_does_not_expose_guild_members_to_unauthorized_user(self) -> None:
        bot = SimpleNamespace(search_guild_members=AsyncMock())
        interaction = SimpleNamespace(user=SimpleNamespace(roles=[]))
        group = RaffleCommands(bot)  # type: ignore[arg-type]

        choices = await group.guild_member_autocomplete(
            interaction,  # type: ignore[arg-type]
            "member",
        )

        assert choices == []
        bot.search_guild_members.assert_not_awaited()

    async def test_returns_matching_guild_members_for_officer(self) -> None:
        bot = SimpleNamespace(
            search_guild_members=AsyncMock(return_value=["Member.1234"])
        )
        interaction = SimpleNamespace(
            user=SimpleNamespace(
                roles=[SimpleNamespace(id=RAFFLE_OFFICER_ROLE_ID)]
            )
        )
        group = RaffleCommands(bot)  # type: ignore[arg-type]

        choices = await group.guild_member_autocomplete(
            interaction,  # type: ignore[arg-type]
            "member",
        )

        assert [choice.value for choice in choices] == ["Member.1234"]

    async def test_failure_logging_omits_secret_bearing_exception(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        secret = "autocomplete-failure-secret"
        bot = SimpleNamespace(
            search_guild_members=AsyncMock(
                side_effect=aiohttp.ClientError(
                    f"request failed with access_token={secret}"
                )
            )
        )
        interaction = SimpleNamespace(
            user=SimpleNamespace(
                roles=[SimpleNamespace(id=RAFFLE_ADDTICKET_ROLE_ID)]
            )
        )
        group = RaffleCommands(bot)  # type: ignore[arg-type]

        with caplog.at_level(logging.ERROR, logger="gw2bot.main"):
            choices = await group.guild_member_autocomplete(
                interaction,  # type: ignore[arg-type]
                "member",
            )

        assert choices == []
        assert secret not in caplog.text
        assert "Could not refresh the guild member cache for autocomplete" in caplog.text


class TestAddRaffleTicketsCommand:
    async def test_addticket_without_amount_adds_one_manual_ticket(self) -> None:
        total = raffle_total("Member.1234", free=1)
        bot = SimpleNamespace(
            authorize_raffle_command=AsyncMock(return_value=True),
            resolve_guild_member=AsyncMock(return_value=total.username),
            add_manual_raffle_ticket=MagicMock(return_value=total),
            send_notification=AsyncMock(return_value=True),
        )
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=1234),
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        group = RaffleCommands(bot)  # type: ignore[arg-type]
        command = next(
            command for command in group.commands if command.name == "addticket"
        )

        await command.callback(group, interaction, "member.1234")  # type: ignore[arg-type]

        bot.authorize_raffle_command.assert_awaited_once_with(
            interaction,
            RAFFLE_ADDTICKET_ROLE_ID,
        )
        bot.add_manual_raffle_ticket.assert_called_once_with("Member.1234")
        assert "Added one raffle ticket" in interaction.followup.send.await_args.args[0]

    async def test_officer_amount_records_purchased_ticket_event(self) -> None:
        total = raffle_total("Member.1234", purchased=4, free=1)
        bot = SimpleNamespace(
            authorize_raffle_command=AsyncMock(return_value=True),
            resolve_guild_member=AsyncMock(return_value=total.username),
            add_officer_raffle_purchase=AsyncMock(return_value=total),
            add_manual_raffle_ticket=MagicMock(),
        )
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=1234),
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        group = RaffleCommands(bot)  # type: ignore[arg-type]
        command = next(
            command for command in group.commands if command.name == "addticket"
        )

        await command.callback(group, interaction, "member.1234", 4)  # type: ignore[arg-type]

        bot.authorize_raffle_command.assert_awaited_once_with(
            interaction,
            RAFFLE_OFFICER_ROLE_ID,
        )
        bot.add_officer_raffle_purchase.assert_awaited_once_with(
            "Member.1234",
            4,
        )
        bot.add_manual_raffle_ticket.assert_not_called()
        interaction.followup.send.assert_awaited_once_with(
            "Recorded **4 gold** deposited by **Member.1234** and added "
            "4 purchased raffle tickets. They now have 4 purchased and "
            "5 total current tickets.",
            ephemeral=True,
        )

    async def test_amount_requires_officer_role(self) -> None:
        bot = SimpleNamespace(
            authorize_raffle_command=AsyncMock(return_value=False),
            resolve_guild_member=AsyncMock(),
            add_officer_raffle_purchase=AsyncMock(),
        )
        interaction = SimpleNamespace()
        group = RaffleCommands(bot)  # type: ignore[arg-type]
        command = next(
            command for command in group.commands if command.name == "addticket"
        )

        await command.callback(group, interaction, "member.1234", 2)  # type: ignore[arg-type]

        bot.authorize_raffle_command.assert_awaited_once_with(
            interaction,
            RAFFLE_OFFICER_ROLE_ID,
        )
        bot.resolve_guild_member.assert_not_awaited()
        bot.add_officer_raffle_purchase.assert_not_awaited()

    async def test_rejects_officer_purchase_over_cap(self) -> None:
        bot = SimpleNamespace(
            authorize_raffle_command=AsyncMock(return_value=True),
            resolve_guild_member=AsyncMock(return_value="Member.1234"),
            add_officer_raffle_purchase=AsyncMock(
                side_effect=ValueError(
                    "Adding 3 purchased raffle tickets would put Member.1234 "
                    "over the maximum of 10. They currently have 8 purchased "
                    "tickets."
                )
            ),
        )
        interaction = SimpleNamespace(
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        group = RaffleCommands(bot)  # type: ignore[arg-type]
        command = next(
            command for command in group.commands if command.name == "addticket"
        )

        await command.callback(group, interaction, "member.1234", 3)  # type: ignore[arg-type]

        interaction.followup.send.assert_awaited_once_with(
            "Adding 3 purchased raffle tickets would put Member.1234 over "
            "the maximum of 10. They currently have 8 purchased tickets.",
            ephemeral=True,
        )

    def test_parses_squad_attendance_account_names(self) -> None:
        attendance = (
            ":Shadowgopher.8015, Merys Braun\n"
            ":PsycoPrinny.6781, Ivalera Vandimion\n"
            "\n"
            ":Runts.9704, Maldorfic"
        )

        assert parse_squad_attendance_usernames(attendance) == [
            "Shadowgopher.8015",
            "PsycoPrinny.6781",
            "Runts.9704",
        ]

    def test_attendance_parser_logging_omits_pasted_content(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        secret = "attendance-content-secret.1234"

        with caplog.at_level(logging.DEBUG, logger="gw2bot.main"):
            parse_squad_attendance_usernames(
                f":{secret}, Character Name\n:, Missing Account"
            )

        assert secret not in caplog.text
        assert (
            "Parsed squad attendance text; characters="
            in caplog.text
        )
        assert "usernames=1 skipped_lines=1" in caplog.text

    async def test_bulk_attendance_command_opens_paragraph_modal(self) -> None:
        bot = SimpleNamespace(
            authorize_raffle_command=AsyncMock(return_value=True),
        )
        interaction = SimpleNamespace(
            response=SimpleNamespace(send_modal=AsyncMock()),
        )
        group = RaffleCommands(bot)  # type: ignore[arg-type]
        command = next(
            command
            for command in group.commands
            if command.name == "bulkaddtickets"
        )

        await command.callback(group, interaction)  # type: ignore[arg-type]

        bot.authorize_raffle_command.assert_awaited_once_with(
            interaction,
            RAFFLE_ADDTICKET_ROLE_ID,
        )
        modal = interaction.response.send_modal.await_args.args[0]
        assert isinstance(modal, RaffleBulkAddTicketsModal)
        assert modal.attendance.style is discord.TextStyle.paragraph
        assert modal.attendance.max_length == 4_000
        assert modal.attendance.placeholder == ":Username.1234, Character Name"

    async def test_bulk_attendance_modal_adds_parsed_unique_members(self) -> None:
        bot = SimpleNamespace(
            authorize_raffle_command=AsyncMock(return_value=True),
            resolve_guild_member=AsyncMock(
                side_effect=[
                    "Shadowgopher.8015",
                    "PsycoPrinny.6781",
                    "Shadowgopher.8015",
                ]
            ),
            add_manual_raffle_ticket=MagicMock(),
            send_notification=AsyncMock(return_value=True),
        )
        group = RaffleCommands(bot)  # type: ignore[arg-type]
        modal = RaffleBulkAddTicketsModal(group)
        modal.attendance._value = (
            ":Shadowgopher.8015, Merys Braun\n"
            ":PsycoPrinny.6781, Ivalera Vandimion\n"
            ":shadowgopher.8015, Another Character"
        )
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=1234),
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

        await modal.on_submit(interaction)  # type: ignore[arg-type]

        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        assert bot.resolve_guild_member.await_count == 3
        assert bot.add_manual_raffle_ticket.call_args_list == [
            call("Shadowgopher.8015"),
            call("PsycoPrinny.6781"),
        ]
        assert bot.send_notification.await_count == 2
        message = interaction.followup.send.await_args.args[0]
        assert "Added one raffle ticket to 2 guild members." in message
        assert "Duplicate selections skipped: **Shadowgopher.8015**" in message

    async def test_bulk_attendance_modal_rechecks_authorization(self) -> None:
        bot = SimpleNamespace(
            authorize_raffle_command=AsyncMock(return_value=False),
            resolve_guild_member=AsyncMock(),
        )
        group = RaffleCommands(bot)  # type: ignore[arg-type]
        modal = RaffleBulkAddTicketsModal(group)
        modal.attendance._value = ":Member.1234, Character Name"
        interaction = SimpleNamespace(
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

        await modal.on_submit(interaction)  # type: ignore[arg-type]

        bot.authorize_raffle_command.assert_awaited_once_with(
            interaction,
            RAFFLE_ADDTICKET_ROLE_ID,
        )
        interaction.response.defer.assert_not_awaited()
        bot.resolve_guild_member.assert_not_awaited()

    async def test_bulk_attendance_modal_rejects_empty_parsed_input(self) -> None:
        bot = SimpleNamespace(
            authorize_raffle_command=AsyncMock(return_value=True),
            resolve_guild_member=AsyncMock(),
        )
        group = RaffleCommands(bot)  # type: ignore[arg-type]
        modal = RaffleBulkAddTicketsModal(group)
        modal.attendance._value = "\n:, Missing Account\n"
        interaction = SimpleNamespace(
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

        await modal.on_submit(interaction)  # type: ignore[arg-type]

        bot.resolve_guild_member.assert_not_awaited()
        interaction.followup.send.assert_awaited_once_with(
            "No GW2 account names were found in the pasted attendance text.",
            ephemeral=True,
        )

    async def test_adds_valid_unique_members_and_reports_all_other_results(
        self,
    ) -> None:
        bot = SimpleNamespace(
            authorize_raffle_command=AsyncMock(return_value=True),
            resolve_guild_member=AsyncMock(
                side_effect=[
                    "Alpha.1234",
                    "Alpha.1234",
                    None,
                    "Existing.5678",
                    "Beta.9012",
                ]
            ),
            add_manual_raffle_ticket=MagicMock(
                side_effect=[
                    raffle_total("Alpha.1234", free=1),
                    ValueError(
                        "Existing.5678 already has the maximum of 1 manual raffle ticket"
                    ),
                    raffle_total("Beta.9012", free=1),
                ]
            ),
            send_notification=AsyncMock(side_effect=[True, False]),
        )
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=1234),
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        group = RaffleCommands(bot)  # type: ignore[arg-type]
        command = next(
            command for command in group.commands if command.name == "addtickets"
        )
        assert isinstance(command, app_commands.Command)
        callback = cast(AddTicketsCallback, command.callback)

        await callback(
            group,
            cast(discord.Interaction, interaction),
            "alpha.1234",
            "ALPHA.1234",
            "outside.3456",
            "existing.5678",
            "beta.9012",
        )

        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        assert bot.resolve_guild_member.await_count == 5
        assert bot.add_manual_raffle_ticket.call_args_list == [
            call("Alpha.1234"),
            call("Existing.5678"),
            call("Beta.9012"),
        ]
        assert bot.send_notification.await_args_list == [
            call("<@1234> added 1 raffle ticket to Alpha.1234."),
            call("<@1234> added 1 raffle ticket to Beta.9012."),
        ]
        message = interaction.followup.send.await_args.args[0]
        assert "Added one raffle ticket to 2 guild members." in message
        assert "Added: **Alpha.1234**, **Beta.9012**" in message
        assert "Not in the configured guild: 1" in message
        assert "Duplicate selections skipped: **Alpha.1234**" in message
        assert "Could not add: **Existing.5678**" in message
        assert "1 audit delivery failed." in message
        assert interaction.followup.send.await_args.kwargs["ephemeral"]

    def test_bulk_summary_is_bounded_for_long_values(self) -> None:
        long_name = "x" * 5_000

        message = format_bulk_addtickets_summary(
            [long_name] * 100,
            100,
            [long_name] * 100,
            [long_name] * 100,
            100,
        )

        assert len(message) <= 2_000
        assert long_name not in message
        assert "(+90 more)" in message

    async def test_cache_failure_adds_no_tickets_and_omits_exception_secret(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        secret = "bulk-member-cache-secret"
        bot = SimpleNamespace(
            authorize_raffle_command=AsyncMock(return_value=True),
            resolve_guild_member=AsyncMock(
                side_effect=aiohttp.ClientError(
                    f"request failed with access_token={secret}"
                )
            ),
            add_manual_raffle_ticket=MagicMock(),
        )
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=1234),
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        group = RaffleCommands(bot)  # type: ignore[arg-type]
        command = next(
            command for command in group.commands if command.name == "addtickets"
        )
        assert isinstance(command, app_commands.Command)
        callback = cast(AddTicketsCallback, command.callback)

        with caplog.at_level(logging.ERROR, logger="gw2bot.main"):
            await callback(
                group,
                cast(discord.Interaction, interaction),
                "member.1234",
            )

        bot.add_manual_raffle_ticket.assert_not_called()
        assert secret not in caplog.text
        interaction.followup.send.assert_awaited_once_with(
            "Could not verify guild membership. No tickets were added. "
            "Try again later.",
            ephemeral=True,
        )

    async def test_requires_at_least_one_selection(self) -> None:
        bot = SimpleNamespace(
            authorize_raffle_command=AsyncMock(return_value=True),
        )
        interaction = SimpleNamespace(
            response=SimpleNamespace(send_message=AsyncMock()),
        )
        group = RaffleCommands(bot)  # type: ignore[arg-type]
        command = next(
            command for command in group.commands if command.name == "addtickets"
        )
        assert isinstance(command, app_commands.Command)
        callback = cast(AddTicketsCallback, command.callback)

        await callback(
            group,
            cast(discord.Interaction, interaction),
        )

        interaction.response.send_message.assert_awaited_once_with(
            "Select at least one guild member.",
            ephemeral=True,
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
            raffle_total(f"Member {index:02d}.1234", purchased=1)
            for index in range(10)
        ]
        await tickets_list.callback(group, interaction)  # type: ignore[arg-type]
        assert "view" not in interaction.response.send_message.await_args.kwargs
        embeds = interaction.response.send_message.await_args.kwargs["embeds"]
        assert [embed.title for embed in embeds] == [
            "Raffle Tier Summary",
            "Raffle Tickets",
        ]

        interaction.response.send_message.reset_mock()
        bot.get_raffle_totals.return_value = [
            raffle_total(f"Member {index:02d}.1234", purchased=1)
            for index in range(11)
        ]
        await tickets_list.callback(group, interaction)  # type: ignore[arg-type]
        view = interaction.response.send_message.await_args.kwargs["view"]
        assert isinstance(view, RaffleTicketsListView)
        assert len(view.children) == 2

    async def test_list_paginates_ten_players_at_a_time(self) -> None:
        totals = [
            raffle_total(f"Member {index:02d}.1234", purchased=index + 1)
            for index in range(11)
        ]
        totals.append(raffle_total("Former Participant.9999"))
        first_embed = raffle_ticket_list_embed(totals, 0)
        view = RaffleTicketsListView(totals)
        interaction = SimpleNamespace(
            response=SimpleNamespace(edit_message=AsyncMock()),
        )

        assert "Member 10.1234" in (first_embed.description or "")
        assert "Member 01.1234" in (first_embed.description or "")
        assert "Member 00.1234" not in (first_embed.description or "")
        assert "Former Participant.9999" not in (first_embed.description or "")
        assert (
            "**Member 10.1234**\nPurchased: 11\nFree: 0\nTotal: 11"
            in (first_embed.description or "")
        )
        assert (
            "**Member 01.1234**\nPurchased: 2\nFree: 0\nTotal: 2"
            in (first_embed.description or "")
        )
        assert "```" not in (first_embed.description or "")

        await view.change_page(interaction, 1)  # type: ignore[arg-type]

        page_embeds = interaction.response.edit_message.await_args.kwargs["embeds"]
        assert page_embeds[0].title == "Raffle Tier Summary"
        second_embed = page_embeds[1]
        assert "Member 00.1234" in (second_embed.description or "")
        assert "Member 01.1234" not in (second_embed.description or "")
        assert (
            second_embed.description
            == "**Member 00.1234**\nPurchased: 1\nFree: 0\nTotal: 1"
        )

    async def test_list_omits_retained_zero_ticket_records(self) -> None:
        bot = SimpleNamespace(
            get_raffle_totals=MagicMock(
                return_value=[
                    raffle_total(f"Former Member {index:02d}.1234")
                    for index in range(15)
                ]
            )
        )
        group = RaffleCommands(bot)  # type: ignore[arg-type]
        tickets_list = next(
            command for command in group.commands if command.name == "list"
        )
        interaction = SimpleNamespace(
            response=SimpleNamespace(send_message=AsyncMock()),
        )

        await tickets_list.callback(group, interaction)  # type: ignore[arg-type]

        kwargs = interaction.response.send_message.await_args.kwargs
        assert "view" not in kwargs
        assert (
            kwargs["embeds"][1].description
            == "No players currently have raffle tickets."
        )
        assert [field.value for field in kwargs["embeds"][0].fields] == [
            "No tier reached",
            "0",
            "50",
        ]

    def test_list_orders_total_descending_then_username_case_insensitively(
        self,
    ) -> None:
        totals = [
            raffle_total("Zulu.1234", purchased=2),
            raffle_total("alpha.1234", purchased=2),
            raffle_total("Lowest.1234", purchased=1),
            raffle_total("Highest.1234", purchased=3),
            raffle_total("Beta.1234", purchased=2),
        ]

        description = raffle_ticket_list_embed(totals, 0).description or ""

        positions = [
            description.index(name)
            for name in (
                "Highest.1234",
                "alpha.1234",
                "Beta.1234",
                "Zulu.1234",
                "Lowest.1234",
            )
        ]
        assert positions == sorted(positions)

    def test_list_ordering_logs_counts_without_account_names(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        secret = "list-account-secret"

        with caplog.at_level(logging.DEBUG, logger="gw2bot.main"):
            raffle_ticket_list_embed(
                [raffle_total(f"{secret}.1234", purchased=1)],
                0,
            )

        assert secret not in caplog.text
        assert (
            "Ordered raffle totals for display; records=1 active_players=1"
            in caplog.text
        )
        assert (
            "Rendering raffle ticket list page; page=1 page_count=1 players=1"
            in caplog.text
        )

    def test_tier_summary_uses_purchased_tickets_and_reports_next_tier(
        self,
    ) -> None:
        embed = raffle_tier_summary_embed(
            [
                raffle_total("Buyer A.1234", purchased=50, free=10),
                raffle_total("Buyer B.5678", purchased=25, free=10),
            ]
        )

        assert [field.name for field in embed.fields] == [
            "Current Tier",
            "Total Tickets Purchased",
            "Tickets Until Next Tier",
        ]
        assert [field.value for field in embed.fields] == [
            "Tier 1",
            "75",
            "25",
        ]

    def test_tier_summary_handles_no_reached_tier_and_highest_tier(self) -> None:
        below_first = raffle_tier_summary_embed(
            [raffle_total("Member.1234", purchased=49, free=100)]
        )
        highest = raffle_tier_summary_embed(
            [raffle_total("Member.1234", purchased=200)]
        )

        assert [field.value for field in below_first.fields] == [
            "No tier reached",
            "49",
            "1",
        ]
        assert [field.value for field in highest.fields] == [
            "Tier 4",
            "200",
            "0 (highest tier reached)",
        ]

    def test_tier_summary_logging_omits_account_names(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        secret = "tier-summary-account-secret"

        with caplog.at_level(logging.DEBUG, logger="gw2bot.main"):
            raffle_tier_summary_embed(
                [raffle_total(f"{secret}.1234", purchased=75)]
            )

        assert secret not in caplog.text
        assert (
            "Rendered raffle tier summary; purchased_tickets=75 "
            "current_tier_reached=True next_tier_exists=True"
            in caplog.text
        )

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
                        winners=(
                            SimpleNamespace(username="Winner A.1234"),
                            SimpleNamespace(username="Winner B.5678"),
                        ),
                        total_tickets=10,
                        purchased_tickets=8,
                        free_tickets=2,
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
            "Raffle winners:\n"
            "1. **Winner A.1234**\n"
            "2. **Winner B.5678**\n"
            "Selected 2 winners from 8 purchased tickets and 2 free tickets. "
            "All current raffle tickets have been reset."
        )
        bot.mark_raffle_announcement_sent.assert_called_once_with(7)

    async def test_retries_pending_announcement_without_refreshing_or_redrawing(
        self,
    ) -> None:
        pending = SimpleNamespace(
            run_id=7,
            winners=(SimpleNamespace(username="Winner.1234"),),
            total_tickets=10,
            purchased_tickets=9,
            free_tickets=1,
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
                    winners=(SimpleNamespace(username="Winner.1234"),),
                    total_tickets=10,
                    purchased_tickets=10,
                    free_tickets=0,
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

    def test_formats_repeat_winners_in_draw_order(self) -> None:
        result = SimpleNamespace(
            winners=(
                SimpleNamespace(username="Repeat.1234"),
                SimpleNamespace(username="Other.5678"),
                SimpleNamespace(username="Repeat.1234"),
            ),
            total_tickets=20,
            purchased_tickets=17,
            free_tickets=3,
        )

        message = format_raffle_result(result)  # type: ignore[arg-type]

        assert message.startswith(
            "Raffle winners:\n"
            "1. **Repeat.1234**\n"
            "2. **Other.5678**\n"
            "3. **Repeat.1234**\n"
        )

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


class TestRemoveRaffleTicketsCommand:
    async def test_officer_removes_purchased_tickets_with_default_amount(
        self,
    ) -> None:
        total = raffle_total("Member.1234", purchased=2, free=1)
        bot = SimpleNamespace(
            authorize_raffle_command=AsyncMock(return_value=True),
            resolve_guild_member=AsyncMock(return_value=total.username),
            remove_gold_raffle_tickets=MagicMock(return_value=total),
            send_notification=AsyncMock(return_value=True),
        )
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=1234),
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        group = RaffleCommands(bot)  # type: ignore[arg-type]
        command = next(
            command
            for command in group.commands
            if command.name == "removetickets"
        )

        await command.callback(group, interaction, "member.1234")  # type: ignore[arg-type]

        bot.authorize_raffle_command.assert_awaited_once_with(
            interaction,
            RAFFLE_DRAW_ROLE_ID,
        )
        bot.remove_gold_raffle_tickets.assert_called_once_with(
            "Member.1234",
            1,
        )
        bot.send_notification.assert_awaited_once_with(
            "<@1234> removed 1 purchased raffle ticket from Member.1234."
        )
        interaction.followup.send.assert_awaited_once_with(
            "Removed 1 purchased raffle ticket from **Member.1234**. "
            "They now have 2 purchased and 3 total current tickets.",
            ephemeral=True,
        )

    async def test_rejects_excess_ticket_removal_without_audit(self) -> None:
        bot = SimpleNamespace(
            authorize_raffle_command=AsyncMock(return_value=True),
            resolve_guild_member=AsyncMock(return_value="Member.1234"),
            remove_gold_raffle_tickets=MagicMock(
                side_effect=ValueError(
                    "Member.1234 has only 1 purchased raffle ticket"
                )
            ),
            send_notification=AsyncMock(),
        )
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=1234),
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        group = RaffleCommands(bot)  # type: ignore[arg-type]
        command = next(
            command
            for command in group.commands
            if command.name == "removetickets"
        )

        await command.callback(group, interaction, "Member.1234", 2)  # type: ignore[arg-type]

        bot.send_notification.assert_not_awaited()
        interaction.followup.send.assert_awaited_once_with(
            "Member.1234 has only 1 purchased raffle ticket",
            ephemeral=True,
        )

    async def test_lookup_failure_does_not_log_secret_bearing_exception(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        secret = "removal-lookup-secret"
        bot = SimpleNamespace(
            authorize_raffle_command=AsyncMock(return_value=True),
            resolve_guild_member=AsyncMock(
                side_effect=aiohttp.ClientError(
                    f"request failed with access_token={secret}"
                )
            ),
        )
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=1234),
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        group = RaffleCommands(bot)  # type: ignore[arg-type]
        command = next(
            command
            for command in group.commands
            if command.name == "removetickets"
        )

        with caplog.at_level(logging.ERROR, logger="gw2bot.main"):
            await command.callback(group, interaction, "Member.1234", 1)  # type: ignore[arg-type]

        assert secret not in caplog.text
        assert "Could not refresh the guild member cache" in caplog.text
        interaction.followup.send.assert_awaited_once_with(
            "Could not verify guild membership. Try again later.",
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
        assert bot.intents.guild_messages
        assert not bot.intents.members
        assert bot.intents.message_content
        raffle_store.assert_called_once()


class TestTrialForumTagging:
    async def test_applies_in_review_tag_to_new_trial_forum_post(self) -> None:
        existing_tag = SimpleNamespace(id=101)
        in_review_tag = SimpleNamespace(id=TRIAL_IN_REVIEW_TAG_ID)
        forum = SimpleNamespace(available_tags=[existing_tag, in_review_tag])
        thread = SimpleNamespace(
            id=202,
            parent_id=TRIAL_FORUM_CHANNEL_ID,
            parent=forum,
            applied_tags=[existing_tag],
            _applied_tags=[existing_tag.id],
            edit=AsyncMock(),
        )
        bot = TrialForumTaggingBot()

        await Gw2Bot._apply_trial_forum_in_review_tag(
            cast(Gw2Bot, bot),
            cast(discord.Thread, thread),
        )

        thread.edit.assert_awaited_once_with(
            applied_tags=[existing_tag, in_review_tag],
            reason="Automatically apply In Review tag",
        )

    async def test_fetches_forum_tag_when_thread_parent_cache_is_missing(
        self,
    ) -> None:
        in_review_tag = SimpleNamespace(id=TRIAL_IN_REVIEW_TAG_ID)
        forum = SimpleNamespace(available_tags=[in_review_tag])
        thread = SimpleNamespace(
            id=202,
            parent_id=TRIAL_FORUM_CHANNEL_ID,
            parent=None,
            applied_tags=[],
            _applied_tags=[],
            edit=AsyncMock(),
        )
        bot = TrialForumTaggingBot(fetch_channel=AsyncMock(return_value=forum))

        await Gw2Bot._apply_trial_forum_in_review_tag(
            cast(Gw2Bot, bot),
            cast(discord.Thread, thread),
        )

        bot.fetch_channel.assert_awaited_once_with(TRIAL_FORUM_CHANNEL_ID)
        thread.edit.assert_awaited_once_with(
            applied_tags=[in_review_tag],
            reason="Automatically apply In Review tag",
        )

    async def test_skips_threads_outside_trial_forum(self) -> None:
        thread = SimpleNamespace(
            id=202,
            parent_id=999,
            parent=None,
            applied_tags=[],
            _applied_tags=[],
            edit=AsyncMock(),
        )
        bot = SimpleNamespace()

        await Gw2Bot._apply_trial_forum_in_review_tag(
            cast(Gw2Bot, bot),
            cast(discord.Thread, thread),
        )

        thread.edit.assert_not_awaited()

    async def test_skips_thread_that_already_has_in_review_tag(self) -> None:
        in_review_tag = SimpleNamespace(id=TRIAL_IN_REVIEW_TAG_ID)
        thread = SimpleNamespace(
            id=202,
            parent_id=TRIAL_FORUM_CHANNEL_ID,
            parent=None,
            applied_tags=[in_review_tag],
            _applied_tags=[TRIAL_IN_REVIEW_TAG_ID],
            edit=AsyncMock(),
        )
        bot = SimpleNamespace()

        await Gw2Bot._apply_trial_forum_in_review_tag(
            cast(Gw2Bot, bot),
            cast(discord.Thread, thread),
        )

        thread.edit.assert_not_awaited()

    async def test_missing_in_review_tag_is_logged_without_editing(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        forum = SimpleNamespace(available_tags=[])
        thread = SimpleNamespace(
            id=202,
            parent_id=TRIAL_FORUM_CHANNEL_ID,
            parent=forum,
            applied_tags=[],
            _applied_tags=[],
            edit=AsyncMock(),
        )
        bot = TrialForumTaggingBot(fetch_channel=AsyncMock(return_value=forum))

        with caplog.at_level(logging.ERROR, logger="gw2bot.main"):
            await Gw2Bot._apply_trial_forum_in_review_tag(
                cast(Gw2Bot, bot),
                cast(discord.Thread, thread),
            )

        thread.edit.assert_not_awaited()
        assert "tag_id=1317349421821726790 not found" in caplog.text

    async def test_tagging_failure_logging_omits_raw_exception_body(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        secret = "raw-discord-tagging-secret"

        class DiscordFailure(discord.DiscordException):
            status = 403
            code = 50013

            def __str__(self) -> str:
                return secret

        in_review_tag = SimpleNamespace(id=TRIAL_IN_REVIEW_TAG_ID)
        forum = SimpleNamespace(available_tags=[in_review_tag])
        thread = SimpleNamespace(
            id=202,
            parent_id=TRIAL_FORUM_CHANNEL_ID,
            parent=forum,
            applied_tags=[],
            _applied_tags=[],
            edit=AsyncMock(side_effect=DiscordFailure()),
        )
        bot = TrialForumTaggingBot()

        with caplog.at_level(logging.ERROR, logger="gw2bot.main"):
            await Gw2Bot._apply_trial_forum_in_review_tag(
                cast(Gw2Bot, bot),
                cast(discord.Thread, thread),
            )

        assert secret not in caplog.text
        assert "type=DiscordFailure status=403 code=50013" in caplog.text


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
            "150 total tickets have been purchased for this raffle. "
            "Tier 3 rewards have been reached!"
        ) in output
        assert "Guild Storage is low on **Diagnostic Feast**: 5 left" in output
        assert "Trial members past the 14-day mark" in output
        assert (
            "The guild member count has not been retrieved yet, so the "
            "channel description is not set."
        ) in output
        assert "Guild Storage polling failed: API unavailable" in output
        assert "Guild Storage polling recovered." in output

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
            "Prepared automated message diagnostics; messages=9 contributors=1"
            in caplog.text
        )
        assert caplog.text.count("Attempting automated diagnostic delivery") == 10
        assert caplog.text.count("Automated diagnostic delivery succeeded") == 10
        assert (
            "Automated message diagnostics completed; attempted=10 delivered=10 "
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

        assert channel.send.await_count == 10
        assert secret not in caplog.text
        assert (
            "Automated diagnostic delivery failed; kind=contribution-report "
            "error_type=RuntimeError"
            in caplog.text
        )
        assert (
            "Automated message diagnostics completed; attempted=10 delivered=9 "
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
            _handle_poll_error=AsyncMock(),
            _handle_poll_success=AsyncMock(),
        )

        await Gw2Bot._poll_guild_member_count_topic(cast(Gw2Bot, bot))

        bot._update_guild_member_count_topic.assert_awaited_once()
        bot._handle_poll_success.assert_awaited_once_with("Guild Member Count")
        bot._handle_poll_error.assert_not_awaited()
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
            _handle_poll_error=AsyncMock(),
            _handle_poll_success=AsyncMock(),
        )

        await Gw2Bot._poll_guild_member_count_topic(cast(Gw2Bot, bot))

        bot._handle_poll_error.assert_awaited_once_with(
            "Guild Member Count",
            error,
        )
        bot._handle_poll_success.assert_not_awaited()
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
            _handle_poll_error=AsyncMock(),
            _handle_poll_success=AsyncMock(),
            _config=SimpleNamespace(guild_log_poll_interval_seconds=60),
        )

        await Gw2Bot._poll_guild_log(cast(Gw2Bot, bot))

        bot._send_pending_raffle_notifications.assert_awaited_once()
        bot._send_pending_deposit_audit_notifications.assert_awaited_once()
        bot._handle_poll_success.assert_awaited_once_with("Guild Log")
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
            _send_notification=AsyncMock(side_effect=_forbidden_error(50013)),
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

    async def test_paginates_discord_indexed_search_without_reading_thread_history(
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
            side_effect=[
                {"total_results": 26, "messages": [], "threads": []},
                {
                    "total_results": 26,
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
                },
            ]
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
        assert search.await_count == 2
        search_call = search.await_args_list[1]
        assert search_call is not None
        route = search_call.args[0]
        assert route.path == "/guilds/{guild_id}/messages/search"
        assert ("channel_id", str(TRIAL_FORUM_CHANNEL_ID)) in search_call.kwargs["params"]
        assert ("offset", "25") in search_call.kwargs["params"]

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

    async def test_requires_exact_normalized_account_name_match(self) -> None:
        owner = SimpleNamespace(id=777, roles=[SimpleNamespace(id=TRIAL_ROLE_ID)])

        async def messages() -> Any:
            yield SimpleNamespace(content="  OTHERUSER.1234  ", author=owner)

        accepted_thread = SimpleNamespace(
            id=900,
            parent_id=TRIAL_FORUM_CHANNEL_ID,
            owner_id=777,
            owner=owner,
            applied_tags=[SimpleNamespace(name="Accepted")],
            name="OtherUser.1234 application",
            history=lambda **_: messages(),
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
            return_value={"total_results": 0, "messages": [], "threads": []}
        )
        bot = SimpleNamespace(
            fetch_channel=AsyncMock(return_value=forum),
            http=SimpleNamespace(request=search),
        )

        entries = await Gw2Bot._resolve_trial_member_discord_statuses(
            cast(Gw2Bot, bot),
            ["User.1234", "OtherUser.1234"],
        )

        assert entries == [
            TrialMemberReportEntry("User.1234"),
            TrialMemberReportEntry("OtherUser.1234", 777, "Trial"),
        ]

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

    def test_formats_contributors_as_mobile_friendly_blocks(self) -> None:
        contributions = [
            RaffleContribution("Alpha.1234", 2, 1),
            RaffleContribution("Beta.1234", 0, 2),
        ]

        embed = raffle_contribution_report_embed(contributions, 0)
        description = embed.description or ""

        assert embed.title == "Raffle contributions from the last 6 hours"
        assert description == (
            "**Alpha.1234**\n"
            "Purchased: 2\n"
            "Free: 1\n"
            "Total: 3\n\n"
            "**Beta.1234**\n"
            "Purchased: 0\n"
            "Free: 2\n"
            "Total: 2"
        )

    async def test_empty_window_does_not_send_message(self) -> None:
        report_end = datetime(2026, 6, 7, 6, tzinfo=UTC)
        bot = SimpleNamespace(
            get_raffle_contributions=MagicMock(return_value=[]),
            _send_raffle_contribution_embed=AsyncMock(),
        )

        await Gw2Bot._send_raffle_contribution_report(
            cast(Gw2Bot, bot),
            report_end,
        )

        bot.get_raffle_contributions.assert_called_once_with(
            datetime(2026, 6, 7, 0, tzinfo=UTC),
            report_end,
        )
        bot._send_raffle_contribution_embed.assert_not_awaited()

    async def test_free_ticket_only_window_sends_embed(self) -> None:
        report_end = datetime(2026, 6, 7, 6, tzinfo=UTC)
        bot = SimpleNamespace(
            get_raffle_contributions=MagicMock(
                return_value=[RaffleContribution("Free Only.1234", 0, 1)]
            ),
            _send_raffle_contribution_embed=AsyncMock(),
        )

        await Gw2Bot._send_raffle_contribution_report(
            cast(Gw2Bot, bot),
            report_end,
        )

        bot._send_raffle_contribution_embed.assert_awaited_once()
        embed, view = bot._send_raffle_contribution_embed.await_args.args
        assert view is None
        assert (
            embed.description
            == "**Free Only.1234**\nPurchased: 0\nFree: 1\nTotal: 1"
        )

    async def test_contribution_report_paginates_ten_users_at_a_time(self) -> None:
        report_end = datetime(2026, 6, 7, 6, tzinfo=UTC)
        contributions = [
            RaffleContribution(f"Member {index:02d}.1234", index, 0)
            for index in range(11)
        ]
        bot = SimpleNamespace(
            get_raffle_contributions=MagicMock(return_value=contributions),
            _send_raffle_contribution_embed=AsyncMock(),
        )

        await Gw2Bot._send_raffle_contribution_report(
            cast(Gw2Bot, bot),
            report_end,
        )

        embed, view = bot._send_raffle_contribution_embed.await_args.args
        assert isinstance(view, RaffleContributionReportView)
        assert "Member 09.1234" in (embed.description or "")
        assert "Member 10.1234" not in (embed.description or "")

        interaction = SimpleNamespace(
            response=SimpleNamespace(edit_message=AsyncMock()),
        )
        await view.change_page(interaction, 1)  # type: ignore[arg-type]

        second_embed = interaction.response.edit_message.await_args.kwargs["embed"]
        assert "Member 10.1234" in (second_embed.description or "")
        assert "Member 09.1234" not in (second_embed.description or "")

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

    async def test_officer_purchase_attempts_all_purchase_deliveries(self) -> None:
        total = raffle_total("Member.1234", purchased=3)
        store = MagicMock()
        store.add_officer_purchase.return_value = total
        bot = SimpleNamespace(
            _raffle_store=store,
            _send_pending_raffle_notifications=AsyncMock(),
            _send_pending_deposit_audit_notifications=AsyncMock(),
            _send_pending_raffle_milestones=AsyncMock(),
        )

        result = await Gw2Bot.add_officer_raffle_purchase(
            cast(Gw2Bot, bot),
            "Member.1234",
            3,
        )

        assert result == total
        store.add_officer_purchase.assert_called_once_with("Member.1234", 3)
        bot._send_pending_raffle_notifications.assert_awaited_once()
        bot._send_pending_deposit_audit_notifications.assert_awaited_once()
        bot._send_pending_raffle_milestones.assert_awaited_once()

    async def test_sends_pending_deposit_audits_to_notification_channel(self) -> None:
        deposit = SimpleNamespace(event_id=101, message="purchase message")
        store = MagicMock()
        store.get_pending_deposit_audit_notifications.return_value = [deposit]
        bot = SimpleNamespace(
            _raffle_store=store,
            _try_send_notification=AsyncMock(return_value=True),
        )

        await Gw2Bot._send_pending_deposit_audit_notifications(cast(Gw2Bot, bot))

        bot._try_send_notification.assert_awaited_once_with("purchase message")
        store.mark_deposit_audit_notification_sent.assert_called_once_with(101)

    async def test_retries_pending_deposit_audit_after_delivery_failure(self) -> None:
        deposit = SimpleNamespace(event_id=101, message="purchase message")
        store = MagicMock()
        store.get_pending_deposit_audit_notifications.return_value = [deposit]
        bot = SimpleNamespace(
            _raffle_store=store,
            _try_send_notification=AsyncMock(return_value=False),
        )

        await Gw2Bot._send_pending_deposit_audit_notifications(cast(Gw2Bot, bot))

        store.mark_deposit_audit_notification_sent.assert_not_called()

    async def test_sends_pending_join_messages_to_notification_channel(self) -> None:
        join = SimpleNamespace(event_id=101, message="join message")
        store = MagicMock()
        store.get_pending_join_notifications.return_value = [join]
        bot = SimpleNamespace(
            _raffle_store=store,
            _try_send_notification=AsyncMock(return_value=True),
        )

        await Gw2Bot._send_pending_join_notifications(cast(Gw2Bot, bot))

        bot._try_send_notification.assert_awaited_once_with("join message")
        store.mark_join_notification_sent.assert_called_once_with(101)

    async def test_retries_pending_join_after_delivery_failure(self) -> None:
        join = SimpleNamespace(event_id=101, message="join message")
        store = MagicMock()
        store.get_pending_join_notifications.return_value = [join]
        bot = SimpleNamespace(
            _raffle_store=store,
            _try_send_notification=AsyncMock(return_value=False),
        )

        await Gw2Bot._send_pending_join_notifications(cast(Gw2Bot, bot))

        store.mark_join_notification_sent.assert_not_called()

    async def test_sends_pending_milestones_to_raffle_channel(self) -> None:
        milestone = SimpleNamespace(threshold=50, message="milestone message")
        store = MagicMock()
        store.get_pending_milestones.return_value = [milestone]
        bot = SimpleNamespace(
            _raffle_store=store,
            _try_send_raffle_contribution_message=AsyncMock(return_value=True),
        )

        await Gw2Bot._send_pending_raffle_milestones(cast(Gw2Bot, bot))

        bot._try_send_raffle_contribution_message.assert_awaited_once_with(
            "milestone message"
        )
        store.mark_milestone_notification_sent.assert_called_once_with(50)

    async def test_retries_pending_milestone_after_delivery_failure(self) -> None:
        milestone = SimpleNamespace(threshold=50, message="milestone message")
        store = MagicMock()
        store.get_pending_milestones.return_value = [milestone]
        bot = SimpleNamespace(
            _raffle_store=store,
            _try_send_raffle_contribution_message=AsyncMock(return_value=False),
        )

        await Gw2Bot._send_pending_raffle_milestones(cast(Gw2Bot, bot))

        store.mark_milestone_notification_sent.assert_not_called()

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

        async def get_channel() -> Any:
            return await Gw2Bot._get_raffle_contribution_channel(
                cast(Gw2Bot, bot)
            )

        bot._get_raffle_contribution_channel = get_channel

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

    async def test_sends_contribution_embed_with_pagination_view(self) -> None:
        channel = SimpleNamespace(
            guild=SimpleNamespace(id=5678),
            send=AsyncMock(),
        )
        bot = SimpleNamespace(
            _get_raffle_contribution_channel=AsyncMock(return_value=channel),
        )
        embed = discord.Embed(title="Report")
        view = discord.ui.View()

        await Gw2Bot._send_raffle_contribution_embed(
            cast(Gw2Bot, bot),
            embed,
            view,
        )

        channel.send.assert_awaited_once_with(embed=embed, view=view)
        bot._get_raffle_contribution_channel.assert_awaited_once_with()

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

    @patch("gw2bot.main.raffle_contribution_report_end")
    @patch("gw2bot.main.seconds_until_raffle_contribution_report", return_value=123)
    @patch("gw2bot.main.asyncio.sleep", new_callable=AsyncMock)
    async def test_poller_posts_persisted_report_after_refresh_timeout(
        self,
        sleep: AsyncMock,
        seconds_until_report: MagicMock,
        report_end: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        boundary = datetime(2026, 6, 7, 6, tzinfo=UTC)
        report_end.return_value = boundary
        bot = SimpleNamespace(
            wait_until_ready=AsyncMock(),
            is_closed=MagicMock(side_effect=[False, False, True]),
            refresh_guild_log=AsyncMock(side_effect=TimeoutError("secret-timeout")),
            _send_raffle_contribution_report=AsyncMock(),
            _handle_poll_error=AsyncMock(),
            _handle_poll_success=AsyncMock(),
        )

        with caplog.at_level(logging.DEBUG, logger="gw2bot.main"):
            await Gw2Bot._poll_raffle_contributions(bot)  # type: ignore[arg-type]

        sleep.assert_awaited_once_with(123)
        bot.refresh_guild_log.assert_awaited_once()
        bot._send_raffle_contribution_report.assert_awaited_once_with(boundary)
        bot._handle_poll_success.assert_awaited_once_with("Raffle Contributions")
        bot._handle_poll_error.assert_not_awaited()
        assert "secret-timeout" not in caplog.text
        assert (
            "Raffle Contributions guild-log refresh failed; posting persisted "
            "report; error_type=TimeoutError"
            in caplog.text
        )
        assert (
            "Raffle Contributions poll completed successfully; "
            "guild_log_refreshed=False"
            in caplog.text
        )

    @patch("gw2bot.main.raffle_contribution_report_end")
    @patch("gw2bot.main.seconds_until_raffle_contribution_report", return_value=123)
    @patch("gw2bot.main.asyncio.sleep", new_callable=AsyncMock)
    async def test_poller_reports_actual_contribution_delivery_timeout(
        self,
        sleep: AsyncMock,
        seconds_until_report: MagicMock,
        report_end: MagicMock,
    ) -> None:
        boundary = datetime(2026, 6, 7, 6, tzinfo=UTC)
        report_end.return_value = boundary
        error = TimeoutError("Discord unavailable")
        bot = SimpleNamespace(
            wait_until_ready=AsyncMock(),
            is_closed=MagicMock(side_effect=[False, False, True]),
            refresh_guild_log=AsyncMock(),
            _send_raffle_contribution_report=AsyncMock(side_effect=error),
            _handle_poll_error=AsyncMock(),
            _handle_poll_success=AsyncMock(),
        )

        await Gw2Bot._poll_raffle_contributions(bot)  # type: ignore[arg-type]

        sleep.assert_awaited_once_with(123)
        bot._handle_poll_error.assert_awaited_once_with(
            "Raffle Contributions",
            error,
        )
        bot._handle_poll_success.assert_not_awaited()

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
