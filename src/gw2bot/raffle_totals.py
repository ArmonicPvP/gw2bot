from __future__ import annotations

import os

from gw2bot.raffle import RaffleStore, format_gold


def main() -> None:
    # RAFFLE_DB_PATH is still an environment variable; the guild id is not.
    # The ledger already records which guild it belongs to, and reading totals
    # out of it needs no guild id at all, so this asks for nothing that the
    # migration told the operator to delete.
    database_path = os.getenv("RAFFLE_DB_PATH", "data/gw2bot.db")

    store = RaffleStore(database_path, None)
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
