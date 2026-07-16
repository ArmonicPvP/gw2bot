from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import aiohttp
from sqlalchemy.exc import SQLAlchemyError

from gw2bot.raffle import OFFICER_RANK, parse_gold_deposit
from gw2bot.raffle.formatting import raffle_deposit_embed

if TYPE_CHECKING:
    from gw2bot.bot import Gw2Bot

LOGGER = logging.getLogger(__name__)


async def refresh_guild_log(bot: Gw2Bot) -> None:
    if bot._api is None:
        raise RuntimeError("GW2 API client was not initialized")
    cursor = bot._raffle_store.get_cursor()
    events = await bot._api.get_guild_log(
        bot._config.gw2_guild_id,
        cursor,
    )
    LOGGER.debug(
        "Fetched %s guild log events after cursor %s",
        len(events),
        cursor,
    )
    if cursor is None:
        latest_event_id = max(
            (int(event["id"]) for event in events),
            default=0,
        )
        bot._raffle_store.initialize_cursor(latest_event_id)
        LOGGER.info(
            "Initialized guild log cursor at event %s",
            latest_event_id,
        )
        return
    officer_usernames: set[str] = set()
    if any(
        int(event["id"]) > cursor and parse_gold_deposit(event) is not None
        for event in events
    ):
        if bot._guild_members is None:
            raise RuntimeError("Guild member cache was not initialized")
        officer_usernames = await bot._guild_members.usernames_with_rank(
            OFFICER_RANK,
            force_refresh=True,
        )
    bot._raffle_store.process_events(events, officer_usernames)
    LOGGER.debug("Processed %s fetched guild log events", len(events))


async def poll_guild_log(bot: Gw2Bot) -> None:
    await bot.wait_until_ready()
    LOGGER.debug("Guild Log poller started")
    if bot._session is None:
        raise RuntimeError("HTTP session was not initialized")

    if bot._api is None:
        raise RuntimeError("GW2 API client was not initialized")
    while not bot.is_closed():
        LOGGER.debug("Starting Guild Log poll")
        try:
            await bot.refresh_guild_log()
            await bot._send_pending_raffle_notifications()
            await bot._send_pending_deposit_audit_notifications()
            await bot._send_pending_raffle_milestones()
            await bot._send_pending_join_notifications()
            await bot._send_pending_leave_notifications()
            await bot._send_pending_invite_notifications()
            await bot._send_pending_rank_change_notifications()
        except (aiohttp.ClientError, asyncio.TimeoutError, SQLAlchemyError) as exc:
            bot._poll_status.record_error("Guild Log", exc)
        else:
            bot._poll_status.record_success("Guild Log")
            LOGGER.debug("Guild Log poll completed successfully")

        await asyncio.sleep(bot._config.guild_log_poll_interval_seconds)

async def send_pending_raffle_notifications(bot: Gw2Bot) -> None:
    pending = bot._raffle_store.get_pending_notifications()
    LOGGER.debug("Found %s pending raffle notifications", len(pending))
    for deposit in pending:
        # A deposit below one gold buys no tickets; suppress the public embed
        # while still marking it sent so it does not stay pending forever.
        if deposit.raffle_tickets <= 0:
            LOGGER.debug(
                "Skipping sub-gold raffle deposit embed for event %s; "
                "coins=%s tickets=%s",
                deposit.event_id,
                deposit.coins_deposited,
                deposit.raffle_tickets,
            )
            bot._raffle_store.mark_notification_sent(deposit.event_id)
            continue
        if await bot._try_send_raffle_contribution_embed(
            raffle_deposit_embed(deposit)
        ):
            bot._raffle_store.mark_notification_sent(deposit.event_id)

async def send_pending_deposit_audit_notifications(bot: Gw2Bot) -> None:
    pending = bot._raffle_store.get_pending_deposit_audit_notifications()
    LOGGER.debug("Found %s pending raffle deposit audit notifications", len(pending))
    for deposit in pending:
        if await bot._try_send_notification(deposit.message):
            bot._raffle_store.mark_deposit_audit_notification_sent(
                deposit.event_id
            )

async def send_pending_raffle_milestones(bot: Gw2Bot) -> None:
    pending = bot._raffle_store.get_pending_milestones()
    LOGGER.debug("Found %s pending raffle milestones", len(pending))
    for milestone in pending:
        if await bot._try_send_raffle_contribution_message(milestone.message):
            bot._raffle_store.mark_milestone_notification_sent(
                milestone.threshold
            )

async def send_pending_leave_notifications(bot: Gw2Bot) -> None:
    pending = bot._raffle_store.get_pending_leave_notifications()
    LOGGER.debug("Found %s pending guild-leave notifications", len(pending))
    for leave in pending:
        if await bot._try_send_notification(leave.message):
            bot._raffle_store.mark_leave_notification_sent(leave.event_id)

async def send_pending_join_notifications(bot: Gw2Bot) -> None:
    pending = bot._raffle_store.get_pending_join_notifications()
    LOGGER.debug("Found %s pending guild-join notifications", len(pending))
    for join in pending:
        if await bot._try_send_notification(join.message):
            bot._raffle_store.mark_join_notification_sent(join.event_id)

async def send_pending_invite_notifications(bot: Gw2Bot) -> None:
    pending = bot._raffle_store.get_pending_invite_notifications()
    LOGGER.debug("Found %s pending guild-invite notifications", len(pending))
    for invite in pending:
        if await bot._try_send_notification(invite.message):
            bot._raffle_store.mark_invite_notification_sent(invite.event_id)

async def send_pending_rank_change_notifications(bot: Gw2Bot) -> None:
    pending = bot._raffle_store.get_pending_rank_change_notifications()
    LOGGER.debug(
        "Found %s pending guild-rank-change notifications",
        len(pending),
    )
    for rank_change in pending:
        if await bot._try_send_notification(rank_change.message):
            bot._raffle_store.mark_rank_change_notification_sent(
                rank_change.event_id
            )
