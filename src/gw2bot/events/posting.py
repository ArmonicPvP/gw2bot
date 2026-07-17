from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import discord
from sqlalchemy.exc import SQLAlchemyError

from gw2bot.discord_utils import discord_failure_reason, log_discord_failure
from gw2bot.events.formatting import (
    compute_status,
    event_embed,
    event_thread_name,
    next_occurrence_start,
)
from gw2bot.events.models import (
    Event,
    EventOccurrence,
    EventRole,
    EventSignup,
    EventStatus,
    RepeatFrequency,
    choose_assigned_role,
    is_roster_full,
    rebalance_signups,
)

if TYPE_CHECKING:
    from gw2bot.bot import Gw2Bot

LOGGER = logging.getLogger(__name__)


async def resolve_channel(bot: Gw2Bot, channel_id: int) -> Any:
    channel = bot.get_channel(channel_id)
    if channel is None:
        channel = await bot.fetch_channel(channel_id)
    return channel


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
    message = await channel.send(
        embed=occurrence_embed(event, occurrence, signups, now),
        view=build_signup_view(occurrence.occurrence_id),
    )
    thread_id: int | None = None
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
            event.channel_id,
            message.id,
            thread_id,
        )
    except SQLAlchemyError as exc:
        LOGGER.error(
            "Could not persist posted event occurrence; occurrence_id=%s "
            "error_type=%s",
            occurrence.occurrence_id,
            type(exc).__name__,
        )
        await _delete_orphaned_message(
            bot, message, thread_id, occurrence.occurrence_id
        )
        raise
    LOGGER.debug(
        "Posted event occurrence; event_id=%s occurrence_id=%s status=%s "
        "thread_created=%s signups=%s",
        event.event_id,
        occurrence.occurrence_id,
        status.value,
        thread_id is not None,
        len(signups),
    )
    updated = bot.event_store.get_occurrence(occurrence.occurrence_id)
    if updated is None:
        raise RuntimeError("The posted event occurrence disappeared")
    return updated


