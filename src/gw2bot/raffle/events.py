from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from gw2bot.gold.models import DEPOSIT, WITHDRAW, GoldLedgerEntry
from gw2bot.raffle.models import (
    COPPER_PER_GOLD,
    GoldWithdrawal,
    GuildInvite,
    GuildJoin,
    GuildLeave,
    GuildRankChange,
    RaffleDeposit,
)

# The two stash operations that move coins in and out of the guild bank. A
# third, ``move``, shuffles items between vault tabs and never carries coins.
STASH_DEPOSIT = "deposit"
STASH_WITHDRAW = "withdraw"


def parse_gold_deposit(event: dict[str, Any]) -> RaffleDeposit | None:
    coins = int(event.get("coins", 0))
    if (
        event.get("type") != "stash"
        or event.get("operation") != STASH_DEPOSIT
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


def parse_gold_withdrawal(event: dict[str, Any]) -> GoldWithdrawal | None:
    """Read one guild-log event as coins leaving the guild bank.

    The mirror of :func:`parse_gold_deposit`: a ``stash`` event carrying coins,
    with the operation the other way round. An item withdrawal is a ``stash``
    event too and reports ``coins`` as zero, so the amount is what tells the
    two apart.
    """
    coins = int(event.get("coins", 0))
    if (
        event.get("type") != "stash"
        or event.get("operation") != STASH_WITHDRAW
        or not event.get("user")
        or coins <= 0
    ):
        return None

    return GoldWithdrawal(
        event_id=int(event["id"]),
        username=str(event["user"]),
        coins_withdrawn=coins,
        event_time=str(event.get("time", "")),
    )


def parse_stash_coin_movement(
    event: dict[str, Any],
) -> GoldLedgerEntry | None:
    """Read one guild-log event as a movement of the guild bank's coins.

    Either direction, and unfiltered: the raffle turns some deposits away -
    an oversized Officer one buys no tickets and is never recorded as a
    deposit - but the gold still reached the bank, so the ledger this feeds
    takes every coin movement the log reports.
    """
    deposit = parse_gold_deposit(event)
    if deposit is not None:
        return GoldLedgerEntry(
            event_id=deposit.event_id,
            username=deposit.username,
            operation=DEPOSIT,
            coins=deposit.coins_deposited,
            event_time=deposit.event_time,
        )
    withdrawal = parse_gold_withdrawal(event)
    if withdrawal is not None:
        return GoldLedgerEntry(
            event_id=withdrawal.event_id,
            username=withdrawal.username,
            operation=WITHDRAW,
            coins=withdrawal.coins_withdrawn,
            event_time=withdrawal.event_time,
        )
    return None


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


def parse_event_time(event_time: str) -> datetime | None:
    """Read a stored guild-log timestamp, or None when it cannot be read.

    The GW2 API writes ``Z`` where fromisoformat wants an offset, and rows
    written by the bot itself carry a plain offset, so both spellings have to
    parse. A value that parses without a zone is read as UTC, which is what
    every producer of these strings means.
    """
    try:
        parsed = datetime.fromisoformat(event_time.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def event_in_window(event_time: str, start: datetime, end: datetime) -> bool:
    parsed_utc = parse_event_time(event_time)
    if parsed_utc is None:
        return False
    return start <= parsed_utc < end
