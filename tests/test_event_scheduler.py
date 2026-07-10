from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from gw2bot.events.models import (
    AutoSignupChoice,
    EventCategory,
    EventRole,
    EventStatus,
    RepeatFrequency,
)
from gw2bot.events.posting import post_occurrence
from gw2bot.events.scheduler import run_event_maintenance
from gw2bot.events.store import EventStore

from test_event_posting import FakeBot, FakeChannel

START = datetime(2027, 1, 30, 20, 0, tzinfo=UTC)
BEFORE_START = START - timedelta(hours=2)
AFTER_END = START + timedelta(hours=2)


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


async def post_event(
    bot: Any,
    store: EventStore,
    repeat_frequency: RepeatFrequency = RepeatFrequency.NONE,
    repeat_days: tuple[int, ...] = (),
):
    event = store.create_event(
        category=EventCategory.FRACTAL,
        title="Kitty Cleanup",
        description="Bring food.",
        channel_id=1234,
        leader_discord_id=42,
        start_time=START,
        duration_minutes=90,
        repeat_frequency=repeat_frequency,
        repeat_days=repeat_days,
    )
    occurrence = store.create_occurrence(event.event_id, event.start_time)
    posted = await post_occurrence(bot, event, occurrence, BEFORE_START)
    return event, posted