async def _delete_orphaned_message(
    bot: Gw2Bot, message: Any, thread_id: int | None, occurrence_id: int
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
    await _delete_occurrence_thread(bot, thread_id, occurrence_id)


async def _delete_occurrence_thread(
    bot: Gw2Bot, thread_id: int | None, occurrence_id: int
) -> None:
    # Discord does not delete a thread when its starter message is removed;
    # the thread survives as an orphan unless it is deleted separately.
    if thread_id is None:
        LOGGER.debug(
            "No event thread to delete; skipping; occurrence_id=%s",
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
    message_refreshed = True
    if occurrence.message_id is not None:
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


async def _rename_occurrence_thread(
    bot: Gw2Bot,
    occurrence: EventOccurrence,
    status: EventStatus,
) -> bool:
    if occurrence.thread_id is None:
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


async def repost_occurrence(
    bot: Gw2Bot,
    event: Event,
    occurrence: EventOccurrence,
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
    reposted = await post_occurrence(bot, event, occurrence)
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
        # strand the thread.
        await _delete_occurrence_thread(
            bot, old_thread_id, occurrence.occurrence_id
        )
    signups = bot.event_store.get_signups(reposted.occurrence_id)
    for signup in signups:
        await update_thread_membership(
            bot,
            reposted,
            signup.discord_user_id,
            add=True,
        )
    LOGGER.debug(
        "Reposted event occurrence to new channel; occurrence_id=%s "
        "signups=%s",
        reposted.occurrence_id,
        len(signups),
    )
    return reposted


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
    for occurrence in occurrences:
        if occurrence.message_id is None:
            continue
        channel_id = occurrence_channel_id(event, occurrence)
        if channel_id in unresolvable:
            continue
        channel = channels.get(channel_id)
        if channel is None:
            try:
                channel = await resolve_channel(bot, channel_id)
            except discord.HTTPException as exc:
                # One dead channel must not strand the posts in the others.
                unresolvable.add(channel_id)
                LOGGER.error(
                    "Could not resolve channel to delete event posts; "
                    "event_id=%s error_type=%s",
                    event.event_id,
                    type(exc).__name__,
                )
                continue
            channels[channel_id] = channel
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
        # must not also strand the thread.
        await _delete_occurrence_thread(
            bot, occurrence.thread_id, occurrence.occurrence_id
        )
    LOGGER.debug(
        "Deleted event posts; event_id=%s messages_deleted=%s",
        event.event_id,
        deleted,
    )
    return deleted


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
    signups = bot.event_store.get_signups(occurrence.occurrence_id)
    # The role/flex/remember views can linger until their timeout, so the
    # occurrence may have ended between opening the flow and this click.
    # Refuse to mutate a historical roster (which would also update thread
    # membership and refresh the past message).
    if occurrence_status(event, occurrence, signups, now) is EventStatus.OVER:
        raise ValueError(
            "This event has already ended, so you can no longer sign up."
        )
    assigned_role: EventRole | None = None
    waitlisted: bool
    if event.capacity.has_roles:
        if role is None:
            raise ValueError("This event requires picking a role.")
        assigned_role = choose_assigned_role(
            event.capacity,
            signups,
            role,
            flex_roles,
        )
        waitlisted = assigned_role is None
    else:
        waitlisted = is_roster_full(event.capacity, signups)
    signup = bot.event_store.add_signup(
        occurrence_id=occurrence.occurrence_id,
        discord_user_id=discord_user_id,
        role=role,
        assigned_role=assigned_role,
        flex_roles=flex_roles,
        waitlisted=waitlisted,
    )
    await update_thread_membership(
        bot,
        occurrence,
        discord_user_id,
        add=True,
    )
    await refresh_occurrence_message(bot, event, occurrence)
    return signup


async def remove_signup(
    bot: Gw2Bot,
    event: Event,
    occurrence: EventOccurrence,
    discord_user_id: int,
) -> tuple[EventSignup | None, EventSignup | None]:
    removed = bot.event_store.remove_signup(
        occurrence.occurrence_id,
        discord_user_id,
    )
    if removed is None:
        return None, None
    # Promote a waitlisted user into the freed slot before yielding to any
    # awaited Discord I/O. Both the removal and promotion are synchronous store
    # writes, so keeping them adjacent makes the mutation atomic: a concurrent
    # complete_signup cannot observe the freed slot and claim it ahead of the
    # existing waitlist while we await the thread update below.
    promoted: EventSignup | None = None
    if not removed.waitlisted:
        promoted = _promote_first_fitting_waitlisted(bot, event, occurrence)
    await update_thread_membership(
        bot,
        occurrence,
        discord_user_id,
        add=False,
    )
    await refresh_occurrence_message(bot, event, occurrence)
    return removed, promoted


def rebalance_occurrence_roster(
    bot: Gw2Bot,
    event: Event,
    occurrence: EventOccurrence,
) -> int:
    # Call this after an edit changes an event's category: the stored
    # assignments were seated against the old category's capacity and no longer
    # describe a valid roster. Returns how many signups actually moved.
    signups = bot.event_store.get_signups(occurrence.occurrence_id)
    if not signups:
        return 0
    reseated = rebalance_signups(event.capacity, signups)
    changed = 0
    for before, after in zip(signups, reseated, strict=True):
        if (
            before.role is after.role
            and before.assigned_role is after.assigned_role
            and before.waitlisted == after.waitlisted
        ):
            continue
        bot.event_store.set_signup_assignment(
            occurrence.occurrence_id,
            after.discord_user_id,
            role=after.role,
            assigned_role=after.assigned_role,
            waitlisted=after.waitlisted,
        )
        changed += 1
    LOGGER.debug(
        "Rebalanced event roster for a new category; occurrence_id=%s "
        "category=%s signups=%s changed=%s",
        occurrence.occurrence_id,
        event.category.value,
        len(signups),
        changed,
    )
    return changed


def _promote_first_fitting_waitlisted(
    bot: Gw2Bot,
    event: Event,
    occurrence: EventOccurrence,
) -> EventSignup | None:
    signups = bot.event_store.get_signups(occurrence.occurrence_id)
    waitlisted = [signup for signup in signups if signup.waitlisted]
    for candidate in waitlisted:
        if event.capacity.has_roles:
            if candidate.role is None:
                continue
            assigned_role = choose_assigned_role(
                event.capacity,
                signups,
                candidate.role,
                candidate.flex_roles,
            )
            if assigned_role is None:
                continue
        else:
            if is_roster_full(event.capacity, signups):
                break
            assigned_role = None
        bot.event_store.promote_signup(
            occurrence.occurrence_id,
            candidate.discord_user_id,
            assigned_role,
        )
        LOGGER.debug(
            "Auto-promoted waitlisted event signup; occurrence_id=%s "
            "user_id=%s",
            occurrence.occurrence_id,
            candidate.discord_user_id,
        )
        return bot.event_store.get_signup(
            occurrence.occurrence_id,
            candidate.discord_user_id,
        )
    return None


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
        if event.capacity.has_roles:
            if entry.role is None:
                LOGGER.debug(
                    "Skipped auto signup without a stored role; "
                    "event_id=%s user_id=%s",
                    event.event_id,
                    entry.discord_user_id,
                )
                continue
            assigned_role = choose_assigned_role(
                event.capacity,
                signups,
                entry.role,
                entry.flex_roles,
            )
            waitlisted = assigned_role is None
        else:
            waitlisted = is_roster_full(event.capacity, signups)
        bot.event_store.add_signup(
            occurrence_id=occurrence.occurrence_id,
            discord_user_id=entry.discord_user_id,
            role=entry.role,
            assigned_role=assigned_role,
            flex_roles=entry.flex_roles,
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
    applied = apply_auto_signups(bot, event, new_occurrence)
    LOGGER.debug(
        "Created next recurring event occurrence; event_id=%s "
        "occurrence_id=%s auto_signups=%s",
        event.event_id,
        new_occurrence.occurrence_id,
        applied,
    )
    return new_occurrence
