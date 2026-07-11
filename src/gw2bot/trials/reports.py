from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

import aiohttp
import discord

from gw2bot.discord_utils import log_discord_failure
from gw2bot.guild_members import (
    SUNBORNE_DISCORD_STATUS,
    TRIAL_WARNING_MARK_HEADER,
    TRIAL_WARNING_PENDING_HEADER,
    TrialMemberReportEntry,
    filter_sunborne_discord_entries,
    format_before_mark_trial_report,
    format_overdue_trial_report,
    get_overdue_trial_members,
    get_recent_trial_members,
    partition_tracked_overdue_members,
    seconds_until_trial_report,
    select_pending_warning_members,
    select_warned_overdue_members,
)
from gw2bot.trials.forum import TRIAL_FORUM_CHANNEL_ID

if TYPE_CHECKING:
    from gw2bot.bot import Gw2Bot

LOGGER = logging.getLogger(__name__)

TRIAL_ROLE_ID = 1450164501696741597
SUNBORNE_ROLE_ID = 1317140660188352584


def format_track_audit(
    username: str,
    discord_user_id: int,
    *,
    tracked: bool,
) -> str:
    verb = "tracked" if tracked else "untracked"
    return f"{username} warning {verb} by <@{discord_user_id}>"


def get_trial_member_discord_status(member: Any) -> str | None:
    role_ids = {role.id for role in getattr(member, "roles", ())}
    if SUNBORNE_ROLE_ID in role_ids:
        return "Sunborne"
    if TRIAL_ROLE_ID in role_ids:
        return "Trial"
    return None


def contains_normalized_account_name(value: object, key: str) -> bool:
    normalized = str(value).strip().casefold()
    return (
        re.search(
            rf"(?<![\w.]){re.escape(key)}(?![\w.])",
            normalized,
        )
        is not None
    )


async def poll_overdue_trials(bot: Gw2Bot) -> None:
    await bot.wait_until_ready()
    LOGGER.debug("Trial Members poller started")
    while not bot.is_closed():
        delay = seconds_until_trial_report(datetime.now(UTC))
        LOGGER.debug("Trial Members poll scheduled in %s seconds", delay)
        await asyncio.sleep(delay)
        if bot.is_closed():
            return

        LOGGER.debug("Starting Trial Members poll")
        try:
            delivered = await bot._check_overdue_trials()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            bot._poll_status.record_error("Trial Members", exc)
        else:
            bot._poll_status.record_success("Trial Members")
            LOGGER.debug(
                "Trial Members poll completed; delivered=%s",
                delivered,
            )

async def build_trial_report_messages(
    bot: Gw2Bot,
    now: datetime | None = None,
) -> list[str]:
    if bot._api is None:
        raise RuntimeError("GW2 API client was not initialized")
    now = now or datetime.now(UTC)
    members = await bot._api.get_guild_members(bot._config.gw2_guild_id)
    overdue = get_overdue_trial_members(members, now)
    recent = get_recent_trial_members(members, now)
    tracked_times = bot.get_tracked_trial_member_times()
    untracked_overdue, tracked_overdue, stale_tracked = (
        partition_tracked_overdue_members(overdue, set(tracked_times))
    )
    for username in stale_tracked:
        bot.untrack_trial_member(username)
    tracked_entries = await bot._resolve_trial_member_discord_statuses(
        tracked_overdue
    )
    # Tracked members who reached Sunborne in Discord no longer need their
    # warning; untrack them and return them to the past-14-day report so
    # the in-game rank-up is not forgotten.
    promoted_entries = filter_sunborne_discord_entries(tracked_entries)
    for entry in promoted_entries:
        bot.untrack_trial_member(entry.username)
    still_tracked_entries = [
        entry
        for entry in tracked_entries
        if entry.discord_status != SUNBORNE_DISCORD_STATUS
    ]
    still_tracked = [entry.username for entry in still_tracked_entries]
    warned_overdue = select_warned_overdue_members(
        still_tracked,
        tracked_times,
        now,
    )
    pending_deadlines = select_pending_warning_members(
        still_tracked,
        tracked_times,
        now,
    )
    LOGGER.debug(
        "Found %s overdue (%s tracked, %s untracked after Discord rank-up, "
        "%s inside warning window, %s past 7-day warning) and %s recent "
        "Trial members from %s guild members; auto_untracked=%s",
        len(overdue),
        len(tracked_overdue),
        len(promoted_entries),
        len(pending_deadlines),
        len(warned_overdue),
        len(recent),
        len(members),
        len(stale_tracked),
    )
    recent_entries = await bot._resolve_trial_member_discord_statuses(recent)
    before_mark_entries = filter_sunborne_discord_entries(recent_entries)
    overdue_entries = (
        await bot._resolve_trial_member_discord_statuses(untracked_overdue)
        + promoted_entries
    )
    entries_by_username = {
        entry.username: entry for entry in still_tracked_entries
    }
    pending_entries = [
        replace(entries_by_username[username], warning_deadline=deadline)
        for username, deadline in pending_deadlines.items()
    ]
    warning_entries = [
        entries_by_username[username] for username in warned_overdue
    ]
    messages = (
        format_before_mark_trial_report(before_mark_entries)
        + format_overdue_trial_report(overdue_entries)
        + format_overdue_trial_report(
            pending_entries,
            header=TRIAL_WARNING_PENDING_HEADER,
        )
        + format_overdue_trial_report(
            warning_entries,
            header=TRIAL_WARNING_MARK_HEADER,
        )
    )
    LOGGER.debug("Formatted Trial report into %s messages", len(messages))
    return messages

