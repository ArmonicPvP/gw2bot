from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, cast

import aiohttp
import discord

from gw2bot.discord_utils import (
    TopicEditableChannel,
    discord_failure_reason,
    discord_failure_signature,
    log_discord_failure,
)

if TYPE_CHECKING:
    from gw2bot.bot import Gw2Bot

LOGGER = logging.getLogger(__name__)

GW2_GUILD_MEMBER_LIMIT = 500
GW2_GUILD_INVITED_RANK = "invited"
GUILD_MEMBER_COUNT_TOPIC_UPDATE_SECONDS = 60


def count_active_guild_members(
    members: list[dict[str, Any]],
) -> tuple[int, int]:
    pending_invite_count = sum(
        1
        for member in members
        if str(member.get("rank", "")).strip().casefold()
        == GW2_GUILD_INVITED_RANK
    )
    return len(members) - pending_invite_count, pending_invite_count


def format_guild_member_count_topic(
    member_count: int,
    pending_invite_count: int,
) -> str:
    return f"{member_count}/{GW2_GUILD_MEMBER_LIMIT} ({pending_invite_count} pending)"


async def poll_guild_member_count_topic(bot: Gw2Bot) -> None:
    await bot.wait_until_ready()
    LOGGER.debug("Guild Member Count poller started")
    if bot._api is None:
        raise RuntimeError("GW2 API client was not initialized")
    while not bot.is_closed():
        LOGGER.debug("Starting Guild Member Count poll")
        try:
            updated = await bot._update_guild_member_count_topic()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            bot._poll_status.record_error("Guild Member Count", exc)
        else:
            if updated:
                bot._poll_status.record_success("Guild Member Count")
            LOGGER.debug(
                "Guild Member Count poll completed; topic_updated=%s",
                updated,
            )

        await asyncio.sleep(GUILD_MEMBER_COUNT_TOPIC_UPDATE_SECONDS)

async def update_guild_member_count_topic(bot: Gw2Bot) -> bool:
    if bot._api is None:
        raise RuntimeError("GW2 API client was not initialized")
    members = await bot._api.get_guild_members(bot._config.gw2_guild_id)
    member_count, pending_invite_count = count_active_guild_members(members)
    bot._last_guild_member_count = member_count
    bot._last_pending_guild_invite_count = pending_invite_count
    topic = format_guild_member_count_topic(member_count, pending_invite_count)
    LOGGER.debug(
        "Fetched guild member count; records=%s members=%s "
        "pending_invites=%s topic_characters=%s",
        len(members),
        member_count,
        pending_invite_count,
        len(topic),
    )
    return await bot._try_update_logging_channel_topic(topic)

async def try_update_logging_channel_topic(bot: Gw2Bot, topic: str) -> bool:
    LOGGER.debug(
        "Updating logging channel description; characters=%s",
        len(topic),
    )
    try:
        channel = await bot._get_notification_channel()
        current_topic = getattr(channel, "topic", None)
        if current_topic == topic:
            LOGGER.debug("Logging channel description already current")
            if bot._last_topic_update_failure is not None:
                bot._last_topic_update_failure = None
                LOGGER.info("Logging channel description update recovered")
            return True
        edit = getattr(channel, "edit", None)
        if not callable(edit):
            if bot._last_topic_update_failure != "not_editable":
                bot._last_topic_update_failure = "not_editable"
                LOGGER.error(
                    "Could not update logging channel description; "
                    "channel_id=%s supports_topic=false",
                    bot._config.discord_notification_channel_id,
                )
            return False
        editable_channel = cast(TopicEditableChannel, channel)
        updated_channel = await editable_channel.edit(
            topic=topic,
            reason="Update GW2 guild member count",
        )
    except discord.DiscordException as exc:
        signature = discord_failure_signature(exc)
        if bot._last_topic_update_failure != signature:
            bot._last_topic_update_failure = signature
            log_discord_failure(
                "Could not update logging channel description; reason=%s "
                "channel_id=%s "
                "required_permissions=view_channel,manage_channels",
                exc,
                discord_failure_reason(exc),
                bot._config.discord_notification_channel_id,
            )
        return False
    if updated_channel is not None:
        bot._notification_channel = updated_channel
    if bot._last_topic_update_failure is not None:
        bot._last_topic_update_failure = None
        LOGGER.info("Logging channel description update recovered")
    LOGGER.debug(
        "Updated logging channel description; characters=%s",
        len(topic),
    )
    return True
