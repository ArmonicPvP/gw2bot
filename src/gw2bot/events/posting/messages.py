"""Posting the event message, refreshing it, and taking it down.

One posting lock per occurrence serialises these against each other, because
two passes posting the same occurrence would leave a duplicate nobody owns.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, TYPE_CHECKING

import discord
from sqlalchemy.exc import SQLAlchemyError

from gw2bot.events.formatting import (
    calendar_link,
    event_embed,
    event_thread_name,
    message_link,
)
from gw2bot.events.models import (
    Event,
    EventOccurrence,
    EventSignup,
    EventStatus,
    RosterUpdate,
)
from gw2bot.events.posting.channels import (
    _delete_occurrence_thread,
    _delete_orphaned_message,
    _rename_occurrence_thread,
    _reopen_thread,
    _resolve_cached_channel,
    is_thread_channel,
    reopen_occurrence_thread,
    resolve_channel,
    update_thread_membership,
)
from gw2bot.events.posting.pings import (
    _keep_stale_announcement,
    announce_occurrence_ping,
    delete_occurrence_announcement,
    drop_announcement,
    ping_send_kwargs,
    record_occurrence_announcement,
    refresh_occurrence_announcement,
    resolve_ping_announcement,
    retire_occurrence_announcement,
    sweep_stale_announcement,
    verified_ping_role_ids,
)
from gw2bot.events.posting.roster import (
    check_roster_membership,
    merge_roster_updates,
    notify_roster_update,
)
from gw2bot.events.posting.state import (
    occurrence_channel_id,
    occurrence_finished,
    occurrence_status,
)

if TYPE_CHECKING:
    from gw2bot.bot import Gw2Bot

LOGGER = logging.getLogger(__name__)


def occurrence_embed(
    bot: Gw2Bot,
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
        # Read per render rather than captured: /settings can turn the
        # calendar on or off while events are posted, and the footer of every
        # message refreshed after that has to say what is true then.
        calendar_url=calendar_link(bot._config),
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
    embed = occurrence_embed(bot, event, occurrence, signups, now)
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


async def refresh_occurrence_message(
    bot: Gw2Bot,
    event: Event,
    occurrence: EventOccurrence,
    now: datetime | None = None,
    *,
    force_thread_rename: bool = False,
) -> EventStatus:
    # Imported here rather than at module scope: seeding the next occurrence
    # applies its auto-sign-ups, which refresh a post through this module.
    from gw2bot.events.posting.occurrences import (
        ensure_next_recurring_occurrence,
    )

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
                embed=occurrence_embed(bot, event, occurrence, signups, now),
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


async def repost_occurrence(
    bot: Gw2Bot,
    event: Event,
    occurrence: EventOccurrence,
    deferred_update: RosterUpdate = RosterUpdate(),
) -> EventOccurrence | None:
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
    # The same thing that can happen to a fresh post can happen here: the
    # check removes through remove_signup, whose refresh addresses the
    # replacement message, and one deleted while the lookups were in flight
    # answers NotFound - retiring this run and seeding its successor. There is
    # no live post to subscribe a roster to then, and no thread worth
    # mentioning anybody in, so the move reports nothing moved and the caller
    # says so rather than counting it as refreshed.
    #
    # The move itself is done by now - the new post is live, its id is stored
    # and the old message is deleted - so a store that cannot answer these
    # reads must not turn it into a failure. The caller would restore the old
    # channel and tell the commander the event stayed there, while its only
    # post sits in the new one. What is lost is the subscriptions and the
    # announcement, which the next roster change makes good.
    try:
        settled = bot.event_store.get_occurrence(reposted.occurrence_id)
        signups = (
            bot.event_store.get_signups(reposted.occurrence_id)
            if settled is not None
            else []
        )
    except SQLAlchemyError as exc:
        LOGGER.error(
            "Could not read the roster back after moving it; "
            "occurrence_id=%s error_type=%s",
            reposted.occurrence_id,
            type(exc).__name__,
        )
        # The move is reported as done, so nothing after this announces the
        # caller's deferred re-seat or what the check moved - and the row
        # already names the thread the move opened, which is where both
        # belong. Only the subscriptions are lost with that read.
        await notify_roster_update(
            bot,
            reposted,
            merge_roster_updates([deferred_update, checked], departed),
        )
        return reposted
    if settled is None or (
        settled.status is EventStatus.OVER
        and reposted.status is not EventStatus.OVER
    ):
        LOGGER.error(
            "Moved occurrence retired by its own roster check; "
            "occurrence_id=%s exists=%s",
            reposted.occurrence_id,
            settled is not None,
        )
        return None
    reposted = settled
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


def split_event_history(
    event: Event,
    occurrences: list[EventOccurrence],
    now: datetime,
) -> tuple[list[EventOccurrence], list[EventOccurrence]]:
    """Split an event's runs into the history to keep and the rest to remove.

    Deleting an event calls off what is still to come; it does not undo what
    the guild already ran. A run that has finished and still has its post is
    kept as it stands - the post stays in the channel, the row stays in the
    store, and the calendar goes on showing the day it was run on - so only
    the rest is removed.

    "Finished" is `occurrence_finished`: the clock, or the stored status for a
    run retired early. A row that never reached a message is removed whether
    or not its time has passed, because there is no post to keep and the
    maintenance pass posts a pending row it finds - which would put a fresh
    message in the channel for an event that has just been deleted.

    Returns (kept, removed), each in the order it was given.
    """
    kept: list[EventOccurrence] = []
    removed: list[EventOccurrence] = []
    for occurrence in occurrences:
        if occurrence.message_id is not None and occurrence_finished(
            event, occurrence, now
        ):
            kept.append(occurrence)
        else:
            removed.append(occurrence)
    LOGGER.debug(
        "Split an event's runs for deletion; event_id=%s kept=%s removed=%s",
        event.event_id,
        len(kept),
        len(removed),
    )
    return kept, removed


async def refresh_retired_posts(
    bot: Gw2Bot,
    event: Event,
    occurrences: list[EventOccurrence],
    now: datetime,
) -> int:
    """Show the runs a deletion keeps as the finished runs they now are.

    Retiring the rows as OVER takes them out of the maintenance pass for
    good, so this is the last chance to put that on the posts. A run whose
    end no pass had caught up with yet - the minute after it finishes, or
    longer behind a refresh Discord kept refusing - would otherwise stand in
    the channel advertising itself as open for as long as the post lives,
    which is a poor record of what the guild ran.

    Deliberately not refresh_occurrence_message: that path seeds the next
    occurrence of a recurring series on its way through OVER, which is the
    one thing a deleted event must never do. Nothing here writes to the
    store - the retirement already did - so a failure only costs this post
    its final render, and one failure must not cost the others theirs.

    Takes the occurrences as they were read before the retirement, which is
    what says whether a post can be stale: one already stored as OVER was
    rendered by the pass that persisted it, unless that pass came away dirty.
    """
    refreshed = 0
    for occurrence in occurrences:
        if occurrence.message_id is None:
            continue
        if occurrence.status is EventStatus.OVER and (
            not occurrence.needs_refresh
        ):
            continue
        try:
            signups = bot.event_store.get_signups(occurrence.occurrence_id)
        except SQLAlchemyError as exc:
            # Contained per run, like the Discord failure below: the rows are
            # already retired, so a roster this pass cannot read must not cost
            # the other posts their final render - nor the commander the
            # report waiting on this call to return.
            LOGGER.error(
                "Could not read a kept run's roster to render it; "
                "occurrence_id=%s error_type=%s",
                occurrence.occurrence_id,
                type(exc).__name__,
            )
            continue
        # Discord refuses edits inside an archived thread, and a forum post an
        # event was sent into can have been dormant for weeks.
        await reopen_occurrence_thread(bot, occurrence)
        # Logged before the await, not only after it: an edit that hangs, is
        # cancelled, or is cut off by a restart reaches neither the success
        # count nor the failure line below, and this render - the last one
        # this post will ever get - would leave no trace at all.
        LOGGER.debug(
            "Rendering a kept event run as finished; occurrence_id=%s",
            occurrence.occurrence_id,
        )
        try:
            channel = await resolve_channel(
                bot,
                occurrence_channel_id(event, occurrence),
            )
            await channel.get_partial_message(occurrence.message_id).edit(
                embed=occurrence_embed(bot, event, occurrence, signups, now),
            )
        except discord.HTTPException as exc:
            # NotFound included: a post somebody deleted by hand is nothing to
            # retire, and the row stays as the record of the run either way.
            LOGGER.error(
                "Could not show a kept event run as finished; "
                "occurrence_id=%s error_type=%s",
                occurrence.occurrence_id,
                type(exc).__name__,
            )
            continue
        refreshed += 1
        # The thread name carries the status too, so it would keep announcing
        # a run that is open. Best-effort, and independent of the edit above.
        await _rename_occurrence_thread(bot, occurrence, EventStatus.OVER)
    LOGGER.debug(
        "Showed an event's kept runs as finished; event_id=%s refreshed=%s "
        "kept=%s",
        event.event_id,
        refreshed,
        len(occurrences),
    )
    return refreshed


async def delete_event_posts(
    bot: Gw2Bot,
    event: Event,
    occurrences: list[EventOccurrence],
) -> int:
    # Best-effort cleanup of the public posts (and their threads) of the runs
    # it is given - which on a deletion is not every run the event has had:
    # the ones it has already put on keep their posts and are never passed
    # here (see split_event_history). Discord does not delete a thread when
    # its starter message is removed, so each occurrence's thread is deleted
    # separately below. This runs after the store rows are gone, so any
    # message that survives a failure here just has buttons that gracefully
    # report the event is no longer available.
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
    already, to be gone, or to have been retired by its own roster check
    before that roster could be set up. Members on its roster are subscribed
    to the post, which is how a successor's auto-signups reach the thread they
    were seeded into.
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
        # That check removes through remove_signup, whose refresh addresses
        # the message sent moments ago - and a message or channel deleted
        # while the lookups were in flight answers it with NotFound, which
        # retires this run and seeds its successor. Subscribing a roster to
        # the thread of a run that has just been replaced, and mentioning
        # them in it, is worse than saying nothing, so the row is read back
        # and the setup stops there.
        #
        # Reported as nothing posted, rather than as this row: the successor
        # the refresh seeded is what the series is on now, and a caller told
        # otherwise sends the commander to a run that has been superseded.
        # Callers resolve what actually stands from None.
        #
        # A run that was over before this check - posting blocked past its
        # end - is a different thing and is still returned, because the
        # scheduler seeds the series' next run off exactly that.
        #
        # The post itself is done by the time this runs - the message is live
        # and its id is stored - so a store that cannot answer must not turn
        # it into a failure. The scheduler would take the exception for a
        # failed post and skip the cleanup behind it, which for an occurrence
        # that was already over means never seeding the series' next run.
        try:
            settled = bot.event_store.get_occurrence(posted.occurrence_id)
        except SQLAlchemyError as exc:
            LOGGER.error(
                "Could not read the run back after posting it; "
                "occurrence_id=%s error_type=%s",
                posted.occurrence_id,
                type(exc).__name__,
            )
            # The post is reported as made, so nothing behind this announces
            # what the check moved. The thread is the one this post opened
            # and nobody is in it yet, but the line belongs there rather
            # than nowhere - the same call the move's recovery makes.
            await notify_roster_update(bot, posted, checked)
            return posted
        if settled is None or (
            settled.status is EventStatus.OVER
            and posted.status is not EventStatus.OVER
        ):
            LOGGER.debug(
                "Posted occurrence retired by its own roster check; "
                "occurrence_id=%s exists=%s",
                posted.occurrence_id,
                settled is not None,
            )
            return None
        posted = settled
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
        # Whatever the check moved is committed, and the subscription loop
        # below is what would have carried it. Say it in the thread rather
        # than nowhere: it is in the post's embed either way, and a member who
        # opens the thread finds the line waiting.
        await notify_roster_update(bot, posted, checked)
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
