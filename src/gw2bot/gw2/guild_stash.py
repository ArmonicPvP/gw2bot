from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

import aiohttp
from sqlalchemy.exc import SQLAlchemyError

if TYPE_CHECKING:
    from gw2bot.bot import Gw2Bot

LOGGER = logging.getLogger(__name__)


def stash_coin_balance(stash: list[dict[str, Any]]) -> int:
    """The guild bank's whole coin balance, in copper.

    ``/v2/guild/:id/stash`` answers with one entry per vault section, each
    carrying its own ``coins``. The guild log never says which section a
    deposit reached, and the gold page tracks the bank rather than any one
    tab, so the sections are summed into a single balance. A section that
    reports no coins, or reports something that is not a number, contributes
    nothing rather than failing the whole reading.
    """
    total = 0
    unreadable = 0
    for section in stash:
        try:
            total += int(section.get("coins", 0))
        except (TypeError, ValueError):
            unreadable += 1
    if unreadable:
        LOGGER.warning(
            "Ignored %s guild stash section(s) with an unreadable balance",
            unreadable,
        )
    return total


async def poll_guild_stash(bot: Gw2Bot) -> None:
    await bot.wait_until_ready()
    LOGGER.debug("Guild Stash poller started")
    if bot._session is None:
        raise RuntimeError("HTTP session was not initialized")
    if bot._api is None or bot._config.gw2_guild_id is None:
        raise RuntimeError("GW2 API client was not initialized")
    while not bot.is_closed():
        LOGGER.debug("Starting Guild Stash poll")
        try:
            await bot._record_guild_stash_balance()
        except (aiohttp.ClientError, asyncio.TimeoutError, SQLAlchemyError) as exc:
            bot._poll_status.record_error("Guild Stash", exc)
        else:
            bot._poll_status.record_success("Guild Stash")
            LOGGER.debug("Guild Stash poll completed successfully")

        await asyncio.sleep(bot._config.poll_interval_seconds)

async def record_guild_stash_balance(bot: Gw2Bot) -> int:
    """Read the guild bank's coin balance and log it, returning what it is.

    The reading is the anchor the /gold page measures every derived balance
    from, so it is taken on its own schedule rather than inferred from the
    movements the guild log reports - which is exactly what makes a movement
    the log dropped shift the older history instead of the present.
    """
    guild_id = bot._config.gw2_guild_id
    if bot._api is None or guild_id is None:
        raise RuntimeError("GW2 API client was not initialized")
    stash = await bot._api.get_guild_stash(guild_id)
    coins = stash_coin_balance(stash)
    recorded = bot._raffle_store.record_stash_balance(coins, time.time())
    LOGGER.debug(
        "Read the guild stash balance; sections=%s recorded=%s",
        len(stash),
        recorded,
    )
    return coins
