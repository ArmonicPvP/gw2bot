"""Resolving the channel or thread an occurrence lives in, and keeping it usable.

A text channel takes the event as a message with a signup thread under it; a
forum post takes it as a message inside the post, which stands in for that
thread. Reopening, renaming and deleting either one happens here.
"""

from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING

import discord

from gw2bot.core.discord_utils import (
    discord_failure_reason,
    log_discord_failure,
)
from gw2bot.events.formatting import event_thread_name
from gw2bot.events.models import Event, EventOccurrence, EventStatus
from gw2bot.events.posting.state import occurrence_posted_in_thread

if TYPE_CHECKING:
    from gw2bot.bot import Gw2Bot

LOGGER = logging.getLogger(__name__)


# An event can be posted into a forum post, which Discord models as a thread.
# The bot never opens one: the post belongs to whoever created it, so the event
# is only a message inside it. Every thread type is listed because this answers
# the mechanical question "does the message live in the thread itself?" - the
# picker in views.py is what limits the choice to forum posts.
THREAD_CHANNEL_TYPES = frozenset(
    {
        discord.ChannelType.public_thread,
        discord.ChannelType.private_thread,
        discord.ChannelType.news_thread,
    }
)


async def resolve_channel(bot: Gw2Bot, channel_id: int) -> Any:
    channel = bot.get_channel(channel_id)
    if channel is None:
        channel = await bot.fetch_channel(channel_id)
    return channel


def is_thread_channel(channel: Any) -> bool:
    return getattr(channel, "type", None) in THREAD_CHANNEL_TYPES


async def _delete_orphaned_message(
    bot: Gw2Bot,
    message: Any,
    channel_id: int,
    thread_id: int | None,
    occurrence_id: int,
) -> None:
    try:
        await message.delete()
    except discord.HTTPException as exc:
        LOGGER.error(
            "Could not delete orphaned event message; occurrence_id=%s "
            "error_type=%s",
            occurrence_id,
            type(exc).__name__,
        )
    await _delete_occurrence_thread(bot, channel_id, thread_id, occurrence_id)


async def _delete_occurrence_thread(
    bot: Gw2Bot,
    channel_id: int | None,
    thread_id: int | None,
    occurrence_id: int,
) -> None:
    # Discord does not delete a thread when its starter message is removed;
    # the thread survives as an orphan unless it is deleted separately.
    if thread_id is None:
        LOGGER.debug(
            "No event thread to delete; skipping; occurrence_id=%s",
            occurrence_id,
        )
        return
    if thread_id == channel_id:
        # The event was posted into a thread that already existed, so the thread
        # is not the bot's to remove: deleting it would take the forum post and
        # everything else in it with the event's message.
        LOGGER.debug(
            "Event thread was not created for the event; keeping it; "
            "occurrence_id=%s",
            occurrence_id,
        )
        return
    try:
        thread = await resolve_channel(bot, thread_id)
        await thread.delete()
    except discord.NotFound:
        LOGGER.debug(
            "Event thread already gone; skipping delete; occurrence_id=%s",
            occurrence_id,
        )
        return
    except discord.HTTPException as exc:
        log_discord_failure(
            "Could not delete event thread; reason=%s occurrence_id=%s "
            "required_permissions=manage_threads",
            exc,
            discord_failure_reason(exc),
            occurrence_id,
        )
        return
    LOGGER.debug(
        "Deleted event thread; occurrence_id=%s",
        occurrence_id,
    )


async def _reopen_thread(thread: Any, occurrence_id: int) -> None:
    # Discord archives a thread after a stretch of inactivity, and refuses both
    # messages and message edits inside an archived one. An event's own signup
    # thread stays open through the event's updates, but a forum post the event
    # was posted into can be dormant for weeks, so it is reopened before the bot
    # writes in it. A failure here is only logged: the send or edit that follows
    # reports the real outcome to its own caller.
    if not getattr(thread, "archived", False):
        return
    try:
        await thread.edit(archived=False, reason="Post a GW2 guild event update")
    except discord.HTTPException as exc:
        log_discord_failure(
            "Could not reopen the archived thread holding an event; reason=%s "
            "occurrence_id=%s required_permissions=manage_threads",
            exc,
            discord_failure_reason(exc),
            occurrence_id,
        )
        return
    LOGGER.debug(
        "Reopened the archived thread holding an event; occurrence_id=%s",
        occurrence_id,
    )


async def reopen_occurrence_thread(
    bot: Gw2Bot,
    occurrence: EventOccurrence,
) -> None:
    # Only an occurrence posted into a forum post needs this: its message lives
    # in that post, so an archived post blocks every update to it.
    thread_id = occurrence.thread_id
    if thread_id is None or not occurrence_posted_in_thread(occurrence):
        return
    try:
        thread = await resolve_channel(bot, thread_id)
    except discord.NotFound:
        # The thread was deleted. The caller's own send or edit reports the same
        # thing, so this is expected rather than a failure.
        LOGGER.debug(
            "Thread holding an event is gone; skipping reopen; "
            "occurrence_id=%s",
            occurrence.occurrence_id,
        )
        return
    except discord.HTTPException as exc:
        LOGGER.error(
            "Could not resolve the thread holding an event; occurrence_id=%s "
            "error_type=%s",
            occurrence.occurrence_id,
            type(exc).__name__,
        )
        return
    await _reopen_thread(thread, occurrence.occurrence_id)


