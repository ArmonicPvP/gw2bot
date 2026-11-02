"""Showing a closed poll's results, whether an officer or the clock ended it.

`/poll end` and the closing pass below share these, so a poll that reaches its
end time looks and is announced exactly like one an officer ended.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import discord
from discord.utils import escape_markdown
from sqlalchemy.exc import SQLAlchemyError

from gw2bot.core.discord_utils import log_discord_failure
from gw2bot.polls.formatting import closed_poll_embed
from gw2bot.polls.models import PollResults
from gw2bot.polls.views import NO_MENTIONS, poll_message

if TYPE_CHECKING:
    from gw2bot.bot import Gw2Bot

LOGGER = logging.getLogger(__name__)

# How often polls are checked for a passed end time, which is how late after
# it one can close at most. Votes are refused from the end time itself.
POLL_CLOSING_INTERVAL_SECONDS = 30


def results_summary(results: PollResults) -> str:
    leaders = results.leaders
    options = results.poll.options
    if not leaders:
        return "No votes were cast."
    top = results.counts[leaders[0]]
    votes = f"{top} vote" + ("" if top == 1 else "s")
    if len(leaders) == 1:
        return f"🏆 Winner: **{escape_markdown(options[leaders[0]])}** with {votes}."
    tied = ", ".join(f"**{escape_markdown(options[position])}**" for position in leaders)
    return f"🤝 Tie between {tied} with {votes} each."


async def close_post(bot: Gw2Bot, results: PollResults) -> bool:
    """Swap the post's dropdown for the results; False if it could not be."""
    message = poll_message(bot, results.poll)
    if message is None:
        return False
    try:
        await message.edit(embed=closed_poll_embed(results), view=None)
    except discord.HTTPException as error:
        log_discord_failure(
            "Could not show a closed poll's results on its post; poll_id=%s",
            error,
            results.poll.poll_id,
        )
        return False
    LOGGER.debug("Closed poll post; poll_id=%s", results.poll.poll_id)
    return True


async def announce_results(bot: Gw2Bot, results: PollResults) -> bool:
    """Reply to the post, so the channel hears the poll ended."""
    message = poll_message(bot, results.poll)
    if message is None:
        return False
    content = (
        f"📊 **{escape_markdown(results.poll.name)}** has ended. "
        f"{results_summary(results)} The full results are on the poll."
    )
    try:
        await message.reply(content, allowed_mentions=NO_MENTIONS)
    except discord.HTTPException as error:
        log_discord_failure(
            "Could not announce a poll's results; poll_id=%s",
            error,
            results.poll.poll_id,
        )
        return False
    LOGGER.debug("Announced poll results; poll_id=%s", results.poll.poll_id)
    return True


async def close_due_polls(bot: Gw2Bot, now: datetime | None = None) -> int:
    """End every open poll whose end time has passed; returns how many closed.

    A post that cannot be updated is logged and left: the poll is closed in
    the store either way, and running `/poll end` on it puts the results back.
    """
    current_time = now if now is not None else datetime.now(UTC)
    due = bot.poll_store.due_polls(current_time)
    closed = 0
    for poll in due:
        try:
            results = bot.poll_store.close_poll(poll.poll_id, None, now=current_time)
        except SQLAlchemyError as error:
            LOGGER.error(
                "Could not close a poll at its end time; poll_id=%s "
                "error_type=%s",
                poll.poll_id,
                type(error).__name__,
            )
            continue
        if results is None:
            # An officer ended it between the read and the close.
            continue
        closed += 1
        if await close_post(bot, results):
            await announce_results(bot, results)
    LOGGER.debug(
        "Poll closing pass finished; due=%s closed=%s",
        len(due),
        closed,
    )
    return closed


async def poll_scheduled_closings(bot: Gw2Bot) -> None:
    await bot.wait_until_ready()
    LOGGER.debug("Poll closing poller started")
    while not bot.is_closed():
        try:
            await close_due_polls(bot)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOGGER.error(
                "Poll closing pass failed; error_type=%s",
                type(exc).__name__,
            )
        await asyncio.sleep(POLL_CLOSING_INTERVAL_SECONDS)
