from collections import deque

from gw2bot.gw2.feast_stock import (
    LOW_STOCK_REMINDER_SECONDS,
    ROLLING_AVERAGE_DAYS,
    SECONDS_PER_DAY,
    FeastConsumption,
    FeastRestock,
    FeastStockSample,
    FeastStockSeries,
    changed_feast_counts,
    day_of,
    depositors_for_addition,
    feast_additions,
    feast_consumptions,
    feast_day_series,
    feast_days,
    feast_removals,
    get_due_low_stock_alerts,
    history_start,
    rolling_averages,
    tracked_feast_counts,
    window_series,
)
from gw2bot.gw2.feast_stock import _draw_down  # pyright: ignore[reportPrivateUsage]  # the dry-queue branch is unreachable through the public API


def _series(
    prior_count: int | None,
    samples: list[tuple[float, int]],
) -> FeastStockSeries:
    return FeastStockSeries(
        guild_storage_id=1078,
        prior_count=prior_count,
        samples=tuple(
            FeastStockSample(recorded_at=t, count=c) for t, c in samples
        ),
    )


class TestFeastStock:
    def test_alerts_present_feasts_and_ignores_missing_ones(self) -> None:
        # 1112 (Spherified Cilantro Oyster Soup) is absent from the poll, so it
        # is treated as unknown and does not alert; only present low feasts do.
        counts = tracked_feast_counts(
            [
                {"id": 1078, "count": 10},
                {"id": 1089, "count": 20},
                {"id": 1102, "count": 21},
            ]
        )

        alerts, currently_low = get_due_low_stock_alerts(
            counts,
            last_alerted_at={},
            now=100.0,
        )

        assert [alert.message for alert in alerts] == [
            "Guild Storage is low on **Bowl of Fruit Salad with Mint Garnish**: "
            "10 left",
        ]
        assert currently_low == {1078}

    def test_alerts_present_feast_that_is_genuinely_empty(self) -> None:
        alerts, currently_low = get_due_low_stock_alerts(
            {1112: 0},
            last_alerted_at={},
            now=100.0,
        )

        assert [alert.message for alert in alerts] == [
            "Guild Storage is low on **Spherified Cilantro Oyster Soup**: 0 left",
        ]
        assert currently_low == {1112}

    def test_repeats_alert_every_eight_hours_while_low(self) -> None:
        low_counts = {1078: 2, 1089: 21, 1102: 21, 1112: 21}

        first_alerts, _ = get_due_low_stock_alerts(low_counts, {}, now=100.0)
        early_alerts, _ = get_due_low_stock_alerts(
            low_counts,
            {1078: 100.0},
            now=100.0 + LOW_STOCK_REMINDER_SECONDS - 1,
        )
        repeated_alerts, _ = get_due_low_stock_alerts(
            low_counts,
            {1078: 100.0},
            now=100.0 + LOW_STOCK_REMINDER_SECONDS,
        )

        assert len(first_alerts) == 1
        assert early_alerts == []
        assert len(repeated_alerts) == 1


class TestTrackedFeastCounts:
    def test_returns_only_present_tracked_feasts(self) -> None:
        counts = tracked_feast_counts(
            [
                {"id": 1078, "count": 2},
                {"id": 1102, "count": 5},
                {"id": 9999, "count": 42},
            ]
        )

        # Missing tracked feasts (1089, 1112) are omitted, not zeroed, and the
        # untracked id 9999 is ignored.
        assert counts == {1078: 2, 1102: 5}

    def test_ignores_entries_without_id_or_count(self) -> None:
        counts = tracked_feast_counts(
            [
                {"id": 1078, "count": 7},
                {"id": 1089},
                {"count": 3},
            ]
        )

        assert counts == {1078: 7}


