from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

TRIAL_RANK = "Trial"
TRIAL_PERIOD = timedelta(days=14)
TRIAL_REPORT_HOUR_UTC = 17
DISCORD_MESSAGE_LIMIT = 2_000


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
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    async def resolve(self, username: str) -> str | None:
        await self._refresh_if_expired()
        return self._members.get(username.strip().casefold())

    async def _refresh_if_expired(self) -> None:
        if self._clock() < self._expires_at:
            return

        async with self._lock:
            if self._clock() < self._expires_at:
                return
            members = await self._api.get_guild_members(self._guild_id)
            self._members = {
                str(member["name"]).casefold(): str(member["name"])
                for member in members
                if member.get("name")
            }
            self._expires_at = self._clock() + self._ttl_seconds


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
    return sorted(overdue, key=str.casefold)


def format_overdue_trial_report(usernames: list[str]) -> list[str]:
    if not usernames:
        return []

    header = (
        "**Trial members past the 14-day mark**\n"
        "Please confirm whether these users have completed the challenges "
        "and can be ranked up to Sunborne:\n"
    )
    messages: list[str] = []
    current = header
    for username in usernames:
        line = f"- {username}\n"
        if len(current) + len(line) > DISCORD_MESSAGE_LIMIT:
            messages.append(current.rstrip())
            current = header
        current += line
    messages.append(current.rstrip())
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
