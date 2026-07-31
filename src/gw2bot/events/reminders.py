from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import discord

from gw2bot.events.formatting import reminder_messages
from gw2bot.events.models import (
    REMINDER_GRACE,
    REMINDER_OFFSETS,
    Event,
    EventOccurrence,
    EventSignup,
    EventStatus,
    reminder_offset_minutes,
)
from gw2bot.events.posting import (
    occurrence_status,
    reopen_occurrence_thread,
    resolve_channel,
)

if TYPE_CHECKING:
    from gw2bot.bot import Gw2Bot

LOGGER = logging.getLogger(__name__)


def reminder_participants(signups: Sequence[EventSignup]) -> list[int]:
    # Only seated members are participating: a waitlisted member has no place in
    # the squad yet, so telling them the event is about to start would be an
    # invitation to show up for a seat they do not have. Store order is sign-up
    # order, which keeps the ping list stable between reminders.
    return [
        signup.discord_user_id
        for signup in signups
        if not signup.waitlisted
    ]


def can_remind(
    occurrence: EventOccurrence,
    signups: Sequence[EventSignup],
) -> bool:
    # Whether a reminder has both an audience and somewhere to reach it. Neither
    # is a transient condition a retry would fix within a reminder's window, so
    # an automatic reminder settles rather than retries when this is False.
    return (
        bool(reminder_participants(signups))
        and occurrence.thread_id is not None
    )


def due_reminder_offsets(
    occurrence: EventOccurrence,
    handled: set[int],
    now: datetime,
) -> tuple[list[int], int | None]:
    """Split the outstanding reminders into (all due, the one to deliver).

    A reminder comes due at ``start - offset``. Everything due and unrecorded is
    returned as handled by this pass, but only the most imminent one still
    inside :data:`REMINDER_GRACE` is delivered - the earlier ones it overtook
    would each add a ping that the delivered one already makes, and one that
    lapsed outside the grace window (downtime across its moment) is stale rather
    than useful. The deliverable offset is None when every due reminder lapsed.
    """
    due: list[int] = []
    deliverable: list[int] = []
    for offset in REMINDER_OFFSETS:
        minutes = reminder_offset_minutes(offset)
        if minutes in handled:
            continue
        moment = occurrence.start_time - offset
        if now < moment:
            continue
        due.append(minutes)
        if now <= moment + REMINDER_GRACE:
            deliverable.append(minutes)
    # REMINDER_OFFSETS runs from the earliest reminder to the latest, so the
    # last deliverable entry is the one closest to the start.
    return due, deliverable[-1] if deliverable else None


async def send_reminder(
    bot: Gw2Bot,
    event: Event,
    occurrence: EventOccurrence,
    signups: Sequence[EventSignup],
) -> bool:
    """Ping an occurrence's seated members where the event was posted.

    Returns whether the ping was delivered. False means it was not: there was
    nobody to ping, nowhere to ping them, or Discord refused the message. Every
    case is logged here, and the caller decides what it means for it.
    """
    participants = reminder_participants(signups)
    if not participants or occurrence.thread_id is None:
        # Nothing to do rather than a failure: the roster can be empty, and an
        # occurrence has no thread when its creation failed after the event was
        # posted to a channel, or when it was never posted at all - either way
        # there is nowhere the roster is gathered to be reminded in.
        LOGGER.debug(
            "Skipped event reminder with nobody or nowhere to ping; "
            "occurrence_id=%s participants=%s has_thread=%s",
            occurrence.occurrence_id,
            len(participants),
            occurrence.thread_id is not None,
        )
        return False
    messages = reminder_messages(
        event.title,
        occurrence.start_time,
        participants,
    )
    # A dormant forum post refuses messages until it is reopened, and a reminder
    # can easily be the first thing written in one for weeks.
    await reopen_occurrence_thread(bot, occurrence)
    try:
        thread = await resolve_channel(bot, occurrence.thread_id)
        for message in messages:
            await thread.send(message)
    except discord.HTTPException as exc:
        LOGGER.error(
            "Could not send event reminder; occurrence_id=%s participants=%s "
            "messages=%s error_type=%s",
            occurrence.occurrence_id,
            len(participants),
            len(messages),
            type(exc).__name__,
        )
        return False
    LOGGER.debug(
        "Sent event reminder; event_id=%s occurrence_id=%s participants=%s "
        "messages=%s characters=%s",
        event.event_id,
        occurrence.occurrence_id,
        len(participants),
        len(messages),
        sum(len(message) for message in messages),
    )
    return True


async def deliver_due_reminders(
    bot: Gw2Bot,
    event: Event,
    occurrence: EventOccurrence,
    now: datetime | None = None,
) -> int | None:
    """Send an occurrence's outstanding automatic reminder, if one is due.

    Returns the offset that was delivered, or None when nothing was sent. Every
    reminder this pass resolves is recorded, so it is never repeated; a delivery
    that fails is deliberately left unrecorded so the next maintenance pass
    retries it while it is still inside the grace window.
    """
    current_time = now if now is not None else datetime.now(UTC)
    handled = bot.event_store.get_handled_reminder_offsets(
        occurrence.occurrence_id
    )
    due, deliverable = due_reminder_offsets(occurrence, handled, current_time)
    if not due:
        return None
    signups = bot.event_store.get_signups(occurrence.occurrence_id)
    # A finished occurrence is about to leave maintenance for good, so anything
    # still outstanding on it is settled here rather than left as a reminder
    # that can never resolve. Reminding a roster about an event that is already
    # over would be wrong in any case.
    finished = (
        occurrence_status(event, occurrence, signups, current_time)
        is EventStatus.OVER
    )
    LOGGER.debug(
        "Resolving due event reminders; event_id=%s occurrence_id=%s due=%s "
        "deliverable_offset_minutes=%s finished=%s",
        event.event_id,
        occurrence.occurrence_id,
        len(due),
        deliverable,
        finished,
    )
    if finished or deliverable is None or not can_remind(occurrence, signups):
        # The occurrence is over, every due reminder lapsed outside the grace
        # window, or there is nobody to ping and nowhere to ping them. None of
        # those improves by waiting, so the reminders are settled rather than
        # retried.
        LOGGER.debug(
            "Settling due event reminders without a ping; occurrence_id=%s "
            "settled=%s finished=%s lapsed=%s",
            occurrence.occurrence_id,
            len(due),
            finished,
            deliverable is None,
        )
        bot.event_store.mark_reminders_handled(
            occurrence.occurrence_id,
            due,
            current_time,
        )
        return None
    sent = await send_reminder(bot, event, occurrence, signups)
    # A failed delivery keeps its own offset outstanding so the next pass can
    # retry it, while the reminders it superseded are settled either way.
    resolved = (
        due if sent else [minutes for minutes in due if minutes != deliverable]
    )
    bot.event_store.mark_reminders_handled(
        occurrence.occurrence_id,
        resolved,
        current_time,
    )
    LOGGER.debug(
        "Resolved due event reminders; occurrence_id=%s delivered=%s "
        "recorded=%s outstanding=%s",
        occurrence.occurrence_id,
        sent,
        len(resolved),
        len(due) - len(resolved),
    )
    return deliverable if sent else None
