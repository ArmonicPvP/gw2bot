from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

LOGGER = logging.getLogger(__name__)

TRIAL_RANK = "Trial"
TRIAL_PERIOD = timedelta(days=14)
TRIAL_REPORT_HOUR_UTC = 17
DISCORD_MESSAGE_LIMIT = 2_000
BACKGROUND_REFRESH_RETRY_SECONDS = 30
SUNBORNE_DISCORD_STATUS = "Sunborne"
TRIAL_DISCORD_STATUS = "Trial"
TRIAL_REPORT_STATUS_ORDER = {
    SUNBORNE_DISCORD_STATUS: 0,
    TRIAL_DISCORD_STATUS: 1,
}
TRIAL_PAST_MARK_HEADER = (
    "**Trial members past the 14-day mark**\n"
    "Please confirm whether these users have completed the challenges "
    "and can be ranked up to Sunborne:\n"
)
TRIAL_BEFORE_MARK_HEADER = (
    "**Trial members before the 14-day mark**\n"
    "These users are still Trial in-game but already Sunborne in Discord:\n"
)


@dataclass(frozen=True, slots=True)
class TrialMemberReportEntry:
    username: str
    discord_user_id: int | None = None
    discord_status: str | None = None


class GuildMemberApi(Protocol):
    async def get_guild_members(self, guild_id: str) -> list[dict[str, Any]]: ...


