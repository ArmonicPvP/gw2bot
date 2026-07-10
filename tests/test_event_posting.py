from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import discord
import pytest

from gw2bot.events.models import (
    AutoSignupChoice,
    EventCategory,
    EventRole,
    EventStatus,
    RepeatFrequency,
)
from gw2bot.events.posting import (
    apply_auto_signups,
    complete_signup,
    occurrence_status,
    post_occurrence,
    refresh_occurrence_message,
    remove_signup,
)
from gw2bot.events.store import EventStore

from factories import forbidden_error

START = datetime(2027, 1, 30, 20, 0, tzinfo=UTC)
BEFORE_START = START - timedelta(hours=2)


class FakeThread:
    def __init__(self, thread_id: int = 777):
        self.id = thread_id
        self.add_user = AsyncMock()
        self.remove_user = AsyncMock()
        self.edit = AsyncMock()


class FakeChannel:
    def __init__(self, channel_id: int = 1234, thread: FakeThread | None = None):
        self.id = channel_id
        self.thread = thread if thread is not None else FakeThread()
        self.sent: list[dict[str, Any]] = []
        self.partial_message = SimpleNamespace(edit=AsyncMock())
        self.create_thread_error: Exception | None = None
        self.send_error: Exception | None = None

    async def send(self, *, embed: Any = None, view: Any = None) -> Any:
        if self.send_error is not None:
            error = self.send_error
            self.send_error = None
            raise error
        message = SimpleNamespace(
            id=555,
            create_thread=AsyncMock(return_value=self.thread),
        )
        if self.create_thread_error is not None:
            message.create_thread = AsyncMock(
                side_effect=self.create_thread_error
            )
        self.sent.append({"embed": embed, "view": view, "message": message})
        return message

    def get_partial_message(self, message_id: int) -> Any:
        return self.partial_message


class FakeBot:
    def __init__(self, store: EventStore, channel: FakeChannel):
        self.event_store = store
        self.event_timezone = ZoneInfo("UTC")
        self._channels: dict[int, Any] = {
            channel.id: channel,
            channel.thread.id: channel.thread,
        }

    def get_channel(self, channel_id: int) -> Any:
        return self._channels.get(channel_id)

    async def fetch_channel(self, channel_id: int) -> Any:
        return self._channels[channel_id]


@pytest.fixture
def store(tmp_path: Path):
    store = EventStore(str(tmp_path / "gw2bot.db"))
    yield store
    store.close()


@pytest.fixture
def channel() -> FakeChannel:
    return FakeChannel()


@pytest.fixture
def bot(store: EventStore, channel: FakeChannel) -> Any:
    return cast(Any, FakeBot(store, channel))


def create_event(
    store: EventStore,
    category: EventCategory = EventCategory.FRACTAL,
    repeat_frequency: RepeatFrequency = RepeatFrequency.NONE,
    repeat_days: tuple[int, ...] = (),
):
    return store.create_event(
        category=category,
        title="Kitty Cleanup",
        description="Bring food.",
        channel_id=1234,
        leader_discord_id=42,
        start_time=START,
        duration_minutes=90,
        repeat_frequency=repeat_frequency,
        repeat_days=repeat_days,
    )


async def post_new_event(
    bot: Any,
    store: EventStore,
    category: EventCategory = EventCategory.FRACTAL,
    repeat_frequency: RepeatFrequency = RepeatFrequency.NONE,
    repeat_days: tuple[int, ...] = (),
):
    event = create_event(store, category, repeat_frequency, repeat_days)
    occurrence = store.create_occurrence(event.event_id, event.start_time)
    posted = await post_occurrence(bot, event, occurrence, BEFORE_START)
    return event, posted


