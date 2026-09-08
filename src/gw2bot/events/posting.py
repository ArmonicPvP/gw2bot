from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from weakref import WeakKeyDictionary

import discord
from sqlalchemy.exc import SQLAlchemyError

from gw2bot.discord_utils import (
    GuildMembership,
    discord_failure_reason,
    log_discord_failure,
    resolve_guild_memberships,
)
from gw2bot.events.formatting import (
    compute_status,
    event_embed,
    event_thread_name,
    format_role_mentions,
    message_link,
    next_occurrence_start,
    ping_announcement_content,
    roster_update_messages,
    signup_edit_limit_message,
)
from gw2bot.events.models import (
    AutoSignupChoice,
    Event,
    EventOccurrence,
    EventRole,
    EventSignup,
    EventStatus,
    RepeatFrequency,
    RoleChange,
    RosterAssignment,
    RosterCandidate,
    RosterUpdate,
    available_edit_tokens,
    can_admit,
    is_pingable_role_name,
    is_roster_full,
    normalize_stored_roles,
    preferred_role_order,
    rebalance_signups,
    roster_feasible,
    seated_candidates,
    solve_roster,
)

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


def occurrence_posted_in_thread(occurrence: EventOccurrence) -> bool:
    # True when the event was posted into a forum post rather than into a
    # channel: the post is then both the channel the message was sent to and the
    # thread members discuss it in, so the two stored ids are the same. An
    # occurrence posted to a text channel has its message in the channel and a
    # signup thread the bot opened under it, so they differ. Such a thread is the
    # bot's to rename and delete; a post it was merely posted into is not.
    # Rows written before either id was tracked read as a plain channel message.
    return (
        occurrence.thread_id is not None
        and occurrence.channel_id == occurrence.thread_id
    )


def occurrence_channel_id(event: Event, occurrence: EventOccurrence) -> int:
    # Where an occurrence's message actually lives. Discord addresses a message
    # by (channel, message), so editing or deleting one must target the channel
    # it was posted to. That is not necessarily event.channel_id: a channel edit
    # only re-posts the live occurrences, so finished ones (and any re-post that
    # failed) stay behind in the previous channel. Rows written before the
    # channel was tracked fall back to the event's channel, which is where they
    # were posted.
    return (
        occurrence.channel_id
        if occurrence.channel_id is not None
        else event.channel_id
    )


def occurrence_status(
    event: Event,
    occurrence: EventOccurrence,
    signups: list[EventSignup],
    now: datetime | None = None,
) -> EventStatus:
    current_time = now if now is not None else datetime.now(UTC)
    return compute_status(
        occurrence.start_time,
        event.duration_minutes,
        current_time,
        is_roster_full(event.capacity, signups),
    )


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


def occurrence_embed(
    event: Event,
    occurrence: EventOccurrence,
    signups: list[EventSignup],
    now: datetime | None = None,
) -> discord.Embed:
    status = occurrence_status(event, occurrence, signups, now)
    return event_embed(
        event,
        signups,
        status,
        start_time=occurrence.start_time,
    )


async def post_occurrence(
    bot: Gw2Bot,
    event: Event,
    occurrence: EventOccurrence,
    now: datetime | None = None,
) -> EventOccurrence:
    from gw2bot.events.views import build_signup_view

    signups = bot.event_store.get_signups(occurrence.occurrence_id)
    status = occurrence_status(event, occurrence, signups, now)
    channel = await resolve_channel(bot, event.channel_id)
    embed = occurrence_embed(event, occurrence, signups, now)
    view = build_signup_view(occurrence.occurrence_id)
    in_thread = is_thread_channel(channel)
    thread_id: int | None = None
    if in_thread:
        # Discord refuses messages in an archived thread, and a forum post can
        # have been dormant for weeks before an event is posted into it.
        await _reopen_thread(channel, occurrence.occurrence_id)
    # The role pings ride on the message content, which Discord renders above
    # the embed. Only the post pings them: reminders address the roster. The
    # roles are checked against the server the event is going to, which is the
    # one whose roles the mentions would notify.
    guild = getattr(channel, "guild", None)
    ping_role_ids = verified_ping_role_ids(guild, event)
    # An event sent into a forum post announces itself somewhere the roles will
    # actually see it, when a channel is configured for that. The post then
    # carries no mentions of its own, so nobody is pinged twice for one event.
    announcement = (
        await resolve_ping_announcement(bot, guild, occurrence.occurrence_id)
        if in_thread and ping_role_ids
        else None
    )
    ping_kwargs = (
        {} if announcement is not None else ping_send_kwargs(ping_role_ids)
    )
    message = await channel.send(embed=embed, view=view, **ping_kwargs)
    if in_thread:
        # The event went into a forum post that already exists. Nothing is
        # created there - a forum post cannot hold threads of its own - and the
        # message lives in the post rather than in its parent forum, so the post
        # is what every later edit and delete has to address, and it stands in
        # for the signup thread the branch below opens for a channel.
        message_channel_id = channel.id
        thread_id = channel.id
    else:
        message_channel_id = event.channel_id
        try:
            thread = await message.create_thread(
                name=event_thread_name(
                    status,
                    occurrence.start_time,
                    bot.event_timezone,
                ),
            )
            thread_id = thread.id
        except discord.HTTPException as exc:
            LOGGER.error(
                "Could not create event thread; occurrence_id=%s error_type=%s",
                occurrence.occurrence_id,
                type(exc).__name__,
            )
    try:
        # Write the status before the message id. The message id marks the
        # occurrence as posted, so persisting it last keeps the sequence
        # recoverable: if any write fails the occurrence still looks unposted
        # and the just-sent message can be deleted, avoiding an orphaned post
        # (whose buttons would reference a missing occurrence) or a duplicate
        # message from the next scheduler pass.
        bot.event_store.set_occurrence_status(occurrence.occurrence_id, status)
        bot.event_store.set_occurrence_message(
            occurrence.occurrence_id,
            message_channel_id,
            message.id,
            thread_id,
        )
    except (SQLAlchemyError, ValueError) as exc:
        # A ValueError here means the row went away while the message was in
        # flight - the event was deleted, or this run cancelled, by someone
        # else in that window. It is the same situation as a failed write and
        # needs the same recovery: without it the message just sent stays in
        # the channel forever, referencing an occurrence that no longer exists.
        LOGGER.error(
            "Could not persist posted event occurrence; occurrence_id=%s "
            "error_type=%s",
            occurrence.occurrence_id,
            type(exc).__name__,
        )
        await _delete_orphaned_message(
            bot,
            message,
            message_channel_id,
            thread_id,
            occurrence.occurrence_id,
        )
        raise
    announced = None
    withdrawn = False
    if announcement is not None:
        announced = await announce_occurrence_ping(
            announcement.channel,
            event,
            occurrence,
            message_link(
                announcement.guild_id,
                message_channel_id,
                message.id,
            ),
            ping_role_ids,
        )
        if announced is not None and not record_occurrence_announcement(
            bot,
            occurrence.occurrence_id,
            announcement.channel.id,
            announced.id,
            ping_role_ids,
        ):
            # An announcement the row does not hold is one no later move,
            # cancellation or delete can reach, and it would sit in the ping
            # channel linking to a message those paths remove. This is the
            # only moment it is still in hand, so it is taken back here.
            #
            # The ping itself cannot be taken back - the members it named have
            # already been notified - so this costs them a link to an event
            # that is up and working. That is the smaller harm than a link
            # left pointing at nothing for good, and the event's own post
            # carries no mentions to fall back on either way. Both facts are
            # logged below rather than collapsed into one flag.
            withdrawn = await delete_occurrence_announcement(
                announcement.channel,
                announced.id,
                occurrence.occurrence_id,
            )
    LOGGER.debug(
        "Posted event occurrence; event_id=%s occurrence_id=%s status=%s "
        "in_existing_thread=%s thread_created=%s signups=%s "
        "stored_ping_roles=%s pinged_roles=%s pinged_from_channel=%s "
        "announcement_withdrawn=%s",
        event.event_id,
        occurrence.occurrence_id,
        status.value,
        in_thread,
        thread_id is not None and not in_thread,
        len(signups),
        len(event.ping_role_ids),
        # How many of the stored roles survived the check, so a ping that
        # silently stopped going out is visible in the log.
        len(ping_role_ids),
        # Whether the announcement was actually delivered, not merely whether
        # a channel was configured for it: a refused send pings nobody.
        announced is not None,
        # And whether it was taken back again because the row could not hold
        # it. Kept apart from the line above so the log still says the roles
        # were pinged, which is what the members saw.
        withdrawn,
    )
    updated = bot.event_store.get_occurrence(occurrence.occurrence_id)
    if updated is None:
        raise RuntimeError("The posted event occurrence disappeared")
    return updated


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


async def refresh_occurrence_message(
    bot: Gw2Bot,
    event: Event,
    occurrence: EventOccurrence,
    now: datetime | None = None,
    *,
    force_thread_rename: bool = False,
) -> EventStatus:
    signups = bot.event_store.get_signups(occurrence.occurrence_id)
    status = occurrence_status(event, occurrence, signups, now)
    # Owed from an earlier move and cheap to skip: the columns are almost
    # always empty, and when they are not this is the pass that finishes the
    # removal.
    await sweep_stale_announcement(bot, occurrence)
    message_refreshed = True
    if occurrence.message_id is not None:
        # Discord rejects edits inside an archived thread, so an event posted
        # into one is reopened first; otherwise every refresh would fail and the
        # occurrence would sit dirty with a stale embed until it was retired.
        await reopen_occurrence_thread(bot, occurrence)
        try:
            channel = await resolve_channel(
                bot,
                occurrence_channel_id(event, occurrence),
            )
            await channel.get_partial_message(occurrence.message_id).edit(
                embed=occurrence_embed(event, occurrence, signups, now),
            )
        except discord.NotFound:
            # The message or its channel was permanently deleted. Retrying
            # every maintenance pass would fail forever, so retire the
            # occurrence: persist OVER and clear the refresh flag so it drops
            # out of get_posted_unfinished_occurrences() instead of logging the
            # same failure each minute.
            LOGGER.warning(
                "Event message or channel is gone; retiring occurrence; "
                "occurrence_id=%s",
                occurrence.occurrence_id,
            )
            # We may be retiring an occurrence that has not naturally ended, so
            # the scheduler's normal "create the next occurrence once status is
            # OVER" path never runs for it. Seed the next occurrence here so a
            # recurring series does not stop after a single deleted message.
            current_time = now if now is not None else datetime.now(UTC)
            ensure_next_recurring_occurrence(
                bot, event, occurrence, current_time
            )
            bot.event_store.set_occurrence_status(
                occurrence.occurrence_id,
                EventStatus.OVER,
            )
            if occurrence.needs_refresh:
                bot.event_store.set_occurrence_needs_refresh(
                    occurrence.occurrence_id,
                    False,
                )
            # The announcement points at the message that has just gone, and a
            # retired occurrence leaves maintenance, so this is the last pass
            # that will look at it.
            await retire_occurrence_announcement(bot, event, occurrence)
            return EventStatus.OVER
        except discord.HTTPException as exc:
            LOGGER.error(
                "Could not refresh event message; occurrence_id=%s "
                "error_type=%s",
                occurrence.occurrence_id,
                type(exc).__name__,
            )
            message_refreshed = False
    # Only commit the status transition once both the message and the thread
    # name reflect it. Committing early (especially to OVER) would let the
    # scheduler see a matching status and stop retrying, leaving the public
    # message or thread name stale forever. An edit that reschedules the
    # occurrence forces a rename even when the status is unchanged, because the
    # thread name encodes the date and time. A dirty occurrence also re-attempts
    # the rename: the forced rename may have failed transiently, and the
    # scheduler's retry (which never passes force_thread_rename) must still be
    # able to finish it before clearing the dirty flag.
    status_changed = status != occurrence.status
    thread_renamed = True
    if message_refreshed and (
        status_changed or force_thread_rename or occurrence.needs_refresh
    ):
        thread_renamed = await _rename_occurrence_thread(
            bot,
            occurrence,
            status,
        )
        # The announcement repeats the title and the start, so it goes stale on
        # exactly the edits the thread name does. A failure here is logged and
        # does not hold back the status: the event's own message is the record,
        # and the announcement is a notice that has already been delivered.
        await refresh_occurrence_announcement(bot, event, occurrence)
        if thread_renamed and status_changed:
            if status is EventStatus.OVER:
                # The status is recomputed inside this call, after the awaited
                # Discord I/O above, so a caller other than the scheduler - a
                # roster change landing just before start + duration - can be
                # the one that crosses into OVER. The scheduler secures the next
                # occurrence before an OVER transition, but a non-scheduler
                # caller has not, so seed it here too (mirroring the NotFound
                # branch), or the series would end silently once this occurrence
                # drops out of the unfinished set. ensure_next_recurring_
                # occurrence is idempotent, so the scheduler's pre-seed is never
                # duplicated.
                current_time = now if now is not None else datetime.now(UTC)
                ensure_next_recurring_occurrence(
                    bot, event, occurrence, current_time
                )
            bot.event_store.set_occurrence_status(
                occurrence.occurrence_id,
                status,
            )
            LOGGER.debug(
                "Event occurrence status transitioned; occurrence_id=%s "
                "previous=%s status=%s",
                occurrence.occurrence_id,
                occurrence.status.value,
                status.value,
            )
    if not (message_refreshed and thread_renamed):
        # A stale message or thread name must be retried by the scheduler,
        # so mark the occurrence dirty and leave the stored status alone.
        if not occurrence.needs_refresh:
            bot.event_store.set_occurrence_needs_refresh(
                occurrence.occurrence_id,
                True,
            )
        return occurrence.status
    if occurrence.needs_refresh:
        # Every part of the refresh has now succeeded; clear the flag.
        bot.event_store.set_occurrence_needs_refresh(
            occurrence.occurrence_id,
            False,
        )
    return status


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


