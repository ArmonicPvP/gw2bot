from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import text

from gw2bot.database import create_database_engine, initialize_database
from gw2bot.events.models import (
    AutoSignupChoice,
    EventCategory,
    EventRole,
    EventStatus,
    PreferenceMode,
    RepeatFrequency,
)
from gw2bot.events.store import EventStore

START = datetime(2027, 1, 30, 20, 0, tzinfo=UTC)


@pytest.fixture
def store(tmp_path: Path):
    store = EventStore(str(tmp_path / "gw2bot.db"))
    yield store
    store.close()


def test_migration_adds_delete_previous_on_repeat_to_existing_db(
    tmp_path: Path,
) -> None:
    db_path = str(tmp_path / "legacy.db")
    engine = create_database_engine(db_path)
    # Build the current schema, then simulate a database created before the
    # column existed by dropping it and inserting a legacy row.
    initialize_database(engine)
    with engine.begin() as connection:
        connection.execute(
            text(
                "ALTER TABLE gw2_events "
                "DROP COLUMN delete_previous_on_repeat"
            )
        )
        connection.execute(
            text(
                "INSERT INTO gw2_events (category, title, description, "
                "channel_id, leader_discord_id, start_time, duration_minutes, "
                "repeat_frequency, repeat_days, created_at, cancelled) VALUES "
                "('Fractal', 't', 'd', 1, 2, "
                "'2027-01-30T20:00:00+00:00', 90, 'daily', '', "
                "'2027-01-01T00:00:00+00:00', 0)"
            )
        )

    added = initialize_database(engine)
    engine.dispose()

    assert "delete_previous_on_repeat" in added
    store = EventStore(db_path)
    try:
        legacy = store.get_event(1)
        assert legacy is not None
        # The backfilled column defaults to False for pre-existing rows.
        assert legacy.delete_previous_on_repeat is False
    finally:
        store.close()


def test_migration_backfills_occurrence_channel_id(tmp_path: Path) -> None:
    db_path = str(tmp_path / "legacy.db")
    engine = create_database_engine(db_path)
    # Build the current schema, then simulate a database created before the
    # occurrence tracked its own channel.
    initialize_database(engine)
    with engine.begin() as connection:
        connection.execute(
            text("ALTER TABLE gw2_event_occurrences DROP COLUMN channel_id")
        )
        connection.execute(
            text(
                "INSERT INTO gw2_events (category, title, description, "
                "channel_id, leader_discord_id, start_time, duration_minutes, "
                "repeat_frequency, repeat_days, created_at, cancelled, "
                "delete_previous_on_repeat) VALUES "
                "('Fractal', 't', 'd', 4321, 2, "
                "'2027-01-30T20:00:00+00:00', 90, 'daily', '', "
                "'2027-01-01T00:00:00+00:00', 0, 0)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO gw2_event_occurrences (event_id, start_time, "
                "message_id, thread_id, status, needs_refresh) VALUES "
                "(1, '2027-01-30T20:00:00+00:00', 555, 777, 'over', 0)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO gw2_event_occurrences (event_id, start_time, "
                "message_id, thread_id, status, needs_refresh) VALUES "
                "(1, '2027-01-31T20:00:00+00:00', NULL, NULL, 'open', 0)"
            )
        )

    added = initialize_database(engine)
    engine.dispose()

    assert "channel_id" in added
    store = EventStore(db_path)
    try:
        posted, unposted = store.get_event_occurrences(1)
        # A legacy posted row is backfilled from the event's channel, which is
        # the only channel it could have been posted through.
        assert posted.channel_id == 4321
        # An unposted row has no message, so it has no channel either.
        assert unposted.channel_id is None
    finally:
        store.close()


def create_event(store: EventStore, **overrides: object):
    parameters: dict = {
        "category": EventCategory.FRACTAL,
        "title": "Kitty Cleanup",
        "description": "Bring food.",
        "channel_id": 1234,
        "leader_discord_id": 42,
        "start_time": START,
        "duration_minutes": 90,
        "repeat_frequency": RepeatFrequency.NONE,
        "repeat_days": (),
    }
    parameters.update(overrides)
    return store.create_event(**parameters)


