from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from gw2bot.events.formatting import next_occurrence_start
from gw2bot.events.models import (
    CategoryCapacity,
    Event,
    EventSignup,
    EventStatus,
    RepeatFrequency,
    RosterCounts,
    count_roster,
)
from gw2bot.events.posting import occurrence_status
from gw2bot.events.store import EventStore

LOGGER = logging.getLogger(__name__)

# Status shown for occurrences that only exist as recurrence projections.
PROJECTED_STATUS = "scheduled"

# Hard bound on recurrence steps per event so a corrupt anchor far in the
# past, or a recurrence that fails to advance, cannot spin the loop
# unbounded. Steps spent fast-forwarding a stale anchor up to the requested
# window are cheap and must not count against what the window may emit, so
# this is large enough to walk a daily repeat across several years.
PROJECTION_STEP_CAP = 4000

# Bound on entries projected per event. The API caps a range at 62 days, so
# even a daily repeat cannot legitimately exceed this.
PROJECTION_ENTRY_CAP = 100


@dataclass(frozen=True, slots=True)
class RoleTally:
    """One kind of seat an event has, and how many of them are taken."""

    label: str
    count: int


def role_tallies(
    capacity: CategoryCapacity,
    counts: RosterCounts,
) -> tuple[RoleTally, ...]:
    """The kinds of seat a category has, each with its seated count.

    Only the seats the category actually has are listed: a dungeon has no
    healer and PvP has no boons, and a "0" beside a seat nobody can take
    reads as one still waiting for somebody. PvP's heal seat is its Support,
    named as the event post names it. A role-less category has none.
    """
    if not capacity.has_roles:
        return ()
    tallies: list[RoleTally] = []
    if capacity.healers:
        label = "Support" if capacity.heals_are_supports else "Healers"
        tallies.append(RoleTally(label, counts.healers))
    if capacity.dps:
        tallies.append(RoleTally("DPS", counts.dps))
    if capacity.quickness:
        tallies.append(RoleTally("Quickness", counts.quickness))
    if capacity.alacrity:
        tallies.append(RoleTally("Alacrity", counts.alacrity))
    return tuple(tallies)


@dataclass(frozen=True, slots=True)
class ViewerSignup:
    """The signed-in member's own place on one run's roster."""

    # The seat they hold, or None while they are waitlisted or when the
    # category has no roles to seat them in.
    role: str | None
    waitlisted: bool


def _viewer_signup(
    signups: list[EventSignup],
    viewer_discord_id: int | None,
) -> ViewerSignup | None:
    """The viewer's signup among ``signups``, or None when they have none."""
    if viewer_discord_id is None:
        return None
    for signup in signups:
        if signup.discord_user_id != viewer_discord_id:
            continue
        seat = None if signup.waitlisted else signup.assigned_role
        return ViewerSignup(
            role=None if seat is None else seat.value,
            waitlisted=signup.waitlisted,
        )
    return None


@dataclass(frozen=True, slots=True)
class CalendarEntry:
    event_id: int
    occurrence_id: int | None
    title: str
    category: str
    description: str
    requirements: str
    start_epoch: int
    duration_minutes: int
    leader_discord_id: int
    status: str
    projected: bool
    active_count: int
    waitlist_count: int
    # Empty for a role-less category, which the page shows as a headcount
    # alone.
    roles: tuple[RoleTally, ...]
    # None for an uncapped category (General): the page shows a plain headcount
    # rather than a "seated of total" ratio.
    capacity_total: int | None
    # The member asking, when they are on this run's roster. Always None for a
    # projection, which nobody can sign up for until it is posted.
    viewer_signup: ViewerSignup | None


def _projected_entry(event: Event, start_time: datetime) -> CalendarEntry:
    capacity = event.capacity
    return CalendarEntry(
        event_id=event.event_id,
        occurrence_id=None,
        title=event.title,
        category=event.category.value,
        description=event.description,
        requirements=event.requirements,
        start_epoch=int(start_time.timestamp()),
        duration_minutes=event.duration_minutes,
        leader_discord_id=event.leader_discord_id,
        status=PROJECTED_STATUS,
        projected=True,
        active_count=0,
        waitlist_count=0,
        roles=role_tallies(capacity, count_roster([])),
        capacity_total=capacity.total,
        viewer_signup=None,
    )


