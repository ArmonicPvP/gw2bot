"""What the events statistics page reads out of the run history.

Pure arithmetic over the runs one window holds: nothing here reads the store
or asks Discord for a name. Every figure is about that window alone - the
cumulative line starts from nothing at its opening edge, and the totals and
the top commander count only the runs that ended inside it. The one number
that reaches past the window, a mentee's runs over the whole history, is read
by the store and handed to the server separately.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from gw2bot.events.formatting import NO_REQUIREMENTS_TEXT
from gw2bot.events.models import EventRun, MenteeStatus

# Time windows the events statistics page offers, mapped to their length in
# seconds. The keys are the values the ``/api/admin/events?range=`` query
# accepts; they mirror the other dashboards so every page reads the same way.
EVENT_STATS_RANGES: dict[str, int] = {
    "24h": 24 * 60 * 60,
    "7d": 7 * 24 * 60 * 60,
    "30d": 30 * 24 * 60 * 60,
}


@dataclass(frozen=True, slots=True)
class RunPoint:
    """One run on the cumulative line: when it ended and the count after it."""

    at: float
    runs: int
    title: str
    leader_discord_id: int


@dataclass(frozen=True, slots=True)
class MenteeActivity:
    """One member who asked for a mentee slot on a run in the window.

    ``asked`` counts every run they asked on, whether they held the slot or
    waited for it; ``completed`` counts the runs that ended with them holding
    it.
    """

    discord_user_id: int
    asked: int
    completed: int


@dataclass(frozen=True, slots=True)
class EventRunGroup:
    """The runs of one event that match a table's question.

    Grouped by event rather than listed per run, so a weekly raid reads as one
    row with a count instead of four identical ones. An event is its id and
    its creation time together: SQLite hands a deleted event's id to the next
    one created, and the history outlives the deletion. The title, commander,
    category and mentee setting are the latest matching run's, which is what
    the event looked like most recently.
    """

    event_id: int
    title: str
    category: str
    leader_discord_id: int
    mentee_enabled: bool
    runs: int
    last_ended_at: float


@dataclass(frozen=True, slots=True)
class EventStats:
    runs: int
    total_minutes: int
    # Distinct members who took part: everyone seated on a run's roster, and
    # the commander who led it whether or not they signed up to it.
    participants: int
    # Every commander tied for the most runs, and how many that is. Empty,
    # with a count of zero, when the window holds no runs.
    top_commanders: tuple[int, ...]
    top_commander_runs: int
    points: tuple[RunPoint, ...]
    mentees: tuple[MenteeActivity, ...]
    without_mentee: tuple[EventRunGroup, ...]
    without_requirements: tuple[EventRunGroup, ...]


def has_no_requirements(requirements: str) -> bool:
    """Whether a run's requirements read as none on its post.

    A blank field is shown as "None", and a commander may have typed the word
    themselves, so both are the same answer.
    """
    stripped = requirements.strip().casefold()
    return not stripped or stripped == NO_REQUIREMENTS_TEXT.casefold()


def run_had_mentee(run: EventRun) -> bool:
    """Whether somebody held the run's mentee slot when it ended."""
    return any(
        participant.mentee is MenteeStatus.MENTEE
        for participant in run.participants
    )


def _group_runs(runs: Sequence[EventRun]) -> tuple[EventRunGroup, ...]:
    groups: dict[tuple[int, str], EventRunGroup] = {}
    for run in sorted(runs, key=lambda run: (run.ended_at, run.run_id)):
        key = (run.event_id, run.event_created_at)
        previous = groups.get(key)
        groups[key] = EventRunGroup(
            event_id=run.event_id,
            title=run.title,
            category=run.category,
            leader_discord_id=run.leader_discord_id,
            mentee_enabled=run.mentee_enabled,
            runs=1 if previous is None else previous.runs + 1,
            last_ended_at=run.ended_at,
        )
    # The event run most recently comes first, the way the other tables on
    # the site read newest first.
    return tuple(
        sorted(
            groups.values(),
            key=lambda group: (-group.last_ended_at, group.event_id),
        )
    )


def build_event_stats(runs: Sequence[EventRun]) -> EventStats:
    """Every figure the events statistics page shows for one window's runs.

    ``runs`` is what the store read for the window, so every run in it ended
    inside it; nothing here re-checks the edges.
    """
    ordered = sorted(runs, key=lambda run: (run.ended_at, run.run_id))
    points = tuple(
        RunPoint(
            at=run.ended_at,
            runs=index,
            title=run.title,
            leader_discord_id=run.leader_discord_id,
        )
        for index, run in enumerate(ordered, start=1)
    )

    leaders = Counter(run.leader_discord_id for run in ordered)
    top_count = max(leaders.values(), default=0)
    top_commanders = tuple(
        sorted(
            leader_id
            for leader_id, count in leaders.items()
            if count == top_count
        )
    )

    participants: set[int] = set(leaders)
    asked: Counter[int] = Counter()
    completed: Counter[int] = Counter()
    for run in ordered:
        for participant in run.participants:
            if not participant.waitlisted:
                participants.add(participant.discord_user_id)
            if participant.mentee is MenteeStatus.NONE:
                continue
            asked[participant.discord_user_id] += 1
            if participant.mentee is MenteeStatus.MENTEE:
                completed[participant.discord_user_id] += 1
    mentees = tuple(
        sorted(
            (
                MenteeActivity(
                    discord_user_id=user_id,
                    asked=count,
                    completed=completed[user_id],
                )
                for user_id, count in asked.items()
            ),
            key=lambda row: (-row.completed, -row.asked, row.discord_user_id),
        )
    )

    return EventStats(
        runs=len(ordered),
        total_minutes=sum(run.duration_minutes for run in ordered),
        participants=len(participants),
        top_commanders=top_commanders,
        top_commander_runs=top_count,
        points=points,
        mentees=mentees,
        without_mentee=_group_runs(
            [run for run in ordered if not run_had_mentee(run)]
        ),
        without_requirements=_group_runs(
            [run for run in ordered if has_no_requirements(run.requirements)]
        ),
    )