class TestChangedFeastCounts:
    def test_returns_everything_when_previous_is_empty(self) -> None:
        current = {1078: 2, 1089: 3}

        assert changed_feast_counts(current, {}) == {1078: 2, 1089: 3}

    def test_returns_nothing_when_unchanged(self) -> None:
        counts = {1078: 2, 1089: 3}

        assert changed_feast_counts(counts, dict(counts)) == {}

    def test_returns_only_changed_feasts(self) -> None:
        changed = changed_feast_counts(
            {1078: 0, 1089: 3},
            {1078: 2, 1089: 3},
        )

        assert changed == {1078: 0}


class TestFeastRemovals:
    def test_reports_each_decrease_with_amount_and_remaining(self) -> None:
        series = _series(None, [(1.0, 40), (2.0, 33), (3.0, 30)])

        removals = feast_removals(series)

        assert [(r.recorded_at, r.amount, r.remaining) for r in removals] == [
            (2.0, 7, 33),
            (3.0, 3, 30),
        ]

    def test_ignores_restocks_and_unchanged_samples(self) -> None:
        # 40 -> 45 is a restock and 45 -> 45 is unchanged; neither is a removal.
        series = _series(None, [(1.0, 40), (2.0, 45), (3.0, 45), (4.0, 20)])

        removals = feast_removals(series)

        assert [(r.amount, r.remaining) for r in removals] == [(25, 20)]

    def test_uses_prior_count_for_a_decrease_across_the_window_edge(
        self,
    ) -> None:
        # The first in-window sample fell below the last count recorded before
        # the window, so it is still a removal even though the higher value
        # predates the window.
        series = _series(50, [(10.0, 44), (11.0, 40)])

        removals = feast_removals(series)

        assert [(r.recorded_at, r.amount, r.remaining) for r in removals] == [
            (10.0, 6, 44),
            (11.0, 4, 40),
        ]

    def test_no_prior_count_makes_the_first_sample_a_baseline(self) -> None:
        # With no earlier record the first sample is the baseline, not a drop.
        series = _series(None, [(1.0, 30), (2.0, 25)])

        removals = feast_removals(series)

        assert [(r.amount, r.remaining) for r in removals] == [(5, 25)]

    def test_empty_series_yields_no_removals(self) -> None:
        assert feast_removals(_series(None, [])) == []


class TestFeastAdditions:
    def test_reports_each_increase_with_amount_and_remaining(self) -> None:
        series = _series(None, [(1.0, 10), (2.0, 34), (3.0, 40)])

        additions = feast_additions(series)

        assert [(a.recorded_at, a.amount, a.remaining) for a in additions] == [
            (2.0, 24, 34),
            (3.0, 6, 40),
        ]

    def test_ignores_removals_and_unchanged_samples(self) -> None:
        series = _series(None, [(1.0, 40), (2.0, 20), (3.0, 20), (4.0, 45)])

        additions = feast_additions(series)

        assert [(a.amount, a.remaining) for a in additions] == [(25, 45)]

    def test_uses_prior_count_for_a_restock_across_the_window_edge(
        self,
    ) -> None:
        series = _series(12, [(10.0, 40), (11.0, 44)])

        additions = feast_additions(series)

        assert [(a.recorded_at, a.amount, a.remaining) for a in additions] == [
            (10.0, 28, 40),
            (11.0, 4, 44),
        ]

    def test_no_prior_count_makes_the_first_sample_a_baseline(self) -> None:
        # The shelf was not observed filling, it was observed for the first
        # time, so the first reading is not a restock.
        series = _series(None, [(1.0, 30), (2.0, 35)])

        additions = feast_additions(series)

        assert [(a.amount, a.remaining) for a in additions] == [(5, 35)]

    def test_each_addition_carries_its_stock_log_row(self) -> None:
        # The row id is what a recorded cost is filed against, so it has to
        # survive the walk over the samples.
        series = FeastStockSeries(
            guild_storage_id=1078,
            prior_count=10,
            samples=(
                FeastStockSample(recorded_at=1.0, count=40, log_id=7),
                FeastStockSample(recorded_at=2.0, count=44, log_id=9),
            ),
        )

        assert [addition.log_id for addition in feast_additions(series)] == [
            7,
            9,
        ]

    def test_previous_at_opens_at_the_reading_the_restock_rose_from(
        self,
    ) -> None:
        # The first in-window addition rose from a reading outside it, which
        # has no timestamp here; every later one names the reading before it.
        series = _series(10, [(10.0, 40), (20.0, 44)])

        additions = feast_additions(series)

        assert [addition.previous_at for addition in additions] == [None, 10.0]

    def test_empty_series_yields_no_additions(self) -> None:
        assert feast_additions(_series(None, [])) == []


