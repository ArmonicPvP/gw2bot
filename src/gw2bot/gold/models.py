from __future__ import annotations

from dataclasses import dataclass

# The two directions a coin movement can go, and how each moves the guild
# bank's balance. They are the guild log's own spellings for a stash
# operation, stored verbatim so a row reads the same as the event it came
# from; gw2bot.raffle.events names the same two constants for parsing.
DEPOSIT = "deposit"
WITHDRAW = "withdraw"

# How each direction moves the balance, in copper.
MOVEMENT_SIGNS: dict[str, int] = {DEPOSIT: 1, WITHDRAW: -1}

# Time windows the gold page offers, mapped to their length in seconds. The
# keys are the values the ``/api/gold?range=`` query accepts; they mirror the
# roster and feast usage pages so all three read the same way.
GOLD_RANGES: dict[str, int] = {
    "24h": 24 * 60 * 60,
    "7d": 7 * 24 * 60 * 60,
    "30d": 30 * 24 * 60 * 60,
}


@dataclass(frozen=True, slots=True)
class GoldMovement:
    """One deposit or withdrawal of coins, whenever it was recorded.

    ``coins`` is always positive - the amount that moved - and ``operation``
    says which way. ``delta`` is what that means for the balance.
    """

    occurred_at: float
    operation: str
    username: str
    coins: int

    @property
    def delta(self) -> int:
        return MOVEMENT_SIGNS.get(self.operation, 0) * self.coins


@dataclass(frozen=True, slots=True)
class GoldLedgerEntry:
    """One coin movement as the guild log spells it, ready to be stored.

    ``event_time`` is the log's own timestamp text rather than a parsed
    moment, so a row round-trips whatever the API said. ``event_id`` is the
    guild log's id, which is what lets the poller and the one-time import
    write the same movement without either counting it twice.
    """

    event_id: int
    username: str
    operation: str
    coins: int
    event_time: str


@dataclass(frozen=True, slots=True)
class GoldBalanceSample:
    """One observed guild stash coin balance, and when it was observed."""

    recorded_at: float
    coins: int


@dataclass(frozen=True, slots=True)
class GoldPoint:
    """One vertex of the balance line."""

    at: float
    coins: int


@dataclass(frozen=True, slots=True)
class GoldEvent:
    """One coin movement plotted on the graph.

    ``coins_after`` is the balance immediately after the movement, or ``None``
    when no balance has ever been observed and there is therefore nothing to
    measure the history against.
    """

    occurred_at: float
    operation: str
    username: str
    coins: int
    coins_after: int | None


@dataclass(frozen=True, slots=True)
class GoldSeries:
    """Everything the gold page draws for one time window.

    ``points`` are the balance line's vertices, oldest first, and empty when
    no balance has ever been observed. ``movements`` are the in-window
    deposits and withdrawals, oldest first, one dot each.
    """

    points: tuple[GoldPoint, ...]
    movements: tuple[GoldEvent, ...]

    @property
    def deposited(self) -> int:
        return sum(
            movement.coins
            for movement in self.movements
            if movement.operation == DEPOSIT
        )

    @property
    def withdrawn(self) -> int:
        return sum(
            movement.coins
            for movement in self.movements
            if movement.operation == WITHDRAW
        )

    @property
    def net(self) -> int:
        return self.deposited - self.withdrawn


@dataclass(frozen=True, slots=True)
class GoldImportResult:
    """What one run of the one-time guild log import did.

    ``balance_recorded`` says whether the stash balance read alongside the log
    became a new anchor row; it does not when the balance has not moved since
    the last observation, which is not a failure.
    """

    fetched: int
    matched: int
    imported: int
    duplicates: int
    balance_coins: int | None
    balance_recorded: bool