class TestEventStoreEvents:
    def test_create_and_get_event_round_trip(self, store: EventStore) -> None:
        created = create_event(
            store,
            repeat_frequency=RepeatFrequency.WEEKLY,
            repeat_days=(2, 6),
        )

        loaded = store.get_event(created.event_id)

        assert loaded == created
        assert loaded is not None
        assert loaded.category is EventCategory.FRACTAL
        assert loaded.start_time == START
        assert loaded.repeat_frequency is RepeatFrequency.WEEKLY
        assert loaded.repeat_days == (2, 6)
        assert not loaded.cancelled

    def test_get_event_returns_none_for_unknown_id(
        self,
        store: EventStore,
    ) -> None:
        assert store.get_event(999) is None

    def test_update_event_overwrites_fields_and_returns(
        self,
        store: EventStore,
    ) -> None:
        event = create_event(store)
        new_start = datetime(2027, 2, 1, 18, 30, tzinfo=UTC)

        updated = store.update_event(
            event_id=event.event_id,
            category=EventCategory.RAID,
            title="New Title",
            description="New description.",
            channel_id=9999,
            leader_discord_id=7,
            start_time=new_start,
            duration_minutes=120,
            repeat_frequency=RepeatFrequency.WEEKLY,
            repeat_days=(0, 3),
        )

        assert updated.category is EventCategory.RAID
        assert updated.title == "New Title"
        assert updated.description == "New description."
        assert updated.channel_id == 9999
        assert updated.leader_discord_id == 7
        assert updated.start_time == new_start
        assert updated.duration_minutes == 120
        assert updated.repeat_frequency is RepeatFrequency.WEEKLY
        assert updated.repeat_days == (0, 3)
        assert store.get_event(event.event_id) == updated

    def test_create_event_stores_delete_previous_on_repeat(
        self,
        store: EventStore,
    ) -> None:
        created = create_event(
            store,
            repeat_frequency=RepeatFrequency.DAILY,
            delete_previous_on_repeat=True,
        )

        loaded = store.get_event(created.event_id)

        assert loaded is not None
        assert loaded.delete_previous_on_repeat is True

    def test_update_event_sets_delete_previous_on_repeat(
        self,
        store: EventStore,
    ) -> None:
        event = create_event(store)
        assert not event.delete_previous_on_repeat

        updated = store.update_event(
            event_id=event.event_id,
            category=event.category,
            title=event.title,
            description=event.description,
            channel_id=event.channel_id,
            leader_discord_id=event.leader_discord_id,
            start_time=event.start_time,
            duration_minutes=event.duration_minutes,
            repeat_frequency=RepeatFrequency.DAILY,
            repeat_days=(),
            delete_previous_on_repeat=True,
        )

        assert updated.delete_previous_on_repeat is True
        reloaded = store.get_event(event.event_id)
        assert reloaded is not None
        assert reloaded.delete_previous_on_repeat is True

    def test_update_event_unknown_id_raises(self, store: EventStore) -> None:
        with pytest.raises(ValueError, match="Unknown event"):
            store.update_event(
                event_id=999,
                category=EventCategory.RAID,
                title="x",
                description="y",
                channel_id=1,
                leader_discord_id=1,
                start_time=START,
                duration_minutes=60,
                repeat_frequency=RepeatFrequency.NONE,
                repeat_days=(),
            )