def _restock(occurred_at: float, username: str) -> FeastRestock:
    return FeastRestock(
        occurred_at=occurred_at,
        guild_storage_id=1078,
        username=username,
        count=10,
    )


class TestDepositorsForAddition:
    def test_names_the_deposits_between_the_two_readings(self) -> None:
        addition = feast_additions(_series(10, [(10.0, 20), (20.0, 40)]))[1]

        named = depositors_for_addition(
            addition,
            [
                _restock(9.0, "Before.1234"),
                _restock(15.0, "Cook.1234"),
                _restock(25.0, "After.1234"),
            ],
            window_since=5.0,
        )

        assert named == ["Cook.1234"]

    def test_a_deposit_at_the_reading_itself_belongs_to_it(self) -> None:
        # The poll reads the new count at or after the deposit that raised it,
        # never before, so the closing edge is inclusive and the opening one
        # is not.
        addition = feast_additions(_series(10, [(10.0, 20), (20.0, 40)]))[1]

        named = depositors_for_addition(
            addition,
            [_restock(10.0, "Early.1234"), _restock(20.0, "Cook.1234")],
            window_since=5.0,
        )

        assert named == ["Cook.1234"]

    def test_several_depositors_are_all_named_once_each(self) -> None:
        addition = feast_additions(_series(10, [(10.0, 20), (20.0, 40)]))[1]

        named = depositors_for_addition(
            addition,
            [
                _restock(12.0, "Cook.1234"),
                _restock(14.0, "Baker.5678"),
                _restock(16.0, "Cook.1234"),
            ],
            window_since=5.0,
        )

        assert named == ["Cook.1234", "Baker.5678"]

    def test_the_window_edge_opens_the_first_addition(self) -> None:
        # Its previous reading predates the window, which is the only stretch
        # the deposits were loaded for, so the window's own start stands in.
        addition = feast_additions(_series(10, [(10.0, 40)]))[0]

        named = depositors_for_addition(
            addition,
            [_restock(4.0, "Outside.1234"), _restock(6.0, "Cook.1234")],
            window_since=5.0,
        )

        assert named == ["Cook.1234"]

    def test_an_unattributable_addition_names_nobody(self) -> None:
        addition = feast_additions(_series(10, [(10.0, 20), (20.0, 40)]))[1]

        assert depositors_for_addition(addition, [], window_since=5.0) == []



# A round UTC midnight to build day grids from, so a test reads as the days it
# means rather than as arithmetic on whatever today happens to be.
DAY = 1_750_000_000 // SECONDS_PER_DAY * SECONDS_PER_DAY

# The last second of a day, so a window that covers whole days is written as
# the moments it runs between rather than as a midnight that cuts one short.
DAY_END = SECONDS_PER_DAY - 1


# A gold coin in copper, so the prices below read the way an officer types
# them.
GOLD = 10_000


def _used(at: float, amount: int, cost: int = 0) -> FeastConsumption:
    return FeastConsumption(recorded_at=at, amount=amount, cost=cost)


