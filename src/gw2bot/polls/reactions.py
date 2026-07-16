from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

import discord
from sqlalchemy.exc import SQLAlchemyError

from gw2bot.polls.formatting import build_poll_embed
from gw2bot.polls.models import (
    Poll,
    emoji_for_index,
    index_for_emoji,
    is_valid_option_index,
)

if TYPE_CHECKING:
    from gw2bot.bot import Gw2Bot

LOGGER = logging.getLogger(__name__)

# How long reaction-driven renders are coalesced. Rapid voting on a busy poll
# collapses into a single trailing edit, keeping well clear of Discord's message
# edit rate limit while still updating within a couple of seconds.
RENDER_DEBOUNCE_SECONDS = 1.5


async def resolve_channel(bot: Gw2Bot, channel_id: int) -> Any:
    channel = bot.get_channel(channel_id)
    if channel is None:
        channel = await bot.fetch_channel(channel_id)
    return channel


def _bot_user_id(bot: Gw2Bot) -> int | None:
    user = bot.user
    return user.id if user is not None else None


async def fetch_poll_message(bot: Gw2Bot, poll: Poll) -> discord.Message | None:
    """The poll's live message, or None if it was permanently deleted.

    A NotFound means the message (or its channel) is gone; other HTTP errors are
    transient and are re-raised so the caller can retry on the next pass.
    """
    if poll.message_id is None:
        return None
    channel = await resolve_channel(bot, poll.channel_id)
    try:
        return await channel.fetch_message(poll.message_id)
    except discord.NotFound:
        return None


async def read_live_votes(
    message: discord.Message,
    option_count: int,
    bot_user_id: int | None,
) -> dict[int, set[int]]:
    """Voter ids per option, taken straight from the message's reactions.

    The bot's own seed reactions are excluded so they never count toward a
    tally. This is the source of truth used to reconcile the stored votes.
    """
    votes: dict[int, set[int]] = {}
    for reaction in message.reactions:
        index = index_for_emoji(str(reaction.emoji))
        if not is_valid_option_index(index, option_count):
            continue
        assert index is not None
        voters: set[int] = set()
        async for user in reaction.users():
            if bot_user_id is not None and user.id == bot_user_id:
                continue
            voters.add(user.id)
        votes[index] = voters
    return votes


async def render_poll_message(
    bot: Gw2Bot,
    poll: Poll,
    *,
    ended: bool = False,
    message: discord.Message | None = None,
) -> None:
    counts = bot.poll_store.get_vote_counts(poll.poll_id)
    embed = build_poll_embed(poll, counts, ended=ended)
    try:
        if message is not None:
            await message.edit(embed=embed)
        elif poll.message_id is not None:
            channel = await resolve_channel(bot, poll.channel_id)
            await channel.get_partial_message(poll.message_id).edit(embed=embed)
    except discord.HTTPException as exc:
        LOGGER.error(
            "Could not render poll message; poll_id=%s ended=%s error_type=%s",
            poll.poll_id,
            ended,
            type(exc).__name__,
        )


async def reconcile_poll(bot: Gw2Bot, poll: Poll) -> Poll | None:
    """Rewrite the stored votes to match the live reactions, then re-render.

    Returns the poll, or None if its message is permanently gone (in which case
    the poll is deleted, since it can no longer be shown or voted on).
    """
    try:
        message = await fetch_poll_message(bot, poll)
    except discord.HTTPException as exc:
        LOGGER.error(
            "Could not fetch poll message to reconcile; poll_id=%s "
            "error_type=%s",
            poll.poll_id,
            type(exc).__name__,
        )
        return poll
    if message is None:
        LOGGER.warning(
            "Poll message is gone; deleting poll; poll_id=%s",
            poll.poll_id,
        )
        bot.poll_store.delete_poll(poll.poll_id)
        return None
    live = await read_live_votes(message, len(poll.options), _bot_user_id(bot))
    bot.poll_store.replace_votes(poll.poll_id, live)
    await render_poll_message(bot, poll, message=message)
    LOGGER.debug("Reconciled poll from live reactions; poll_id=%s", poll.poll_id)
    return poll


