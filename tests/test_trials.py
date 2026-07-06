import logging
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, call, patch

import aiohttp
import discord
import pytest
from discord import app_commands

from factories import not_found_error
from gw2bot.guild_members import TrialMemberReportEntry
from gw2bot.bot import Gw2Bot
from gw2bot.raffle import RaffleStore, TrialForumPost
from gw2bot.raffle.roles import RAFFLE_OFFICER_ROLE_ID
from gw2bot.trials.forum import (
    TRIAL_ACCEPTED_TAG_ID,
    TRIAL_FORUM_CHANNEL_ID,
    TRIAL_IN_REVIEW_TAG_ID,
)
from gw2bot.trials.reports import (
    SUNBORNE_ROLE_ID,
    TRIAL_ROLE_ID,
    format_track_audit,
)


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


class TestTrialForumFailureLogging:
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
        with caplog.at_level(logging.ERROR, logger="gw2bot"):
            entries = await Gw2Bot._resolve_trial_member_discord_statuses(
                cast(Gw2Bot, bot), ["User.1234"]
            )

        assert entries == [TrialMemberReportEntry("User.1234")]
        assert "raw-response-body-secret" not in caplog.text
        assert "type=DiscordFailure status=403 code=50001" in caplog.text


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

        with caplog.at_level(logging.ERROR, logger="gw2bot"):
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

        with caplog.at_level(logging.ERROR, logger="gw2bot"):
            await Gw2Bot._apply_trial_forum_in_review_tag(
                cast(Gw2Bot, bot),
                cast(discord.Thread, thread),
            )

        assert secret not in caplog.text
        assert "type=DiscordFailure status=403 code=50013" in caplog.text


def _trial_status_resolver(
    status_by_user: dict[str, str | None],
) -> AsyncMock:
    async def resolve(usernames: list[str]) -> list[TrialMemberReportEntry]:
        return [
            TrialMemberReportEntry(
                username,
                discord_user_id=100,
                discord_status=status_by_user.get(username),
            )
            for username in usernames
        ]

    return AsyncMock(side_effect=resolve)