async def repost_occurrence(
    bot: Gw2Bot,
    event: Event,
    occurrence: EventOccurrence,
    deferred_update: RosterUpdate = RosterUpdate(),
) -> EventOccurrence:
    # Discord cannot move a message between channels, so a channel change is
    # applied by sending a fresh post and removing the old one. The occurrence
    # row (and therefore the roster) is preserved because signups are keyed by
    # occurrence_id, not by message.
    #
    # The old message is addressed through the channel the occurrence was posted
    # to, which is not necessarily the event's previous channel: a series can
    # have posts spread over several channels after more than one move.
    #
    # The new post is sent and persisted *before* the old message is deleted.
    # post_occurrence only writes the new message id once the message is live,
    # and it raises if the send or that write fails. Deleting first would drop
    # the only public post while occurrence.message_id still referenced it, and
    # because the caller has already committed the new channel_id, the next
    # refresh would look for that dead id in the new channel, get NotFound and
    # retire a still-active occurrence. Posting first means a failed move leaves
    # the old message live and still correctly referenced.
    old_message_id = occurrence.message_id
    old_thread_id = occurrence.thread_id
    old_channel_id = occurrence_channel_id(event, occurrence)
    # Read before the re-post, which claims these columns for the new post's
    # announcement: the old announcement links to the message deleted below,
    # so leaving it would point members at nothing and stand beside the new
    # one.
    old_ping_channel_id = occurrence.ping_channel_id
    old_ping_message_id = occurrence.ping_message_id
    reposted = await post_occurrence(bot, event, occurrence)
    if old_ping_channel_id is not None and old_ping_message_id is not None:
        removed = await drop_announcement(
            bot,
            old_ping_channel_id,
            old_ping_message_id,
            occurrence.occurrence_id,
        )
        if not removed:
            # The columns that held this pair now describe the replacement, so
            # a refusal has to be parked somewhere or the announcement outlives
            # the message it links to with nothing left to find it. The
            # maintenance pass retries it until it is gone.
            _keep_stale_announcement(
                bot,
                occurrence.occurrence_id,
                old_ping_channel_id,
                old_ping_message_id,
            )
    if old_message_id is not None:
        try:
            old_channel = await resolve_channel(bot, old_channel_id)
            await old_channel.get_partial_message(old_message_id).delete()
        except discord.HTTPException as exc:
            # The old message is left orphaned but the move still proceeds,
            # because the new post is already live and persisted.
            LOGGER.error(
                "Could not delete old event message during channel move; "
                "occurrence_id=%s error_type=%s",
                occurrence.occurrence_id,
                type(exc).__name__,
            )
        # The old thread is deleted independently of the message above: it does
        # not disappear on its own, and a failed message delete must not also
        # strand the thread. A thread the event was only posted into is kept.
        await _delete_occurrence_thread(
            bot, old_channel_id, old_thread_id, occurrence.occurrence_id
        )
    # The roster carries over to the new post untouched and is subscribed to
    # the new thread below, so anyone who has left is taken off it first.
    # After the move rather than before it: the check removes through
    # remove_signup, whose refresh would edit the *old* message - and a
    # message someone had already deleted answers that with NotFound, which
    # retires this still-live occurrence and seeds a successor the series does
    # not want yet. Once the row names the new post there is a message to
    # refresh, and the roster settles before anyone is subscribed to it.
    #
    # Forced: a move begins at /event edit, whose preview checks this very
    # roster, so the answer here would otherwise be the one from before the
    # commander opened the channel picker - and whoever left while that
    # confirmation sat open would be carried across anyway. The freshness the
    # window buys is there to bound bursts of roster changes; a move happens
    # once.
    departed, checked = await check_roster_membership(
        bot,
        event,
        reposted,
        force=True,
        notify=False,
    )
    signups = bot.event_store.get_signups(reposted.occurrence_id)
    for signup in signups:
        await update_thread_membership(
            bot,
            reposted,
            signup.discord_user_id,
            add=True,
        )
    # A move deletes the thread the caller would have announced in, so it
    # hands its announcement over instead - a category change's re-seat, say.
    # Both describe the same roster settling, and the check ran after the
    # caller computed its half, so they are folded into one line per member:
    # a member moved twice reads as one move, and one who has left is dropped
    # rather than told about a seat they no longer hold.
    #
    # After the subscriptions above, because the mentions are only worth
    # sending to members who are in the thread to receive them.
    await notify_roster_update(
        bot,
        reposted,
        merge_roster_updates([deferred_update, checked], departed),
    )
    LOGGER.debug(
        "Reposted event occurrence to new channel; occurrence_id=%s "
        "signups=%s",
        reposted.occurrence_id,
        len(signups),
    )
    return reposted


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


async def delete_event_posts(
    bot: Gw2Bot,
    event: Event,
    occurrences: list[EventOccurrence],
) -> int:
    # Best-effort cleanup of the public posts (and their threads) when an event
    # is deleted. Discord does not delete a thread when its starter message is
    # removed, so each occurrence's thread is deleted separately below. This
    # runs after the store rows are gone, so any message that survives a
    # failure here just has buttons that gracefully report the event is no
    # longer available.
    #
    # Each occurrence is deleted through the channel it was posted to, not the
    # event's current one. A channel edit only re-posts the live occurrences, so
    # a series that has been moved has finished posts sitting in the previous
    # channel; addressing those through the current channel returns NotFound and
    # would leave them visible forever after the rows are gone.
    channels: dict[int, Any] = {}
    unresolvable: set[int] = set()
    deleted = 0
    announcements = 0
    for occurrence in occurrences:
        # Done before the message below, and independently of it: an
        # announcement lives in its own channel, so a post whose channel has
        # gone must not also strand the announcement pointing at it.
        if (
            occurrence.ping_channel_id is not None
            and occurrence.ping_message_id is not None
        ):
            ping_channel = await _resolve_cached_channel(
                bot,
                channels,
                unresolvable,
                occurrence.ping_channel_id,
                event.event_id,
            )
            if ping_channel is not None:
                removed = await delete_occurrence_announcement(
                    ping_channel,
                    occurrence.ping_message_id,
                    occurrence.occurrence_id,
                )
                announcements += int(removed)
        for stale_channel_id, stale_message_id in (
            occurrence.stale_ping_messages
        ):
            # The last chance to clear the removals an earlier move could not
            # finish: the row is about to go, taking the notes with it.
            stale_channel = await _resolve_cached_channel(
                bot,
                channels,
                unresolvable,
                stale_channel_id,
                event.event_id,
            )
            if stale_channel is not None:
                announcements += int(
                    await delete_occurrence_announcement(
                        stale_channel,
                        stale_message_id,
                        occurrence.occurrence_id,
                    )
                )
        if occurrence.message_id is None:
            continue
        channel_id = occurrence_channel_id(event, occurrence)
        channel = await _resolve_cached_channel(
            bot,
            channels,
            unresolvable,
            channel_id,
            event.event_id,
        )
        if channel is None:
            continue
        try:
            await channel.get_partial_message(occurrence.message_id).delete()
            deleted += 1
        except discord.HTTPException as exc:
            LOGGER.error(
                "Could not delete event message during deletion; "
                "occurrence_id=%s error_type=%s",
                occurrence.occurrence_id,
                type(exc).__name__,
            )
        # Deleted independently of the message above: a failed message delete
        # must not also strand the thread. A thread the event was only posted
        # into is kept; only the event's own message is removed from it.
        await _delete_occurrence_thread(
            bot, channel_id, occurrence.thread_id, occurrence.occurrence_id
        )
    LOGGER.debug(
        "Deleted event posts; event_id=%s messages_deleted=%s "
        "announcements_deleted=%s",
        event.event_id,
        deleted,
        announcements,
    )
    return deleted


# Two paths post an occurrence that has no message yet: the maintenance pass,
# and a cancellation posting the successor it just seeded. Each holds this
# occurrence's lock while it sends, so the second to arrive re-reads the row
# inside the lock and finds the message the first one stored instead of sending
# the same run twice and orphaning one of the posts. The bot is a single
# process, so process-local locks cover every poster there is.
#
# Keyed by occurrence rather than shared, because a send can sit in a Discord
# rate limit for seconds and posting one event's run must not hold up anyone
# else's. Entries live only while someone holds or waits on them.
_POSTING_LOCKS: dict[int, asyncio.Lock] = {}
_POSTING_LOCK_HOLDERS: dict[int, int] = {}


@asynccontextmanager
async def _posting_lock(occurrence_id: int) -> AsyncIterator[None]:
    lock = _POSTING_LOCKS.setdefault(occurrence_id, asyncio.Lock())
    _POSTING_LOCK_HOLDERS[occurrence_id] = (
        _POSTING_LOCK_HOLDERS.get(occurrence_id, 0) + 1
    )
    try:
        async with lock:
            yield
    finally:
        remaining = _POSTING_LOCK_HOLDERS[occurrence_id] - 1
        if remaining:
            _POSTING_LOCK_HOLDERS[occurrence_id] = remaining
        else:
            # Last one out drops the entry, so the table tracks what is in
            # flight rather than every occurrence ever posted.
            del _POSTING_LOCK_HOLDERS[occurrence_id]
            del _POSTING_LOCKS[occurrence_id]


