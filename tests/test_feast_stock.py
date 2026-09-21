from gw2bot.gw2.feast_stock import (
    LOW_STOCK_REMINDER_SECONDS,
    ROLLING_AVERAGE_DAYS,
    SECONDS_PER_DAY,
    FeastAddition,
    FeastRemoval,
    FeastRestock,
    FeastStockSample,
    FeastStockSeries,
    changed_feast_counts,
    day_of,
    depositors_for_addition,
    feast_additions,
    feast_day_series,
    feast_days,
    feast_removals,
    get_due_low_stock_alerts,
    history_start,
    rolling_averages,
    tracked_feast_counts,
    window_series,
)


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


def _removal(at: float, amount: int) -> FeastRemoval:
    return FeastRemoval(recorded_at=at, amount=amount, remaining=0)


def _addition(log_id: int, at: float, amount: int) -> FeastAddition:
    return FeastAddition(
        log_id=log_id,
        recorded_at=at,
        previous_at=None,
        amount=amount,
        remaining=0,
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


class TestFeastDays:
    def test_a_quiet_day_is_still_a_day(self) -> None:
        # Dropping it would let a rolling average step over the quiet stretch
        # as though it had never happened.
        days = feast_days(
            [_removal(DAY + 10, 4)],
            [],
            {},
            DAY,
            DAY + 2 * SECONDS_PER_DAY,
        )

        assert [day.day for day in days] == [
            DAY,
            DAY + SECONDS_PER_DAY,
            DAY + 2 * SECONDS_PER_DAY,
        ]
        assert [day.used for day in days] == [4, 0, 0]

    def test_removals_and_priced_restocks_are_bucketed_by_their_day(
        self,
    ) -> None:
        days = feast_days(
            [
                _removal(DAY + 60, 3),
                _removal(DAY + 120, 2),
                _removal(DAY + SECONDS_PER_DAY + 60, 5),
            ],
            [
                _addition(1, DAY + 300, 10),
                _addition(2, DAY + SECONDS_PER_DAY + 300, 20),
            ],
            {1: 1000, 2: 4000},
            DAY,
            DAY + SECONDS_PER_DAY + DAY_END,
        )

        assert [(day.used, day.cost, day.priced_amount) for day in days] == [
            (5, 1000, 10),
            (5, 4000, 20),
        ]
        assert [day.unit_cost for day in days] == [100.0, 200.0]

    def test_an_unpriced_restock_counts_towards_neither_side(self) -> None:
        # It is missing, not free: counting its feasts would drag the day's
        # cost per feast towards zero on the strength of a figure nobody has
        # recorded yet.
        days = feast_days(
            [],
            [_addition(1, DAY + 60, 10), _addition(2, DAY + 120, 90)],
            {1: 5000},
            DAY,
            DAY + DAY_END,
        )

        assert (days[0].cost, days[0].priced_amount) == (5000, 10)
        assert days[0].unit_cost == 500.0

    def test_a_day_nothing_was_priced_on_has_no_cost_per_feast(self) -> None:
        days = feast_days([_removal(DAY + 60, 3)], [], {}, DAY, DAY + DAY_END)

        assert days[0].cost == 0
        assert days[0].unit_cost is None

    def test_anything_outside_the_bounds_is_left_out(self) -> None:
        days = feast_days(
            [_removal(DAY - 60, 99), _removal(DAY + SECONDS_PER_DAY, 99)],
            [_addition(1, DAY - 60, 99)],
            {1: 9999},
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
        # Seven days of history, one feast used a day, and then the window's
        # own first day: the average on it covers the week behind it rather
        # than starting over at the window's edge.
        opened = DAY
        removals = [
            _removal(history_start(opened) + index * SECONDS_PER_DAY + 60, 7)
            for index in range(ROLLING_AVERAGE_DAYS)
        ]
        removals.append(_removal(opened + 60, 7))

        series = feast_day_series(removals, [], {}, opened, opened + DAY_END)

        assert [point.day for point in series] == [opened]
        assert series[0].used == 7
        assert series[0].used_average == 7.0

    def test_a_window_without_history_still_starts_at_its_first_day(
        self,
    ) -> None:
        series = feast_day_series(
            [_removal(DAY + 60, 14)],
            [],
            {},
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

    def test_costs_carry_their_own_average_and_price_per_feast(self) -> None:
        series = feast_day_series(
            [],
            [_addition(1, DAY + 60, 4)],
            {1: 8000},
            DAY,
            DAY + DAY_END,
        )

        assert series[0].cost == 8000
        assert series[0].cost_average == 8000 / 7
        assert series[0].unit_cost == 2000.0


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
