from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from gw2bot.events.formatting import next_occurrence_start
from gw2bot.events.models import (
    Event,
    EventOccurrence,
    EventStatus,
    RepeatFrequency,
)
from gw2bot.events.posting import (
    apply_auto_signups,
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
        if status == occurrence.status:
            continue
        await refresh_occurrence_message(bot, event, occurrence, current_time)
        if (
            status is EventStatus.OVER
            and event.repeat_frequency is not RepeatFrequency.NONE
            and not bot.event_store.has_later_occurrence(
                event.event_id,
                occurrence.start_time,
            )
        ):
            await _post_next_occurrence(bot, event, occurrence, current_time)


async def _post_next_occurrence(
    bot: Gw2Bot,
    event: Event,
    occurrence: EventOccurrence,
    now: datetime,
) -> None:
    next_start = next_occurrence_start(
        event.repeat_frequency,
        event.repeat_days,
        occurrence.start_time,
        bot.event_timezone,
    )
    # Catch up after downtime so the next posted occurrence is in the future.
    while next_start <= now:
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
    applied = apply_auto_signups(bot, event, new_occurrence)
    posted = await post_occurrence(bot, event, new_occurrence, now)
    if applied:
        for signup in bot.event_store.get_signups(posted.occurrence_id):
            await update_thread_membership(
                bot,
                posted,
                signup.discord_user_id,
                add=True,
            )
    LOGGER.debug(
        "Posted recurring event occurrence; event_id=%s occurrence_id=%s "
        "auto_signups=%s",
        event.event_id,
        posted.occurrence_id,
        applied,
    )