class TestTrialMemberReportMessages:
    async def test_builds_before_and_past_mark_trial_reports(self) -> None:
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
                        "name": "EarlySunborne.1234",
                        "rank": "Trial",
                        "joined": (now - timedelta(days=1)).isoformat(),
                    },
                    {
                        "name": "EarlyTrial.1234",
                        "rank": "Trial",
                        "joined": (now - timedelta(days=2)).isoformat(),
                    },
                ]
            )
        )
        bot = SimpleNamespace(
            _api=api,
            _config=SimpleNamespace(gw2_guild_id="guild-id"),
            get_tracked_trial_member_times=MagicMock(return_value={}),
            untrack_trial_member=MagicMock(),
            _resolve_trial_member_discord_statuses=_trial_status_resolver(
                {
                    "Overdue.1234": "Trial",
                    "EarlySunborne.1234": "Sunborne",
                    "EarlyTrial.1234": "Trial",
                }
            ),
        )

        before_mark, past_mark = await Gw2Bot._build_trial_report_messages(
            cast(Gw2Bot, bot),
            now,
        )

        api.get_guild_members.assert_awaited_once_with("guild-id")
        assert "Trial members before the 14-day mark" in before_mark
        assert "EarlySunborne.1234" in before_mark
        assert "EarlyTrial.1234" not in before_mark
        assert "Overdue.1234" not in before_mark
        assert "Trial members past the 14-day mark" in past_mark
        assert "Overdue.1234" in past_mark
        assert "ranked up to Sunborne" in past_mark
        assert "EarlySunborne.1234" not in past_mark

    async def test_builds_only_past_mark_when_no_early_sunborne_members(self) -> None:
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
                        "name": "EarlyTrial.1234",
                        "rank": "Trial",
                        "joined": (now - timedelta(days=1)).isoformat(),
                    },
                ]
            )
        )
        bot = SimpleNamespace(
            _api=api,
            _config=SimpleNamespace(gw2_guild_id="guild-id"),
            get_tracked_trial_member_times=MagicMock(return_value={}),
            untrack_trial_member=MagicMock(),
            _resolve_trial_member_discord_statuses=_trial_status_resolver(
                {"Overdue.1234": "Trial", "EarlyTrial.1234": "Trial"}
            ),
        )

        messages = await Gw2Bot._build_trial_report_messages(cast(Gw2Bot, bot), now)

        assert len(messages) == 1
        assert "Trial members past the 14-day mark" in messages[0]
        assert "Overdue.1234" in messages[0]

    async def test_builds_no_messages_when_no_trials(self) -> None:
        bot = SimpleNamespace(
            _api=SimpleNamespace(get_guild_members=AsyncMock(return_value=[])),
            _config=SimpleNamespace(gw2_guild_id="guild-id"),
            get_tracked_trial_member_times=MagicMock(return_value={}),
            untrack_trial_member=MagicMock(),
            _resolve_trial_member_discord_statuses=_trial_status_resolver({}),
        )

        messages = await Gw2Bot._build_trial_report_messages(
            cast(Gw2Bot, bot),
            datetime(2026, 6, 7, tzinfo=UTC),
        )

        assert messages == []

    async def test_moves_tracked_members_to_warning_report(self) -> None:
        now = datetime(2026, 6, 7, 17, 0, tzinfo=UTC)
        api = SimpleNamespace(
            get_guild_members=AsyncMock(
                return_value=[
                    {
                        "name": "Overdue.1234",
                        "rank": "Trial",
                        "joined": (now - timedelta(days=20)).isoformat(),
                    },
                    {
                        "name": "Tracked.5678",
                        "rank": "Trial",
                        "joined": (now - timedelta(days=20)).isoformat(),
                    },
                ]
            )
        )
        untrack = MagicMock()
        bot = SimpleNamespace(
            _api=api,
            _config=SimpleNamespace(gw2_guild_id="guild-id"),
            get_tracked_trial_member_times=MagicMock(
                return_value={
                    # Tracked more than 7 days ago -> past the warning mark.
                    "tracked.5678": now - timedelta(days=8),
                    # No longer overdue -> auto-untracked.
                    "Gone.9012": now - timedelta(days=8),
                }
            ),
            untrack_trial_member=untrack,
            _resolve_trial_member_discord_statuses=_trial_status_resolver(
                {"Overdue.1234": "Trial", "Tracked.5678": "Trial"}
            ),
        )

        past_mark, warning = await Gw2Bot._build_trial_report_messages(
            cast(Gw2Bot, bot),
            now,
        )

        assert "Trial members past the 14-day mark" in past_mark
        assert "Overdue.1234" in past_mark
        assert "Tracked.5678" not in past_mark
        assert "Trial members past the 7-day warning mark (to be kicked)" in warning
        assert "Tracked.5678" in warning
        untrack.assert_called_once_with("Gone.9012")

    async def test_sunborne_tracked_members_return_to_past_mark_report(self) -> None:
        now = datetime(2026, 6, 7, 17, 0, tzinfo=UTC)
        api = SimpleNamespace(
            get_guild_members=AsyncMock(
                return_value=[
                    {
                        "name": name,
                        "rank": "Trial",
                        "joined": (now - timedelta(days=20)).isoformat(),
                    }
                    for name in (
                        "Promoted Warned.1234",
                        "Promoted Pending.5678",
                        "Still Trial.9012",
                    )
                ]
            )
        )
        untrack = MagicMock()
        bot = SimpleNamespace(
            _api=api,
            _config=SimpleNamespace(gw2_guild_id="guild-id"),
            get_tracked_trial_member_times=MagicMock(
                return_value={
                    "Promoted Warned.1234": now - timedelta(days=8),
                    "Promoted Pending.5678": now - timedelta(days=2),
                    "Still Trial.9012": now - timedelta(days=8),
                }
            ),
            untrack_trial_member=untrack,
            _resolve_trial_member_discord_statuses=_trial_status_resolver(
                {
                    "Promoted Warned.1234": "Sunborne",
                    "Promoted Pending.5678": "Sunborne",
                    "Still Trial.9012": "Trial",
                }
            ),
        )

        past_mark, warning = await Gw2Bot._build_trial_report_messages(
            cast(Gw2Bot, bot),
            now,
        )

        # Members who reached Sunborne in Discord leave the warning flow and
        # return to the past-14-day report until their in-game rank changes.
        assert "Trial members past the 14-day mark" in past_mark
        assert "Promoted Warned.1234 - <@100> - Sunborne" in past_mark
        assert "Promoted Pending.5678 - <@100> - Sunborne" in past_mark
        assert "Still Trial.9012" not in past_mark
        assert "Trial members past the 7-day warning mark (to be kicked)" in warning
        assert "Still Trial.9012" in warning
        assert "Promoted Warned.1234" not in warning
        assert "Promoted Pending.5678" not in warning
        assert sorted(call_.args[0] for call_ in untrack.call_args_list) == [
            "Promoted Pending.5678",
            "Promoted Warned.1234",
        ]

    async def test_tracked_member_in_grace_window_shows_kick_countdown(self) -> None:
        now = datetime(2026, 6, 7, 17, 0, tzinfo=UTC)
        api = SimpleNamespace(
            get_guild_members=AsyncMock(
                return_value=[
                    {
                        "name": "Tracked.5678",
                        "rank": "Trial",
                        "joined": (now - timedelta(days=20)).isoformat(),
                    },
                ]
            )
        )
        untrack = MagicMock()
        bot = SimpleNamespace(
            _api=api,
            _config=SimpleNamespace(gw2_guild_id="guild-id"),
            get_tracked_trial_member_times=MagicMock(
                # Tracked only 2 days ago -> still inside the 7-day grace window.
                return_value={"Tracked.5678": now - timedelta(days=2)}
            ),
            untrack_trial_member=untrack,
            _resolve_trial_member_discord_statuses=_trial_status_resolver(
                {"Tracked.5678": "Trial"}
            ),
        )

        messages = await Gw2Bot._build_trial_report_messages(cast(Gw2Bot, bot), now)

        # Removed from the 14-day report when tracked, kept off the 7-day
        # warning report, and shown with a Discord timestamp counting down to
        # the end of the warning window.
        assert len(messages) == 1
        assert "Trial members within the 7-day warning window" in messages[0]
        deadline = int((now + timedelta(days=5)).timestamp())
        assert f"* Tracked.5678 - <@100> - Trial - kick <t:{deadline}:R>" in (
            messages[0]
        )
        assert "to be kicked" not in messages[0]
        assert "past the 14-day mark" not in messages[0]
        untrack.assert_not_called()


