from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from gw2bot.polls.reactions import finalize_poll, reconcile_poll

if TYPE_CHECKING:
    from gw2bot.bot import Gw2Bot

LOGGER = logging.getLogger(__name__)

POLL_SCHEDULER_INTERVAL_SECONDS = 60


async def run_poll_scheduler(bot: Gw2Bot) -> None:
    await bot.wait_until_ready()
    LOGGER.debug("Poll scheduler started")
    while not bot.is_closed():
        try:
            await run_poll_maintenance(bot)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOGGER.error(
                "Poll maintenance pass failed; error_type=%s",
                type(exc).__name__,
            )
        await asyncio.sleep(POLL_SCHEDULER_INTERVAL_SECONDS)


async def run_poll_maintenance(
    bot: Gw2Bot,
    now: datetime | None = None,
) -> None:
    current_time = now if now is not None else datetime.now(UTC)
    polls = bot.poll_store.get_active_polls()
    expired_ids = {
        poll.poll_id for poll in bot.poll_store.get_expired_polls(current_time)
    }
    LOGGER.debug(
        "Starting poll maintenance pass; active_polls=%s expired=%s",
        len(polls),
        len(expired_ids),
    )
    for poll in polls:
        # An unposted poll is the creation flow's responsibility, mirroring how
        # the event scheduler leaves manual posts alone.
        if poll.message_id is None:
            continue
        try:
            if poll.poll_id in expired_ids:
                # A poll whose timer has elapsed is finalized: reconcile against
                # the live reactions, lock the message, clear reactions, and
                # stop tracking it.
                await finalize_poll(bot, poll, reason="expired")
            else:
                # Reconcile still-open polls so votes that changed while the bot
                # was offline are caught up and the message re-rendered.
                await reconcile_poll(bot, poll)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # One poll failing must not block maintenance of the others.
            LOGGER.error(
                "Could not maintain poll; poll_id=%s error_type=%s",
                poll.poll_id,
                type(exc).__name__,
            )
