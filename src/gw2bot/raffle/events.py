from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from gw2bot.raffle.models import (
    COPPER_PER_GOLD,
    GuildInvite,
    GuildJoin,
    GuildLeave,
    GuildRankChange,
    RaffleDeposit,
)


def parse_gold_deposit(event: dict[str, Any]) -> RaffleDeposit | None:
    coins = int(event.get("coins", 0))
    if (
        event.get("type") != "stash"
        or event.get("operation") != "deposit"
        or not event.get("user")
        or coins <= 0
    ):
        return None

    return RaffleDeposit(
        event_id=int(event["id"]),
        username=str(event["user"]),
        coins_deposited=coins,
        raffle_tickets=coins // COPPER_PER_GOLD,
        event_time=str(event.get("time", "")),
    )


def parse_guild_leave(event: dict[str, Any]) -> GuildLeave | None:
    if not event.get("user"):
        return None
    if event.get("type") not in {"kick", "left"}:
        return None
    username = str(event["user"])
    kicked_by_raw = event.get("kicked_by")
    # GW2 reports a voluntary departure as a self-kick.
    kicked_by = (
        str(kicked_by_raw)
        if kicked_by_raw and str(kicked_by_raw) != username
        else None
    )
    return GuildLeave(
        event_id=int(event["id"]),
        username=username,
        event_time=str(event.get("time", "")),
        kicked_by=kicked_by,
    )


def parse_guild_join(event: dict[str, Any]) -> GuildJoin | None:
    if event.get("type") != "joined" or not event.get("user"):
        return None
    return GuildJoin(
        event_id=int(event["id"]),
        username=str(event["user"]),
        event_time=str(event.get("time", "")),
    )


def parse_guild_invite(event: dict[str, Any]) -> GuildInvite | None:
    if event.get("type") != "invited" or not event.get("user"):
        return None
    invited_by_raw = event.get("invited_by")
    invited_by = str(invited_by_raw) if invited_by_raw else None
    return GuildInvite(
        event_id=int(event["id"]),
        username=str(event["user"]),
        event_time=str(event.get("time", "")),
        invited_by=invited_by,
    )


def parse_guild_rank_change(event: dict[str, Any]) -> GuildRankChange | None:
    if event.get("type") != "rank_change" or not event.get("user"):
        return None
    changed_by_raw = event.get("changed_by")
    changed_by = str(changed_by_raw) if changed_by_raw else None
    return GuildRankChange(
        event_id=int(event["id"]),
        username=str(event["user"]),
        old_rank=str(event.get("old_rank", "")),
        new_rank=str(event.get("new_rank", "")),
        event_time=str(event.get("time", "")),
        changed_by=changed_by,
    )


def event_in_window(event_time: str, start: datetime, end: datetime) -> bool:
    try:
        parsed = datetime.fromisoformat(event_time.replace("Z", "+00:00"))
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    parsed_utc = parsed.astimezone(UTC)
    return start <= parsed_utc < end
