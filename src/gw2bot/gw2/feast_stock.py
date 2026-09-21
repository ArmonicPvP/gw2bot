from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Mapping

LOGGER = logging.getLogger(__name__)

LOW_STOCK_THRESHOLD = 10
LOW_STOCK_REMINDER_SECONDS = 8 * 60 * 60


@dataclass(frozen=True, slots=True)
class Feast:
    guild_storage_id: int
    name: str


@dataclass(frozen=True, slots=True)
class FeastAlert:
    guild_storage_id: int
    name: str
    count: int

    @property
    def message(self) -> str:
        return f"Guild Storage is low on **{self.name}**: {self.count} left"


TRACKED_FEASTS = (
    Feast(1078, "Bowl of Fruit Salad with Mint Garnish"),
    Feast(1089, "Cilantro and Cured Meat Flatbread"),
    Feast(1102, "Cilantro Lime Sous-Vide Steak"),
    Feast(1112, "Spherified Cilantro Oyster Soup"),
)

# Time windows the feast usage dashboard offers, mapped to their length in
# seconds. The keys are the values the ``/api/food?range=`` query accepts.
FEAST_USAGE_RANGES: dict[str, int] = {
    "24h": 24 * 60 * 60,
    "7d": 7 * 24 * 60 * 60,
    "30d": 30 * 24 * 60 * 60,
}


@dataclass(frozen=True, slots=True)
class FeastStockSample:
    """One recorded count for a tracked feast at a point in time.

    ``log_id`` is the stock log row this reading was written as. It is what a
    recorded restock cost is filed against, because a row id names one
    observation for good where a timestamp only names when it happened.
    """

    recorded_at: float
    count: int
    log_id: int = 0


@dataclass(frozen=True, slots=True)
class FeastStockSeries:
    """A tracked feast's recorded counts within a window.

    ``samples`` are the counts logged inside the window, oldest first.
    ``prior_count`` is the last count recorded strictly before the window,
    or ``None`` when the feast has no earlier record, so a decrease that
    straddles the window's start edge is still attributable.
    """

    guild_storage_id: int
    prior_count: int | None
    samples: tuple[FeastStockSample, ...]


@dataclass(frozen=True, slots=True)
class FeastRemoval:
    """A single observed drop in a tracked feast's on-hand count."""

    recorded_at: float
    amount: int
    remaining: int


@dataclass(frozen=True, slots=True)
class FeastAddition:
    """A single observed rise in a tracked feast's on-hand count.

    ``previous_at`` is when the reading it rose from was taken, or ``None``
    when that reading predates the window being drawn. It is the open edge of
    the stretch a guild-log deposit has to fall in to be the one that put the
    feasts there: the poll sees the new count some time after the deposit
    itself, never before it.
    """

    log_id: int
    recorded_at: float
    previous_at: float | None
    amount: int
    remaining: int


@dataclass(frozen=True, slots=True)
class FeastDeposit:
    """One guild-log event placing tracked feasts into Guild Storage.

    ``event_time`` is the guild log's own text, kept as written the way every
    other event the bot stores is; :class:`FeastRestock` is the same deposit
    once it has been placed in time.
    """

    event_id: int
    guild_storage_id: int
    username: str
    count: int
    event_time: str


@dataclass(frozen=True, slots=True)
class FeastRestock:
    """One stored feast deposit, placed at a moment in time."""

    occurred_at: float
    guild_storage_id: int
    username: str
    count: int


# The largest cost an officer may record against one restock, in copper. Two
# million gold is the most an account can hold, so nothing above it can have
# been paid; the ceiling exists so a mistyped figure is refused at the edge
# rather than stored and drawn.
MAX_FEAST_COST_COPPER = 2_000_000 * 10_000


def feast_removals(series: FeastStockSeries) -> list[FeastRemoval]:
    """Return each in-window count decrease as a removal, oldest first.

    A removal is a sample whose count fell below the previous recorded count;
    ``amount`` is how far it fell and ``remaining`` is the new on-hand count.
    The comparison spans ``series.prior_count`` so a decrease across the
    window's start edge is still reported. Restocks (increases) and unchanged
    samples produce no removal.
    """
    removals: list[FeastRemoval] = []
    previous = series.prior_count
    for sample in series.samples:
        if previous is not None and sample.count < previous:
            removals.append(
                FeastRemoval(
                    recorded_at=sample.recorded_at,
                    amount=previous - sample.count,
                    remaining=sample.count,
                )
            )
        previous = sample.count
    return removals


