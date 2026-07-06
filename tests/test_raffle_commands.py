import logging
from types import SimpleNamespace
from typing import Protocol, cast
from unittest.mock import AsyncMock, MagicMock, call

import aiohttp
import discord
import pytest
from discord import app_commands
from sqlalchemy.exc import SQLAlchemyError

from gw2bot.raffle import (
    RaffleAudit,
    RaffleAuditDraw,
    RaffleAuditRange,
    RaffleContribution,
    RaffleRunSummary,
    RaffleWinner,
)
from gw2bot.raffle.commands import (
    GUILD_ROSTER_ROLE_ID,
    RAFFLE_ADDTICKET_ROLE_ID,
    RAFFLE_DRAW_ROLE_ID,
    RAFFLE_OFFICER_ROLE_ID,
    RaffleCommands,
)
from gw2bot.raffle.formatting import (
    RAFFLE_AUDIT_EMBED_FIELD_LIMIT,
    RAFFLE_AUDIT_FIELD_CHAR_LIMIT,
    RAFFLE_AUDIT_VERIFY_FOOTER,
    format_addticket_audit,
    format_bulk_addtickets_summary,
    format_raffle_result,
    format_removetickets_audit,
    format_unknown_raffle_run_message,
    parse_squad_attendance_usernames,
    raffle_audit_embeds,
    raffle_ticket_embed,
    raffle_ticket_list_embed,
    raffle_tier_summary_embed,
)
from gw2bot.raffle.views import (
    RaffleAccountLinkModal,
    RaffleTicketTableView,
    RaffleBulkAddTicketsModal,
    RaffleTicketsListView,
)
from gw2bot.discord_utils import user_has_role

from factories import raffle_total


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


