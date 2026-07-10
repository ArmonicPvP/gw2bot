from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import discord

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
    bot.event_store.set_occurrence_message(
        occurrence.occurrence_id,
        message.id,
        thread_id,
    )
    bot.event_store.set_occurrence_status(occurrence.occurrence_id, status)
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


async def refresh_occurrence_message(
    bot: Gw2Bot,
    event: Event,
    occurrence: EventOccurrence,
    now: datetime | None = None,
) -> EventStatus:
    signups = bot.event_store.get_signups(occurrence.occurrence_id)
    status = occurrence_status(event, occurrence, signups, now)
    if occurrence.message_id is not None:
        try:
            channel = await resolve_channel(bot, event.channel_id)
            await channel.get_partial_message(occurrence.message_id).edit(
                embed=occurrence_embed(event, occurrence, signups, now),
            )
        except discord.HTTPException as exc:
            LOGGER.error(
                "Could not refresh event message; occurrence_id=%s "
                "error_type=%s",
                occurrence.occurrence_id,
                type(exc).__name__,
            )
            # The public message is now stale. Leave the stored status
            # unchanged so the scheduler keeps this occurrence in the
            # unfinished set and retries; persisting the transition (for
            # example to OVER) would drop it and leave the message stale
            # indefinitely.
            return occurrence.status
    if status != occurrence.status:
        bot.event_store.set_occurrence_status(
            occurrence.occurrence_id,
            status,
        )
        await _rename_occurrence_thread(bot, occurrence, status)
        LOGGER.debug(
            "Event occurrence status transitioned; occurrence_id=%s "
            "previous=%s status=%s",
            occurrence.occurrence_id,
            occurrence.status.value,
            status.value,
        )
    return status


async def _rename_occurrence_thread(
    bot: Gw2Bot,
    occurrence: EventOccurrence,
    status: EventStatus,
) -> None:
    if occurrence.thread_id is None:
        return
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
) -> EventSignup:
    signups = bot.event_store.get_signups(occurrence.occurrence_id)
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
    await update_thread_membership(
        bot,
        occurrence,
        discord_user_id,
        add=False,
    )
    promoted: EventSignup | None = None
    if not removed.waitlisted:
        promoted = _promote_first_fitting_waitlisted(bot, event, occurrence)
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
