from gw2bot.events.models import EventRun, EventRunParticipant, MenteeStatus
from gw2bot.events.stats import (
    EVENT_STATS_RANGES,
    build_event_stats,
    has_no_requirements,
)

HOUR = 3600.0


def run(
    run_id: int,
    ended_at: float,
    *,
    event_id: int = 1,
    event_created_at: str = "2026-01-01T00:00:00+00:00",
    title: str = "Kitty Cleanup",
    leader: int = 42,
    requirements: str = "Bring food.",
    mentee_enabled: bool = False,
    duration_minutes: int = 90,
    participants: tuple[EventRunParticipant, ...] = (),
) -> EventRun:
    return EventRun(
        run_id=run_id,
        occurrence_id=run_id,
        event_id=event_id,
        event_created_at=event_created_at,
        category="Fractal",
        title=title,
        leader_discord_id=leader,
        requirements=requirements,
        mentee_enabled=mentee_enabled,
        started_at=ended_at - duration_minutes * 60,
        ended_at=ended_at,
        duration_minutes=duration_minutes,
        participants=participants,
    )


def seated(
    user_id: int,
    mentee: MenteeStatus = MenteeStatus.NONE,
) -> EventRunParticipant:
    return EventRunParticipant(user_id, False, mentee)


def waiting(
    user_id: int,
    mentee: MenteeStatus = MenteeStatus.NONE,
) -> EventRunParticipant:
    return EventRunParticipant(user_id, True, mentee)


def test_the_ranges_match_the_other_dashboards() -> None:
    assert EVENT_STATS_RANGES == {
        "24h": 24 * 60 * 60,
        "7d": 7 * 24 * 60 * 60,
        "30d": 30 * 24 * 60 * 60,
    }


def test_an_empty_window_is_all_zeroes() -> None:
    stats = build_event_stats([])

    assert (stats.runs, stats.total_minutes, stats.participants) == (0, 0, 0)
    assert stats.top_commanders == ()
    assert stats.top_commander_runs == 0
    assert stats.points == ()
    assert stats.mentees == ()
    assert stats.without_mentee == ()
    assert stats.without_requirements == ()


def test_totals_count_runs_and_their_scheduled_time() -> None:
    stats = build_event_stats(
        [
            run(1, HOUR, duration_minutes=90),
            run(2, 2 * HOUR, duration_minutes=45),
        ]
    )

    assert stats.runs == 2
    assert stats.total_minutes == 135


def test_participants_are_distinct_seated_members_and_commanders() -> None:
    stats = build_event_stats(
        [
            run(1, HOUR, leader=42, participants=(seated(7), seated(8))),
            # 7 again, a commander who also played, and a waitlisted member
            # who never got a seat.
            run(
                2,
                2 * HOUR,
                leader=43,
                participants=(seated(7), seated(42), waiting(9)),
            ),
        ]
    )

    assert stats.participants == 4  # 7, 8, 42, 43


def test_the_top_commander_is_whoever_led_the_most_runs() -> None:
    stats = build_event_stats(
        [
            run(1, HOUR, leader=42),
            run(2, 2 * HOUR, leader=43),
            run(3, 3 * HOUR, leader=42),
        ]
    )

    assert stats.top_commanders == (42,)
    assert stats.top_commander_runs == 2


def test_tied_commanders_are_all_named() -> None:
    stats = build_event_stats(
        [run(1, HOUR, leader=43), run(2, 2 * HOUR, leader=42)]
    )

    assert stats.top_commanders == (42, 43)
    assert stats.top_commander_runs == 1


def test_the_line_climbs_by_one_at_each_run_end_in_order() -> None:
    stats = build_event_stats(
        [
            run(2, 3 * HOUR, title="Second"),
            run(1, HOUR, title="First", leader=7),
        ]
    )

    assert [
        (point.at, point.runs, point.title, point.leader_discord_id)
        for point in stats.points
    ] == [(HOUR, 1, "First", 7), (3 * HOUR, 2, "Second", 42)]


