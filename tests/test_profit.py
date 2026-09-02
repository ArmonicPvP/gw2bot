from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import discord
import pytest
from cryptography.fernet import Fernet

from factories import default_config
from gw2bot.database import create_database_engine
from gw2bot.logging_setup import SecretRegistry
from gw2bot.profit.api import (
    DELIVERY_PATH,
    TRANSACTION_PATHS,
    ProfitApiClient,
    ProfitApiAuthorizationError,
    ProfitApiError,
)
from gw2bot.profit.commands import ProfitApiKeyModal, ProfitCommands
from gw2bot.profit.models import (
    BuyLot,
    DeliveryItem,
    ItemProfit,
    MarketPrice,
    OpenBuyOrder,
    ProfitReport,
    RealizedProfit,
    Transaction,
    UnrealizedProfit,
    allocated_net_revenue,
    calculate_realized_profit,
    calculate_unrealized_profit,
    group_open_buy_orders,
    sale_fee_total,
)
from gw2bot.profit.service import (
    MissingProfitApiKey,
    ProfitService,
    serialize_profit_report,
)
from gw2bot.profit.store import ProfitStore
from gw2bot.settings.crypto import SettingsCipher


def transaction(
    transaction_id: str,
    *,
    item_id: int = 1,
    price: int = 100,
    quantity: int = 1,
    occurred_at: datetime | None = None,
) -> Transaction:
    return Transaction(
        transaction_id=transaction_id,
        item_id=item_id,
        price=price,
        quantity=quantity,
        occurred_at=(
            datetime(2026, 8, 1, tzinfo=UTC)
            if occurred_at is None
            else occurred_at
        ),
    )


@pytest.fixture
def profit_store(tmp_path: Path):
    registry = SecretRegistry()
    store = ProfitStore(
        str(tmp_path / "gw2bot.db"),
        SettingsCipher(Fernet.generate_key()),
        registry,
    )
    yield store, registry, tmp_path / "gw2bot.db"
    store.close()