class TestPostOccurrence:
    async def test_posts_message_with_thread_and_stores_ids(
        self,
        bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event, posted = await post_new_event(bot, store)

        assert posted.message_id == 555
        assert posted.thread_id == 777
        assert posted.status is EventStatus.OPEN
        assert len(channel.sent) == 1
        embed = channel.sent[0]["embed"]
        assert embed.footer.text == f"eventID: {event.event_id}"
        view = channel.sent[0]["view"]
        custom_ids = {
            item.item.custom_id
            for item in view.children
            if isinstance(item, discord.ui.DynamicItem)
        }
        occurrence_id = posted.occurrence_id
        assert custom_ids == {
            f"gw2bot:event-signup:{occurrence_id}",
            f"gw2bot:event-signout:{occurrence_id}",
            f"gw2bot:event-settings:{occurrence_id}",
        }

    async def test_thread_creation_failure_still_posts_the_event(
        self,
        bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        channel.create_thread_error = forbidden_error(50001)

        event, posted = await post_new_event(bot, store)

        assert posted.message_id == 555
        assert posted.thread_id is None


class TestCompleteSignup:
    async def test_assigns_role_updates_thread_and_message(
        self,
        bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event, occurrence = await post_new_event(bot, store)

        signup = await complete_signup(
            bot,
            event,
            occurrence,
            11,
            EventRole.QUICKNESS_HEAL,
            (EventRole.DPS,),
        )

        assert not signup.waitlisted
        assert signup.assigned_role is EventRole.QUICKNESS_HEAL
        channel.thread.add_user.assert_awaited_once()
        channel.partial_message.edit.assert_awaited()

    async def test_boon_capacity_forces_flex_or_waitlist(
        self,
        bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = await post_new_event(bot, store)
        await complete_signup(
            bot,
            event,
            occurrence,
            11,
            EventRole.QUICKNESS_DPS,
            (),
        )

        # Quickness is full (1/1 for fractals); the flex role is used.
        flexed = await complete_signup(
            bot,
            event,
            occurrence,
            12,
            EventRole.QUICKNESS_HEAL,
            (EventRole.ALACRITY_HEAL,),
        )
        assert flexed.assigned_role is EventRole.ALACRITY_HEAL

        # Quickness and healers are both full; no flex fits, so waitlist.
        waitlisted = await complete_signup(
            bot,
            event,
            occurrence,
            13,
            EventRole.QUICKNESS_HEAL,
            (),
        )
        assert waitlisted.waitlisted
        assert waitlisted.assigned_role is None

    async def test_wvw_signs_up_without_roles_and_waitlists_beyond_capacity(
        self,
        bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = await post_new_event(
            bot,
            store,
            category=EventCategory.WVW,
        )
        for user_id in range(1, 51):
            signup = await complete_signup(
                bot,
                event,
                occurrence,
                user_id,
                None,
                (),
            )
            assert not signup.waitlisted

        overflow = await complete_signup(bot, event, occurrence, 51, None, ())

        assert overflow.waitlisted

    async def test_instanced_event_requires_a_role(
        self,
        bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = await post_new_event(bot, store)

        with pytest.raises(ValueError, match="requires picking a role"):
            await complete_signup(bot, event, occurrence, 11, None, ())

    async def test_signup_after_event_ends_is_rejected(
        self,
        bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event, occurrence = await post_new_event(bot, store)
        after_end = START + timedelta(hours=3)

        # A view left open until after the event ends must not be able to
        # mutate the historical roster on a late click.
        with pytest.raises(ValueError, match="already ended"):
            await complete_signup(
                bot,
                event,
                occurrence,
                11,
                EventRole.DPS,
                (),
                now=after_end,
            )

        assert store.get_signups(occurrence.occurrence_id) == []
        channel.thread.add_user.assert_not_awaited()

    async def test_full_event_status_becomes_full(
        self,
        bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = await post_new_event(bot, store)
        roster = [
            (11, EventRole.QUICKNESS_HEAL),
            (12, EventRole.ALACRITY_DPS),
            (13, EventRole.DPS),
            (14, EventRole.DPS),
            (15, EventRole.DPS),
        ]
        for user_id, role in roster:
            await complete_signup(bot, event, occurrence, user_id, role, ())

        updated = store.get_occurrence(occurrence.occurrence_id)
        assert updated is not None
        signups = store.get_signups(occurrence.occurrence_id)
        assert occurrence_status(
            event,
            updated,
            signups,
            BEFORE_START,
        ) is EventStatus.FULL


class TestRemoveSignup:
    async def test_removes_signup_and_promotes_fitting_waitlisted_user(
        self,
        bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event, occurrence = await post_new_event(bot, store)
        await complete_signup(
            bot,
            event,
            occurrence,
            11,
            EventRole.QUICKNESS_HEAL,
            (),
        )
        # Healer slot is full, so this signup lands on the waitlist.
        waitlisted = await complete_signup(
            bot,
            event,
            occurrence,
            12,
            EventRole.ALACRITY_HEAL,
            (),
        )
        assert waitlisted.waitlisted

        removed, promoted = await remove_signup(bot, event, occurrence, 11)

        assert removed is not None
        assert promoted is not None
        assert promoted.discord_user_id == 12
        assert not promoted.waitlisted
        assert promoted.assigned_role is EventRole.ALACRITY_HEAL
        channel.thread.remove_user.assert_awaited_once()

    async def test_promotion_respects_boon_capacity(
        self,
        bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = await post_new_event(bot, store)
        await complete_signup(
            bot,
            event,
            occurrence,
            11,
            EventRole.QUICKNESS_HEAL,
            (),
        )
        for user_id, role in (
            (12, EventRole.ALACRITY_DPS),
            (13, EventRole.DPS),
            (14, EventRole.DPS),
            (15, EventRole.DPS),
        ):
            await complete_signup(bot, event, occurrence, user_id, role, ())
        # Quickness provider exists, so a quickness-only candidate cannot
        # be promoted into the freed pure-DPS slot.
        quickness_candidate = await complete_signup(
            bot,
            event,
            occurrence,
            16,
            EventRole.QUICKNESS_DPS,
            (),
        )
        dps_candidate = await complete_signup(
            bot,
            event,
            occurrence,
            17,
            EventRole.DPS,
            (),
        )
        assert quickness_candidate.waitlisted
        assert dps_candidate.waitlisted

        removed, promoted = await remove_signup(bot, event, occurrence, 13)

        assert removed is not None
        assert promoted is not None
        assert promoted.discord_user_id == 17

    async def test_removing_unknown_signup_returns_none(
        self,
        bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = await post_new_event(bot, store)

        removed, promoted = await remove_signup(bot, event, occurrence, 99)

        assert removed is None
        assert promoted is None


class TestApplyAutoSignups:
    async def test_applies_stored_yes_choices_with_roles(
        self,
        bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = await post_new_event(
            bot,
            store,
            repeat_frequency=RepeatFrequency.DAILY,
        )
        store.set_auto_signup(
            event.event_id,
            11,
            AutoSignupChoice.YES,
            EventRole.QUICKNESS_HEAL,
            (),
        )
        store.set_auto_signup(
            event.event_id,
            12,
            AutoSignupChoice.NO,
            EventRole.DPS,
            (),
        )
        store.set_auto_signup(
            event.event_id,
            13,
            AutoSignupChoice.YES,
            None,
            (),
        )

        applied = apply_auto_signups(bot, event, occurrence)

        assert applied == 1
        signups = store.get_signups(occurrence.occurrence_id)
        assert [signup.discord_user_id for signup in signups] == [11]
        assert signups[0].assigned_role is EventRole.QUICKNESS_HEAL

    async def test_skips_users_who_are_already_signed_up(
        self,
        bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = await post_new_event(
            bot,
            store,
            repeat_frequency=RepeatFrequency.DAILY,
        )
        await complete_signup(
            bot,
            event,
            occurrence,
            11,
            EventRole.DPS,
            (),
        )
        store.set_auto_signup(
            event.event_id,
            11,
            AutoSignupChoice.YES,
            EventRole.DPS,
            (),
        )

        assert apply_auto_signups(bot, event, occurrence) == 0


class TestRefreshOccurrenceMessage:
    async def test_status_transition_renames_thread_and_persists(
        self,
        bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event, occurrence = await post_new_event(bot, store)
        during_event = START + timedelta(minutes=10)

        status = await refresh_occurrence_message(
            bot,
            event,
            occurrence,
            during_event,
        )

        assert status is EventStatus.ONGOING
        updated = store.get_occurrence(occurrence.occurrence_id)
        assert updated is not None
        assert updated.status is EventStatus.ONGOING
        channel.thread.edit.assert_awaited_once()
        rename = channel.thread.edit.await_args
        assert rename is not None
        assert rename.kwargs["name"].startswith("🟡|")

    async def test_unchanged_status_does_not_rename_the_thread(
        self,
        bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event, occurrence = await post_new_event(bot, store)

        status = await refresh_occurrence_message(
            bot,
            event,
            occurrence,
            BEFORE_START,
        )

        assert status is EventStatus.OPEN
        channel.thread.edit.assert_not_awaited()

    async def test_failed_thread_rename_defers_status_for_retry(
        self,
        bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event, occurrence = await post_new_event(bot, store)
        during_event = START + timedelta(minutes=10)
        channel.thread.edit = AsyncMock(side_effect=forbidden_error(50001))

        status = await refresh_occurrence_message(
            bot,
            event,
            occurrence,
            during_event,
        )

        # The transition must not be committed while the thread name is still
        # stale, or the scheduler would stop retrying the rename.
        assert status is EventStatus.OPEN
        updated = store.get_occurrence(occurrence.occurrence_id)
        assert updated is not None
        assert updated.status is EventStatus.OPEN
        assert updated.needs_refresh
        assert updated.occurrence_id in {
            live.occurrence_id
            for live in store.get_posted_unfinished_occurrences()
        }

        # Once the thread rename succeeds, the transition is committed and the
        # dirty flag cleared.
        channel.thread.edit = AsyncMock()
        retry = await refresh_occurrence_message(
            bot,
            event,
            updated,
            during_event,
        )

        assert retry is EventStatus.ONGOING
        committed = store.get_occurrence(occurrence.occurrence_id)
        assert committed is not None
        assert committed.status is EventStatus.ONGOING
        assert not committed.needs_refresh
        channel.thread.edit.assert_awaited_once()

    async def test_failed_message_edit_keeps_status_for_retry(
        self,
        bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event, occurrence = await post_new_event(bot, store)
        after_end = START + timedelta(hours=3)
        channel.partial_message.edit = AsyncMock(
            side_effect=forbidden_error(50001)
        )

        status = await refresh_occurrence_message(
            bot,
            event,
            occurrence,
            after_end,
        )

        # The transition to OVER must not be persisted when the public
        # message could not be refreshed, so the scheduler keeps retrying.
        assert status is EventStatus.OPEN
        updated = store.get_occurrence(occurrence.occurrence_id)
        assert updated is not None
        assert updated.status is EventStatus.OPEN
        assert updated.needs_refresh
        assert updated.occurrence_id in {
            live.occurrence_id
            for live in store.get_posted_unfinished_occurrences()
        }
        channel.thread.edit.assert_not_awaited()

    async def test_failed_refresh_marks_dirty_when_status_unchanged(
        self,
        bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event, occurrence = await post_new_event(bot, store)
        channel.partial_message.edit = AsyncMock(
            side_effect=forbidden_error(50001)
        )

        # A roster change that leaves the status OPEN but fails to edit the
        # message must still record dirty state so the scheduler retries.
        status = await refresh_occurrence_message(
            bot,
            event,
            occurrence,
            BEFORE_START,
        )

        assert status is EventStatus.OPEN
        updated = store.get_occurrence(occurrence.occurrence_id)
        assert updated is not None
        assert updated.needs_refresh

    async def test_successful_refresh_clears_dirty_flag(
        self,
        bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event, occurrence = await post_new_event(bot, store)
        store.set_occurrence_needs_refresh(occurrence.occurrence_id, True)
        dirty = store.get_occurrence(occurrence.occurrence_id)
        assert dirty is not None and dirty.needs_refresh

        await refresh_occurrence_message(bot, event, dirty, BEFORE_START)

        cleared = store.get_occurrence(occurrence.occurrence_id)
        assert cleared is not None
        assert not cleared.needs_refresh
        channel.partial_message.edit.assert_awaited()


class TestPostingLoggingSafety:
    async def test_posting_and_signup_logs_never_contain_user_content(
        self,
        bot: Any,
        store: EventStore,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        title = "SECRET EVENT TITLE"
        description = "SECRET EVENT DESCRIPTION"
        with caplog.at_level("DEBUG"):
            event = store.create_event(
                category=EventCategory.FRACTAL,
                title=title,
                description=description,
                channel_id=1234,
                leader_discord_id=42,
                start_time=START,
                duration_minutes=90,
                repeat_frequency=RepeatFrequency.NONE,
                repeat_days=(),
            )
            occurrence = store.create_occurrence(
                event.event_id,
                event.start_time,
            )
            occurrence = await post_occurrence(
                bot,
                event,
                occurrence,
                BEFORE_START,
            )
            await complete_signup(
                bot,
                event,
                occurrence,
                11,
                EventRole.DPS,
                (),
            )
            await remove_signup(bot, event, occurrence, 11)

        assert title not in caplog.text
        assert description not in caplog.text