async def post_pending_occurrence(
    bot: Gw2Bot,
    event: Event,
    occurrence: EventOccurrence,
    now: datetime | None = None,
) -> EventOccurrence | None:
    """Post an occurrence that has no message yet, at most once.

    Returns the posted occurrence, or None when it turned out to be posted
    already (or gone). Members on its roster are subscribed to the post, which
    is how a successor's auto-signups reach the thread they were seeded into.
    """
    async with _posting_lock(occurrence.occurrence_id):
        current = bot.event_store.get_occurrence(occurrence.occurrence_id)
        if current is None or current.message_id is not None:
            LOGGER.debug(
                "Skipping a pending occurrence that is no longer pending; "
                "occurrence_id=%s exists=%s",
                occurrence.occurrence_id,
                current is not None,
            )
            return None
        posted = await post_occurrence(bot, event, current, now)
        if current.needs_refresh:
            # The flag only asked for this posting, and the message it produced
            # is current, so clear it rather than leave every later maintenance
            # pass re-rendering a post nothing has changed.
            try:
                bot.event_store.set_occurrence_needs_refresh(
                    posted.occurrence_id,
                    False,
                )
            except SQLAlchemyError as exc:
                # The post is live and its message id is stored; this was
                # housekeeping. Failing the call over it would tell the caller
                # a delivered post never went out - and a cancellation would
                # then offer to put the series back, which means calling off
                # the run that is sitting in the channel. A flag left set only
                # costs one redundant refresh on the next pass, which clears
                # it.
                LOGGER.error(
                    "Could not clear the refresh flag after posting; "
                    "occurrence_id=%s error_type=%s",
                    posted.occurrence_id,
                    type(exc).__name__,
                )
        # This roster was seeded from the automatic sign-ups of the run before
        # it and nothing has looked at it since, so a member who left the
        # server in the meantime is on it: holding a seat in a run they cannot
        # see, and seeded again next week. Take them off now, before the
        # roster is subscribed to the post below; the removal refreshes the
        # embed that has just gone out.
        #
        # After the post rather than before it. The refresh flag is a
        # cancellation's claim on this run, the check removes through
        # remove_signup, and that removal's own refresh clears the flag
        # because an unposted occurrence has no message to refresh. A post
        # that then failed would leave the series with nothing posted, an
        # unclaimed pending row that maintenance skips forever, and a
        # cancellation reporting a retry that is not coming.
        #
        # Forced for the same reason a move is: posting an occurrence happens
        # once, so there is no burst for the window to bound, and a roster
        # nobody has looked at since it was seeded is exactly the one worth
        # asking about.
        #
        # Quietly: whatever it moves is announced after the roster below is
        # subscribed to the post's thread, because a mention only reaches a
        # member who is in the thread to receive it - and this thread was
        # opened moments ago with nobody in it.
        _, checked = await check_roster_membership(
            bot,
            event,
            posted,
            now=now,
            force=True,
            notify=False,
        )
    try:
        signups = bot.event_store.get_signups(posted.occurrence_id)
    except SQLAlchemyError as exc:
        # Delivery is done and recorded; subscribing the roster is what
        # follows it. Failing the call over this would report a post that is
        # in the channel as never sent, and a cancellation would then promise
        # a retry that cannot happen - the scheduler only posts rows with no
        # message. The members keep their seats and miss only the thread
        # subscription, which signing up again would give them.
        LOGGER.error(
            "Could not read the roster to subscribe it after posting; "
            "occurrence_id=%s error_type=%s",
            posted.occurrence_id,
            type(exc).__name__,
        )
        return posted
    for signup in signups:
        await update_thread_membership(
            bot,
            posted,
            signup.discord_user_id,
            add=True,
        )
    await notify_roster_update(bot, posted, checked)
    return posted


@dataclass(frozen=True, slots=True)
class OccurrenceCancellation:
    """What cancelling one occurrence of a repeating series left behind.

    ``successor`` is the occurrence the series continues with, and
    ``successor_posted`` says whether its public message is live. A successor
    that could not be posted is reported rather than raised: the cancellation
    itself is already committed by then, so the caller has to tell the
    commander what is (and is not) in the channel.
    """

    successor: EventOccurrence | None
    successor_posted: bool
    # Whether an unposted successor is queued for another attempt. False only
    # when the retry marker could not be written either, which leaves the
    # series with nothing in the channel and nothing coming to fix it.
    retry_pending: bool = True


async def cancel_occurrence(
    bot: Gw2Bot,
    event: Event,
    occurrence: EventOccurrence,
    now: datetime | None = None,
) -> OccurrenceCancellation:
    """Cancel one occurrence of a repeating series, keeping the series alive.

    The occurrence's roster, store rows and public post are removed, and the
    series carries on from the next one. Store failures propagate before
    anything is removed, so a cancellation that raises has changed nothing.
    """
    current_time = now if now is not None else datetime.now(UTC)
    # Seed the successor before the cancelled row goes: its start is computed
    # from the occurrence being cancelled, and has_later_occurrence can only
    # tell that the series still needs one while that row is there. It is a
    # no-op when a later occurrence already exists, which is the case when the
    # occurrence being cancelled is one the scheduler has already moved past.
    seeded = ensure_next_recurring_occurrence(
        bot, event, occurrence, current_time
    )
    # Claim the run that takes over before anything is destroyed. Everything
    # from the delete onwards is what would otherwise have posted it, and the
    # bot can stop there for any reason - a restart, a database error, a
    # Discord failure. The series would then have no posted occurrence and an
    # unclaimed pending one, which is exactly the state the maintenance pass
    # leaves alone, so it would quietly never come back. Posting clears the
    # claim again.
    successor = leading_occurrence(
        bot,
        event,
        current_time,
        excluding=occurrence.occurrence_id,
    )
    claimed = successor is not None and successor.message_id is None
    try:
        if claimed and successor is not None:
            # Deliberately strict, unlike the re-claim after a failed post:
            # nothing has been destroyed yet, and a claim that will not write
            # means the store is unhealthy right now. Going on to delete rows
            # against it risks the one state nothing recovers from - a series
            # with no posted run and an unclaimed pending one. Refusing costs
            # a retry.
            _claim_cancellation_successor(bot, successor)
        bot.event_store.delete_occurrence(occurrence.occurrence_id)
    except SQLAlchemyError:
        # The successor is committed in its own transaction, so it outlives a
        # claim or a delete that fails. Leaving it behind would let the next
        # maintenance pass post the following run while the one that failed to
        # cancel is still live, so take it back out before reporting the
        # failure. A successor that was already there keeps its row and gives
        # back the claim, which would otherwise invite that same premature
        # post.
        if seeded is not None:
            _discard_occurrence(
                bot,
                seeded,
                "the successor of a failed cancellation",
            )
        elif claimed and successor is not None:
            _release_cancellation_claim(bot, successor)
        raise
    await delete_event_posts(bot, event, [occurrence])
    try:
        successor = leading_occurrence(bot, event, current_time)
    except SQLAlchemyError as exc:
        # The cancellation is done - the row, its roster and its post are all
        # gone - so this read failing is not a cancellation failure. The
        # successor is claimed, so the maintenance pass will post it; report
        # that rather than a failure the commander would retry.
        LOGGER.error(
            "Could not read the series after cancelling; event_id=%s "
            "error_type=%s",
            event.event_id,
            type(exc).__name__,
        )
        return OccurrenceCancellation(
            successor=successor,
            successor_posted=False,
            retry_pending=claimed,
        )
    LOGGER.debug(
        "Cancelled event occurrence; event_id=%s occurrence_id=%s "
        "successor_id=%s",
        event.event_id,
        occurrence.occurrence_id,
        successor.occurrence_id if successor is not None else None,
    )
    if successor is None:
        return OccurrenceCancellation(successor=None, successor_posted=False)
    if successor.message_id is not None:
        return OccurrenceCancellation(
            successor=successor,
            successor_posted=True,
        )
    # The successor is posted here rather than left to the scheduler, which
    # only posts a pending occurrence for a series that already has a posted
    # one. Cancelling the only posted occurrence of a series leaves none, so
    # waiting for the scheduler would strand the series unposted forever.
    try:
        posted = await post_pending_occurrence(
            bot, event, successor, current_time
        )
    except ValueError:
        # The successor's row went away while its message was in flight,
        # because another cancellation or a delete reached it first.
        # post_occurrence has already removed the message it sent, and this
        # cancellation is itself done, so this is not a failure to retry: fall
        # through and report the series as it now stands.
        LOGGER.debug(
            "A cancelled occurrence's successor was removed mid-post; "
            "event_id=%s occurrence_id=%s",
            event.event_id,
            successor.occurrence_id,
        )
        posted = None
    except (discord.HTTPException, SQLAlchemyError, RuntimeError) as exc:
        LOGGER.error(
            "Could not post the successor of a cancelled occurrence; "
            "event_id=%s occurrence_id=%s error_type=%s",
            event.event_id,
            successor.occurrence_id,
            type(exc).__name__,
        )
        return OccurrenceCancellation(
            successor=successor,
            successor_posted=False,
            retry_pending=_mark_cancellation_successor_pending(
                bot,
                successor,
            ),
        )
    if posted is None:
        # Declining has two causes: a maintenance pass posted this occurrence
        # while the cancelled run's post was being cleared, or the row is gone
        # because another cancellation (or a delete) reached it first. Report
        # what the series actually has now rather than assuming the first -
        # announcing a run that is not there would send the commander off to
        # rebuild an event that is still going.
        current = leading_occurrence(bot, event, current_time)
        LOGGER.debug(
            "Cancelled occurrence's successor was settled elsewhere; "
            "event_id=%s successor_id=%s leading_id=%s",
            event.event_id,
            successor.occurrence_id,
            current.occurrence_id if current is not None else None,
        )
        return OccurrenceCancellation(
            successor=current,
            successor_posted=(
                current is not None and current.message_id is not None
            ),
        )
    return OccurrenceCancellation(successor=posted, successor_posted=True)


def leading_occurrence(
    bot: Gw2Bot,
    event: Event,
    now: datetime,
    *,
    excluding: int | None = None,
) -> EventOccurrence | None:
    """The run a series is on now: its earliest occurrence still to happen.

    Judged on the clock as well as on the stored status, the way `/event
    cancel` picks its target. The status alone lags: it only moves when a
    maintenance pass persists it, so a run that ended while its refresh was
    failing still reads as live, and taking one of those for the series'
    next run would report a date that has already passed.
    """
    duration = timedelta(minutes=event.duration_minutes)
    return next(
        (
            candidate
            for candidate in bot.event_store.get_event_occurrences(
                event.event_id
            )
            if candidate.occurrence_id != excluding
            and candidate.status is not EventStatus.OVER
            and candidate.start_time + duration > now
        ),
        None,
    )


def _claim_cancellation_successor(
    bot: Gw2Bot,
    successor: EventOccurrence,
) -> None:
    # Raises rather than reporting: see the caller. Already-claimed rows need
    # no write, which is what makes this safe to call again later.
    if successor.needs_refresh:
        return
    bot.event_store.set_occurrence_needs_refresh(
        successor.occurrence_id,
        True,
    )
    LOGGER.debug(
        "Claimed a cancelled occurrence's successor for posting; "
        "occurrence_id=%s",
        successor.occurrence_id,
    )


