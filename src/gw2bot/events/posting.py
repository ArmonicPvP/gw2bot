from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import discord
from sqlalchemy.exc import SQLAlchemyError

from gw2bot.events.formatting import (
    compute_status,
    event_embed,
    event_thread_name,
)
from gw2bot.events.models import (
    Event,
    EventOccurrence,
    EventRole,
    EventSignup,
    EventStatus,
    choose_assigned_role,
    is_roster_full,
)

if TYPE_CHECKING:
    from gw2bot.bot import Gw2Bot

LOGGER = logging.getLogger(__name__)


async def resolve_channel(bot: Gw2Bot, channel_id: int) -> Any:
    channel = bot.get_channel(channel_id)
    if channel is None:
        channel = await bot.fetch_channel(channel_id)
    return channel


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
        await _delete_orphaned_message(message, occurrence.occurrence_id)
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


async def _delete_orphaned_message(message: Any, occurrence_id: int) -> None:
    # Deleting the starter message also removes any thread anchored to it.
    try:
        await message.delete()
    except discord.HTTPException as exc:
        LOGGER.error(
            "Could not delete orphaned event message; occurrence_id=%s "
            "error_type=%s",
            occurrence_id,
            type(exc).__name__,
        )


async def refresh_occurrence_message(
    bot: Gw2Bot,
    event: Event,
    occurrence: EventOccurrence,
    now: datetime | None = None,
) -> EventStatus:
    signups = bot.event_store.get_signups(occurrence.occurrence_id)
    status = occurrence_status(event, occurrence, signups, now)
    message_refreshed = True
    if occurrence.message_id is not None:
        try:
            channel = await resolve_channel(bot, event.channel_id)
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
    # message or thread name stale forever.
    thread_renamed = True
    if message_refreshed and status != occurrence.status:
        thread_renamed = await _rename_occurrence_thread(
            bot,
            occurrence,
            status,
        )
        if thread_renamed:
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
    except discord.HTTPException as exc:
        LOGGER.error(
            "Could not rename event thread; occurrence_id=%s error_type=%s",
            occurrence.occurrence_id,
            type(exc).__name__,
        )
        return False
    return True


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