class TestEventStoreOccurrences:
    def test_create_occurrence_and_store_message(
        self,
        store: EventStore,
    ) -> None:
        event = create_event(store)

        occurrence = store.create_occurrence(event.event_id, START)
        store.set_occurrence_message(occurrence.occurrence_id, 1234, 555, 777)
        store.set_occurrence_status(
            occurrence.occurrence_id,
            EventStatus.FULL,
        )

        loaded = store.get_occurrence(occurrence.occurrence_id)
        assert loaded is not None
        assert loaded.message_id == 555
        assert loaded.thread_id == 777
        assert loaded.status is EventStatus.FULL

    def test_posted_unfinished_occurrences_excludes_unposted_and_over(
        self,
        store: EventStore,
    ) -> None:
        event = create_event(store)
        unposted = store.create_occurrence(event.event_id, START)
        posted = store.create_occurrence(event.event_id, START)
        finished = store.create_occurrence(event.event_id, START)
        store.set_occurrence_message(posted.occurrence_id, 1234, 1, None)
        store.set_occurrence_message(finished.occurrence_id, 1234, 2, None)
        store.set_occurrence_status(finished.occurrence_id, EventStatus.OVER)

        live = store.get_posted_unfinished_occurrences()

        assert [entry.occurrence_id for entry in live] == [
            posted.occurrence_id
        ]
        assert unposted.occurrence_id not in {
            entry.occurrence_id for entry in live
        }

    def test_unposted_occurrences_lists_rows_without_a_message(
        self,
        store: EventStore,
    ) -> None:
        event = create_event(store)
        unposted = store.create_occurrence(event.event_id, START)
        posted = store.create_occurrence(event.event_id, START)
        store.set_occurrence_message(posted.occurrence_id, 1234, 1, None)

        pending = store.get_unposted_occurrences()

        assert [entry.occurrence_id for entry in pending] == [
            unposted.occurrence_id
        ]

    def test_has_posted_occurrence(self, store: EventStore) -> None:
        event = create_event(store)
        occurrence = store.create_occurrence(event.event_id, START)

        assert not store.has_posted_occurrence(event.event_id)

        store.set_occurrence_message(occurrence.occurrence_id, 1234, 1, None)

        assert store.has_posted_occurrence(event.event_id)

    def test_delete_event_removes_occurrences_and_signups(
        self,
        store: EventStore,
    ) -> None:
        event = create_event(store)
        occurrence = store.create_occurrence(event.event_id, START)
        store.add_signup(
            occurrence_id=occurrence.occurrence_id,
            discord_user_id=1,
            role=EventRole.DPS,
            assigned_role=EventRole.DPS,
            flex_roles=(),
            waitlisted=False,
        )
        store.set_auto_signup(
            event.event_id,
            1,
            AutoSignupChoice.YES,
            EventRole.DPS,
            (),
        )
        untouched = create_event(store)
        untouched_occurrence = store.create_occurrence(
            untouched.event_id,
            START,
        )

        store.delete_event(event.event_id)

        assert store.get_event(event.event_id) is None
        assert store.get_occurrence(occurrence.occurrence_id) is None
        assert store.get_signups(occurrence.occurrence_id) == []
        assert store.get_auto_signup(event.event_id, 1) is None
        assert store.get_event(untouched.event_id) is not None
        assert (
            store.get_occurrence(untouched_occurrence.occurrence_id)
            is not None
        )

    def test_delete_occurrence_removes_occurrence_and_signups(
        self,
        store: EventStore,
    ) -> None:
        event = create_event(store)
        occurrence = store.create_occurrence(event.event_id, START)
        other = store.create_occurrence(event.event_id, START.replace(hour=22))
        store.add_signup(
            occurrence_id=occurrence.occurrence_id,
            discord_user_id=1,
            role=EventRole.DPS,
            assigned_role=EventRole.DPS,
            flex_roles=(),
            waitlisted=False,
        )

        store.delete_occurrence(occurrence.occurrence_id)

        assert store.get_occurrence(occurrence.occurrence_id) is None
        assert store.get_signups(occurrence.occurrence_id) == []
        # The event and the other occurrence are untouched.
        assert store.get_event(event.event_id) is not None
        assert store.get_occurrence(other.occurrence_id) is not None

    def test_has_later_occurrence(self, store: EventStore) -> None:
        event = create_event(store)
        store.create_occurrence(event.event_id, START)

        assert not store.has_later_occurrence(event.event_id, START)

        later = START.replace(hour=22)
        store.create_occurrence(event.event_id, later)

        assert store.has_later_occurrence(event.event_id, START)

    def test_set_occurrence_start_time_reschedules(
        self,
        store: EventStore,
    ) -> None:
        event = create_event(store)
        occurrence = store.create_occurrence(event.event_id, START)
        new_start = START.replace(hour=22)

        store.set_occurrence_start_time(occurrence.occurrence_id, new_start)

        loaded = store.get_occurrence(occurrence.occurrence_id)
        assert loaded is not None
        assert loaded.start_time == new_start

    def test_set_occurrence_start_time_unknown_raises(
        self,
        store: EventStore,
    ) -> None:
        with pytest.raises(ValueError, match="Unknown event occurrence"):
            store.set_occurrence_start_time(999, START)

    def test_get_event_occurrences_orders_by_start(
        self,
        store: EventStore,
    ) -> None:
        event = create_event(store)
        later = store.create_occurrence(
            event.event_id,
            START.replace(hour=23),
        )
        earlier = store.create_occurrence(
            event.event_id,
            START.replace(hour=18),
        )

        occurrences = store.get_event_occurrences(event.event_id)

        assert [entry.occurrence_id for entry in occurrences] == [
            earlier.occurrence_id,
            later.occurrence_id,
        ]

    def test_get_active_events_excludes_over_and_empty_events(
        self,
        store: EventStore,
    ) -> None:
        active = create_event(store, title="Active")
        active_occurrence = store.create_occurrence(active.event_id, START)
        store.set_occurrence_message(
            active_occurrence.occurrence_id, 1234, 1, None
        )
        completed = create_event(store, title="Completed")
        completed_occurrence = store.create_occurrence(
            completed.event_id,
            START,
        )
        store.set_occurrence_status(
            completed_occurrence.occurrence_id,
            EventStatus.OVER,
        )
        create_event(store, title="No occurrences")

        events = store.get_active_events()

        ids = [entry.event_id for entry in events]
        assert active.event_id in ids
        assert completed.event_id not in ids
        assert len(ids) == 1

    def test_get_active_events_orders_newest_first(
        self,
        store: EventStore,
    ) -> None:
        first = create_event(store)
        store.create_occurrence(first.event_id, START)
        second = create_event(store)
        store.create_occurrence(second.event_id, START)

        events = store.get_active_events()

        assert [entry.event_id for entry in events] == [
            second.event_id,
            first.event_id,
        ]

    def test_get_active_events_includes_events_with_mixed_statuses(
        self,
        store: EventStore,
    ) -> None:
        event = create_event(store)
        over = store.create_occurrence(event.event_id, START)
        store.set_occurrence_status(over.occurrence_id, EventStatus.OVER)
        store.create_occurrence(event.event_id, START.replace(hour=22))

        events = store.get_active_events()

        # The correlated EXISTS must count the still-live occurrence even when
        # an earlier one is OVER.
        assert event.event_id in [entry.event_id for entry in events]


