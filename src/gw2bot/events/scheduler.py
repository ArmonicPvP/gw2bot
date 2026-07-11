from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from gw2bot.events.models import EventStatus
from gw2bot.events.posting import (
    ensure_next_recurring_occurrence,
    occurrence_status,
    post_occurrence,
    refresh_occurrence_message,
    update_thread_membership,
)

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
        await refresh_occurrence_message(bot, event, occurrence, current_time)
    await _post_pending_occurrences(bot, current_time)


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
        # own posting flow and duplicate the message.
        if not bot.event_store.has_posted_occurrence(event.event_id):
            LOGGER.debug(
                "Skipping pending occurrence awaiting manual posting; "
                "occurrence_id=%s",
                occurrence.occurrence_id,
            )
            continue
        # One failed posting must not block the remaining pending
        # occurrences; failures are retried on the next maintenance pass.
        try:
            posted = await post_occurrence(bot, event, occurrence, now)
            for signup in bot.event_store.get_signups(
                posted.occurrence_id
            ):
                await update_thread_membership(
                    bot,
                    posted,
                    signup.discord_user_id,
                    add=True,
                )
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
        LOGGER.debug(
            "Posted pending event occurrence; event_id=%s occurrence_id=%s",
            event.event_id,
            posted.occurrence_id,
        )
        # A pending occurrence can already be over by the time it is posted
        # (for example, posting was blocked past its end). Posting it
        # persists OVER, so it never enters the unfinished set that drives
        # the OVER transition; seed the next occurrence here so a recurring
        # series catches up instead of stopping.
        if posted.status is EventStatus.OVER:
            ensure_next_recurring_occurrence(bot, event, posted, now)
