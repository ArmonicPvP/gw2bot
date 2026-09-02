from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

import aiohttp
import discord

from gw2bot.discord_utils import log_discord_failure
from gw2bot.guild_members import (
    SUNBORNE_DISCORD_STATUS,
    TRIAL_WARNING_MARK_HEADER,
    TrialMemberReportEntry,
    filter_sunborne_discord_entries,
    format_before_mark_trial_report,
    format_overdue_trial_report,
    get_overdue_trial_members,
    get_recent_trial_members,
    partition_tracked_overdue_members,
    seconds_until_trial_report,
    select_warned_overdue_members,
)

if TYPE_CHECKING:
    from gw2bot.bot import Gw2Bot

LOGGER = logging.getLogger(__name__)



def format_track_audit(
    username: str,
    discord_user_id: int,
    *,
    tracked: bool,
) -> str:
    verb = "tracked" if tracked else "untracked"
    return f"{username} warning {verb} by <@{discord_user_id}>"


def get_trial_member_discord_status(
    member: Any,
    trial_role_id: int,
    sunborne_role_id: int,
) -> str | None:
    role_ids = {role.id for role in getattr(member, "roles", ())}
    if sunborne_role_id in role_ids:
        return "Sunborne"
    if trial_role_id in role_ids:
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
    guild_id = bot._config.gw2_guild_id
    if bot._api is None or guild_id is None:
        raise RuntimeError("GW2 API client was not initialized")
    now = now or datetime.now(UTC)
    members = await bot._api.get_guild_members(guild_id)
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
    # Every still-tracked member the warning mark has not caught up with is
    # inside the grace window, and so is reported nowhere. Counting the
    # omission keeps that outcome traceable now that no report shows it.
    inside_warning_window = len(still_tracked) - len(warned_overdue)
    LOGGER.debug(
        "Found %s overdue (%s tracked, %s untracked after Discord rank-up, "
        "%s inside warning window and reported nowhere, %s past 7-day "
        "warning) and %s recent Trial members from %s guild members; "
        "auto_untracked=%s",
        len(overdue),
        len(tracked_overdue),
        len(promoted_entries),
        inside_warning_window,
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
    warning_entries = [
        entries_by_username[username] for username in warned_overdue
    ]
    messages = (
        format_before_mark_trial_report(before_mark_entries)
        # Only this report groups by resolved Discord status; the warning and
        # kick lists below stay alphabetical.
        + format_overdue_trial_report(overdue_entries, group_by_status=True)
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


@dataclass(frozen=True, slots=True)
class TrialForumMatches:
    """What one pass over the application forum could establish.

    ``forum_read`` is deliberately kept beside the entries. Discord refusing
    the forum, or the channel not being one, brings every entry back unmatched
    - and an unmatched entry there proves nothing about whether the account
    ever applied. A caller that tells a reader "no application matched" has to
    be able to tell that apart from a forum that was actually searched.
    """

    entries: list[TrialMemberReportEntry]
    forum_read: bool


async def resolve_trial_member_discord_statuses(
    bot: Gw2Bot,
    usernames: list[str],
) -> list[TrialMemberReportEntry]:
    """The matched entries alone, for the reports that only list names."""
    return (await resolve_trial_forum_matches(bot, usernames)).entries


async def resolve_trial_forum_matches(
    bot: Gw2Bot,
    usernames: list[str],
    *,
    resolve_status: bool = True,
) -> TrialForumMatches:
    """Match accounts to their application posts.

    ``resolve_status`` reads each matched author's current Discord rank, which
    costs a member fetch apiece on a bot without the members intent. A caller
    that only wants the match - the pending invites do, and name the account
    themselves - passes False and gets entries without a status.
    """
    forum_channel_id = bot._config.trial_forum_channel_id
    trial_role_id = bot._config.trial_role_id
    sunborne_role_id = bot._config.sunborne_role_id
    entries = [TrialMemberReportEntry(username) for username in usernames]
    unresolved = {username.casefold(): username for username in usernames}
    if not unresolved:
        # Nothing was asked about, so nothing is unmatched: an empty answer is
        # as complete as a full one.
        return TrialForumMatches(entries, True)

    LOGGER.debug("Resolving %s Trial members from application forum", len(unresolved))
    try:
        forum = await bot.fetch_channel(forum_channel_id)
    except discord.DiscordException as error:
        log_discord_failure("Could not access the Trial application forum", error)
        return TrialForumMatches(entries, False)
    if not hasattr(forum, "archived_threads") or not hasattr(forum, "guild"):
        LOGGER.error(
            "Trial application channel %s is not a forum channel",
            forum_channel_id,
        )
        return TrialForumMatches(entries, False)
    forum = cast(discord.ForumChannel, forum)

    # A refusal part-way through the walk leaves the index missing posts, so
    # a match that is absent from it proves nothing about the account.
    forum_read = await bot._refresh_trial_forum_index(forum)
    index = bot._raffle_store.get_trial_forum_index()
    LOGGER.debug(
        "Matching %s unresolved Trial members against %s indexed forum posts",
        len(unresolved),
        len(index),
    )

    resolved: dict[str, TrialMemberReportEntry] = {}
    owner_statuses: dict[int, str | None] = {}

    async def resolve_owner_status(owner_id: int) -> str | None:
        if not resolve_status:
            return None
        if owner_id in owner_statuses:
            return owner_statuses[owner_id]

        status: str | None = None
        get_member = getattr(forum.guild, "get_member", None)
        if callable(get_member):
            status = get_trial_member_discord_status(
                get_member(owner_id),
                trial_role_id,
                sunborne_role_id,
            )
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
                status = get_trial_member_discord_status(
                    member,
                    trial_role_id,
                    sunborne_role_id,
                )

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
        "Forum index resolution completed; resolved=%s unresolved=%s "
        "statuses=%s forum_read=%s",
        len(resolved),
        len(unresolved),
        resolve_status,
        forum_read,
    )
    return TrialForumMatches(
        [resolved.get(entry.username.casefold(), entry) for entry in entries],
        forum_read,
    )