def test_mentees_count_every_ask_and_every_slot_they_held() -> None:
    stats = build_event_stats(
        [
            run(
                1,
                HOUR,
                participants=(
                    seated(7, MenteeStatus.MENTEE),
                    seated(8, MenteeStatus.WAITLISTED),
                    seated(9),
                ),
            ),
            run(
                2,
                2 * HOUR,
                participants=(
                    seated(8, MenteeStatus.MENTEE),
                    # Asking from the waitlist is still asking.
                    waiting(10, MenteeStatus.WAITLISTED),
                ),
            ),
            run(3, 3 * HOUR, participants=(seated(7, MenteeStatus.MENTEE),)),
        ]
    )

    assert [
        (row.discord_user_id, row.asked, row.completed)
        for row in stats.mentees
    ] == [(7, 2, 2), (8, 2, 1), (10, 1, 0)]


def test_runs_without_a_mentee_are_grouped_by_event() -> None:
    stats = build_event_stats(
        [
            run(1, HOUR, event_id=1, title="Old Title", mentee_enabled=True),
            run(2, 2 * HOUR, event_id=2, title="Raid"),
            run(
                3,
                3 * HOUR,
                event_id=1,
                participants=(seated(7, MenteeStatus.MENTEE),),
            ),
            run(4, 4 * HOUR, event_id=1, title="New Title"),
        ]
    )

    # Newest first, each event once, named and set up as its latest run was.
    assert [
        (
            group.event_id,
            group.title,
            group.runs,
            group.last_ended_at,
            group.mentee_enabled,
        )
        for group in stats.without_mentee
    ] == [
        (1, "New Title", 2, 4 * HOUR, False),
        (2, "Raid", 1, 2 * HOUR, False),
    ]


def test_a_mentee_still_waiting_does_not_count_as_having_one() -> None:
    stats = build_event_stats(
        [run(1, HOUR, participants=(waiting(7, MenteeStatus.WAITLISTED),))]
    )

    assert [group.event_id for group in stats.without_mentee] == [1]


def test_runs_whose_requirements_read_as_none_are_grouped() -> None:
    stats = build_event_stats(
        [
            run(1, HOUR, event_id=1, requirements=""),
            run(2, 2 * HOUR, event_id=2, requirements="Bring food."),
            run(3, 3 * HOUR, event_id=3, requirements="  None "),
            run(4, 4 * HOUR, event_id=1, requirements="none"),
        ]
    )

    assert [
        (group.event_id, group.runs) for group in stats.without_requirements
    ] == [(1, 2), (3, 1)]


def test_requirements_read_as_none_the_way_the_post_shows_them() -> None:
    assert has_no_requirements("")
    assert has_no_requirements("   ")
    assert has_no_requirements("None")
    assert has_no_requirements("NONE")
    assert not has_no_requirements("None of your gear may be exotic.")
    assert not has_no_requirements("Ascended armor")


def test_a_reused_event_id_is_not_merged_with_the_deleted_event() -> None:
    # SQLite hands a deleted event's id to the next one created, and the run
    # history outlives the deletion, so the id alone would fold two unrelated
    # events into one row.
    stats = build_event_stats(
        [
            run(
                1,
                HOUR,
                title="Deleted Event",
                requirements="",
                event_created_at="2026-01-01T00:00:00+00:00",
            ),
            run(
                2,
                2 * HOUR,
                title="New Event",
                requirements="",
                event_created_at="2026-02-01T00:00:00+00:00",
            ),
        ]
    )

    assert [
        (group.title, group.runs) for group in stats.without_requirements
    ] == [("New Event", 1), ("Deleted Event", 1)]
    assert [
        (group.title, group.runs) for group in stats.without_mentee
    ] == [("New Event", 1), ("Deleted Event", 1)]