def _release_cancellation_claim(
    bot: Gw2Bot,
    successor: EventOccurrence,
) -> None:
    # The cancellation did not happen after all, so the run it claimed is a
    # future one again and must not be posted ahead of the run that is still
    # live. Best effort: the failure that brought us here is already being
    # reported.
    try:
        bot.event_store.set_occurrence_needs_refresh(
            successor.occurrence_id,
            False,
        )
    except SQLAlchemyError as exc:
        LOGGER.error(
            "Could not release a cancellation claim; occurrence_id=%s "
            "error_type=%s",
            successor.occurrence_id,
            type(exc).__name__,
        )


def _discard_occurrence(
    bot: Gw2Bot,
    occurrence: EventOccurrence,
    reason: str,
) -> None:
    # Take back an occurrence row whose creation could not be completed. Best
    # effort: a failure here is logged rather than raised, because it is always
    # reported alongside the original failure that made the row unwanted.
    try:
        bot.event_store.delete_occurrence(occurrence.occurrence_id)
    except SQLAlchemyError as exc:
        LOGGER.error(
            "Could not discard %s; occurrence_id=%s error_type=%s",
            reason,
            occurrence.occurrence_id,
            type(exc).__name__,
        )
        return
    LOGGER.debug(
        "Discarded %s; occurrence_id=%s",
        reason,
        occurrence.occurrence_id,
    )


def _mark_cancellation_successor_pending(
    bot: Gw2Bot,
    successor: EventOccurrence,
) -> bool:
    # Claim the successor for the bot to post. The scheduler skips a pending
    # occurrence whose series has no posted one, because that normally means a
    # manual post is still in flight; the refresh flag is what tells it this
    # one is its own to send instead. A cancellation removes the series' last
    # post, so without the flag anything that stops this call short - a
    # Discord failure, a restart - would hide the series for good.
    #
    # Set once before the Discord work and again if that work fails, so the
    # second call is normally a no-op. Posting clears it.
    if successor.needs_refresh:
        return True
    try:
        bot.event_store.set_occurrence_needs_refresh(
            successor.occurrence_id,
            True,
        )
    except SQLAlchemyError as exc:
        # Without the flag nothing will post this run: the series has no posted
        # occurrence left, which is exactly what makes the scheduler leave a
        # pending one alone. Report it so the commander is told the series
        # needs a hand rather than promised a retry that is not coming.
        LOGGER.error(
            "Could not flag a cancelled occurrence's successor for posting; "
            "occurrence_id=%s error_type=%s",
            successor.occurrence_id,
            type(exc).__name__,
        )
        return False
    return True


async def prune_superseded_occurrences(bot: Gw2Bot, event: Event) -> int:
    # For a recurring event with delete_previous_on_repeat, remove the
    # occurrences the current post supersedes (their message, thread and store
    # rows) so the channel keeps only the current post. Only finished (OVER)
    # occurrences earlier than it qualify, so a live occurrence is never removed.
    # Message deletes are best-effort; the store rows are always removed so the
    # series does not accumulate history.
    #
    # The current post is derived here rather than passed in, because the two
    # conditions this waits on can land in either order: the next occurrence
    # being posted, and the previous one being persisted as OVER.
    # refresh_occurrence_message withholds the OVER commit until the message edit
    # and the thread rename have both succeeded, so a transient Discord failure
    # can leave the previous occurrence still non-OVER at the moment the next one
    # is posted. Deriving the state makes this idempotent, so whichever of the two
    # lands last can run the cleanup.
    if (
        event.repeat_frequency is RepeatFrequency.NONE
        or not event.delete_previous_on_repeat
    ):
        return 0
    occurrences = bot.event_store.get_event_occurrences(event.event_id)
    posted = [
        occurrence
        for occurrence in occurrences
        if occurrence.message_id is not None
    ]
    if not posted:
        return 0
    # Only a posted occurrence can supersede the previous one: removing the old
    # post before the next is live would leave the channel with no post at all.
    current = max(posted, key=lambda occurrence: occurrence.start_time)
    superseded = [
        occurrence
        for occurrence in occurrences
        if occurrence.occurrence_id != current.occurrence_id
        and occurrence.status is EventStatus.OVER
        and occurrence.start_time < current.start_time
    ]
    if not superseded:
        return 0
    await delete_event_posts(bot, event, superseded)
    deleted = 0
    for occurrence in superseded:
        try:
            bot.event_store.delete_occurrence(occurrence.occurrence_id)
            deleted += 1
        except SQLAlchemyError as exc:
            LOGGER.error(
                "Could not delete superseded occurrence row; "
                "occurrence_id=%s error_type=%s",
                occurrence.occurrence_id,
                type(exc).__name__,
            )
    LOGGER.debug(
        "Deleted superseded occurrences; event_id=%s count=%s "
        "current_occurrence_id=%s",
        event.event_id,
        deleted,
        current.occurrence_id,
    )
    return deleted


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


async def complete_signup(
    bot: Gw2Bot,
    event: Event,
    occurrence: EventOccurrence,
    discord_user_id: int,
    role: EventRole | None,
    flex_roles: tuple[EventRole, ...],
    now: datetime | None = None,
) -> EventSignup:
    """Seat one member who signed themselves up, announcing what it moved."""
    signup, _ = await seat_signup(
        bot,
        event,
        occurrence,
        discord_user_id,
        role,
        flex_roles,
        now,
    )
    return signup


async def seat_signup(
    bot: Gw2Bot,
    event: Event,
    occurrence: EventOccurrence,
    discord_user_id: int,
    role: EventRole | None,
    flex_roles: tuple[EventRole, ...],
    now: datetime | None = None,
    *,
    notify: bool = True,
) -> tuple[EventSignup, RosterUpdate]:
    """Put one member on the roster and report what seating them moved.

    A leader adding several members at once wants one announcement rather than
    one per member, so it passes notify=False and merges the updates itself;
    everything else takes the announcement here. Mirrors remove_signup.
    """
    signups = bot.event_store.get_signups(occurrence.occurrence_id)
    # The role/flex/remember views can linger until their timeout, so the
    # occurrence may have ended between opening the flow and this click.
    # Refuse to mutate a historical roster (which would also update thread
    # membership and refresh the past message).
    if occurrence_status(event, occurrence, signups, now) is EventStatus.OVER:
        raise ValueError(
            "This event has already ended, so you can no longer sign up."
        )
    # Seat this member against a roster the server still recognises. A member
    # who has left holds a seat nobody can fill, and admitting around one
    # would send this member to the waitlist over a place that is not really
    # taken, so the departed go first and the seating below is solved without
    # them.
    event, occurrence, signups, checked = await _checked_roster(
        bot,
        event,
        occurrence,
        now,
        "This event has already ended, so you can no longer sign up.",
    )
    assigned_role: EventRole | None = None
    waitlisted: bool
    update = RosterUpdate()
    if event.capacity.has_roles:
        if role is None:
            # The check moved the roster before this call refused to seat
            # anybody, and a call that raises hands its caller no update to
            # fold, whatever notify says. Announce it here or nowhere.
            await notify_roster_update(bot, occurrence, checked)
            raise ValueError("This event requires picking a role.")
        # The newcomer is appended after the seated members rather than
        # re-sorted: their signup time is "now", so they carry the lowest
        # seating priority. Seated members keep their full acceptable sets,
        # so admission may flex them to another of their roles but can never
        # unseat them; when even that cannot fit the newcomer, they are
        # waitlisted.
        candidates = seated_candidates(signups)
        candidates.append(
            RosterCandidate(
                discord_user_id=discord_user_id,
                preferences=preferred_role_order(role, flex_roles),
            )
        )
        solution = solve_roster(event.capacity, candidates)
        waitlisted = solution is None
        if solution is not None:
            assigned_role = solution[discord_user_id]
            assignments, changes = _seated_reassignments(signups, solution)
            # Persist the reshuffle before the new row, and keep both writes
            # synchronous and adjacent: no concurrent interaction can observe
            # the half-applied roster, and a crash between the two commits
            # leaves a roster that still respects every cap (the movers
            # vacated the contested seats before the newcomer exists) and is
            # re-canonicalised by the next mutation.
            bot.event_store.apply_roster_assignments(
                occurrence.occurrence_id,
                assignments,
            )
            update = RosterUpdate(reassigned=tuple(changes))
    else:
        waitlisted = is_roster_full(event.capacity, signups)
    LOGGER.debug(
        "Resolved signup seating; occurrence_id=%s user_id=%s "
        "waitlisted=%s assigned_role=%s reassigned=%s",
        occurrence.occurrence_id,
        discord_user_id,
        waitlisted,
        assigned_role.value if assigned_role is not None else None,
        len(update.reassigned),
    )
    signup = bot.event_store.add_signup(
        occurrence_id=occurrence.occurrence_id,
        discord_user_id=discord_user_id,
        role=role,
        assigned_role=assigned_role,
        flex_roles=flex_roles,
        waitlisted=waitlisted,
    )
    # The check moved the roster before this seating did, and a member it
    # promoted can be one the seating then flexes. Folded, they read as one
    # move apiece.
    update = merge_roster_updates([checked, update])
    await update_thread_membership(
        bot,
        occurrence,
        discord_user_id,
        add=True,
    )
    if notify:
        await notify_roster_update(bot, occurrence, update)
    await refresh_occurrence_message(bot, event, occurrence)
    return signup, update


async def remove_signup(
    bot: Gw2Bot,
    event: Event,
    occurrence: EventOccurrence,
    discord_user_id: int,
    *,
    notify: bool = True,
) -> tuple[EventSignup | None, RosterUpdate]:
    # Before the seat is freed, not after: the resettle below hands it to the
    # waitlist, and a waitlisted member who has left the server would be
    # promoted into a run they cannot see. Checking first takes them out of
    # the queue, so the seat goes to somebody who can actually use it. The
    # check itself removes through this function, and re-enters it while its
    # own removals are in flight; that re-entry is refused there rather than
    # here, so this stays the one door onto the roster.
    #
    # Its movements are announced with this removal rather than ahead of it:
    # the member being removed can be one the check has just promoted into a
    # seat a departure freed, and telling the thread they moved up moments
    # before taking them off it says two contradictory things.
    _, checked = await check_roster_membership(
        bot,
        event,
        occurrence,
        notify=False,
    )
    # That awaited Discord, and the run can cross its end - or be retired by
    # the check's own refresh - while the lookups are in flight. Read the row
    # back and leave a roster that is now history alone: the resettle below
    # hands the freed seat to the waitlist, so going on would promote somebody
    # into a run that has already finished. prune_departed_signups stops on
    # the same test rather than removing from a finished roster.
    current = bot.event_store.get_occurrence(occurrence.occurrence_id)
    if current is None or occurrence_finished(event, current):
        LOGGER.debug(
            "Skipped a removal from a roster that is history; "
            "occurrence_id=%s user_id=%s exists=%s",
            occurrence.occurrence_id,
            discord_user_id,
            current is not None,
        )
        if notify:
            await notify_roster_update(bot, occurrence, checked)
        return None, checked
    occurrence = current
    removed = bot.event_store.remove_signup(
        occurrence.occurrence_id,
        discord_user_id,
    )
    if removed is None:
        # This member was not on the roster, but the check may still have
        # moved it, and that movement is real whatever this call does next.
        if notify:
            await notify_roster_update(bot, occurrence, checked)
        return None, checked
    # Resettle the roster into the freed capacity before yielding to any
    # awaited Discord I/O. The removal and the resettle are synchronous store
    # writes, so keeping them adjacent makes the mutation atomic: a concurrent
    # complete_signup cannot observe the freed slot and claim it ahead of the
    # existing waitlist while we await the thread update below. A waitlisted
    # departure frees nothing, so the roster is left untouched.
    update = RosterUpdate()
    if not removed.waitlisted:
        update = _resettle_roster(bot, event, occurrence)
    # The check moved the roster first and this removal moved it after, so
    # they fold into one line per member with this member dropped: whatever
    # seat the check gave them, they are off the roster now.
    update = merge_roster_updates([checked, update], [discord_user_id])
    await update_thread_membership(
        bot,
        occurrence,
        discord_user_id,
        add=False,
    )
    if notify:
        await notify_roster_update(bot, occurrence, update)
    await refresh_occurrence_message(bot, event, occurrence)
    return removed, update


