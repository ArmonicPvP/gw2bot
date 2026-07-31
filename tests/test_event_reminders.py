from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from gw2bot.events.formatting import reminder_messages
from gw2bot.events.models import (
    REMINDER_GRACE,
    REMINDER_OFFSETS,
    EventCategory,
    EventRole,
    EventStatus,
    RepeatFrequency,
    reminder_offset_minutes,
)
from gw2bot.events.posting import post_occurrence
from gw2bot.events.reminders import (
    deliver_due_reminders,
    due_reminder_offsets,
    reminder_participants,
    send_reminder,
)
from gw2bot.events.scheduler import run_event_maintenance
from gw2bot.events.store import EventStore
from gw2bot.guild_members import DISCORD_MESSAGE_LIMIT

from factories import forbidden_error
from test_event_posting import (
    FakeBot,
    FakeChannel,
    FakeForumPost,
    forum_post_bot,
)

START = datetime(2027, 1, 30, 20, 0, tzinfo=UTC)
BEFORE_START = START - timedelta(hours=2)
AN_HOUR_BEFORE = START - timedelta(hours=1)
FIFTEEN_BEFORE = START - timedelta(minutes=15)
AFTER_END = START + timedelta(hours=2)

HOUR_OFFSET = reminder_offset_minutes(timedelta(hours=1))
QUARTER_OFFSET = reminder_offset_minutes(timedelta(minutes=15))
START_OFFSET = reminder_offset_minutes(timedelta(0))


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
    *,
    title: str = "Kitty Cleanup",
    repeat_frequency: RepeatFrequency = RepeatFrequency.NONE,
):
    return store.create_event(
        category=EventCategory.FRACTAL,
        title=title,
        description="Bring food.",
        channel_id=1234,
        leader_discord_id=42,
        start_time=START,
        duration_minutes=90,
        repeat_frequency=repeat_frequency,
        repeat_days=(),
    )


def sign_up(
    store: EventStore,
    occurrence_id: int,
    discord_user_id: int,
    *,
    waitlisted: bool = False,
    role: EventRole = EventRole.DPS,
) -> None:
    store.add_signup(
        occurrence_id=occurrence_id,
        discord_user_id=discord_user_id,
        role=role,
        assigned_role=None if waitlisted else role,
        flex_roles=(),
        waitlisted=waitlisted,
        now=BEFORE_START,
    )


async def post_event(
    bot: Any,
    store: EventStore,
    *,
    title: str = "Kitty Cleanup",
    participants: tuple[int, ...] = (11, 22),
    waitlisted: tuple[int, ...] = (),
):
    event = create_event(store, title=title)
    occurrence = store.create_occurrence(event.event_id, event.start_time)
    posted = await post_occurrence(bot, event, occurrence, BEFORE_START)
    for discord_user_id in participants:
        sign_up(store, posted.occurrence_id, discord_user_id)
    for discord_user_id in waitlisted:
        sign_up(store, posted.occurrence_id, discord_user_id, waitlisted=True)
    return event, posted


class TestReminderMessages:
    def test_pings_every_member_with_the_title_and_a_relative_start(
        self,
    ) -> None:
        messages = reminder_messages("Kitty Cleanup", START, [11, 22, 33])

        assert messages == [
            "<@11> <@22> <@33>: Kitty Cleanup starts "
            f"<t:{int(START.timestamp())}:R>"
        ]

    def test_no_members_produces_no_message(self) -> None:
        assert reminder_messages("Kitty Cleanup", START, []) == []

    def test_members_are_split_over_messages_that_fit_discord(self) -> None:
        # Every member must be pinged, so a roster whose mentions outgrow one
        # message is split rather than truncated.
        user_ids = list(range(100_000_000_000_000_000, 100_000_000_000_000_120))

        messages = reminder_messages("Kitty Cleanup", START, user_ids)

        assert len(messages) > 1
        assert all(
            len(message) <= DISCORD_MESSAGE_LIMIT for message in messages
        )
        mentioned = [
            mention
            for message in messages
            for mention in message.split(":")[0].split()
        ]
        assert mentioned == [f"<@{user_id}>" for user_id in user_ids]

    def test_an_oversized_title_cannot_squeeze_out_the_ping(self) -> None:
        messages = reminder_messages("t" * 4_000, START, [11, 22])

        assert len(messages) == 1
        assert len(messages[0]) <= DISCORD_MESSAGE_LIMIT
        assert messages[0].startswith("<@11> <@22>: ")
        assert messages[0].endswith(f"starts <t:{int(START.timestamp())}:R>")