class TestRunEventMaintenance:
    async def test_transitions_status_and_renames_thread(
        self,
        bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event, occurrence = await post_event(bot, store)

        await run_event_maintenance(bot, START + timedelta(minutes=5))

        updated = store.get_occurrence(occurrence.occurrence_id)
        assert updated is not None
        assert updated.status is EventStatus.ONGOING
        channel.thread.edit.assert_awaited_once()

    async def test_unchanged_occurrences_are_left_alone(
        self,
        bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        await post_event(bot, store)

        await run_event_maintenance(bot, BEFORE_START)

        channel.thread.edit.assert_not_awaited()
        channel.partial_message.edit.assert_not_awaited()

    async def test_finished_non_repeating_event_posts_nothing_new(
        self,
        bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event, occurrence = await post_event(bot, store)
        posted_before = len(channel.sent)

        await run_event_maintenance(bot, AFTER_END)

        updated = store.get_occurrence(occurrence.occurrence_id)
        assert updated is not None
        assert updated.status is EventStatus.OVER
        assert len(channel.sent) == posted_before
        assert not store.has_later_occurrence(
            event.event_id,
            occurrence.start_time,
        )

    async def test_finished_repeating_event_posts_the_next_occurrence(
        self,
        bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event, occurrence = await post_event(
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

        await run_event_maintenance(bot, AFTER_END)

        occurrences = store.get_posted_unfinished_occurrences()
        assert len(occurrences) == 1
        next_occurrence = occurrences[0]
        assert next_occurrence.occurrence_id != occurrence.occurrence_id
        assert next_occurrence.start_time == START + timedelta(days=1)
        assert len(channel.sent) == 2
        signups = store.get_signups(next_occurrence.occurrence_id)
        assert [signup.discord_user_id for signup in signups] == [11]
        channel.thread.add_user.assert_awaited()

    async def test_catch_up_skips_past_occurrences_after_downtime(
        self,
        bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = await post_event(
            bot,
            store,
            repeat_frequency=RepeatFrequency.DAILY,
        )
        long_after = START + timedelta(days=10, hours=3)

        await run_event_maintenance(bot, long_after)

        occurrences = store.get_posted_unfinished_occurrences()
        assert len(occurrences) == 1
        assert occurrences[0].start_time > long_after

    async def test_second_pass_does_not_duplicate_the_next_occurrence(
        self,
        bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        await post_event(
            bot,
            store,
            repeat_frequency=RepeatFrequency.DAILY,
        )

        await run_event_maintenance(bot, AFTER_END)
        await run_event_maintenance(bot, AFTER_END)

        assert len(channel.sent) == 2

    async def test_failed_recurrence_post_is_retried_on_the_next_pass(
        self,
        bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        from factories import forbidden_error

        event, occurrence = await post_event(
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
        channel.send_error = forbidden_error(50001)

        await run_event_maintenance(bot, AFTER_END)

        # The failed send leaves the next occurrence stored but unposted,
        # with its auto signups already applied.
        assert len(channel.sent) == 1
        finished = store.get_occurrence(occurrence.occurrence_id)
        assert finished is not None
        assert finished.status is EventStatus.OVER
        pending = store.get_unposted_occurrences()
        assert len(pending) == 1
        assert [
            signup.discord_user_id
            for signup in store.get_signups(pending[0].occurrence_id)
        ] == [11]

        await run_event_maintenance(bot, AFTER_END)

        assert len(channel.sent) == 2
        posted = store.get_posted_unfinished_occurrences()
        assert [entry.occurrence_id for entry in posted] == [
            pending[0].occurrence_id
        ]
        assert store.get_unposted_occurrences() == []
        channel.thread.add_user.assert_awaited()

        await run_event_maintenance(bot, AFTER_END)

        assert len(channel.sent) == 2

    async def test_dirty_occurrence_is_refreshed_without_status_change(
        self,
        bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event, occurrence = await post_event(bot, store)
        # An earlier roster-change refresh failed while the status stayed
        # OPEN, so the occurrence is flagged dirty.
        store.set_occurrence_needs_refresh(occurrence.occurrence_id, True)

        # The status still matches, but the stale message must be re-rendered
        # and the flag cleared instead of being skipped forever.
        await run_event_maintenance(bot, BEFORE_START)

        channel.partial_message.edit.assert_awaited()
        refreshed = store.get_occurrence(occurrence.occurrence_id)
        assert refreshed is not None
        assert refreshed.status is EventStatus.OPEN
        assert not refreshed.needs_refresh

    async def test_pending_occurrence_already_over_seeds_the_next(
        self,
        bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        from factories import forbidden_error

        await post_event(
            bot,
            store,
            repeat_frequency=RepeatFrequency.DAILY,
        )
        # The next occurrence is created but its posting fails, leaving it
        # pending and unposted.
        channel.send_error = forbidden_error(50001)
        await run_event_maintenance(bot, AFTER_END)
        pending = store.get_unposted_occurrences()
        assert len(pending) == 1
        next_start = pending[0].start_time

        # Posting is only fixed after that pending occurrence has itself
        # ended, so it can only post as OVER.
        after_next_end = next_start + timedelta(hours=2)
        await run_event_maintenance(bot, after_next_end)

        finished = store.get_occurrence(pending[0].occurrence_id)
        assert finished is not None
        assert finished.status is EventStatus.OVER
        # The recurring series must catch up with a fresh future occurrence
        # instead of stopping with nothing to drive it.
        upcoming = store.get_unposted_occurrences()
        assert len(upcoming) == 1
        assert upcoming[0].start_time > after_next_end

    async def test_pending_occurrence_of_a_never_posted_event_is_skipped(
        self,
        bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        # A series without any posted occurrence belongs to a manual post
        # still in flight; posting it would race the creator's own flow.
        event = store.create_event(
            category=EventCategory.FRACTAL,
            title="Kitty Cleanup",
            description="Bring food.",
            channel_id=1234,
            leader_discord_id=42,
            start_time=START,
            duration_minutes=90,
            repeat_frequency=RepeatFrequency.NONE,
            repeat_days=(),
        )
        store.create_occurrence(event.event_id, event.start_time)

        await run_event_maintenance(bot, BEFORE_START)

        assert channel.sent == []
        assert len(store.get_unposted_occurrences()) == 1

    async def test_maintenance_logs_never_contain_user_content(
        self,
        bot: Any,
        store: EventStore,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        title = "SECRET EVENT TITLE"
        description = "SECRET EVENT DESCRIPTION"
        event = store.create_event(
            category=EventCategory.FRACTAL,
            title=title,
            description=description,
            channel_id=1234,
            leader_discord_id=42,
            start_time=START,
            duration_minutes=90,
            repeat_frequency=RepeatFrequency.DAILY,
            repeat_days=(),
        )
        occurrence = store.create_occurrence(event.event_id, event.start_time)
        await post_occurrence(bot, event, occurrence, BEFORE_START)

        with caplog.at_level("DEBUG"):
            await run_event_maintenance(bot, AFTER_END)

        assert title not in caplog.text
        assert description not in caplog.text
