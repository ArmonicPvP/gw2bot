from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from gw2bot.events.models import (
    EventCategory,
    EventRole,
    EventStatus,
    RepeatFrequency,
)
from gw2bot.events.store import EventStore
from gw2bot.web.calendar import (
    PROJECTED_STATUS,
    PROJECTION_ENTRY_CAP,
    PROJECTION_STEP_CAP,
    RoleTally,
    ViewerSignup,
    calendar_entries,
)

UTC_ZONE = ZoneInfo("UTC")
NOW = datetime(2027, 1, 1, 0, 0, tzinfo=UTC)


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
        "start_time": datetime(2027, 1, 30, 20, 0, tzinfo=UTC),
        "duration_minutes": 90,
        "repeat_frequency": RepeatFrequency.NONE,
        "repeat_days": (),
    }
    parameters.update(overrides)
    return store.create_event(**parameters)


class TestMaterializedEntries:
    def test_includes_range_start_and_excludes_range_end(
        self,
        store: EventStore,
    ) -> None:
        event = create_event(store)
        at_start = store.create_occurrence(
            event.event_id,
            datetime(2027, 1, 30, 0, 0, tzinfo=UTC),
        )
        store.create_occurrence(
            event.event_id,
            datetime(2027, 2, 1, 0, 0, tzinfo=UTC),
        )

        entries = calendar_entries(
            store,
            UTC_ZONE,
            datetime(2027, 1, 30, 0, 0, tzinfo=UTC),
            datetime(2027, 2, 1, 0, 0, tzinfo=UTC),
            NOW,
        )

        assert [entry.occurrence_id for entry in entries] == [
            at_start.occurrence_id
        ]

    def test_counts_active_and_waitlisted_signups(
        self,
        store: EventStore,
    ) -> None:
        event = create_event(store, category=EventCategory.RAID)
        occurrence = store.create_occurrence(
            event.event_id,
            datetime(2027, 1, 30, 20, 0, tzinfo=UTC),
        )
        store.add_signup(
            occurrence_id=occurrence.occurrence_id,
            discord_user_id=1,
            role=EventRole.QUICKNESS_HEAL,
            assigned_role=EventRole.QUICKNESS_HEAL,
            flex_roles=(),
            waitlisted=False,
        )
        store.add_signup(
            occurrence_id=occurrence.occurrence_id,
            discord_user_id=2,
            role=EventRole.DPS,
            assigned_role=EventRole.DPS,
            flex_roles=(),
            waitlisted=False,
        )
        store.add_signup(
            occurrence_id=occurrence.occurrence_id,
            discord_user_id=3,
            role=EventRole.DPS,
            assigned_role=None,
            flex_roles=(),
            waitlisted=True,
        )

        entries = calendar_entries(
            store,
            UTC_ZONE,
            datetime(2027, 1, 1, 0, 0, tzinfo=UTC),
            datetime(2027, 3, 1, 0, 0, tzinfo=UTC),
            NOW,
        )

        assert len(entries) == 1
        entry = entries[0]
        assert entry.active_count == 2
        assert entry.waitlist_count == 1
        assert entry.roles == (
            RoleTally("Healers", 1),
            RoleTally("DPS", 1),
            RoleTally("Quickness", 1),
            RoleTally("Alacrity", 0),
        )
        assert entry.capacity_total == 10
        assert entry.status == "open"
        assert not entry.projected

    def test_wvw_entry_has_no_roles_and_flat_capacity(
        self,
        store: EventStore,
    ) -> None:
        event = create_event(store, category=EventCategory.WVW)
        store.create_occurrence(
            event.event_id,
            datetime(2027, 1, 30, 20, 0, tzinfo=UTC),
        )

        entries = calendar_entries(
            store,
            UTC_ZONE,
            datetime(2027, 1, 1, 0, 0, tzinfo=UTC),
            datetime(2027, 3, 1, 0, 0, tzinfo=UTC),
            NOW,
        )

        assert len(entries) == 1
        entry = entries[0]
        assert entry.roles == ()
        assert entry.capacity_total == 50

    def test_general_entry_reports_an_absent_capacity(
        self,
        store: EventStore,
    ) -> None:
        event = create_event(store, category=EventCategory.GENERAL)
        store.create_occurrence(
            event.event_id,
            datetime(2027, 1, 30, 20, 0, tzinfo=UTC),
        )

        entries = calendar_entries(
            store,
            UTC_ZONE,
            datetime(2027, 1, 1, 0, 0, tzinfo=UTC),
            datetime(2027, 3, 1, 0, 0, tzinfo=UTC),
            NOW,
        )

        assert len(entries) == 1
        entry = entries[0]
        assert entry.roles == ()
        # None rather than a number: the page drops the denominator instead of
        # printing a cap the event does not have.
        assert entry.capacity_total is None

    def test_dungeon_entry_lists_no_healer_seat(
        self,
        store: EventStore,
    ) -> None:
        event = create_event(store, category=EventCategory.DUNGEON)
        occurrence = store.create_occurrence(
            event.event_id,
            datetime(2027, 1, 30, 20, 0, tzinfo=UTC),
        )
        store.add_signup(
            occurrence_id=occurrence.occurrence_id,
            discord_user_id=1,
            role=EventRole.ALACRITY_DPS,
            assigned_role=EventRole.ALACRITY_DPS,
            flex_roles=(),
            waitlisted=False,
        )

        entries = calendar_entries(
            store,
            UTC_ZONE,
            datetime(2027, 1, 1, 0, 0, tzinfo=UTC),
            datetime(2027, 3, 1, 0, 0, tzinfo=UTC),
            NOW,
        )

        # A dungeon has no healer seat, so it is not counted as an empty one.
        assert [entry.roles for entry in entries] == [
            (
                RoleTally("DPS", 1),
                RoleTally("Quickness", 0),
                RoleTally("Alacrity", 1),
            )
        ]

    def test_pvp_entry_lists_a_support_and_no_boons(
        self,
        store: EventStore,
    ) -> None:
        event = create_event(store, category=EventCategory.PVP)
        occurrence = store.create_occurrence(
            event.event_id,
            datetime(2027, 1, 30, 20, 0, tzinfo=UTC),
        )
        for user_id, role in (
            (1, EventRole.SUPPORT),
            (2, EventRole.DPS),
            (3, EventRole.DPS),
        ):
            store.add_signup(
                occurrence_id=occurrence.occurrence_id,
                discord_user_id=user_id,
                role=role,
                assigned_role=role,
                flex_roles=(),
                waitlisted=False,
            )

        entries = calendar_entries(
            store,
            UTC_ZONE,
            datetime(2027, 1, 1, 0, 0, tzinfo=UTC),
            datetime(2027, 3, 1, 0, 0, tzinfo=UTC),
            NOW,
        )

        assert [entry.roles for entry in entries] == [
            (RoleTally("Support", 1), RoleTally("DPS", 2))
        ]

    def test_fractal_entry_lists_every_seat_kind(
        self,
        store: EventStore,
    ) -> None:
        event = create_event(store, category=EventCategory.FRACTAL)
        store.create_occurrence(
            event.event_id,
            datetime(2027, 1, 30, 20, 0, tzinfo=UTC),
        )

        entries = calendar_entries(
            store,
            UTC_ZONE,
            datetime(2027, 1, 1, 0, 0, tzinfo=UTC),
            datetime(2027, 3, 1, 0, 0, tzinfo=UTC),
            NOW,
        )

        assert [entry.roles for entry in entries] == [
            (
                RoleTally("Healers", 0),
                RoleTally("DPS", 0),
                RoleTally("Quickness", 0),
                RoleTally("Alacrity", 0),
            )
        ]

    @pytest.mark.parametrize(
        "category",
        [EventCategory.STORY, EventCategory.OPEN_WORLD],
    )
    def test_role_less_entries_list_no_seat_kinds(
        self,
        store: EventStore,
        category: EventCategory,
    ) -> None:
        event = create_event(store, category=category)
        store.create_occurrence(
            event.event_id,
            datetime(2027, 1, 30, 20, 0, tzinfo=UTC),
        )

        entries = calendar_entries(
            store,
            UTC_ZONE,
            datetime(2027, 1, 1, 0, 0, tzinfo=UTC),
            datetime(2027, 3, 1, 0, 0, tzinfo=UTC),
            NOW,
        )

        assert [entry.roles for entry in entries] == [()]

    def test_entries_carry_the_requirements(
        self,
        store: EventStore,
    ) -> None:
        event = create_event(store, requirements="Level 80")
        store.create_occurrence(
            event.event_id,
            datetime(2027, 1, 30, 20, 0, tzinfo=UTC),
        )

        entries = calendar_entries(
            store,
            UTC_ZONE,
            datetime(2027, 1, 1, 0, 0, tzinfo=UTC),
            datetime(2027, 3, 1, 0, 0, tzinfo=UTC),
            NOW,
        )

        assert [entry.requirements for entry in entries] == ["Level 80"]

    def test_past_occurrence_is_reported_over(
        self,
        store: EventStore,
    ) -> None:
        event = create_event(store)
        store.create_occurrence(
            event.event_id,
            datetime(2026, 12, 1, 20, 0, tzinfo=UTC),
        )

        entries = calendar_entries(
            store,
            UTC_ZONE,
            datetime(2026, 12, 1, 0, 0, tzinfo=UTC),
            datetime(2026, 12, 15, 0, 0, tzinfo=UTC),
            NOW,
        )

        assert len(entries) == 1
        assert entries[0].status == "over"


    def test_a_run_retired_early_reads_as_over(
        self,
        store: EventStore,
    ) -> None:
        # A run retires before its end time when its message turns out to be
        # gone. Derived from the schedule alone it would advertise itself as
        # open until that end - for good, once a deleted event's kept runs
        # leave the maintenance pass.
        event = create_event(store)
        occurrence = store.create_occurrence(
            event.event_id,
            datetime(2027, 1, 30, 20, 0, tzinfo=UTC),
        )
        store.set_occurrence_status(occurrence.occurrence_id, EventStatus.OVER)

        entries = calendar_entries(
            store,
            UTC_ZONE,
            datetime(2027, 1, 1, 0, 0, tzinfo=UTC),
            datetime(2027, 2, 1, 0, 0, tzinfo=UTC),
            datetime(2027, 1, 30, 20, 30, tzinfo=UTC),
        )

        assert [entry.status for entry in entries] == [EventStatus.OVER.value]

    def test_keeps_the_runs_of_a_deleted_event(
        self,
        store: EventStore,
    ) -> None:
        # `/event delete` keeps the runs an event has already put on, so the
        # calendar goes on showing them on the days they were run - and
        # projects nothing further for a series that has ended.
        anchor = datetime(2027, 1, 6, 20, 0, tzinfo=UTC)
        event = create_event(
            store,
            start_time=anchor,
            repeat_frequency=RepeatFrequency.WEEKLY,
            repeat_days=(2,),
        )
        finished = store.create_occurrence(event.event_id, anchor)
        store.set_occurrence_message(finished.occurrence_id, 1234, 555, 777)
        upcoming = store.create_occurrence(
            event.event_id,
            anchor + timedelta(days=7),
        )

        store.retire_event(event.event_id, [upcoming.occurrence_id])

        entries = calendar_entries(
            store,
            UTC_ZONE,
            datetime(2027, 1, 1, 0, 0, tzinfo=UTC),
            datetime(2027, 2, 1, 0, 0, tzinfo=UTC),
            anchor + timedelta(days=10),
        )

        assert [entry.occurrence_id for entry in entries] == [
            finished.occurrence_id
        ]
        assert not entries[0].projected


