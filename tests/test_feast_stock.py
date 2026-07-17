from gw2bot.feast_stock import (
    LOW_STOCK_REMINDER_SECONDS,
    changed_feast_counts,
    get_due_low_stock_alerts,
    tracked_feast_counts,
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
