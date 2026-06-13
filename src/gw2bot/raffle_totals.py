from __future__ import annotations

import os

from gw2bot.raffle import RaffleStore, format_gold


def main() -> None:
    database_path = os.getenv("RAFFLE_DB_PATH", "data/gw2bot.db")
    guild_id = os.environ.get("GW2_GUILD_ID")
    if guild_id is None:
        raise RuntimeError("GW2_GUILD_ID must be set to read raffle totals")

    store = RaffleStore(database_path, guild_id)
    try:
        totals = sorted(
            store.get_totals(),
            key=lambda total: (
                -total.raffle_tickets,
                total.username.casefold(),
                total.username,
            ),
        )
    finally:
        store.close()

    if not totals:
        print("No raffle totals recorded.")
        return

    for total in totals:
        print(
            f"{total.username}: {total.raffle_tickets} current tickets, "
            f"{format_gold(total.coins_deposited)} lifetime gold"
        )


if __name__ == "__main__":
    main()