def departed_roster_members(
    signups: Sequence[EventSignup],
    memberships: Mapping[int, GuildMembership],
) -> list[int]:
    """Pick out the roster members Discord has confirmed have left.

    Only a definite "not a member" counts. A lookup that failed - a permission
    error, an outage, a member the bot was never told about - reports None, and
    treating that as a departure would drop half a roster the first time
    Discord is unreachable.
    """
    return [
        signup.discord_user_id
        for signup in signups
        if memberships.get(signup.discord_user_id, GuildMembership()).in_guild
        is False
    ]


def _roster_movement(
    before: Sequence[EventSignup],
    after: Sequence[EventSignup],
    removed_user_id: int,
) -> RosterUpdate:
    """What changed between two readings of one roster, minus who left.

    Used to recover the movement a removal made when the store failed after
    committing it: the resettle may have landed and the refresh behind it
    raised, so re-running the resettle finds nothing left to do and reports
    nothing, while the promotion is sitting in the rows. Comparing the two
    readings says what really happened whichever half failed.
    """
    was = {signup.discord_user_id: signup for signup in before}
    promoted: list[EventSignup] = []
    reassigned: list[RoleChange] = []
    for signup in after:
        previous = was.get(signup.discord_user_id)
        if previous is None or signup.discord_user_id == removed_user_id:
            continue
        if previous.waitlisted and not signup.waitlisted:
            promoted.append(signup)
        elif (
            not previous.waitlisted
            and previous.assigned_role is not None
            and signup.assigned_role is not None
            and previous.assigned_role is not signup.assigned_role
        ):
            reassigned.append(
                RoleChange(
                    discord_user_id=signup.discord_user_id,
                    old_role=previous.assigned_role,
                    new_role=signup.assigned_role,
                )
            )
    return RosterUpdate(
        reassigned=tuple(reassigned),
        promoted=tuple(promoted),
    )


async def prune_departed_signups(
    bot: Gw2Bot,
    event: Event,
    occurrence: EventOccurrence,
    memberships: Mapping[int, GuildMembership],
    now: datetime | None = None,
) -> tuple[list[int], RosterUpdate]:
    """Drop roster members who have left the Discord server.

    A member who leaves the server keeps their seat: the bot runs without the
    members intent, so it never hears about the departure, and the seat would
    hold a place nobody can fill for an event they cannot even see. Every
    roster edit re-checks the members it lists and clears those out, promoting
    the waitlist into the freed seats exactly as a leader's own removal would.

    Automatic sign-up is switched off for each departure as well, so a
    recurring series does not seat them again on its next occurrence.

    Returns the ids actually removed and the merged roster movement to report,
    which the caller announces or folds into its own announcement.
    """
    signups = bot.event_store.get_signups(occurrence.occurrence_id)
    if not signups:
        return [], RosterUpdate()
    if occurrence_status(event, occurrence, signups, now) is EventStatus.OVER:
        # A finished occurrence's roster is history. Removing from it would
        # promote someone into a run that is already over, and re-rendering the
        # message can persist OVER without seeding the series' next occurrence.
        LOGGER.debug(
            "Skipped pruning departed members from a finished occurrence; "
            "occurrence_id=%s",
            occurrence.occurrence_id,
        )
        return [], RosterUpdate()
    departed = departed_roster_members(signups, memberships)
    if not departed:
        LOGGER.debug(
            "No departed members on the roster; occurrence_id=%s roster=%s",
            occurrence.occurrence_id,
            len(signups),
        )
        return [], RosterUpdate()
    removed: list[int] = []
    updates: list[RosterUpdate] = []
    for index, user_id in enumerate(departed):
        # remove_signup awaits Discord I/O between members, so the event can
        # cross its end partway through a long roster even though the check
        # above passed. Re-read both the clock and the occurrence every
        # iteration - the row can be rescheduled or retired outright while the
        # loop runs - and stop the moment the run is over, so no removal (and
        # no waitlist promotion behind it) ever lands on a finished roster.
        # An explicit now pins the clock for callers that asked for one;
        # otherwise it really is the elapsing time that counts.
        current_time = now if now is not None else datetime.now(UTC)
        # Every store call that opens an iteration fails the same way and
        # stops the loop the same way: what the members ahead of this one
        # committed is reported, rather than thrown away by an exception
        # escaping to the check above.
        try:
            current = bot.event_store.get_occurrence(occurrence.occurrence_id)
        except SQLAlchemyError as exc:
            LOGGER.error(
                "Could not read the occurrence mid-prune; stopping; "
                "occurrence_id=%s kept=%s error_type=%s",
                occurrence.occurrence_id,
                len(departed) - index,
                type(exc).__name__,
            )
            break
        if current is None or current_time >= current.start_time + timedelta(
            minutes=event.duration_minutes
        ):
            LOGGER.debug(
                "Event ended mid-prune; stopping; occurrence_id=%s kept=%s "
                "exists=%s",
                occurrence.occurrence_id,
                len(departed) - index,
                current is not None,
            )
            break
        # One member failing to leave the roster must not strand the rest, and
        # remove_signup already absorbs its own Discord failures. A store
        # failure is different: it says the database is refusing writes right
        # now, so the removals still to come would fail too. Stop there and
        # report what did land - the rows are committed one removal at a
        # time, and a caller told nothing happened would announce a roster it
        # no longer has and seat members against seats that are already free.
        # Read before the removal so the recovery below can say what it did:
        # the store can fail with the seat already handed on, and what landed
        # is only visible by comparing the two readings. Guarded like the read
        # above, and for the same reason.
        try:
            before = bot.event_store.get_signups(current.occurrence_id)
        except SQLAlchemyError as exc:
            LOGGER.error(
                "Could not read the roster before removing a departed "
                "member; stopping the prune; occurrence_id=%s kept=%s "
                "error_type=%s",
                occurrence.occurrence_id,
                len(departed) - index,
                type(exc).__name__,
            )
            break
        try:
            signup, update = await remove_signup(
                bot,
                event,
                current,
                user_id,
                notify=False,
            )
        except SQLAlchemyError as exc:
            LOGGER.error(
                "Could not remove a departed member; stopping the prune; "
                "occurrence_id=%s kept=%s error_type=%s",
                occurrence.occurrence_id,
                len(departed) - index,
                type(exc).__name__,
            )
            # The deletion commits before the resettle and the message
            # refresh behind it, so this can fail with the member already off
            # the roster. Read the row back and report the departure if it
            # landed: a caller told it did not happen keeps them in whatever
            # it announces, naming a seat that is no longer theirs.
            try:
                still_on = (
                    bot.event_store.get_signup(
                        current.occurrence_id,
                        user_id,
                    )
                    is not None
                )
            except SQLAlchemyError:
                # The store cannot say either way, and claiming a departure
                # that did not happen is the worse mistake of the two.
                still_on = True
            if not still_on:
                removed.append(user_id)
                # The deletion landed; what follows it did not. Try that
                # again rather than leave the seat it freed unclaimed with a
                # waitlist behind it, and the member's automatic sign-up on
                # to seed them onto the next run. The store may still refuse,
                # which is logged and left: the resettle re-solves from what
                # is there, so the next roster change picks it up, and a
                # member seeded again is one the next post's check removes.
                try:
                    _resettle_roster(bot, event, current)
                    disable_auto_signup(bot, event, current, user_id)
                except SQLAlchemyError as cleanup_error:
                    LOGGER.error(
                        "Could not finish a departed member's removal; "
                        "occurrence_id=%s user_id=%s error_type=%s",
                        occurrence.occurrence_id,
                        user_id,
                        type(cleanup_error).__name__,
                    )
                # Whatever the roster did, whichever half failed: the resettle
                # above reports only what it moved itself, and it moves
                # nothing when the first one had already landed.
                try:
                    updates.append(
                        _roster_movement(
                            before,
                            bot.event_store.get_signups(
                                current.occurrence_id
                            ),
                            user_id,
                        )
                    )
                except SQLAlchemyError:
                    LOGGER.error(
                        "Could not read back a recovered removal's roster; "
                        "occurrence_id=%s user_id=%s",
                        occurrence.occurrence_id,
                        user_id,
                    )
            break
        if signup is None:
            continue
        removed.append(user_id)
        updates.append(update)
        try:
            # The fresh row again: disable_auto_signup withdraws the member
            # from occurrences later than this one, which is decided by
            # comparing start times.
            disable_auto_signup(bot, event, current, user_id)
        except SQLAlchemyError as exc:
            LOGGER.error(
                "Could not disable auto signup for a departed member; "
                "occurrence_id=%s user_id=%s error_type=%s",
                occurrence.occurrence_id,
                user_id,
                type(exc).__name__,
            )
    merged = merge_roster_updates(updates, removed)
    LOGGER.debug(
        "Pruned departed members from the roster; event_id=%s "
        "occurrence_id=%s roster=%s departed=%s removed=%s promoted=%s",
        event.event_id,
        occurrence.occurrence_id,
        len(signups),
        len(departed),
        len(removed),
        len(merged.promoted),
    )
    return removed, merged


# How long one membership check of an occurrence's roster stands. The bot runs
# without the members intent, so its member cache is empty and every member on
# the roster costs a Discord lookup; a burst of sign-ups on a fifty-seat roster
# would otherwise spend fifty of them per click. A commander who opened the
# roster asks for a fresh answer with force=True, and a roster the check has
# never seen - a newly seeded occurrence about to be posted - is never covered
# by an earlier one, so what this bounds is only how often the same live roster
# is re-asked about.
ROSTER_MEMBERSHIP_CHECK_SECONDS = 60.0


@dataclass(slots=True)
class _RosterMembershipChecks:
    """One bot's bookkeeping for the roster membership checks.

    ``checked_at`` is when each occurrence's roster was last asked about, and
    ``in_flight`` the occurrences a check is running over right now. Held per
    bot rather than per module because it describes that bot's conversation
    with Discord: a second bot (a test's, say) starts with its own empty state
    and cannot be answered from the first one's.
    """

    checked_at: dict[int, float] = field(default_factory=dict)
    in_flight: set[int] = field(default_factory=set)


_MEMBERSHIP_CHECKS: WeakKeyDictionary[Any, _RosterMembershipChecks] = (
    WeakKeyDictionary()
)


def _membership_checks(bot: Gw2Bot) -> _RosterMembershipChecks:
    checks = _MEMBERSHIP_CHECKS.get(bot)
    if checks is None:
        checks = _RosterMembershipChecks()
        _MEMBERSHIP_CHECKS[bot] = checks
    return checks


