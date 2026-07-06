from types import SimpleNamespace

import discord

from gw2bot.raffle import RaffleTotal

"""Shared builders for fake GW2 guild-log events used across test modules."""


def gold_deposit(
    event_id: int,
    username: str = "Username.1234",
    coins: int = 10_000,
    event_time: str = "2026-06-07T06:26:17.000Z",
) -> dict[str, object]:
    return {
        "id": event_id,
        "time": event_time,
        "type": "stash",
        "user": username,
        "operation": "deposit",
        "coins": coins,
        "item_id": 0,
        "count": 0,
    }


def guild_leave(
    event_id: int,
    username: str = "Username.1234",
) -> dict[str, object]:
    return {
        "id": event_id,
        "time": "2026-06-07T06:26:17.000Z",
        "type": "kick",
        "user": username,
        "kicked_by": username,
    }


def guild_kick(
    event_id: int,
    username: str = "Kicked.1234",
    kicked_by: str = "Officer.5678",
) -> dict[str, object]:
    return {
        "id": event_id,
        "time": "2026-06-07T06:26:17.000Z",
        "type": "kick",
        "user": username,
        "kicked_by": kicked_by,
    }


def guild_join(
    event_id: int,
    username: str = "Username.1234",
) -> dict[str, object]:
    return {
        "id": event_id,
        "time": "2026-06-07T06:26:17.000Z",
        "type": "joined",
        "user": username,
    }


def guild_invite(
    event_id: int,
    username: str = "Invited.1234",
    invited_by: str = "Officer.5678",
) -> dict[str, object]:
    return {
        "id": event_id,
        "time": "2026-06-07T06:26:17.000Z",
        "type": "invited",
        "user": username,
        "invited_by": invited_by,
    }


def guild_rank_change(
    event_id: int,
    username: str = "Member.1234",
    old_rank: str = "Trial",
    new_rank: str = "Sunborne",
    changed_by: str = "Officer.5678",
) -> dict[str, object]:
    return {
        "id": event_id,
        "time": "2026-06-07T06:26:17.000Z",
        "type": "rank_change",
        "user": username,
        "old_rank": old_rank,
        "new_rank": new_rank,
        "changed_by": changed_by,
    }

def forbidden_error(code: int) -> discord.Forbidden:
    response = SimpleNamespace(status=403, reason="Forbidden")
    return discord.Forbidden(
        response,  # type: ignore[arg-type]
        {"code": code, "message": "Missing Access"},
    )


def not_found_error() -> discord.NotFound:
    response = SimpleNamespace(status=404, reason="Not Found")
    return discord.NotFound(
        response,  # type: ignore[arg-type]
        {"code": 10007, "message": "Unknown Member"},
    )


def raffle_total(
    username: str,
    *,
    purchased: int = 0,
    free: int = 0,
) -> RaffleTotal:
    return RaffleTotal(
        username=username,
        coins_deposited=purchased * 10_000,
        raffle_tickets=purchased + free,
        gold_raffle_tickets=purchased,
        manual_raffle_tickets=free,
    )
