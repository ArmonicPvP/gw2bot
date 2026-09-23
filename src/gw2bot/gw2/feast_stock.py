from __future__ import annotations

import logging
import math
from collections import deque
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

SECONDS_PER_DAY = 24 * 60 * 60

# How many days the dashboard's rolling averages cover, and so how far before
# a drawn window its history has to be read: an average over the last seven
# days is only that on the window's first day if the seven days before it were
# read too.
ROLLING_AVERAGE_DAYS = 7


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


def day_of(moment: float) -> float:
    """The UTC midnight that opens the day ``moment`` falls in.

    Days are cut in UTC rather than in the reader's own zone: the dashboard's
    daily figures are a property of the window the server drew, so two members
    in two zones reading the same window have to be shown the same days.
    """
    return math.floor(moment / SECONDS_PER_DAY) * SECONDS_PER_DAY


def history_start(since: float) -> float:
    """How far before a drawn window its rolling averages have to be read.

    A whole number of days before the window's own first day, so the history
    lines up with the day grid it is bucketed into rather than opening
    part-way through a day and under-counting it.
    """
    return day_of(since) - ROLLING_AVERAGE_DAYS * SECONDS_PER_DAY


@dataclass(frozen=True, slots=True)
class FeastConsumption:
    """One observed drop in a feast's count, with what the feasts that left
    had cost.

    ``cost`` is in copper, worked out first in, first out: the feasts that
    leave are the oldest still on the shelf, at the price the restock that
    put them there was recorded at. That is what makes the cost of a week the
    cost of what was eaten in it, rather than of whatever was bought in it.
    """

    recorded_at: float
    amount: int
    cost: int


def feast_consumptions(
    series: FeastStockSeries,
    costs: Mapping[int, int],
) -> list[FeastConsumption]:
    """Every drop in ``series``, each costed from the stock it drew down.

    The shelf is kept as a queue of lots, oldest first. Every rise adds a
    lot of the feasts it put there, carrying the cost recorded against it,
    and every drop takes its feasts from the front of the queue. Stock that
    was already there when the series opens - its ``prior_count``, or the
    first count ever recorded - was not seen being bought, so it is a lot
    that cost nothing and is used up first. A restock nobody has priced yet
    is a lot that costs nothing too, until an officer records what it cost.

    A lot is held as its feasts and the copper still on them rather than as a
    price per feast, and a drop that takes part of a lot takes that share of
    its copper, rounded. So a lot's copper is spent exactly by the time its
    last feast leaves, however unevenly it divides.

    The series has to reach back to the feast's first recorded count for the
    lots to be right: a lot bought before the series opens would otherwise be
    read as stock that cost nothing.
    """
    lots: deque[list[int]] = deque()
    consumptions: list[FeastConsumption] = []
    previous = series.prior_count
    if previous is not None and previous > 0:
        lots.append([previous, 0])
    for sample in series.samples:
        if previous is None:
            if sample.count > 0:
                lots.append([sample.count, 0])
        elif sample.count > previous:
            lots.append(
                [sample.count - previous, costs.get(sample.log_id, 0)]
            )
        elif sample.count < previous:
            amount = previous - sample.count
            consumptions.append(
                FeastConsumption(
                    recorded_at=sample.recorded_at,
                    amount=amount,
                    cost=_draw_down(lots, amount),
                )
            )
        previous = sample.count
    return consumptions


def _draw_down(lots: deque[list[int]], amount: int) -> int:
    """Take ``amount`` feasts from the front of ``lots``; return their cost.

    The queue holds exactly the feasts the counts say are on the shelf, so it
    cannot run dry; if a hand-edited log ever made it, the feasts beyond it
    are counted as costing nothing rather than refused.
    """
    cost = 0
    wanted = amount
    while wanted > 0 and lots:
        lot = lots[0]
        taken = min(wanted, lot[0])
        share = round(lot[1] * taken / lot[0])
        cost += share
        lot[0] -= taken
        lot[1] -= share
        wanted -= taken
        if lot[0] == 0:
            lots.popleft()
    return cost


@dataclass(frozen=True, slots=True)
class FeastDay:
    """One UTC day of one tracked feast's usage and what it cost.

    ``day`` is the day's UTC midnight in epoch seconds. ``used`` is how many
    feasts were observed leaving storage that day and ``cost`` is what those
    feasts had cost, in copper, first in, first out.
    """

    day: float
    used: int
    cost: int


