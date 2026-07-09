from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

from gw2bot.events.commands import EventCommands
from gw2bot.events.roles import EVENT_CREATE_ROLE_ID
from gw2bot.events.models import (
    EventCategory,
    EventRole,
    RepeatFrequency,
)
from gw2bot.events.views import (
    EventDetailsModal,
    EventDraft,
    EventRepeatModal,
    EventScheduleModal,
    EventSignUpButton,
    RolePickSelect,
    SignupFlow,
    _signup_summary,
    build_signup_view,
)

FUTURE_START_TEXT = "01.30.2107 20:00"


def make_bot() -> Any:
    return cast(
        Any,
        SimpleNamespace(event_timezone=ZoneInfo("UTC"), event_store=None),
    )


def make_interaction(
    *,
    role_ids: tuple[int, ...] = (),
    message: Any = None,
) -> Any:
    interaction = MagicMock()
    interaction.user = SimpleNamespace(
        id=42,
        roles=[SimpleNamespace(id=role_id) for role_id in role_ids],
    )
    interaction.message = message
    interaction.response.send_message = AsyncMock()
    interaction.response.send_modal = AsyncMock()
    interaction.response.edit_message = AsyncMock()
    return interaction


def ephemeral_message() -> Any:
    return SimpleNamespace(flags=SimpleNamespace(ephemeral=True))


class TestEventCommandGroup:
    def test_registers_event_command_group(self) -> None:
        group = EventCommands(make_bot())
        commands = {command.name for command in group.commands}

        assert group.name == "event"
        assert group.guild_only
        assert commands == {"new"}

    async def test_new_rejects_users_without_the_create_role(self) -> None:
        group = EventCommands(make_bot())
        interaction = make_interaction()

        await cast(Any, group.new.callback)(group, interaction)

        interaction.response.send_message.assert_awaited_once()
        assert interaction.response.send_message.await_args is not None
        kwargs = interaction.response.send_message.await_args.kwargs
        assert kwargs["ephemeral"] is True
        interaction.response.send_modal.assert_not_awaited()

    async def test_new_opens_the_details_modal_for_authorized_users(
        self,
    ) -> None:
        group = EventCommands(make_bot())
        interaction = make_interaction(role_ids=(EVENT_CREATE_ROLE_ID,))

        await cast(Any, group.new.callback)(group, interaction)

        interaction.response.send_modal.assert_awaited_once()
        assert interaction.response.send_modal.await_args is not None
        modal = interaction.response.send_modal.await_args.args[0]
        assert isinstance(modal, EventDetailsModal)


class TestEventDraft:
    def test_incomplete_draft_reports_missing_fields(self) -> None:
        draft = EventDraft(leader_discord_id=42)

        assert not draft.is_complete()
        with pytest.raises(ValueError, match="missing required fields"):
            draft.to_event()

    def test_complete_draft_builds_an_event(self) -> None:
        draft = EventDraft(
            leader_discord_id=42,
            category=EventCategory.RAID,
            title="Kitty Cleanup",
            description="Bring food.",
            channel_id=1234,
            start_time=datetime(2107, 1, 30, 20, 0, tzinfo=UTC),
            duration_minutes=90,
            repeat_frequency=RepeatFrequency.WEEKLY,
            repeat_days=(6,),
        )

        assert draft.is_complete()
        event = draft.to_event(event_id=9)
        assert event.event_id == 9
        assert event.category is EventCategory.RAID
        assert event.repeat_days == (6,)


class TestEventDetailsModal:
    async def test_submit_stores_details_and_offers_step_two(self) -> None:
        draft = EventDraft(leader_discord_id=42)
        modal = EventDetailsModal(make_bot(), draft)
        modal.category._values = ["Raid"]
        modal.title_input._value = "  Kitty Cleanup  "
        modal.description_input._value = "Bring food."
        cast(Any, modal.channel)._values = [SimpleNamespace(id=1234)]
        interaction = make_interaction()

        await modal.on_submit(interaction)

        assert draft.category is EventCategory.RAID
        assert draft.title == "Kitty Cleanup"
        assert draft.description == "Bring food."
        assert draft.channel_id == 1234
        interaction.response.send_message.assert_awaited_once()
        assert interaction.response.send_message.await_args is not None
        kwargs = interaction.response.send_message.await_args.kwargs
        assert kwargs["ephemeral"] is True
        assert "Step 2" in interaction.response.send_message.await_args.args[0]