class TestEventStoreSignups:
    def test_signup_round_trip_orders_by_signup_time(
        self,
        store: EventStore,
    ) -> None:
        event = create_event(store)
        occurrence = store.create_occurrence(event.event_id, START)
        first_time = datetime(2027, 1, 1, 10, 0, tzinfo=UTC)
        store.add_signup(
            occurrence_id=occurrence.occurrence_id,
            discord_user_id=2,
            role=EventRole.DPS,
            assigned_role=EventRole.DPS,
            flex_roles=(EventRole.QUICKNESS_DPS, EventRole.ALACRITY_DPS),
            waitlisted=False,
            now=first_time.replace(hour=12),
        )
        store.add_signup(
            occurrence_id=occurrence.occurrence_id,
            discord_user_id=1,
            role=EventRole.QUICKNESS_HEAL,
            assigned_role=EventRole.QUICKNESS_HEAL,
            flex_roles=(),
            waitlisted=False,
            now=first_time,
        )

        signups = store.get_signups(occurrence.occurrence_id)

        assert [signup.discord_user_id for signup in signups] == [1, 2]
        assert signups[1].flex_roles == (
            EventRole.QUICKNESS_DPS,
            EventRole.ALACRITY_DPS,
        )
        loaded = store.get_signup(occurrence.occurrence_id, 1)
        assert loaded is not None
        assert loaded.role is EventRole.QUICKNESS_HEAL

    def test_duplicate_signup_is_rejected(self, store: EventStore) -> None:
        event = create_event(store)
        occurrence = store.create_occurrence(event.event_id, START)
        store.add_signup(
            occurrence_id=occurrence.occurrence_id,
            discord_user_id=1,
            role=None,
            assigned_role=None,
            flex_roles=(),
            waitlisted=False,
        )

        with pytest.raises(ValueError, match="already signed up"):
            store.add_signup(
                occurrence_id=occurrence.occurrence_id,
                discord_user_id=1,
                role=None,
                assigned_role=None,
                flex_roles=(),
                waitlisted=False,
            )

    def test_remove_signup_returns_the_removed_entry(
        self,
        store: EventStore,
    ) -> None:
        event = create_event(store)
        occurrence = store.create_occurrence(event.event_id, START)
        store.add_signup(
            occurrence_id=occurrence.occurrence_id,
            discord_user_id=1,
            role=EventRole.DPS,
            assigned_role=EventRole.DPS,
            flex_roles=(),
            waitlisted=False,
        )

        removed = store.remove_signup(occurrence.occurrence_id, 1)

        assert removed is not None
        assert removed.role is EventRole.DPS
        assert store.get_signup(occurrence.occurrence_id, 1) is None
        assert store.remove_signup(occurrence.occurrence_id, 1) is None

    def test_promote_signup_clears_waitlist_flag(
        self,
        store: EventStore,
    ) -> None:
        event = create_event(store)
        occurrence = store.create_occurrence(event.event_id, START)
        store.add_signup(
            occurrence_id=occurrence.occurrence_id,
            discord_user_id=1,
            role=EventRole.DPS,
            assigned_role=None,
            flex_roles=(EventRole.QUICKNESS_DPS,),
            waitlisted=True,
        )

        store.promote_signup(
            occurrence.occurrence_id,
            1,
            EventRole.QUICKNESS_DPS,
        )

        promoted = store.get_signup(occurrence.occurrence_id, 1)
        assert promoted is not None
        assert not promoted.waitlisted
        assert promoted.assigned_role is EventRole.QUICKNESS_DPS

    def test_promote_missing_signup_raises(self, store: EventStore) -> None:
        with pytest.raises(ValueError):
            store.promote_signup(1, 1, None)


