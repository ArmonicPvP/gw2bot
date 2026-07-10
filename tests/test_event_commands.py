from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

from gw2bot.events.commands import EventCommands
from gw2bot.events.posting import post_occurrence
from gw2bot.events.roles import EVENT_CREATE_ROLE_ID
from gw2bot.events.models import (
    AutoSignupChoice,
    EventCategory,
    EventRole,
    RepeatFrequency,
)
from gw2bot.events.store import EventStore
from gw2bot.events.views import (
    AutoSignupChoiceView,
    EventConfirmView,
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

from factories import forbidden_error
from test_event_posting import FakeBot, FakeChannel

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


@pytest.fixture
def store(tmp_path: Path):
    store = EventStore(str(tmp_path / "gw2bot.db"))
    yield store
    store.close()


@pytest.fixture
def channel() -> FakeChannel:
    return FakeChannel()


@pytest.fixture
def fake_bot(store: EventStore, channel: FakeChannel) -> Any:
    return cast(Any, FakeBot(store, channel))


def make_complete_draft() -> EventDraft:
    return EventDraft(
        leader_discord_id=42,
        category=EventCategory.FRACTAL,
        title="Kitty Cleanup",
        description="Bring food.",
        channel_id=1234,
        start_time=datetime(2107, 1, 30, 20, 0, tzinfo=UTC),
        duration_minutes=90,
    )


class TestPostEventButton:
    async def test_failed_post_cleans_up_and_a_retry_posts_once(
        self,
        fake_bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        draft = make_complete_draft()
        view = EventConfirmView(fake_bot, draft)
        channel.send_error = forbidden_error(50001)
        interaction = make_interaction(message=ephemeral_message())
        interaction.followup.send = AsyncMock()

        await view.post_event.callback(interaction)

        assert not draft.posted
        assert channel.sent == []
        assert store.get_unposted_occurrences() == []
        interaction.followup.send.assert_awaited_once()
        assert interaction.followup.send.await_args is not None
        assert (
            "could not be posted"
            in interaction.followup.send.await_args.args[0]
        )

        retry_interaction = make_interaction(message=ephemeral_message())
        retry_interaction.followup.send = AsyncMock()

        await view.post_event.callback(retry_interaction)

        assert draft.posted
        assert len(channel.sent) == 1
        posted = store.get_posted_unfinished_occurrences()
        assert len(posted) == 1
        events = {
            store.get_event(occurrence.event_id).event_id  # type: ignore[union-attr]
            for occurrence in posted
        }
        assert len(events) == 1

    async def test_successful_post_stores_and_sends_once(
        self,
        fake_bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        draft = make_complete_draft()
        view = EventConfirmView(fake_bot, draft)
        interaction = make_interaction(message=ephemeral_message())
        interaction.followup.send = AsyncMock()

        await view.post_event.callback(interaction)

        assert draft.posted
        assert len(channel.sent) == 1
        assert len(store.get_posted_unfinished_occurrences()) == 1


class TestAutoSignupPrompt:
    async def make_flow(
        self,
        fake_bot: Any,
        store: EventStore,
        user_id: int,
    ) -> SignupFlow:
        event = store.create_event(
            category=EventCategory.FRACTAL,
            title="Kitty Cleanup",
            description="Bring food.",
            channel_id=1234,
            leader_discord_id=42,
            start_time=datetime(2107, 1, 30, 20, 0, tzinfo=UTC),
            duration_minutes=90,
            repeat_frequency=RepeatFrequency.DAILY,
            repeat_days=(),
        )
        occurrence = store.create_occurrence(
            event.event_id,
            event.start_time,
        )
        occurrence = await post_occurrence(
            fake_bot,
            event,
            occurrence,
            datetime(2107, 1, 30, 10, 0, tzinfo=UTC),
        )
        flow = SignupFlow(fake_bot, event, occurrence, user_id)
        flow.role = EventRole.DPS
        return flow

    def make_flow_interaction(self) -> Any:
        interaction = make_interaction(message=ephemeral_message())
        interaction.response.is_done = MagicMock(return_value=False)
        interaction.edit_original_response = AsyncMock()
        return interaction

    async def finalize_and_get_kwargs(
        self,
        flow: SignupFlow,
    ) -> dict[str, Any]:
        interaction = self.make_flow_interaction()
        await flow.finalize(interaction)
        interaction.edit_original_response.assert_awaited_once()
        await_args = interaction.edit_original_response.await_args
        assert await_args is not None
        return await_args.kwargs

    async def test_prompts_when_no_choice_is_stored(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        flow = await self.make_flow(fake_bot, store, 21)

        kwargs = await self.finalize_and_get_kwargs(flow)

        assert "automatically" in kwargs["content"]
        assert isinstance(kwargs["view"], AutoSignupChoiceView)

    async def test_prompts_again_after_a_plain_no(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        flow = await self.make_flow(fake_bot, store, 21)
        store.set_auto_signup(
            flow.event.event_id,
            21,
            AutoSignupChoice.NO,
            None,
            (),
        )

        kwargs = await self.finalize_and_get_kwargs(flow)

        assert "automatically" in kwargs["content"]
        assert isinstance(kwargs["view"], AutoSignupChoiceView)

    async def test_never_ask_again_suppresses_the_prompt(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        flow = await self.make_flow(fake_bot, store, 21)
        store.set_auto_signup(
            flow.event.event_id,
            21,
            AutoSignupChoice.NEVER_ASK,
            None,
            (),
        )

        kwargs = await self.finalize_and_get_kwargs(flow)

        assert "automatically" not in kwargs["content"]
        assert kwargs["view"] is None

    async def test_yes_suppresses_the_prompt(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        flow = await self.make_flow(fake_bot, store, 21)
        store.set_auto_signup(
            flow.event.event_id,
            21,
            AutoSignupChoice.YES,
            EventRole.DPS,
            (),
        )

        kwargs = await self.finalize_and_get_kwargs(flow)

        assert "automatically" not in kwargs["content"]
        assert kwargs["view"] is None


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
