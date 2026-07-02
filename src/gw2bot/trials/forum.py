from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import discord

from gw2bot.discord_utils import (
    forum_tags_for_ids,
    log_discord_failure,
    safe_int,
    thread_applied_tag_ids,
)
from gw2bot.raffle import TrialForumPost

if TYPE_CHECKING:
    from gw2bot.bot import Gw2Bot

LOGGER = logging.getLogger(__name__)

TRIAL_FORUM_CHANNEL_ID = 1317206104727621693
TRIAL_ACCEPTED_TAG_ID = 1317349209619562587
TRIAL_IN_REVIEW_TAG_ID = 1317349421821726790
TRIAL_FORUM_INDEX_GRACE = timedelta(hours=1)


async def apply_trial_forum_in_review_tag(
    bot: Gw2Bot,
    thread: discord.Thread,
) -> None:
    thread_id = getattr(thread, "id", "unknown")
    parent_id = getattr(thread, "parent_id", None)
    LOGGER.debug(
        "Discord thread created; thread_id=%s parent_id=%s",
        thread_id,
        parent_id,
    )
    if parent_id != TRIAL_FORUM_CHANNEL_ID:
        LOGGER.debug(
            "Ignoring created thread outside Trial application forum; "
            "thread_id=%s",
            thread_id,
        )
        return

    applied_tag_ids = thread_applied_tag_ids(thread)
    if TRIAL_IN_REVIEW_TAG_ID in applied_tag_ids:
        LOGGER.debug(
            "Trial application forum thread %s already has In Review tag",
            thread_id,
        )
        return

    tag_ids_to_resolve = {*applied_tag_ids, TRIAL_IN_REVIEW_TAG_ID}
    resolved_tags = await bot._resolve_trial_forum_tags(
        thread,
        tag_ids_to_resolve,
    )
    in_review_tag = resolved_tags.get(TRIAL_IN_REVIEW_TAG_ID)
    if in_review_tag is None:
        LOGGER.error(
            "Could not apply In Review tag to Trial application forum "
            "thread %s; tag_id=%s not found",
            thread_id,
            TRIAL_IN_REVIEW_TAG_ID,
        )
        return

    edit_tags = [
        resolved_tags[tag_id]
        for tag_id in applied_tag_ids
        if tag_id in resolved_tags
    ]
    unresolved_existing_tags = len(applied_tag_ids) - len(edit_tags)
    if unresolved_existing_tags:
        LOGGER.warning(
            "Could not apply In Review tag to Trial application forum "
            "thread %s; unresolved_existing_tags=%s",
            thread_id,
            unresolved_existing_tags,
        )
        return
    if len(edit_tags) >= 5:
        LOGGER.warning(
            "Could not apply In Review tag to Trial application forum "
            "thread %s; existing_tags=%s tag_limit=5",
            thread_id,
            len(edit_tags),
        )
        return

    LOGGER.debug(
        "Applying In Review tag to Trial application forum thread %s; "
        "existing_tags=%s",
        thread_id,
        len(edit_tags),
    )
    try:
        await thread.edit(
            applied_tags=[*edit_tags, in_review_tag],
            reason="Automatically apply In Review tag",
        )
    except discord.DiscordException as error:
        log_discord_failure(
            "Could not apply In Review tag to Trial application forum thread %s",
            error,
            thread_id,
        )
        return
    LOGGER.debug(
        "Applied In Review tag to Trial application forum thread %s; "
        "tag_count=%s",
        thread_id,
        len(edit_tags) + 1,
    )

async def resolve_trial_forum_tags(
    bot: Gw2Bot,
    thread: discord.Thread,
    tag_ids: set[int],
) -> dict[int, discord.ForumTag]:
    parent = getattr(thread, "parent", None)
    tags = forum_tags_for_ids(parent, tag_ids)
    missing_tag_ids = tag_ids - set(tags)
    if not missing_tag_ids:
        LOGGER.debug(
            "Resolved %s Trial application forum tags from thread parent cache",
            len(tags),
        )
        return tags

    LOGGER.debug(
        "Trial application forum tag metadata missing from cache; "
        "missing_tags=%s",
        len(missing_tag_ids),
    )
    try:
        forum = await bot.fetch_channel(TRIAL_FORUM_CHANNEL_ID)
    except discord.DiscordException as error:
        log_discord_failure(
            "Could not fetch Trial application forum while resolving %s tag IDs",
            error,
            len(missing_tag_ids),
        )
        return tags

    fetched_tags = forum_tags_for_ids(forum, missing_tag_ids)
    tags.update(fetched_tags)
    LOGGER.debug(
        "Resolved %s Trial application forum tags from fetched forum; "
        "unresolved_tags=%s",
        len(fetched_tags),
        len(tag_ids - set(tags)),
    )
    return tags


