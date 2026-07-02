from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import aiohttp
import discord
from sqlalchemy.exc import SQLAlchemyError

from gw2bot.raffle.formatting import (
    RAFFLE_TICKETS_PAGE_SIZE,
    raffle_contribution_report_embed,
)
from gw2bot.raffle.views import RaffleContributionReportView

if TYPE_CHECKING:
    from gw2bot.bot import Gw2Bot

LOGGER = logging.getLogger(__name__)

RAFFLE_CONTRIBUTION_CHANNEL_ID = 856343628984746014
RAFFLE_CONTRIBUTION_REPORT_HOURS = 6


def raffle_contribution_report_end(now: datetime) -> datetime:
    now_utc = now.astimezone(UTC)
    return now_utc.replace(
        hour=(
            now_utc.hour // RAFFLE_CONTRIBUTION_REPORT_HOURS
        ) * RAFFLE_CONTRIBUTION_REPORT_HOURS,
        minute=0,
        second=0,
        microsecond=0,
    )


def seconds_until_raffle_contribution_report(now: datetime) -> float:
    report_end = raffle_contribution_report_end(now)
    next_report = report_end + timedelta(hours=RAFFLE_CONTRIBUTION_REPORT_HOURS)
    return (next_report - now.astimezone(UTC)).total_seconds()


async def poll_raffle_contributions(bot: Gw2Bot) -> None:
    await bot.wait_until_ready()
    LOGGER.debug("Raffle Contributions poller started")
    while not bot.is_closed():
        delay = seconds_until_raffle_contribution_report(datetime.now(UTC))
        LOGGER.debug("Raffle Contributions poll scheduled in %s seconds", delay)
        await asyncio.sleep(delay)
        if bot.is_closed():
            return

        report_end = raffle_contribution_report_end(datetime.now(UTC))
        refreshed = True
        try:
            await bot.refresh_guild_log()
        except (aiohttp.ClientError, asyncio.TimeoutError, SQLAlchemyError) as exc:
            refreshed = False
            LOGGER.warning(
                "Raffle Contributions guild-log refresh failed; posting "
                "persisted report; error_type=%s",
                type(exc).__name__,
            )

        try:
            await bot._send_raffle_contribution_report(report_end)
        except (
            asyncio.TimeoutError,
            discord.DiscordException,
            SQLAlchemyError,
        ) as exc:
            bot._poll_status.record_error("Raffle Contributions", exc)
        else:
            bot._poll_status.record_success("Raffle Contributions")
            LOGGER.debug(
                "Raffle Contributions poll completed successfully; "
                "guild_log_refreshed=%s",
                refreshed,
            )

async def send_raffle_contribution_report(bot: Gw2Bot, report_end: datetime) -> None:
    report_start = report_end - timedelta(
        hours=RAFFLE_CONTRIBUTION_REPORT_HOURS
    )
    contributions = bot.get_raffle_contributions(report_start, report_end)
    LOGGER.debug(
        "Formatted raffle contribution report; contributors=%s",
        len(contributions),
    )
    if not contributions:
        return
    view = (
        RaffleContributionReportView(contributions)
        if len(contributions) > RAFFLE_TICKETS_PAGE_SIZE
        else None
    )
    await bot._send_raffle_contribution_embed(
        raffle_contribution_report_embed(contributions, 0),
        view,
    )

async def send_raffle_contribution_message(bot: Gw2Bot, message: str) -> None:
    LOGGER.debug(
        "Sending raffle contribution text message; characters=%s",
        len(message),
    )
    channel = await bot._get_raffle_contribution_channel()
    await channel.send(message)
    LOGGER.debug("Raffle contribution text message sent")

async def send_raffle_contribution_embed(
    bot: Gw2Bot,
    embed: discord.Embed,
    view: discord.ui.View | None,
) -> None:
    LOGGER.debug(
        "Sending raffle contribution embed; characters=%s view=%s",
        len(embed.description or ""),
        view is not None,
    )
    channel = await bot._get_raffle_contribution_channel()
    if view is None:
        await channel.send(embed=embed)
    else:
        await channel.send(embed=embed, view=view)
    LOGGER.debug("Raffle contribution embed sent")

async def get_raffle_contribution_channel(bot: Gw2Bot) -> Any:
    if bot._raffle_contribution_channel is None:
        LOGGER.debug(
            "Fetching raffle contribution channel %s",
            RAFFLE_CONTRIBUTION_CHANNEL_ID,
        )
        channel = await bot.fetch_channel(RAFFLE_CONTRIBUTION_CHANNEL_ID)
        if (
            getattr(getattr(channel, "guild", None), "id", None)
            != bot._config.discord_command_guild_id
        ):
            raise discord.ClientException(
                "Raffle contribution channel must belong to "
                "DISCORD_COMMAND_GUILD_ID"
            )
        bot._raffle_contribution_channel = channel
    return bot._raffle_contribution_channel


async def try_send_raffle_contribution_message(bot: Gw2Bot, message: str) -> bool:
    LOGGER.debug(
        "Attempting raffle contribution message delivery; characters=%s",
        len(message),
    )
    try:
        await bot._send_raffle_contribution_message(message)
    except discord.DiscordException as exc:
        LOGGER.error(
            "Could not send raffle contribution message; error_type=%s",
            type(exc).__name__,
        )
        return False
    LOGGER.debug("Raffle contribution message delivery succeeded")
    return True