async def finalize_poll(bot: Gw2Bot, poll: Poll, *, reason: str) -> bool:
    """End a poll: lock the message to final results, clear its reactions, and
    stop tracking it. Returns False when a transient failure means it should be
    retried on the next pass (its rows are kept)."""
    try:
        message = await fetch_poll_message(bot, poll)
    except discord.HTTPException as exc:
        LOGGER.error(
            "Could not fetch poll message to finalize; poll_id=%s "
            "error_type=%s",
            poll.poll_id,
            type(exc).__name__,
        )
        return False
    if message is not None:
        live = await read_live_votes(
            message,
            len(poll.options),
            _bot_user_id(bot),
        )
        bot.poll_store.replace_votes(poll.poll_id, live)
        counts = bot.poll_store.get_vote_counts(poll.poll_id)
        try:
            await message.edit(embed=build_poll_embed(poll, counts, ended=True))
        except discord.HTTPException as exc:
            LOGGER.error(
                "Could not render final poll results; poll_id=%s error_type=%s",
                poll.poll_id,
                type(exc).__name__,
            )
            return False
        try:
            await message.clear_reactions()
        except discord.HTTPException as exc:
            # Clearing needs Manage Messages; the poll still ends without it.
            LOGGER.error(
                "Could not clear poll reactions on finalize; poll_id=%s "
                "error_type=%s",
                poll.poll_id,
                type(exc).__name__,
            )
    bot.poll_store.delete_poll(poll.poll_id)
    LOGGER.debug(
        "Finalized poll; poll_id=%s reason=%s message_present=%s",
        poll.poll_id,
        reason,
        message is not None,
    )
    return True


async def seed_poll_reactions(
    message: discord.Message | discord.PartialMessage,
    option_count: int,
) -> None:
    for index in range(option_count):
        await message.add_reaction(emoji_for_index(index))


async def post_poll(bot: Gw2Bot, poll: Poll) -> Poll:
    """Send a poll's message, store it, and seed its option reactions.

    The message id is stored before the reactions are seeded so a reaction event
    (the bot's own seeds, which are ignored) can already resolve the poll. Any
    failure after the message is sent deletes it, so a failed post never leaves
    an orphaned message whose reactions reference no poll.
    """
    channel = await resolve_channel(bot, poll.channel_id)
    message = await channel.send(embed=build_poll_embed(poll, {}))
    try:
        bot.poll_store.set_poll_message(poll.poll_id, poll.channel_id, message.id)
        await seed_poll_reactions(message, len(poll.options))
    except (discord.HTTPException, SQLAlchemyError):
        try:
            await message.delete()
        except discord.HTTPException as exc:
            LOGGER.error(
                "Could not delete orphaned poll message; poll_id=%s "
                "error_type=%s",
                poll.poll_id,
                type(exc).__name__,
            )
        raise
    updated = bot.poll_store.get_poll(poll.poll_id)
    if updated is None:
        raise RuntimeError("The posted poll disappeared")
    LOGGER.debug(
        "Posted poll; poll_id=%s options=%s", poll.poll_id, len(poll.options)
    )
    return updated


async def repost_poll(
    bot: Gw2Bot,
    poll: Poll,
    old_channel_id: int,
    new_channel_id: int,
) -> Poll:
    """Re-post a poll in a new channel, resetting its reactions and votes.

    Discord cannot move a message between channels, so a channel change sends a
    fresh message and removes the old one. Reactions (and therefore votes) do not
    survive the move, so the votes are cleared and reactions re-seeded.
    """
    new_channel = await resolve_channel(bot, new_channel_id)
    message = await new_channel.send(embed=build_poll_embed(poll, {}))
    bot.poll_store.set_poll_message(poll.poll_id, new_channel_id, message.id)
    bot.poll_store.clear_votes(poll.poll_id)
    await seed_poll_reactions(message, len(poll.options))
    if poll.message_id is not None:
        try:
            old_channel = await resolve_channel(bot, old_channel_id)
            await old_channel.get_partial_message(poll.message_id).delete()
        except discord.HTTPException as exc:
            LOGGER.error(
                "Could not delete old poll message during channel move; "
                "poll_id=%s error_type=%s",
                poll.poll_id,
                type(exc).__name__,
            )
    LOGGER.debug("Reposted poll to a new channel; poll_id=%s", poll.poll_id)
    updated = bot.poll_store.get_poll(poll.poll_id)
    return updated if updated is not None else poll


async def reseed_poll_reactions(bot: Gw2Bot, poll: Poll) -> None:
    """Clear and re-add a poll's option reactions in place.

    Used when an edit changes the number of options, so the reactions match the
    new option set. Votes are cleared separately by the caller.
    """
    if poll.message_id is None:
        return
    channel = await resolve_channel(bot, poll.channel_id)
    message = channel.get_partial_message(poll.message_id)
    try:
        await message.clear_reactions()
    except discord.HTTPException as exc:
        LOGGER.error(
            "Could not clear reactions before reseeding; poll_id=%s "
            "error_type=%s",
            poll.poll_id,
            type(exc).__name__,
        )
    await seed_poll_reactions(message, len(poll.options))


