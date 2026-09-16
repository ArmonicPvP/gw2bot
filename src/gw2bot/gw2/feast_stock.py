from __future__ import annotations

import logging
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
    """One recorded count for a tracked feast at a point in time."""

    recorded_at: float
    count: int


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