class TestViewerSignup:
    @staticmethod
    def _entries(store: EventStore, viewer_discord_id: int | None):
        return calendar_entries(
            store,
            UTC_ZONE,
            datetime(2027, 1, 1, 0, 0, tzinfo=UTC),
            datetime(2027, 3, 1, 0, 0, tzinfo=UTC),
            NOW,
            viewer_discord_id=viewer_discord_id,
        )

    @staticmethod
    def _occurrence(store: EventStore, **overrides: object):
        event = create_event(store, **overrides)
        return store.create_occurrence(
            event.event_id,
            datetime(2027, 1, 30, 20, 0, tzinfo=UTC),
        )

    def test_marks_the_seat_the_viewer_holds(
        self,
        store: EventStore,
    ) -> None:
        occurrence = self._occurrence(store, category=EventCategory.RAID)
        store.add_signup(
            occurrence_id=occurrence.occurrence_id,
            discord_user_id=7,
            role=EventRole.DPS,
            assigned_role=EventRole.QUICKNESS_DPS,
            flex_roles=(EventRole.QUICKNESS_DPS,),
            waitlisted=False,
        )

        entries = self._entries(store, viewer_discord_id=7)

        # The seat they were given, not the one they asked for first.
        assert [entry.viewer_signup for entry in entries] == [
            ViewerSignup(role="Quickness DPS", waitlisted=False)
        ]

    def test_marks_a_waitlisted_viewer_without_a_seat(
        self,
        store: EventStore,
    ) -> None:
        occurrence = self._occurrence(store, category=EventCategory.RAID)
        store.add_signup(
            occurrence_id=occurrence.occurrence_id,
            discord_user_id=7,
            role=EventRole.DPS,
            assigned_role=None,
            flex_roles=(),
            waitlisted=True,
        )

        entries = self._entries(store, viewer_discord_id=7)

        assert [entry.viewer_signup for entry in entries] == [
            ViewerSignup(role=None, waitlisted=True)
        ]

    def test_role_less_signup_names_no_seat(
        self,
        store: EventStore,
    ) -> None:
        occurrence = self._occurrence(store, category=EventCategory.WVW)
        store.add_signup(
            occurrence_id=occurrence.occurrence_id,
            discord_user_id=7,
            role=None,
            assigned_role=None,
            flex_roles=(),
            waitlisted=False,
        )

        entries = self._entries(store, viewer_discord_id=7)

        assert [entry.viewer_signup for entry in entries] == [
            ViewerSignup(role=None, waitlisted=False)
        ]

    def test_someone_elses_signup_is_not_the_viewers(
        self,
        store: EventStore,
    ) -> None:
        occurrence = self._occurrence(store, category=EventCategory.RAID)
        store.add_signup(
            occurrence_id=occurrence.occurrence_id,
            discord_user_id=8,
            role=EventRole.DPS,
            assigned_role=EventRole.DPS,
            flex_roles=(),
            waitlisted=False,
        )

        entries = self._entries(store, viewer_discord_id=7)

        assert [entry.viewer_signup for entry in entries] == [None]
        assert [entry.active_count for entry in entries] == [1]

    def test_no_viewer_marks_nothing(
        self,
        store: EventStore,
    ) -> None:
        occurrence = self._occurrence(store, category=EventCategory.RAID)
        store.add_signup(
            occurrence_id=occurrence.occurrence_id,
            discord_user_id=7,
            role=EventRole.DPS,
            assigned_role=EventRole.DPS,
            flex_roles=(),
            waitlisted=False,
        )

        entries = self._entries(store, viewer_discord_id=None)

        assert [entry.viewer_signup for entry in entries] == [None]

    def test_projection_carries_no_viewer_signup(
        self,
        store: EventStore,
    ) -> None:
        anchor = datetime(2027, 1, 6, 20, 0, tzinfo=UTC)
        event = create_event(
            store,
            category=EventCategory.RAID,
            start_time=anchor,
            repeat_frequency=RepeatFrequency.WEEKLY,
            repeat_days=(2,),
        )
        occurrence = store.create_occurrence(event.event_id, anchor)
        store.add_signup(
            occurrence_id=occurrence.occurrence_id,
            discord_user_id=7,
            role=EventRole.DPS,
            assigned_role=EventRole.DPS,
            flex_roles=(),
            waitlisted=False,
        )

        entries = calendar_entries(
            store,
            UTC_ZONE,
            datetime(2027, 1, 1, 0, 0, tzinfo=UTC),
            datetime(2027, 1, 17, 0, 0, tzinfo=UTC),
            NOW,
            viewer_discord_id=7,
        )

        # Being on one run's roster says nothing about the next, which nobody
        # can sign up for until it is posted.
        assert [
            (entry.projected, entry.viewer_signup is not None)
            for entry in entries
        ] == [(False, True), (True, False)]