def _membership_check_due(
    checks: _RosterMembershipChecks,
    occurrence_id: int,
    moment: float,
) -> bool:
    # Expired entries are dropped as they are passed over, so the table holds
    # the rosters checked within the window rather than every occurrence this
    # process has ever posted.
    for checked_id, checked_at in list(checks.checked_at.items()):
        if moment - checked_at >= ROSTER_MEMBERSHIP_CHECK_SECONDS:
            del checks.checked_at[checked_id]
    return occurrence_id not in checks.checked_at


def _event_guild(bot: Gw2Bot) -> discord.Guild | None:
    """The server an event's roster is a roster of.

    Membership is a question about that one server, and the posting and
    scheduler paths have no interaction to read it off, so it comes from the
    configured command guild. The guild cache needs no members intent, so this
    is a local lookup rather than a Discord call.
    """
    return bot.get_guild(bot._config.discord_command_guild_id)


async def check_roster_membership(
    bot: Gw2Bot,
    event: Event,
    occurrence: EventOccurrence,
    *,
    guild: discord.Guild | None = None,
    memberships: Mapping[int, GuildMembership] | None = None,
    now: datetime | None = None,
    force: bool = False,
    notify: bool = True,
) -> tuple[list[int], RosterUpdate]:
    """Re-check a roster against Discord and take off everyone who has left.

    The bot is never told that a member left the server: it runs without the
    members intent, so nothing arrives to act on and a departed member keeps
    their seat. That seat holds a place nobody can fill for an event they
    cannot even see, blocks the waitlist behind it, and on a repeating event
    is handed straight back to them when their automatic sign-up seeds the
    next occurrence. So the roster is asked about again whenever it changes
    and before an occurrence is posted, rather than only when a commander
    opens it.

    Callers that have already looked the roster up - the pickers, which need
    the same lookups for their names - pass ``memberships`` and spend no
    further calls here.

    Returns the ids actually removed and the roster movement their removal
    caused, which has already been announced - unless the caller passed
    notify=False because it has an announcement of its own to fold this into.
    Nothing here is allowed to fail its caller: a sign-up, a removal or a post
    must land whether or not the check behind it could be made.
    """
    occurrence_id = occurrence.occurrence_id
    checks = _membership_checks(bot)
    if occurrence_id in checks.in_flight:
        # prune_departed_signups removes through remove_signup, which asks for
        # this check itself. Without this the removal would re-enter the check
        # that is making it, over a roster read before any of it landed, and
        # go round removing the same members again.
        LOGGER.debug(
            "Skipped a roster membership check already in flight; "
            "occurrence_id=%s",
            occurrence_id,
        )
        return [], RosterUpdate()
    moment = time.monotonic()
    if not force and not _membership_check_due(checks, occurrence_id, moment):
        LOGGER.debug(
            "Skipped a roster membership check made moments ago; "
            "occurrence_id=%s",
            occurrence_id,
        )
        return [], RosterUpdate()
    server = guild if guild is not None else _event_guild(bot)
    if memberships is None and server is None:
        # Only the server can say who is still in it. Without one there is no
        # answer to act on, and guessing would cost members their seats, so
        # the roster stands as it is until a check can be made.
        LOGGER.debug(
            "Skipped a roster membership check without a server to ask; "
            "occurrence_id=%s",
            occurrence_id,
        )
        return [], RosterUpdate()
    try:
        signups = bot.event_store.get_signups(occurrence_id)
    except SQLAlchemyError as exc:
        LOGGER.error(
            "Could not read the roster to check it against the server; "
            "occurrence_id=%s error_type=%s",
            occurrence_id,
            type(exc).__name__,
        )
        return [], RosterUpdate()
    if not signups:
        checks.checked_at[occurrence_id] = moment
        LOGGER.debug(
            "Checked an empty roster against the server; occurrence_id=%s",
            occurrence_id,
        )
        return [], RosterUpdate()
    checks.in_flight.add(occurrence_id)
    try:
        resolved = (
            memberships
            if memberships is not None
            else await resolve_guild_memberships(
                bot,
                server,
                [signup.discord_user_id for signup in signups],
            )
        )
        # Those lookups awaited Discord, and a commander can save the event
        # while they are in flight. The prune judges the run's end by the
        # event's duration and re-seats the roster it leaves behind against
        # the event's capacity, so it needs the event as it stands now:
        # _checked_roster reads it back for its own callers, but only after
        # this prune has already moved the roster.
        edited = bot.event_store.get_event(event.event_id)
        if edited is None or edited.cancelled:
            LOGGER.debug(
                "Skipped a roster prune for an event that is gone; "
                "occurrence_id=%s exists=%s",
                occurrence_id,
                edited is not None,
            )
            return [], RosterUpdate()
        departed, update = await prune_departed_signups(
            bot,
            edited,
            occurrence,
            resolved,
            now,
        )
        if departed and notify:
            await notify_roster_update(bot, occurrence, update)
    except (discord.DiscordException, SQLAlchemyError) as exc:
        # A roster the bot could not check is still a roster: report nothing
        # removed and let the sign-up, removal or post behind this carry on.
        LOGGER.error(
            "Could not check the roster against the server; occurrence_id=%s "
            "error_type=%s",
            occurrence_id,
            type(exc).__name__,
        )
        return [], RosterUpdate()
    finally:
        checks.in_flight.discard(occurrence_id)
        # Recorded whatever the outcome. A check that failed against an
        # unreachable Discord must not be retried by every click behind it,
        # which would spend the same failing lookups over and over.
        checks.checked_at[occurrence_id] = time.monotonic()
    LOGGER.debug(
        "Checked the roster against the server; event_id=%s occurrence_id=%s "
        "roster=%s departed=%s promoted=%s",
        event.event_id,
        occurrence_id,
        len(signups),
        len(departed),
        len(update.promoted),
    )
    return departed, update


def occurrence_finished(
    event: Event,
    occurrence: EventOccurrence,
    now: datetime | None = None,
) -> bool:
    """Whether an occurrence's roster is history, by the clock or the status.

    Both count, which is why this is not compute_status. The clock is the
    ordinary end; the stored status is how an occurrence retires early, when a
    message somebody deleted answers a refresh with NotFound and OVER is
    persisted (and the series' next run seeded) before this one's time is up.
    Deriving the status from the schedule alone reads such an occurrence as
    open, and a roster change would land on a run that has already been
    replaced.
    """
    if occurrence.status is EventStatus.OVER:
        return True
    current_time = now if now is not None else datetime.now(UTC)
    return current_time >= occurrence.start_time + timedelta(
        minutes=event.duration_minutes
    )


async def _checked_roster(
    bot: Gw2Bot,
    event: Event,
    occurrence: EventOccurrence,
    now: datetime | None,
    ended_message: str,
    *,
    force: bool = False,
) -> tuple[Event, EventOccurrence, list[EventSignup], RosterUpdate]:
    """Check the roster against the server, then read back what stands.

    The check awaits Discord, so nothing read before it holds afterwards.
    Members it removed are off the roster - and a prune that failed partway
    through still committed the removals it had made, which it cannot report -
    so the roster is read again whatever the check said rather than only when
    it named someone. The occurrence can also cross its end (or be deleted
    outright) while the lookups are in flight, which is what admitted the
    caller in the first place, so that decision is taken again too: seating
    somebody into a run that finished meanwhile would mutate a historical
    roster and refresh a post nobody is coming back to. The check can retire
    the occurrence itself - the removal it makes refreshes a message that may
    be gone - so the stored status is what is read back, not a status derived
    from the schedule.

    The event comes back too. A commander can save an edit while the lookups
    are in flight, and the roster is seated against whatever category the
    event carries now - admitting somebody under the capacity it had before
    would seat them into a squad shape that no longer exists, and a shortened
    duration can have ended the run outright.

    Whatever the check moved comes back rather than being announced here: the
    caller is about to move the same roster, and on a role-limited one it can
    move the very member the check just promoted. One announcement, folded,
    says one thing about each of them.
    """
    _, checked = await check_roster_membership(
        bot,
        event,
        occurrence,
        now=now,
        force=force,
        notify=False,
    )
    edited = bot.event_store.get_event(event.event_id)
    current = bot.event_store.get_occurrence(occurrence.occurrence_id)
    if edited is None or edited.cancelled or current is None:
        raise ValueError(ended_message)
    if occurrence_finished(edited, current, now):
        raise ValueError(ended_message)
    signups = bot.event_store.get_signups(current.occurrence_id)
    return edited, current, signups, checked


def rebalance_occurrence_roster(
    bot: Gw2Bot,
    event: Event,
    occurrence: EventOccurrence,
) -> tuple[int, RosterUpdate]:
    # Call this after an edit changes an event's category: the stored
    # assignments were seated against the old category's capacity and no longer
    # describe a valid roster. Returns how many signups actually moved and the
    # role changes to announce.
    signups = bot.event_store.get_signups(occurrence.occurrence_id)
    if not signups:
        return 0, RosterUpdate()
    reseated = rebalance_signups(event.capacity, signups)
    assignments: list[RosterAssignment] = []
    reassigned: list[RoleChange] = []
    promoted: list[EventSignup] = []
    for before, after in zip(signups, reseated, strict=True):
        if (
            before.role is after.role
            and before.flex_roles == after.flex_roles
            and before.assigned_role is after.assigned_role
            and before.waitlisted == after.waitlisted
        ):
            continue
        assignments.append(
            RosterAssignment(
                discord_user_id=after.discord_user_id,
                role=after.role,
                assigned_role=after.assigned_role,
                waitlisted=after.waitlisted,
                flex_roles=after.flex_roles,
            )
        )
        if before.waitlisted and not after.waitlisted:
            promoted.append(after)
        elif (
            not before.waitlisted
            and not after.waitlisted
            and before.assigned_role is not None
            and after.assigned_role is not None
        ):
            reassigned.append(
                RoleChange(
                    discord_user_id=after.discord_user_id,
                    old_role=before.assigned_role,
                    new_role=after.assigned_role,
                )
            )
    bot.event_store.apply_roster_assignments(
        occurrence.occurrence_id,
        assignments,
    )
    LOGGER.debug(
        "Rebalanced event roster for a new category; occurrence_id=%s "
        "category=%s signups=%s changed=%s",
        occurrence.occurrence_id,
        event.category.value,
        len(signups),
        len(assignments),
    )
    return len(assignments), RosterUpdate(
        reassigned=tuple(reassigned),
        promoted=tuple(promoted),
    )


def _seated_reassignments(
    signups: Sequence[EventSignup],
    solution: dict[int, EventRole],
) -> tuple[list[RosterAssignment], list[RoleChange]]:
    # Diff the solver's canonical assignment against the stored seated rows.
    # Rows the solution does not cover (the newcomer being admitted) and rows
    # it leaves in place produce no write.
    assignments: list[RosterAssignment] = []
    changes: list[RoleChange] = []
    for signup in signups:
        if signup.waitlisted:
            continue
        target = solution.get(signup.discord_user_id)
        if target is None or signup.assigned_role is target:
            continue
        assignments.append(
            RosterAssignment(
                discord_user_id=signup.discord_user_id,
                role=signup.role,
                assigned_role=target,
                waitlisted=False,
            )
        )
        if signup.assigned_role is not None:
            changes.append(
                RoleChange(
                    discord_user_id=signup.discord_user_id,
                    old_role=signup.assigned_role,
                    new_role=target,
                )
            )
    return assignments, changes