async def check_overdue_trials(bot: Gw2Bot, now: datetime | None = None) -> bool:
    messages = await bot._build_trial_report_messages(now)
    for message in messages:
        if not await bot._try_send_notification(message):
            return False
    return True


async def resolve_trial_member_discord_statuses(
    bot: Gw2Bot,
    usernames: list[str],
) -> list[TrialMemberReportEntry]:
    entries = [TrialMemberReportEntry(username) for username in usernames]
    unresolved = {username.casefold(): username for username in usernames}
    if not unresolved:
        return entries

    LOGGER.debug("Resolving %s Trial members from application forum", len(unresolved))
    try:
        forum = await bot.fetch_channel(TRIAL_FORUM_CHANNEL_ID)
    except discord.DiscordException as error:
        log_discord_failure("Could not access the Trial application forum", error)
        return entries
    if not hasattr(forum, "archived_threads") or not hasattr(forum, "guild"):
        LOGGER.error(
            "Trial application channel %s is not a forum channel",
            TRIAL_FORUM_CHANNEL_ID,
        )
        return entries
    forum = cast(discord.ForumChannel, forum)

    await bot._refresh_trial_forum_index(forum)
    index = bot._raffle_store.get_trial_forum_index()
    LOGGER.debug(
        "Matching %s unresolved Trial members against %s indexed forum posts",
        len(unresolved),
        len(index),
    )

    resolved: dict[str, TrialMemberReportEntry] = {}
    owner_statuses: dict[int, str | None] = {}

    async def resolve_owner_status(owner_id: int) -> str | None:
        if owner_id in owner_statuses:
            return owner_statuses[owner_id]

        status: str | None = None
        get_member = getattr(forum.guild, "get_member", None)
        if callable(get_member):
            status = get_trial_member_discord_status(get_member(owner_id))
        if status is None:
            LOGGER.debug(
                "Fetching role data for matched Trial application creator %s",
                owner_id,
            )
            try:
                member = await forum.guild.fetch_member(owner_id)
            except discord.NotFound:
                LOGGER.debug(
                    "Trial application creator %s is no longer a guild member",
                    owner_id,
                )
            except discord.DiscordException as error:
                log_discord_failure(
                    "Could not resolve Trial application creator %s",
                    error,
                    owner_id,
                )
            else:
                status = get_trial_member_discord_status(member)

        owner_statuses[owner_id] = status
        LOGGER.debug(
            "Resolved creator %s status=%s",
            owner_id,
            status or "unknown",
        )
        return status

    for post in sorted(index.values(), key=lambda entry: entry.thread_id):
        if not unresolved:
            break
        if post.owner_id is None:
            continue
        matched_keys = [
            key
            for key in unresolved
            if contains_normalized_account_name(post.normalized_content, key)
        ]
        if not matched_keys:
            continue
        owner_status = await resolve_owner_status(post.owner_id)
        for key in matched_keys:
            resolved[key] = TrialMemberReportEntry(
                unresolved[key],
                discord_user_id=post.owner_id,
                discord_status=owner_status,
            )
            del unresolved[key]
        LOGGER.debug(
            "Trial forum index post %s resolved %s usernames; remaining=%s",
            post.thread_id,
            len(matched_keys),
            len(unresolved),
        )

    LOGGER.debug(
        "Forum index resolution completed; resolved=%s unresolved=%s",
        len(resolved),
        len(unresolved),
    )
    return [resolved.get(entry.username.casefold(), entry) for entry in entries]
