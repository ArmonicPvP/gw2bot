from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import discord
import pytest
from sqlalchemy.exc import SQLAlchemyError

from gw2bot.events.commands import EventCommands
from gw2bot.events.posting import post_occurrence
from gw2bot.events.roles import EVENT_CREATE_ROLE_ID
from gw2bot.events.scheduler import run_event_maintenance
from gw2bot.events.models import (
    AutoSignupChoice,
    EventCategory,
    EventRole,
    EventStatus,
    RepeatFrequency,
)
from gw2bot.events.store import EventStore
from gw2bot.events.views import (
    AutoSignupChoiceView,
    ChannelMoveConfirmView,
    EventConfirmView,
    EventDeleteConfirmView,
    EventDetailsModal,
    EventDraft,
    EventEditConfirmView,
    EventFieldEditModal,
    EventRepeatModal,
    EventScheduleModal,
    EventSignOutButton,
    EventSignUpButton,
    RolePickSelect,
    SignOutConfirmView,
    SignupFlow,
    _signup_summary,
    build_signup_view,
    draft_from_event,
)

from factories import forbidden_error
from test_event_posting import FakeBot, FakeChannel, FakeThread

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
        assert commands == {"new", "edit", "delete"}

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


class TestModalComponentLimits:
    """Discord rejects an over-long label with a 400 at send_modal time.

    Nothing in the type system or the library catches it, so every modal in
    the event flow is built here and measured against Discord's limits.
    """

    # https://discord.com/developers/docs/components/reference
    LABEL_MAX_LENGTH = 45
    DESCRIPTION_MAX_LENGTH = 100

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

    def modals(self) -> list[discord.ui.Modal]:
        bot = make_bot()
        return [
            EventDetailsModal(bot, self.make_draft()),
            EventScheduleModal(bot, self.make_draft()),
            EventRepeatModal(bot, self.make_draft()),
            *(
                EventFieldEditModal(bot, self.make_draft(), field_name)
                for field_name in ("title", "description", "start", "duration")
            ),
        ]

    def test_labels_are_within_discord_limits(self) -> None:
        for modal in self.modals():
            labels = [
                item
                for item in modal.children
                if isinstance(item, discord.ui.Label)
            ]
            assert labels
            for label in labels:
                assert 1 <= len(label.text) <= self.LABEL_MAX_LENGTH, (
                    f"{type(modal).__name__} label {label.text!r} is "
                    f"{len(label.text)} characters"
                )
                if label.description is not None:
                    assert (
                        1
                        <= len(label.description)
                        <= self.DESCRIPTION_MAX_LENGTH
                    ), (
                        f"{type(modal).__name__} description "
                        f"{label.description!r} is "
                        f"{len(label.description)} characters"
                    )


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
        modal.delete_previous._values = ["yes"]
        interaction = make_interaction()

        await modal.on_submit(interaction)

        assert draft.repeat_frequency is RepeatFrequency.WEEKLY
        assert draft.repeat_days == (2, 6)
        assert draft.delete_previous_on_repeat is True
        interaction.response.send_message.assert_awaited_once()
        kwargs = interaction.response.send_message.await_args.kwargs
        assert "removing the previous post" in kwargs["embeds"][1].description

    async def test_submit_invalid_days_offers_retry(self) -> None:
        draft = self.make_draft()
        modal = EventRepeatModal(make_bot(), draft)
        modal.frequency._values = ["monthly"]
        modal.days_input._value = "first"
        modal.delete_previous._values = ["no"]
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
        interaction = make_interaction(
            role_ids=(EVENT_CREATE_ROLE_ID,),
            message=ephemeral_message(),
        )
        interaction.followup.send = AsyncMock()
        interaction.edit_original_response = AsyncMock()

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
        # The Post event button must be restored so the user can retry
        # from the same preview rather than restarting /event new.
        interaction.edit_original_response.assert_awaited_once_with(view=view)

        retry_interaction = make_interaction(
            role_ids=(EVENT_CREATE_ROLE_ID,),
            message=ephemeral_message(),
        )
        retry_interaction.followup.send = AsyncMock()
        retry_interaction.edit_original_response = AsyncMock()

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
        interaction = make_interaction(
            role_ids=(EVENT_CREATE_ROLE_ID,),
            message=ephemeral_message(),
        )
        interaction.followup.send = AsyncMock()

        await view.post_event.callback(interaction)

        assert draft.posted
        assert len(channel.sent) == 1
        assert len(store.get_posted_unfinished_occurrences()) == 1

    async def test_failed_save_restores_post_controls(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        draft = make_complete_draft()
        view = EventConfirmView(fake_bot, draft)
        store.create_event = MagicMock(  # type: ignore[method-assign]
            side_effect=SQLAlchemyError("boom")
        )
        interaction = make_interaction(
            role_ids=(EVENT_CREATE_ROLE_ID,),
            message=ephemeral_message(),
        )
        interaction.followup.send = AsyncMock()
        interaction.edit_original_response = AsyncMock()

        await view.post_event.callback(interaction)

        assert not draft.posted
        interaction.followup.send.assert_awaited_once()
        assert interaction.followup.send.await_args is not None
        assert (
            "could not be saved"
            in interaction.followup.send.await_args.args[0]
        )
        interaction.edit_original_response.assert_awaited_once_with(view=view)

    async def test_post_rejected_when_creator_role_revoked(
        self,
        fake_bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        draft = make_complete_draft()
        view = EventConfirmView(fake_bot, draft)
        # The preview was opened earlier, but the creator role is gone now.
        interaction = make_interaction(message=ephemeral_message())

        await view.post_event.callback(interaction)

        assert not draft.posted
        assert channel.sent == []
        assert store.get_unposted_occurrences() == []
        interaction.response.send_message.assert_awaited_once()
        assert interaction.response.send_message.await_args is not None
        assert (
            "required role"
            in interaction.response.send_message.await_args.args[0]
        )


class TestRolePickSelect:
    def _make_role_flow(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> SignupFlow:
        event = store.create_event(
            category=EventCategory.RAID,
            title="Full quickness",
            description="Bring food.",
            channel_id=1234,
            leader_discord_id=42,
            start_time=datetime(2107, 1, 30, 20, 0, tzinfo=UTC),
            duration_minutes=90,
            repeat_frequency=RepeatFrequency.NONE,
            repeat_days=(),
        )
        occurrence = store.create_occurrence(event.event_id, event.start_time)
        # Fill both quickness slots so Quickness roles are full while
        # Alacrity and plain DPS remain open.
        for user_id, role in (
            (1, EventRole.QUICKNESS_HEAL),
            (2, EventRole.QUICKNESS_DPS),
        ):
            store.add_signup(
                occurrence_id=occurrence.occurrence_id,
                discord_user_id=user_id,
                role=role,
                assigned_role=role,
                flex_roles=(),
                waitlisted=False,
            )
        return SignupFlow(fake_bot, event, occurrence, 42)

    def test_offers_full_roles_alongside_open_roles(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        flow = self._make_role_flow(fake_bot, store)

        select = RolePickSelect(flow)
        labels = {option.value: option.label for option in select.options}

        # Every role is selectable so a full preferred role can fall back to
        # an open flex role (or waitlist for a specific role).
        assert set(labels) == {role.value for role in EventRole}
        assert labels[EventRole.QUICKNESS_HEAL.value] == "Quickness Heal (full)"
        assert labels[EventRole.QUICKNESS_DPS.value] == "Quickness DPS (full)"
        assert labels[EventRole.ALACRITY_HEAL.value] == "Alacrity Heal"
        assert labels[EventRole.DPS.value] == "Just DPS"

    def test_labels_all_roles_as_waitlist_when_roster_full(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        event = store.create_event(
            category=EventCategory.FRACTAL,
            title="Packed fractal",
            description="Bring food.",
            channel_id=1234,
            leader_discord_id=42,
            start_time=datetime(2107, 1, 30, 20, 0, tzinfo=UTC),
            duration_minutes=90,
            repeat_frequency=RepeatFrequency.NONE,
            repeat_days=(),
        )
        occurrence = store.create_occurrence(event.event_id, event.start_time)
        # Fractal capacity is 1 healer and 4 dps; fill every slot.
        assignments = [
            EventRole.QUICKNESS_HEAL,
            EventRole.ALACRITY_DPS,
            EventRole.DPS,
            EventRole.DPS,
            EventRole.DPS,
        ]
        for user_id, role in enumerate(assignments, start=1):
            store.add_signup(
                occurrence_id=occurrence.occurrence_id,
                discord_user_id=user_id,
                role=role,
                assigned_role=role,
                flex_roles=(),
                waitlisted=False,
            )
        flow = SignupFlow(fake_bot, event, occurrence, 99)

        select = RolePickSelect(flow)

        assert {option.value for option in select.options} == {
            role.value for role in EventRole
        }
        assert all(
            option.label.endswith("(waitlist)") for option in select.options
        )


class TestSignOutFlow:
    def _make_ended_occurrence(self, store: EventStore) -> Any:
        past_start = datetime.now(UTC) - timedelta(hours=3)
        event = store.create_event(
            category=EventCategory.WVW,
            title="Border skirmish",
            description="Bring siege.",
            channel_id=1234,
            leader_discord_id=42,
            start_time=past_start,
            duration_minutes=90,
            repeat_frequency=RepeatFrequency.NONE,
            repeat_days=(),
        )
        occurrence = store.create_occurrence(event.event_id, event.start_time)
        return event, occurrence

    async def test_confirm_after_end_keeps_roster(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = self._make_ended_occurrence(store)
        store.add_signup(
            occurrence_id=occurrence.occurrence_id,
            discord_user_id=42,
            role=None,
            assigned_role=None,
            flex_roles=(),
            waitlisted=False,
        )
        store.add_signup(
            occurrence_id=occurrence.occurrence_id,
            discord_user_id=99,
            role=None,
            assigned_role=None,
            flex_roles=(),
            waitlisted=True,
        )
        view = SignOutConfirmView(fake_bot, event, occurrence)
        interaction = make_interaction(message=ephemeral_message())

        await view.remove_me.callback(interaction)

        # The historical roster must be untouched: no removal of the active
        # participant and no promotion of the waitlisted one.
        assert store.get_signup(occurrence.occurrence_id, 42) is not None
        waitlisted = store.get_signup(occurrence.occurrence_id, 99)
        assert waitlisted is not None
        assert waitlisted.waitlisted
        interaction.response.edit_message.assert_awaited_once()
        assert interaction.response.edit_message.await_args is not None
        assert (
            "already ended"
            in interaction.response.edit_message.await_args.kwargs["content"]
        )

    async def test_button_after_end_does_not_open_confirmation(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = self._make_ended_occurrence(store)
        store.add_signup(
            occurrence_id=occurrence.occurrence_id,
            discord_user_id=42,
            role=None,
            assigned_role=None,
            flex_roles=(),
            waitlisted=False,
        )
        button = EventSignOutButton(occurrence.occurrence_id)
        interaction = make_interaction(message=ephemeral_message())
        interaction.client = fake_bot

        await button.callback(interaction)

        interaction.response.send_message.assert_awaited_once()
        assert interaction.response.send_message.await_args is not None
        assert (
            "already ended"
            in interaction.response.send_message.await_args.args[0]
        )


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


FAR_FUTURE = datetime(2107, 1, 30, 20, 0, tzinfo=UTC)


def make_edit_event(store: EventStore, channel_id: int = 1234) -> Any:
    return store.create_event(
        category=EventCategory.FRACTAL,
        title="Original Title",
        description="Original description.",
        channel_id=channel_id,
        leader_discord_id=42,
        start_time=FAR_FUTURE,
        duration_minutes=90,
        repeat_frequency=RepeatFrequency.NONE,
        repeat_days=(),
    )


def make_posted_edit_event(
    store: EventStore,
    channel_id: int = 1234,
) -> Any:
    event = make_edit_event(store, channel_id)
    occurrence = store.create_occurrence(event.event_id, event.start_time)
    store.set_occurrence_message(occurrence.occurrence_id, 1234, 555, 777)
    return event, occurrence


def make_ongoing_edit_event(store: EventStore) -> Any:
    # A recurring event that started ten minutes ago and is still running.
    started = datetime.now(UTC) - timedelta(minutes=10)
    event = store.create_event(
        category=EventCategory.FRACTAL,
        title="Ongoing Title",
        description="Original description.",
        channel_id=1234,
        leader_discord_id=42,
        start_time=started,
        duration_minutes=90,
        repeat_frequency=RepeatFrequency.DAILY,
        repeat_days=(),
    )
    occurrence = store.create_occurrence(event.event_id, started)
    store.set_occurrence_message(occurrence.occurrence_id, 1234, 555, 777)
    store.set_occurrence_status(occurrence.occurrence_id, EventStatus.ONGOING)
    return event, occurrence


# A recurring series whose live occurrence (SERIES_WEEK4) has advanced past the
# series origin (SERIES_ORIGIN), reproducing the divergence that caused the
# spurious-reschedule bug.
SERIES_ORIGIN = datetime(2107, 1, 6, 20, 0, tzinfo=UTC)
SERIES_WEEK4 = datetime(2107, 1, 27, 20, 0, tzinfo=UTC)


def make_advanced_recurring_event(
    store: EventStore,
    channel_id: int = 1234,
    *,
    posted: bool = True,
) -> Any:
    event = store.create_event(
        category=EventCategory.FRACTAL,
        title="Weekly clear",
        description="Bring food.",
        channel_id=channel_id,
        leader_discord_id=42,
        start_time=SERIES_ORIGIN,
        duration_minutes=90,
        repeat_frequency=RepeatFrequency.WEEKLY,
        repeat_days=(0,),
    )
    occurrence = store.create_occurrence(event.event_id, SERIES_WEEK4)
    if posted:
        store.set_occurrence_message(occurrence.occurrence_id, 1234, 555, 777)
    return event, occurrence


class TestEditCommand:
    async def test_edit_rejects_users_without_the_create_role(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        group = EventCommands(fake_bot)
        event, _ = make_posted_edit_event(store)
        interaction = make_interaction()

        await cast(Any, group.edit.callback)(group, interaction, event.event_id)

        interaction.response.send_message.assert_awaited_once()
        assert interaction.response.send_message.await_args is not None
        kwargs = interaction.response.send_message.await_args.kwargs
        assert kwargs["ephemeral"] is True
        # An error, not a preview.
        assert "embeds" not in kwargs

    async def test_edit_rejects_unknown_event(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        group = EventCommands(fake_bot)
        interaction = make_interaction(role_ids=(EVENT_CREATE_ROLE_ID,))

        await cast(Any, group.edit.callback)(group, interaction, 999)

        interaction.response.send_message.assert_awaited_once()
        assert interaction.response.send_message.await_args is not None
        assert (
            "does not exist or is over"
            in interaction.response.send_message.await_args.args[0]
        )

    async def test_edit_rejects_completed_event(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        group = EventCommands(fake_bot)
        event, occurrence = make_posted_edit_event(store)
        store.set_occurrence_status(
            occurrence.occurrence_id,
            EventStatus.OVER,
        )
        interaction = make_interaction(role_ids=(EVENT_CREATE_ROLE_ID,))

        await cast(Any, group.edit.callback)(group, interaction, event.event_id)

        interaction.response.send_message.assert_awaited_once()
        assert interaction.response.send_message.await_args is not None
        assert (
            "does not exist or is over"
            in interaction.response.send_message.await_args.args[0]
        )

    async def test_edit_opens_preview_for_an_active_event(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        group = EventCommands(fake_bot)
        event, _ = make_posted_edit_event(store)
        interaction = make_interaction(role_ids=(EVENT_CREATE_ROLE_ID,))

        await cast(Any, group.edit.callback)(group, interaction, event.event_id)

        interaction.response.send_message.assert_awaited_once()
        assert interaction.response.send_message.await_args is not None
        kwargs = interaction.response.send_message.await_args.kwargs
        assert len(kwargs["embeds"]) == 2
        assert isinstance(kwargs["view"], EventEditConfirmView)

    async def test_edit_preview_uses_the_live_occurrence_date(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        group = EventCommands(fake_bot)
        event, _ = make_advanced_recurring_event(store)
        interaction = make_interaction(role_ids=(EVENT_CREATE_ROLE_ID,))

        await cast(Any, group.edit.callback)(group, interaction, event.event_id)

        # The preview must show the upcoming occurrence's date (week 4), not the
        # series origin (week 1) stored on the event.
        kwargs = interaction.response.send_message.await_args.kwargs
        preview = kwargs["embeds"][0]
        date_field = next(
            field for field in preview.fields if field.name == "📅 Date & Time"
        )
        assert date_field.value == f"<t:{int(SERIES_WEEK4.timestamp())}:f>"

    async def test_autocomplete_lists_only_active_events(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        group = EventCommands(fake_bot)
        active, _ = make_posted_edit_event(store)
        completed, completed_occurrence = make_posted_edit_event(store)
        store.set_occurrence_status(
            completed_occurrence.occurrence_id,
            EventStatus.OVER,
        )
        interaction = make_interaction(role_ids=(EVENT_CREATE_ROLE_ID,))

        choices = await group.active_event_id_autocomplete(interaction, "")

        values = [choice.value for choice in choices]
        assert active.event_id in values
        assert completed.event_id not in values

    async def test_autocomplete_filters_by_query(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        group = EventCommands(fake_bot)
        wing = store.create_event(
            category=EventCategory.RAID,
            title="Wing seven",
            description="Bring food.",
            channel_id=1234,
            leader_discord_id=42,
            start_time=FAR_FUTURE,
            duration_minutes=90,
            repeat_frequency=RepeatFrequency.NONE,
            repeat_days=(),
        )
        store.create_occurrence(wing.event_id, FAR_FUTURE)
        dailies = store.create_event(
            category=EventCategory.FRACTAL,
            title="Daily fractals",
            description="Bring food.",
            channel_id=1234,
            leader_discord_id=42,
            start_time=FAR_FUTURE,
            duration_minutes=90,
            repeat_frequency=RepeatFrequency.NONE,
            repeat_days=(),
        )
        store.create_occurrence(dailies.event_id, FAR_FUTURE)
        interaction = make_interaction(role_ids=(EVENT_CREATE_ROLE_ID,))

        choices = await group.active_event_id_autocomplete(interaction, "wing")

        assert [choice.value for choice in choices] == [wing.event_id]

    async def test_autocomplete_returns_nothing_for_unauthorized_users(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        group = EventCommands(fake_bot)
        make_posted_edit_event(store)
        interaction = make_interaction()

        choices = await group.active_event_id_autocomplete(interaction, "")

        assert choices == []


class TestEditCommandOngoing:
    async def test_edit_rejects_an_ongoing_event(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        group = EventCommands(fake_bot)
        event, _ = make_ongoing_edit_event(store)
        interaction = make_interaction(role_ids=(EVENT_CREATE_ROLE_ID,))

        await cast(Any, group.edit.callback)(
            group, interaction, event.event_id
        )

        # An ongoing event can only be deleted, so no preview is opened.
        interaction.response.send_message.assert_awaited_once()
        kwargs = interaction.response.send_message.await_args.kwargs
        assert "view" not in kwargs
        message = interaction.response.send_message.await_args.args[0]
        assert "already started" in message
        assert "/event delete" in message


class TestEventEditConfirmView:
    async def test_save_changes_updates_event_and_refreshes_message(
        self,
        fake_bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event, _ = make_posted_edit_event(store)
        draft = draft_from_event(event, ZoneInfo("UTC"))
        draft.title = "Edited Title"
        view = EventEditConfirmView(fake_bot, draft)
        interaction = make_interaction(
            role_ids=(EVENT_CREATE_ROLE_ID,),
            message=ephemeral_message(),
        )
        interaction.edit_original_response = AsyncMock()

        await view.save_changes.callback(interaction)

        updated = store.get_event(event.event_id)
        assert updated is not None
        assert updated.title == "Edited Title"
        channel.partial_message.edit.assert_awaited()
        interaction.edit_original_response.assert_awaited()
        assert interaction.edit_original_response.await_args is not None
        assert (
            "was updated"
            in interaction.edit_original_response.await_args.kwargs["content"]
        )

    async def test_save_changes_reschedules_the_posted_occurrence(
        self,
        fake_bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event, occurrence = make_posted_edit_event(store)
        draft = draft_from_event(event, ZoneInfo("UTC"))
        new_start = datetime(2107, 2, 5, 21, 0, tzinfo=UTC)
        draft.start_time = new_start
        draft.start_text = "02.05.2107 21:00"
        view = EventEditConfirmView(fake_bot, draft)
        interaction = make_interaction(
            role_ids=(EVENT_CREATE_ROLE_ID,),
            message=ephemeral_message(),
        )
        interaction.edit_original_response = AsyncMock()

        await view.save_changes.callback(interaction)

        rescheduled = store.get_occurrence(occurrence.occurrence_id)
        assert rescheduled is not None
        assert rescheduled.start_time == new_start
        # The reschedule forces the thread name to update.
        channel.thread.edit.assert_awaited()

    async def test_editing_recurring_event_does_not_reschedule_on_no_date_change(
        self,
        fake_bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        # Drive the whole flow through the command so the draft is hydrated the
        # way production does; a regression in either the hydration source or
        # the reschedule guard drags the occurrence back to the series origin.
        group = EventCommands(fake_bot)
        event, occurrence = make_advanced_recurring_event(store)
        open_interaction = make_interaction(role_ids=(EVENT_CREATE_ROLE_ID,))
        await cast(Any, group.edit.callback)(
            group, open_interaction, event.event_id
        )
        open_args = open_interaction.response.send_message.await_args
        assert open_args is not None
        view = open_args.kwargs["view"]
        assert isinstance(view, EventEditConfirmView)

        # Change only the title, then save through the real preview view.
        view._draft.title = "Renamed clear"
        save_interaction = make_interaction(
            role_ids=(EVENT_CREATE_ROLE_ID,),
            message=ephemeral_message(),
        )
        save_interaction.edit_original_response = AsyncMock()
        await view.save_changes.callback(save_interaction)

        # The upcoming occurrence must NOT be dragged back to the series origin.
        reloaded = store.get_occurrence(occurrence.occurrence_id)
        assert reloaded is not None
        assert reloaded.start_time == SERIES_WEEK4
        assert store.get_event(event.event_id).title == "Renamed clear"  # type: ignore[union-attr]

    async def test_editing_recurring_event_preserves_the_series_origin(
        self,
        fake_bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        # The draft is seeded with the *live occurrence's* start, which has long
        # since advanced past the series origin. Writing it back into the event
        # row would drag the origin forward on every edit until it no longer
        # records when the series began.
        event, _ = make_advanced_recurring_event(store)
        draft = draft_from_event(
            event,
            ZoneInfo("UTC"),
            start_time_override=SERIES_WEEK4,
        )
        draft.title = "Renamed clear"
        view = EventEditConfirmView(fake_bot, draft)
        interaction = make_interaction(
            role_ids=(EVENT_CREATE_ROLE_ID,),
            message=ephemeral_message(),
        )
        interaction.edit_original_response = AsyncMock()

        await view.save_changes.callback(interaction)

        reloaded = store.get_event(event.event_id)
        assert reloaded is not None
        assert reloaded.title == "Renamed clear"
        assert reloaded.start_time == SERIES_ORIGIN

    async def test_rescheduling_a_series_shifts_the_origin_by_the_same_delta(
        self,
        fake_bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        # Moving the occurrence an hour later moves the origin an hour later
        # too, so the origin keeps describing the same series rather than being
        # overwritten with the occurrence's absolute date.
        event, occurrence = make_advanced_recurring_event(store)
        draft = draft_from_event(
            event,
            ZoneInfo("UTC"),
            start_time_override=SERIES_WEEK4,
        )
        new_start = SERIES_WEEK4 + timedelta(hours=1)
        draft.start_time = new_start
        draft.start_text = "01.27.2107 21:00"
        view = EventEditConfirmView(fake_bot, draft)
        interaction = make_interaction(
            role_ids=(EVENT_CREATE_ROLE_ID,),
            message=ephemeral_message(),
        )
        interaction.edit_original_response = AsyncMock()

        await view.save_changes.callback(interaction)

        rescheduled = store.get_occurrence(occurrence.occurrence_id)
        assert rescheduled is not None
        assert rescheduled.start_time == new_start
        reloaded = store.get_event(event.event_id)
        assert reloaded is not None
        assert reloaded.start_time == SERIES_ORIGIN + timedelta(hours=1)

    async def test_editing_date_reschedules_an_unposted_occurrence(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = make_advanced_recurring_event(store, posted=False)
        draft = draft_from_event(
            event,
            ZoneInfo("UTC"),
            start_time_override=occurrence.start_time,
        )
        new_start = datetime(2107, 2, 3, 20, 0, tzinfo=UTC)
        draft.start_time = new_start
        draft.start_text = "02.03.2107 20:00"
        view = EventEditConfirmView(fake_bot, draft)
        interaction = make_interaction(
            role_ids=(EVENT_CREATE_ROLE_ID,),
            message=ephemeral_message(),
        )
        interaction.edit_original_response = AsyncMock()

        await view.save_changes.callback(interaction)

        # The as-yet-unposted occurrence must be rescheduled so the scheduler
        # posts it at the new time.
        reloaded = store.get_occurrence(occurrence.occurrence_id)
        assert reloaded is not None
        assert reloaded.start_time == new_start

    async def test_save_changes_ignores_a_racing_second_click(
        self,
        fake_bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event, occurrence = make_posted_edit_event(store)
        draft = draft_from_event(
            event,
            ZoneInfo("UTC"),
            start_time_override=occurrence.start_time,
        )
        draft.title = "First save"
        view = EventEditConfirmView(fake_bot, draft)
        first = make_interaction(
            role_ids=(EVENT_CREATE_ROLE_ID,),
            message=ephemeral_message(),
        )
        first.edit_original_response = AsyncMock()

        await view.save_changes.callback(first)
        assert draft.edit_applied

        # A second click on the same (already-applied) draft must be a no-op.
        draft.title = "Second save"
        second = make_interaction(
            role_ids=(EVENT_CREATE_ROLE_ID,),
            message=ephemeral_message(),
        )
        second.edit_original_response = AsyncMock()

        await view.save_changes.callback(second)

        assert store.get_event(event.event_id).title == "First save"  # type: ignore[union-attr]
        second.response.send_message.assert_awaited_once()
        assert (
            "already updated"
            in second.response.send_message.await_args.args[0]
        )

    async def test_channel_move_keeps_the_old_post_when_the_repost_fails(
        self,
        store: EventStore,
    ) -> None:
        old_channel = FakeChannel(channel_id=1234, thread=FakeThread(777))
        new_channel = FakeChannel(channel_id=5678, thread=FakeThread(888))
        bot = cast(Any, FakeBot(store, old_channel))
        bot._channels[new_channel.id] = new_channel
        bot._channels[new_channel.thread.id] = new_channel.thread
        event = make_edit_event(store, channel_id=old_channel.id)
        occurrence = store.create_occurrence(event.event_id, event.start_time)
        posted = await post_occurrence(bot, event, occurrence)
        draft = draft_from_event(
            event,
            ZoneInfo("UTC"),
            start_time_override=occurrence.start_time,
        )
        draft.channel_id = new_channel.id
        new_channel.send_error = forbidden_error(50001)
        view = ChannelMoveConfirmView(bot, draft, old_channel.id)
        interaction = make_interaction(
            role_ids=(EVENT_CREATE_ROLE_ID,),
            message=ephemeral_message(),
        )
        interaction.edit_original_response = AsyncMock()

        await view.move.callback(interaction)

        # The move failed, so the only public post must survive in the old
        # channel and the stored channel must be put back to match it. Leaving
        # channel_id on the new channel would send the next scheduler refresh
        # looking for this message there, get NotFound and retire a live event.
        old_channel.partial_message.delete.assert_not_awaited()
        assert store.get_event(event.event_id).channel_id == old_channel.id  # type: ignore[union-attr]
        stored = store.get_occurrence(occurrence.occurrence_id)
        assert stored is not None
        assert stored.message_id == posted.message_id
        assert stored.status is not EventStatus.OVER
        assert interaction.edit_original_response.await_args is not None
        content = interaction.edit_original_response.await_args.kwargs["content"]
        assert "stays in the current one" in content

    async def test_save_changes_refuses_an_event_that_started_during_preview(
        self,
        fake_bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        # The reported failure: shortening a running recurring event's duration
        # so that start + duration is already behind now. The refresh would
        # persist OVER without seeding the next occurrence the way the scheduler
        # does, which silently ends the series. Ongoing events are not editable.
        event, occurrence = make_ongoing_edit_event(store)
        draft = draft_from_event(
            event,
            ZoneInfo("UTC"),
            start_time_override=occurrence.start_time,
        )
        draft.duration_minutes = 1
        view = EventEditConfirmView(fake_bot, draft)
        interaction = make_interaction(
            role_ids=(EVENT_CREATE_ROLE_ID,),
            message=ephemeral_message(),
        )
        interaction.edit_original_response = AsyncMock()

        await view.save_changes.callback(interaction)

        # Nothing was written and the occurrence was not retired, so the
        # scheduler still owns the OVER transition and seeds the next occurrence.
        assert store.get_event(event.event_id).duration_minutes == 90  # type: ignore[union-attr]
        stored = store.get_occurrence(occurrence.occurrence_id)
        assert stored is not None
        assert stored.status is not EventStatus.OVER
        assert store.get_event_occurrences(event.event_id) == [stored]
        channel.partial_message.edit.assert_not_awaited()
        assert interaction.edit_original_response.await_args is not None
        content = interaction.edit_original_response.await_args.kwargs["content"]
        assert "already started" in content
        assert "/event delete" in content

    async def test_category_change_reseats_the_roster(
        self,
        fake_bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event = store.create_event(
            category=EventCategory.WVW,
            title="Border Push",
            description="Bring siege.",
            channel_id=1234,
            leader_discord_id=42,
            start_time=FAR_FUTURE,
            duration_minutes=90,
            repeat_frequency=RepeatFrequency.NONE,
            repeat_days=(),
        )
        occurrence = store.create_occurrence(event.event_id, FAR_FUTURE)
        store.set_occurrence_message(occurrence.occurrence_id, 1234, 555, 777)
        # WvW has no roles, so every signup is stored without one.
        for user_id in range(1, 8):
            store.add_signup(
                occurrence_id=occurrence.occurrence_id,
                discord_user_id=user_id,
                role=None,
                assigned_role=None,
                flex_roles=(),
                waitlisted=False,
            )
        draft = draft_from_event(
            event,
            ZoneInfo("UTC"),
            start_time_override=occurrence.start_time,
        )
        draft.category = EventCategory.FRACTAL
        view = EventEditConfirmView(fake_bot, draft)
        interaction = make_interaction(
            role_ids=(EVENT_CREATE_ROLE_ID,),
            message=ephemeral_message(),
        )
        interaction.edit_original_response = AsyncMock()

        await view.save_changes.callback(interaction)

        signups = store.get_signups(occurrence.occurrence_id)
        admitted = [signup for signup in signups if not signup.waitlisted]
        waitlisted = [signup for signup in signups if signup.waitlisted]
        # A Fractal seats 1 healer and 4 DPS. Nobody picked a role in WvW, so
        # they all fall back to DPS: the first four keep seats in sign-up order
        # and the rest are waitlisted, instead of seven role-less signups the
        # capacity check would read as an empty roster and keep admitting onto.
        assert [signup.discord_user_id for signup in admitted] == [1, 2, 3, 4]
        assert len(waitlisted) == 3
        assert all(
            signup.assigned_role is EventRole.DPS for signup in admitted
        )
        # The role is materialised too, because waitlist promotion skips a
        # signup that has no role.
        assert all(signup.role is EventRole.DPS for signup in signups)
        assert all(signup.assigned_role is None for signup in waitlisted)

    async def test_failed_move_leaves_the_old_post_flagged_for_refresh(
        self,
        store: EventStore,
    ) -> None:
        old_channel = FakeChannel(channel_id=1234, thread=FakeThread(777))
        new_channel = FakeChannel(channel_id=5678, thread=FakeThread(888))
        bot = cast(Any, FakeBot(store, old_channel))
        bot._channels[new_channel.id] = new_channel
        bot._channels[new_channel.thread.id] = new_channel.thread
        event = make_edit_event(store, channel_id=old_channel.id)
        occurrence = store.create_occurrence(event.event_id, event.start_time)
        await post_occurrence(bot, event, occurrence)
        draft = draft_from_event(
            event,
            ZoneInfo("UTC"),
            start_time_override=occurrence.start_time,
        )
        # A channel move bundled with a title change.
        draft.channel_id = new_channel.id
        draft.title = "Edited Title"
        new_channel.send_error = forbidden_error(50001)
        view = ChannelMoveConfirmView(bot, draft, old_channel.id)
        interaction = make_interaction(
            role_ids=(EVENT_CREATE_ROLE_ID,),
            message=ephemeral_message(),
        )
        interaction.edit_original_response = AsyncMock()

        await view.move.callback(interaction)

        # The move failed, but the title change is committed, so the surviving
        # post in the old channel is now stale. An edit does not change the
        # status, so the scheduler would skip it forever unless it is flagged.
        assert store.get_event(event.event_id).title == "Edited Title"  # type: ignore[union-attr]
        stale = store.get_occurrence(occurrence.occurrence_id)
        assert stale is not None
        assert stale.needs_refresh
        old_channel.partial_message.edit.assert_not_awaited()

        # The next maintenance pass re-renders it in place, in the channel it
        # actually lives in.
        await run_event_maintenance(bot, FAR_FUTURE - timedelta(hours=2))

        old_channel.partial_message.edit.assert_awaited()
        new_channel.partial_message.edit.assert_not_awaited()
        recovered = store.get_occurrence(occurrence.occurrence_id)
        assert recovered is not None
        assert not recovered.needs_refresh

    async def test_save_changes_reports_a_failed_message_refresh(
        self,
        fake_bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event, occurrence = make_posted_edit_event(store)
        # refresh_occurrence_message absorbs this failure, marks the occurrence
        # dirty and returns instead of raising, so the edit flow must not report
        # the stale public message as successfully updated.
        channel.partial_message.edit = AsyncMock(
            side_effect=forbidden_error(50001)
        )
        draft = draft_from_event(event, ZoneInfo("UTC"))
        draft.title = "Edited Title"
        view = EventEditConfirmView(fake_bot, draft)
        interaction = make_interaction(
            role_ids=(EVENT_CREATE_ROLE_ID,),
            message=ephemeral_message(),
        )
        interaction.edit_original_response = AsyncMock()

        await view.save_changes.callback(interaction)

        # The event row is still saved, and the occurrence is left dirty so the
        # scheduler retries it.
        assert store.get_event(event.event_id).title == "Edited Title"  # type: ignore[union-attr]
        stored = store.get_occurrence(occurrence.occurrence_id)
        assert stored is not None
        assert stored.needs_refresh
        assert interaction.edit_original_response.await_args is not None
        content = interaction.edit_original_response.await_args.kwargs["content"]
        assert "could not be updated" in content
        assert "was updated" not in content

    async def test_save_changes_rejects_users_without_the_role(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        event, _ = make_posted_edit_event(store)
        draft = draft_from_event(event, ZoneInfo("UTC"))
        draft.title = "Sneaky Edit"
        view = EventEditConfirmView(fake_bot, draft)
        interaction = make_interaction(message=ephemeral_message())

        await view.save_changes.callback(interaction)

        interaction.response.send_message.assert_awaited_once()
        assert store.get_event(event.event_id).title == "Original Title"  # type: ignore[union-attr]

    async def test_save_changes_prompts_before_moving_a_posted_event(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        event, _ = make_posted_edit_event(store)
        draft = draft_from_event(event, ZoneInfo("UTC"))
        draft.channel_id = 5678
        view = EventEditConfirmView(fake_bot, draft)
        interaction = make_interaction(
            role_ids=(EVENT_CREATE_ROLE_ID,),
            message=ephemeral_message(),
        )

        await view.save_changes.callback(interaction)

        interaction.response.edit_message.assert_awaited_once()
        assert interaction.response.edit_message.await_args is not None
        kwargs = interaction.response.edit_message.await_args.kwargs
        assert isinstance(kwargs["view"], ChannelMoveConfirmView)
        assert "delete" in kwargs["content"].lower()
        # Nothing is saved until the move is confirmed.
        assert store.get_event(event.event_id).channel_id == 1234  # type: ignore[union-attr]

    async def test_channel_move_confirm_reposts_to_the_new_channel(
        self,
        store: EventStore,
    ) -> None:
        old_channel = FakeChannel(channel_id=1234, thread=FakeThread(777))
        new_channel = FakeChannel(channel_id=5678, thread=FakeThread(888))
        bot = cast(Any, FakeBot(store, old_channel))
        bot._channels[new_channel.id] = new_channel
        bot._channels[new_channel.thread.id] = new_channel.thread
        event = make_edit_event(store, channel_id=old_channel.id)
        occurrence = store.create_occurrence(event.event_id, event.start_time)
        await post_occurrence(bot, event, occurrence)
        draft = draft_from_event(event, ZoneInfo("UTC"))
        draft.channel_id = new_channel.id
        view = ChannelMoveConfirmView(bot, draft, old_channel.id)
        interaction = make_interaction(
            role_ids=(EVENT_CREATE_ROLE_ID,),
            message=ephemeral_message(),
        )
        interaction.edit_original_response = AsyncMock()

        await view.move.callback(interaction)

        old_channel.partial_message.delete.assert_awaited_once()
        assert len(new_channel.sent) == 1
        updated = store.get_event(event.event_id)
        assert updated is not None
        assert updated.channel_id == new_channel.id
        assert interaction.edit_original_response.await_args is not None
        assert (
            "was updated"
            in interaction.edit_original_response.await_args.kwargs["content"]
        )

    async def test_channel_move_keep_reverts_and_returns_to_preview(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        event, _ = make_posted_edit_event(store)
        draft = draft_from_event(event, ZoneInfo("UTC"))
        draft.channel_id = 5678
        view = ChannelMoveConfirmView(fake_bot, draft, 1234)
        interaction = make_interaction(
            role_ids=(EVENT_CREATE_ROLE_ID,),
            message=ephemeral_message(),
        )

        await view.keep.callback(interaction)

        assert draft.channel_id == 1234
        interaction.response.edit_message.assert_awaited_once()
        assert interaction.response.edit_message.await_args is not None
        kwargs = interaction.response.edit_message.await_args.kwargs
        assert isinstance(kwargs["view"], EventEditConfirmView)


class TestDeleteCommand:
    async def test_delete_rejects_users_without_the_create_role(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        group = EventCommands(fake_bot)
        event, _ = make_posted_edit_event(store)
        interaction = make_interaction()

        await cast(Any, group.delete.callback)(
            group, interaction, event.event_id
        )

        interaction.response.send_message.assert_awaited_once()
        kwargs = interaction.response.send_message.await_args.kwargs
        assert kwargs["ephemeral"] is True
        assert "view" not in kwargs
        # The event is untouched.
        assert store.get_event(event.event_id) is not None

    async def test_delete_rejects_unknown_event(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        group = EventCommands(fake_bot)
        interaction = make_interaction(role_ids=(EVENT_CREATE_ROLE_ID,))

        await cast(Any, group.delete.callback)(group, interaction, 999)

        interaction.response.send_message.assert_awaited_once()
        assert (
            "does not exist"
            in interaction.response.send_message.await_args.args[0]
        )

    async def test_delete_opens_confirmation_for_an_existing_event(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        group = EventCommands(fake_bot)
        event, _ = make_posted_edit_event(store)
        interaction = make_interaction(role_ids=(EVENT_CREATE_ROLE_ID,))

        await cast(Any, group.delete.callback)(
            group, interaction, event.event_id
        )

        interaction.response.send_message.assert_awaited_once()
        kwargs = interaction.response.send_message.await_args.kwargs
        assert isinstance(kwargs["view"], EventDeleteConfirmView)
        assert kwargs["ephemeral"] is True
        # Confirmation only; nothing is deleted yet.
        assert store.get_event(event.event_id) is not None


class TestEventDeleteConfirmView:
    async def test_delete_removes_event_rows_and_message(
        self,
        fake_bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event, occurrence = make_posted_edit_event(store)
        store.add_signup(
            occurrence_id=occurrence.occurrence_id,
            discord_user_id=11,
            role=EventRole.DPS,
            assigned_role=EventRole.DPS,
            flex_roles=(),
            waitlisted=False,
        )
        view = EventDeleteConfirmView(fake_bot, event)
        interaction = make_interaction(
            role_ids=(EVENT_CREATE_ROLE_ID,),
            message=ephemeral_message(),
        )
        interaction.edit_original_response = AsyncMock()

        await view.delete.callback(interaction)

        assert store.get_event(event.event_id) is None
        assert store.get_occurrence(occurrence.occurrence_id) is None
        assert store.get_signups(occurrence.occurrence_id) == []
        channel.partial_message.delete.assert_awaited_once()
        assert interaction.edit_original_response.await_args is not None
        assert (
            "was deleted"
            in interaction.edit_original_response.await_args.kwargs["content"]
        )

    async def test_delete_rejects_users_without_the_role(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        event, _ = make_posted_edit_event(store)
        view = EventDeleteConfirmView(fake_bot, event)
        interaction = make_interaction(message=ephemeral_message())

        await view.delete.callback(interaction)

        interaction.response.send_message.assert_awaited_once()
        assert store.get_event(event.event_id) is not None

    async def test_delete_ignores_a_racing_second_click(
        self,
        fake_bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event, _ = make_posted_edit_event(store)
        view = EventDeleteConfirmView(fake_bot, event)
        first = make_interaction(
            role_ids=(EVENT_CREATE_ROLE_ID,),
            message=ephemeral_message(),
        )
        first.edit_original_response = AsyncMock()

        await view.delete.callback(first)
        assert view._deleting

        second = make_interaction(
            role_ids=(EVENT_CREATE_ROLE_ID,),
            message=ephemeral_message(),
        )
        await view.delete.callback(second)

        second.response.send_message.assert_awaited_once()
        assert (
            "already being deleted"
            in second.response.send_message.await_args.args[0]
        )

    async def test_keep_cancels_without_deleting(
        self,
        fake_bot: Any,
        store: EventStore,
    ) -> None:
        event, _ = make_posted_edit_event(store)
        view = EventDeleteConfirmView(fake_bot, event)
        interaction = make_interaction(
            role_ids=(EVENT_CREATE_ROLE_ID,),
            message=ephemeral_message(),
        )

        await view.keep.callback(interaction)

        assert store.get_event(event.event_id) is not None
        interaction.response.edit_message.assert_awaited_once()
        assert (
            "not deleted"
            in interaction.response.edit_message.await_args.kwargs["content"]
        )