def _resettle_roster(
    bot: Gw2Bot,
    event: Event,
    occurrence: EventOccurrence,
) -> RosterUpdate:
    """Promote fitting waitlisted members and re-canonicalise assignments.

    Runs after a seated member departs. Fully synchronous - callers rely on
    the store read and every write landing without an intervening await. The
    waitlist is swept once in FCFS order and each candidate whose addition is
    feasible (counting seated flexers moving aside) is admitted; one pass is
    complete because admitting a member never makes another candidate newly
    feasible. The final solve then snaps every seated flexer back to the best
    role their seniority allows, so a member flexed away from their primary
    pick recovers it as soon as the roster permits.
    """
    signups = bot.event_store.get_signups(occurrence.occurrence_id)
    if not event.capacity.has_roles:
        assignments: list[RosterAssignment] = []
        promoted: list[EventSignup] = []
        active = sum(1 for signup in signups if not signup.waitlisted)
        # An uncapped category (General) has no seat to run out of, so every
        # waitlisted member is promoted.
        capacity_total = event.capacity.total
        for signup in signups:
            if not signup.waitlisted:
                continue
            if capacity_total is not None and active >= capacity_total:
                break
            assignments.append(
                RosterAssignment(
                    discord_user_id=signup.discord_user_id,
                    role=signup.role,
                    assigned_role=None,
                    waitlisted=False,
                )
            )
            promoted.append(replace(signup, waitlisted=False))
            active += 1
        bot.event_store.apply_roster_assignments(
            occurrence.occurrence_id,
            assignments,
        )
        LOGGER.debug(
            "Resettled role-less event roster; occurrence_id=%s promoted=%s",
            occurrence.occurrence_id,
            len(promoted),
        )
        return RosterUpdate(promoted=tuple(promoted))
    seated = [signup for signup in signups if not signup.waitlisted]
    waitlisted = sorted(
        (signup for signup in signups if signup.waitlisted),
        key=lambda signup: (signup.signed_up_at, signup.discord_user_id),
    )
    admitted_ids: set[int] = set()
    skipped = 0
    for candidate in waitlisted:
        if candidate.role is None:
            # A role-less signup cannot hold a seat in a role-based roster;
            # left waitlisted, exactly as the pre-solver promotion did.
            continue
        if can_admit(
            event.capacity,
            seated,
            candidate.role,
            candidate.flex_roles,
        ):
            seated.append(replace(candidate, waitlisted=False))
            admitted_ids.add(candidate.discord_user_id)
        else:
            skipped += 1
    solution = solve_roster(event.capacity, seated_candidates(seated))
    if solution is None:
        # Unreachable with well-formed data: the seated set was feasible when
        # each member was admitted. Never unseat anyone over corrupt state;
        # leave the stored roster untouched.
        LOGGER.error(
            "Roster resettle found seated members infeasible; leaving the "
            "stored roster untouched; occurrence_id=%s seated=%s",
            occurrence.occurrence_id,
            len(seated),
        )
        return RosterUpdate()
    assignments, changes = _seated_reassignments(
        [
            signup
            for signup in seated
            if signup.discord_user_id not in admitted_ids
        ],
        solution,
    )
    promoted_signups: list[EventSignup] = []
    for signup in seated:
        if signup.discord_user_id not in admitted_ids:
            continue
        promoted_signup = replace(
            signup,
            assigned_role=solution[signup.discord_user_id],
            waitlisted=False,
        )
        assignments.append(
            RosterAssignment(
                discord_user_id=signup.discord_user_id,
                role=signup.role,
                assigned_role=promoted_signup.assigned_role,
                waitlisted=False,
            )
        )
        promoted_signups.append(promoted_signup)
    bot.event_store.apply_roster_assignments(
        occurrence.occurrence_id,
        assignments,
    )
    LOGGER.debug(
        "Resettled event roster; occurrence_id=%s seated=%s "
        "waitlist_skipped=%s promoted=%s reassigned=%s",
        occurrence.occurrence_id,
        len(seated),
        skipped,
        len(promoted_signups),
        len(changes),
    )
    return RosterUpdate(
        reassigned=tuple(changes),
        promoted=tuple(promoted_signups),
    )


async def notify_roster_update(
    bot: Gw2Bot,
    occurrence: EventOccurrence,
    update: RosterUpdate,
) -> None:
    # A failure here must never fail the signup or removal that produced the
    # update: the roster is already persisted and the embed refresh that
    # follows does not depend on this message landing.
    contents = roster_update_messages(update)
    if not contents:
        return
    if occurrence.thread_id is None:
        LOGGER.debug(
            "Skipped roster update notification without a thread; "
            "occurrence_id=%s reassigned=%s promoted=%s",
            occurrence.occurrence_id,
            len(update.reassigned),
            len(update.promoted),
        )
        return
    # An event posted into a dormant forum post has to reopen it before the
    # announcement can land; a signup flow has usually done so already, and this
    # no-ops when the thread is open.
    await reopen_occurrence_thread(bot, occurrence)
    try:
        thread = await resolve_channel(bot, occurrence.thread_id)
    except discord.HTTPException as exc:
        LOGGER.error(
            "Could not resolve thread for roster update notification; "
            "occurrence_id=%s reassigned=%s promoted=%s error_type=%s",
            occurrence.occurrence_id,
            len(update.reassigned),
            len(update.promoted),
            type(exc).__name__,
        )
        return
    # A large update is split over several messages; one that fails is logged
    # and the rest are still attempted, so a single rejection does not cost
    # every other member their notification.
    sent = 0
    for part, content in enumerate(contents, start=1):
        try:
            await thread.send(content)
        except discord.HTTPException as exc:
            LOGGER.error(
                "Could not send roster update notification; occurrence_id=%s "
                "part=%s parts=%s reassigned=%s promoted=%s error_type=%s",
                occurrence.occurrence_id,
                part,
                len(contents),
                len(update.reassigned),
                len(update.promoted),
                type(exc).__name__,
            )
        else:
            sent += 1
    LOGGER.debug(
        "Sent roster update notification; occurrence_id=%s sent=%s parts=%s "
        "reassigned=%s promoted=%s",
        occurrence.occurrence_id,
        sent,
        len(contents),
        len(update.reassigned),
        len(update.promoted),
    )


def merge_roster_updates(
    updates: Sequence[RosterUpdate],
    removed_user_ids: Sequence[int] = (),
) -> RosterUpdate:
    """Fold sequential roster updates into one announcement.

    A leader removing several members produces one update per removal; the
    merged result reads as a single change. Per-user reassignments chain into
    first-old to last-new (dropped when they end where they started), a
    promotion followed by later reassignments folds into one promotion line
    at the final seat, and users removed later in the same batch are dropped
    entirely - they are off the roster, so reporting a move or promotion for
    them would be wrong.
    """
    removed = set(removed_user_ids)
    chains: dict[int, RoleChange] = {}
    promoted: dict[int, EventSignup] = {}
    for update in updates:
        for signup in update.promoted:
            promoted[signup.discord_user_id] = signup
        for change in update.reassigned:
            promoted_signup = promoted.get(change.discord_user_id)
            if promoted_signup is not None:
                promoted[change.discord_user_id] = replace(
                    promoted_signup,
                    assigned_role=change.new_role,
                )
                continue
            existing = chains.get(change.discord_user_id)
            chains[change.discord_user_id] = RoleChange(
                discord_user_id=change.discord_user_id,
                old_role=(
                    existing.old_role
                    if existing is not None
                    else change.old_role
                ),
                new_role=change.new_role,
            )
    return RosterUpdate(
        reassigned=tuple(
            change
            for change in chains.values()
            if change.discord_user_id not in removed
            and change.old_role is not change.new_role
        ),
        promoted=tuple(
            signup
            for user_id, signup in promoted.items()
            if user_id not in removed
        ),
    )


@dataclass(frozen=True, slots=True)
class SignupEditResult:
    # The edited row after the roster settled, or None when nothing was
    # applied because the member must first confirm losing their seat.
    signup: EventSignup | None
    update: RosterUpdate
    needs_waitlist_confirmation: bool = False


def _without_member(update: RosterUpdate, discord_user_id: int) -> RosterUpdate:
    return RosterUpdate(
        reassigned=tuple(
            change
            for change in update.reassigned
            if change.discord_user_id != discord_user_id
        ),
        promoted=tuple(
            signup
            for signup in update.promoted
            if signup.discord_user_id != discord_user_id
        ),
    )