class TestProfitCalculation:
    def test_sale_fees_match_the_trading_post_rounding(self) -> None:
        assert sale_fee_total(101, 3) == 47
        assert allocated_net_revenue(101, 3, 2) == 170

    def test_matches_realized_profit_fifo_and_groups_it_by_sale_day(self) -> None:
        buys = [
            transaction("buy-1", price=100, quantity=10),
            transaction(
                "buy-2",
                price=150,
                quantity=2,
                occurred_at=datetime(2026, 8, 2, tzinfo=UTC),
            ),
        ]
        sells = [
            transaction(
                "sell-1",
                price=200,
                quantity=4,
                occurred_at=datetime(2026, 8, 3, tzinfo=UTC),
            ),
            transaction(
                "sell-2",
                price=250,
                quantity=3,
                occurred_at=datetime(2026, 8, 4, tzinfo=UTC),
            ),
        ]

        result = calculate_realized_profit(buys, sells)

        assert result.items[1].matched_quantity == 7
        assert result.items[1].cost == 700
        assert result.items[1].net_revenue == 1_317
        assert result.items[1].profit == 617
        assert result.items[1].median_hold_seconds == 2 * 86_400
        assert result.days["2026-08-03"].profit == 280
        assert result.days["2026-08-04"].profit == 337
        assert result.unmatched_buys[1][0].remaining == 3
        assert result.unmatched_buys[1][1].remaining == 2

    @pytest.mark.parametrize(
        ("buy_quantity", "sell_quantity"),
        [(250, 4), (4, 250)],
    )
    def test_excludes_items_when_either_side_matches_fewer_than_five_units(
        self,
        buy_quantity: int,
        sell_quantity: int,
    ) -> None:
        result = calculate_realized_profit(
            [transaction("buy", quantity=buy_quantity)],
            [
                transaction(
                    "sell",
                    quantity=sell_quantity,
                    occurred_at=datetime(2026, 8, 2, tzinfo=UTC),
                )
            ],
            minimum_flip_quantity=5,
        )

        assert result.items == {}
        assert result.days == {}
        assert result.unmatched_buys == {}
        assert result.total_matched_quantity == 0

    def test_excludes_sales_that_happened_before_any_purchase(self) -> None:
        result = calculate_realized_profit(
            [
                transaction(
                    "buy",
                    quantity=5,
                    occurred_at=datetime(2026, 8, 2, tzinfo=UTC),
                )
            ],
            [transaction("sell", quantity=5)],
            minimum_flip_quantity=5,
        )

        assert result.items == {}
        assert result.days == {}
        assert result.total_matched_quantity == 0

    def test_excludes_sales_with_the_same_timestamp_as_a_purchase(self) -> None:
        occurred_at = datetime(2026, 8, 1, tzinfo=UTC)

        result = calculate_realized_profit(
            [transaction("buy", quantity=5, occurred_at=occurred_at)],
            [transaction("sell", quantity=5, occurred_at=occurred_at)],
            minimum_flip_quantity=5,
        )

        assert result.items == {}
        assert result.days == {}

    def test_median_holding_time_is_weighted_by_matched_units(self) -> None:
        buys = [
            transaction(
                "older-buy",
                quantity=1,
                occurred_at=datetime(2026, 8, 1, tzinfo=UTC),
            ),
            transaction(
                "newer-buy",
                quantity=9,
                occurred_at=datetime(2026, 8, 10, tzinfo=UTC),
            ),
        ]
        sells = [
            transaction(
                "sell",
                price=200,
                quantity=10,
                occurred_at=datetime(2026, 8, 11, tzinfo=UTC),
            )
        ]

        result = calculate_realized_profit(buys, sells)

        assert result.items[1].median_hold_seconds == 86_400

    def test_profit_share_is_each_items_signed_part_of_net_profit(
        self,
    ) -> None:
        buys = [
            transaction("buy-1", item_id=1),
            transaction("buy-2", item_id=2),
        ]
        sells = [
            transaction(
                "sell-1",
                item_id=1,
                price=200,
                occurred_at=datetime(2026, 8, 2, tzinfo=UTC),
            ),
            transaction(
                "sell-2",
                item_id=2,
                price=50,
                occurred_at=datetime(2026, 8, 2, tzinfo=UTC),
            ),
        ]
        realized = calculate_realized_profit(buys, sells)
        report = ProfitReport(
            days=30,
            window_start=datetime(2026, 8, 1, tzinfo=UTC),
            window_end=datetime(2026, 8, 31, tzinfo=UTC),
            buy_transaction_count=2,
            sell_transaction_count=2,
            realized=realized,
            unrealized=UnrealizedProfit({}, 0, 0, 0, 0),
            unclaimed_coins=0,
            unclaimed_items=(),
            item_names={1: "Winner", 2: "Loser"},
        )

        payload = cast(dict[str, Any], serialize_profit_report(report))
        items = {row["item_id"]: row for row in payload["items"]}

        assert items[1]["profit_share_percent"] == pytest.approx(
            70 / 12 * 100
        )
        assert items[2]["profit_share_percent"] == pytest.approx(
            -58 / 12 * 100
        )
        assert sum(
            row["profit_share_percent"] for row in items.values()
        ) == pytest.approx(100)

    def test_preserves_sale_revenue_remainder_across_fifo_lots(self) -> None:
        buys = [
            transaction("buy-1", price=10, quantity=2),
            transaction("buy-2", price=20, quantity=1),
        ]
        sells = [
            transaction(
                "sell",
                price=101,
                quantity=3,
                occurred_at=datetime(2026, 8, 2, tzinfo=UTC),
            )
        ]

        result = calculate_realized_profit(buys, sells)

        assert result.total_net_revenue == 256
        assert result.items[1].net_revenue == 256
        assert result.days["2026-08-02"].net_revenue == 256

    def test_projects_only_unmatched_buys_that_are_currently_listed(self) -> None:
        realized = calculate_realized_profit(
            [transaction("buy", price=100, quantity=5)],
            [
                transaction(
                    "sell",
                    price=200,
                    quantity=2,
                    occurred_at=datetime(2026, 8, 2, tzinfo=UTC),
                )
            ],
        )
        unrealized = calculate_unrealized_profit(
            realized.unmatched_buys,
            [
                transaction(
                    "listing",
                    price=300,
                    quantity=2,
                    occurred_at=datetime(2026, 8, 3, tzinfo=UTC),
                ),
                transaction(
                    "other",
                    item_id=2,
                    price=500,
                    quantity=1,
                ),
            ],
        )

        assert set(unrealized.items) == {1}
        assert unrealized.total_quantity == 2
        assert unrealized.total_cost == 200
        assert unrealized.total_projected_net_revenue == 510
        assert unrealized.total_projected_profit == 310

    def test_preserves_listing_revenue_remainder_across_fifo_lots(self) -> None:
        unrealized = calculate_unrealized_profit(
            {
                1: (
                    BuyLot(2, 10, datetime(2026, 8, 1, tzinfo=UTC)),
                    BuyLot(1, 20, datetime(2026, 8, 2, tzinfo=UTC)),
                )
            },
            [
                transaction(
                    "listing",
                    price=101,
                    quantity=3,
                    occurred_at=datetime(2026, 8, 3, tzinfo=UTC),
                )
            ],
        )

        assert unrealized.total_projected_net_revenue == 256
        assert unrealized.items[1].projected_net_revenue == 256

    def test_matches_a_purchase_only_to_a_listing_created_after_it(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        older_id = "older-listing-secret"
        newer_id = "newer-listing-secret"
        with caplog.at_level(logging.DEBUG, logger="gw2bot"):
            unrealized = calculate_unrealized_profit(
                {
                    1: (
                        BuyLot(
                            1,
                            100,
                            datetime(2026, 8, 2, tzinfo=UTC),
                        ),
                    )
                },
                [
                    transaction(
                        older_id,
                        price=200,
                        occurred_at=datetime(2026, 8, 1, tzinfo=UTC),
                    ),
                    transaction(
                        newer_id,
                        price=300,
                        occurred_at=datetime(2026, 8, 3, tzinfo=UTC),
                    ),
                ],
            )

        assert unrealized.total_quantity == 1
        assert unrealized.total_cost == 100
        assert unrealized.total_projected_net_revenue == 255
        assert unrealized.total_projected_profit == 155
        assert "chronology_blocked=1" in caplog.text
        assert older_id not in caplog.text
        assert newer_id not in caplog.text

    def test_picks_leave_out_items_whose_current_roi_is_negative(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        kept_name = "kept-pick-secret"
        skipped_name = "skipped-pick-secret"
        undefined_name = "undefined-roi-pick-secret"
        report = ProfitReport(
            days=30,
            window_start=datetime(2026, 8, 1, tzinfo=UTC),
            window_end=datetime(2026, 8, 31, tzinfo=UTC),
            buy_transaction_count=0,
            sell_transaction_count=0,
            realized=RealizedProfit(
                items={
                    item_id: ItemProfit(5, 500, 850, 350, 0.0)
                    for item_id in (1, 2, 3)
                },
                days={},
                unmatched_buys={},
                total_cost=1_500,
                total_net_revenue=2_550,
                total_profit=1_050,
                total_matched_quantity=15,
            ),
            unrealized=UnrealizedProfit({}, 0, 0, 0, 0),
            unclaimed_coins=0,
            unclaimed_items=(),
            item_names={
                1: kept_name,
                2: skipped_name,
                3: undefined_name,
            },
            market_prices={
                1: MarketPrice(100, 200),
                2: MarketPrice(200, 200),
                3: MarketPrice(0, 200),
            },
        )

        with caplog.at_level(logging.DEBUG, logger="gw2bot"):
            payload = cast(dict[str, Any], serialize_profit_report(report))

        picks = {row["item_id"]: row for row in payload["picks"]}

        assert set(picks) == {1, 3}
        assert picks[1]["roi_percent"] == 70
        assert picks[3]["roi_percent"] is None
        assert "kept=2 skipped_negative_roi=1" in caplog.text
        assert kept_name not in caplog.text
        assert skipped_name not in caplog.text
        assert undefined_name not in caplog.text


class TestOpenBuyOrders:
    def test_collapses_orders_that_share_an_item_and_price(self) -> None:
        placed = datetime(2026, 8, 20, tzinfo=UTC)
        orders = [
            transaction(
                "second",
                item_id=7,
                price=19,
                quantity=250,
                occurred_at=placed + timedelta(minutes=5),
            ),
            transaction(
                "first",
                item_id=7,
                price=19,
                quantity=178,
                occurred_at=placed,
            ),
        ]

        grouped = group_open_buy_orders(orders)

        assert grouped == (
            OpenBuyOrder(
                item_id=7,
                unit_price=19,
                quantity=428,
                order_count=2,
                placed_at=placed,
            ),
        )

    def test_keeps_two_prices_for_one_item_apart(self) -> None:
        # The order price decides the trade's profit, so orders placed at
        # different prices stay separate rows even for the same item.
        orders = [
            transaction("cheap", item_id=7, price=17, quantity=100),
            transaction("dear", item_id=7, price=19, quantity=50),
            transaction("other", item_id=2, price=61, quantity=10),
        ]

        grouped = group_open_buy_orders(orders)

        assert [(row.item_id, row.unit_price, row.quantity) for row in grouped] == [
            (2, 61, 10),
            (7, 17, 100),
            (7, 19, 50),
        ]

    def test_no_orders_group_to_nothing(self) -> None:
        assert group_open_buy_orders([]) == ()


class TestProfitStore:
    def test_encrypts_and_isolates_each_members_api_key(
        self,
        profit_store: tuple[ProfitStore, SecretRegistry, Path],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        store, registry, database = profit_store
        first = "first-member-profit-secret"
        second = "second-member-profit-secret"

        with caplog.at_level(logging.DEBUG, logger="gw2bot"):
            store.set_api_key(101, first)
            store.set_api_key(202, second)
            assert store.get_api_key(101) == first
            assert store.get_api_key(202) == second

        assert first.encode() not in database.read_bytes()
        assert second.encode() not in database.read_bytes()
        assert {first, second} <= set(registry.current())
        assert first not in caplog.text
        assert second not in caplog.text

    def test_deleting_one_members_key_does_not_touch_another(
        self,
        profit_store: tuple[ProfitStore, SecretRegistry, Path],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        store, _, _ = profit_store
        first_secret = "first-secret"
        now = datetime(2026, 8, 21, tzinfo=UTC)
        store.set_api_key(101, first_secret)
        store.set_api_key(202, "second-secret")
        store.store_transactions(
            101,
            "history_buys",
            [transaction("first")],
            now=now,
        )
        store.touch_cache(101, "history_buys", now=now)
        store.store_transactions(
            202,
            "history_buys",
            [transaction("second")],
            now=now,
        )
        store.touch_cache(202, "history_buys", now=now)

        with caplog.at_level(logging.DEBUG, logger="gw2bot"):
            assert store.delete_api_key(101)
        assert store.get_api_key(101) is None
        assert store.get_api_key(202) == "second-secret"
        assert store.get_transactions(101, "history_buys") == []
        assert not store.is_cache_fresh(101, "history_buys", 300, now=now)
        assert [
            row.transaction_id
            for row in store.get_transactions(202, "history_buys")
        ] == ["second"]
        assert store.is_cache_fresh(202, "history_buys", 300, now=now)
        assert first_secret not in caplog.text

    def test_replacing_a_key_clears_only_that_members_cached_data(
        self,
        profit_store: tuple[ProfitStore, SecretRegistry, Path],
    ) -> None:
        store, _, _ = profit_store
        now = datetime(2026, 8, 21, tzinfo=UTC)
        store.set_api_key(101, "old-secret")
        store.set_api_key(202, "other-secret")
        for transaction_kind in (
            "history_buys",
            "history_sells",
            "current_sells",
        ):
            store.store_transactions(
                101,
                transaction_kind,
                [transaction(f"old-{transaction_kind}")],
                now=now,
            )
            store.touch_cache(101, transaction_kind, now=now)
        store.store_transactions(
            202,
            "history_buys",
            [transaction("other")],
            now=now,
        )
        store.touch_cache(202, "history_buys", now=now)

        store.set_api_key(101, "replacement-secret")

        assert store.get_api_key(101) == "replacement-secret"
        for transaction_kind in (
            "history_buys",
            "history_sells",
            "current_sells",
        ):
            assert store.get_transactions(101, transaction_kind) == []
            assert not store.is_cache_fresh(
                101,
                transaction_kind,
                300,
                now=now,
            )
        assert [
            row.transaction_id
            for row in store.get_transactions(202, "history_buys")
        ] == ["other"]
        assert store.is_cache_fresh(202, "history_buys", 300, now=now)

    def test_rejects_a_transaction_snapshot_fetched_with_a_replaced_key(
        self,
        profit_store: tuple[ProfitStore, SecretRegistry, Path],
    ) -> None:
        store, _, _ = profit_store
        now = datetime(2026, 8, 21, tzinfo=UTC)
        store.set_api_key(101, "old-secret")
        snapshot = store.get_api_key_snapshot(101)
        assert snapshot is not None

        store.set_api_key(101, "replacement-secret")
        accepted = store.store_transaction_snapshot(
            101,
            snapshot.generation,
            [
                ("history_buys", [transaction("old-buy")]),
                ("history_sells", [transaction("old-sell")]),
                ("current_sells", [transaction("old-listing")]),
            ],
            now=now,
        )

        assert not accepted
        for transaction_kind in TRANSACTION_PATHS:
            assert store.get_transactions(101, transaction_kind) == []
            assert not store.is_cache_fresh(
                101,
                transaction_kind,
                300,
                now=now,
            )

    def test_transaction_caches_are_isolated_by_member(
        self,
        profit_store: tuple[ProfitStore, SecretRegistry, Path],
    ) -> None:
        store, _, _ = profit_store
        store.store_transactions(101, "history_buys", [transaction("one")])
        store.store_transactions(
            202,
            "history_buys",
            [transaction("two", item_id=2)],
        )

        assert [row.transaction_id for row in store.get_transactions(101, "history_buys")] == [
            "one"
        ]
        assert [row.transaction_id for row in store.get_transactions(202, "history_buys")] == [
            "two"
        ]

    def test_current_collection_replacement_clears_stale_listings(
        self,
        profit_store: tuple[ProfitStore, SecretRegistry, Path],
    ) -> None:
        store, _, _ = profit_store
        store.store_transactions(101, "current_sells", [transaction("old")])
        store.store_transactions(101, "current_sells", [])

        assert store.get_transactions(101, "current_sells") == []

    def test_history_refresh_updates_an_existing_transaction(
        self,
        profit_store: tuple[ProfitStore, SecretRegistry, Path],
    ) -> None:
        store, _, _ = profit_store
        store.store_transactions(
            101,
            "history_buys",
            [transaction("same", price=100)],
        )
        store.store_transactions(
            101,
            "history_buys",
            [transaction("same", price=125)],
        )

        rows = store.get_transactions(101, "history_buys")
        assert len(rows) == 1
        assert rows[0].price == 125

    def test_cache_freshness_is_per_member_and_kind(
        self,
        profit_store: tuple[ProfitStore, SecretRegistry, Path],
    ) -> None:
        store, _, _ = profit_store
        now = datetime(2026, 8, 21, tzinfo=UTC)
        store.touch_cache(101, "history_buys", now=now)

        assert store.is_cache_fresh(101, "history_buys", 300, now=now)
        assert not store.is_cache_fresh(202, "history_buys", 300, now=now)
        assert not store.is_cache_fresh(101, "history_sells", 300, now=now)
        assert not store.is_cache_fresh(
            101,
            "history_buys",
            300,
            now=now + timedelta(seconds=300),
        )

    def test_item_name_cache_excludes_expired_and_future_rows(
        self,
        profit_store: tuple[ProfitStore, SecretRegistry, Path],
    ) -> None:
        store, _, _ = profit_store
        now = datetime(2026, 8, 21, tzinfo=UTC)
        store.store_item_names(
            {1: "Expired"},
            now=now - timedelta(seconds=300),
        )
        store.store_item_names(
            {2: "Fresh"},
            now=now - timedelta(seconds=299),
        )
        store.store_item_names(
            {3: "Future"},
            now=now + timedelta(seconds=1),
        )

        assert store.get_item_names({1, 2, 3}, 300, now=now) == {
            2: "Fresh"
        }


    def test_remembers_each_members_report_window_separately(
        self,
        profit_store: tuple[ProfitStore, SecretRegistry, Path],
    ) -> None:
        store, _, _ = profit_store

        assert store.get_report_days(101) is None

        store.set_report_days(101, 60)
        store.set_report_days(202, 7)
        store.set_report_days(101, 14)

        assert store.get_report_days(101) == 14
        assert store.get_report_days(202) == 7

    @pytest.mark.parametrize("days", [0, 91, -1])
    def test_refuses_a_report_window_outside_the_served_range(
        self,
        profit_store: tuple[ProfitStore, SecretRegistry, Path],
        days: int,
    ) -> None:
        store, _, _ = profit_store

        with pytest.raises(ValueError):
            store.set_report_days(101, days)

        assert store.get_report_days(101) is None

    def test_reads_an_out_of_range_stored_window_as_unset(
        self,
        profit_store: tuple[ProfitStore, SecretRegistry, Path],
    ) -> None:
        store, _, database = profit_store
        store.set_report_days(101, 30)
        engine = create_database_engine(str(database))
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "UPDATE gw2_profit_preferences SET report_days = 365"
            )
        engine.dispose()

        assert store.get_report_days(101) is None

    def test_excluded_order_items_stay_with_the_member_who_set_them(
        self,
        profit_store: tuple[ProfitStore, SecretRegistry, Path],
    ) -> None:
        store, _, _ = profit_store

        assert store.get_excluded_order_items(101) == frozenset()
        assert store.set_order_exclusion(101, 19_721, True)
        # Excluding an already excluded item changes nothing and does not fail.
        assert not store.set_order_exclusion(101, 19_721, True)
        assert store.set_order_exclusion(101, 24_292, True)
        assert store.set_order_exclusion(202, 8_871, True)

        assert store.get_excluded_order_items(101) == frozenset(
            {19_721, 24_292}
        )
        assert store.get_excluded_order_items(202) == frozenset({8_871})

        assert store.set_order_exclusion(101, 19_721, False)
        assert not store.set_order_exclusion(101, 19_721, False)

        assert store.get_excluded_order_items(101) == frozenset({24_292})
        assert store.get_excluded_order_items(202) == frozenset({8_871})

    def test_refuses_an_exclusion_without_a_usable_item(
        self,
        profit_store: tuple[ProfitStore, SecretRegistry, Path],
    ) -> None:
        store, _, _ = profit_store

        with pytest.raises(ValueError):
            store.set_order_exclusion(101, 0, True)

    def test_replacing_a_key_keeps_the_window_and_exclusions(
        self,
        profit_store: tuple[ProfitStore, SecretRegistry, Path],
    ) -> None:
        store, _, _ = profit_store
        store.set_api_key(101, "first-member-secret")
        store.set_report_days(101, 60)
        store.set_order_exclusion(101, 19_721, True)

        store.set_api_key(101, "replacement-member-secret")

        assert store.get_report_days(101) == 60
        assert store.get_excluded_order_items(101) == frozenset({19_721})

    def test_deleting_a_key_takes_the_window_and_exclusions_with_it(
        self,
        profit_store: tuple[ProfitStore, SecretRegistry, Path],
    ) -> None:
        store, _, _ = profit_store
        store.set_api_key(101, "member-secret")
        store.set_report_days(101, 60)
        store.set_order_exclusion(101, 19_721, True)
        store.set_api_key(202, "other-member-secret")
        store.set_report_days(202, 7)
        store.set_order_exclusion(202, 8_871, True)

        assert store.delete_api_key(101)

        assert store.get_report_days(101) is None
        assert store.get_excluded_order_items(101) == frozenset()
        assert store.get_report_days(202) == 7
        assert store.get_excluded_order_items(202) == frozenset({8_871})


class TestProfitService:
    async def test_refreshes_stale_collections_then_uses_the_member_cache(
        self,
        profit_store: tuple[ProfitStore, SecretRegistry, Path],
    ) -> None:
        store, _, _ = profit_store
        store.set_api_key(101, "member-secret")
        now = datetime(2026, 8, 21, 18, 30, tzinfo=UTC)
        buys = [
            transaction(
                "buy",
                price=100,
                quantity=10,
                occurred_at=now - timedelta(days=2),
            )
        ]
        sells = [
            transaction(
                "sell",
                price=200,
                quantity=5,
                occurred_at=now - timedelta(days=1),
            )
        ]
        current = [
            transaction(
                "current",
                price=300,
                quantity=5,
                occurred_at=now,
            )
        ]

        orders = [
            transaction(
                "order",
                price=90,
                quantity=20,
                occurred_at=now,
            )
        ]

        async def fetched(path: str, api_key: str) -> list[Transaction]:
            assert api_key == "member-secret"
            if path.endswith("history/buys"):
                return buys
            if path.endswith("history/sells"):
                return sells
            if path.endswith("current/buys"):
                return orders
            return current

        service = ProfitService(
            store,
            cast(aiohttp.ClientSession, None),
            "https://api.example",
        )
        api = SimpleNamespace(
            fetch_transactions=AsyncMock(side_effect=fetched),
            fetch_delivery=AsyncMock(
                return_value=(12_345, (DeliveryItem(1, 7),))
            ),
            fetch_item_names=AsyncMock(return_value={1: "Test Item"}),
            fetch_market_prices=AsyncMock(return_value={1: MarketPrice(100, 200)}),
        )
        service._api = api  # type: ignore[assignment]

        first = await service.load_report(101, 30, now=now)
        second = await service.load_report(101, 30, now=now)

        assert first == second
        assert first.window_start == datetime(2026, 7, 23, tzinfo=UTC)
        assert first.window_end == now
        assert first.item_names == {1: "Test Item"}
        assert first.realized.total_profit == 350
        assert first.unrealized.total_projected_profit == 775
        assert first.unclaimed_coins == 12_345
        assert first.unclaimed_items == (DeliveryItem(1, 7),)
        assert first.open_buy_orders == (
            OpenBuyOrder(
                item_id=1,
                unit_price=90,
                quantity=20,
                order_count=1,
                placed_at=now,
            ),
        )
        assert first.open_orders_available
        payload = cast(dict[str, Any], serialize_profit_report(first))
        assert payload["summary"]["roi_percent"] == 70
        assert payload["items"][0]["roi_percent"] == 70
        assert payload["items"][0]["median_hold_seconds"] == 86_400
        assert payload["items"][0]["profit_share_percent"] == 100
        assert payload["picks"] == [
            {
                "item_id": 1,
                "name": "Test Item",
                "buy_price": 100,
                "sell_price": 200,
                "net_revenue": 170,
                "profit": 70,
                "roi_percent": 70,
            }
        ]
        assert payload["unrealized"]["roi_percent"] == 155
        assert payload["unrealized"]["items"][0]["roi_percent"] == 155
        assert payload["delivery"]["coins"] == 12_345
        assert payload["delivery"]["items"] == [
            {"item_id": 1, "name": "Test Item", "quantity": 7}
        ]
        assert payload["open_orders"]["available"]
        assert payload["open_orders"]["excluded"] == []
        assert payload["open_orders"]["orders"] == [
            {
                "item_id": 1,
                "name": "Test Item",
                "quantity": 20,
                "order_count": 1,
                "unit_price": 90,
                "cost": 1_800,
                "buy_price": 100,
                "sell_price": 200,
                "net_revenue": 170,
                "profit": 80,
                "total_profit": 1_600,
                "roi_percent": pytest.approx(88.888, rel=1e-3),
                "has_order": True,
            }
        ]
        assert api.fetch_transactions.await_count == 4
        assert api.fetch_delivery.await_count == 2
        api.fetch_delivery.assert_awaited_with("member-secret")
        api.fetch_item_names.assert_awaited_once_with({1})
        assert api.fetch_market_prices.await_count == 2

    async def test_legacy_restricted_key_keeps_its_cached_report(
        self,
        profit_store: tuple[ProfitStore, SecretRegistry, Path],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        store, _, _ = profit_store
        secret = "legacy-route-restricted-secret"
        store.set_api_key(101, secret)
        now = datetime(2026, 8, 21, tzinfo=UTC)
        for transaction_kind in TRANSACTION_PATHS:
            store.touch_cache(101, transaction_kind, now=now)
        service = ProfitService(
            store,
            cast(aiohttp.ClientSession, None),
            "https://api.example",
        )
        api = SimpleNamespace(
            fetch_transactions=AsyncMock(),
            fetch_delivery=AsyncMock(
                side_effect=ProfitApiAuthorizationError(
                    "GW2 API request returned HTTP 403"
                )
            ),
            fetch_item_names=AsyncMock(),
            fetch_market_prices=AsyncMock(return_value={}),
        )
        service._api = api  # type: ignore[assignment]

        with caplog.at_level(logging.DEBUG, logger="gw2bot"):
            report = await service.load_report(101, 30, now=now)

        assert report.unclaimed_coins is None
        payload = cast(dict[str, Any], serialize_profit_report(report))
        assert payload["delivery"] == {"coins": None, "items": None}
        assert payload["open_orders"] == {
            "available": True,
            "orders": [],
            "excluded": [],
        }
        api.fetch_transactions.assert_not_awaited()
        api.fetch_delivery.assert_awaited_once_with(secret)
        assert "reason=unauthorized" in caplog.text
        assert "unclaimed_delivery=unavailable" in caplog.text
        assert secret not in caplog.text

    async def test_non_authorization_delivery_failure_still_fails_report(
        self,
        profit_store: tuple[ProfitStore, SecretRegistry, Path],
    ) -> None:
        store, _, _ = profit_store
        store.set_api_key(101, "member-secret")
        now = datetime(2026, 8, 21, tzinfo=UTC)
        for transaction_kind in TRANSACTION_PATHS:
            store.touch_cache(101, transaction_kind, now=now)
        service = ProfitService(
            store,
            cast(aiohttp.ClientSession, None),
            "https://api.example",
        )
        api = SimpleNamespace(
            fetch_transactions=AsyncMock(),
            fetch_delivery=AsyncMock(
                side_effect=ProfitApiError("GW2 API request returned HTTP 500")
            ),
            fetch_item_names=AsyncMock(),
            fetch_market_prices=AsyncMock(return_value={}),
        )
        service._api = api  # type: ignore[assignment]

        with pytest.raises(ProfitApiError):
            await service.load_report(101, 30, now=now)

    async def test_discards_an_old_key_snapshot_and_retries_replacement(
        self,
        profit_store: tuple[ProfitStore, SecretRegistry, Path],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        store, _, _ = profit_store
        old_key = "old-member-secret"
        replacement_key = "replacement-member-secret"
        store.set_api_key(101, old_key)
        now = datetime(2026, 8, 21, tzinfo=UTC)
        replaced = False

        async def fetched(path: str, api_key: str) -> list[Transaction]:
            nonlocal replaced
            if api_key == old_key:
                prefix = "old"
                if not replaced:
                    replaced = True
                    store.set_api_key(101, replacement_key)
            else:
                assert api_key == replacement_key
                prefix = "replacement"
            if path.endswith("history/buys"):
                return [
                    transaction(
                        f"{prefix}-buy",
                        price=100,
                        quantity=5,
                        occurred_at=now - timedelta(days=2),
                    )
                ]
            if path.endswith("history/sells"):
                return [
                    transaction(
                        f"{prefix}-sell",
                        price=200,
                        quantity=5,
                        occurred_at=now - timedelta(days=1),
                    )
                ]
            return []

        service = ProfitService(
            store,
            cast(aiohttp.ClientSession, None),
            "https://api.example",
        )
        api = SimpleNamespace(
            fetch_transactions=AsyncMock(side_effect=fetched),
            fetch_delivery=AsyncMock(return_value=(0, ())),
            fetch_item_names=AsyncMock(return_value={1: "Test Item"}),
            fetch_market_prices=AsyncMock(return_value={}),
        )
        service._api = api  # type: ignore[assignment]

        with caplog.at_level(logging.DEBUG, logger="gw2bot"):
            report = await service.load_report(101, 30, now=now)

        assert report.realized.total_profit == 350
        assert api.fetch_transactions.await_count == 8
        api.fetch_delivery.assert_awaited_once_with(replacement_key)
        assert [
            row.transaction_id
            for row in store.get_transactions(101, "history_buys")
        ] == ["replacement-buy"]
        assert [
            row.transaction_id
            for row in store.get_transactions(101, "history_sells")
        ] == ["replacement-sell"]
        assert store.get_transactions(101, "current_sells") == []
        assert all(
            store.is_cache_fresh(101, transaction_kind, 300, now=now)
            for transaction_kind in TRANSACTION_PATHS
        )
        assert "Discarded stale profit transaction snapshot" in caplog.text
        assert old_key not in caplog.text
        assert replacement_key not in caplog.text

    async def test_refuses_a_member_without_using_another_members_key(
        self,
        profit_store: tuple[ProfitStore, SecretRegistry, Path],
    ) -> None:
        store, _, _ = profit_store
        store.set_api_key(101, "member-secret")
        service = ProfitService(
            store,
            cast(aiohttp.ClientSession, None),
            "https://api.example",
        )

        with pytest.raises(MissingProfitApiKey):
            await service.load_report(202, 30)

    async def test_refreshes_an_expired_item_name(
        self,
        profit_store: tuple[ProfitStore, SecretRegistry, Path],
    ) -> None:
        store, _, _ = profit_store
        now = datetime(2026, 8, 21, tzinfo=UTC)
        store.set_api_key(101, "member-secret")
        store.store_transactions(
            101,
            "history_buys",
            [transaction("buy", quantity=5)],
            now=now,
        )
        store.store_transactions(
            101,
            "history_sells",
            [
                transaction(
                    "sell",
                    price=200,
                    quantity=5,
                    occurred_at=datetime(2026, 8, 2, tzinfo=UTC),
                )
            ],
            now=now,
        )
        store.store_transactions(101, "current_sells", [], now=now)
        for transaction_kind in TRANSACTION_PATHS:
            store.touch_cache(101, transaction_kind, now=now)
        store.store_item_names(
            {1: "Old Name"},
            now=now - timedelta(seconds=300),
        )
        service = ProfitService(
            store,
            cast(aiohttp.ClientSession, None),
            "https://api.example",
        )
        api = SimpleNamespace(
            fetch_transactions=AsyncMock(),
            fetch_delivery=AsyncMock(return_value=(0, ())),
            fetch_item_names=AsyncMock(return_value={1: "New Name"}),
            fetch_market_prices=AsyncMock(return_value={}),
        )
        service._api = api  # type: ignore[assignment]

        report = await service.load_report(101, 30, now=now)

        assert report.item_names == {1: "New Name"}
        api.fetch_transactions.assert_not_awaited()
        api.fetch_item_names.assert_awaited_once_with({1})
        assert store.get_item_names({1}, 300, now=now) == {1: "New Name"}


    async def test_excluded_items_leave_the_open_order_table(
        self,
        profit_store: tuple[ProfitStore, SecretRegistry, Path],
    ) -> None:
        store, _, _ = profit_store
        store.set_api_key(101, "member-secret")
        now = datetime(2026, 8, 21, tzinfo=UTC)
        store.store_transactions(
            101,
            "current_buys",
            [
                transaction("kept", item_id=1, price=90, quantity=10),
                transaction("hidden", item_id=2, price=40, quantity=5),
            ],
            now=now,
        )
        for transaction_kind in TRANSACTION_PATHS:
            store.touch_cache(101, transaction_kind, now=now)
        store.set_order_exclusion(101, 2, True)
        # An item excluded while it had an order but with none left today
        # still has to be offered back to the member.
        store.set_order_exclusion(101, 3, True)
        service = ProfitService(
            store,
            cast(aiohttp.ClientSession, None),
            "https://api.example",
        )
        api = SimpleNamespace(
            fetch_transactions=AsyncMock(),
            fetch_delivery=AsyncMock(return_value=(0, ())),
            fetch_item_names=AsyncMock(
                return_value={1: "Kept", 2: "Hidden", 3: "Gone"}
            ),
            fetch_market_prices=AsyncMock(
                return_value={1: MarketPrice(100, 200)}
            ),
        )
        service._api = api  # type: ignore[assignment]

        report = await service.load_report(101, 30, now=now)

        assert report.excluded_order_items == frozenset({2, 3})
        api.fetch_market_prices.assert_awaited_once_with({1, 2})
        payload = cast(dict[str, Any], serialize_profit_report(report))
        orders = payload["open_orders"]
        assert [row["item_id"] for row in orders["orders"]] == [1]
        assert [row["item_id"] for row in orders["excluded"]] == [2, 3]
        assert orders["excluded"][0]["has_order"] is True
        assert orders["excluded"][0]["quantity"] == 5
        assert orders["excluded"][1] == {
            "item_id": 3,
            "name": "Gone",
            "quantity": 0,
            "order_count": 0,
            "unit_price": None,
            "cost": 0,
            "buy_price": None,
            "sell_price": None,
            "net_revenue": None,
            "profit": None,
            "total_profit": None,
            "roi_percent": None,
            "has_order": False,
        }

    async def test_an_unpriced_open_order_still_lists_without_a_return(
        self,
        profit_store: tuple[ProfitStore, SecretRegistry, Path],
    ) -> None:
        store, _, _ = profit_store
        store.set_api_key(101, "member-secret")
        now = datetime(2026, 8, 21, tzinfo=UTC)
        store.store_transactions(
            101,
            "current_buys",
            [transaction("order", item_id=1, price=90, quantity=10)],
            now=now,
        )
        for transaction_kind in TRANSACTION_PATHS:
            store.touch_cache(101, transaction_kind, now=now)
        service = ProfitService(
            store,
            cast(aiohttp.ClientSession, None),
            "https://api.example",
        )
        service._api = SimpleNamespace(  # type: ignore[assignment]
            fetch_transactions=AsyncMock(),
            fetch_delivery=AsyncMock(return_value=(0, ())),
            fetch_item_names=AsyncMock(return_value={}),
            fetch_market_prices=AsyncMock(return_value={}),
        )

        report = await service.load_report(101, 30, now=now)

        payload = cast(dict[str, Any], serialize_profit_report(report))
        order = payload["open_orders"]["orders"][0]
        assert order["name"] == "Item 1"
        assert order["quantity"] == 10
        assert order["cost"] == 900
        assert order["buy_price"] is None
        assert order["sell_price"] is None
        assert order["profit"] is None
        assert order["roi_percent"] is None

    async def test_legacy_key_without_buy_orders_keeps_the_rest_of_the_report(
        self,
        profit_store: tuple[ProfitStore, SecretRegistry, Path],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        store, _, _ = profit_store
        secret = "buy-order-restricted-secret"
        store.set_api_key(101, secret)
        now = datetime(2026, 8, 21, tzinfo=UTC)
        for transaction_kind in TRANSACTION_PATHS:
            if transaction_kind != "current_buys":
                store.touch_cache(101, transaction_kind, now=now)

        async def fetched(path: str, api_key: str) -> list[Transaction]:
            assert api_key == secret
            raise ProfitApiAuthorizationError(
                "GW2 API request returned HTTP 403"
            )

        service = ProfitService(
            store,
            cast(aiohttp.ClientSession, None),
            "https://api.example",
        )
        api = SimpleNamespace(
            fetch_transactions=AsyncMock(side_effect=fetched),
            fetch_delivery=AsyncMock(return_value=(0, ())),
            fetch_item_names=AsyncMock(return_value={}),
            fetch_market_prices=AsyncMock(return_value={}),
        )
        service._api = api  # type: ignore[assignment]

        with caplog.at_level(logging.DEBUG, logger="gw2bot"):
            report = await service.load_report(101, 30, now=now)

        assert not report.open_orders_available
        assert report.open_buy_orders == ()
        payload = cast(dict[str, Any], serialize_profit_report(report))
        assert payload["open_orders"]["available"] is False
        # The collection was not stored as an empty snapshot, so a replacement
        # key picks the buy orders up on the next report.
        assert not store.is_cache_fresh(101, "current_buys", 300, now=now)
        assert "open_orders=unavailable" in caplog.text
        assert secret not in caplog.text

    async def test_an_unauthorized_sale_history_still_fails_the_report(
        self,
        profit_store: tuple[ProfitStore, SecretRegistry, Path],
    ) -> None:
        store, _, _ = profit_store
        store.set_api_key(101, "member-secret")
        now = datetime(2026, 8, 21, tzinfo=UTC)
        service = ProfitService(
            store,
            cast(aiohttp.ClientSession, None),
            "https://api.example",
        )
        service._api = SimpleNamespace(  # type: ignore[assignment]
            fetch_transactions=AsyncMock(
                side_effect=ProfitApiAuthorizationError(
                    "GW2 API request returned HTTP 403"
                )
            ),
            fetch_delivery=AsyncMock(return_value=(0, ())),
            fetch_item_names=AsyncMock(return_value={}),
            fetch_market_prices=AsyncMock(return_value={}),
        )

        with pytest.raises(ProfitApiAuthorizationError):
            await service.load_report(101, 30, now=now)

    async def test_report_window_is_remembered_once_a_member_picks_one(
        self,
        profit_store: tuple[ProfitStore, SecretRegistry, Path],
    ) -> None:
        store, _, _ = profit_store
        service = ProfitService(
            store,
            cast(aiohttp.ClientSession, None),
            "https://api.example",
        )

        assert await service.resolve_report_days(101, None) == 30
        assert await service.resolve_report_days(101, 60) == 60
        assert await service.resolve_report_days(101, None) == 60
        assert await service.resolve_report_days(202, None) == 30

    async def test_report_window_refuses_a_value_outside_the_range(
        self,
        profit_store: tuple[ProfitStore, SecretRegistry, Path],
    ) -> None:
        store, _, _ = profit_store
        service = ProfitService(
            store,
            cast(aiohttp.ClientSession, None),
            "https://api.example",
        )

        with pytest.raises(ValueError):
            await service.resolve_report_days(101, 91)

        assert store.get_report_days(101) is None

    async def test_order_exclusions_are_stored_through_the_service(
        self,
        profit_store: tuple[ProfitStore, SecretRegistry, Path],
    ) -> None:
        store, _, _ = profit_store
        service = ProfitService(
            store,
            cast(aiohttp.ClientSession, None),
            "https://api.example",
        )

        assert await service.set_order_exclusion(101, 19_721, True)
        assert store.get_excluded_order_items(101) == frozenset({19_721})
        assert await service.set_order_exclusion(101, 19_721, False)
        assert store.get_excluded_order_items(101) == frozenset()


class _FakeResponse:
    def __init__(
        self,
        status: int,
        payload: object,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status = status
        self._payload = payload
        self.headers = {} if headers is None else headers

    async def __aenter__(self) -> _FakeResponse:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def json(self) -> object:
        return self._payload


class TestProfitApiLogging:
    async def test_fetches_current_market_prices_without_logging_payload(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        payload_secret = "market-payload-secret"
        http = SimpleNamespace(
            get=MagicMock(
                return_value=_FakeResponse(
                    200,
                    [
                        {
                            "id": 1,
                            "note": payload_secret,
                            "buys": {"unit_price": 100},
                            "sells": {"unit_price": 200},
                        },
                        {
                            "id": 2,
                            "buys": {"unit_price": 0},
                            "sells": {"unit_price": 300},
                        },
                    ],
                )
            )
        )
        client = ProfitApiClient(
            cast(aiohttp.ClientSession, http),
            "https://api.example",
        )

        with caplog.at_level(logging.DEBUG, logger="gw2bot"):
            prices = await client.fetch_market_prices({2, 1})

        assert prices == {1: MarketPrice(100, 200)}
        assert payload_secret not in caplog.text
        request = http.get.call_args
        assert request.args[0] == "https://api.example/v2/commerce/prices"
        assert request.kwargs["params"] == {"ids": "1,2"}

    @pytest.mark.parametrize(
        "restricted_urls",
        [None, [*TRANSACTION_PATHS.values(), DELIVERY_PATH]],
        ids=("unrestricted", "required-routes"),
    )
    async def test_accepts_keys_with_required_transaction_route_access(
        self,
        restricted_urls: list[str] | None,
    ) -> None:
        payload: dict[str, object] = {"permissions": ["tradingpost"]}
        if restricted_urls is not None:
            payload["urls"] = restricted_urls
        http = SimpleNamespace(
            get=MagicMock(return_value=_FakeResponse(200, payload))
        )
        client = ProfitApiClient(
            cast(aiohttp.ClientSession, http),
            "https://api.example",
        )

        assert await client.validate_key("member-key")

    async def test_rejects_subtoken_without_every_transaction_route(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        secret = "route-restricted-member-key"
        payload_secret = "tokeninfo-name-secret"
        http = SimpleNamespace(
            get=MagicMock(
                return_value=_FakeResponse(
                    200,
                    {
                        "name": payload_secret,
                        "permissions": ["tradingpost"],
                        "urls": [
                            TRANSACTION_PATHS["history_buys"],
                            TRANSACTION_PATHS["history_sells"],
                        ],
                    },
                )
            )
        )
        client = ProfitApiClient(
            cast(aiohttp.ClientSession, http),
            "https://api.example",
        )

        with caplog.at_level(logging.DEBUG, logger="gw2bot"):
            assert not await client.validate_key(secret)

        assert secret not in caplog.text
        assert payload_secret not in caplog.text

    async def test_rejects_subtoken_without_the_delivery_route(self) -> None:
        http = SimpleNamespace(
            get=MagicMock(
                return_value=_FakeResponse(
                    200,
                    {
                        "permissions": ["tradingpost"],
                        "urls": list(TRANSACTION_PATHS.values()),
                    },
                )
            )
        )
        client = ProfitApiClient(
            cast(aiohttp.ClientSession, http),
            "https://api.example",
        )

        assert not await client.validate_key("member-key")

    async def test_fetches_unclaimed_delivery_coins_and_items(self) -> None:
        http = SimpleNamespace(
            get=MagicMock(
                return_value=_FakeResponse(
                    200,
                    {
                        "coins": 12_345,
                        "items": [
                            {"id": 2, "count": 2},
                            {"id": 1, "count": 3},
                            {"id": 2, "count": 5},
                        ],
                    },
                )
            )
        )
        client = ProfitApiClient(
            cast(aiohttp.ClientSession, http),
            "https://api.example",
        )

        # Two stacks of the same item are one delivery row, and the rows come
        # back ordered so the dashboard renders the same list every reload.
        assert await client.fetch_delivery("member-key") == (
            12_345,
            (DeliveryItem(1, 3), DeliveryItem(2, 7)),
        )
        request = http.get.call_args
        assert request.args[0] == "https://api.example/v2/commerce/delivery"
        assert request.kwargs["headers"] == {
            "Authorization": "Bearer member-key"
        }

    @pytest.mark.parametrize(
        "items",
        [
            [{"count": 2}],
            [{"id": 0, "count": 2}],
            [{"id": 1}],
            [{"id": 1, "count": -1}],
            [{"id": True, "count": 2}],
        ],
        ids=(
            "no-id",
            "zero-id",
            "no-count",
            "negative-count",
            "boolean-id",
        ),
    )
    async def test_rejects_a_delivery_item_without_usable_fields(
        self,
        items: list[dict[str, object]],
    ) -> None:
        http = SimpleNamespace(
            get=MagicMock(
                return_value=_FakeResponse(200, {"coins": 0, "items": items})
            )
        )
        client = ProfitApiClient(
            cast(aiohttp.ClientSession, http),
            "https://api.example",
        )

        with pytest.raises(ProfitApiError):
            await client.fetch_delivery("member-key")

    @pytest.mark.parametrize("status", [401, 403])
    async def test_delivery_authorization_failure_is_typed_and_sanitized(
        self,
        status: int,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        secret = "legacy-route-restricted-secret"
        response_secret = "authorization-response-secret"
        http = SimpleNamespace(
            get=MagicMock(
                return_value=_FakeResponse(
                    status,
                    {"text": response_secret},
                )
            )
        )
        client = ProfitApiClient(
            cast(aiohttp.ClientSession, http),
            "https://api.example",
        )

        with caplog.at_level(logging.DEBUG, logger="gw2bot"):
            with pytest.raises(ProfitApiAuthorizationError):
                await client.fetch_delivery(secret)

        assert secret not in caplog.text
        assert response_secret not in caplog.text

    async def test_invalid_delivery_never_logs_key_or_payload(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        secret = "member-delivery-key"
        payload_secret = "delivery-payload-secret"
        http = SimpleNamespace(
            get=MagicMock(
                return_value=_FakeResponse(200, {"coins": payload_secret})
            )
        )
        client = ProfitApiClient(
            cast(aiohttp.ClientSession, http),
            "https://api.example",
        )

        with caplog.at_level(logging.DEBUG, logger="gw2bot"):
            with pytest.raises(ProfitApiError):
                await client.fetch_delivery(secret)

        assert secret not in caplog.text
        assert payload_secret not in caplog.text

    async def test_paginates_and_parses_transaction_collections(self) -> None:
        http = SimpleNamespace(
            get=MagicMock(
                side_effect=[
                    _FakeResponse(
                        200,
                        [
                            {
                                "id": 10,
                                "item_id": 1,
                                "price": 100,
                                "quantity": 2,
                                "purchased": "2026-08-20T01:00:00Z",
                            }
                        ],
                        {"X-Page-Total": "2"},
                    ),
                    _FakeResponse(
                        200,
                        [
                            {
                                "id": 11,
                                "item_id": 2,
                                "price": 200,
                                "quantity": 1,
                                "created": "2026-08-21T01:00:00Z",
                            }
                        ],
                    ),
                ]
            )
        )
        client = ProfitApiClient(
            cast(aiohttp.ClientSession, http),
            "https://api.example/",
        )

        rows = await client.fetch_transactions(
            "/v2/commerce/transactions/history/buys",
            "member-key",
        )

        assert [row.transaction_id for row in rows] == ["10", "11"]
        assert rows[0].occurred_at == datetime(2026, 8, 20, 1, tzinfo=UTC)
        assert [call.kwargs["params"]["page"] for call in http.get.call_args_list] == [
            "0",
            "1",
        ]

    async def test_an_http_failure_never_logs_the_key_or_response_body(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        secret = "member-profit-key"
        response_secret = "response-body-secret"
        http = SimpleNamespace(
            get=MagicMock(
                return_value=_FakeResponse(
                    401,
                    {"text": response_secret},
                )
            )
        )
        client = ProfitApiClient(
            cast(aiohttp.ClientSession, http),
            "https://api.example",
        )

        with caplog.at_level(logging.DEBUG, logger="gw2bot"):
            with pytest.raises(ProfitApiError):
                await client.validate_key(secret)

        assert secret not in caplog.text
        assert response_secret not in caplog.text
        request = http.get.call_args
        assert request.kwargs["headers"] == {
            "Authorization": f"Bearer {secret}"
        }


class _InteractionResponse:
    def __init__(self) -> None:
        self.done = False
        self.send_message = AsyncMock(side_effect=self._send)
        self.defer = AsyncMock(side_effect=self._defer)
        self.send_modal = AsyncMock()

    def is_done(self) -> bool:
        return self.done

    async def _send(self, *args: object, **kwargs: object) -> None:
        self.done = True

    async def _defer(self, *args: object, **kwargs: object) -> None:
        self.done = True


def interaction(user_id: int = 101) -> Any:
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id),
        response=_InteractionResponse(),
        followup=SimpleNamespace(send=AsyncMock()),
    )


class TestProfitCommands:
    def test_every_command_is_under_the_profit_group(self) -> None:
        group = ProfitCommands(cast(Any, object()))

        assert group.name == "profit"
        assert group.guild_only
        assert {command.name for command in group.commands} == {
            "setkey",
            "deletekey",
            "view",
        }

    def test_key_modal_allows_full_length_subtokens(self) -> None:
        modal = ProfitApiKeyModal(cast(Any, object()))

        assert modal.api_key.max_length == 4000

    async def test_view_links_the_combined_web_dashboard(self) -> None:
        bot = SimpleNamespace(
            _config=default_config(
                web_enabled=True,
                web_base_url="https://guild.example",
                discord_oauth_client_id="client",
                discord_oauth_client_secret="secret",
                web_session_secret="s" * 32,
            )
        )
        group = ProfitCommands(cast(Any, bot))
        command = next(command for command in group.commands if command.name == "view")
        invoked = interaction()

        await command.callback(group, invoked, 60)  # type: ignore[arg-type]

        invoked.response.send_message.assert_awaited_once_with(
            "[Open your 60-day profit dashboard](https://guild.example/profit?days=60)",
            ephemeral=True,
        )

    async def test_modal_registers_a_candidate_before_a_failed_validation(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        secret = "candidate-profit-secret"
        registry = SecretRegistry()
        bot = SimpleNamespace(
            secrets=registry,
            profit_service=SimpleNamespace(
                validate_api_key=AsyncMock(
                    side_effect=ProfitApiError(f"rejected {secret}")
                )
            ),
        )
        modal = ProfitApiKeyModal(cast(Any, bot))
        modal.api_key._value = secret
        invoked = interaction()

        with caplog.at_level(logging.WARNING, logger="gw2bot"):
            await modal.on_submit(cast(discord.Interaction, invoked))

        assert secret in registry.current()
        assert secret not in caplog.text
        reply = invoked.followup.send.await_args.args[0]
        assert secret not in reply

    async def test_modal_names_the_delivery_route_when_rejecting_a_key(
        self,
    ) -> None:
        bot = SimpleNamespace(
            secrets=SecretRegistry(),
            profit_service=SimpleNamespace(
                validate_api_key=AsyncMock(return_value=False)
            ),
        )
        modal = ProfitApiKeyModal(cast(Any, bot))
        modal.api_key._value = "route-restricted-key"
        invoked = interaction()

        await modal.on_submit(cast(discord.Interaction, invoked))

        reply = invoked.followup.send.await_args.args[0]
        assert "all Trading Post transaction routes" in reply
        assert f"`{DELIVERY_PATH}`" in reply
        assert "subtoken URL restrictions" in reply