@dataclass(frozen=True, slots=True)
class FeastDayPoint:
    """One day of the dashboard's daily charts, averages included.

    ``used_average`` and ``cost_average`` are the trailing means over
    :data:`ROLLING_AVERAGE_DAYS` days ending on this one, which is why the
    series is built over the history before the window and only then cut back
    to it.
    """

    day: float
    used: int
    used_average: float
    cost: int
    cost_average: float


def feast_days(
    consumptions: Sequence[FeastConsumption],
    since: float,
    until: float,
) -> list[FeastDay]:
    """One entry per UTC day from ``since`` through ``until``, oldest first.

    A day nothing was used on is still an entry: it is a day the guild ate no
    feasts and so spent nothing on them, and dropping it would let a rolling
    average step over the quiet stretch as though it had never happened.

    A drop outside the two bounds is ignored rather than folded into the
    nearest day, so a series never counts something the window it was read
    for does not hold.
    """
    first = day_of(since)
    last = day_of(until)
    used: dict[float, int] = {}
    spent: dict[float, int] = {}
    for consumption in consumptions:
        if consumption.recorded_at < since or consumption.recorded_at > until:
            continue
        day = day_of(consumption.recorded_at)
        used[day] = used.get(day, 0) + consumption.amount
        spent[day] = spent.get(day, 0) + consumption.cost
    days: list[FeastDay] = []
    day = first
    while day <= last:
        days.append(
            FeastDay(day=day, used=used.get(day, 0), cost=spent.get(day, 0))
        )
        day += SECONDS_PER_DAY
    return days


def rolling_averages(values: Sequence[float], window: int) -> list[float]:
    """The mean of the last ``window`` entries, one per entry in ``values``.

    An entry with fewer than ``window`` behind it averages what there is
    rather than being left out, which is what makes the series continuous at
    its left edge. The dashboard reads a whole window of days before the one
    it draws precisely so none of those partial means is ever drawn.
    """
    if window <= 0:
        return [0.0 for _ in values]
    averages: list[float] = []
    running = 0.0
    for index, value in enumerate(values):
        running += value
        if index >= window:
            running -= values[index - window]
        averages.append(running / min(index + 1, window))
    return averages


def feast_day_series(
    consumptions: Sequence[FeastConsumption],
    since: float,
    until: float,
) -> list[FeastDayPoint]:
    """The drawn window's days, each carrying the averages behind it.

    ``consumptions`` have to reach back to :func:`history_start`; the days
    before ``since`` are what the rolling averages are worked out over and
    are then dropped, so every day that survives carries a full
    :data:`ROLLING_AVERAGE_DAYS` of history whether or not the reader asked
    for a window that wide.

    Both of the window's edges cut a day in half, and both are honoured:
    the newest day is usually still running, and the oldest holds only the
    part of itself the window opened over. So the totals drawn are the
    window's own, the way the removals and additions tables beside them are,
    rather than whole days reaching back over hours the reader did not ask
    for. That is also why the averages are trailing means rather than
    centred ones.

    The averages stay whole-day means over whole days of history, because
    that is what an average over the last seven days is; they are worked out
    on the history buckets and looked up by day.
    """
    history = feast_days(consumptions, history_start(since), until)
    used = rolling_averages(
        [float(day.used) for day in history], ROLLING_AVERAGE_DAYS
    )
    spent = rolling_averages(
        [float(day.cost) for day in history], ROLLING_AVERAGE_DAYS
    )
    averages = {
        day.day: (used[index], spent[index])
        for index, day in enumerate(history)
    }
    return [
        FeastDayPoint(
            day=day.day,
            used=day.used,
            used_average=averages[day.day][0],
            cost=day.cost,
            cost_average=averages[day.day][1],
        )
        for day in feast_days(consumptions, since, until)
    ]


def window_series(series: FeastStockSeries, since: float) -> FeastStockSeries:
    """The part of a series that falls inside the window being drawn.

    The dashboard reads a week further back than it draws so its rolling
    averages have history behind them. Everything else on the page is about
    the window itself, so it is served this slice instead, and the slice is
    the series the store would have returned for that window on its own:
    ``prior_count`` carries the last reading before ``since`` however far
    back it was, so a drop across the window's start edge is still measured.
    """
    prior = series.prior_count
    samples: list[FeastStockSample] = []
    for sample in series.samples:
        if sample.recorded_at < since:
            prior = sample.count
        else:
            samples.append(sample)
    return FeastStockSeries(
        guild_storage_id=series.guild_storage_id,
        prior_count=prior,
        samples=tuple(samples),
    )


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