async def apply_signup_edit(
    bot: Gw2Bot,
    event: Event,
    occurrence: EventOccurrence,
    discord_user_id: int,
    role: EventRole,
    flex_roles: tuple[EventRole, ...],
    *,
    allow_waitlist: bool = False,
    now: datetime | None = None,
) -> SignupEditResult:
    """Replace a member's declared roles without costing them their place.

    Unlike sign-out-and-rejoin, the signup row (and its signed_up_at, which
    decides seating priority) survives, so the member keeps their seat when
    the new roles still fit and keeps their queue position when they do not.
    A seated member whose new selection cannot fit alongside the other seated
    members is only moved to the waitlist after opting in via
    ``allow_waitlist`` - callers get ``needs_waitlist_confirmation`` back and
    nothing is mutated until the member confirms.
    """
    signups = bot.event_store.get_signups(occurrence.occurrence_id)
    # The edit views can linger like the signup views; refuse to mutate a
    # historical roster.
    if occurrence_status(event, occurrence, signups, now) is EventStatus.OVER:
        raise ValueError(
            "This event has already ended, so your signup can no longer be "
            "changed."
        )
    if not event.capacity.has_roles:
        raise ValueError("This event has no roles to edit.")
    # The new selection is judged against the members who are actually still
    # here, so an edit is not sent to the waitlist by a seat its holder left
    # the server on.
    event, occurrence, signups, checked = await _checked_roster(
        bot,
        event,
        occurrence,
        now,
        "This event has already ended, so your signup can no longer be "
        "changed.",
        # A confirmed edit is the member's second look at this roster: the
        # call that offered them the waitlist checked it moments ago, and
        # answering from that would drop them behind a seat whose holder left
        # while the confirmation sat open. Only EditWaitlistConfirmView gets
        # here, so this forces a confirmation rather than every edit.
        force=allow_waitlist,
    )
    current = next(
        (
            signup
            for signup in signups
            if signup.discord_user_id == discord_user_id
        ),
        None,
    )
    if current is None:
        # Nothing below this will announce what the check moved, and it did
        # move the roster: the departures are committed and so is whatever
        # they promoted. Say so before this call ends empty-handed.
        await notify_roster_update(bot, occurrence, checked)
        raise ValueError("You are not signed up for this event.")
    # Rate limit: a token bucket per signup (three edits, refilling one per
    # three hours) keeps a member from churning the roster and pinging the
    # thread over and over. Checked before anything mutates; consumed only
    # when an edit actually applies.
    current_time = now if now is not None else datetime.now(UTC)
    tokens = available_edit_tokens(current, current_time)
    if tokens < 1.0:
        LOGGER.debug(
            "Rejected signup edit over the rate limit; occurrence_id=%s "
            "user_id=%s tokens=%.2f",
            occurrence.occurrence_id,
            discord_user_id,
            tokens,
        )
        await notify_roster_update(bot, occurrence, checked)
        raise ValueError(signup_edit_limit_message(tokens))
    keeps_seat = current.waitlisted is False
    if keeps_seat:
        others = [
            candidate.preferences
            for candidate in seated_candidates(signups)
            if candidate.discord_user_id != discord_user_id
        ]
        keeps_seat = roster_feasible(
            event.capacity,
            [*others, preferred_role_order(role, flex_roles)],
        )
        if not keeps_seat and not allow_waitlist:
            LOGGER.debug(
                "Signup edit would waitlist the member; awaiting "
                "confirmation; occurrence_id=%s user_id=%s role=%s "
                "flex_count=%s",
                occurrence.occurrence_id,
                discord_user_id,
                role.value,
                len(flex_roles),
            )
            # The check ahead of this edit has already taken its departures
            # off the roster and moved whoever that promoted, and this exit
            # changes nothing further. The member may well cancel the
            # confirmation they are about to see, and a confirmed one re-runs
            # the check over a roster that is settled by then, so there is no
            # later announcement to fold this into: it is announced here.
            await notify_roster_update(bot, occurrence, checked)
            return SignupEditResult(
                signup=None,
                update=checked,
                needs_waitlist_confirmation=True,
            )
    bot.event_store.set_signup_edit_tokens(
        occurrence.occurrence_id,
        discord_user_id,
        tokens - 1.0,
        current_time,
    )
    # Write the new declaration, then resettle, both synchronously: the
    # resettle re-solves the seated set (fixing an assigned role the new
    # declaration no longer covers), seats a waitlisted editor whose new
    # roles now fit, and offers capacity the editor vacated to the waitlist.
    bot.event_store.update_signup_roles(
        occurrence.occurrence_id,
        discord_user_id,
        role=role,
        flex_roles=flex_roles,
        assigned_role=current.assigned_role if keeps_seat else None,
        waitlisted=not keeps_seat,
    )
    update = _resettle_roster(bot, event, occurrence)
    updated = bot.event_store.get_signup(
        occurrence.occurrence_id,
        discord_user_id,
    )
    if updated is None:
        # The resettle above committed too, so both halves are announced
        # rather than lost with the row.
        await notify_roster_update(
            bot,
            occurrence,
            merge_roster_updates([checked, update]),
        )
        raise ValueError("You are not signed up for this event.")
    # A stored auto sign-up snapshots the roles it will use for future
    # occurrences, so an enabled one must follow the edit or next week's
    # roster would resurrect the old selection.
    auto = bot.event_store.get_auto_signup(event.event_id, discord_user_id)
    if auto is not None and auto.choice is AutoSignupChoice.YES:
        bot.event_store.set_auto_signup(
            event.event_id,
            discord_user_id,
            AutoSignupChoice.YES,
            role,
            flex_roles,
        )
    LOGGER.debug(
        "Applied signup edit; occurrence_id=%s user_id=%s role=%s "
        "flex_count=%s waitlisted=%s assigned_role=%s reassigned=%s "
        "promoted=%s",
        occurrence.occurrence_id,
        discord_user_id,
        role.value,
        len(flex_roles),
        updated.waitlisted,
        (
            updated.assigned_role.value
            if updated.assigned_role is not None
            else None
        ),
        len(update.reassigned),
        len(update.promoted),
    )
    # The check moved the roster before this edit did, and a member it
    # promoted can be one the edit then flexes, so the two are folded first.
    update = merge_roster_updates([checked, update])
    # The editor sees their own outcome in the ephemeral summary; the thread
    # only hears about the members their edit moved.
    await notify_roster_update(
        bot,
        occurrence,
        _without_member(update, discord_user_id),
    )
    await refresh_occurrence_message(bot, event, occurrence)
    return SignupEditResult(signup=updated, update=update)


def apply_auto_signups(
    bot: Gw2Bot,
    event: Event,
    occurrence: EventOccurrence,
) -> int:
    applied = 0
    for entry in bot.event_store.get_auto_signup_entries(event.event_id):
        signups = bot.event_store.get_signups(occurrence.occurrence_id)
        if any(
            signup.discord_user_id == entry.discord_user_id
            for signup in signups
        ):
            continue
        assigned_role: EventRole | None = None
        signup_role = entry.role
        signup_flex_roles = entry.flex_roles
        if event.capacity.has_roles:
            if entry.role is None:
                LOGGER.debug(
                    "Skipped auto signup without a stored role; "
                    "event_id=%s user_id=%s",
                    event.event_id,
                    entry.discord_user_id,
                )
                continue
            signup_role, signup_flex_roles = normalize_stored_roles(
                event.capacity,
                entry.role,
                entry.flex_roles,
            )
            if (
                signup_role is not entry.role
                or signup_flex_roles != entry.flex_roles
            ):
                bot.event_store.set_auto_signup(
                    event.event_id,
                    entry.discord_user_id,
                    entry.choice,
                    signup_role,
                    signup_flex_roles,
                )
                LOGGER.debug(
                    "Normalized automatic signup roles for the current "
                    "category; event_id=%s user_id=%s stored_role=%s "
                    "normalized_role=%s normalized_flex_count=%s",
                    event.event_id,
                    entry.discord_user_id,
                    entry.role.value,
                    signup_role.value,
                    len(signup_flex_roles),
                )
            # Same admission as a live signup: earlier entries may be flexed
            # aside to fit this one, but are never unseated. The roster is
            # freshly seeded and unseen, so the reshuffle happens silently.
            candidates = seated_candidates(signups)
            candidates.append(
                RosterCandidate(
                    discord_user_id=entry.discord_user_id,
                    preferences=preferred_role_order(
                        signup_role,
                        signup_flex_roles,
                    ),
                )
            )
            solution = solve_roster(event.capacity, candidates)
            waitlisted = solution is None
            if solution is not None:
                assigned_role = solution[entry.discord_user_id]
                assignments, _ = _seated_reassignments(signups, solution)
                bot.event_store.apply_roster_assignments(
                    occurrence.occurrence_id,
                    assignments,
                )
        else:
            waitlisted = is_roster_full(event.capacity, signups)
        bot.event_store.add_signup(
            occurrence_id=occurrence.occurrence_id,
            discord_user_id=entry.discord_user_id,
            role=signup_role,
            assigned_role=assigned_role,
            flex_roles=signup_flex_roles,
            waitlisted=waitlisted,
        )
        applied += 1
    LOGGER.debug(
        "Applied auto signups; event_id=%s occurrence_id=%s applied=%s",
        event.event_id,
        occurrence.occurrence_id,
        applied,
    )
    return applied


@dataclass(frozen=True, slots=True)
class AutoSignupDisableResult:
    """What turning automatic sign-up off did beyond storing the choice."""

    # Occurrences the member's automatic seat was pulled out of.
    withdrawn: tuple[EventOccurrence, ...] = ()
    # Later occurrences that still seat the member because they are already
    # posted, so the seat may have been taken deliberately. The caller has to
    # say so rather than promise the member is off every future roster.
    still_seated: tuple[EventOccurrence, ...] = ()


def disable_auto_signup(
    bot: Gw2Bot,
    event: Event,
    occurrence: EventOccurrence,
    discord_user_id: int,
) -> AutoSignupDisableResult:
    """Store the "no automatic sign-up" choice and undo the seats it took.

    Storing the choice only stops future seeding, and by the time a caller
    gets here the next occurrence can already hold this member: any roster
    change that crosses the occurrence's end (or finds its message gone) seeds
    the next one from inside refresh_occurrence_message, and the scheduler
    seeds it while a sign-out prompt sits open. Telling the member they will
    not be signed up again while that seat stands would be false, so drop it.

    Only occurrences that have not been posted are withdrawn from. A member
    can only sign themselves up from a posted message and apply_auto_signups
    runs only on a freshly created occurrence, so a signup on an unposted
    occurrence can only be automatic. A seat on a posted occurrence may well
    have been taken on purpose, and removing that - unseating the member and
    promoting someone in their place - would be the worse mistake, so those
    are reported back for the caller to mention instead.
    """
    bot.event_store.set_auto_signup(
        event.event_id,
        discord_user_id,
        AutoSignupChoice.NO,
        None,
        (),
    )
    withdrawn: list[EventOccurrence] = []
    still_seated: list[EventOccurrence] = []
    for later in bot.event_store.get_event_occurrences(event.event_id):
        if later.start_time <= occurrence.start_time:
            continue
        if later.message_id is not None:
            if (
                bot.event_store.get_signup(
                    later.occurrence_id,
                    discord_user_id,
                )
                is not None
            ):
                still_seated.append(later)
            continue
        removed = bot.event_store.remove_signup(
            later.occurrence_id,
            discord_user_id,
        )
        if removed is None:
            continue
        withdrawn.append(later)
        if not removed.waitlisted:
            # The seeded roster was solved with this member on it, so hand the
            # freed seat to the waitlist. The occurrence has not been posted,
            # so there is no message to refresh and nobody to notify.
            _resettle_roster(bot, event, later)
    LOGGER.debug(
        "Disabled auto signup; event_id=%s occurrence_id=%s user_id=%s "
        "withdrawn=%s still_seated=%s",
        event.event_id,
        occurrence.occurrence_id,
        discord_user_id,
        len(withdrawn),
        len(still_seated),
    )
    return AutoSignupDisableResult(
        withdrawn=tuple(withdrawn),
        still_seated=tuple(still_seated),
    )


def ensure_next_recurring_occurrence(
    bot: Gw2Bot,
    event: Event,
    occurrence: EventOccurrence,
    now: datetime,
) -> EventOccurrence | None:
    """Seed the next occurrence of a recurring series when one is due.

    Returns the created occurrence, or None for a non-repeating event or when a
    later occurrence already exists (so callers never create duplicates).
    """
    if event.repeat_frequency is RepeatFrequency.NONE:
        return None
    if bot.event_store.has_later_occurrence(
        event.event_id,
        occurrence.start_time,
    ):
        return None
    return _create_next_occurrence(bot, event, occurrence, now)


def _create_next_occurrence(
    bot: Gw2Bot,
    event: Event,
    occurrence: EventOccurrence,
    now: datetime,
) -> EventOccurrence:
    next_start = next_occurrence_start(
        event.repeat_frequency,
        event.repeat_days,
        occurrence.start_time,
        bot.event_timezone,
    )
    # Catch up after downtime, but skip only occurrences that have fully
    # ended. If the bot was down when an occurrence's start passed yet it is
    # still in progress, keep it so it can post as ongoing (preserving its
    # auto-signups and public post) instead of jumping to the next one.
    duration = timedelta(minutes=event.duration_minutes)
    while next_start + duration <= now:
        next_start = next_occurrence_start(
            event.repeat_frequency,
            event.repeat_days,
            next_start,
            bot.event_timezone,
        )
    new_occurrence = bot.event_store.create_occurrence(
        event.event_id,
        next_start,
    )
    try:
        applied = apply_auto_signups(bot, event, new_occurrence)
    except Exception:
        # The row is committed but its roster is half-built. Leaving it would
        # publish a run carrying an arbitrary slice of the members who asked to
        # be signed up automatically - and a caller that never saw this
        # occurrence returned cannot take it back, so a cancellation whose
        # seeding failed here would report failure while the maintenance pass
        # went on to post the run anyway. Seeding lands whole or not at all.
        _discard_occurrence(
            bot,
            new_occurrence,
            "a successor whose auto-signups failed",
        )
        raise
    LOGGER.debug(
        "Created next recurring event occurrence; event_id=%s "
        "occurrence_id=%s auto_signups=%s",
        event.event_id,
        new_occurrence.occurrence_id,
        applied,
    )
    return new_occurrence