async def refresh_trial_forum_index(
    bot: Gw2Bot,
    forum: discord.ForumChannel,
) -> None:
    cached = bot._raffle_store.get_trial_forum_index()
    watermark = bot._raffle_store.get_trial_forum_watermark()
    run_start = datetime.now(UTC)
    threshold = (
        watermark - TRIAL_FORUM_INDEX_GRACE if watermark is not None else None
    )
    cold_build = threshold is None

    upserts: list[TrialForumPost] = []
    deletions: set[int] = set()
    enumerated = 0
    indexed = 0
    reused = 0
    completed = True

    def thread_last_activity(thread: Any) -> datetime:
        candidates: list[datetime] = []
        last_message_id = safe_int(getattr(thread, "last_message_id", None))
        if last_message_id:
            candidates.append(discord.utils.snowflake_time(last_message_id))
        for attribute in ("archive_timestamp", "created_at"):
            value = getattr(thread, attribute, None)
            if isinstance(value, datetime):
                candidates.append(value)
        if not candidates:
            return run_start
        return max(candidate.astimezone(UTC) for candidate in candidates)

    async def index_thread(thread: Any) -> None:
        nonlocal indexed, reused, completed
        thread_id = safe_int(getattr(thread, "id", None))
        if thread_id is None:
            return
        if getattr(thread, "parent_id", None) != getattr(forum, "id", None):
            return
        if TRIAL_ACCEPTED_TAG_ID not in thread_applied_tag_ids(thread):
            if thread_id in cached:
                deletions.add(thread_id)
            return
        last_activity = thread_last_activity(thread)
        existing = cached.get(thread_id)
        if (
            existing is not None
            and threshold is not None
            and last_activity < threshold
        ):
            reused += 1
            return
        owner_id = safe_int(getattr(thread, "owner_id", None))
        content_parts = [str(getattr(thread, "name", ""))]
        try:
            async for message in thread.history(limit=None, oldest_first=True):
                content_parts.append(str(getattr(message, "content", "")))
        except discord.DiscordException as error:
            completed = False
            log_discord_failure(
                "Could not index Trial application forum thread %s",
                error,
                thread_id,
            )
            return
        upserts.append(
            TrialForumPost(
                thread_id=thread_id,
                owner_id=owner_id,
                normalized_content="\n".join(content_parts).casefold(),
                last_activity=last_activity.isoformat(),
            )
        )
        indexed += 1

    try:
        active_threads = await forum.guild.active_threads()
    except discord.DiscordException as error:
        completed = False
        log_discord_failure(
            "Could not enumerate active Trial application threads",
            error,
        )
        active_threads = []
    for thread in active_threads:
        enumerated += 1
        await index_thread(thread)

    try:
        async for thread in forum.archived_threads(limit=None):
            if not cold_build and threshold is not None:
                archive_ts = getattr(thread, "archive_timestamp", None)
                if (
                    isinstance(archive_ts, datetime)
                    and archive_ts.astimezone(UTC) < threshold
                ):
                    break
            enumerated += 1
            await index_thread(thread)
    except discord.DiscordException as error:
        completed = False
        log_discord_failure(
            "Could not enumerate archived Trial application threads",
            error,
        )
    except AttributeError:
        completed = False
        LOGGER.error("Could not enumerate archived Trial application threads")

    bot._raffle_store.upsert_trial_forum_posts(upserts)
    bot._raffle_store.delete_trial_forum_posts(deletions)
    if completed:
        bot._raffle_store.set_trial_forum_watermark(run_start)
    LOGGER.debug(
        "Trial forum index refreshed; enumerated=%s indexed=%s reused=%s "
        "deleted=%s cold_build=%s completed=%s",
        enumerated,
        indexed,
        reused,
        len(deletions),
        cold_build,
        completed,
    )