class TestTrialMemberNotification:
    async def test_posts_each_built_message_to_notification_channel(self) -> None:
        bot = SimpleNamespace(
            _build_trial_report_messages=AsyncMock(
                return_value=["before mark", "past mark"]
            ),
            _try_send_notification=AsyncMock(return_value=True),
        )

        delivered = await Gw2Bot._check_overdue_trials(cast(Gw2Bot, bot), None)

        assert delivered
        assert bot._try_send_notification.await_args_list == [
            call("before mark"),
            call("past mark"),
        ]

    async def test_does_not_post_when_no_trials_are_overdue(self) -> None:
        bot = SimpleNamespace(
            _build_trial_report_messages=AsyncMock(return_value=[]),
            _try_send_notification=AsyncMock(return_value=True),
        )

        delivered = await Gw2Bot._check_overdue_trials(cast(Gw2Bot, bot), None)

        assert delivered
        bot._try_send_notification.assert_not_awaited()

    async def test_reports_failed_delivery_to_poller(self) -> None:
        bot = SimpleNamespace(
            _build_trial_report_messages=AsyncMock(return_value=["past mark"]),
            _try_send_notification=AsyncMock(return_value=False),
        )

        delivered = await Gw2Bot._check_overdue_trials(cast(Gw2Bot, bot), None)

        assert not delivered


