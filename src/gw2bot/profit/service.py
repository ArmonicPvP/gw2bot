from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

import aiohttp

from gw2bot.profit.api import (
    TRANSACTION_PATHS,
    ProfitApiClient,
    ProfitApiAuthorizationError,
    ProfitApiError,
)
from gw2bot.profit.models import (
    MIN_FLIP_QUANTITY,
    DeliveryItem,
    OpenBuyOrder,
    ProfitReport,
    Transaction,
    calculate_realized_profit,
    calculate_unrealized_profit,
    group_open_buy_orders,
    sale_fee_total,
)
from gw2bot.profit.store import (
    MAX_REPORT_DAYS,
    MIN_REPORT_DAYS,
    ProfitStore,
)

LOGGER = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 300
DEFAULT_REPORT_DAYS = 30

# The one transaction collection a member key saved before Open Orders existed
# may not reach. Every other collection failing authorization still fails the
# report, because without them there is nothing to show.
OPTIONAL_TRANSACTION_KIND = "current_buys"


class MissingProfitApiKey(RuntimeError):
    """The signed-in Discord member has not configured a profit API key."""


class ProfitService:
    def __init__(
        self,
        store: ProfitStore,
        http: aiohttp.ClientSession,
        base_url: str,
    ) -> None:
        self._store = store
        self._api = ProfitApiClient(http, base_url)

    async def validate_api_key(self, api_key: str) -> bool:
        LOGGER.debug("Validating a member profit API key")
        valid = await self._api.validate_key(api_key)
        LOGGER.debug("Member profit API key validation completed; valid=%s", valid)
        return valid

    async def resolve_report_days(
        self,
        discord_user_id: int,
        requested_days: int | None,
    ) -> int:
        """Return the window to report, remembering an explicit choice.

        A member who picks a window keeps it: the page opens on it again on
        their next visit, from any browser, because the choice is stored
        against their Discord ID rather than left in the URL they came from.
        """
        if requested_days is not None:
            if not MIN_REPORT_DAYS <= requested_days <= MAX_REPORT_DAYS:
                raise ValueError("Profit report days must be between 1 and 90")
            await asyncio.to_thread(
                self._store.set_report_days,
                discord_user_id,
                requested_days,
            )
            return requested_days
        stored = await asyncio.to_thread(
            self._store.get_report_days,
            discord_user_id,
        )
        LOGGER.debug(
            "Resolved profit report window; user_id=%s remembered=%s",
            discord_user_id,
            stored is not None,
        )
        return DEFAULT_REPORT_DAYS if stored is None else stored

    async def set_order_exclusion(
        self,
        discord_user_id: int,
        item_id: int,
        excluded: bool,
    ) -> bool:
        """Exclude or restore one item in the member's Open Orders table."""
        return await asyncio.to_thread(
            self._store.set_order_exclusion,
            discord_user_id,
            item_id,
            excluded,
        )

    async def load_report(
        self,
        discord_user_id: int,
        days: int,
        *,
        now: datetime | None = None,
    ) -> ProfitReport:
        if not MIN_REPORT_DAYS <= days <= MAX_REPORT_DAYS:
            raise ValueError("Profit report days must be between 1 and 90")
        loaded_at = datetime.now(UTC) if now is None else now
        window_start = loaded_at.replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        ) - timedelta(days=days - 1)
        LOGGER.debug(
            "Loading profit report; user_id=%s days=%s",
            discord_user_id,
            days,
        )
        api_key, orders_available = await self._refresh_transactions(
            discord_user_id,
            loaded_at,
        )
        unclaimed_coins, unclaimed_items = await self._load_delivery(
            discord_user_id,
            api_key,
        )

        cutoff = window_start
        (
            buys,
            sells,
            current_sells,
            current_buys,
            excluded_order_items,
        ) = await asyncio.to_thread(
            self._read_member_data,
            discord_user_id,
            cutoff,
        )
        realized = await asyncio.to_thread(
            calculate_realized_profit,
            buys,
            sells,
            minimum_flip_quantity=MIN_FLIP_QUANTITY,
        )
        unrealized = await asyncio.to_thread(
            calculate_unrealized_profit,
            realized.unmatched_buys,
            current_sells,
        )
        open_buy_orders = await asyncio.to_thread(
            group_open_buy_orders,
            current_buys,
        )
        order_item_ids = {order.item_id for order in open_buy_orders}
        # One price lookup covers both readers of it: the picks built from
        # items already flipped, and every open buy order's current spread.
        market_prices = await self._api.fetch_market_prices(
            set(realized.items) | order_item_ids
        )
        item_ids = (
            set(realized.items)
            | set(unrealized.items)
            | order_item_ids
            # An excluded item with no open order still needs its name, or the
            # member cannot tell what they are restoring.
            | set(excluded_order_items)
            | {row.item_id for row in unclaimed_items or ()}
        )
        item_names = await asyncio.to_thread(
            self._store.get_item_names,
            item_ids,
            CACHE_TTL_SECONDS,
            now=loaded_at,
        )
        missing_ids = item_ids - set(item_names)
        if missing_ids:
            fetched_names = await self._api.fetch_item_names(missing_ids)
            if fetched_names:
                await asyncio.to_thread(
                    self._store.store_item_names,
                    fetched_names,
                    now=loaded_at,
                )
                item_names.update(fetched_names)
        for item_id in item_ids:
            item_names.setdefault(item_id, f"Item {item_id}")

        report = ProfitReport(
            days=days,
            window_start=window_start,
            window_end=loaded_at,
            buy_transaction_count=len(buys),
            sell_transaction_count=len(sells),
            realized=realized,
            unrealized=unrealized,
            unclaimed_coins=unclaimed_coins,
            unclaimed_items=unclaimed_items,
            item_names=item_names,
            market_prices=market_prices,
            open_buy_orders=open_buy_orders,
            open_orders_available=orders_available,
            excluded_order_items=excluded_order_items,
        )
        LOGGER.debug(
            "Loaded profit report; user_id=%s days=%s realized_items=%s "
            "unrealized_items=%s market_prices=%s unclaimed_delivery=%s "
            "delivery_items=%s open_order_rows=%s open_orders=%s "
            "excluded_order_items=%s",
            discord_user_id,
            days,
            len(realized.items),
            len(unrealized.items),
            len(market_prices),
            (
                "unavailable"
                if unclaimed_coins is None
                else "available" if unclaimed_coins > 0 else "empty"
            ),
            "unavailable" if unclaimed_items is None else len(unclaimed_items),
            len(open_buy_orders),
            "available" if orders_available else "unavailable",
            len(excluded_order_items),
        )
        return report

    async def _load_delivery(
        self,
        discord_user_id: int,
        api_key: str,
    ) -> tuple[int | None, tuple[DeliveryItem, ...] | None]:
        try:
            return await self._api.fetch_delivery(api_key)
        except ProfitApiAuthorizationError:
            # Keys accepted before delivery reporting existed may be subtokens
            # restricted to the original three transaction routes. Preserve
            # that member's established report and degrade only the new field.
            LOGGER.warning(
                "Could not load Trading Post delivery; user_id=%s "
                "reason=unauthorized",
                discord_user_id,
            )
            return None, None

    def _cache_freshness(
        self,
        discord_user_id: int,
        now: datetime,
    ) -> dict[str, bool]:
        return {
            kind: self._store.is_cache_fresh(
                discord_user_id,
                kind,
                CACHE_TTL_SECONDS,
                now=now,
            )
            for kind in TRANSACTION_PATHS
        }

    async def _fetch_collection(
        self,
        transaction_kind: str,
        api_key: str,
        discord_user_id: int,
    ) -> list[Transaction] | None:
        """Fetch one collection; None when only Open Orders is out of reach."""
        try:
            return await self._api.fetch_transactions(
                TRANSACTION_PATHS[transaction_kind],
                api_key,
            )
        except ProfitApiAuthorizationError:
            if transaction_kind != OPTIONAL_TRANSACTION_KIND:
                raise
            LOGGER.warning(
                "Could not load Trading Post buy orders; user_id=%s "
                "reason=unauthorized",
                discord_user_id,
            )
            return None

    async def _refresh_transactions(
        self,
        discord_user_id: int,
        now: datetime,
    ) -> tuple[str, bool]:
        for attempt in range(2):
            snapshot = await asyncio.to_thread(
                self._store.get_api_key_snapshot,
                discord_user_id,
            )
            if snapshot is None:
                LOGGER.debug(
                    "Skipped profit report; user_id=%s reason=api-key-unset",
                    discord_user_id,
                )
                raise MissingProfitApiKey
            fresh = await asyncio.to_thread(
                self._cache_freshness,
                discord_user_id,
                now,
            )
            stale_kinds = [
                kind for kind, is_fresh in fresh.items() if not is_fresh
            ]
            LOGGER.debug(
                "Resolved profit cache refresh; user_id=%s stale=%s fresh=%s "
                "attempt=%s",
                discord_user_id,
                len(stale_kinds),
                len(fresh) - len(stale_kinds),
                attempt + 1,
            )
            if not stale_kinds:
                return snapshot.api_key, True
            fetched = await asyncio.gather(
                *(
                    self._fetch_collection(
                        kind,
                        snapshot.api_key,
                        discord_user_id,
                    )
                    for kind in stale_kinds
                )
            )
            # A collection the key cannot reach is left out of the snapshot
            # rather than stored as an empty one, so its cache marker stays
            # stale and a replacement key picks it up on the next report.
            collections = [
                (kind, transactions)
                for kind, transactions in zip(stale_kinds, fetched, strict=True)
                if transactions is not None
            ]
            orders_available = len(collections) == len(stale_kinds)
            accepted = await asyncio.to_thread(
                self._store.store_transaction_snapshot,
                discord_user_id,
                snapshot.generation,
                collections,
                now=now,
            )
            if accepted:
                return snapshot.api_key, orders_available
            LOGGER.info(
                "Discarded stale profit transaction snapshot; user_id=%s "
                "attempt=%s retry=%s",
                discord_user_id,
                attempt + 1,
                attempt == 0,
            )
        raise ProfitApiError("GW2 API key changed during profit refresh")

    def _read_member_data(
        self,
        discord_user_id: int,
        cutoff: datetime,
    ) -> tuple[
        list[Transaction],
        list[Transaction],
        list[Transaction],
        list[Transaction],
        frozenset[int],
    ]:
        return (
            self._store.get_transactions(
                discord_user_id,
                "history_buys",
                cutoff,
            ),
            self._store.get_transactions(
                discord_user_id,
                "history_sells",
                cutoff,
            ),
            self._store.get_transactions(discord_user_id, "current_sells"),
            self._store.get_transactions(discord_user_id, "current_buys"),
            self._store.get_excluded_order_items(discord_user_id),
        )