def _shelf(
    counts: list[int], prior_count: int | None = None
) -> FeastStockSeries:
    """A feast whose count went through ``counts``, one reading a second.

    Each reading's log id is its position plus one, so a test prices the
    restock read at position ``n`` by giving ``n + 1`` a cost.
    """
    return FeastStockSeries(
        guild_storage_id=1078,
        prior_count=prior_count,
        samples=tuple(
            FeastStockSample(recorded_at=index + 1, count=count, log_id=index + 1)
            for index, count in enumerate(counts)
        ),
    )


class TestDayGrid:
    def test_a_moment_is_placed_in_the_utc_day_it_falls_in(self) -> None:
        assert day_of(DAY) == DAY
        assert day_of(DAY + 1) == DAY
        assert day_of(DAY + SECONDS_PER_DAY - 1) == DAY
        assert day_of(DAY + SECONDS_PER_DAY) == DAY + SECONDS_PER_DAY

    def test_history_reaches_a_whole_week_before_the_window_opens(
        self,
    ) -> None:
        # A whole number of days back from the window's own first day, so the
        # history lines up with the grid it is bucketed into instead of
        # opening part-way through a day and under-counting it.
        opened = DAY + 13 * 60 * 60
        assert history_start(opened) == DAY - ROLLING_AVERAGE_DAYS * (
            SECONDS_PER_DAY
        )


class TestFeastConsumptions:
    """Each feast used is priced at the restock it came from, oldest first."""

    def test_stock_that_was_never_priced_leaves_first_and_costs_nothing(
        self,
    ) -> None:
        # Ten on the shelf nobody paid for, then thirty bought for thirty
        # gold. Thirty used takes the ten free ones first and twenty of the
        # new lot, so it cost twenty gold, not thirty.
        series = _shelf([10, 40, 10])

        used = feast_consumptions(series, {2: 30 * GOLD})

        assert [(c.amount, c.cost) for c in used] == [(30, 20 * GOLD)]

    def test_a_dearer_restock_waits_behind_the_stock_before_it(self) -> None:
        # The ten left of the one-gold lot go before any of the two-gold lot,
        # so the next thirty cost 10 x 1g + 20 x 2g.
        series = _shelf([10, 40, 10, 40, 10])

        used = feast_consumptions(series, {2: 30 * GOLD, 4: 60 * GOLD})

        assert [c.cost for c in used] == [20 * GOLD, 50 * GOLD]

    def test_a_deposit_is_not_a_cost_until_its_feasts_are_used(self) -> None:
        # Buying is not spending on this chart: a restock nobody has eaten
        # from yet adds nothing, and each feast costs its share as it goes.
        series = _shelf([0, 30, 28, 26])

        used = feast_consumptions(series, {2: 30 * GOLD})

        assert [(c.amount, c.cost) for c in used] == [
            (2, 2 * GOLD),
            (2, 2 * GOLD),
        ]

    def test_stock_already_there_before_the_series_opened_is_a_free_lot(
        self,
    ) -> None:
        # prior_count is stock the replay did not see bought.
        series = _shelf([35, 5], prior_count=5)

        used = feast_consumptions(series, {1: 30 * GOLD})

        assert [c.cost for c in used] == [25 * GOLD]

    def test_a_restock_nobody_has_priced_is_free_until_it_is(self) -> None:
        series = _shelf([0, 10, 0])

        assert [c.cost for c in feast_consumptions(series, {})] == [0]
        assert [c.cost for c in feast_consumptions(series, {2: 7 * GOLD})] == [
            7 * GOLD
        ]

    def test_an_uneven_price_is_spent_exactly_by_the_last_feast(self) -> None:
        # Ten copper over three feasts does not divide; each drop takes its
        # rounded share and the last takes what is left, so nothing is lost
        # or invented along the way.
        series = _shelf([0, 3, 2, 1, 0])

        used = feast_consumptions(series, {2: 10})

        assert sum(c.cost for c in used) == 10
        assert [c.cost for c in used] == [3, 4, 3]

    def test_a_drop_spanning_two_lots_pays_each_its_own_price(self) -> None:
        series = _shelf([0, 4, 10, 1])

        used = feast_consumptions(series, {2: 4 * GOLD, 3: 12 * GOLD})

        assert [(c.amount, c.cost) for c in used] == [
            (9, 4 * GOLD + 5 * 2 * GOLD)
        ]

    def test_a_first_reading_is_stock_and_not_a_restock(self) -> None:
        # The shelf was not seen filling, it was seen for the first time.
        series = _shelf([20, 15])

        used = feast_consumptions(series, {1: 99 * GOLD})

        assert [c.cost for c in used] == [0]

    def test_a_drop_the_shelf_cannot_account_for_costs_nothing(self) -> None:
        # The queue follows the counts, so it cannot run dry through
        # feast_consumptions; a hand-edited log that ever made it would price
        # the feasts beyond it at nothing rather than refuse the page.
        lots: deque[list[int]] = deque([[2, 4 * GOLD]])

        assert _draw_down(lots, 5) == 4 * GOLD
        assert not lots