class TestProjectedEntries:
    def test_weekly_projection_holds_local_time_across_dst(
        self,
        store: EventStore,
    ) -> None:
        new_york = ZoneInfo("America/New_York")
        # Wednesday 2027-03-10 20:00 EST; the US switches to EDT on
        # 2027-03-14, so the next Wednesday is 20:00 EDT.
        anchor = datetime(2027, 3, 10, 20, 0, tzinfo=new_york)
        event = create_event(
            store,
            start_time=anchor,
            repeat_frequency=RepeatFrequency.WEEKLY,
            repeat_days=(2,),
        )
        store.create_occurrence(event.event_id, anchor)

        entries = calendar_entries(
            store,
            new_york,
            datetime(2027, 3, 8, 0, 0, tzinfo=UTC),
            datetime(2027, 3, 22, 0, 0, tzinfo=UTC),
            NOW,
        )

        assert len(entries) == 2
        materialized, projected = entries
        assert not materialized.projected
        assert projected.projected
        assert projected.occurrence_id is None
        assert projected.status == PROJECTED_STATUS
        # Local wall-clock time holds, so the UTC gap shrinks by the
        # skipped DST hour.
        week_seconds = 7 * 24 * 60 * 60
        assert (
            projected.start_epoch - materialized.start_epoch
            == week_seconds - 3600
        )

    def test_projection_lists_its_category_seat_kinds_empty(
        self,
        store: EventStore,
    ) -> None:
        anchor = datetime(2027, 1, 6, 20, 0, tzinfo=UTC)
        event = create_event(
            store,
            category=EventCategory.PVP,
            start_time=anchor,
            repeat_frequency=RepeatFrequency.WEEKLY,
            repeat_days=(2,),
        )
        store.create_occurrence(event.event_id, anchor)

        entries = calendar_entries(
            store,
            UTC_ZONE,
            datetime(2027, 1, 10, 0, 0, tzinfo=UTC),
            datetime(2027, 1, 17, 0, 0, tzinfo=UTC),
            NOW,
        )

        assert [entry.projected for entry in entries] == [True]
        assert entries[0].roles == (
            RoleTally("Support", 0),
            RoleTally("DPS", 0),
        )

    def test_monthly_projection_clamps_to_short_months(
        self,
        store: EventStore,
    ) -> None:
        anchor = datetime(2027, 1, 31, 20, 0, tzinfo=UTC)
        event = create_event(
            store,
            start_time=anchor,
            repeat_frequency=RepeatFrequency.MONTHLY,
            repeat_days=(31,),
        )
        store.create_occurrence(event.event_id, anchor)

        entries = calendar_entries(
            store,
            UTC_ZONE,
            datetime(2027, 2, 1, 0, 0, tzinfo=UTC),
            datetime(2027, 3, 1, 0, 0, tzinfo=UTC),
            NOW,
        )

        assert [entry.start_epoch for entry in entries] == [
            int(datetime(2027, 2, 28, 20, 0, tzinfo=UTC).timestamp())
        ]

    def test_materialized_occurrence_wins_over_projection(
        self,
        store: EventStore,
    ) -> None:
        anchor = datetime(2027, 1, 4, 20, 0, tzinfo=UTC)
        event = create_event(
            store,
            start_time=anchor,
            repeat_frequency=RepeatFrequency.DAILY,
        )
        store.create_occurrence(event.event_id, anchor)
        # The scheduler has already materialized the next slot.
        next_slot = store.create_occurrence(
            event.event_id,
            datetime(2027, 1, 5, 20, 0, tzinfo=UTC),
        )

        entries = calendar_entries(
            store,
            UTC_ZONE,
            datetime(2027, 1, 4, 0, 0, tzinfo=UTC),
            datetime(2027, 1, 7, 0, 0, tzinfo=UTC),
            NOW,
        )

        assert [
            (entry.occurrence_id, entry.projected) for entry in entries
        ] == [
            (next_slot.occurrence_id - 1, False),
            (next_slot.occurrence_id, False),
            (None, True),
        ]

    def test_non_repeating_events_are_not_projected(
        self,
        store: EventStore,
    ) -> None:
        event = create_event(store)
        store.create_occurrence(
            event.event_id,
            datetime(2027, 1, 30, 20, 0, tzinfo=UTC),
        )

        entries = calendar_entries(
            store,
            UTC_ZONE,
            datetime(2027, 2, 1, 0, 0, tzinfo=UTC),
            datetime(2027, 3, 1, 0, 0, tzinfo=UTC),
            NOW,
        )

        assert entries == []

    def test_daily_event_still_projects_far_beyond_the_anchor(
        self,
        store: EventStore,
    ) -> None:
        # Browsing months ahead leaves range_start far past the newest
        # materialized occurrence. Steps spent fast-forwarding to the window
        # must not exhaust the projection budget, or a daily event would
        # silently vanish from the calendar.
        anchor = datetime(2027, 1, 1, 20, 0, tzinfo=UTC)
        event = create_event(
            store,
            start_time=anchor,
            repeat_frequency=RepeatFrequency.DAILY,
        )
        store.create_occurrence(event.event_id, anchor)

        entries = calendar_entries(
            store,
            UTC_ZONE,
            datetime(2028, 3, 1, 0, 0, tzinfo=UTC),
            datetime(2028, 3, 8, 0, 0, tzinfo=UTC),
            NOW,
        )

        assert [entry.start_epoch for entry in entries] == [
            int(
                datetime(2028, 3, day, 20, 0, tzinfo=UTC).timestamp()
            )
            for day in range(1, 8)
        ]
        assert all(entry.projected for entry in entries)

    def test_projection_is_bounded_by_step_cap(
        self,
        store: EventStore,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        anchor = datetime(2027, 1, 1, 20, 0, tzinfo=UTC)
        event = create_event(
            store,
            start_time=anchor,
            repeat_frequency=RepeatFrequency.DAILY,
        )
        store.create_occurrence(event.event_id, anchor)

        # A window further from the anchor than the step cap can walk yields
        # nothing rather than looping unbounded.
        with caplog.at_level("WARNING"):
            entries = calendar_entries(
                store,
                UTC_ZONE,
                datetime(2050, 1, 1, 0, 0, tzinfo=UTC),
                datetime(2050, 2, 1, 0, 0, tzinfo=UTC),
                NOW,
            )

        assert entries == []
        assert PROJECTION_STEP_CAP == 4000
        # An empty month for a series that does recur then is indistinguishable
        # from a genuinely empty one unless the truncation is said out loud.
        assert "Stopping projection at step cap" in caplog.text
        assert str(event.event_id) in caplog.text

    def test_reachable_window_does_not_warn(
        self,
        store: EventStore,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        anchor = datetime(2027, 1, 1, 20, 0, tzinfo=UTC)
        event = create_event(
            store,
            start_time=anchor,
            repeat_frequency=RepeatFrequency.DAILY,
        )
        store.create_occurrence(event.event_id, anchor)

        with caplog.at_level("WARNING"):
            entries = calendar_entries(
                store,
                UTC_ZONE,
                datetime(2027, 2, 1, 0, 0, tzinfo=UTC),
                datetime(2027, 2, 8, 0, 0, tzinfo=UTC),
                NOW,
            )

        assert entries
        assert "Stopping projection" not in caplog.text

    def test_projection_is_bounded_by_entry_cap(
        self,
        store: EventStore,
    ) -> None:
        anchor = datetime(2027, 1, 1, 20, 0, tzinfo=UTC)
        event = create_event(
            store,
            start_time=anchor,
            repeat_frequency=RepeatFrequency.DAILY,
        )
        store.create_occurrence(event.event_id, anchor)

        # A window wider than a daily repeat's entry budget stops at the cap
        # instead of projecting an unbounded number of entries.
        entries = calendar_entries(
            store,
            UTC_ZONE,
            anchor,
            anchor + timedelta(days=PROJECTION_ENTRY_CAP + 50),
            NOW,
        )

        projected = [entry for entry in entries if entry.projected]
        assert len(projected) == PROJECTION_ENTRY_CAP