class TestEventStorePreferences:
    def test_preference_round_trip_and_overwrite(
        self,
        store: EventStore,
    ) -> None:
        assert store.get_signup_preference(1) is None

        store.set_signup_preference(
            1,
            EventRole.ALACRITY_HEAL,
            (EventRole.DPS,),
            PreferenceMode.REMEMBER,
        )
        preference = store.get_signup_preference(1)

        assert preference is not None
        assert preference.role is EventRole.ALACRITY_HEAL
        assert preference.flex_roles == (EventRole.DPS,)
        assert preference.mode is PreferenceMode.REMEMBER

        store.set_signup_preference(1, None, (), PreferenceMode.NEVER_ASK)
        preference = store.get_signup_preference(1)

        assert preference is not None
        assert preference.role is None
        assert preference.mode is PreferenceMode.NEVER_ASK


class TestEventStoreAutoSignups:
    def test_auto_signup_round_trip(self, store: EventStore) -> None:
        event = create_event(store)

        assert store.get_auto_signup(event.event_id, 1) is None

        store.set_auto_signup(
            event.event_id,
            1,
            AutoSignupChoice.YES,
            EventRole.DPS,
            (EventRole.QUICKNESS_DPS,),
        )
        entry = store.get_auto_signup(event.event_id, 1)

        assert entry is not None
        assert entry.choice is AutoSignupChoice.YES
        assert entry.role is EventRole.DPS
        assert entry.flex_roles == (EventRole.QUICKNESS_DPS,)

    def test_auto_signup_entries_only_include_yes_choices(
        self,
        store: EventStore,
    ) -> None:
        event = create_event(store)
        store.set_auto_signup(
            event.event_id,
            1,
            AutoSignupChoice.YES,
            EventRole.DPS,
            (),
        )
        store.set_auto_signup(
            event.event_id,
            2,
            AutoSignupChoice.NO,
            None,
            (),
        )
        store.set_auto_signup(
            event.event_id,
            3,
            AutoSignupChoice.NEVER_ASK,
            None,
            (),
        )

        entries = store.get_auto_signup_entries(event.event_id)

        assert [entry.discord_user_id for entry in entries] == [1]