def serialize_profit_report(report: ProfitReport) -> dict[str, object]:
    realized = report.realized
    unrealized = report.unrealized

    def percentage(numerator: int, denominator: int) -> float | None:
        return numerator / denominator * 100 if denominator else None

    items = [
        {
            "item_id": item_id,
            "name": report.item_names[item_id],
            "units": totals.matched_quantity,
            "cost": totals.cost,
            "net_revenue": totals.net_revenue,
            "profit": totals.profit,
            "roi_percent": percentage(totals.profit, totals.cost),
            "median_hold_seconds": totals.median_hold_seconds,
            "profit_share_percent": percentage(
                totals.profit,
                realized.total_profit,
            ),
        }
        for item_id, totals in sorted(
            realized.items.items(),
            key=lambda entry: entry[1].profit,
            reverse=True,
        )
    ]
    day_rows = [
        {
            "date": sold_day,
            "units": totals.matched_quantity,
            "cost": totals.cost,
            "net_revenue": totals.net_revenue,
            "profit": totals.profit,
        }
        for sold_day, totals in sorted(realized.days.items())
    ]
    unrealized_items = [
        {
            "item_id": item_id,
            "name": report.item_names[item_id],
            "units": totals.quantity,
            "cost": totals.cost,
            "projected_net_revenue": totals.projected_net_revenue,
            "projected_profit": totals.projected_profit,
            "roi_percent": percentage(
                totals.projected_profit,
                totals.cost,
            ),
        }
        for item_id, totals in sorted(
            unrealized.items.items(),
            key=lambda entry: entry[1].projected_profit,
            reverse=True,
        )
    ]
    picks = []
    skipped_picks = 0
    # Picks revisit the items this member already flipped. Prices are now
    # fetched for open buy orders too, so the realized items - not every
    # priced item - decide what belongs here.
    for item_id in sorted(realized.items):
        price = report.market_prices.get(item_id)
        if price is None:
            continue
        net_revenue = price.sell_unit_price - sale_fee_total(
            price.sell_unit_price, 1
        )
        profit = net_revenue - price.buy_unit_price
        roi_percent = percentage(profit, price.buy_unit_price)
        if roi_percent is not None and roi_percent < 0:
            skipped_picks += 1
            continue
        picks.append(
            {
                "item_id": item_id,
                "name": report.item_names[item_id],
                "buy_price": price.buy_unit_price,
                "sell_price": price.sell_unit_price,
                "net_revenue": net_revenue,
                "profit": profit,
                "roi_percent": roi_percent,
            }
        )
    LOGGER.debug(
        "Built profit picks; kept=%s skipped_negative_roi=%s",
        len(picks),
        skipped_picks,
    )
    open_orders, excluded_orders = _open_order_rows(report)
    return {
        "days": report.days,
        "window": {
            "start_date": report.window_start.date().isoformat(),
            "end_date": report.window_end.date().isoformat(),
        },
        "summary": {
            "buy_transactions": report.buy_transaction_count,
            "sell_transactions": report.sell_transaction_count,
            "matched_units": realized.total_matched_quantity,
            "cost": realized.total_cost,
            "net_revenue": realized.total_net_revenue,
            "profit": realized.total_profit,
            "roi_percent": percentage(
                realized.total_profit,
                realized.total_cost,
            ),
        },
        "items": items,
        "picks": picks,
        "days_table": day_rows,
        "unrealized": {
            "items": unrealized_items,
            "units": unrealized.total_quantity,
            "cost": unrealized.total_cost,
            "projected_net_revenue": unrealized.total_projected_net_revenue,
            "projected_profit": unrealized.total_projected_profit,
            "roi_percent": percentage(
                unrealized.total_projected_profit,
                unrealized.total_cost,
            ),
        },
        "open_orders": {
            "available": report.open_orders_available,
            "orders": open_orders,
            "excluded": excluded_orders,
        },
        "delivery": {
            "coins": report.unclaimed_coins,
            "items": (
                None
                if report.unclaimed_items is None
                else [
                    {
                        "item_id": row.item_id,
                        "name": report.item_names[row.item_id],
                        "quantity": row.quantity,
                    }
                    for row in report.unclaimed_items
                ]
            ),
        },
    }


