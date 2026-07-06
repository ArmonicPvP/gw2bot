from __future__ import annotations

import logging
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
