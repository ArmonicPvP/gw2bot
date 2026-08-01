from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol, cast

import discord

LOGGER = logging.getLogger(__name__)


class TopicEditableChannel(Protocol):
    async def edit(
        self,
        *,
        topic: str,
        reason: str | None = None,
    ) -> Any: ...


def user_has_role(user: Any, required_role_id: int) -> bool:
    return any(
        role.id == required_role_id
        for role in getattr(user, "roles", ())
    )


def log_discord_failure(message: str, error: discord.DiscordException, *args: object) -> None:
    LOGGER.error(
        message + " (type=%s status=%s code=%s)",
        *args,
        type(error).__name__,
        getattr(error, "status", "unknown"),
        getattr(error, "code", "unknown"),
    )


def discord_failure_reason(error: discord.DiscordException) -> str:
    code = getattr(error, "code", None)
    if code == 50001:
        return "missing_access"
    if code == 50013:
        return "missing_permissions"
    return "discord_error"


def discord_failure_signature(error: discord.DiscordException) -> str:
    # Sanitized identity (no raw response body) used to deduplicate repeated
    # failure logs; mirrors the fields emitted by log_discord_failure.
    return (
        f"{type(error).__name__}:"
        f"{getattr(error, 'status', 'unknown')}:"
        f"{getattr(error, 'code', 'unknown')}"
    )


@dataclass(frozen=True, slots=True)
class GuildMembership:
    """What one member lookup could establish about a Discord user.

    ``in_guild`` is deliberately three-valued. Discord answering "unknown
    member" proves the user has left, but a permission error or an outage
    proves nothing, and a caller that drops people from a roster must be able
    to tell those apart: only a definite False may cost someone their seat.
    """

    display_name: str | None = None
    in_guild: bool | None = None


async def resolve_guild_membership(
    bot: Any,
    guild: discord.Guild | None,
    user_id: int,
) -> GuildMembership:
    """Look a member up once, reporting both their name and their membership.

    The bot runs without the members intent, so the guild member cache is
    almost always empty and the member has to be fetched. The guild member is
    preferred over the global user so a server nickname wins, and that same
    fetch is what answers whether the user is still in the server at all.
    """
    in_guild: bool | None = None
    if guild is not None:
        member = guild.get_member(user_id)
        if member is not None:
            return GuildMembership(member.display_name, True)
        try:
            member = await guild.fetch_member(user_id)
        except discord.NotFound as exc:
            # Discord knows the guild and does not know this member in it, so
            # they have left. The global lookup below still runs: the name is
            # wanted for whatever the caller reports about the departure.
            LOGGER.debug(
                "Guild member lookup reported a departed member; user_id=%s "
                "failure=%s",
                user_id,
                discord_failure_signature(exc),
            )
            in_guild = False
            member = None
        except discord.HTTPException as exc:
            # The global lookup below can still succeed and hide this from the
            # aggregate counts, so record the guild failure on its own: a
            # roster that silently loses every nickname is otherwise
            # indistinguishable from one that has none. The sanitized failure
            # signature only, never the response body.
            LOGGER.debug(
                "Guild member lookup failed; falling back to the global user; "
                "user_id=%s failure=%s",
                user_id,
                discord_failure_signature(exc),
            )
            member = None
        if member is not None:
            return GuildMembership(member.display_name, True)
    try:
        user = await bot.fetch_user(user_id)
    except discord.HTTPException as exc:
        LOGGER.debug(
            "Display name lookup failed; user_id=%s failure=%s",
            user_id,
            discord_failure_signature(exc),
        )
        return GuildMembership(None, in_guild)
    return GuildMembership(user.display_name, in_guild)


async def resolve_guild_memberships(
    bot: Any,
    guild: discord.Guild | None,
    user_ids: Sequence[int],
) -> dict[int, GuildMembership]:
    """Resolve several members at once.

    Lookups run concurrently so a whole roster costs one round trip rather
    than one per member.
    """
    unique = list(dict.fromkeys(user_ids))
    if not unique:
        return {}
    memberships = await asyncio.gather(
        *(resolve_guild_membership(bot, guild, user_id) for user_id in unique)
    )
    resolved = dict(zip(unique, memberships, strict=True))
    LOGGER.debug(
        "Resolved guild memberships; requested=%s named=%s unnamed=%s "
        "in_guild=%s departed=%s unknown=%s",
        len(unique),
        sum(1 for entry in memberships if entry.display_name is not None),
        sum(1 for entry in memberships if entry.display_name is None),
        sum(1 for entry in memberships if entry.in_guild is True),
        sum(1 for entry in memberships if entry.in_guild is False),
        sum(1 for entry in memberships if entry.in_guild is None),
    )
    return resolved


async def resolve_display_name(
    bot: Any,
    guild: discord.Guild | None,
    user_id: int,
) -> str | None:
    """Return the display name, or None when Discord cannot be reached."""
    membership = await resolve_guild_membership(bot, guild, user_id)
    return membership.display_name


async def resolve_display_names(
    bot: Any,
    guild: discord.Guild | None,
    user_ids: Sequence[int],
) -> dict[int, str | None]:
    """Resolve several display names at once.

    An entry is None when Discord could not be reached for that member;
    callers substitute their own placeholder.
    """
    memberships = await resolve_guild_memberships(bot, guild, user_ids)
    return {
        user_id: membership.display_name
        for user_id, membership in memberships.items()
    }


async def send_direct_message(bot: Any, user_id: int, content: str) -> bool:
    """Direct-message a member, reporting whether delivery succeeded.

    Members can block the bot or close their DMs, which raises Forbidden. That
    is an expected outcome rather than a bug, so it is logged and reported to
    the caller instead of propagating: a single unreachable member must never
    abort a bulk notification.
    """
    LOGGER.debug(
        "Sending direct message; user_id=%s characters=%s",
        user_id,
        len(content),
    )
    try:
        user = await bot.fetch_user(user_id)
        await user.send(content)
    except discord.DiscordException as error:
        log_discord_failure(
            "Could not deliver a direct message; user_id=%s",
            error,
            user_id,
        )
        return False
    LOGGER.debug("Delivered direct message; user_id=%s", user_id)
    return True


def safe_int(value: object) -> int | None:
    if isinstance(value, int):
        return value
    if not isinstance(value, str):
        return None
    try:
        return int(value)
    except ValueError:
        return None


def thread_applied_tag_ids(thread: object) -> list[int]:
    tag_ids: list[int] = []
    for tag in getattr(thread, "applied_tags", ()):
        tag_id = safe_int(getattr(tag, "id", None))
        if tag_id is not None and tag_id not in tag_ids:
            tag_ids.append(tag_id)
    for value in getattr(thread, "_applied_tags", ()):
        tag_id = safe_int(value)
        if tag_id is not None and tag_id not in tag_ids:
            tag_ids.append(tag_id)
    return tag_ids


def forum_tags_for_ids(
    forum: object,
    tag_ids: set[int],
) -> dict[int, discord.ForumTag]:
    tags: dict[int, discord.ForumTag] = {}
    get_tag = getattr(forum, "get_tag", None)
    if callable(get_tag):
        for tag_id in tag_ids:
            tag = get_tag(tag_id)
            if tag is not None:
                tags[tag_id] = cast(discord.ForumTag, tag)

    for tag in getattr(forum, "available_tags", ()):
        tag_id = safe_int(getattr(tag, "id", None))
        if tag_id in tag_ids and tag_id not in tags:
            tags[tag_id] = cast(discord.ForumTag, tag)
    return tags
