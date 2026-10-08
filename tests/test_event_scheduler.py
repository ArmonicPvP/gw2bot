from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock

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

from factories import forbidden_error
from test_event_posting import (
    FakeBot,
    FakeChannel,
    FakeForumPost,
    forum_post_bot,
)

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

    async def test_the_pass_that_ends_a_run_records_it(
        self,
        bot: Any,
        store: EventStore,
    ) -> None:
        event, occurrence = await post_event(bot, store)

        await run_event_maintenance(bot, START + timedelta(minutes=5))
        assert store.get_event_runs(0, AFTER_END.timestamp()) == []

        await run_event_maintenance(bot, AFTER_END)

        # Judged on the pass's own clock: the run is in the future by the
        # wall clock, so a status write that ignored it would leave it out.
        [run] = store.get_event_runs(0, AFTER_END.timestamp())
        assert run.occurrence_id == occurrence.occurrence_id
        assert run.event_id == event.event_id

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

    async def post_forum_event_owing_a_removal(
        self,
        store: EventStore,
        ping_channel: FakeChannel,
        post: FakeForumPost,
        repeat_frequency: RepeatFrequency = RepeatFrequency.NONE,
    ):
        bot = cast(Any, forum_post_bot(store, post, ping_channel=ping_channel))
        event = store.create_event(
            category=EventCategory.FRACTAL,
            title="Kitty Cleanup",
            description="Bring food.",
            channel_id=post.id,
            leader_discord_id=42,
            start_time=START,
            duration_minutes=90,
            repeat_frequency=repeat_frequency,
            repeat_days=(),
            ping_role_ids=(11,),
        )
        occurrence = store.create_occurrence(event.event_id, event.start_time)
        posted = await post_occurrence(bot, event, occurrence, BEFORE_START)
        store.add_occurrence_stale_ping_message(
            posted.occurrence_id,
            ping_channel.id,
            9090,
        )
        return bot, event, posted

    async def test_a_finished_occurrence_still_retries_its_leftovers(
        self,
        store: EventStore,
    ) -> None:
        # The regression: an occurrence that reaches OVER leaves
        # get_posted_unfinished_occurrences(), so a removal Discord was still
        # refusing on the pass that ended the event was never tried again. For
        # a one-off event the dead link stood in the ping channel until
        # somebody deleted the whole event, even after permissions recovered.
        post = FakeForumPost()
        ping_channel = FakeChannel(channel_id=4321)
        bot, _, posted = await self.post_forum_event_owing_a_removal(
            store, ping_channel, post
        )
        store.set_occurrence_status(posted.occurrence_id, EventStatus.OVER)
        assert store.get_posted_unfinished_occurrences() == []

        await run_event_maintenance(bot, AFTER_END)

        ping_channel.partial_message.delete.assert_awaited_once()
        stored = store.get_occurrence(posted.occurrence_id)
        assert stored is not None
        assert stored.stale_ping_messages == ()

    async def test_a_refreshed_occurrence_is_not_swept_twice_in_one_pass(
        self,
        store: EventStore,
    ) -> None:
        # The refresh sweeps what the occurrence owes, so the pass-wide sweep
        # must not come back for the same one: a refusal would otherwise cost
        # two Discord calls a minute for as long as it lasted.
        post = FakeForumPost()
        ping_channel = FakeChannel(channel_id=4321)
        bot, _, posted = await self.post_forum_event_owing_a_removal(
            store, ping_channel, post
        )
        ping_channel.partial_message.delete = AsyncMock(
            side_effect=forbidden_error(50013)
        )

        # A status move puts the occurrence through refresh_occurrence_message.
        await run_event_maintenance(bot, START + timedelta(minutes=5))

        ping_channel.partial_message.delete.assert_awaited_once()
        # Still owed, so the next pass tries it again.
        stored = store.get_occurrence(posted.occurrence_id)
        assert stored is not None
        assert stored.stale_ping_messages == ((ping_channel.id, 9090),)

    async def test_an_unchanged_occurrence_still_retries_its_leftovers(
        self,
        store: EventStore,
    ) -> None:
        # The regression: an announcement a channel move could not remove was
        # only retried by a refresh, which an unchanged occurrence never
        # reaches - so for an event weeks out the dead link stood in the ping
        # channel until some unrelated roster or status change happened to
        # bring the occurrence back through one.
        post = FakeForumPost()
        ping_channel = FakeChannel(channel_id=4321)
        bot = cast(Any, forum_post_bot(store, post, ping_channel=ping_channel))
        event = store.create_event(
            category=EventCategory.FRACTAL,
            title="Kitty Cleanup",
            description="Bring food.",
            channel_id=post.id,
            leader_discord_id=42,
            start_time=START,
            duration_minutes=90,
            repeat_frequency=RepeatFrequency.NONE,
            repeat_days=(),
            ping_role_ids=(11,),
        )
        occurrence = store.create_occurrence(event.event_id, event.start_time)
        posted = await post_occurrence(bot, event, occurrence, BEFORE_START)
        store.add_occurrence_stale_ping_message(
            posted.occurrence_id,
            ping_channel.id,
            9090,
        )

        # Nothing about the occurrence has moved: same status, not dirty.
        await run_event_maintenance(bot, BEFORE_START)

        ping_channel.partial_message.delete.assert_awaited_once()
        stored = store.get_occurrence(posted.occurrence_id)
        assert stored is not None
        assert stored.stale_ping_messages == ()
        # The pass still leaves the unchanged occurrence's own post alone.
        post.partial_message.edit.assert_not_awaited()

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

    async def test_rollover_deletes_previous_occurrence_when_opted_in(
        self,
        bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event = store.create_event(
            category=EventCategory.FRACTAL,
            title="Kitty Cleanup",
            description="Bring food.",
            channel_id=1234,
            leader_discord_id=42,
            start_time=START,
            duration_minutes=90,
            repeat_frequency=RepeatFrequency.DAILY,
            repeat_days=(),
            delete_previous_on_repeat=True,
        )
        occurrence = store.create_occurrence(event.event_id, event.start_time)
        posted = await post_occurrence(bot, event, occurrence, BEFORE_START)

        await run_event_maintenance(bot, AFTER_END)

        # The finished occurrence and its post are removed; only the freshly
        # posted next occurrence remains.
        assert store.get_occurrence(posted.occurrence_id) is None
        channel.partial_message.delete.assert_awaited()
        live = store.get_posted_unfinished_occurrences()
        assert len(live) == 1
        assert live[0].occurrence_id != posted.occurrence_id

    async def test_rollover_keeps_previous_occurrence_by_default(
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

        await run_event_maintenance(bot, AFTER_END)

        # Without the opt-in, history is kept and no message is deleted.
        assert store.get_occurrence(occurrence.occurrence_id) is not None
        channel.partial_message.delete.assert_not_awaited()

    async def test_rollover_prunes_the_previous_post_on_a_refresh_retry(
        self,
        bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event = store.create_event(
            category=EventCategory.FRACTAL,
            title="Kitty Cleanup",
            description="Bring food.",
            channel_id=1234,
            leader_discord_id=42,
            start_time=START,
            duration_minutes=90,
            repeat_frequency=RepeatFrequency.DAILY,
            repeat_days=(),
            delete_previous_on_repeat=True,
        )
        occurrence = store.create_occurrence(event.event_id, event.start_time)
        posted = await post_occurrence(bot, event, occurrence, BEFORE_START)
        # The refresh that would persist OVER fails transiently, so the next
        # occurrence gets posted while the old one is still not OVER.
        channel.partial_message.edit = AsyncMock(
            side_effect=forbidden_error(50001)
        )

        await run_event_maintenance(bot, AFTER_END)

        stale = store.get_occurrence(posted.occurrence_id)
        assert stale is not None
        assert stale.status is not EventStatus.OVER
        assert stale.needs_refresh
        channel.partial_message.delete.assert_not_awaited()

        # The refresh recovers on a later pass, which is when the old occurrence
        # finally reaches OVER. Nothing new is posted then, so the post-time
        # cleanup never runs again: without pruning on the OVER transition too,
        # the old message and row would survive forever despite the opt-in.
        channel.partial_message.edit = AsyncMock()

        await run_event_maintenance(bot, AFTER_END)

        assert store.get_occurrence(posted.occurrence_id) is None
        channel.partial_message.delete.assert_awaited()
        live = store.get_posted_unfinished_occurrences()
        assert len(live) == 1
        assert live[0].occurrence_id != posted.occurrence_id

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

    async def test_pending_occurrence_flagged_for_posting_is_retried(
        self,
        bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        # A cancellation whose successor could not be posted leaves the series
        # with no posted occurrence at all. The refresh flag is what separates
        # that from a manual post in flight, so this one has to go out.
        event = store.create_event(
            category=EventCategory.FRACTAL,
            title="Kitty Cleanup",
            description="Bring food.",
            channel_id=1234,
            leader_discord_id=42,
            start_time=START,
            duration_minutes=90,
            repeat_frequency=RepeatFrequency.DAILY,
            repeat_days=(),
        )
        occurrence = store.create_occurrence(event.event_id, event.start_time)
        store.set_occurrence_needs_refresh(occurrence.occurrence_id, True)

        await run_event_maintenance(bot, BEFORE_START)

        assert len(channel.sent) == 1
        posted = store.get_occurrence(occurrence.occurrence_id)
        assert posted is not None
        assert posted.message_id is not None
        # The flag asked for the posting and nothing else, so a later pass must
        # not keep re-rendering the message it produced.
        assert not posted.needs_refresh

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


class TestHeldSeatRelease:
    """Rosters seated before admission held their last seats for the boons."""

    @staticmethod
    def seat_old_roster(store: EventStore, occurrence_id: int) -> None:
        # A healer and four plain DPS leave the fractal's boon DPS with no
        # seat; the alacrity DPS who could bring it is stuck behind them.
        for minute, (user_id, role, waitlisted) in enumerate(
            [
                (11, EventRole.QUICKNESS_HEAL, False),
                (12, EventRole.DPS, False),
                (13, EventRole.DPS, False),
                (14, EventRole.DPS, False),
                (15, EventRole.DPS, False),
                (16, EventRole.ALACRITY_DPS, True),
            ]
        ):
            store.add_signup(
                occurrence_id=occurrence_id,
                discord_user_id=user_id,
                role=role,
                assigned_role=None if waitlisted else role,
                flex_roles=(),
                waitlisted=waitlisted,
                now=BEFORE_START - timedelta(days=1, minutes=-minute),
            )

    async def test_the_pass_releases_the_seat_and_says_so(
        self,
        bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        _, occurrence = await post_event(bot, store)
        self.seat_old_roster(store, occurrence.occurrence_id)

        await run_event_maintenance(bot, BEFORE_START)

        released = store.get_signup(occurrence.occurrence_id, 15)
        assert released is not None
        assert released.waitlisted
        promoted = store.get_signup(occurrence.occurrence_id, 16)
        assert promoted is not None
        assert promoted.assigned_role is EventRole.ALACRITY_DPS
        assert channel.thread.send.await_args is not None
        content = channel.thread.send.await_args.args[0]
        assert "<@15> moved to the waitlist" in content
        assert "<@16> moved up from the waitlist" in content
        # The post is refreshed to show the roster that now stands.
        channel.partial_message.edit.assert_awaited()
        updated = store.get_occurrence(occurrence.occurrence_id)
        assert updated is not None
        assert updated.status is EventStatus.FULL

    async def test_a_later_pass_finds_nothing_left_to_release(
        self,
        bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        _, occurrence = await post_event(bot, store)
        self.seat_old_roster(store, occurrence.occurrence_id)
        await run_event_maintenance(bot, BEFORE_START)
        sent = channel.thread.send.await_count
        edits = channel.partial_message.edit.await_count

        await run_event_maintenance(bot, BEFORE_START)

        assert channel.thread.send.await_count == sent
        assert channel.partial_message.edit.await_count == edits

    async def test_a_run_under_way_is_left_as_it_is_played(
        self,
        bot: Any,
        store: EventStore,
    ) -> None:
        _, occurrence = await post_event(bot, store)
        self.seat_old_roster(store, occurrence.occurrence_id)

        await run_event_maintenance(bot, START + timedelta(minutes=5))

        seated = store.get_signup(occurrence.occurrence_id, 15)
        assert seated is not None
        assert not seated.waitlisted

    async def test_a_roster_that_cannot_be_settled_does_not_stop_the_pass(
        self,
        bot: Any,
        store: EventStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _, first = await post_event(bot, store)
        self.seat_old_roster(store, first.occurrence_id)
        _, second = await post_event(bot, store)
        # Owed a re-render, so the pass has work to do on it after the
        # first run's failure.
        store.set_occurrence_needs_refresh(second.occurrence_id, True)
        broken = AsyncMock(side_effect=RuntimeError("boom"))
        monkeypatch.setattr(
            "gw2bot.events.scheduler.settle_held_seats", broken
        )

        await run_event_maintenance(bot, BEFORE_START)

        broken.assert_awaited_once()
        refreshed = store.get_occurrence(second.occurrence_id)
        assert refreshed is not None
        assert not refreshed.needs_refresh