class TestEventStoreOccurrenceRange:
    def test_range_is_inclusive_start_exclusive_end(
        self,
        store: EventStore,
    ) -> None:
        event = create_event(store)
        before = store.create_occurrence(
            event.event_id,
            datetime(2027, 1, 29, 20, 0, tzinfo=UTC),
        )
        at_start = store.create_occurrence(
            event.event_id,
            datetime(2027, 1, 30, 0, 0, tzinfo=UTC),
        )
        inside = store.create_occurrence(
            event.event_id,
            datetime(2027, 1, 31, 20, 0, tzinfo=UTC),
        )
        at_end = store.create_occurrence(
            event.event_id,
            datetime(2027, 2, 1, 0, 0, tzinfo=UTC),
        )

        results = store.get_occurrences_between(
            datetime(2027, 1, 30, 0, 0, tzinfo=UTC),
            datetime(2027, 2, 1, 0, 0, tzinfo=UTC),
        )

        occurrence_ids = [
            occurrence.occurrence_id for _, occurrence in results
        ]
        assert occurrence_ids == [
            at_start.occurrence_id,
            inside.occurrence_id,
        ]
        assert before.occurrence_id not in occurrence_ids
        assert at_end.occurrence_id not in occurrence_ids

    def test_results_are_ordered_and_paired_with_events(
        self,
        store: EventStore,
    ) -> None:
        first_event = create_event(store, title="First")
        second_event = create_event(store, title="Second")
        later = store.create_occurrence(
            second_event.event_id,
            datetime(2027, 1, 31, 20, 0, tzinfo=UTC),
        )
        earlier = store.create_occurrence(
            first_event.event_id,
            datetime(2027, 1, 30, 20, 0, tzinfo=UTC),
        )

        results = store.get_occurrences_between(
            datetime(2027, 1, 1, 0, 0, tzinfo=UTC),
            datetime(2027, 3, 1, 0, 0, tzinfo=UTC),
        )

        assert [
            (event.event_id, occurrence.occurrence_id)
            for event, occurrence in results
        ] == [
            (first_event.event_id, earlier.occurrence_id),
            (second_event.event_id, later.occurrence_id),
        ]

    def test_cancelled_events_are_excluded(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "gw2bot.db")
        store = EventStore(db_path)
        try:
            event = create_event(store)
            store.create_occurrence(event.event_id, START)
            engine = create_database_engine(db_path)
            with engine.begin() as connection:
                connection.execute(
                    text("UPDATE gw2_events SET cancelled = 1"),
                )
            engine.dispose()

            results = store.get_occurrences_between(
                datetime(2027, 1, 1, 0, 0, tzinfo=UTC),
                datetime(2027, 3, 1, 0, 0, tzinfo=UTC),
            )
        finally:
            store.close()

        assert results == []


