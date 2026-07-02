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