async def _rename_occurrence_thread(
    bot: Gw2Bot,
    occurrence: EventOccurrence,
    status: EventStatus,
) -> bool:
    if occurrence.thread_id is None:
        return True
    if occurrence_posted_in_thread(occurrence):
        # The event was posted into a thread that already existed, so its name
        # describes whatever that thread is for - not this event's status. Report
        # success: there is nothing to rename, and returning False would keep the
        # occurrence dirty and block its status from ever being persisted.
        LOGGER.debug(
            "Event thread was not created for the event; keeping its name; "
            "occurrence_id=%s",
            occurrence.occurrence_id,
        )
        return True
    name = event_thread_name(
        status,
        occurrence.start_time,
        bot.event_timezone,
    )
    try:
        thread = await resolve_channel(bot, occurrence.thread_id)
        await thread.edit(name=name)
    except discord.NotFound:
        # The thread was deleted, so there is nothing left to rename. Treat it
        # as done rather than a transient failure: returning False here would
        # block the status from being persisted and keep the occurrence in
        # maintenance forever, retrying this same doomed rename every minute.
        LOGGER.warning(
            "Event thread is gone; skipping rename; occurrence_id=%s",
            occurrence.occurrence_id,
        )
        return True
    except discord.HTTPException as exc:
        LOGGER.error(
            "Could not rename event thread; occurrence_id=%s error_type=%s",
            occurrence.occurrence_id,
            type(exc).__name__,
        )
        return False
    return True


async def _resolve_cached_channel(
    bot: Gw2Bot,
    channels: dict[int, Any],
    unresolvable: set[int],
    channel_id: int,
    event_id: int,
) -> Any | None:
    """Resolve a channel once for a removal that spans several of them.

    A deleted series can hold posts and announcements across a handful of
    channels, so each is resolved once and a dead one is remembered: without
    that, one channel that has gone would be re-fetched for every occurrence
    and could strand the posts in the others.
    """
    if channel_id in unresolvable:
        return None
    channel = channels.get(channel_id)
    if channel is not None:
        return channel
    try:
        channel = await resolve_channel(bot, channel_id)
    except discord.HTTPException as exc:
        unresolvable.add(channel_id)
        LOGGER.error(
            "Could not resolve channel to delete event posts; "
            "event_id=%s error_type=%s",
            event_id,
            type(exc).__name__,
        )
        return None
    channels[channel_id] = channel
    return channel


async def update_thread_membership(
    bot: Gw2Bot,
    occurrence: EventOccurrence,
    discord_user_id: int,
    *,
    add: bool,
) -> None:
    if occurrence.thread_id is None:
        return
    if not add and occurrence_posted_in_thread(occurrence):
        # The event was posted into a thread that already existed, so its members
        # are not this event's roster: the same forum post can hold several
        # events, and members join it to read it. Removing someone who signed out
        # of one event would drop them from every other event in that post, and
        # in a private thread it would take away their access to it. Adding is
        # kept - it only subscribes them - so membership is one-way here and the
        # member leaves the post themselves when they are done with it.
        LOGGER.debug(
            "Event thread was not created for the event; keeping its members; "
            "occurrence_id=%s user_id=%s",
            occurrence.occurrence_id,
            discord_user_id,
        )
        return
    # An archived thread refuses both the membership change here and the roster
    # announcement that follows it, so reopen it first. This is the first thread
    # call of every signup flow; the refresh at the end of the flow relies on the
    # same reopen.
    await reopen_occurrence_thread(bot, occurrence)
    try:
        thread = await resolve_channel(bot, occurrence.thread_id)
        member = discord.Object(id=discord_user_id)
        if add:
            await thread.add_user(member)
        else:
            await thread.remove_user(member)
    except discord.HTTPException as exc:
        LOGGER.error(
            "Could not update event thread membership; occurrence_id=%s "
            "user_id=%s add=%s error_type=%s",
            occurrence.occurrence_id,
            discord_user_id,
            add,
            type(exc).__name__,
        )
    else:
        LOGGER.debug(
            "Updated event thread membership; occurrence_id=%s user_id=%s "
            "add=%s",
            occurrence.occurrence_id,
            discord_user_id,
            add,
        )


def _event_guild(bot: Gw2Bot) -> discord.Guild | None:
    """The server an event's roster is a roster of.

    Membership is a question about that one server, and the posting and
    scheduler paths have no interaction to read it off, so it comes from the
    configured command guild. The guild cache needs no members intent, so this
    is a local lookup rather than a Discord call.
    """
    return bot.get_guild(bot._config.discord_command_guild_id)