class TestRaffleCommandGroup:
    def test_registers_raffle_command_group(self) -> None:
        group = RaffleCommands(object())  # type: ignore[arg-type]
        commands = {command.name: command for command in group.commands}

        assert group.name == "raffle"
        assert group.guild_only
        assert set(commands) == {
            "draw",
            "audit",
            "addticket",
            "addtickets",
            "bulkaddtickets",
            "removetickets",
            "tickets",
            "list",
            "leaderboard",
        }
        assert "tickets-list" not in commands
        assert "win" not in commands
        audit = commands["audit"]
        assert isinstance(audit, app_commands.Command)
        assert [parameter.name for parameter in audit.parameters] == ["run_id"]
        assert audit.parameters[0].required
        assert audit.parameters[0].autocomplete
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
        assert tickets.parameters[0].autocomplete
        assert not tickets.parameters[0].required
        removetickets = commands["removetickets"]
        assert isinstance(removetickets, app_commands.Command)
        assert [parameter.name for parameter in removetickets.parameters] == [
            "username",
            "amount",
        ]
        leaderboard = commands["leaderboard"]
        assert isinstance(leaderboard, app_commands.Command)
        assert [parameter.name for parameter in leaderboard.parameters] == [
            "sortby"
        ]
        assert not leaderboard.parameters[0].required
        assert [choice.value for choice in leaderboard.parameters[0].choices] == [
            "purchased",
            "free",
            "total",
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


class TestRaffleGuildMemberAutocomplete:
    async def test_returns_matching_guild_members_for_authorized_user(self) -> None:
        bot = SimpleNamespace(
            search_guild_members=AsyncMock(
                return_value=["Member One.1234", "Member Two.5678"]
            )
        )
        interaction = SimpleNamespace(
            user=SimpleNamespace(
                roles=[SimpleNamespace(id=GUILD_ROSTER_ROLE_ID)]
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

    async def test_raffle_roles_alone_do_not_expose_guild_members(self) -> None:
        bot = SimpleNamespace(search_guild_members=AsyncMock())
        interaction = SimpleNamespace(
            user=SimpleNamespace(
                roles=[
                    SimpleNamespace(id=RAFFLE_ADDTICKET_ROLE_ID),
                    SimpleNamespace(id=RAFFLE_OFFICER_ROLE_ID),
                    SimpleNamespace(id=RAFFLE_DRAW_ROLE_ID),
                ]
            )
        )
        group = RaffleCommands(bot)  # type: ignore[arg-type]

        choices = await group.guild_member_autocomplete(
            interaction,  # type: ignore[arg-type]
            "member",
        )

        assert choices == []
        bot.search_guild_members.assert_not_awaited()

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
                roles=[SimpleNamespace(id=GUILD_ROSTER_ROLE_ID)]
            )
        )
        group = RaffleCommands(bot)  # type: ignore[arg-type]

        with caplog.at_level(logging.ERROR, logger="gw2bot"):
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

        with caplog.at_level(logging.DEBUG, logger="gw2bot"):
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

        with caplog.at_level(logging.ERROR, logger="gw2bot"):
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

        with caplog.at_level(logging.ERROR, logger="gw2bot"):
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

    async def test_leaderboard_lists_split_and_paginates(self) -> None:
        bot = SimpleNamespace(
            get_lifetime_raffle_contributions=MagicMock(return_value=[])
        )
        group = RaffleCommands(bot)  # type: ignore[arg-type]
        leaderboard = next(
            command for command in group.commands if command.name == "leaderboard"
        )
        interaction = SimpleNamespace(
            response=SimpleNamespace(send_message=AsyncMock()),
        )

        bot.get_lifetime_raffle_contributions.return_value = [
            RaffleContribution("Member.1234", 3, 1)
        ]
        await leaderboard.callback(group, interaction)  # type: ignore[arg-type]
        assert "view" not in interaction.response.send_message.await_args.kwargs
        embed = interaction.response.send_message.await_args.kwargs["embed"]
        assert embed.title == "Lifetime raffle tickets"
        assert (
            embed.description
            == "**Member.1234**\nPurchased: 3\nFree: 1\nTotal: 4"
        )

        interaction.response.send_message.reset_mock()
        bot.get_lifetime_raffle_contributions.return_value = [
            RaffleContribution(f"Member {index:02d}.1234", index + 1, 0)
            for index in range(11)
        ]
        await leaderboard.callback(group, interaction)  # type: ignore[arg-type]
        view = interaction.response.send_message.await_args.kwargs["view"]
        assert isinstance(view, RaffleTicketTableView)

    async def test_leaderboard_sortby_reorders_rows(self) -> None:
        bot = SimpleNamespace(
            get_lifetime_raffle_contributions=MagicMock(
                return_value=[
                    RaffleContribution("Buyer.1234", 5, 0),
                    RaffleContribution("Earner.5678", 1, 3),
                ]
            )
        )
        group = RaffleCommands(bot)  # type: ignore[arg-type]
        leaderboard = next(
            command for command in group.commands if command.name == "leaderboard"
        )
        interaction = SimpleNamespace(
            response=SimpleNamespace(send_message=AsyncMock()),
        )

        await leaderboard.callback(group, interaction, sortby="free")  # type: ignore[arg-type]

        embed = interaction.response.send_message.await_args.kwargs["embed"]
        assert embed.title == "Lifetime raffle tickets (by free)"
        assert (
            embed.description
            == "**Earner.5678**\nPurchased: 1\nFree: 3\nTotal: 4\n\n"
            "**Buyer.1234**\nPurchased: 5\nFree: 0\nTotal: 5"
        )

        interaction.response.send_message.reset_mock()
        await leaderboard.callback(group, interaction, sortby="purchased")  # type: ignore[arg-type]

        embed = interaction.response.send_message.await_args.kwargs["embed"]
        assert embed.title == "Lifetime raffle tickets (by purchased)"
        assert (embed.description or "").startswith("**Buyer.1234**")

    async def test_leaderboard_reports_when_no_history(self) -> None:
        bot = SimpleNamespace(
            get_lifetime_raffle_contributions=MagicMock(return_value=[])
        )
        group = RaffleCommands(bot)  # type: ignore[arg-type]
        leaderboard = next(
            command for command in group.commands if command.name == "leaderboard"
        )
        interaction = SimpleNamespace(
            response=SimpleNamespace(send_message=AsyncMock()),
        )

        await leaderboard.callback(group, interaction)  # type: ignore[arg-type]

        interaction.response.send_message.assert_awaited_once_with(
            "No lifetime raffle tickets have been recorded yet."
        )

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

        with caplog.at_level(logging.DEBUG, logger="gw2bot"):
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

        with caplog.at_level(logging.DEBUG, logger="gw2bot"):
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
                            RaffleWinner("Winner A.1234", 1, 10, tickets_held=6),
                            RaffleWinner("Winner B.5678", 8, 9, tickets_held=4),
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
            "1. **Winner A.1234** (60.0% chance)\n"
            "2. **Winner B.5678** (44.4% chance)\n"
            "Selected 2 winners from 8 purchased tickets and 2 free tickets. "
            "All current raffle tickets have been reset."
        )
        bot.mark_raffle_announcement_sent.assert_called_once_with(7)

    async def test_retries_pending_announcement_without_refreshing_or_redrawing(
        self,
    ) -> None:
        pending = SimpleNamespace(
            run_id=7,
            winners=(RaffleWinner("Winner.1234", 1, 10, tickets_held=9),),
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
                    winners=(RaffleWinner("Winner.1234", 1, 10, tickets_held=10),),
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

    def test_formats_repeat_winners_with_win_chances_in_draw_order(self) -> None:
        result = SimpleNamespace(
            winners=(
                RaffleWinner("Repeat.1234", 3, 20, tickets_held=5),
                RaffleWinner("Other.5678", 11, 19, tickets_held=3),
                RaffleWinner("Repeat.1234", 2, 18, tickets_held=4),
            ),
            total_tickets=20,
            purchased_tickets=17,
            free_tickets=3,
        )

        message = format_raffle_result(result)  # type: ignore[arg-type]

        assert message.startswith(
            "Raffle winners:\n"
            "1. **Repeat.1234** (25.0% chance)\n"
            "2. **Other.5678** (15.8% chance)\n"
            "3. **Repeat.1234** (22.2% chance)\n"
        )

    def test_formats_legacy_winner_without_win_chance(self) -> None:
        result = SimpleNamespace(
            winners=(RaffleWinner("Legacy.1234", 4, 10),),
            total_tickets=10,
            purchased_tickets=10,
            free_tickets=0,
        )

        message = format_raffle_result(result)  # type: ignore[arg-type]

        assert "1. **Legacy.1234**\n" in message
        assert "chance" not in message

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

        with caplog.at_level(logging.ERROR, logger="gw2bot"):
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

        with caplog.at_level(logging.ERROR, logger="gw2bot"):
            await command.callback(group, interaction, "Member.1234", 1)  # type: ignore[arg-type]

        assert secret not in caplog.text
        assert "Could not refresh the guild member cache" in caplog.text
        interaction.followup.send.assert_awaited_once_with(
            "Could not verify guild membership. Try again later.",
            ephemeral=True,
        )


def two_draw_audit() -> RaffleAudit:
    initial_ranges = (
        RaffleAuditRange("Alice.1111", 10, 1, 10),
        RaffleAuditRange("Bob.2222", 6, 11, 16),
    )
    second_ranges = (
        RaffleAuditRange("Alice.1111", 10, 1, 10),
        RaffleAuditRange("Bob.2222", 5, 11, 15),
    )
    return RaffleAudit(
        run_id=7,
        run_time="2026-06-07 12:00:00",
        total_tickets=16,
        purchased_tickets=15,
        free_tickets=1,
        entrants=initial_ranges,
        draws=(
            RaffleAuditDraw(
                draw_position=1,
                username="Bob.2222",
                winning_ticket=11,
                tickets_before_draw=16,
                tickets_held=6,
                ranges=initial_ranges,
            ),
            RaffleAuditDraw(
                draw_position=2,
                username="Bob.2222",
                winning_ticket=11,
                tickets_before_draw=15,
                tickets_held=5,
                ranges=second_ranges,
            ),
        ),
    )


def legacy_audit() -> RaffleAudit:
    return RaffleAudit(
        run_id=1,
        run_time="2026-01-01 00:00:00",
        total_tickets=10,
        purchased_tickets=10,
        free_tickets=0,
        entrants=(),
        draws=(
            RaffleAuditDraw(
                draw_position=1,
                username="Winner.1234",
                winning_ticket=4,
                tickets_before_draw=10,
                tickets_held=None,
                ranges=(),
            ),
        ),
    )


class TestRaffleAuditCommand:
    async def test_sends_public_audit_embed_without_role_gate(self) -> None:
        # The bot namespace omits authorize_raffle_command on purpose: the
        # audit command must be usable by every member, so gating would fail
        # this test with an AttributeError.
        bot = SimpleNamespace(get_raffle_audit=MagicMock(return_value=two_draw_audit()))
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=1234),
            response=SimpleNamespace(send_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        group = RaffleCommands(bot)  # type: ignore[arg-type]
        command = next(
            command for command in group.commands if command.name == "audit"
        )

        await command.callback(group, interaction, 7)  # type: ignore[arg-type]

        bot.get_raffle_audit.assert_called_once_with(7)
        kwargs = interaction.response.send_message.await_args.kwargs
        assert "ephemeral" not in kwargs
        embed = kwargs["embed"]
        assert embed.title == "Raffle Run #7 Audit"
        assert "Drawn at 2026-06-07 12:00:00 UTC." in (embed.description or "")
        assert [field.name for field in embed.fields] == [
            "Ticket Ranges (2 entrants)",
            "Ticket Pool",
            "Draws",
        ]
        fields = {field.name: field.value for field in embed.fields}
        assert fields["Ticket Ranges (2 entrants)"] == (
            "**Alice.1111** — #1–#10 (10 tickets)\n"
            "**Bob.2222** — #11–#16 (6 tickets)"
        )
        assert fields["Ticket Pool"] == "Total tickets: 16\nPurchased: 15\nFree: 1"
        assert fields["Draws"] == (
            "Draw 1: ticket #11 of 16 — **Bob.2222** "
            "(held #11–#16, 37.5% chance)\n"
            "Draw 2: ticket #11 of 15 — **Bob.2222** "
            "(held #11–#15, 33.3% chance)"
        )
        assert embed.footer.text == RAFFLE_AUDIT_VERIFY_FOOTER
        interaction.followup.send.assert_not_awaited()

    async def test_reports_unknown_run_and_lists_valid_ids(self) -> None:
        bot = SimpleNamespace(
            get_raffle_audit=MagicMock(return_value=None),
            get_raffle_run_summaries=MagicMock(
                return_value=[
                    RaffleRunSummary(3, "2026-06-21 12:00:00"),
                    RaffleRunSummary(2, "2026-06-14 12:00:00"),
                    RaffleRunSummary(1, "2026-06-07 12:00:00"),
                ]
            ),
        )
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=1234),
            response=SimpleNamespace(send_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        group = RaffleCommands(bot)  # type: ignore[arg-type]
        command = next(
            command for command in group.commands if command.name == "audit"
        )

        await command.callback(group, interaction, 99)  # type: ignore[arg-type]

        interaction.response.send_message.assert_awaited_once_with(
            "Raffle run 99 was not found. Valid run ids: 3, 2, 1.",
            ephemeral=True,
        )
        interaction.followup.send.assert_not_awaited()


class TestRaffleRunAutocomplete:
    async def test_filters_run_ids_by_typed_prefix(self) -> None:
        bot = SimpleNamespace(
            get_raffle_run_summaries=MagicMock(
                return_value=[
                    RaffleRunSummary(12, "2026-06-28 12:00:00"),
                    RaffleRunSummary(11, "2026-06-21 12:00:00"),
                    RaffleRunSummary(2, "2026-06-14 12:00:00"),
                    RaffleRunSummary(1, "2026-06-07 12:00:00"),
                ]
            )
        )
        interaction = SimpleNamespace(user=SimpleNamespace(id=1234))
        group = RaffleCommands(bot)  # type: ignore[arg-type]

        choices = await group.raffle_run_autocomplete(
            interaction,  # type: ignore[arg-type]
            "1",
        )

        assert [(choice.name, choice.value) for choice in choices] == [
            ("Run 12 — 2026-06-28 12:00:00", 12),
            ("Run 11 — 2026-06-21 12:00:00", 11),
            ("Run 1 — 2026-06-07 12:00:00", 1),
        ]

    async def test_lists_newest_runs_first_without_typed_text(self) -> None:
        bot = SimpleNamespace(
            get_raffle_run_summaries=MagicMock(
                return_value=[
                    RaffleRunSummary(run_id, "2026-06-07 12:00:00")
                    for run_id in range(30, 0, -1)
                ]
            )
        )
        interaction = SimpleNamespace(user=SimpleNamespace(id=1234))
        group = RaffleCommands(bot)  # type: ignore[arg-type]

        choices = await group.raffle_run_autocomplete(
            interaction,  # type: ignore[arg-type]
            "",
        )

        assert len(choices) == 25
        assert [choice.value for choice in choices][:3] == [30, 29, 28]

    async def test_failure_logging_omits_secret_bearing_exception(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        secret = "run-autocomplete-secret"
        bot = SimpleNamespace(
            get_raffle_run_summaries=MagicMock(
                side_effect=SQLAlchemyError(
                    f"query failed with access_token={secret}"
                )
            )
        )
        interaction = SimpleNamespace(user=SimpleNamespace(id=1234))
        group = RaffleCommands(bot)  # type: ignore[arg-type]

        with caplog.at_level(logging.ERROR, logger="gw2bot"):
            choices = await group.raffle_run_autocomplete(
                interaction,  # type: ignore[arg-type]
                "1",
            )

        assert choices == []
        assert secret not in caplog.text
        assert "Could not load raffle runs for autocomplete" in caplog.text


class TestRaffleAuditFormatting:
    def test_legacy_run_embed_notes_missing_snapshot(self) -> None:
        embeds = raffle_audit_embeds(legacy_audit())

        assert len(embeds) == 1
        embed = embeds[0]
        assert (
            "The full entrant snapshot isn't available for this run"
            in (embed.description or "")
        )
        assert [field.name for field in embed.fields] == ["Ticket Pool", "Draw"]
        fields = {field.name: field.value for field in embed.fields}
        assert fields["Draw"] == "Draw 1: ticket #4 of 10 — **Winner.1234**"
        assert fields["Ticket Pool"] == (
            "Total tickets: 10\nPurchased: 10\nFree: 0"
        )
        assert embed.footer.text == RAFFLE_AUDIT_VERIFY_FOOTER

    def test_single_ticket_range_renders_one_number(self) -> None:
        entrants = (RaffleAuditRange("Solo.1234", 1, 1, 1),)
        audit = RaffleAudit(
            run_id=2,
            run_time="2026-06-07 12:00:00",
            total_tickets=1,
            purchased_tickets=1,
            free_tickets=0,
            entrants=entrants,
            draws=(
                RaffleAuditDraw(
                    draw_position=1,
                    username="Solo.1234",
                    winning_ticket=1,
                    tickets_before_draw=1,
                    tickets_held=1,
                    ranges=entrants,
                ),
            ),
        )

        embeds = raffle_audit_embeds(audit)

        fields = {field.name: field.value for field in embeds[0].fields}
        assert fields["Ticket Ranges (1 entrant)"] == (
            "**Solo.1234** — #1 (1 ticket)"
        )
        assert fields["Draw"] == (
            "Draw 1: ticket #1 of 1 — **Solo.1234** (held #1, 100.0% chance)"
        )

    def test_splits_long_entrant_list_across_fields_and_embeds(self) -> None:
        entrants = tuple(
            RaffleAuditRange(f"Member {index:03d}.1234", 1, index + 1, index + 1)
            for index in range(400)
        )
        audit = RaffleAudit(
            run_id=9,
            run_time="2026-06-07 12:00:00",
            total_tickets=400,
            purchased_tickets=400,
            free_tickets=0,
            entrants=entrants,
            draws=(
                RaffleAuditDraw(
                    draw_position=1,
                    username="Member 000.1234",
                    winning_ticket=1,
                    tickets_before_draw=400,
                    tickets_held=1,
                    ranges=entrants,
                ),
            ),
        )

        embeds = raffle_audit_embeds(audit)

        assert len(embeds) > 1
        for embed in embeds:
            assert len(embed.fields) <= RAFFLE_AUDIT_EMBED_FIELD_LIMIT
            characters = (
                len(embed.title or "")
                + len(embed.description or "")
                + len(embed.footer.text or "")
            )
            for field in embed.fields:
                assert field.value is not None
                assert len(field.value) <= RAFFLE_AUDIT_FIELD_CHAR_LIMIT
                characters += len(field.name or "") + len(field.value)
            assert characters <= 6_000
        combined = "\n".join(
            field.value or "" for embed in embeds for field in embed.fields
        )
        assert all(entrant.username in combined for entrant in entrants)
        assert [field.name for field in embeds[0].fields][-2:] == [
            "Ticket Pool",
            "Draw",
        ]
        assert all(
            field.name is not None and field.name.startswith("Ticket Ranges")
            for embed in embeds[1:]
            for field in embed.fields
        )

    def test_unknown_run_message_truncates_long_id_lists(self) -> None:
        summaries = [
            RaffleRunSummary(run_id, "2026-06-07 12:00:00")
            for run_id in range(20, 0, -1)
        ]

        message = format_unknown_raffle_run_message(42, summaries)

        assert message == (
            "Raffle run 42 was not found. Valid run ids: 20, 19, 18, 17, 16, "
            "15, 14, 13, 12, 11, 10, 9, 8, 7, 6 (+5 more)."
        )

    def test_unknown_run_message_when_no_runs_recorded(self) -> None:
        assert format_unknown_raffle_run_message(1, []) == (
            "Raffle run 1 was not found. "
            "No raffle draws have been recorded yet."
        )
