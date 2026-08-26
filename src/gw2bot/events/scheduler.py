from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from gw2bot.events.models import EventStatus
from gw2bot.events.posting import (
    ensure_next_recurring_occurrence,
    occurrence_status,
    post_pending_occurrence,
    prune_superseded_occurrences,
    refresh_occurrence_message,
    sweep_stale_announcement,
)
from gw2bot.events.reminders import deliver_due_reminders

if TYPE_CHECKING:
    from gw2bot.bot import Gw2Bot

LOGGER = logging.getLogger(__name__)

EVENT_SCHEDULER_INTERVAL_SECONDS = 60


async def poll_event_updates(bot: Gw2Bot) -> None:
    await bot.wait_until_ready()
    LOGGER.debug("Event scheduler poller started")
    while not bot.is_closed():
        try:
            await run_event_maintenance(bot)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOGGER.error(
                "Event maintenance pass failed; error_type=%s",
                type(exc).__name__,
            )
        await asyncio.sleep(EVENT_SCHEDULER_INTERVAL_SECONDS)


async def run_event_maintenance(
    bot: Gw2Bot,
    now: datetime | None = None,
) -> None:
    current_time = now if now is not None else datetime.now(UTC)
    swept: set[int] = set()
    occurrences = bot.event_store.get_posted_unfinished_occurrences()
    LOGGER.debug(
        "Starting event maintenance pass; live_occurrences=%s",
        len(occurrences),
    )
    for occurrence in occurrences:
        event = bot.event_store.get_event(occurrence.event_id)
        if event is None or event.cancelled:
            LOGGER.debug(
                "Skipping occurrence without an active event; "
                "occurrence_id=%s",
                occurrence.occurrence_id,
            )
            continue
        # Reminders are resolved before the status work below, which skips an
        # occurrence whose status has not moved: a reminder comes due on the
        # clock rather than on a roster or status change, so it must not be
        # gated behind one. A failure here is contained so one occurrence's
        # reminder cannot stop the rest of the pass.
        try:
            await deliver_due_reminders(bot, event, occurrence, current_time)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOGGER.error(
                "Could not resolve event reminders; occurrence_id=%s "
                "error_type=%s",
                occurrence.occurrence_id,
                type(exc).__name__,
            )
        signups = bot.event_store.get_signups(occurrence.occurrence_id)
        status = occurrence_status(event, occurrence, signups, current_time)
        # A dirty occurrence still needs its message re-rendered even when the
        # status is unchanged, because an earlier roster-change refresh failed.
        if status == occurrence.status and not occurrence.needs_refresh:
            continue
        # The next occurrence row is secured before the OVER status is
        # persisted, so a failure here leaves the transition unfinished
        # and it is retried on the next maintenance pass.
        if status is EventStatus.OVER:
            ensure_next_recurring_occurrence(
                bot, event, occurrence, current_time
            )
        refreshed = await refresh_occurrence_message(
            bot, event, occurrence, current_time
        )
        # refresh_occurrence_message only commits OVER once the message edit and
        # the thread rename have both landed, so a transient Discord failure can
        # push the OVER transition to a later pass - by which time the next
        # occurrence has already been posted and its own cleanup has run and
        # found nothing to remove. Prune on this edge too, otherwise the retry
        # has no trigger and the superseded post and row survive forever despite
        # the opt-in. The prune is idempotent, so the common case is a no-op.
        if refreshed is EventStatus.OVER:
            await prune_superseded_occurrences(bot, event)
        # Refreshing an occurrence sweeps what it owes, so this pass has
        # already tried these and the sweep below must not try them twice.
        swept.add(occurrence.occurrence_id)
    await _sweep_outstanding_announcements(bot, swept)
    await _post_pending_occurrences(bot, current_time)


async def _sweep_outstanding_announcements(
    bot: Gw2Bot,
    already_swept: set[int],
) -> None:
    """Retry every announcement removal still owed, whatever owes it.

    The removals outlive the occurrences that owe them, so the loop above
    cannot be what drives them. It reaches an occurrence only while the
    occurrence is live and something about it has moved: one that is clean and
    unchanged is skipped, one that reached OVER has left the unfinished set
    entirely, and one the loop passes over for want of an active event never
    reaches a sweep either. A removal Discord was refusing on the pass that
    ended the event would never be tried again, and for a one-off event the
    dead link would stand in the ping channel until somebody deleted the
    whole event.

    Driven off the outstanding list instead, which is exactly the set of
    removals still owed and says nothing about status. One occurrence's
    failure is contained so it cannot stop the rest.
    """
    outstanding = [
        occurrence
        for occurrence in (
            bot.event_store.get_occurrences_with_stale_announcements()
        )
        if occurrence.occurrence_id not in already_swept
    ]
    if not outstanding:
        return
    LOGGER.debug(
        "Retrying announcement removals outside the live set; occurrences=%s",
        len(outstanding),
    )
    for occurrence in outstanding:
        try:
            await sweep_stale_announcement(bot, occurrence)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOGGER.error(
                "Could not retry an announcement removal; occurrence_id=%s "
                "error_type=%s",
                occurrence.occurrence_id,
                type(exc).__name__,
            )


async def _post_pending_occurrences(bot: Gw2Bot, now: datetime) -> None:
    pending = bot.event_store.get_unposted_occurrences()
    if not pending:
        return
    LOGGER.debug(
        "Posting pending event occurrences; pending=%s",
        len(pending),
    )
    for occurrence in pending:
        event = bot.event_store.get_event(occurrence.event_id)
        if event is None or event.cancelled:
            LOGGER.debug(
                "Skipping pending occurrence without an active event; "
                "occurrence_id=%s",
                occurrence.occurrence_id,
            )
            continue
        # A series without any posted occurrence is a manual post still in
        # flight (or abandoned); posting it here would race the creator's
        # own posting flow and duplicate the message. An occurrence flagged
        # for refresh is the exception: a cancellation already removed the
        # series' last post and only failed to send this one, so nobody is
        # coming to post it by hand and it is the bot's to retry.
        if not occurrence.needs_refresh and not (
            bot.event_store.has_posted_occurrence(event.event_id)
        ):
            LOGGER.debug(
                "Skipping pending occurrence awaiting manual posting; "
                "occurrence_id=%s",
                occurrence.occurrence_id,
            )
            continue
        # One failed posting must not block the remaining pending
        # occurrences; failures are retried on the next maintenance pass.
        try:
            posted = await post_pending_occurrence(bot, event, occurrence, now)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOGGER.error(
                "Could not post pending event occurrence; occurrence_id=%s "
                "error_type=%s",
                occurrence.occurrence_id,
                type(exc).__name__,
            )
            continue
        if posted is None:
            # A cancellation posted it between this pass reading the pending
            # set and reaching it.
            continue
        LOGGER.debug(
            "Posted pending event occurrence; event_id=%s occurrence_id=%s",
            event.event_id,
            posted.occurrence_id,
        )
        # Once the new occurrence is posted, a recurring event that opted in
        # removes the occurrence(s) it supersedes so the channel keeps only the
        # current post. This is best-effort and never raises, and it no-ops for
        # an event that did not opt in.
        await prune_superseded_occurrences(bot, event)
        # A pending occurrence can already be over by the time it is posted
        # (for example, posting was blocked past its end). Posting it
        # persists OVER, so it never enters the unfinished set that drives
        # the OVER transition; seed the next occurrence here so a recurring
        # series catches up instead of stopping.
        if posted.status is EventStatus.OVER:
            ensure_next_recurring_occurrence(bot, event, posted, now)