class GuildMemberCache:
    def __init__(
        self,
        api: GuildMemberApi,
        guild_id: str,
        ttl_seconds: int,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._api = api
        self._guild_id = guild_id
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._members: dict[str, str] = {}
        self._member_ranks: dict[str, str] = {}
        self._expires_at = 0.0
        self._next_background_refresh_at = 0.0
        self._lock = asyncio.Lock()
        self._refresh_task: asyncio.Task[None] | None = None

    async def resolve(
        self,
        username: str,
        *,
        force_refresh: bool = False,
    ) -> str | None:
        await self._refresh_if_expired(force=force_refresh)
        result = self._members.get(username.strip().casefold())
        LOGGER.debug("Guild member cache lookup completed; matched=%s", result is not None)
        return result

    async def usernames_with_rank(
        self,
        rank: str,
        *,
        force_refresh: bool = False,
    ) -> set[str]:
        await self._refresh_if_expired(force=force_refresh)
        rank_key = rank.strip().casefold()
        results = {
            username
            for account_key, username in self._members.items()
            if self._member_ranks.get(account_key, "").casefold() == rank_key
        }
        LOGGER.debug(
            "Guild member rank cache lookup completed; matches=%s",
            len(results),
        )
        return results

    async def search(self, query: str, *, limit: int = 25) -> list[str]:
        started_at = time.perf_counter()
        query_chars = len(query.strip())
        cache_expired = self._clock() >= self._expires_at
        LOGGER.debug(
            "Guild member cache search started; query_chars=%s limit=%s "
            "cached_members=%s cache_expired=%s",
            query_chars,
            limit,
            len(self._members),
            cache_expired,
        )
        refresh_started = self.start_background_refresh()
        query_key = query.strip().casefold()
        matches = [
            username
            for username in self._members.values()
            if query_key in username.casefold()
        ]
        matches.sort(
            key=lambda username: (
                not username.casefold().startswith(query_key),
                username.casefold(),
                username,
            )
        )
        results = matches[:max(0, limit)]
        LOGGER.debug(
            "Guild member cache search completed; query_chars=%s matches=%s "
            "returned=%s background_refresh_started=%s elapsed_ms=%.3f",
            query_chars,
            len(matches),
            len(results),
            refresh_started,
            (time.perf_counter() - started_at) * 1_000,
        )
        return results

    def start_background_refresh(self) -> bool:
        now = self._clock()
        if now < self._expires_at:
            LOGGER.debug("Guild member cache background refresh not needed")
            return False
        if now < self._next_background_refresh_at:
            LOGGER.debug("Guild member cache background refresh retry delayed")
            return False
        if self._refresh_task is not None and not self._refresh_task.done():
            LOGGER.debug("Guild member cache background refresh already running")
            return False
        self._refresh_task = asyncio.create_task(
            self._refresh_in_background(),
            name="gw2-guild-member-cache-refresh",
        )
        LOGGER.debug("Started guild member cache background refresh")
        return True

    async def close(self) -> None:
        task = self._refresh_task
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self._refresh_task = None
        LOGGER.debug("Stopped guild member cache background refresh")

    async def _refresh_in_background(self) -> None:
        try:
            await self._refresh_if_expired()
        except Exception as exc:
            self._next_background_refresh_at = (
                self._clock() + BACKGROUND_REFRESH_RETRY_SECONDS
            )
            LOGGER.error(
                "Guild member cache background refresh failed; error_type=%s",
                type(exc).__name__,
            )
        finally:
            self._refresh_task = None

    async def _refresh_if_expired(self, *, force: bool = False) -> None:
        if not force and self._clock() < self._expires_at:
            LOGGER.debug("Reusing guild member cache")
            return

        async with self._lock:
            if not force and self._clock() < self._expires_at:
                LOGGER.debug("Guild member cache was refreshed by another task")
                return
            LOGGER.debug("Refreshing guild member cache")
            members = await self._api.get_guild_members(self._guild_id)
            self._members = {
                str(member["name"]).casefold(): str(member["name"])
                for member in members
                if member.get("name")
            }
            self._member_ranks = {
                str(member["name"]).casefold(): str(member.get("rank", ""))
                for member in members
                if member.get("name")
            }
            self._expires_at = self._clock() + self._ttl_seconds
            self._next_background_refresh_at = 0.0
            LOGGER.debug(
                "Guild member cache refreshed; members=%s ttl_seconds=%s",
                len(self._members),
                self._ttl_seconds,
            )


def get_overdue_trial_members(
    members: list[dict[str, Any]],
    now: datetime,
) -> list[str]:
    cutoff = now.astimezone(UTC) - TRIAL_PERIOD
    overdue: list[str] = []
    for member in members:
        if str(member.get("rank", "")).casefold() != TRIAL_RANK.casefold():
            continue
        name = str(member.get("name", "")).strip()
        joined = _parse_api_datetime(member.get("joined"))
        if name and joined is not None and joined <= cutoff:
            overdue.append(name)
    # Case-sensitive sort: uppercase sorts before lowercase ("Z" before "a").
    result = sorted(overdue)
    LOGGER.debug(
        "Evaluated %s guild members; overdue_trials=%s",
        len(members),
        len(result),
    )
    return result


def get_recent_trial_members(
    members: list[dict[str, Any]],
    now: datetime,
) -> list[str]:
    cutoff = now.astimezone(UTC) - TRIAL_PERIOD
    recent: list[str] = []
    for member in members:
        if str(member.get("rank", "")).casefold() != TRIAL_RANK.casefold():
            continue
        name = str(member.get("name", "")).strip()
        joined = _parse_api_datetime(member.get("joined"))
        if name and joined is not None and joined > cutoff:
            recent.append(name)
    # Case-sensitive sort: uppercase sorts before lowercase ("Z" before "a").
    result = sorted(recent)
    LOGGER.debug(
        "Evaluated %s guild members; recent_trials=%s",
        len(members),
        len(result),
    )
    return result


def filter_sunborne_discord_entries(
    entries: Sequence[TrialMemberReportEntry],
) -> list[TrialMemberReportEntry]:
    filtered = [
        entry
        for entry in entries
        if entry.discord_status == SUNBORNE_DISCORD_STATUS
    ]
    LOGGER.debug(
        "Filtered %s Trial entries to %s Sunborne-in-Discord entries",
        len(entries),
        len(filtered),
    )
    return filtered


def format_overdue_trial_report(
    entries: Sequence[TrialMemberReportEntry | str],
    header: str = TRIAL_PAST_MARK_HEADER,
) -> list[str]:
    if not entries:
        return []

    sorted_entries = sorted(
        (
            TrialMemberReportEntry(value)
            if isinstance(value, str)
            else value
            for value in entries
        ),
        key=lambda entry: (
            TRIAL_REPORT_STATUS_ORDER.get(entry.discord_status or "", 2),
            # Case-sensitive: uppercase sorts before lowercase ("Z" before "a").
            entry.username,
        ),
    )
    messages: list[str] = []
    current = header
    for entry in sorted_entries:
        line = f"* {entry.username}"
        if entry.discord_user_id is not None:
            line += f" - <@{entry.discord_user_id}>"
            if entry.discord_status is not None:
                line += f" - {entry.discord_status}"
        line += "\n"
        if len(current) + len(line) > DISCORD_MESSAGE_LIMIT:
            messages.append(current.rstrip())
            current = header
        current += line
    messages.append(current.rstrip())
    LOGGER.debug(
        "Formatted %s Trial report entries into %s Discord messages",
        len(sorted_entries),
        len(messages),
    )
    return messages


def seconds_until_trial_report(now: datetime) -> float:
    now_utc = now.astimezone(UTC)
    next_run = now_utc.replace(
        hour=TRIAL_REPORT_HOUR_UTC,
        minute=0,
        second=0,
        microsecond=0,
    )
    if next_run <= now_utc:
        next_run += timedelta(days=1)
    return (next_run - now_utc).total_seconds()


def _parse_api_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
