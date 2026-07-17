from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

import aiohttp
import discord
from sqlalchemy.exc import SQLAlchemyError

from gw2bot.feast_stock import (
    changed_feast_counts,
    get_due_low_stock_alerts,
    tracked_feast_counts,
)

if TYPE_CHECKING:
    from gw2bot.bot import Gw2Bot

LOGGER = logging.getLogger(__name__)


async def poll_guild_storage(bot: Gw2Bot) -> None:
    await bot.wait_until_ready()
    LOGGER.debug("Guild Storage poller started")
    if bot._session is None:
        raise RuntimeError("HTTP session was not initialized")

    if bot._api is None:
        raise RuntimeError("GW2 API client was not initialized")
    while not bot.is_closed():
        LOGGER.debug("Starting Guild Storage poll")
        try:
            storage = await bot._api.get_guild_storage(bot._config.gw2_guild_id)
            await bot._handle_storage(storage)
        except (aiohttp.ClientError, asyncio.TimeoutError, SQLAlchemyError) as exc:
            bot._poll_status.record_error("Guild Storage", exc)
        else:
            bot._poll_status.record_success("Guild Storage")
            LOGGER.debug("Guild Storage poll completed successfully")

        await asyncio.sleep(bot._config.poll_interval_seconds)

async def handle_storage(bot: Gw2Bot, storage: list[dict[str, Any]]) -> None:
    now = time.time()
    counts = tracked_feast_counts(storage)
    last_alerted_at = bot._raffle_store.get_feast_alert_times()
    alerts, currently_low = get_due_low_stock_alerts(
        counts,
        last_alerted_at,
        now,
    )
    LOGGER.debug(
        "Evaluated %s storage entries; tracked_present=%s low=%s due_alerts=%s",
        len(storage),
        len(counts),
        len(currently_low),
        len(alerts),
    )
    # Only clear a feast's alert once we actually see it restocked above the
    # threshold. Feasts missing from this poll are left untouched rather than
    # being treated as restocked (or empty).
    for feast_id in (last_alerted_at.keys() - currently_low) & counts.keys():
        bot._raffle_store.clear_feast_alert(feast_id)
    for alert in alerts:
        if await bot._try_send_feast_notification(alert.message):
            bot._raffle_store.mark_feast_alert_sent(
                alert.guild_storage_id,
                now,
            )
    _record_feast_counts(bot, counts, now)


def _record_feast_counts(
    bot: Gw2Bot,
    counts: dict[int, int],
    now: float,
) -> None:
    # Seed the last-known counts from the database once; afterwards every poll
    # compares against the in-memory cache so no read is needed.
    if bot._feast_counts is None:
        bot._feast_counts = bot._raffle_store.get_last_feast_counts()
    changes = changed_feast_counts(counts, bot._feast_counts)
    if changes:
        bot._raffle_store.record_feast_counts(changes, now)
        # Only update the cache after a successful write so a failed write is
        # retried on the next poll instead of being silently dropped.
        bot._feast_counts.update(changes)
    LOGGER.debug("Recorded %s changed feast counts", len(changes))

async def try_send_feast_notification(bot: Gw2Bot, message: str) -> bool:
    LOGGER.debug("Sending feast alert to notification channel")
    if not await bot._try_send_notification(message):
        return False
    if bot._config.discord_feast_notification_user_id is None:
        return True
    try:
        await bot._send_feast_private_message(message)
    except discord.DiscordException:
        LOGGER.exception("Could not send private feast notification")
    return True

async def send_feast_private_message(bot: Gw2Bot, message: str) -> None:
    user_id = bot._config.discord_feast_notification_user_id
    if user_id is None:
        return
    if bot._feast_notification_user is None:
        LOGGER.debug("Fetching feast notification user %s", user_id)
        bot._feast_notification_user = await bot.fetch_user(user_id)
    await bot._feast_notification_user.send(message)
    LOGGER.debug("Sent feast private notification to user %s", user_id)