class TestFeastDays:
    def test_a_quiet_day_is_still_a_day(self) -> None:
        # Dropping it would let a rolling average step over the quiet stretch
        # as though it had never happened.
        days = feast_days(
            [_used(DAY + 10, 4, 400)],
            DAY,
            DAY + 2 * SECONDS_PER_DAY,
        )

        assert [day.day for day in days] == [
            DAY,
            DAY + SECONDS_PER_DAY,
            DAY + 2 * SECONDS_PER_DAY,
        ]
        assert [(day.used, day.cost) for day in days] == [
            (4, 400),
            (0, 0),
            (0, 0),
        ]

    def test_usage_and_its_cost_are_bucketed_by_their_day(self) -> None:
        days = feast_days(
            [
                _used(DAY + 60, 3, 300),
                _used(DAY + 120, 2, 200),
                _used(DAY + SECONDS_PER_DAY + 60, 5, 1000),
            ],
            DAY,
            DAY + SECONDS_PER_DAY + DAY_END,
        )

        assert [(day.used, day.cost) for day in days] == [
            (5, 500),
            (5, 1000),
        ]

    def test_anything_outside_the_bounds_is_left_out(self) -> None:
        days = feast_days(
            [
                _used(DAY - 60, 99, 9999),
                _used(DAY + SECONDS_PER_DAY, 99, 9999),
            ],
            DAY,
            DAY + DAY_END,
        )

        assert [(day.used, day.cost) for day in days] == [(0, 0)]


class TestRollingAverages:
    def test_a_full_window_averages_the_last_entries_only(self) -> None:
        assert rolling_averages([1, 2, 3, 4, 5, 6, 7, 8], 7) == [
            1.0,
            1.5,
            2.0,
            2.5,
            3.0,
            3.5,
            4.0,
            (2 + 3 + 4 + 5 + 6 + 7 + 8) / 7,
        ]

    def test_an_early_entry_averages_what_there_is(self) -> None:
        # Continuity at the left edge is what the dashboard reads a whole
        # window of days before the one it draws for.
        assert rolling_averages([4, 8], 7) == [4.0, 6.0]

    def test_an_empty_series_averages_nothing(self) -> None:
        assert rolling_averages([], 7) == []


