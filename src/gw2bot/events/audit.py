"""Audit lines for events: who created, edited or deleted one, and what it was.

Posted to the notification channel beside the guild log's joins and leaves.
Each line carries the event itself - as it was posted, before and after an
edit, or as it stood when it was deleted - so the channel answers what changed
without anybody having seen the post.

A line is a record of a change that has already been made, never a condition
on it: a notification channel that is unset or unreachable costs the line and
nothing else.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

import discord

from gw2bot.events.formatting import (
    EMBED_TOTAL_LIMIT,
    EMPTY_FIELD_TEXT,
    _embed_title,
    _fit_within_total_limit,
    describe_repeat,
    event_footer_text,
    format_duration,
    format_role_mentions,
    requirements_text,
)
from gw2bot.events.models import Event, RepeatFrequency

if TYPE_CHECKING:
    from gw2bot.bot import Gw2Bot

LOGGER = logging.getLogger(__name__)

CREATED_COLOR = 0x2ECC71
BEFORE_COLOR = 0x95A5A6
AFTER_COLOR = 0x3498DB
DELETED_COLOR = 0xE74C3C

NO_PING_ROLES_TEXT = "None"
UNCAPPED_SQUAD_TEXT = "No limit"


@dataclass(frozen=True, slots=True)
class EventAudit:
    """One audit line: the sentence naming the change, and the event shown."""

    action: str
    event_id: int
    content: str
    embeds: tuple[discord.Embed, ...]


@dataclass(frozen=True, slots=True)
class _Detail:
    label: str
    emoji: str
    value: str
    inline: bool = True


def _details(event: Event, start_time: datetime) -> tuple[_Detail, ...]:
    """Every field an audit embed shows, in the order it shows them.

    An edit's line names the labels whose values differ, so what it says
    changed is exactly what differs between its two embeds.
    """
    repeat = describe_repeat(event.repeat_frequency, event.repeat_days)
    if (
        event.repeat_frequency is not RepeatFrequency.NONE
        and event.delete_previous_on_repeat
    ):
        repeat += ", removing the previous post each time"
    squad = event.capacity.total
    return (
        _Detail("Category", "🏷️", event.category.value),
        _Detail("Date & Time", "📅", f"<t:{int(start_time.timestamp())}:f>"),
        _Detail("Duration", "⏳", format_duration(event.duration_minutes)),
        _Detail("Leader", "👑", f"<@{event.leader_discord_id}>"),
        _Detail("Channel", "📢", f"<#{event.channel_id}>"),
        _Detail("Repeats", "🔁", repeat),
        _Detail(
            "Squad size",
            "👥",
            str(squad) if squad is not None else UNCAPPED_SQUAD_TEXT,
        ),
        _Detail(
            "Role pings",
            "📣",
            format_role_mentions(event.ping_role_ids) or NO_PING_ROLES_TEXT,
        ),
        _Detail(
            "Mentee slot",
            "🎓",
            "Offered" if event.mentee_enabled else "Not offered",
        ),
        _Detail(
            "Requirements",
            "📌",
            requirements_text(event.requirements),
            inline=False,
        ),
    )


def _changed_labels(
    before: Event,
    before_start: datetime,
    after: Event,
    after_start: datetime,
) -> list[str]:
    changed: list[str] = []
    if before.title != after.title:
        changed.append("Title")
    if before.description != after.description:
        changed.append("Description")
    changed.extend(
        old.label
        for old, new in zip(
            _details(before, before_start),
            _details(after, after_start),
            strict=True,
        )
        if old.value != new.value
    )
    return changed


def event_audit_embed(
    event: Event,
    start_time: datetime,
    *,
    heading: str,
    color: int,
    budget: int = EMBED_TOTAL_LIMIT,
) -> discord.Embed:
    """The event as an audit line shows it: every detail it was saved with.

    The post itself leaves out where it was posted, how it repeats and whom
    it pings, which are as much a part of an edit as the title is. `budget`
    is this embed's share of the message: Discord caps every embed on one
    message together, so an edit's before and after split it between them.
    """
    embed = discord.Embed(
        title=_embed_title(event.category, event.title),
        description=event.description or EMPTY_FIELD_TEXT,
        color=color,
    )
    embed.set_author(name=heading)
    for detail in _details(event, start_time):
        embed.add_field(
            name=f"{detail.emoji} {detail.label}",
            value=detail.value,
            inline=detail.inline,
        )
    embed.set_footer(text=event_footer_text(str(event.event_id)))
    _fit_within_total_limit(embed, reserve=EMBED_TOTAL_LIMIT - budget)
    return embed


def event_created_audit(
    user_id: int,
    event: Event,
    start_time: datetime,
) -> EventAudit:
    return EventAudit(
        action="created",
        event_id=event.event_id,
        content=f"<@{user_id}> created event **{event.event_id}**.",
        embeds=(
            event_audit_embed(
                event,
                start_time,
                heading="Created",
                color=CREATED_COLOR,
            ),
        ),
    )


def event_edited_audit(
    user_id: int,
    before: Event | None,
    before_start: datetime,
    after: Event,
    after_start: datetime,
) -> EventAudit:
    """The line for a saved edit, with the event before and after it.

    The starts are the run the commander edited rather than the events' own:
    a repeating event stores when its series began, which an edit only moves
    by as much as it moved that run.
    """
    if before is None:
        # The edit saved over a row it could not read first. There is nothing
        # to compare against, so the line shows what was saved.
        return EventAudit(
            action="edited",
            event_id=after.event_id,
            content=f"<@{user_id}> edited event **{after.event_id}**.",
            embeds=(
                event_audit_embed(
                    after,
                    after_start,
                    heading="After",
                    color=AFTER_COLOR,
                ),
            ),
        )
    changed = _changed_labels(before, before_start, after, after_start)
    content = (
        f"<@{user_id}> edited event **{after.event_id}**. "
        f"Changed: {', '.join(changed)}."
        if changed
        else f"<@{user_id}> saved event **{after.event_id}** without "
        "changing anything."
    )
    budget = EMBED_TOTAL_LIMIT // 2
    return EventAudit(
        action="edited",
        event_id=after.event_id,
        content=content,
        embeds=(
            event_audit_embed(
                before,
                before_start,
                heading="Before",
                color=BEFORE_COLOR,
                budget=budget,
            ),
            event_audit_embed(
                after,
                after_start,
                heading="After",
                color=AFTER_COLOR,
                budget=budget,
            ),
        ),
    )


def event_deleted_audit(
    user_id: int,
    event: Event,
    start_time: datetime,
    kept: int = 0,
) -> EventAudit:
    content = f"<@{user_id}> deleted event **{event.event_id}**."
    if kept == 1:
        content += " Its one finished run was kept."
    elif kept:
        content += f" Its {kept} finished runs were kept."
    return EventAudit(
        action="deleted",
        event_id=event.event_id,
        content=content,
        embeds=(
            event_audit_embed(
                event,
                start_time,
                heading="Deleted",
                color=DELETED_COLOR,
            ),
        ),
    )


async def send_event_audit(bot: Gw2Bot, audit: EventAudit) -> bool:
    LOGGER.debug(
        "Sending event audit line; action=%s event_id=%s embeds=%s",
        audit.action,
        audit.event_id,
        len(audit.embeds),
    )
    # Delivery reports its own failures, and an unset channel is a skip it
    # logs at debug: the change is made either way.
    delivered = await bot.send_notification(audit.content, audit.embeds)
    LOGGER.debug(
        "Event audit line attempted; action=%s event_id=%s delivered=%s",
        audit.action,
        audit.event_id,
        delivered,
    )
    return delivered