def calendar_entries(
    store: EventStore,
    timezone: ZoneInfo,
    range_start: datetime,
    range_end: datetime,
    now: datetime,
    *,
    viewer_discord_id: int | None = None,
) -> list[CalendarEntry]:
    """Every run between the range's edges, posted or projected.

    ``viewer_discord_id`` is the member the calendar is drawn for, whose own
    signups are marked on the runs they are on; None marks none.
    """
    entries: dict[tuple[int, int], CalendarEntry] = {}

    materialized = store.get_occurrences_between(range_start, range_end)
    signups_by_occurrence = store.get_signups_between(range_start, range_end)
    for event, occurrence in materialized:
        signups = signups_by_occurrence.get(occurrence.occurrence_id, [])
        counts = count_roster(signups)
        capacity = event.capacity
        # The stored status counts as well as the clock, the way the bot's own
        # "is this run finished" test does: an occurrence retires early when
        # its message turns out to be gone, and a run deriving its status from
        # the schedule alone would advertise itself as open until its original
        # end time - for good, once a deleted event's kept runs leave the
        # maintenance pass.
        status = (
            EventStatus.OVER
            if occurrence.status is EventStatus.OVER
            else occurrence_status(event, occurrence, signups, now)
        )
        key = (event.event_id, int(occurrence.start_time.timestamp()))
        entries[key] = CalendarEntry(
            event_id=event.event_id,
            occurrence_id=occurrence.occurrence_id,
            title=event.title,
            category=event.category.value,
            description=event.description,
            requirements=event.requirements,
            start_epoch=int(occurrence.start_time.timestamp()),
            duration_minutes=event.duration_minutes,
            leader_discord_id=event.leader_discord_id,
            status=status.value,
            projected=False,
            active_count=counts.active,
            waitlist_count=sum(
                1 for signup in signups if signup.waitlisted
            ),
            roles=role_tallies(capacity, counts),
            capacity_total=capacity.total,
            viewer_signup=_viewer_signup(signups, viewer_discord_id),
        )

    projected = 0
    latest_starts = store.get_latest_occurrence_starts()
    for event in store.get_active_events():
        if event.repeat_frequency is RepeatFrequency.NONE:
            continue
        # Project forward from the newest materialized occurrence so the
        # calendar matches exactly what the scheduler will create next.
        cursor = latest_starts.get(event.event_id)
        if cursor is None:
            LOGGER.debug(
                "Skipping projection for event without occurrences; "
                "event_id=%s",
                event.event_id,
            )
            continue
        emitted = 0
        for _ in range(PROJECTION_STEP_CAP):
            if emitted >= PROJECTION_ENTRY_CAP:
                LOGGER.warning(
                    "Stopping projection at entry cap; event_id=%s",
                    event.event_id,
                )
                break
            previous = cursor
            cursor = next_occurrence_start(
                event.repeat_frequency,
                event.repeat_days,
                cursor,
                timezone,
            )
            if cursor <= previous:
                LOGGER.warning(
                    "Stopping projection; recurrence did not advance; "
                    "event_id=%s",
                    event.event_id,
                )
                break
            if cursor >= range_end:
                break
            # Steps taken to reach a window far ahead of the anchor are not
            # projections; only emitted entries count against the entry cap.
            if cursor < range_start:
                continue
            key = (event.event_id, int(cursor.timestamp()))
            if key in entries:
                continue
            entries[key] = _projected_entry(event, cursor)
            emitted += 1
            projected += 1
        else:
            # The loop ran out of steps without ever reaching range_end, so the
            # window sits further from the anchor than the cap can walk. The
            # calendar silently shows no occurrence for a series that does
            # recur then, which is indistinguishable from an empty month unless
            # it is said out loud.
            LOGGER.warning(
                "Stopping projection at step cap; window unreachable from the "
                "anchor; event_id=%s",
                event.event_id,
            )

    results = sorted(entries.values(), key=lambda entry: entry.start_epoch)
    LOGGER.debug(
        "Computed calendar entries; materialized=%s projected=%s total=%s "
        "signed_up=%s",
        len(materialized),
        projected,
        len(results),
        sum(1 for entry in results if entry.viewer_signup is not None),
    )
    return results