class TestReminderParticipants:
    def test_only_seated_members_are_reminded(self, store: EventStore) -> None:
        event = create_event(store)
        occurrence = store.create_occurrence(event.event_id, event.start_time)
        sign_up(store, occurrence.occurrence_id, 11)
        sign_up(store, occurrence.occurrence_id, 22, waitlisted=True)

        signups = store.get_signups(occurrence.occurrence_id)

        assert reminder_participants(signups) == [11]


class TestDueReminderOffsets:
    def test_nothing_is_due_before_the_first_reminder(
        self,
        store: EventStore,
    ) -> None:
        event = create_event(store)
        occurrence = store.create_occurrence(event.event_id, event.start_time)

        assert due_reminder_offsets(occurrence, set(), BEFORE_START) == (
            [],
            None,
        )

    def test_each_reminder_comes_due_at_its_offset(
        self,
        store: EventStore,
    ) -> None:
        event = create_event(store)
        occurrence = store.create_occurrence(event.event_id, event.start_time)

        assert due_reminder_offsets(occurrence, set(), AN_HOUR_BEFORE) == (
            [HOUR_OFFSET],
            HOUR_OFFSET,
        )
        assert due_reminder_offsets(
            occurrence,
            {HOUR_OFFSET},
            FIFTEEN_BEFORE,
        ) == ([QUARTER_OFFSET], QUARTER_OFFSET)
        assert due_reminder_offsets(
            occurrence,
            {HOUR_OFFSET, QUARTER_OFFSET},
            START,
        ) == ([START_OFFSET], START_OFFSET)

    def test_recorded_reminders_never_come_due_again(
        self,
        store: EventStore,
    ) -> None:
        event = create_event(store)
        occurrence = store.create_occurrence(event.event_id, event.start_time)
        handled = {
            reminder_offset_minutes(offset) for offset in REMINDER_OFFSETS
        }

        assert due_reminder_offsets(occurrence, handled, START) == ([], None)

    def test_downtime_delivers_only_the_most_imminent_reminder(
        self,
        store: EventStore,
    ) -> None:
        event = create_event(store)
        occurrence = store.create_occurrence(event.event_id, event.start_time)

        due, deliverable = due_reminder_offsets(occurrence, set(), START)

        # Every missed reminder is settled, but only the one that just came due
        # is worth sending: the earlier ones say the same thing, but less
        # accurately.
        assert due == [HOUR_OFFSET, QUARTER_OFFSET, START_OFFSET]
        assert deliverable == START_OFFSET

    def test_reminders_that_lapsed_during_downtime_are_not_delivered(
        self,
        store: EventStore,
    ) -> None:
        event = create_event(store)
        occurrence = store.create_occurrence(event.event_id, event.start_time)
        long_after_start = START + REMINDER_GRACE + timedelta(minutes=1)

        due, deliverable = due_reminder_offsets(
            occurrence,
            set(),
            long_after_start,
        )

        assert due == [HOUR_OFFSET, QUARTER_OFFSET, START_OFFSET]
        assert deliverable is None


