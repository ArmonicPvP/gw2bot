"""Cancelling an occurrence, pruning superseded ones, and seeding the next run."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import discord
from sqlalchemy.exc import SQLAlchemyError

from gw2bot.events.formatting import next_occurrence_start
from gw2bot.events.models import (
    Event,
    EventOccurrence,
    EventStatus,
    RepeatFrequency,
)
from gw2bot.events.posting.messages import (
    delete_event_posts,
    post_pending_occurrence,
)
from gw2bot.events.posting.roster import apply_auto_signups
from gw2bot.events.posting.state import leading_occurrence

if TYPE_CHECKING:
    from gw2bot.bot import Gw2Bot

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class OccurrenceCancellation:
    """What cancelling one occurrence of a repeating series left behind.

    ``successor`` is the occurrence the series continues with, and
    ``successor_posted`` says whether its public message is live. A successor
    that could not be posted is reported rather than raised: the cancellation
    itself is already committed by then, so the caller has to tell the
    commander what is (and is not) in the channel.
    """

    successor: EventOccurrence | None
    successor_posted: bool
    # Whether an unposted successor is queued for another attempt. False only
    # when the retry marker could not be written either, which leaves the
    # series with nothing in the channel and nothing coming to fix it.
    retry_pending: bool = True


async def cancel_occurrence(
    bot: Gw2Bot,
    event: Event,
    occurrence: EventOccurrence,
    now: datetime | None = None,
) -> OccurrenceCancellation:
    """Cancel one occurrence of a repeating series, keeping the series alive.

    The occurrence's roster, store rows and public post are removed, and the
    series carries on from the next one. Store failures propagate before
    anything is removed, so a cancellation that raises has changed nothing.
    """
    current_time = now if now is not None else datetime.now(UTC)
    # Seed the successor before the cancelled row goes: its start is computed
    # from the occurrence being cancelled, and has_later_occurrence can only
    # tell that the series still needs one while that row is there. It is a
    # no-op when a later occurrence already exists, which is the case when the
    # occurrence being cancelled is one the scheduler has already moved past.
    seeded = ensure_next_recurring_occurrence(
        bot, event, occurrence, current_time
    )
    # Claim the run that takes over before anything is destroyed. Everything
    # from the delete onwards is what would otherwise have posted it, and the
    # bot can stop there for any reason - a restart, a database error, a
    # Discord failure. The series would then have no posted occurrence and an
    # unclaimed pending one, which is exactly the state the maintenance pass
    # leaves alone, so it would quietly never come back. Posting clears the
    # claim again.
    successor = leading_occurrence(
        bot,
        event,
        current_time,
        excluding=occurrence.occurrence_id,
    )
    claimed = successor is not None and successor.message_id is None
    try:
        if claimed and successor is not None:
            # Deliberately strict, unlike the re-claim after a failed post:
            # nothing has been destroyed yet, and a claim that will not write
            # means the store is unhealthy right now. Going on to delete rows
            # against it risks the one state nothing recovers from - a series
            # with no posted run and an unclaimed pending one. Refusing costs
            # a retry.
            _claim_cancellation_successor(bot, successor)
        bot.event_store.delete_occurrence(occurrence.occurrence_id)
    except SQLAlchemyError:
        # The successor is committed in its own transaction, so it outlives a
        # claim or a delete that fails. Leaving it behind would let the next
        # maintenance pass post the following run while the one that failed to
        # cancel is still live, so take it back out before reporting the
        # failure. A successor that was already there keeps its row and gives
        # back the claim, which would otherwise invite that same premature
        # post.
        if seeded is not None:
            _discard_occurrence(
                bot,
                seeded,
                "the successor of a failed cancellation",
            )
        elif claimed and successor is not None:
            _release_cancellation_claim(bot, successor)
        raise
    await delete_event_posts(bot, event, [occurrence])
    try:
        successor = leading_occurrence(bot, event, current_time)
    except SQLAlchemyError as exc:
        # The cancellation is done - the row, its roster and its post are all
        # gone - so this read failing is not a cancellation failure. The
        # successor is claimed, so the maintenance pass will post it; report
        # that rather than a failure the commander would retry.
        LOGGER.error(
            "Could not read the series after cancelling; event_id=%s "
            "error_type=%s",
            event.event_id,
            type(exc).__name__,
        )
        return OccurrenceCancellation(
            successor=successor,
            successor_posted=False,
            retry_pending=claimed,
        )
    LOGGER.debug(
        "Cancelled event occurrence; event_id=%s occurrence_id=%s "
        "successor_id=%s",
        event.event_id,
        occurrence.occurrence_id,
        successor.occurrence_id if successor is not None else None,
    )
    if successor is None:
        return OccurrenceCancellation(successor=None, successor_posted=False)
    if successor.message_id is not None:
        return OccurrenceCancellation(
            successor=successor,
            successor_posted=True,
        )
    # The successor is posted here rather than left to the scheduler, which
    # only posts a pending occurrence for a series that already has a posted
    # one. Cancelling the only posted occurrence of a series leaves none, so
    # waiting for the scheduler would strand the series unposted forever.
    try:
        posted = await post_pending_occurrence(
            bot, event, successor, current_time
        )
    except ValueError:
        # The successor's row went away while its message was in flight,
        # because another cancellation or a delete reached it first.
        # post_occurrence has already removed the message it sent, and this
        # cancellation is itself done, so this is not a failure to retry: fall
        # through and report the series as it now stands.
        LOGGER.debug(
            "A cancelled occurrence's successor was removed mid-post; "
            "event_id=%s occurrence_id=%s",
            event.event_id,
            successor.occurrence_id,
        )
        posted = None
    except (discord.HTTPException, SQLAlchemyError, RuntimeError) as exc:
        LOGGER.error(
            "Could not post the successor of a cancelled occurrence; "
            "event_id=%s occurrence_id=%s error_type=%s",
            event.event_id,
            successor.occurrence_id,
            type(exc).__name__,
        )
        return OccurrenceCancellation(
            successor=successor,
            successor_posted=False,
            retry_pending=_mark_cancellation_successor_pending(
                bot,
                successor,
            ),
        )
    if posted is None:
        # Declining has two causes: a maintenance pass posted this occurrence
        # while the cancelled run's post was being cleared, or the row is gone
        # because another cancellation (or a delete) reached it first. Report
        # what the series actually has now rather than assuming the first -
        # announcing a run that is not there would send the commander off to
        # rebuild an event that is still going.
        current = leading_occurrence(bot, event, current_time)
        LOGGER.debug(
            "Cancelled occurrence's successor was settled elsewhere; "
            "event_id=%s successor_id=%s leading_id=%s",
            event.event_id,
            successor.occurrence_id,
            current.occurrence_id if current is not None else None,
        )
        return OccurrenceCancellation(
            successor=current,
            successor_posted=(
                current is not None and current.message_id is not None
            ),
        )
    return OccurrenceCancellation(successor=posted, successor_posted=True)


def _claim_cancellation_successor(
    bot: Gw2Bot,
    successor: EventOccurrence,
) -> None:
    # Raises rather than reporting: see the caller. Already-claimed rows need
    # no write, which is what makes this safe to call again later.
    if successor.needs_refresh:
        return
    bot.event_store.set_occurrence_needs_refresh(
        successor.occurrence_id,
        True,
    )
    LOGGER.debug(
        "Claimed a cancelled occurrence's successor for posting; "
        "occurrence_id=%s",
        successor.occurrence_id,
    )


def _release_cancellation_claim(
    bot: Gw2Bot,
    successor: EventOccurrence,
) -> None:
    # The cancellation did not happen after all, so the run it claimed is a
    # future one again and must not be posted ahead of the run that is still
    # live. Best effort: the failure that brought us here is already being
    # reported.
    try:
        bot.event_store.set_occurrence_needs_refresh(
            successor.occurrence_id,
            False,
        )
    except SQLAlchemyError as exc:
        LOGGER.error(
            "Could not release a cancellation claim; occurrence_id=%s "
            "error_type=%s",
            successor.occurrence_id,
            type(exc).__name__,
        )


def _discard_occurrence(
    bot: Gw2Bot,
    occurrence: EventOccurrence,
    reason: str,
) -> None:
    # Take back an occurrence row whose creation could not be completed. Best
    # effort: a failure here is logged rather than raised, because it is always
    # reported alongside the original failure that made the row unwanted.
    try:
        bot.event_store.delete_occurrence(occurrence.occurrence_id)
    except SQLAlchemyError as exc:
        LOGGER.error(
            "Could not discard %s; occurrence_id=%s error_type=%s",
            reason,
            occurrence.occurrence_id,
            type(exc).__name__,
        )
        return
    LOGGER.debug(
        "Discarded %s; occurrence_id=%s",
        reason,
        occurrence.occurrence_id,
    )


def _mark_cancellation_successor_pending(
    bot: Gw2Bot,
    successor: EventOccurrence,
) -> bool:
    # Claim the successor for the bot to post. The scheduler skips a pending
    # occurrence whose series has no posted one, because that normally means a
    # manual post is still in flight; the refresh flag is what tells it this
    # one is its own to send instead. A cancellation removes the series' last
    # post, so without the flag anything that stops this call short - a
    # Discord failure, a restart - would hide the series for good.
    #
    # Set once before the Discord work and again if that work fails, so the
    # second call is normally a no-op. Posting clears it.
    if successor.needs_refresh:
        return True
    try:
        bot.event_store.set_occurrence_needs_refresh(
            successor.occurrence_id,
            True,
        )
    except SQLAlchemyError as exc:
        # Without the flag nothing will post this run: the series has no posted
        # occurrence left, which is exactly what makes the scheduler leave a
        # pending one alone. Report it so the commander is told the series
        # needs a hand rather than promised a retry that is not coming.
        LOGGER.error(
            "Could not flag a cancelled occurrence's successor for posting; "
            "occurrence_id=%s error_type=%s",
            successor.occurrence_id,
            type(exc).__name__,
        )
        return False
    return True


async def prune_superseded_occurrences(bot: Gw2Bot, event: Event) -> int:
    # For a recurring event with delete_previous_on_repeat, remove the
    # occurrences the current post supersedes (their message, thread and store
    # rows) so the channel keeps only the current post. Only finished (OVER)
    # occurrences earlier than it qualify, so a live occurrence is never removed.
    # Message deletes are best-effort; the store rows are always removed so the
    # series does not accumulate history.
    #
    # The current post is derived here rather than passed in, because the two
    # conditions this waits on can land in either order: the next occurrence
    # being posted, and the previous one being persisted as OVER.
    # refresh_occurrence_message withholds the OVER commit until the message edit
    # and the thread rename have both succeeded, so a transient Discord failure
    # can leave the previous occurrence still non-OVER at the moment the next one
    # is posted. Deriving the state makes this idempotent, so whichever of the two
    # lands last can run the cleanup.
    if (
        event.repeat_frequency is RepeatFrequency.NONE
        or not event.delete_previous_on_repeat
    ):
        return 0
    occurrences = bot.event_store.get_event_occurrences(event.event_id)
    posted = [
        occurrence
        for occurrence in occurrences
        if occurrence.message_id is not None
    ]
    if not posted:
        return 0
    # Only a posted occurrence can supersede the previous one: removing the old
    # post before the next is live would leave the channel with no post at all.
    current = max(posted, key=lambda occurrence: occurrence.start_time)
    superseded = [
        occurrence
        for occurrence in occurrences
        if occurrence.occurrence_id != current.occurrence_id
        and occurrence.status is EventStatus.OVER
        and occurrence.start_time < current.start_time
    ]
    if not superseded:
        return 0
    await delete_event_posts(bot, event, superseded)
    deleted = 0
    for occurrence in superseded:
        try:
            bot.event_store.delete_occurrence(occurrence.occurrence_id)
            deleted += 1
        except SQLAlchemyError as exc:
            LOGGER.error(
                "Could not delete superseded occurrence row; "
                "occurrence_id=%s error_type=%s",
                occurrence.occurrence_id,
                type(exc).__name__,
            )
    LOGGER.debug(
        "Deleted superseded occurrences; event_id=%s count=%s "
        "current_occurrence_id=%s",
        event.event_id,
        deleted,
        current.occurrence_id,
    )
    return deleted


def ensure_next_recurring_occurrence(
    bot: Gw2Bot,
    event: Event,
    occurrence: EventOccurrence,
    now: datetime,
) -> EventOccurrence | None:
    """Seed the next occurrence of a recurring series when one is due.

    Returns the created occurrence, or None for a non-repeating event or when a
    later occurrence already exists (so callers never create duplicates).
    """
    if event.repeat_frequency is RepeatFrequency.NONE:
        return None
    if bot.event_store.has_later_occurrence(
        event.event_id,
        occurrence.start_time,
    ):
        return None
    return _create_next_occurrence(bot, event, occurrence, now)


def _create_next_occurrence(
    bot: Gw2Bot,
    event: Event,
    occurrence: EventOccurrence,
    now: datetime,
) -> EventOccurrence:
    next_start = next_occurrence_start(
        event.repeat_frequency,
        event.repeat_days,
        occurrence.start_time,
        bot.event_timezone,
    )
    # Catch up after downtime, but skip only occurrences that have fully
    # ended. If the bot was down when an occurrence's start passed yet it is
    # still in progress, keep it so it can post as ongoing (preserving its
    # auto-signups and public post) instead of jumping to the next one.
    duration = timedelta(minutes=event.duration_minutes)
    while next_start + duration <= now:
        next_start = next_occurrence_start(
            event.repeat_frequency,
            event.repeat_days,
            next_start,
            bot.event_timezone,
        )
    new_occurrence = bot.event_store.create_occurrence(
        event.event_id,
        next_start,
    )
    try:
        applied = apply_auto_signups(bot, event, new_occurrence)
    except Exception:
        # The row is committed but its roster is half-built. Leaving it would
        # publish a run carrying an arbitrary slice of the members who asked to
        # be signed up automatically - and a caller that never saw this
        # occurrence returned cannot take it back, so a cancellation whose
        # seeding failed here would report failure while the maintenance pass
        # went on to post the run anyway. Seeding lands whole or not at all.
        _discard_occurrence(
            bot,
            new_occurrence,
            "a successor whose auto-signups failed",
        )
        raise
    LOGGER.debug(
        "Created next recurring event occurrence; event_id=%s "
        "occurrence_id=%s auto_signups=%s",
        event.event_id,
        new_occurrence.occurrence_id,
        applied,
    )
    return new_occurrence