async def remove_user_reaction(
    bot: Gw2Bot,
    poll: Poll,
    option_index: int,
    discord_user_id: int,
) -> None:
    if poll.message_id is None:
        return
    try:
        channel = await resolve_channel(bot, poll.channel_id)
        await channel.get_partial_message(poll.message_id).remove_reaction(
            emoji_for_index(option_index),
            discord.Object(id=discord_user_id),
        )
    except discord.HTTPException as exc:
        # Removing someone else's reaction needs Manage Messages; without it the
        # extra reaction lingers, but the stored vote was already dropped.
        LOGGER.error(
            "Could not remove a voter's reaction; poll_id=%s option_index=%s "
            "error_type=%s",
            poll.poll_id,
            option_index,
            type(exc).__name__,
        )


async def enforce_single_choice(bot: Gw2Bot, poll: Poll) -> None:
    """Trim every voter to their newest option after a poll is switched from
    multiple to single choice."""
    if poll.allow_multiple:
        return
    by_user = bot.poll_store.get_user_option_times(poll.poll_id)
    for user_id, entries in by_user.items():
        if len(entries) <= 1:
            continue
        # entries are oldest first, so the last one is the vote to keep.
        for option_index, _ in entries[:-1]:
            bot.poll_store.remove_vote(poll.poll_id, option_index, user_id)
            await remove_user_reaction(bot, poll, option_index, user_id)


def _schedule_render(bot: Gw2Bot, poll: Poll) -> None:
    renderer = getattr(bot, "poll_renderer", None)
    if renderer is not None:
        renderer.schedule(poll)


async def handle_reaction_add(
    bot: Gw2Bot,
    payload: discord.RawReactionActionEvent,
) -> None:
    if payload.user_id == _bot_user_id(bot):
        return
    poll = bot.poll_store.get_poll_by_message(payload.message_id)
    if poll is None:
        return
    index = index_for_emoji(str(payload.emoji))
    if not is_valid_option_index(index, len(poll.options)):
        return
    assert index is not None
    bot.poll_store.add_vote(poll.poll_id, index, payload.user_id)
    if not poll.allow_multiple:
        others = [
            option_index
            for option_index in bot.poll_store.get_user_options(
                poll.poll_id,
                payload.user_id,
            )
            if option_index != index
        ]
        for option_index in others:
            bot.poll_store.remove_vote(
                poll.poll_id,
                option_index,
                payload.user_id,
            )
            await remove_user_reaction(
                bot,
                poll,
                option_index,
                payload.user_id,
            )
    LOGGER.debug(
        "Poll reaction added; poll_id=%s option_index=%s allow_multiple=%s",
        poll.poll_id,
        index,
        poll.allow_multiple,
    )
    _schedule_render(bot, poll)


async def handle_reaction_remove(
    bot: Gw2Bot,
    payload: discord.RawReactionActionEvent,
) -> None:
    if payload.user_id == _bot_user_id(bot):
        return
    poll = bot.poll_store.get_poll_by_message(payload.message_id)
    if poll is None:
        return
    index = index_for_emoji(str(payload.emoji))
    if not is_valid_option_index(index, len(poll.options)):
        return
    assert index is not None
    bot.poll_store.remove_vote(poll.poll_id, index, payload.user_id)
    LOGGER.debug(
        "Poll reaction removed; poll_id=%s option_index=%s",
        poll.poll_id,
        index,
    )
    _schedule_render(bot, poll)


class PollRenderer:
    """Coalesces reaction-driven re-renders so a burst of votes results in a
    single trailing message edit per poll."""

    def __init__(self, bot: Gw2Bot, delay: float = RENDER_DEBOUNCE_SECONDS):
        self._bot = bot
        self._delay = delay
        self._tasks: dict[int, asyncio.Task[None]] = {}

    def schedule(self, poll: Poll) -> None:
        message_id = poll.message_id
        if message_id is None:
            return
        if message_id in self._tasks:
            # A trailing render is already pending; it will read the latest
            # stored counts when it fires.
            return
        self._tasks[message_id] = asyncio.create_task(self._run(message_id))

    async def _run(self, message_id: int) -> None:
        try:
            await asyncio.sleep(self._delay)
            poll = self._bot.poll_store.get_poll_by_message(message_id)
            if poll is not None:
                await render_poll_message(self._bot, poll)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOGGER.error(
                "Poll render task failed; error_type=%s",
                type(exc).__name__,
            )
        finally:
            self._tasks.pop(message_id, None)

    def cancel_all(self) -> None:
        for task in self._tasks.values():
            task.cancel()
        self._tasks.clear()
