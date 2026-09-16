"""What an occurrence is: where it was posted, and whether it is still to come.

Pure reads over an event and its occurrence, with no Discord call behind any
of them. This module is the leaf of the package.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from gw2bot.events.formatting import compute_status
from gw2bot.events.models import (
    Event,
    EventOccurrence,
    EventSignup,
    EventStatus,
    is_roster_full,
)

if TYPE_CHECKING:
    from gw2bot.bot import Gw2Bot


def occurrence_posted_in_thread(occurrence: EventOccurrence) -> bool:
    # True when the event was posted into a forum post rather than into a
    # channel: the post is then both the channel the message was sent to and the
    # thread members discuss it in, so the two stored ids are the same. An
    # occurrence posted to a text channel has its message in the channel and a
    # signup thread the bot opened under it, so they differ. Such a thread is the
    # bot's to rename and delete; a post it was merely posted into is not.
    # Rows written before either id was tracked read as a plain channel message.
    return (
        occurrence.thread_id is not None
        and occurrence.channel_id == occurrence.thread_id
    )


def occurrence_channel_id(event: Event, occurrence: EventOccurrence) -> int:
    # Where an occurrence's message actually lives. Discord addresses a message
    # by (channel, message), so editing or deleting one must target the channel
    # it was posted to. That is not necessarily event.channel_id: a channel edit
    # only re-posts the live occurrences, so finished ones (and any re-post that
    # failed) stay behind in the previous channel. Rows written before the
    # channel was tracked fall back to the event's channel, which is where they
    # were posted.
    return (
        occurrence.channel_id
        if occurrence.channel_id is not None
        else event.channel_id
    )


def occurrence_status(
    event: Event,
    occurrence: EventOccurrence,
    signups: list[EventSignup],
    now: datetime | None = None,
) -> EventStatus:
    current_time = now if now is not None else datetime.now(UTC)
    return compute_status(
        occurrence.start_time,
        event.duration_minutes,
        current_time,
        is_roster_full(event.capacity, signups),
    )


def leading_occurrence(
    bot: Gw2Bot,
    event: Event,
    now: datetime,
    *,
    excluding: int | None = None,
) -> EventOccurrence | None:
    """The run a series is on now: its earliest occurrence still to happen.

    Judged on the clock as well as on the stored status, the way `/event
    cancel` picks its target. The status alone lags: it only moves when a
    maintenance pass persists it, so a run that ended while its refresh was
    failing still reads as live, and taking one of those for the series'
    next run would report a date that has already passed.
    """
    duration = timedelta(minutes=event.duration_minutes)
    return next(
        (
            candidate
            for candidate in bot.event_store.get_event_occurrences(
                event.event_id
            )
            if candidate.occurrence_id != excluding
            and candidate.status is not EventStatus.OVER
            and candidate.start_time + duration > now
        ),
        None,
    )


def occurrence_finished(
    event: Event,
    occurrence: EventOccurrence,
    now: datetime | None = None,
) -> bool:
    """Whether an occurrence's roster is history, by the clock or the status.

    Both count, which is why this is not compute_status. The clock is the
    ordinary end; the stored status is how an occurrence retires early, when a
    message somebody deleted answers a refresh with NotFound and OVER is
    persisted (and the series' next run seeded) before this one's time is up.
    Deriving the status from the schedule alone reads such an occurrence as
    open, and a roster change would land on a run that has already been
    replaced.
    """
    if occurrence.status is EventStatus.OVER:
        return True
    current_time = now if now is not None else datetime.now(UTC)
    return current_time >= occurrence.start_time + timedelta(
        minutes=event.duration_minutes
    )
