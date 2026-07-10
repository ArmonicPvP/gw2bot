from datetime import UTC, datetime
from pathlib import Path

import pytest

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


class TestEventStoreOccurrences:
    def test_create_occurrence_and_store_message(
        self,
        store: EventStore,
    ) -> None:
        event = create_event(store)

        occurrence = store.create_occurrence(event.event_id, START)
        store.set_occurrence_message(occurrence.occurrence_id, 555, 777)
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
        store.set_occurrence_message(posted.occurrence_id, 1, None)
        store.set_occurrence_message(finished.occurrence_id, 2, None)
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
        store.set_occurrence_message(posted.occurrence_id, 1, None)

        pending = store.get_unposted_occurrences()

        assert [entry.occurrence_id for entry in pending] == [
            unposted.occurrence_id
        ]

    def test_has_posted_occurrence(self, store: EventStore) -> None:
        event = create_event(store)
        occurrence = store.create_occurrence(event.event_id, START)

        assert not store.has_posted_occurrence(event.event_id)

        store.set_occurrence_message(occurrence.occurrence_id, 1, None)

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

    def test_has_later_occurrence(self, store: EventStore) -> None:
        event = create_event(store)
        store.create_occurrence(event.event_id, START)

        assert not store.has_later_occurrence(event.event_id, START)

        later = START.replace(hour=22)
        store.create_occurrence(event.event_id, later)

        assert store.has_later_occurrence(event.event_id, START)


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