class TestCheckCommand:
    async def test_command_is_named_check_and_delegates_to_handler(self) -> None:
        bot = SimpleNamespace(_handle_check_command=AsyncMock())
        interaction = SimpleNamespace()

        command = Gw2Bot._create_check_command(cast(Gw2Bot, bot))

        assert command.name == "check"
        assert command.guild_only
        await command.callback(interaction)  # type: ignore[arg-type]
        bot._handle_check_command.assert_awaited_once_with(interaction)

    async def test_rejects_users_without_officer_role(self) -> None:
        bot = SimpleNamespace(_build_trial_report_messages=AsyncMock())
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=1, roles=[]),
            response=SimpleNamespace(send_message=AsyncMock()),
        )

        await Gw2Bot._handle_check_command(
            cast(Gw2Bot, bot),
            cast(discord.Interaction, interaction),
        )

        bot._build_trial_report_messages.assert_not_awaited()
        message = interaction.response.send_message.await_args.args[0]
        assert "required role" in message
        assert interaction.response.send_message.await_args.kwargs == {
            "ephemeral": True
        }

    async def test_sends_report_messages_ephemerally_to_officer(self) -> None:
        bot = SimpleNamespace(
            _build_trial_report_messages=AsyncMock(
                return_value=["before mark", "past mark"]
            ),
        )
        interaction = SimpleNamespace(
            user=SimpleNamespace(
                id=1,
                roles=[SimpleNamespace(id=RAFFLE_OFFICER_ROLE_ID)],
            ),
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

        await Gw2Bot._handle_check_command(
            cast(Gw2Bot, bot),
            cast(discord.Interaction, interaction),
        )

        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        assert interaction.followup.send.await_args_list == [
            call("before mark", ephemeral=True),
            call("past mark", ephemeral=True),
        ]

    async def test_reports_when_no_members_to_report(self) -> None:
        bot = SimpleNamespace(
            _build_trial_report_messages=AsyncMock(return_value=[]),
        )
        interaction = SimpleNamespace(
            user=SimpleNamespace(
                id=1,
                roles=[SimpleNamespace(id=RAFFLE_OFFICER_ROLE_ID)],
            ),
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

        await Gw2Bot._handle_check_command(
            cast(Gw2Bot, bot),
            cast(discord.Interaction, interaction),
        )

        interaction.followup.send.assert_awaited_once_with(
            "No Trial members to report.",
            ephemeral=True,
        )


class TestTrackCommand:
    def test_format_track_audit_uses_mention_and_verb(self) -> None:
        assert (
            format_track_audit("Username.1234", 42, tracked=True)
            == "Username.1234 warning tracked by <@42>"
        )
        assert (
            format_track_audit("Username.1234", 42, tracked=False)
            == "Username.1234 warning untracked by <@42>"
        )

    async def test_command_is_named_track_and_delegates_to_handler(self) -> None:
        async def _autocomplete(
            interaction: discord.Interaction,
            current: str,
        ) -> list[app_commands.Choice[str]]:
            return []

        bot = SimpleNamespace(
            _handle_track_command=AsyncMock(),
            _track_member_autocomplete=_autocomplete,
        )
        interaction = SimpleNamespace()

        command = Gw2Bot._create_track_command(cast(Gw2Bot, bot))

        assert command.name == "track"
        assert command.guild_only
        await command.callback(interaction, "Username.1234")  # type: ignore[arg-type]
        bot._handle_track_command.assert_awaited_once_with(
            interaction,
            "Username.1234",
        )

    async def test_rejects_users_without_officer_role(self) -> None:
        bot = SimpleNamespace(
            resolve_guild_member=AsyncMock(),
            toggle_trial_member_tracking=MagicMock(),
        )
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=1, roles=[]),
            response=SimpleNamespace(send_message=AsyncMock()),
        )

        await Gw2Bot._handle_track_command(
            cast(Gw2Bot, bot),
            cast(discord.Interaction, interaction),
            "Username.1234",
        )

        bot.resolve_guild_member.assert_not_awaited()
        bot.toggle_trial_member_tracking.assert_not_called()
        message = interaction.response.send_message.await_args.args[0]
        assert "required role" in message
        assert interaction.response.send_message.await_args.kwargs == {
            "ephemeral": True
        }

    async def test_rejects_non_guild_member(self) -> None:
        bot = SimpleNamespace(
            resolve_guild_member=AsyncMock(return_value=None),
            toggle_trial_member_tracking=MagicMock(),
        )
        interaction = SimpleNamespace(
            user=SimpleNamespace(
                id=1,
                roles=[SimpleNamespace(id=RAFFLE_OFFICER_ROLE_ID)],
            ),
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

        await Gw2Bot._handle_track_command(
            cast(Gw2Bot, bot),
            cast(discord.Interaction, interaction),
            "Ghost.1234",
        )

        bot.toggle_trial_member_tracking.assert_not_called()
        message = interaction.followup.send.await_args.args[0]
        assert "is not a member of the configured guild" in message

    async def test_tracks_member_and_posts_audit(self) -> None:
        bot = SimpleNamespace(
            resolve_guild_member=AsyncMock(return_value="Username.1234"),
            toggle_trial_member_tracking=MagicMock(return_value=True),
            send_notification=AsyncMock(return_value=True),
        )
        interaction = SimpleNamespace(
            user=SimpleNamespace(
                id=99,
                roles=[SimpleNamespace(id=RAFFLE_OFFICER_ROLE_ID)],
            ),
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

        await Gw2Bot._handle_track_command(
            cast(Gw2Bot, bot),
            cast(discord.Interaction, interaction),
            "username.1234",
        )

        bot.toggle_trial_member_tracking.assert_called_once_with(
            "Username.1234",
            99,
        )
        bot.send_notification.assert_awaited_once_with(
            "Username.1234 warning tracked by <@99>"
        )
        reply = interaction.followup.send.await_args.args[0]
        assert "Now tracking **Username.1234**" in reply
        assert interaction.followup.send.await_args.kwargs == {"ephemeral": True}

    async def test_untracks_member_and_posts_audit(self) -> None:
        bot = SimpleNamespace(
            resolve_guild_member=AsyncMock(return_value="Username.1234"),
            toggle_trial_member_tracking=MagicMock(return_value=False),
            send_notification=AsyncMock(return_value=True),
        )
        interaction = SimpleNamespace(
            user=SimpleNamespace(
                id=99,
                roles=[SimpleNamespace(id=RAFFLE_OFFICER_ROLE_ID)],
            ),
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

        await Gw2Bot._handle_track_command(
            cast(Gw2Bot, bot),
            cast(discord.Interaction, interaction),
            "Username.1234",
        )

        bot.send_notification.assert_awaited_once_with(
            "Username.1234 warning untracked by <@99>"
        )
        reply = interaction.followup.send.await_args.args[0]
        assert "Stopped tracking **Username.1234**" in reply

    async def test_notes_when_audit_delivery_fails(self) -> None:
        bot = SimpleNamespace(
            resolve_guild_member=AsyncMock(return_value="Username.1234"),
            toggle_trial_member_tracking=MagicMock(return_value=True),
            send_notification=AsyncMock(return_value=False),
        )
        interaction = SimpleNamespace(
            user=SimpleNamespace(
                id=99,
                roles=[SimpleNamespace(id=RAFFLE_OFFICER_ROLE_ID)],
            ),
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

        await Gw2Bot._handle_track_command(
            cast(Gw2Bot, bot),
            cast(discord.Interaction, interaction),
            "Username.1234",
        )

        reply = interaction.followup.send.await_args.args[0]
        assert "The audit log could not be delivered." in reply

    async def test_reports_membership_lookup_failure(self) -> None:
        bot = SimpleNamespace(
            resolve_guild_member=AsyncMock(side_effect=aiohttp.ClientError()),
            toggle_trial_member_tracking=MagicMock(),
        )
        interaction = SimpleNamespace(
            user=SimpleNamespace(
                id=1,
                roles=[SimpleNamespace(id=RAFFLE_OFFICER_ROLE_ID)],
            ),
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

        await Gw2Bot._handle_track_command(
            cast(Gw2Bot, bot),
            cast(discord.Interaction, interaction),
            "Username.1234",
        )

        bot.toggle_trial_member_tracking.assert_not_called()
        message = interaction.followup.send.await_args.args[0]
        assert "Could not verify guild membership" in message

    async def test_autocomplete_requires_officer_role(self) -> None:
        bot = SimpleNamespace(search_guild_members=AsyncMock())
        interaction = SimpleNamespace(user=SimpleNamespace(id=1, roles=[]))

        choices = await Gw2Bot._track_member_autocomplete(
            cast(Gw2Bot, bot),
            cast(discord.Interaction, interaction),
            "User",
        )

        assert choices == []
        bot.search_guild_members.assert_not_awaited()

    async def test_autocomplete_returns_officer_choices(self) -> None:
        bot = SimpleNamespace(
            search_guild_members=AsyncMock(return_value=["Username.1234"]),
        )
        interaction = SimpleNamespace(
            user=SimpleNamespace(
                id=1,
                roles=[SimpleNamespace(id=RAFFLE_OFFICER_ROLE_ID)],
            ),
        )

        choices = await Gw2Bot._track_member_autocomplete(
            cast(Gw2Bot, bot),
            cast(discord.Interaction, interaction),
            "User",
        )

        assert [(choice.name, choice.value) for choice in choices] == [
            ("Username.1234", "Username.1234")
        ]


class TestTrialMemberStatusResolution:
    async def test_matches_indexed_posts_and_resolves_live_status(self) -> None:
        index = {
            1: TrialForumPost(1, 101, "application\ngw2 account is title.1234", "t"),
            2: TrialForumPost(
                2,
                202,
                "application\nmy account is body.2345\nreviewer comment.3456",
                "t",
            ),
            3: TrialForumPost(3, 303, "norole.4567 application", "t"),
        }
        members = {
            101: SimpleNamespace(roles=[SimpleNamespace(id=SUNBORNE_ROLE_ID)]),
            202: SimpleNamespace(roles=[SimpleNamespace(id=TRIAL_ROLE_ID)]),
            303: None,
        }
        guild = SimpleNamespace(
            get_member=MagicMock(side_effect=lambda owner_id: members.get(owner_id)),
            fetch_member=AsyncMock(return_value=SimpleNamespace(roles=[])),
        )
        forum = SimpleNamespace(
            id=TRIAL_FORUM_CHANNEL_ID,
            guild=guild,
            archived_threads=None,
        )
        bot = SimpleNamespace(
            fetch_channel=AsyncMock(return_value=forum),
            _refresh_trial_forum_index=AsyncMock(),
            _raffle_store=SimpleNamespace(
                get_trial_forum_index=MagicMock(return_value=index)
            ),
        )

        entries = await Gw2Bot._resolve_trial_member_discord_statuses(
            cast(Gw2Bot, bot),
            ["Title.1234", "Body.2345", "Comment.3456", "NoRole.4567", "Missing.5678"],
        )

        assert entries == [
            TrialMemberReportEntry("Title.1234", 101, "Sunborne"),
            TrialMemberReportEntry("Body.2345", 202, "Trial"),
            TrialMemberReportEntry("Comment.3456", 202, "Trial"),
            TrialMemberReportEntry("NoRole.4567", 303),
            TrialMemberReportEntry("Missing.5678"),
        ]
        bot._refresh_trial_forum_index.assert_awaited_once_with(forum)
        guild.fetch_member.assert_awaited_once_with(303)

    async def test_resolves_status_via_fetch_member_when_not_cached(self) -> None:
        index = {1: TrialForumPost(1, 777, "matched.1234", "t")}
        guild = SimpleNamespace(
            get_member=MagicMock(return_value=None),
            fetch_member=AsyncMock(
                return_value=SimpleNamespace(roles=[SimpleNamespace(id=TRIAL_ROLE_ID)])
            ),
        )
        forum = SimpleNamespace(
            id=TRIAL_FORUM_CHANNEL_ID,
            guild=guild,
            archived_threads=None,
        )
        bot = SimpleNamespace(
            fetch_channel=AsyncMock(return_value=forum),
            _refresh_trial_forum_index=AsyncMock(),
            _raffle_store=SimpleNamespace(
                get_trial_forum_index=MagicMock(return_value=index)
            ),
        )

        entries = await Gw2Bot._resolve_trial_member_discord_statuses(
            cast(Gw2Bot, bot),
            ["Matched.1234"],
        )

        assert entries == [TrialMemberReportEntry("Matched.1234", 777, "Trial")]
        guild.fetch_member.assert_awaited_once_with(777)

    async def test_preserves_matched_user_id_when_creator_left_guild(self) -> None:
        index = {1: TrialForumPost(1, 777, "former.1234", "t")}
        guild = SimpleNamespace(
            get_member=MagicMock(return_value=None),
            fetch_member=AsyncMock(side_effect=not_found_error()),
        )
        forum = SimpleNamespace(
            id=TRIAL_FORUM_CHANNEL_ID,
            guild=guild,
            archived_threads=None,
        )
        bot = SimpleNamespace(
            fetch_channel=AsyncMock(return_value=forum),
            _refresh_trial_forum_index=AsyncMock(),
            _raffle_store=SimpleNamespace(
                get_trial_forum_index=MagicMock(return_value=index)
            ),
        )

        entries = await Gw2Bot._resolve_trial_member_discord_statuses(
            cast(Gw2Bot, bot),
            ["Former.1234"],
        )

        assert entries == [TrialMemberReportEntry("Former.1234", 777)]

    async def test_requires_exact_normalized_account_name_match(self) -> None:
        index = {
            1: TrialForumPost(
                1, 777, "otheruser.1234 application\notheruser.1234", "t"
            )
        }
        guild = SimpleNamespace(
            get_member=MagicMock(
                return_value=SimpleNamespace(roles=[SimpleNamespace(id=TRIAL_ROLE_ID)])
            ),
            fetch_member=AsyncMock(),
        )
        forum = SimpleNamespace(
            id=TRIAL_FORUM_CHANNEL_ID,
            guild=guild,
            archived_threads=None,
        )
        bot = SimpleNamespace(
            fetch_channel=AsyncMock(return_value=forum),
            _refresh_trial_forum_index=AsyncMock(),
            _raffle_store=SimpleNamespace(
                get_trial_forum_index=MagicMock(return_value=index)
            ),
        )

        entries = await Gw2Bot._resolve_trial_member_discord_statuses(
            cast(Gw2Bot, bot),
            ["User.1234", "OtherUser.1234"],
        )

        assert entries == [
            TrialMemberReportEntry("User.1234"),
            TrialMemberReportEntry("OtherUser.1234", 777, "Trial"),
        ]

    async def test_skips_indexed_post_without_owner(self) -> None:
        index = {1: TrialForumPost(1, None, "ownerless.1234", "t")}
        guild = SimpleNamespace(
            get_member=MagicMock(return_value=None),
            fetch_member=AsyncMock(),
        )
        forum = SimpleNamespace(
            id=TRIAL_FORUM_CHANNEL_ID,
            guild=guild,
            archived_threads=None,
        )
        bot = SimpleNamespace(
            fetch_channel=AsyncMock(return_value=forum),
            _refresh_trial_forum_index=AsyncMock(),
            _raffle_store=SimpleNamespace(
                get_trial_forum_index=MagicMock(return_value=index)
            ),
        )

        entries = await Gw2Bot._resolve_trial_member_discord_statuses(
            cast(Gw2Bot, bot),
            ["Ownerless.1234"],
        )

        assert entries == [TrialMemberReportEntry("Ownerless.1234")]
        guild.fetch_member.assert_not_awaited()

    async def test_cold_build_indexes_accepted_threads(self) -> None:
        def history(*contents: str) -> Any:
            async def iterate() -> Any:
                for content in contents:
                    yield SimpleNamespace(content=content)

            return lambda **_: iterate()

        accepted_active = SimpleNamespace(
            id=1,
            parent_id=TRIAL_FORUM_CHANNEL_ID,
            owner_id=101,
            applied_tags=[SimpleNamespace(id=TRIAL_ACCEPTED_TAG_ID)],
            name="Active.1234 application",
            last_message_id=None,
            archive_timestamp=datetime(2026, 6, 1, tzinfo=UTC),
            created_at=datetime(2026, 6, 1, tzinfo=UTC),
            history=history("My account is Body.5678"),
        )
        accepted_archived = SimpleNamespace(
            id=2,
            parent_id=TRIAL_FORUM_CHANNEL_ID,
            owner_id=202,
            applied_tags=[SimpleNamespace(id=TRIAL_ACCEPTED_TAG_ID)],
            name="Archived.2345 application",
            last_message_id=None,
            archive_timestamp=datetime(2026, 6, 2, tzinfo=UTC),
            created_at=datetime(2026, 6, 2, tzinfo=UTC),
            history=history("Welcome"),
        )
        rejected = SimpleNamespace(
            id=3,
            parent_id=TRIAL_FORUM_CHANNEL_ID,
            owner_id=303,
            applied_tags=[SimpleNamespace(id=999)],
            name="Rejected.3456 application",
            last_message_id=None,
            archive_timestamp=datetime(2026, 6, 3, tzinfo=UTC),
            created_at=datetime(2026, 6, 3, tzinfo=UTC),
            history=MagicMock(),
        )
        guild = SimpleNamespace(
            active_threads=AsyncMock(return_value=[accepted_active]),
        )

        async def archived_threads(**_: Any) -> Any:
            yield accepted_archived
            yield rejected

        forum = SimpleNamespace(
            id=TRIAL_FORUM_CHANNEL_ID,
            guild=guild,
            archived_threads=archived_threads,
        )

        with tempfile.TemporaryDirectory() as directory:
            store = RaffleStore(str(Path(directory) / "raffle.db"), "guild-id")
            bot = SimpleNamespace(_raffle_store=store)

            await Gw2Bot._refresh_trial_forum_index(
                cast(Gw2Bot, bot),
                cast(discord.ForumChannel, forum),
            )

            index = store.get_trial_forum_index()
            assert set(index) == {1, 2}
            assert index[1].owner_id == 101
            assert "body.5678" in index[1].normalized_content
            assert "active.1234 application" in index[1].normalized_content
            assert index[2].owner_id == 202
            assert store.get_trial_forum_watermark() is not None
            rejected.history.assert_not_called()
            store.close()

    async def test_incremental_refresh_skips_unmodified_and_purges_unaccepted(
        self,
    ) -> None:
        def history(*contents: str) -> Any:
            async def iterate() -> Any:
                for content in contents:
                    yield SimpleNamespace(content=content)

            return lambda **_: iterate()

        with tempfile.TemporaryDirectory() as directory:
            store = RaffleStore(str(Path(directory) / "raffle.db"), "guild-id")
            store.upsert_trial_forum_posts(
                [
                    TrialForumPost(1, 101, "unchanged.1234 application", "t"),
                    TrialForumPost(2, 202, "dropped.5678 application", "t"),
                ]
            )
            watermark = datetime(2026, 6, 10, tzinfo=UTC)
            store.set_trial_forum_watermark(watermark)

            unchanged_history = MagicMock()
            unchanged = SimpleNamespace(
                id=1,
                parent_id=TRIAL_FORUM_CHANNEL_ID,
                owner_id=101,
                applied_tags=[SimpleNamespace(id=TRIAL_ACCEPTED_TAG_ID)],
                name="Unchanged.1234 application",
                last_message_id=None,
                archive_timestamp=watermark - timedelta(days=5),
                created_at=watermark - timedelta(days=5),
                history=unchanged_history,
            )
            unaccepted = SimpleNamespace(
                id=2,
                parent_id=TRIAL_FORUM_CHANNEL_ID,
                owner_id=202,
                applied_tags=[SimpleNamespace(id=999)],
                name="Dropped.5678 application",
                last_message_id=None,
                archive_timestamp=watermark,
                created_at=watermark,
                history=MagicMock(),
            )
            new_thread = SimpleNamespace(
                id=3,
                parent_id=TRIAL_FORUM_CHANNEL_ID,
                owner_id=303,
                applied_tags=[SimpleNamespace(id=TRIAL_ACCEPTED_TAG_ID)],
                name="New.9012 application",
                last_message_id=None,
                archive_timestamp=watermark,
                created_at=watermark,
                history=history("Fresh application"),
            )
            guild = SimpleNamespace(
                active_threads=AsyncMock(
                    return_value=[unchanged, unaccepted, new_thread]
                ),
            )

            async def archived_threads(**_: Any) -> Any:
                if False:
                    yield None

            forum = SimpleNamespace(
                id=TRIAL_FORUM_CHANNEL_ID,
                guild=guild,
                archived_threads=archived_threads,
            )
            bot = SimpleNamespace(_raffle_store=store)

            await Gw2Bot._refresh_trial_forum_index(
                cast(Gw2Bot, bot),
                cast(discord.ForumChannel, forum),
            )

            index = store.get_trial_forum_index()
            assert set(index) == {1, 3}
            unchanged_history.assert_not_called()
            assert index[1].normalized_content == "unchanged.1234 application"
            assert index[3].owner_id == 303
            store.close()

    @patch("gw2bot.trials.reports.seconds_until_trial_report", return_value=123)
    @patch("gw2bot.trials.reports.asyncio.sleep", new_callable=AsyncMock)
    async def test_poller_waits_for_daily_schedule_before_first_check(
        self,
        sleep: AsyncMock,
        seconds_until_report: MagicMock,
    ) -> None:
        bot = SimpleNamespace(
            wait_until_ready=AsyncMock(),
            is_closed=MagicMock(side_effect=[False, False, True]),
            _check_overdue_trials=AsyncMock(return_value=True),
            _poll_status=SimpleNamespace(
                record_error=MagicMock(),
                record_success=MagicMock(),
            ),
        )

        await Gw2Bot._poll_overdue_trials(bot)  # type: ignore[arg-type]

        bot.wait_until_ready.assert_awaited_once()
        bot._check_overdue_trials.assert_awaited_once()
        bot._poll_status.record_success.assert_called_once_with("Trial Members")
        seconds_until_report.assert_called_once()
        sleep.assert_awaited_once_with(123)

    @patch("gw2bot.trials.reports.seconds_until_trial_report", return_value=123)
    @patch("gw2bot.trials.reports.asyncio.sleep", new_callable=AsyncMock)
    async def test_poller_does_not_run_if_closed_during_scheduled_wait(
        self,
        sleep: AsyncMock,
        seconds_until_report: MagicMock,
    ) -> None:
        bot = SimpleNamespace(
            wait_until_ready=AsyncMock(),
            is_closed=MagicMock(side_effect=[False, True]),
            _check_overdue_trials=AsyncMock(return_value=False),
            _poll_status=SimpleNamespace(
                record_error=MagicMock(),
                record_success=MagicMock(),
            ),
        )

        await Gw2Bot._poll_overdue_trials(bot)  # type: ignore[arg-type]

        seconds_until_report.assert_called_once()
        sleep.assert_awaited_once_with(123)
        bot._check_overdue_trials.assert_not_awaited()
        bot._poll_status.record_success.assert_not_called()

    @patch("gw2bot.trials.reports.seconds_until_trial_report", side_effect=[123, 456])
    @patch("gw2bot.trials.reports.asyncio.sleep", new_callable=AsyncMock)
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
            _poll_status=SimpleNamespace(
                record_error=MagicMock(),
                record_success=MagicMock(),
            ),
        )

        await Gw2Bot._poll_overdue_trials(bot)  # type: ignore[arg-type]

        assert seconds_until_report.call_count == 2
        assert sleep.await_args_list == [call(123), call(456)]
        bot._check_overdue_trials.assert_awaited_once()
        bot._poll_status.record_error.assert_called_once_with("Trial Members", error)
        bot._poll_status.record_success.assert_not_called()
