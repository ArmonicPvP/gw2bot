from gw2bot.feast_stock import (
    LOW_STOCK_REMINDER_SECONDS,
    FeastStockSample,
    FeastStockSeries,
    changed_feast_counts,
    feast_removals,
    get_due_low_stock_alerts,
    tracked_feast_counts,
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