class TestEventStoreCalendarBatches:
    def test_signups_between_groups_by_occurrence_in_range(
        self,
        store: EventStore,
    ) -> None:
        event = create_event(store)
        inside = store.create_occurrence(
            event.event_id,
            datetime(2027, 1, 30, 20, 0, tzinfo=UTC),
        )
        outside = store.create_occurrence(
            event.event_id,
            datetime(2027, 3, 30, 20, 0, tzinfo=UTC),
        )
        for occurrence_id in (inside.occurrence_id, outside.occurrence_id):
            store.add_signup(
                occurrence_id=occurrence_id,
                discord_user_id=1,
                role=EventRole.DPS,
                assigned_role=EventRole.DPS,
                flex_roles=(),
                waitlisted=False,
            )

        signups = store.get_signups_between(
            datetime(2027, 1, 1, 0, 0, tzinfo=UTC),
            datetime(2027, 2, 1, 0, 0, tzinfo=UTC),
        )

        assert list(signups) == [inside.occurrence_id]
        assert [
            signup.discord_user_id
            for signup in signups[inside.occurrence_id]
        ] == [1]

    def test_signups_between_matches_per_occurrence_lookup(
        self,
        store: EventStore,
    ) -> None:
        event = create_event(store)
        occurrence = store.create_occurrence(event.event_id, START)
        for user_id in (1, 2, 3):
            store.add_signup(
                occurrence_id=occurrence.occurrence_id,
                discord_user_id=user_id,
                role=EventRole.DPS,
                assigned_role=EventRole.DPS,
                flex_roles=(),
                waitlisted=user_id == 3,
            )

        signups = store.get_signups_between(
            datetime(2027, 1, 1, 0, 0, tzinfo=UTC),
            datetime(2027, 3, 1, 0, 0, tzinfo=UTC),
        )

        assert signups[occurrence.occurrence_id] == store.get_signups(
            occurrence.occurrence_id
        )

    def test_signups_between_excludes_cancelled_events(
        self,
        tmp_path: Path,
    ) -> None:
        db_path = str(tmp_path / "gw2bot.db")
        store = EventStore(db_path)
        try:
            event = create_event(store)
            occurrence = store.create_occurrence(event.event_id, START)
            store.add_signup(
                occurrence_id=occurrence.occurrence_id,
                discord_user_id=1,
                role=EventRole.DPS,
                assigned_role=EventRole.DPS,
                flex_roles=(),
                waitlisted=False,
            )
            engine = create_database_engine(db_path)
            with engine.begin() as connection:
                connection.execute(text("UPDATE gw2_events SET cancelled = 1"))
            engine.dispose()

            signups = store.get_signups_between(
                datetime(2027, 1, 1, 0, 0, tzinfo=UTC),
                datetime(2027, 3, 1, 0, 0, tzinfo=UTC),
            )
        finally:
            store.close()

        assert signups == {}

    def test_latest_occurrence_starts_returns_newest_per_event(
        self,
        store: EventStore,
    ) -> None:
        first = create_event(store, title="First")
        second = create_event(store, title="Second")
        without_occurrences = create_event(store, title="Third")
        store.create_occurrence(
            first.event_id,
            datetime(2027, 1, 10, 20, 0, tzinfo=UTC),
        )
        newest = datetime(2027, 2, 20, 20, 0, tzinfo=UTC)
        store.create_occurrence(first.event_id, newest)
        store.create_occurrence(
            first.event_id,
            datetime(2027, 1, 15, 20, 0, tzinfo=UTC),
        )
        store.create_occurrence(
            second.event_id,
            datetime(2027, 1, 5, 20, 0, tzinfo=UTC),
        )

        starts = store.get_latest_occurrence_starts()

        assert starts[first.event_id] == newest
        assert starts[second.event_id] == datetime(
            2027, 1, 5, 20, 0, tzinfo=UTC
        )
        assert without_occurrences.event_id not in starts


class TestEventStoreLoggingSafety:
    def test_store_logs_never_contain_user_content(
        self,
        store: EventStore,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        title = "SECRET EVENT TITLE"
        description = "SECRET EVENT DESCRIPTION"
        with caplog.at_level("DEBUG"):
            event = create_event(
                store,
                title=title,
                description=description,
            )
            occurrence = store.create_occurrence(event.event_id, START)
            store.add_signup(
                occurrence_id=occurrence.occurrence_id,
                discord_user_id=1,
                role=EventRole.DPS,
                assigned_role=EventRole.DPS,
                flex_roles=(),
                waitlisted=False,
            )
            store.remove_signup(occurrence.occurrence_id, 1)

        assert title not in caplog.text
        assert description not in caplog.text
