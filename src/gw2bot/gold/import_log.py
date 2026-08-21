from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from gw2bot.gold.models import DEPOSIT, WITHDRAW, GoldImportResult, GoldLedgerEntry
from gw2bot.guild_stash import stash_coin_balance
from gw2bot.raffle.events import parse_stash_coin_movement
from gw2bot.raffle.models import format_gold

if TYPE_CHECKING:
    from gw2bot.bot import Gw2Bot

LOGGER = logging.getLogger(__name__)


async def import_gold_history(bot: Gw2Bot) -> GoldImportResult:
    """Recover the guild bank's history from the guild log and one reading.

    The bot only started keeping coin movements when this feature shipped, and
    the guild log poller sets its cursor to the newest event on its very first
    pass, so everything before that was seen and thrown away. Both halves of
    the history are still recoverable, because the guild log itself keeps
    roughly the latest hundred events of each type and ``/v2/guild/:id/stash``
    says what the bank holds right now.

    So this reads the whole log rather than the slice after the cursor, stores
    every coin movement in it, and logs the current balance beside them. The
    reversal that turns those two into a history happens later, when the page
    is drawn: :func:`gw2bot.gold.history.build_gold_series` walks the
    movements backwards from the observed balance, subtracting each one to
    recover the balance that stood before it.

    Nothing here is destructive and nothing is counted twice. Rows are keyed
    by the guild log's own event ids, so a second run writes nothing new, and
    an event the poller has already recorded is left exactly as it stands -
    including whether its notification was sent. Imported rows are marked as
    already announced, because a withdrawal from months ago is history rather
    than news.
    """
    guild_id = bot._config.gw2_guild_id
    if bot._api is None or guild_id is None:
        raise RuntimeError("GW2 API client was not initialized")

    LOGGER.debug("Starting the guild bank history import")
    # Without ``since`` the log answers with everything it still holds, which
    # is the whole point: the cursor names where the poller got to, and this
    # is meant to reach behind it.
    events = await bot._api.get_guild_log(guild_id)
    # Both reads happen before either write. Storing the movements first and
    # then failing to read the stash would leave the command telling the
    # officer that nothing was imported while a partial import stood in the
    # database - and that is the sentence they would decide whether to retry
    # from. The stash reading is also timestamped here rather than after the
    # writes, because what the anchor records is when the balance was
    # observed, not when the row happened to be written.
    stash = await bot._api.get_guild_stash(guild_id)
    coins = stash_coin_balance(stash)
    observed_at = time.time()

    entries: list[GoldLedgerEntry] = []
    for event in events:
        movement = parse_stash_coin_movement(event)
        if movement is not None:
            entries.append(movement)
    imported = bot._raffle_store.import_gold_movements(entries)
    balance_recorded = bot._raffle_store.record_stash_balance(coins, observed_at)

    result = GoldImportResult(
        fetched=len(events),
        matched=len(entries),
        imported=imported,
        duplicates=len(entries) - imported,
        balance_coins=coins,
        balance_recorded=balance_recorded,
    )
    LOGGER.info(
        "Guild bank history import finished; fetched=%s matched=%s "
        "imported=%s duplicates=%s deposits=%s withdrawals=%s "
        "balance_recorded=%s",
        result.fetched,
        result.matched,
        result.imported,
        result.duplicates,
        sum(1 for entry in entries if entry.operation == DEPOSIT),
        sum(1 for entry in entries if entry.operation == WITHDRAW),
        result.balance_recorded,
    )
    return result


def format_import_result(result: GoldImportResult) -> str:
    """What the officer who ran the import is told about it."""
    lines = [
        "**Guild bank history import**",
        f"Read {result.fetched} guild log event(s) and found "
        f"{result.matched} coin movement(s).",
        f"Imported {result.imported} movement(s) into the gold history.",
    ]
    if result.duplicates:
        lines.append(
            f"{result.duplicates} were already recorded and were left alone."
        )
    if result.balance_coins is not None:
        balance = format_gold(result.balance_coins)
        lines.append(
            f"The guild bank holds {balance} gold, which is what the "
            "history is measured back from."
        )
    lines.append(
        "The guild log only keeps its most recent events, so anything older "
        "than that cannot be recovered. Running the import again is safe and "
        "adds only what is new."
    )
    return "\n".join(lines)
