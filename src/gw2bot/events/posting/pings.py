"""The separate ping message an occurrence announces itself with.

It is posted alongside the event message rather than inside it, so that
editing the event never re-pings a role, and it is swept, retired or dropped
as the occurrence moves on.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, TYPE_CHECKING

import discord
from sqlalchemy.exc import SQLAlchemyError

from gw2bot.core.discord_utils import (
    discord_failure_reason,
    log_discord_failure,
)
from gw2bot.events.formatting import (
    format_role_mentions,
    message_link,
    ping_announcement_content,
)
from gw2bot.events.models import Event, EventOccurrence, is_pingable_role_name
from gw2bot.events.posting.channels import resolve_channel
from gw2bot.events.posting.state import occurrence_channel_id

if TYPE_CHECKING:
    from gw2bot.bot import Gw2Bot

LOGGER = logging.getLogger(__name__)


def verified_ping_role_ids(
    guild: Any,
    event: Event,
) -> tuple[int, ...]:
    """The event's ping roles that still qualify, checked against the server.

    The stored ids are a snapshot of what the picker offered, and an event
    outlives that snapshot: a weekly series keeps its roles for months, during
    which one can be deleted, or renamed and repurposed into something the
    picker would never have offered - an admin role that then gets pinged by
    every occurrence. The marker is re-checked here, immediately before the
    send, so it is a property of the roles actually notified rather than of
    the picker alone.

    A role that stops qualifying is skipped rather than erased: the id stays on
    the event, so renaming the role back resumes its pings. A guild that cannot
    be resolved pings nothing, because nothing can be checked.
    """
    if not event.ping_role_ids:
        return ()
    if guild is None:
        LOGGER.warning(
            "Cannot check an event's ping roles without a guild; pinging "
            "nobody; event_id=%s ping_roles=%s",
            event.event_id,
            len(event.ping_role_ids),
        )
        return ()
    verified: list[int] = []
    for role_id in event.ping_role_ids:
        role = guild.get_role(role_id)
        if role is None:
            LOGGER.debug(
                "Skipping an event ping role that no longer exists; "
                "event_id=%s",
                event.event_id,
            )
            continue
        if not is_pingable_role_name(role.name):
            LOGGER.warning(
                "Skipping an event ping role that no longer carries the "
                "marker; event_id=%s",
                event.event_id,
            )
            continue
        verified.append(role_id)
    return tuple(verified)


def role_ping_mentions(role_ids: Sequence[int]) -> discord.AllowedMentions:
    """Permission to mention exactly these roles and nothing else.

    A role id that reaches here can never widen into an @everyone or a member
    ping, whatever a title or description happens to contain, and a role that
    members may not mention themselves still pings when the bot may mention
    roles.
    """
    return discord.AllowedMentions(
        everyone=False,
        users=False,
        roles=[discord.Object(id=role_id) for role_id in role_ids],
    )


def ping_send_kwargs(role_ids: Sequence[int]) -> dict[str, Any]:
    """Send arguments that mention the given roles above the embed.

    Empty when there is nothing to ping, so the send stays exactly what it was
    before role pinging existed.
    """
    if not role_ids:
        return {}
    return {
        "content": format_role_mentions(role_ids),
        "allowed_mentions": role_ping_mentions(role_ids),
    }


@dataclass(frozen=True, slots=True)
class PingAnnouncement:
    """Where an event in a forum post pings its roles, and in which server.

    The server is carried alongside the channel because the announcement's
    only job is to link back to the post, and a jump link is addressed by
    (server, channel, message).
    """

    channel: Any
    guild_id: int


async def resolve_ping_announcement(
    bot: Gw2Bot,
    guild: Any,
    occurrence_id: int,
) -> PingAnnouncement | None:
    """Where an event in a forum post should ping its roles, if anywhere else.

    A forum post only notifies the members already following it, so mentioning
    a role inside one reaches almost nobody it was meant for. When a channel is
    configured the mentions are sent there instead, with a link back to the
    post.

    Resolved before the event message goes out, because that is what decides
    whether the message carries the pings itself: a channel that has been
    deleted or hidden since it was set falls back to pinging in the post, which
    is what an unset setting does and what every event did before this existed.
    """
    channel_id = bot._config.event_ping_channel_id
    if channel_id is None:
        LOGGER.debug(
            "No event ping channel is configured; pinging in the forum post; "
            "occurrence_id=%s",
            occurrence_id,
        )
        return None
    guild_id = getattr(guild, "id", None)
    if guild_id is None:
        # The roles were checked against a server, so this cannot happen for an
        # event with roles left to ping. It is still the value a link needs, so
        # it is checked here rather than assumed at the send.
        LOGGER.warning(
            "Cannot link back to an event's post without a server; pinging "
            "in the forum post; occurrence_id=%s",
            occurrence_id,
        )
        return None
    try:
        channel = await resolve_channel(bot, channel_id)
    except discord.DiscordException as exc:
        log_discord_failure(
            "Could not read the event ping channel; pinging in the forum "
            "post instead; occurrence_id=%s reason=%s",
            exc,
            occurrence_id,
            discord_failure_reason(exc),
        )
        return None
    return PingAnnouncement(channel, guild_id)


async def announce_occurrence_ping(
    channel: Any,
    event: Event,
    occurrence: EventOccurrence,
    link: str,
    role_ids: Sequence[int],
) -> Any | None:
    """Ping an event's roles outside the forum post it was posted into.

    Sent once the occurrence is persisted, so the link can never point at a
    message the failed-write recovery has already deleted. A failed
    announcement costs the occurrence its ping and nothing else: the post is
    up and its buttons work, and raising here would only re-post it.

    Returns the announcement, so the caller can record it against the
    occurrence and None says plainly that nothing was pinged - which is what
    the posting log reports rather than the weaker fact that a channel
    resolved.
    """
    # Logged before the await, not only after it: a send that hangs, is
    # cancelled, or is cut off by a restart reaches neither the success nor the
    # failure line below, and the delivery would then leave no trace at all.
    LOGGER.debug(
        "Announcing an event's role pings; occurrence_id=%s pinged_roles=%s",
        occurrence.occurrence_id,
        len(role_ids),
    )
    try:
        message = await channel.send(
            content=ping_announcement_content(
                event.title,
                occurrence.start_time,
                role_ids,
                link,
            ),
            allowed_mentions=role_ping_mentions(role_ids),
        )
    except discord.DiscordException as exc:
        log_discord_failure(
            "Could not announce an event's role pings; occurrence_id=%s "
            "reason=%s pinged_roles=%s",
            exc,
            occurrence.occurrence_id,
            discord_failure_reason(exc),
            len(role_ids),
        )
        return None
    LOGGER.debug(
        "Announced an event's role pings outside its forum post; "
        "event_id=%s occurrence_id=%s pinged_roles=%s",
        event.event_id,
        occurrence.occurrence_id,
        len(role_ids),
    )
    return message


def record_occurrence_announcement(
    bot: Gw2Bot,
    occurrence_id: int,
    channel_id: int,
    message_id: int,
    role_ids: Sequence[int],
) -> bool:
    """Remember an announcement so the occurrence's cleanup can remove it.

    False says the announcement is untracked, which the caller has to answer
    while it is still in hand. Nothing else can: a move, a cancellation or a
    delete all reach the announcement through this row, so one missing from it
    outlives the message it links to with nothing left to find it. The write
    that failed is the same row and session any retry would need, so the note
    cannot be parked elsewhere either.
    """
    try:
        bot.event_store.set_occurrence_ping_message(
            occurrence_id,
            channel_id,
            message_id,
            tuple(role_ids),
        )
    except (SQLAlchemyError, ValueError) as exc:
        LOGGER.error(
            "Could not record an event's ping announcement; it will not be "
            "cleaned up with the event; occurrence_id=%s error_type=%s",
            occurrence_id,
            type(exc).__name__,
        )
        return False
    return True


async def delete_occurrence_announcement(
    channel: Any,
    message_id: int,
    occurrence_id: int,
) -> bool:
    """Remove the announcement that pinged an occurrence's roles.

    An announcement is only ever a pointer at the event's message, so once
    that message goes the announcement is a link to nothing. Best-effort like
    every other cleanup here: one that cannot be removed is logged and the
    rest of the removal carries on.

    True means the announcement is no longer there, whether this call removed
    it or found it already gone. Both answers let a caller stop tracking it;
    only a refusal is worth keeping for another try.
    """
    LOGGER.debug(
        "Removing an event's ping announcement; occurrence_id=%s",
        occurrence_id,
    )
    try:
        await channel.get_partial_message(message_id).delete()
    except discord.NotFound:
        LOGGER.debug(
            "Event ping announcement already gone; skipping delete; "
            "occurrence_id=%s",
            occurrence_id,
        )
        return True
    except discord.HTTPException as exc:
        log_discord_failure(
            "Could not delete an event's ping announcement; reason=%s "
            "occurrence_id=%s",
            exc,
            discord_failure_reason(exc),
            occurrence_id,
        )
        return False
    LOGGER.debug(
        "Deleted an event's ping announcement; occurrence_id=%s",
        occurrence_id,
    )
    return True


async def drop_announcement(
    bot: Gw2Bot,
    channel_id: int,
    message_id: int,
    occurrence_id: int,
) -> bool:
    """Resolve an announcement's channel and take the announcement out of it.

    A channel that cannot be read is the same answer as a refused delete: the
    announcement is still there, so whoever tracked it keeps tracking it.
    """
    try:
        channel = await resolve_channel(bot, channel_id)
    except discord.DiscordException as exc:
        log_discord_failure(
            "Could not read the channel holding an event's ping "
            "announcement; reason=%s occurrence_id=%s",
            exc,
            discord_failure_reason(exc),
            occurrence_id,
        )
        return False
    return await delete_occurrence_announcement(
        channel,
        message_id,
        occurrence_id,
    )


async def sweep_stale_announcement(
    bot: Gw2Bot,
    occurrence: EventOccurrence,
) -> None:
    """Retry the announcement removals an earlier pass could not finish.

    A channel move sends the replacement before removing what it replaced, and
    the occurrence has one pair of columns for the announcement it is carrying
    now - so a removal Discord refuses is parked here instead of being lost,
    and every later pass over this occurrence tries it again until it is gone.

    Each one is attempted and forgotten on its own: a refusal that persists for
    one announcement must not hold back the others owed beside it, and a
    removal that lands must not clear their notes with it.
    """
    if not occurrence.stale_ping_messages:
        return
    LOGGER.debug(
        "Retrying leftover event ping announcements; occurrence_id=%s "
        "outstanding=%s",
        occurrence.occurrence_id,
        len(occurrence.stale_ping_messages),
    )
    for channel_id, message_id in occurrence.stale_ping_messages:
        if not await drop_announcement(
            bot,
            channel_id,
            message_id,
            occurrence.occurrence_id,
        ):
            continue
        try:
            bot.event_store.remove_occurrence_stale_ping_message(
                occurrence.occurrence_id,
                channel_id,
                message_id,
            )
        except (SQLAlchemyError, ValueError) as exc:
            # The announcement is gone; only the note saying so failed to
            # clear, so the next pass reads it as already removed and clears
            # it then.
            LOGGER.error(
                "Could not clear a removed leftover ping announcement; "
                "occurrence_id=%s error_type=%s",
                occurrence.occurrence_id,
                type(exc).__name__,
            )


async def retire_occurrence_announcement(
    bot: Gw2Bot,
    event: Event,
    occurrence: EventOccurrence,
) -> None:
    """Remove the announcement of an occurrence whose message has gone.

    The retirement path answers a message deleted in Discord by hand. Nothing
    else will come back for it: a one-off occurrence is persisted OVER and
    drops out of maintenance, so without this the ping channel keeps a link to
    a message that is not there until somebody deletes the whole event.

    A removal Discord refuses is moved to the outstanding list rather than
    left in the pair, because the pair alone says nothing about whether the
    announcement should still be standing - an occurrence that simply ended
    keeps its announcement, and this one must not. The outstanding list is
    read by maintenance whatever the occurrence's status, so the retry
    survives the OVER this retirement is about to persist.
    """
    await sweep_stale_announcement(bot, occurrence)
    if occurrence.ping_channel_id is None or occurrence.ping_message_id is None:
        return
    if not await drop_announcement(
        bot,
        occurrence.ping_channel_id,
        occurrence.ping_message_id,
        occurrence.occurrence_id,
    ):
        _keep_stale_announcement(
            bot,
            occurrence.occurrence_id,
            occurrence.ping_channel_id,
            occurrence.ping_message_id,
        )
    try:
        # Cleared whether the removal landed or was parked above: either way
        # the pair no longer describes an announcement this occurrence should
        # be carrying.
        bot.event_store.clear_occurrence_ping_message(
            occurrence.occurrence_id
        )
    except (SQLAlchemyError, ValueError) as exc:
        # Harmless on its own: the pair names a message that is gone or one
        # already on the outstanding list, and both read as already removed.
        LOGGER.error(
            "Could not clear a removed ping announcement; occurrence_id=%s "
            "error_type=%s",
            occurrence.occurrence_id,
            type(exc).__name__,
        )


async def refresh_occurrence_announcement(
    bot: Gw2Bot,
    event: Event,
    occurrence: EventOccurrence,
) -> bool:
    """Bring an occurrence's announcement back in line with its event.

    The announcement repeats the title and the start time, and a commander can
    change both after it was sent - its link then opens an event that no longer
    matches what the members it pinged were told. It is refreshed on the same
    trigger as the thread name, which carries the date and time for the same
    reason, so an edit corrects every surface the occurrence has.

    The mentions are left exactly as they were delivered. Editing a message
    does not notify a mention added to it, so re-rendering from the event's
    current pick would claim roles that were never alerted and lose the record
    of who actually was.

    An announcement deleted in Discord is forgotten rather than retried, and any
    other failure is logged and left: the event's own message is the record, and
    holding the occurrence dirty for a channel the bot may have lost access to
    would retry forever.
    """
    if occurrence.ping_channel_id is None or occurrence.ping_message_id is None:
        return True
    if occurrence.message_id is None:
        return True
    try:
        channel = await resolve_channel(bot, occurrence.ping_channel_id)
    except discord.DiscordException as exc:
        log_discord_failure(
            "Could not read the channel holding an event's ping "
            "announcement; reason=%s occurrence_id=%s",
            exc,
            discord_failure_reason(exc),
            occurrence.occurrence_id,
        )
        return False
    guild = getattr(channel, "guild", None)
    guild_id = getattr(guild, "id", None)
    if guild_id is None:
        LOGGER.warning(
            "Cannot rebuild an event's ping announcement without a server; "
            "occurrence_id=%s",
            occurrence.occurrence_id,
        )
        return False
    # What the announcement said when it went out. An occurrence whose
    # announcement predates that being recorded has nothing better to offer
    # than the event's own roles, which is what the announcement was rendered
    # from at the time.
    role_ids = occurrence.ping_role_ids or verified_ping_role_ids(guild, event)
    content = ping_announcement_content(
        event.title,
        occurrence.start_time,
        role_ids,
        message_link(
            guild_id,
            occurrence_channel_id(event, occurrence),
            occurrence.message_id,
        ),
    )
    LOGGER.debug(
        "Correcting an event's ping announcement; occurrence_id=%s "
        "pinged_roles=%s characters=%s",
        occurrence.occurrence_id,
        len(role_ids),
        len(content),
    )
    try:
        await channel.get_partial_message(occurrence.ping_message_id).edit(
            content=content,
            allowed_mentions=role_ping_mentions(role_ids),
        )
    except discord.NotFound:
        LOGGER.debug(
            "Event ping announcement is gone; forgetting it; "
            "occurrence_id=%s",
            occurrence.occurrence_id,
        )
        try:
            bot.event_store.clear_occurrence_ping_message(
                occurrence.occurrence_id
            )
        except (SQLAlchemyError, ValueError) as exc:
            LOGGER.error(
                "Could not clear a missing ping announcement; "
                "occurrence_id=%s error_type=%s",
                occurrence.occurrence_id,
                type(exc).__name__,
            )
        return False
    except discord.HTTPException as exc:
        log_discord_failure(
            "Could not refresh an event's ping announcement; reason=%s "
            "occurrence_id=%s",
            exc,
            discord_failure_reason(exc),
            occurrence.occurrence_id,
        )
        return False
    LOGGER.debug(
        "Refreshed an event's ping announcement; occurrence_id=%s "
        "pinged_roles=%s",
        occurrence.occurrence_id,
        len(role_ids),
    )
    return True


def _keep_stale_announcement(
    bot: Gw2Bot,
    occurrence_id: int,
    channel_id: int,
    message_id: int,
) -> None:
    try:
        bot.event_store.add_occurrence_stale_ping_message(
            occurrence_id,
            channel_id,
            message_id,
        )
    except (SQLAlchemyError, ValueError) as exc:
        # Nothing is left holding the announcement, so it stays where it is.
        # Logged as the reason it will not be cleaned up rather than retried
        # here: the row is what a retry would have to read.
        LOGGER.error(
            "Could not keep an event's ping announcement for removal; it "
            "will be left behind; occurrence_id=%s error_type=%s",
            occurrence_id,
            type(exc).__name__,
        )