class TestEventScheduleModal:
    def make_draft(self) -> EventDraft:
        return EventDraft(
            leader_discord_id=42,
            category=EventCategory.FRACTAL,
            title="Kitty Cleanup",
            description="Bring food.",
            channel_id=1234,
        )

    async def test_submit_without_repeat_shows_the_preview(self) -> None:
        draft = self.make_draft()
        modal = EventScheduleModal(make_bot(), draft)
        modal.start_input._value = FUTURE_START_TEXT
        modal.duration_input._value = "01:30"
        modal.repeat._values = ["no"]
        interaction = make_interaction()

        await modal.on_submit(interaction)

        assert draft.start_time == datetime(2107, 1, 30, 20, 0, tzinfo=UTC)
        assert draft.duration_minutes == 90
        assert draft.repeat_frequency is RepeatFrequency.NONE
        interaction.response.send_message.assert_awaited_once()
        assert interaction.response.send_message.await_args is not None
        kwargs = interaction.response.send_message.await_args.kwargs
        assert len(kwargs["embeds"]) == 2

    async def test_submit_with_repeat_offers_step_three(self) -> None:
        draft = self.make_draft()
        modal = EventScheduleModal(make_bot(), draft)
        modal.start_input._value = FUTURE_START_TEXT
        modal.duration_input._value = "01:30"
        modal.repeat._values = ["yes"]
        interaction = make_interaction(message=ephemeral_message())

        await modal.on_submit(interaction)

        interaction.response.edit_message.assert_awaited_once()
        assert interaction.response.edit_message.await_args is not None
        kwargs = interaction.response.edit_message.await_args.kwargs
        assert "Step 3" in kwargs["content"]

    async def test_submit_rejects_past_start_times_with_retry(self) -> None:
        draft = self.make_draft()
        modal = EventScheduleModal(make_bot(), draft)
        modal.start_input._value = "01.30.2007 20:00"
        modal.duration_input._value = "01:30"
        modal.repeat._values = ["no"]
        interaction = make_interaction()

        await modal.on_submit(interaction)

        assert draft.start_time is None
        interaction.response.send_message.assert_awaited_once()
        assert interaction.response.send_message.await_args is not None
        text = interaction.response.send_message.await_args.args[0]
        assert "in the future" in text
        assert "Try again" in text

    async def test_submit_rejects_bad_duration_with_retry(self) -> None:
        draft = self.make_draft()
        modal = EventScheduleModal(make_bot(), draft)
        modal.start_input._value = FUTURE_START_TEXT
        modal.duration_input._value = "ninety"
        modal.repeat._values = ["no"]
        interaction = make_interaction()

        await modal.on_submit(interaction)

        assert draft.duration_minutes is None
        interaction.response.send_message.assert_awaited_once()


class TestEventRepeatModal:
    def make_draft(self) -> EventDraft:
        return EventDraft(
            leader_discord_id=42,
            category=EventCategory.FRACTAL,
            title="Kitty Cleanup",
            description="Bring food.",
            channel_id=1234,
            start_time=datetime(2107, 1, 30, 20, 0, tzinfo=UTC),
            start_text=FUTURE_START_TEXT,
            duration_minutes=90,
            duration_text="01:30",
            repeat_frequency=RepeatFrequency.DAILY,
        )

    async def test_submit_weekly_days_shows_the_preview(self) -> None:
        draft = self.make_draft()
        modal = EventRepeatModal(make_bot(), draft)
        modal.frequency._values = ["weekly"]
        modal.days_input._value = "Sunday, Wednesday"
        interaction = make_interaction()

        await modal.on_submit(interaction)

        assert draft.repeat_frequency is RepeatFrequency.WEEKLY
        assert draft.repeat_days == (2, 6)
        interaction.response.send_message.assert_awaited_once()

    async def test_submit_invalid_days_offers_retry(self) -> None:
        draft = self.make_draft()
        modal = EventRepeatModal(make_bot(), draft)
        modal.frequency._values = ["monthly"]
        modal.days_input._value = "first"
        interaction = make_interaction()

        await modal.on_submit(interaction)

        assert draft.repeat_days == ()
        interaction.response.send_message.assert_awaited_once()
        assert interaction.response.send_message.await_args is not None
        assert (
            "Try again"
            in interaction.response.send_message.await_args.args[0]
        )


class TestSignupViews:
    def test_build_signup_view_is_persistent(self) -> None:
        view = build_signup_view(9)

        assert view.timeout is None
        assert len(view.children) == 3

    def test_signup_button_round_trips_through_custom_id(self) -> None:
        button = EventSignUpButton(9)

        assert button.occurrence_id == 9
        assert button.item.custom_id == "gw2bot:event-signup:9"
        assert button.template.match("gw2bot:event-signup:9") is not None

    def test_signup_summary_describes_flex_fallback(self) -> None:
        summary = _signup_summary(
            SimpleNamespace(
                waitlisted=False,
                assigned_role=EventRole.ALACRITY_DPS,
                role=EventRole.QUICKNESS_DPS,
            )  # type: ignore[arg-type]
        )

        assert "Alacrity DPS" in summary
        assert "flex" in summary

    def test_signup_summary_describes_waitlisting(self) -> None:
        summary = _signup_summary(
            SimpleNamespace(
                waitlisted=True,
                assigned_role=None,
                role=EventRole.DPS,
            )  # type: ignore[arg-type]
        )

        assert "waitlist" in summary