def feast_additions(series: FeastStockSeries) -> list[FeastAddition]:
    """Return each in-window count increase as an addition, oldest first.

    The mirror of :func:`feast_removals`: an addition is a sample whose count
    rose above the previous recorded count, ``amount`` is how far it rose and
    ``remaining`` is the new on-hand count. The comparison spans
    ``series.prior_count`` the same way, so a restock straddling the window's
    start edge is still reported.

    A feast's very first recorded count has nothing to have risen from, so it
    is not an addition: the shelf was not observed filling, it was observed
    for the first time.
    """
    additions: list[FeastAddition] = []
    previous = series.prior_count
    previous_at: float | None = None
    for sample in series.samples:
        if previous is not None and sample.count > previous:
            additions.append(
                FeastAddition(
                    log_id=sample.log_id,
                    recorded_at=sample.recorded_at,
                    previous_at=previous_at,
                    amount=sample.count - previous,
                    remaining=sample.count,
                )
            )
        previous = sample.count
        previous_at = sample.recorded_at
    return additions


def depositors_for_addition(
    addition: FeastAddition,
    restocks: Sequence[FeastRestock],
    window_since: float,
) -> list[str]:
    """Return who deposited the feasts one addition counted, in log order.

    ``restocks`` are that feast's own deposits, oldest first. A poll reads the
    new count some time after the deposit that raised it, so the deposits
    explaining an addition are the ones logged between the previous reading
    and this one. ``window_since`` stands in for that previous reading when it
    predates the window, which is the only stretch the deposits were loaded
    for.

    An addition nobody can be named for comes back empty rather than guessed
    at: an unattributed restock is exactly what the page marks for an officer
    to look at.
    """
    opened = (
        window_since if addition.previous_at is None else addition.previous_at
    )
    named: list[str] = []
    for restock in restocks:
        if not opened < restock.occurred_at <= addition.recorded_at:
            continue
        if restock.username not in named:
            named.append(restock.username)
    return named


def tracked_feast_counts(storage: list[dict[str, Any]]) -> dict[int, int]:
    """Return the on-hand count for each tracked feast present in ``storage``.

    The guild storage endpoint reports a genuinely empty consumable as a count
    of ``0``, so a tracked feast that is *absent* from the response means its
    count is unknown (e.g. a partial response), not that stock is empty. Such
    feasts are omitted here so callers ignore them rather than treating a missing
    feast as ``0``.
    """
    tracked_ids = {feast.guild_storage_id for feast in TRACKED_FEASTS}
    return {
        int(entry["id"]): int(entry["count"])
        for entry in storage
        if "id" in entry
        and "count" in entry
        and int(entry["id"]) in tracked_ids
    }


def changed_feast_counts(
    current: Mapping[int, int],
    previous: Mapping[int, int],
) -> dict[int, int]:
    """Return only the ``current`` counts that differ from ``previous``.

    Feasts absent from ``previous`` are treated as changed so a first
    observation is always recorded.
    """
    return {
        guild_storage_id: count
        for guild_storage_id, count in current.items()
        if previous.get(guild_storage_id) != count
    }


def get_due_low_stock_alerts(
    counts: Mapping[int, int],
    last_alerted_at: Mapping[int, float],
    now: float,
) -> tuple[list[FeastAlert], set[int]]:
    """Return the feast alerts that are due, plus the currently-low feast ids.

    ``counts`` should hold only the tracked feasts observed in the latest poll
    (see :func:`tracked_feast_counts`); a tracked feast missing from it is
    ignored rather than assumed empty.
    """
    currently_low = {
        feast.guild_storage_id
        for feast in TRACKED_FEASTS
        if feast.guild_storage_id in counts
        and counts[feast.guild_storage_id] <= LOW_STOCK_THRESHOLD
    }
    alerts = [
        FeastAlert(
            guild_storage_id=feast.guild_storage_id,
            name=feast.name,
            count=counts[feast.guild_storage_id],
        )
        for feast in TRACKED_FEASTS
        if feast.guild_storage_id in currently_low
        and (
            feast.guild_storage_id not in last_alerted_at
            or now - last_alerted_at[feast.guild_storage_id]
            >= LOW_STOCK_REMINDER_SECONDS
        )
    ]
    LOGGER.debug(
        "Evaluated feast stock; tracked_present=%s tracked=%s low=%s alerts=%s",
        len(counts),
        len(TRACKED_FEASTS),
        len(currently_low),
        len(alerts),
    )
    return alerts, currently_low