class TestFeastDaySeries:
    def test_the_history_is_averaged_over_and_then_dropped(self) -> None:
        # Seven days of history, seven feasts used a day, and then the
        # window's own first day: the average on it covers the week behind it
        # rather than starting over at the window's edge.
        opened = DAY
        used = [
            _used(history_start(opened) + index * SECONDS_PER_DAY + 60, 7)
            for index in range(ROLLING_AVERAGE_DAYS)
        ]
        used.append(_used(opened + 60, 7))

        series = feast_day_series(used, opened, opened + DAY_END)

        assert [point.day for point in series] == [opened]
        assert series[0].used == 7
        assert series[0].used_average == 7.0

    def test_a_window_without_history_still_starts_at_its_first_day(
        self,
    ) -> None:
        series = feast_day_series(
            [_used(DAY + 60, 14)],
            DAY,
            DAY + SECONDS_PER_DAY + DAY_END,
        )

        assert [point.day for point in series] == [
            DAY,
            DAY + SECONDS_PER_DAY,
        ]
        # Seven silent days behind the window pull the average down, which is
        # what a week with one busy day in it looks like.
        assert series[0].used == 14
        assert series[0].used_average == 14 / 7
        assert series[1].used == 0

    def test_a_week_of_steady_use_averages_to_what_a_day_costs(self) -> None:
        # Thirty feasts bought for thirty gold, two eaten a day: every day
        # costs two gold, so the seven-day average is two gold too. It does
        # not spike on the day the restock was bought and then fall away,
        # which is what averaging what was bought used to draw.
        opened = DAY
        counts = [30] + [30 - 2 * day for day in range(1, 15)]
        samples = tuple(
            FeastStockSample(
                recorded_at=history_start(opened) + index * SECONDS_PER_DAY + 60,
                count=count,
                log_id=index + 1,
            )
            for index, count in enumerate(counts)
        )
        shelf = FeastStockSeries(
            guild_storage_id=1078, prior_count=0, samples=samples
        )

        series = feast_day_series(
            feast_consumptions(shelf, {1: 30 * GOLD}),
            opened,
            opened + 6 * SECONDS_PER_DAY + DAY_END,
        )

        assert [point.cost for point in series] == [2 * GOLD] * 7
        assert [point.cost_average for point in series] == [2 * GOLD] * 7

    def test_the_first_day_holds_only_the_part_the_window_opened_over(
        self,
    ) -> None:
        # A preset window opens part-way through a day, so the day it opens
        # on is cut in half the way the day it closes on is. The figures
        # drawn have to be the window's own, the way the removals and
        # additions tables beside them are.
        opened = DAY + 14 * 60 * 60
        before = [_used(DAY + 6 * 60 * 60, 99, 500_000)]
        inside = [_used(DAY + 20 * 60 * 60, 5, 8_000)]

        series = feast_day_series(
            before + inside, opened, opened + SECONDS_PER_DAY
        )

        assert series[0].day == DAY
        assert (series[0].used, series[0].cost) == (5, 8_000)
        # The hours before the window still count towards the averages, which
        # are means over whole days and read a week of them for that reason.
        assert series[0].used_average == 104 / 7
        assert series[0].cost_average == 508_000 / 7


class TestWindowSeries:
    def test_the_window_keeps_only_its_own_samples(self) -> None:
        series = _series(
            None,
            [(DAY - 100, 50), (DAY + 100, 44), (DAY + 200, 40)],
        )

        drawn = window_series(series, DAY)

        assert [sample.count for sample in drawn.samples] == [44, 40]

    def test_the_count_the_window_opened_on_becomes_its_prior(self) -> None:
        # A drop across the window's start edge is still measured, the way
        # the store's own read of that window would have measured it.
        series = _series(None, [(DAY - 100, 50), (DAY + 100, 44)])

        drawn = window_series(series, DAY)

        assert drawn.prior_count == 50
        assert [removal.amount for removal in feast_removals(drawn)] == [6]

    def test_a_feast_untouched_for_longer_than_the_history_keeps_its_prior(
        self,
    ) -> None:
        # Counts are only written when they change, so a feast nobody has
        # touched for weeks has no sample in the history at all; the count it
        # entered the window on is the one the store read before it.
        series = _series(37, [(DAY + 100, 30)])

        drawn = window_series(series, DAY)

        assert drawn.prior_count == 37
        assert [removal.amount for removal in feast_removals(drawn)] == [7]