class TestSendReminder:
    async def test_pings_the_roster_in_the_signup_thread(
        self,
        bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event, occurrence = await post_event(bot, store)
        signups = store.get_signups(occurrence.occurrence_id)

        assert await send_reminder(bot, event, occurrence, signups) is True

        channel.thread.send.assert_awaited_once_with(
            "<@11> <@22>: Kitty Cleanup starts "
            f"<t:{int(START.timestamp())}:R>"
        )

    async def test_pings_inside_the_forum_post_and_reopens_it(
        self,
        store: EventStore,
    ) -> None:
        post = FakeForumPost(archived=True)
        bot = forum_post_bot(store, post)
        event = create_event(store)
        store.update_event(
            event_id=event.event_id,
            category=event.category,
            title=event.title,
            description=event.description,
            channel_id=post.id,
            leader_discord_id=event.leader_discord_id,
            start_time=event.start_time,
            duration_minutes=event.duration_minutes,
            repeat_frequency=event.repeat_frequency,
            repeat_days=event.repeat_days,
        )
        event = cast(Any, store.get_event(event.event_id))
        occurrence = store.create_occurrence(event.event_id, event.start_time)
        posted = await post_occurrence(bot, event, occurrence, BEFORE_START)
        sign_up(store, posted.occurrence_id, 11)
        post.archived = True
        signups = store.get_signups(posted.occurrence_id)

        assert await send_reminder(bot, event, posted, signups) is True

        assert post.archived is False
        assert post.sent[-1]["content"] == (
            f"<@11>: Kitty Cleanup starts <t:{int(START.timestamp())}:R>"
        )

    async def test_a_roster_without_seated_members_is_not_pinged(
        self,
        bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event, occurrence = await post_event(
            bot,
            store,
            participants=(),
            waitlisted=(33,),
        )
        signups = store.get_signups(occurrence.occurrence_id)

        assert await send_reminder(bot, event, occurrence, signups) is False

        channel.thread.send.assert_not_awaited()

    async def test_an_occurrence_without_a_thread_is_not_pinged(
        self,
        bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        channel.create_thread_error = forbidden_error(50013)
        event, occurrence = await post_event(bot, store)
        signups = store.get_signups(occurrence.occurrence_id)

        assert occurrence.thread_id is None
        assert await send_reminder(bot, event, occurrence, signups) is False
        channel.thread.send.assert_not_awaited()

    async def test_a_failed_send_is_reported(
        self,
        bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event, occurrence = await post_event(bot, store)
        channel.thread.send = AsyncMock(side_effect=forbidden_error(50013))
        signups = store.get_signups(occurrence.occurrence_id)

        assert await send_reminder(bot, event, occurrence, signups) is False


class TestDeliverDueReminders:
    async def test_nothing_is_sent_before_the_first_reminder(
        self,
        bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event, occurrence = await post_event(bot, store)

        assert (
            await deliver_due_reminders(bot, event, occurrence, BEFORE_START)
            is None
        )

        channel.thread.send.assert_not_awaited()
        assert (
            store.get_handled_reminder_offsets(occurrence.occurrence_id)
            == set()
        )

    async def test_each_reminder_is_sent_once_at_its_moment(
        self,
        bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event, occurrence = await post_event(bot, store)

        for now, offset in (
            (AN_HOUR_BEFORE, HOUR_OFFSET),
            (FIFTEEN_BEFORE, QUARTER_OFFSET),
            (START, START_OFFSET),
        ):
            delivered = await deliver_due_reminders(bot, event, occurrence, now)
            assert delivered == offset
            # A second pass at the same moment must not ping the roster twice.
            assert (
                await deliver_due_reminders(bot, event, occurrence, now)
                is None
            )

        assert channel.thread.send.await_count == 3
        assert store.get_handled_reminder_offsets(occurrence.occurrence_id) == {
            HOUR_OFFSET,
            QUARTER_OFFSET,
            START_OFFSET,
        }

    async def test_downtime_sends_one_reminder_and_settles_the_rest(
        self,
        bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event, occurrence = await post_event(bot, store)

        assert await deliver_due_reminders(bot, event, occurrence, START) == (
            START_OFFSET
        )

        channel.thread.send.assert_awaited_once()
        assert store.get_handled_reminder_offsets(occurrence.occurrence_id) == {
            HOUR_OFFSET,
            QUARTER_OFFSET,
            START_OFFSET,
        }

    async def test_lapsed_reminders_are_settled_without_a_ping(
        self,
        bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event, occurrence = await post_event(bot, store)
        long_after_start = START + REMINDER_GRACE + timedelta(minutes=1)

        assert (
            await deliver_due_reminders(
                bot,
                event,
                occurrence,
                long_after_start,
            )
            is None
        )

        channel.thread.send.assert_not_awaited()
        assert store.get_handled_reminder_offsets(occurrence.occurrence_id) == {
            HOUR_OFFSET,
            QUARTER_OFFSET,
            START_OFFSET,
        }

    async def test_a_failed_reminder_is_retried_until_it_lapses(
        self,
        bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event, occurrence = await post_event(bot, store)
        channel.thread.send = AsyncMock(side_effect=forbidden_error(50013))

        assert (
            await deliver_due_reminders(bot, event, occurrence, AN_HOUR_BEFORE)
            is None
        )
        # The failed reminder stays outstanding so the next pass can retry it.
        assert (
            store.get_handled_reminder_offsets(occurrence.occurrence_id)
            == set()
        )

        channel.thread.send = AsyncMock()
        retry_time = AN_HOUR_BEFORE + timedelta(minutes=1)

        assert (
            await deliver_due_reminders(bot, event, occurrence, retry_time)
            == HOUR_OFFSET
        )

        channel.thread.send.assert_awaited_once()

    async def test_a_reminder_that_keeps_failing_stops_being_retried(
        self,
        bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event, occurrence = await post_event(bot, store)
        channel.thread.send = AsyncMock(side_effect=forbidden_error(50013))

        await deliver_due_reminders(bot, event, occurrence, AN_HOUR_BEFORE)
        lapsed = AN_HOUR_BEFORE + REMINDER_GRACE + timedelta(minutes=1)
        await deliver_due_reminders(bot, event, occurrence, lapsed)

        # Once outside the grace window the reminder is settled rather than
        # retried forever against a destination the bot cannot post in.
        assert HOUR_OFFSET in store.get_handled_reminder_offsets(
            occurrence.occurrence_id
        )
        assert channel.thread.send.await_count == 1

    async def test_a_roster_without_members_settles_without_a_ping(
        self,
        bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event, occurrence = await post_event(bot, store, participants=())

        assert (
            await deliver_due_reminders(bot, event, occurrence, AN_HOUR_BEFORE)
            is None
        )

        channel.thread.send.assert_not_awaited()
        assert store.get_handled_reminder_offsets(occurrence.occurrence_id) == {
            HOUR_OFFSET
        }

    async def test_a_finished_occurrence_is_never_reminded_about(
        self,
        bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event, occurrence = await post_event(bot, store)

        assert (
            await deliver_due_reminders(bot, event, occurrence, AFTER_END)
            is None
        )

        channel.thread.send.assert_not_awaited()
        assert store.get_handled_reminder_offsets(occurrence.occurrence_id) == {
            HOUR_OFFSET,
            QUARTER_OFFSET,
            START_OFFSET,
        }

    async def test_members_who_sign_up_late_are_reminded_too(
        self,
        bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event, occurrence = await post_event(bot, store, participants=(11,))
        await deliver_due_reminders(bot, event, occurrence, AN_HOUR_BEFORE)
        sign_up(store, occurrence.occurrence_id, 22)

        await deliver_due_reminders(bot, event, occurrence, FIFTEEN_BEFORE)

        assert channel.thread.send.await_args is not None
        assert channel.thread.send.await_args.args[0].startswith(
            "<@11> <@22>: "
        )


class TestReminderStore:
    def test_recording_a_reminder_twice_is_harmless(
        self,
        store: EventStore,
    ) -> None:
        event = create_event(store)
        occurrence = store.create_occurrence(event.event_id, event.start_time)

        store.mark_reminders_handled(occurrence.occurrence_id, [HOUR_OFFSET])
        store.mark_reminders_handled(
            occurrence.occurrence_id,
            [HOUR_OFFSET, QUARTER_OFFSET],
        )

        assert store.get_handled_reminder_offsets(occurrence.occurrence_id) == {
            HOUR_OFFSET,
            QUARTER_OFFSET,
        }

    def test_recorded_reminders_are_scoped_to_their_occurrence(
        self,
        store: EventStore,
    ) -> None:
        event = create_event(store)
        first = store.create_occurrence(event.event_id, event.start_time)
        second = store.create_occurrence(
            event.event_id,
            event.start_time + timedelta(days=1),
        )

        store.mark_reminders_handled(first.occurrence_id, [HOUR_OFFSET])

        assert store.get_handled_reminder_offsets(second.occurrence_id) == set()

    def test_deleting_an_occurrence_removes_its_reminders(
        self,
        store: EventStore,
    ) -> None:
        event = create_event(store)
        occurrence = store.create_occurrence(event.event_id, event.start_time)
        store.mark_reminders_handled(occurrence.occurrence_id, [HOUR_OFFSET])

        store.delete_occurrence(occurrence.occurrence_id)

        assert (
            store.get_handled_reminder_offsets(occurrence.occurrence_id)
            == set()
        )

    def test_deleting_an_event_removes_its_reminders(
        self,
        store: EventStore,
    ) -> None:
        event = create_event(store)
        occurrence = store.create_occurrence(event.event_id, event.start_time)
        store.mark_reminders_handled(occurrence.occurrence_id, [HOUR_OFFSET])

        store.delete_event(event.event_id)

        assert (
            store.get_handled_reminder_offsets(occurrence.occurrence_id)
            == set()
        )


class TestSchedulerReminders:
    async def test_maintenance_reminds_the_roster_on_the_clock(
        self,
        bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event, occurrence = await post_event(bot, store)

        # The status has not moved at this point, so the reminder can only go
        # out if it is resolved independently of the status work.
        await run_event_maintenance(bot, AN_HOUR_BEFORE)

        channel.thread.send.assert_awaited_once_with(
            "<@11> <@22>: Kitty Cleanup starts "
            f"<t:{int(START.timestamp())}:R>"
        )
        updated = store.get_occurrence(occurrence.occurrence_id)
        assert updated is not None
        assert updated.status is EventStatus.OPEN

    async def test_maintenance_reminds_once_across_passes(
        self,
        bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        await post_event(bot, store)

        await run_event_maintenance(bot, AN_HOUR_BEFORE)
        await run_event_maintenance(bot, AN_HOUR_BEFORE + timedelta(minutes=1))

        channel.thread.send.assert_awaited_once()

    async def test_a_reminder_failure_does_not_stop_the_pass(
        self,
        bot: Any,
        store: EventStore,
        channel: FakeChannel,
    ) -> None:
        event, occurrence = await post_event(bot, store)
        channel.thread.send = AsyncMock(side_effect=RuntimeError("boom"))

        await run_event_maintenance(bot, START + timedelta(minutes=5))

        # The status transition still lands even though the reminder blew up.
        updated = store.get_occurrence(occurrence.occurrence_id)
        assert updated is not None
        assert updated.status is EventStatus.ONGOING

    async def test_reminder_logs_never_contain_user_content(
        self,
        bot: Any,
        store: EventStore,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        title = "SECRET EVENT TITLE"
        event, occurrence = await post_event(bot, store, title=title)

        with caplog.at_level("DEBUG"):
            await run_event_maintenance(bot, AN_HOUR_BEFORE)

        assert title not in caplog.text
        assert "SECRET" not in caplog.text
        assert "Bring food." not in caplog.text
        assert "<@11>" not in caplog.text
