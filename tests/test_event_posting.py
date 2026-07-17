from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import discord
import pytest
from sqlalchemy.exc import SQLAlchemyError

from gw2bot.events.models import (
    AutoSignupChoice,
    EventCategory,
    EventRole,
    EventStatus,
    RepeatFrequency,
    choose_assigned_role,
    is_roster_full,
)
from gw2bot.events.posting import (
    apply_auto_signups,
    complete_signup,
    delete_event_posts,
    occurrence_status,
    post_occurrence,
    prune_superseded_occurrences,
    rebalance_occurrence_roster,
    refresh_occurrence_message,
    remove_signup,
    repost_occurrence,
)
from gw2bot.events.store import EventStore

from factories import forbidden_error, not_found_error

START = datetime(2027, 1, 30, 20, 0, tzinfo=UTC)
BEFORE_START = START - timedelta(hours=2)


class FakeThread:
    def __init__(self, thread_id: int = 777):
        self.id = thread_id
        self.add_user = AsyncMock()
        self.remove_user = AsyncMock()
        self.edit = AsyncMock()
        self.delete = AsyncMock()


class FakeChannel:
    def __init__(self, channel_id: int = 1234, thread: FakeThread | None = None):
        self.id = channel_id
        self.thread = thread if thread is not None else FakeThread()
        self.sent: list[dict[str, Any]] = []
        self.partial_message = SimpleNamespace(
            edit=AsyncMock(),
            delete=AsyncMock(),
        )
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
            delete=AsyncMock(),
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
        # Discord raises NotFound for a channel that is gone, so an unknown id
        # must surface that rather than a KeyError.
        if channel_id not in self._channels:
            raise not_found_error()
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
    delete_previous_on_repeat: bool = False,
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
        delete_previous_on_repeat=delete_previous_on_repeat,
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
        # The channel is stored with the message, so a later edit or delete can
        # address it even after the event's channel has moved on.
        assert posted.channel_id == 1234
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

    async def test_persistence_failure_deletes_the_orphaned_message(
        self,
        bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event = create_event(store)
        occurrence = store.create_occurrence(event.event_id, event.start_time)
        store.set_occurrence_message = MagicMock(  # type: ignore[method-assign]
            side_effect=SQLAlchemyError("database is locked")
        )

        with pytest.raises(SQLAlchemyError):
            await post_occurrence(bot, event, occurrence, BEFORE_START)

        # The sent message must be removed so it is not left orphaned, and the
        # occurrence must still look unposted so a retry can re-send cleanly
        # instead of the scheduler adding a duplicate public message. Its
        # thread does not disappear on its own, so it must be deleted too.
        channel.sent[-1]["message"].delete.assert_awaited_once()
        channel.thread.delete.assert_awaited_once()
        stored = store.get_occurrence(occurrence.occurrence_id)
        assert stored is not None
        assert stored.message_id is None
        assert stored.occurrence_id in {
            entry.occurrence_id
            for entry in store.get_unposted_occurrences()
        }


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
        assert rename.kwargs["name"].startswith("🟡 |")

    async def test_over_transition_seeds_next_recurring_occurrence(
        self,
        bot: Any,
        store: EventStore,
    ) -> None:
        # A refresh driven by a roster change (not the scheduler) can be the one
        # that crosses into OVER when it lands just before start + duration. The
        # scheduler seeds the next occurrence before an OVER transition; this
        # path must do the same, or the recurring series ends silently once the
        # occurrence drops out of the unfinished set.
        event, occurrence = await post_new_event(
            bot,
            store,
            repeat_frequency=RepeatFrequency.DAILY,
        )
        after_end = START + timedelta(minutes=90)
        assert len(store.get_event_occurrences(event.event_id)) == 1

        status = await refresh_occurrence_message(
            bot,
            event,
            occurrence,
            after_end,
        )

        assert status is EventStatus.OVER
        occurrences = store.get_event_occurrences(event.event_id)
        assert len(occurrences) == 2
        seeded = next(
            item
            for item in occurrences
            if item.occurrence_id != occurrence.occurrence_id
        )
        assert seeded.start_time == START + timedelta(days=1)

    async def test_over_transition_does_not_seed_a_non_repeating_event(
        self,
        bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = await post_new_event(bot, store)
        after_end = START + timedelta(minutes=90)

        status = await refresh_occurrence_message(
            bot,
            event,
            occurrence,
            after_end,
        )

        assert status is EventStatus.OVER
        # A one-off event has no successor to seed.
        assert len(store.get_event_occurrences(event.event_id)) == 1

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

    async def test_forced_rename_updates_thread_without_status_change(
        self,
        bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event, occurrence = await post_new_event(bot, store)

        # An edit that reschedules the occurrence keeps the OPEN status but must
        # still rename the thread, whose name encodes the date and time.
        status = await refresh_occurrence_message(
            bot,
            event,
            occurrence,
            BEFORE_START,
            force_thread_rename=True,
        )

        assert status is EventStatus.OPEN
        channel.thread.edit.assert_awaited_once()
        rename = channel.thread.edit.await_args
        assert rename is not None
        assert rename.kwargs["name"].startswith("🟢 |")

    async def test_forced_rename_failure_recovers_on_scheduler_retry(
        self,
        bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event, occurrence = await post_new_event(bot, store)
        channel.thread.edit = AsyncMock(side_effect=forbidden_error(50001))

        # An edit forces a rename (status unchanged) but the rename fails
        # transiently, so the occurrence is left dirty.
        await refresh_occurrence_message(
            bot,
            event,
            occurrence,
            BEFORE_START,
            force_thread_rename=True,
        )
        dirty = store.get_occurrence(occurrence.occurrence_id)
        assert dirty is not None
        assert dirty.needs_refresh

        # The scheduler retry does NOT pass force_thread_rename, so the dirty
        # flag itself must trigger the rename; otherwise the thread name would
        # be cleared as clean while still stale.
        channel.thread.edit = AsyncMock()
        await refresh_occurrence_message(bot, event, dirty, BEFORE_START)

        channel.thread.edit.assert_awaited_once()
        cleared = store.get_occurrence(occurrence.occurrence_id)
        assert cleared is not None
        assert not cleared.needs_refresh


class TestRepostOccurrence:
    async def test_reposts_to_new_channel_and_readds_members(
        self,
        store: EventStore,
    ) -> None:
        old_channel = FakeChannel(channel_id=1234, thread=FakeThread(777))
        new_channel = FakeChannel(channel_id=5678, thread=FakeThread(888))
        bot = cast(Any, FakeBot(store, old_channel))
        bot._channels[new_channel.id] = new_channel
        bot._channels[new_channel.thread.id] = new_channel.thread

        event = create_event(store)
        occurrence = store.create_occurrence(event.event_id, event.start_time)
        posted = await post_occurrence(bot, event, occurrence, BEFORE_START)
        for user_id in (11, 12):
            store.add_signup(
                occurrence_id=posted.occurrence_id,
                discord_user_id=user_id,
                role=EventRole.DPS,
                assigned_role=EventRole.DPS,
                flex_roles=(),
                waitlisted=False,
            )
        moved = store.update_event(
            event_id=event.event_id,
            category=event.category,
            title=event.title,
            description=event.description,
            channel_id=new_channel.id,
            leader_discord_id=event.leader_discord_id,
            start_time=event.start_time,
            duration_minutes=event.duration_minutes,
            repeat_frequency=event.repeat_frequency,
            repeat_days=event.repeat_days,
        )

        reposted = await repost_occurrence(bot, moved, posted)

        # Old message and its thread deleted, fresh one sent in the new
        # channel, and every existing signup re-added to the new thread.
        old_channel.partial_message.delete.assert_awaited_once()
        old_channel.thread.delete.assert_awaited_once()
        assert len(new_channel.sent) == 1
        assert reposted.thread_id == 888
        assert new_channel.thread.add_user.await_count == 2
        stored = store.get_occurrence(posted.occurrence_id)
        assert stored is not None
        assert stored.thread_id == 888

    async def test_repost_survives_a_failed_old_message_delete(
        self,
        store: EventStore,
    ) -> None:
        old_channel = FakeChannel(channel_id=1234, thread=FakeThread(777))
        new_channel = FakeChannel(channel_id=5678, thread=FakeThread(888))
        old_channel.partial_message.delete = AsyncMock(
            side_effect=forbidden_error(50001)
        )
        bot = cast(Any, FakeBot(store, old_channel))
        bot._channels[new_channel.id] = new_channel
        bot._channels[new_channel.thread.id] = new_channel.thread

        event = create_event(store)
        occurrence = store.create_occurrence(event.event_id, event.start_time)
        posted = await post_occurrence(bot, event, occurrence, BEFORE_START)
        moved = store.update_event(
            event_id=event.event_id,
            category=event.category,
            title=event.title,
            description=event.description,
            channel_id=new_channel.id,
            leader_discord_id=event.leader_discord_id,
            start_time=event.start_time,
            duration_minutes=event.duration_minutes,
            repeat_frequency=event.repeat_frequency,
            repeat_days=event.repeat_days,
        )

        reposted = await repost_occurrence(bot, moved, posted)

        # A failed delete of the old post must not stop the move from posting
        # into the new channel, nor stop the old thread from being cleaned up.
        assert len(new_channel.sent) == 1
        assert reposted.thread_id == 888
        old_channel.thread.delete.assert_awaited_once()


    async def test_repost_keeps_the_old_post_when_the_new_post_fails(
        self,
        store: EventStore,
    ) -> None:
        old_channel = FakeChannel(channel_id=1234, thread=FakeThread(777))
        new_channel = FakeChannel(channel_id=5678, thread=FakeThread(888))
        bot = cast(Any, FakeBot(store, old_channel))
        bot._channels[new_channel.id] = new_channel
        bot._channels[new_channel.thread.id] = new_channel.thread

        event = create_event(store)
        occurrence = store.create_occurrence(event.event_id, event.start_time)
        posted = await post_occurrence(bot, event, occurrence, BEFORE_START)
        moved = store.update_event(
            event_id=event.event_id,
            category=event.category,
            title=event.title,
            description=event.description,
            channel_id=new_channel.id,
            leader_discord_id=event.leader_discord_id,
            start_time=event.start_time,
            duration_minutes=event.duration_minutes,
            repeat_frequency=event.repeat_frequency,
            repeat_days=event.repeat_days,
        )
        new_channel.send_error = forbidden_error(50001)

        with pytest.raises(discord.HTTPException):
            await repost_occurrence(bot, moved, posted)

        # The new post never went out, so the old one must still be live and
        # still referenced. Deleting it first would strand the occurrence on a
        # dead message id and cost the event its only public post.
        old_channel.partial_message.delete.assert_not_awaited()
        old_channel.thread.delete.assert_not_awaited()
        stored = store.get_occurrence(posted.occurrence_id)
        assert stored is not None
        assert stored.message_id == posted.message_id
        assert stored.thread_id == posted.thread_id


class TestRebalanceOccurrenceRoster:
    def seat(
        self,
        store: EventStore,
        occurrence_id: int,
        user_id: int,
        role: EventRole | None,
        assigned_role: EventRole | None,
        waitlisted: bool = False,
    ) -> None:
        store.add_signup(
            occurrence_id=occurrence_id,
            discord_user_id=user_id,
            role=role,
            assigned_role=assigned_role,
            flex_roles=(),
            waitlisted=waitlisted,
        )

    def test_role_less_roster_falls_back_to_dps_and_waitlists(
        self,
        bot: Any,
        store: EventStore,
    ) -> None:
        event = create_event(store, category=EventCategory.WVW)
        occurrence = store.create_occurrence(event.event_id, event.start_time)
        for user_id in range(1, 8):
            self.seat(store, occurrence.occurrence_id, user_id, None, None)
        fractal = store.update_event(
            event_id=event.event_id,
            category=EventCategory.FRACTAL,
            title=event.title,
            description=event.description,
            channel_id=event.channel_id,
            leader_discord_id=event.leader_discord_id,
            start_time=event.start_time,
            duration_minutes=event.duration_minutes,
            repeat_frequency=event.repeat_frequency,
            repeat_days=event.repeat_days,
        )

        changed = rebalance_occurrence_roster(bot, fractal, occurrence)

        assert changed == 7
        signups = store.get_signups(occurrence.occurrence_id)
        admitted = [signup for signup in signups if not signup.waitlisted]
        # Fractal seats 4 DPS; the role-less WvW roster would otherwise read as
        # zero DPS and keep admitting past capacity.
        assert [signup.discord_user_id for signup in admitted] == [1, 2, 3, 4]
        assert all(
            signup.assigned_role is EventRole.DPS for signup in admitted
        )
        assert not is_roster_full(fractal.capacity, signups)
        # A further DPS no longer fits, so the overfill is closed.
        assert (
            choose_assigned_role(
                fractal.capacity, signups, EventRole.DPS, ()
            )
            is None
        )

    def test_shrinking_capacity_waitlists_the_overflow_in_signup_order(
        self,
        bot: Any,
        store: EventStore,
    ) -> None:
        event = create_event(store, category=EventCategory.RAID)
        occurrence = store.create_occurrence(event.event_id, event.start_time)
        # A full raid roster: 2 healers and 8 DPS.
        self.seat(
            store,
            occurrence.occurrence_id,
            1,
            EventRole.QUICKNESS_HEAL,
            EventRole.QUICKNESS_HEAL,
        )
        self.seat(
            store,
            occurrence.occurrence_id,
            2,
            EventRole.ALACRITY_HEAL,
            EventRole.ALACRITY_HEAL,
        )
        for user_id in range(3, 11):
            self.seat(
                store,
                occurrence.occurrence_id,
                user_id,
                EventRole.DPS,
                EventRole.DPS,
            )
        fractal = store.update_event(
            event_id=event.event_id,
            category=EventCategory.FRACTAL,
            title=event.title,
            description=event.description,
            channel_id=event.channel_id,
            leader_discord_id=event.leader_discord_id,
            start_time=event.start_time,
            duration_minutes=event.duration_minutes,
            repeat_frequency=event.repeat_frequency,
            repeat_days=event.repeat_days,
        )

        rebalance_occurrence_roster(bot, fractal, occurrence)

        signups = store.get_signups(occurrence.occurrence_id)
        seats = {
            signup.discord_user_id: signup.assigned_role
            for signup in signups
            if not signup.waitlisted
        }
        # Fractal seats 1 healer and 4 DPS. User 1 keeps the only heal seat.
        # User 2 was the second healer and no longer fits as one, so rather than
        # being waitlisted they take the DPS fallback. That plus users 3-5 fills
        # the 4 DPS seats, and the remaining DPS drop to the waitlist in sign-up
        # order.
        assert seats == {
            1: EventRole.QUICKNESS_HEAL,
            2: EventRole.DPS,
            3: EventRole.DPS,
            4: EventRole.DPS,
            5: EventRole.DPS,
        }
        assert [
            signup.discord_user_id for signup in signups if signup.waitlisted
        ] == [6, 7, 8, 9, 10]
        assert len(signups) == 10
        assert is_roster_full(fractal.capacity, signups)

    def test_moving_to_a_role_less_category_clears_the_assignments(
        self,
        bot: Any,
        store: EventStore,
    ) -> None:
        event = create_event(store, category=EventCategory.FRACTAL)
        occurrence = store.create_occurrence(event.event_id, event.start_time)
        self.seat(
            store,
            occurrence.occurrence_id,
            1,
            EventRole.QUICKNESS_HEAL,
            EventRole.QUICKNESS_HEAL,
        )
        self.seat(
            store,
            occurrence.occurrence_id,
            2,
            EventRole.DPS,
            None,
            waitlisted=True,
        )
        wvw = store.update_event(
            event_id=event.event_id,
            category=EventCategory.WVW,
            title=event.title,
            description=event.description,
            channel_id=event.channel_id,
            leader_discord_id=event.leader_discord_id,
            start_time=event.start_time,
            duration_minutes=event.duration_minutes,
            repeat_frequency=event.repeat_frequency,
            repeat_days=event.repeat_days,
        )

        rebalance_occurrence_roster(bot, wvw, occurrence)

        signups = store.get_signups(occurrence.occurrence_id)
        # WvW seats plain headcount, so assignments are dropped and the
        # waitlisted DPS gets a seat (50 slots, 2 signups).
        assert all(signup.assigned_role is None for signup in signups)
        assert all(not signup.waitlisted for signup in signups)
        # The role preferences survive, so switching back can honour them.
        assert [signup.role for signup in signups] == [
            EventRole.QUICKNESS_HEAL,
            EventRole.DPS,
        ]


class TestDeleteEventPosts:
    async def test_deletes_posted_messages_and_skips_unposted(
        self,
        bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event = create_event(store)
        posted = store.create_occurrence(event.event_id, event.start_time)
        await post_occurrence(bot, event, posted, BEFORE_START)
        unposted = store.create_occurrence(event.event_id, event.start_time)
        occurrences = store.get_event_occurrences(event.event_id)

        deleted = await delete_event_posts(bot, event, occurrences)

        # Only the posted occurrence has a message to remove, and its thread
        # is deleted separately since it does not disappear on its own.
        assert deleted == 1
        assert unposted.message_id is None
        channel.partial_message.delete.assert_awaited_once()
        channel.thread.delete.assert_awaited_once()

    async def test_deletes_each_post_through_the_channel_it_was_posted_to(
        self,
        store: EventStore,
    ) -> None:
        old_channel = FakeChannel(channel_id=1234, thread=FakeThread(777))
        new_channel = FakeChannel(channel_id=5678, thread=FakeThread(888))
        bot = cast(Any, FakeBot(store, old_channel))
        bot._channels[new_channel.id] = new_channel
        bot._channels[new_channel.thread.id] = new_channel.thread

        event = create_event(store, repeat_frequency=RepeatFrequency.DAILY)
        old = store.create_occurrence(event.event_id, START)
        await post_occurrence(bot, event, old, BEFORE_START)
        store.set_occurrence_status(old.occurrence_id, EventStatus.OVER)
        # The event is moved to another channel. A channel edit only re-posts
        # the live occurrences, so this finished post stays behind in the old
        # channel while event.channel_id moves on.
        moved = store.update_event(
            event_id=event.event_id,
            category=event.category,
            title=event.title,
            description=event.description,
            channel_id=new_channel.id,
            leader_discord_id=event.leader_discord_id,
            start_time=event.start_time,
            duration_minutes=event.duration_minutes,
            repeat_frequency=event.repeat_frequency,
            repeat_days=event.repeat_days,
        )
        new_start = START + timedelta(days=1)
        new = store.create_occurrence(moved.event_id, new_start)
        await post_occurrence(bot, moved, new, new_start - timedelta(hours=1))
        occurrences = store.get_event_occurrences(moved.event_id)

        deleted = await delete_event_posts(bot, moved, occurrences)

        # Both posts are removed, each through the channel it actually lives in.
        # Addressing the old one through the event's current channel returns
        # NotFound and would leave it visible forever once the rows are gone.
        # Both threads are removed too, since neither disappears on its own.
        assert deleted == 2
        old_channel.partial_message.delete.assert_awaited_once()
        new_channel.partial_message.delete.assert_awaited_once()
        old_channel.thread.delete.assert_awaited_once()
        new_channel.thread.delete.assert_awaited_once()

    async def test_an_unresolvable_channel_does_not_strand_the_others(
        self,
        store: EventStore,
    ) -> None:
        old_channel = FakeChannel(channel_id=1234, thread=FakeThread(777))
        new_channel = FakeChannel(channel_id=5678, thread=FakeThread(888))
        bot = cast(Any, FakeBot(store, old_channel))
        bot._channels[new_channel.id] = new_channel
        bot._channels[new_channel.thread.id] = new_channel.thread

        event = create_event(store, repeat_frequency=RepeatFrequency.DAILY)
        old = store.create_occurrence(event.event_id, START)
        await post_occurrence(bot, event, old, BEFORE_START)
        moved = store.update_event(
            event_id=event.event_id,
            category=event.category,
            title=event.title,
            description=event.description,
            channel_id=new_channel.id,
            leader_discord_id=event.leader_discord_id,
            start_time=event.start_time,
            duration_minutes=event.duration_minutes,
            repeat_frequency=event.repeat_frequency,
            repeat_days=event.repeat_days,
        )
        new_start = START + timedelta(days=1)
        new = store.create_occurrence(moved.event_id, new_start)
        await post_occurrence(bot, moved, new, new_start - timedelta(hours=1))
        # The old channel is gone (deleted by a moderator, say).
        del bot._channels[old_channel.id]
        occurrences = store.get_event_occurrences(moved.event_id)

        deleted = await delete_event_posts(bot, moved, occurrences)

        # The post in the surviving channel is still removed, thread included.
        # The old occurrence's channel could not be resolved at all, so its
        # thread delete is never attempted (a real dead parent channel takes
        # its threads with it on Discord's side).
        assert deleted == 1
        new_channel.partial_message.delete.assert_awaited_once()
        new_channel.thread.delete.assert_awaited_once()
        old_channel.thread.delete.assert_not_awaited()

    async def test_a_failed_message_delete_does_not_stop_the_others(
        self,
        bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event = create_event(store)
        first = store.create_occurrence(event.event_id, event.start_time)
        await post_occurrence(bot, event, first, BEFORE_START)
        second = store.create_occurrence(event.event_id, event.start_time)
        await post_occurrence(bot, event, second, BEFORE_START)
        channel.partial_message.delete = AsyncMock(
            side_effect=forbidden_error(50001)
        )
        occurrences = store.get_event_occurrences(event.event_id)

        deleted = await delete_event_posts(bot, event, occurrences)

        # Both message deletes were attempted even though they fail, and a
        # failed message delete does not stop the thread delete either.
        assert deleted == 0
        assert channel.partial_message.delete.await_count == 2
        assert channel.thread.delete.await_count == 2

    async def test_a_failed_thread_delete_does_not_stop_the_others(
        self,
        store: EventStore,
    ) -> None:
        old_channel = FakeChannel(channel_id=1234, thread=FakeThread(777))
        new_channel = FakeChannel(channel_id=5678, thread=FakeThread(888))
        old_channel.thread.delete = AsyncMock(side_effect=forbidden_error(50001))
        bot = cast(Any, FakeBot(store, old_channel))
        bot._channels[new_channel.id] = new_channel
        bot._channels[new_channel.thread.id] = new_channel.thread

        event = create_event(store, repeat_frequency=RepeatFrequency.DAILY)
        old = store.create_occurrence(event.event_id, START)
        await post_occurrence(bot, event, old, BEFORE_START)
        store.set_occurrence_status(old.occurrence_id, EventStatus.OVER)
        moved = store.update_event(
            event_id=event.event_id,
            category=event.category,
            title=event.title,
            description=event.description,
            channel_id=new_channel.id,
            leader_discord_id=event.leader_discord_id,
            start_time=event.start_time,
            duration_minutes=event.duration_minutes,
            repeat_frequency=event.repeat_frequency,
            repeat_days=event.repeat_days,
        )
        new_start = START + timedelta(days=1)
        new = store.create_occurrence(moved.event_id, new_start)
        await post_occurrence(bot, moved, new, new_start - timedelta(hours=1))
        occurrences = store.get_event_occurrences(moved.event_id)

        deleted = await delete_event_posts(bot, moved, occurrences)

        # The old thread's delete fails, but the message deletes (both posts)
        # still go through and the new thread is still removed.
        assert deleted == 2
        old_channel.thread.delete.assert_awaited_once()
        new_channel.thread.delete.assert_awaited_once()

    async def test_a_thread_already_gone_is_skipped_without_error(
        self,
        bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event = create_event(store)
        occurrence = store.create_occurrence(event.event_id, event.start_time)
        await post_occurrence(bot, event, occurrence, BEFORE_START)
        channel.thread.delete = AsyncMock(side_effect=not_found_error())
        occurrences = store.get_event_occurrences(event.event_id)

        deleted = await delete_event_posts(bot, event, occurrences)

        # A thread already gone (deleted by a moderator, or by Discord along
        # with its parent channel) is not a failure worth logging as an error.
        assert deleted == 1
        channel.thread.delete.assert_awaited_once()


class TestPruneSupersededOccurrences:
    async def make_series(
        self,
        bot: Any,
        store: EventStore,
        *,
        delete_previous_on_repeat: bool = True,
    ) -> Any:
        event = create_event(
            store,
            repeat_frequency=RepeatFrequency.DAILY,
            delete_previous_on_repeat=delete_previous_on_repeat,
        )
        old = store.create_occurrence(event.event_id, START)
        posted_old = await post_occurrence(bot, event, old, BEFORE_START)
        new_start = START + timedelta(days=1)
        new = store.create_occurrence(event.event_id, new_start)
        posted_new = await post_occurrence(
            bot, event, new, new_start - timedelta(hours=1)
        )
        return event, posted_old, posted_new

    async def test_removes_earlier_over_occurrences_and_their_posts(
        self,
        bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event, posted_old, posted_new = await self.make_series(bot, store)
        store.set_occurrence_status(
            posted_old.occurrence_id, EventStatus.OVER
        )

        deleted = await prune_superseded_occurrences(bot, event)

        assert deleted == 1
        assert store.get_occurrence(posted_old.occurrence_id) is None
        assert store.get_occurrence(posted_new.occurrence_id) is not None
        channel.partial_message.delete.assert_awaited()

    async def test_keeps_earlier_occurrence_that_is_not_over(
        self,
        bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event, posted_old, _ = await self.make_series(bot, store)

        deleted = await prune_superseded_occurrences(bot, event)

        # The still-live earlier occurrence must never be removed.
        assert deleted == 0
        assert store.get_occurrence(posted_old.occurrence_id) is not None
        channel.partial_message.delete.assert_not_awaited()

    async def test_no_op_for_an_event_that_did_not_opt_in(
        self,
        bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event, posted_old, _ = await self.make_series(
            bot, store, delete_previous_on_repeat=False
        )
        store.set_occurrence_status(
            posted_old.occurrence_id, EventStatus.OVER
        )

        deleted = await prune_superseded_occurrences(bot, event)

        # The opt-in is enforced inside the prune, so no caller can forget it.
        assert deleted == 0
        assert store.get_occurrence(posted_old.occurrence_id) is not None
        channel.partial_message.delete.assert_not_awaited()

    async def test_keeps_the_previous_post_until_the_next_one_is_live(
        self,
        bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event = create_event(
            store,
            repeat_frequency=RepeatFrequency.DAILY,
            delete_previous_on_repeat=True,
        )
        old = store.create_occurrence(event.event_id, START)
        posted_old = await post_occurrence(bot, event, old, BEFORE_START)
        store.set_occurrence_status(
            posted_old.occurrence_id, EventStatus.OVER
        )
        # The next occurrence is seeded but not posted yet.
        store.create_occurrence(event.event_id, START + timedelta(days=1))

        deleted = await prune_superseded_occurrences(bot, event)

        # Removing the old post before the new one is live would leave the
        # channel with no post at all.
        assert deleted == 0
        assert store.get_occurrence(posted_old.occurrence_id) is not None
        channel.partial_message.delete.assert_not_awaited()


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
            await delete_event_posts(
                bot,
                event,
                store.get_event_occurrences(event.event_id),
            )

        assert title not in caplog.text
        assert description not in caplog.text

    async def test_thread_cleanup_is_traceable_end_to_end(
        self,
        bot: Any,
        store: EventStore,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        event = create_event(store)
        occurrence = store.create_occurrence(event.event_id, event.start_time)
        posted = await post_occurrence(bot, event, occurrence, BEFORE_START)
        occurrences = store.get_event_occurrences(event.event_id)

        with caplog.at_level("DEBUG", logger="gw2bot.events.posting"):
            await delete_event_posts(bot, event, occurrences)

        # A successful thread delete is an external Discord action, so it must
        # leave a trace rather than only being visible when it fails.
        assert (
            f"Deleted event thread; occurrence_id={posted.occurrence_id}"
            in caplog.text
        )

    async def test_an_occurrence_without_a_thread_logs_the_skip(
        self,
        bot: Any,
        store: EventStore,
        channel: FakeChannel,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        channel.create_thread_error = forbidden_error(50001)
        event = create_event(store)
        occurrence = store.create_occurrence(event.event_id, event.start_time)
        posted = await post_occurrence(bot, event, occurrence, BEFORE_START)
        assert posted.thread_id is None
        occurrences = store.get_event_occurrences(event.event_id)

        with caplog.at_level("DEBUG", logger="gw2bot.events.posting"):
            await delete_event_posts(bot, event, occurrences)

        # The skip is recorded too, so a post that never got a thread is
        # distinguishable from one whose cleanup silently did nothing.
        assert (
            f"No event thread to delete; skipping; "
            f"occurrence_id={posted.occurrence_id}" in caplog.text
        )

    async def test_missing_manage_threads_logs_actionable_permission_diagnostics(
        self,
        bot: Any,
        store: EventStore,
        channel: FakeChannel,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        event = create_event(store)
        occurrence = store.create_occurrence(event.event_id, event.start_time)
        posted = await post_occurrence(bot, event, occurrence, BEFORE_START)
        channel.thread.delete = AsyncMock(side_effect=forbidden_error(50013))
        occurrences = store.get_event_occurrences(event.event_id)

        with caplog.at_level("ERROR", logger="gw2bot.events.posting"):
            await delete_event_posts(bot, event, occurrences)

        # Deleting a thread needs Manage Threads (README documents this for
        # /event channels); a deployment missing it must get a log that names
        # the permission, not just an opaque error type.
        assert (
            "Could not delete event thread; reason=missing_permissions "
            f"occurrence_id={posted.occurrence_id} "
            "required_permissions=manage_threads "
            "(type=Forbidden status=403 code=50013)" in caplog.text
        )
