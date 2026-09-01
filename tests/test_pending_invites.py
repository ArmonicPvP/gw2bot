from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, call

import discord
import pytest

from factories import configured_bot, default_config, unconfigured_bot
from gw2bot.bot import Gw2Bot
from gw2bot.config import DEFAULT_RAFFLE_OFFICER_ROLE_ID as OFFICER_ROLE_ID
from gw2bot.guild_members import TrialMemberReportEntry
from gw2bot.pending_invites import (
    build_pending_invite_entries,
    build_pending_invite_messages,
)


def guild_member(name: str, rank: str) -> dict[str, object]:
    return {"name": name, "rank": rank}


def api_bot(members: list[dict[str, object]], **attributes: object):
    """A bot whose GW2 API answers with ``members``.

    The Discord match is stubbed on the same seam the Trial reports use, so a
    pending-invite test never drives the forum index itself.
    """
    attributes.setdefault(
        "_resolve_trial_member_discord_statuses",
        AsyncMock(
            side_effect=lambda usernames: [
                TrialMemberReportEntry(username) for username in usernames
            ]
        ),
    )
    attributes.setdefault("_config", default_config(gw2_guild_id="guild-id"))
    return configured_bot(
        _api=SimpleNamespace(get_guild_members=AsyncMock(return_value=members)),
        **attributes,
    )


class TestPendingInviteEntries:
    async def test_matches_only_the_accounts_that_have_not_accepted(
        self,
    ) -> None:
        bot = api_bot(
            [
                guild_member("Waiting.1234", "invited"),
                guild_member("Joined.5678", "Trial"),
                guild_member("Member.9012", "Sunborne"),
            ]
        )

        entries = await build_pending_invite_entries(cast(Gw2Bot, bot))

        assert entries == [TrialMemberReportEntry("Waiting.1234")]
        bot._resolve_trial_member_discord_statuses.assert_awaited_once_with(
            ["Waiting.1234"]
        )

    async def test_does_not_touch_discord_when_nobody_is_waiting(self) -> None:
        bot = api_bot([guild_member("Member.9012", "Sunborne")])

        entries = await build_pending_invite_entries(cast(Gw2Bot, bot))

        assert entries == []
        # Matching costs a refresh of the Trial application forum index, and
        # there is nothing to match.
        bot._resolve_trial_member_discord_statuses.assert_not_awaited()

    async def test_builds_the_report_from_the_matched_entries(self) -> None:
        bot = api_bot(
            [
                guild_member("Bravo.5678", "invited"),
                guild_member("Alpha.1234", "invited"),
            ],
            _resolve_trial_member_discord_statuses=AsyncMock(
                return_value=[
                    TrialMemberReportEntry("Alpha.1234", discord_user_id=7),
                    TrialMemberReportEntry("Bravo.5678"),
                ]
            ),
        )

        messages = await build_pending_invite_messages(cast(Gw2Bot, bot))

        assert len(messages) == 1
        assert messages[0].startswith("**Pending invites**")
        assert "* Alpha.1234 - <@7>\n* Bravo.5678" in messages[0]

    async def test_builds_no_messages_without_pending_invites(self) -> None:
        bot = api_bot([guild_member("Member.9012", "Sunborne")])

        assert await build_pending_invite_messages(cast(Gw2Bot, bot)) == []

    async def test_refuses_to_build_without_an_api_client(self) -> None:
        bot = configured_bot(
            _api=None,
            _resolve_trial_member_discord_statuses=AsyncMock(),
        )

        with pytest.raises(RuntimeError):
            await build_pending_invite_entries(cast(Gw2Bot, bot))

    async def test_logging_names_no_account(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        bot = api_bot([guild_member("Secret.1234", "invited")])

        with caplog.at_level("DEBUG"):
            await build_pending_invite_messages(cast(Gw2Bot, bot))

        assert "Secret.1234" not in caplog.text
        assert "pending=1" in caplog.text


class TestPendingCommand:
    def test_command_is_named_pending_and_delegates_to_handler(self) -> None:
        bot = SimpleNamespace(
            _config=default_config(),
            _handle_pending_command=AsyncMock(),
        )

        command = Gw2Bot._create_pending_command(cast(Gw2Bot, bot))

        assert command.name == "pending"
        assert command.guild_only

    async def test_command_callback_delegates_to_the_handler(self) -> None:
        bot = SimpleNamespace(
            _config=default_config(),
            _handle_pending_command=AsyncMock(),
        )
        interaction = SimpleNamespace()

        command = Gw2Bot._create_pending_command(cast(Gw2Bot, bot))
        await command.callback(interaction)  # type: ignore[arg-type]

        bot._handle_pending_command.assert_awaited_once_with(interaction)

    async def test_rejects_users_without_officer_role(self) -> None:
        bot = SimpleNamespace(
            _config=default_config(),
            _build_pending_invite_messages=AsyncMock(),
        )
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=1, roles=[]),
            response=SimpleNamespace(send_message=AsyncMock()),
        )

        await Gw2Bot._handle_pending_command(
            cast(Gw2Bot, bot),
            cast(discord.Interaction, interaction),
        )

        bot._build_pending_invite_messages.assert_not_awaited()
        message = interaction.response.send_message.await_args.args[0]
        assert "required role" in message
        assert interaction.response.send_message.await_args.kwargs == {
            "ephemeral": True
        }

    async def test_sends_the_report_ephemerally_to_an_officer(self) -> None:
        bot = configured_bot(
            _build_pending_invite_messages=AsyncMock(
                return_value=["first page", "second page"]
            ),
        )
        interaction = SimpleNamespace(
            user=SimpleNamespace(
                id=1,
                roles=[SimpleNamespace(id=OFFICER_ROLE_ID)],
            ),
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

        await Gw2Bot._handle_pending_command(
            cast(Gw2Bot, bot),
            cast(discord.Interaction, interaction),
        )

        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        assert interaction.followup.send.await_args_list == [
            call("first page", ephemeral=True),
            call("second page", ephemeral=True),
        ]

    async def test_reports_the_unset_gw2_variables_to_an_officer(self) -> None:
        bot = unconfigured_bot(_build_pending_invite_messages=AsyncMock())
        interaction = SimpleNamespace(
            user=SimpleNamespace(
                id=1,
                roles=[SimpleNamespace(id=OFFICER_ROLE_ID)],
            ),
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

        await Gw2Bot._handle_pending_command(
            cast(Gw2Bot, bot),
            cast(discord.Interaction, interaction),
        )

        bot.reject_without_gw2_api.assert_awaited_once()
        bot._build_pending_invite_messages.assert_not_awaited()
        interaction.response.defer.assert_not_awaited()

    async def test_reports_when_nobody_is_waiting(self) -> None:
        bot = configured_bot(
            _build_pending_invite_messages=AsyncMock(return_value=[]),
        )
        interaction = SimpleNamespace(
            user=SimpleNamespace(
                id=1,
                roles=[SimpleNamespace(id=OFFICER_ROLE_ID)],
            ),
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

        await Gw2Bot._handle_pending_command(
            cast(Gw2Bot, bot),
            cast(discord.Interaction, interaction),
        )

        interaction.followup.send.assert_awaited_once_with(
            "No pending invites to report.",
            ephemeral=True,
        )