def _open_order_rows(
    report: ProfitReport,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Split the member's open buy orders into kept and excluded rows.

    Both lists carry the same shape so the page can move a row between them
    when the member excludes or restores an item without reloading the report.
    """
    orders: list[dict[str, object]] = []
    excluded: list[dict[str, object]] = []
    for order in report.open_buy_orders:
        row = _open_order_row(report, order)
        if order.item_id in report.excluded_order_items:
            excluded.append(row)
        else:
            orders.append(row)
    # An item excluded while it had an order keeps its entry after the order
    # fills or is cancelled; without it the member has no way to restore it.
    listed = {order.item_id for order in report.open_buy_orders}
    for item_id in sorted(report.excluded_order_items - listed):
        excluded.append(
            {
                "item_id": item_id,
                "name": report.item_names.get(item_id, f"Item {item_id}"),
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
        )
    LOGGER.debug(
        "Built open Trading Post order rows; kept=%s excluded=%s priced=%s",
        len(orders),
        len(excluded),
        sum(1 for row in orders if row["profit"] is not None),
    )
    return orders, excluded


def _open_order_row(
    report: ProfitReport,
    order: OpenBuyOrder,
) -> dict[str, object]:
    price = report.market_prices.get(order.item_id)
    net_revenue: int | None = None
    profit: int | None = None
    total_profit: int | None = None
    roi_percent: float | None = None
    if price is not None:
        # The exit assumed here is the one the member controls: undercutting
        # nothing and selling at the current lowest listing, after both fees.
        net_revenue = price.sell_unit_price - sale_fee_total(
            price.sell_unit_price,
            1,
        )
        profit = net_revenue - order.unit_price
        total_profit = profit * order.quantity
        roi_percent = (
            profit / order.unit_price * 100 if order.unit_price else None
        )
    return {
        "item_id": order.item_id,
        "name": report.item_names.get(
            order.item_id,
            f"Item {order.item_id}",
        ),
        "quantity": order.quantity,
        "order_count": order.order_count,
        "unit_price": order.unit_price,
        "cost": order.unit_price * order.quantity,
        "buy_price": None if price is None else price.buy_unit_price,
        "sell_price": None if price is None else price.sell_unit_price,
        "net_revenue": net_revenue,
        "profit": profit,
        "total_profit": total_profit,
        "roi_percent": roi_percent,
        "has_order": True,
    }
