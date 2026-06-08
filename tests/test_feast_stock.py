import unittest

from gw2bot.feast_stock import (
    LOW_STOCK_REMINDER_SECONDS,
    get_due_low_stock_alerts,
)


class FeastStockTests(unittest.TestCase):
    def test_alerts_at_or_below_threshold_and_treats_missing_as_zero(self) -> None:
        alerts, currently_low = get_due_low_stock_alerts(
            [
                {"id": 1078, "count": 10},
                {"id": 1089, "count": 20},
                {"id": 1102, "count": 21},
            ],
            last_alerted_at={},
            now=100.0,
        )

        self.assertEqual(
            [alert.message for alert in alerts],
            [
                "Guild Storage is low on **Bowl of Fruit Salad with Mint Garnish**: "
                "10 left",
                "Guild Storage is low on **Spherified Cilantro Oyster Soup**: 0 left",
            ],
        )
        self.assertEqual(currently_low, {1078, 1112})

    def test_repeats_alert_every_eight_hours_while_low(self) -> None:
        low_storage = [
            {"id": 1078, "count": 2},
            {"id": 1089, "count": 21},
            {"id": 1102, "count": 21},
            {"id": 1112, "count": 21},
        ]

        first_alerts, _ = get_due_low_stock_alerts(low_storage, {}, now=100.0)
        early_alerts, _ = get_due_low_stock_alerts(
            low_storage,
            {1078: 100.0},
            now=100.0 + LOW_STOCK_REMINDER_SECONDS - 1,
        )
        repeated_alerts, _ = get_due_low_stock_alerts(
            low_storage,
            {1078: 100.0},
            now=100.0 + LOW_STOCK_REMINDER_SECONDS,
        )

        self.assertEqual(len(first_alerts), 1)
        self.assertEqual(early_alerts, [])
        self.assertEqual(len(repeated_alerts), 1)


if __name__ == "__main__":
    unittest.main()
