from __future__ import annotations

import logging
import math
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import UTC, datetime

LOGGER = logging.getLogger(__name__)

MIN_FLIP_QUANTITY = 5


@dataclass(frozen=True, slots=True)
class Transaction:
    transaction_id: str
    item_id: int
    price: int
    quantity: int
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class ItemProfit:
    matched_quantity: int
    cost: int
    net_revenue: int
    profit: int
    median_hold_seconds: float


@dataclass(frozen=True, slots=True)
class DayProfit:
    matched_quantity: int
    cost: int
    net_revenue: int
    profit: int


@dataclass(frozen=True, slots=True)
class BuyLot:
    remaining: int
    unit_price: int
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class RealizedProfit:
    items: dict[int, ItemProfit]
    days: dict[str, DayProfit]
    unmatched_buys: dict[int, tuple[BuyLot, ...]]
    total_cost: int
    total_net_revenue: int
    total_profit: int
    total_matched_quantity: int


@dataclass(frozen=True, slots=True)
class UnrealizedItemProfit:
    quantity: int
    cost: int
    projected_net_revenue: int
    projected_profit: int


@dataclass(frozen=True, slots=True)
class UnrealizedProfit:
    items: dict[int, UnrealizedItemProfit]
    total_quantity: int
    total_cost: int
    total_projected_net_revenue: int
    total_projected_profit: int


@dataclass(frozen=True, slots=True)
class MarketPrice:
    """Current highest buy order and lowest sell listing for one item."""

    buy_unit_price: int
    sell_unit_price: int


@dataclass(frozen=True, slots=True)
class DeliveryItem:
    """One item stack waiting for pickup in the Trading Post delivery box."""

    item_id: int
    quantity: int


@dataclass(frozen=True, slots=True)
class OpenBuyOrder:
    """The member's outstanding buy orders for one item at one price.

    The Trading Post splits a large purchase into many orders, so a member
    who is buying one item can hold dozens of rows that differ in nothing a
    reader cares about. They are collapsed per item and price; an item bought
    at two prices stays two rows, because the price is what decides the
    order's profit and return.
    """

    item_id: int
    unit_price: int
    quantity: int
    order_count: int
    placed_at: datetime


@dataclass(frozen=True, slots=True)
class ProfitReport:
    """The parts of the dashboard built from a member's trade history.

    Delivery and open orders are loaded apart from this, because neither
    depends on the history and both are ready long before it is.
    """

    days: int
    window_start: datetime
    window_end: datetime
    buy_transaction_count: int
    sell_transaction_count: int
    realized: RealizedProfit
    unrealized: UnrealizedProfit
    item_names: dict[int, str]
    market_prices: dict[int, MarketPrice] = field(default_factory=dict)
    # The oldest purchase or sale held for this member, which is how far back
    # a window can usefully be asked to reach.
    history_start: datetime | None = None


@dataclass(frozen=True, slots=True)
class DeliveryReport:
    """What is waiting for pickup, with the names to render it."""

    coins: int | None
    items: tuple[DeliveryItem, ...] | None
    item_names: dict[int, str]


@dataclass(frozen=True, slots=True)
class OpenOrdersReport:
    """The member's outstanding buy orders and what they are worth now."""

    orders: tuple[OpenBuyOrder, ...]
    available: bool
    excluded_items: frozenset[int]
    market_prices: dict[int, MarketPrice]
    item_names: dict[int, str]


@dataclass(slots=True)
class _Event:
    kind: str
    occurred_at: datetime
    unit_price: int
    quantity: int


@dataclass(slots=True)
class _MutableLot:
    remaining: int
    unit_price: int
    occurred_at: datetime


@dataclass(slots=True)
class _MutableListing:
    remaining: int
    unit_price: int
    occurred_at: datetime
    original_quantity: int


@dataclass(slots=True)
class _MutableOrder:
    quantity: int
    order_count: int
    placed_at: datetime


@dataclass(slots=True)
class _Totals:
    matched_quantity: int = 0
    cost: int = 0
    net_revenue: int = 0
    profit: int = 0


def _weighted_median_seconds(
    durations: list[tuple[float, int]],
) -> float:
    """Return the per-unit median without expanding large transaction lots."""
    total_units = sum(quantity for _, quantity in durations)
    if total_units <= 0:
        return 0.0
    lower_index = (total_units - 1) // 2
    upper_index = total_units // 2
    cumulative = 0
    lower_value: float | None = None
    for seconds, quantity in sorted(durations):
        cumulative += quantity
        if lower_value is None and cumulative > lower_index:
            lower_value = seconds
        if cumulative > upper_index:
            if lower_value is None:
                lower_value = seconds
            return (lower_value + seconds) / 2
    raise RuntimeError("Weighted median could not resolve its target unit")


def parse_gw2_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def sale_fee_total(unit_price: int, quantity: int) -> int:
    gross = unit_price * quantity
    return math.ceil(gross * 0.05) + math.ceil(gross * 0.10)


def allocated_net_revenue(
    unit_price: int,
    sold_quantity: int,
    matched_quantity: int,
    *,
    previously_matched_quantity: int = 0,
) -> int:
    """Allocate a sale's net revenue without losing FIFO slice remainders."""
    if sold_quantity <= 0:
        raise ValueError("sold_quantity must be positive")
    if matched_quantity < 0:
        raise ValueError("matched_quantity cannot be negative")
    if previously_matched_quantity < 0:
        raise ValueError("previously_matched_quantity cannot be negative")
    if previously_matched_quantity + matched_quantity > sold_quantity:
        raise ValueError("matched quantities cannot exceed sold_quantity")
    gross = unit_price * sold_quantity
    net = gross - sale_fee_total(unit_price, sold_quantity)
    allocated_before = (
        net * previously_matched_quantity // sold_quantity
    )
    allocated_through_match = (
        net
        * (previously_matched_quantity + matched_quantity)
        // sold_quantity
    )
    return allocated_through_match - allocated_before


def calculate_realized_profit(
    buys: list[Transaction],
    sells: list[Transaction],
    *,
    minimum_flip_quantity: int = 1,
) -> RealizedProfit:
    """Match sales to earlier purchases FIFO, mirroring the original bot."""
    if minimum_flip_quantity <= 0:
        raise ValueError("minimum_flip_quantity must be positive")
    events_by_item: dict[int, list[_Event]] = defaultdict(list)
    for transaction in buys:
        events_by_item[transaction.item_id].append(
            _Event(
                "buy",
                transaction.occurred_at,
                transaction.price,
                transaction.quantity,
            )
        )
    for transaction in sells:
        events_by_item[transaction.item_id].append(
            _Event(
                "sell",
                transaction.occurred_at,
                transaction.price,
                transaction.quantity,
            )
        )

    item_totals: dict[int, ItemProfit] = {}
    day_totals: dict[str, _Totals] = defaultdict(_Totals)
    unmatched: dict[int, tuple[BuyLot, ...]] = {}
    total_cost = 0
    total_net_revenue = 0
    total_profit = 0
    total_matched_quantity = 0
    excluded_items = 0

    for item_id, events in events_by_item.items():
        events.sort(
            key=lambda event: (
                event.occurred_at,
                0 if event.kind == "buy" else 1,
            )
        )
        buy_lots: deque[_MutableLot] = deque()
        item = _Totals()
        item_days: dict[str, _Totals] = defaultdict(_Totals)
        holding_durations: list[tuple[float, int]] = []

        for event in events:
            if event.kind == "buy":
                buy_lots.append(
                    _MutableLot(
                        event.quantity,
                        event.unit_price,
                        event.occurred_at,
                    )
                )
                continue

            sell_remaining = event.quantity
            sell_matched = 0
            while (
                sell_remaining > 0
                and buy_lots
                and buy_lots[0].occurred_at < event.occurred_at
            ):
                buy_lot = buy_lots[0]
                matched = min(sell_remaining, buy_lot.remaining)
                cost = buy_lot.unit_price * matched
                net_revenue = allocated_net_revenue(
                    event.unit_price,
                    event.quantity,
                    matched,
                    previously_matched_quantity=sell_matched,
                )
                profit = net_revenue - cost
                holding_seconds = max(
                    0.0,
                    (
                        event.occurred_at - buy_lot.occurred_at
                    ).total_seconds(),
                )
                holding_durations.append(
                    (holding_seconds, matched)
                )
                item.matched_quantity += matched
                item.cost += cost
                item.net_revenue += net_revenue
                item.profit += profit

                sold_day = event.occurred_at.date().isoformat()
                day = item_days[sold_day]
                day.matched_quantity += matched
                day.cost += cost
                day.net_revenue += net_revenue
                day.profit += profit

                buy_lot.remaining -= matched
                sell_remaining -= matched
                sell_matched += matched
                if buy_lot.remaining == 0:
                    buy_lots.popleft()

        remaining_lots = tuple(
            BuyLot(lot.remaining, lot.unit_price, lot.occurred_at)
            for lot in buy_lots
            if lot.remaining > 0
        )
        # A flip needs at least five units bought and subsequently sold. FIFO
        # matching supplies both sides of that condition and necessarily
        # rejects sales which happened before the first available purchase.
        if item.matched_quantity < minimum_flip_quantity:
            excluded_items += 1
            continue
        if remaining_lots:
            unmatched[item_id] = remaining_lots
        item_totals[item_id] = ItemProfit(
            item.matched_quantity,
            item.cost,
            item.net_revenue,
            item.profit,
            _weighted_median_seconds(holding_durations),
        )
        total_matched_quantity += item.matched_quantity
        total_cost += item.cost
        total_net_revenue += item.net_revenue
        total_profit += item.profit
        for sold_day, item_day in item_days.items():
            day = day_totals[sold_day]
            day.matched_quantity += item_day.matched_quantity
            day.cost += item_day.cost
            day.net_revenue += item_day.net_revenue
            day.profit += item_day.profit

    result = RealizedProfit(
        items=item_totals,
        days={
            sold_day: DayProfit(
                totals.matched_quantity,
                totals.cost,
                totals.net_revenue,
                totals.profit,
            )
            for sold_day, totals in day_totals.items()
        },
        unmatched_buys=unmatched,
        total_cost=total_cost,
        total_net_revenue=total_net_revenue,
        total_profit=total_profit,
        total_matched_quantity=total_matched_quantity,
    )
    LOGGER.debug(
        "Calculated realized Trading Post profit; buys=%s sells=%s "
        "items=%s days=%s matched=%s excluded_items=%s",
        len(buys),
        len(sells),
        len(result.items),
        len(result.days),
        result.total_matched_quantity,
        excluded_items,
    )
    return result


def calculate_unrealized_profit(
    unmatched_buys: dict[int, tuple[BuyLot, ...]],
    current_sells: list[Transaction],
) -> UnrealizedProfit:
    """Project profit for unmatched purchases that are currently listed."""
    sells_by_item: dict[int, list[Transaction]] = defaultdict(list)
    for transaction in current_sells:
        sells_by_item[transaction.item_id].append(transaction)

    item_totals: dict[int, UnrealizedItemProfit] = {}
    total_quantity = 0
    total_cost = 0
    total_projected_net_revenue = 0
    total_projected_profit = 0
    chronology_blocked_listings = 0

    for item_id, buy_lots in unmatched_buys.items():
        sell_listings = sells_by_item.get(item_id, [])
        if not sell_listings:
            continue
        buy_queue = deque(
            _MutableLot(lot.remaining, lot.unit_price, lot.occurred_at)
            for lot in sorted(buy_lots, key=lambda lot: lot.occurred_at)
        )
        quantity = 0
        cost = 0
        projected_net_revenue = 0
        projected_profit = 0

        for transaction in sorted(
            sell_listings,
            key=lambda listing: listing.occurred_at,
        ):
            sell_listing = _MutableListing(
                transaction.quantity,
                transaction.price,
                transaction.occurred_at,
                transaction.quantity,
            )
            while (
                buy_queue
                and sell_listing.remaining > 0
                and buy_queue[0].occurred_at <= sell_listing.occurred_at
            ):
                buy_lot = buy_queue[0]
                matched = min(buy_lot.remaining, sell_listing.remaining)
                matched_cost = buy_lot.unit_price * matched
                matched_revenue = allocated_net_revenue(
                    sell_listing.unit_price,
                    sell_listing.original_quantity,
                    matched,
                    previously_matched_quantity=(
                        sell_listing.original_quantity
                        - sell_listing.remaining
                    ),
                )
                quantity += matched
                cost += matched_cost
                projected_net_revenue += matched_revenue
                projected_profit += matched_revenue - matched_cost
                buy_lot.remaining -= matched
                sell_listing.remaining -= matched
                if buy_lot.remaining == 0:
                    buy_queue.popleft()
            if (
                buy_queue
                and sell_listing.remaining > 0
                and buy_queue[0].occurred_at > sell_listing.occurred_at
            ):
                chronology_blocked_listings += 1

        if quantity == 0:
            continue
        item_totals[item_id] = UnrealizedItemProfit(
            quantity,
            cost,
            projected_net_revenue,
            projected_profit,
        )
        total_quantity += quantity
        total_cost += cost
        total_projected_net_revenue += projected_net_revenue
        total_projected_profit += projected_profit

    result = UnrealizedProfit(
        items=item_totals,
        total_quantity=total_quantity,
        total_cost=total_cost,
        total_projected_net_revenue=total_projected_net_revenue,
        total_projected_profit=total_projected_profit,
    )
    LOGGER.debug(
        "Calculated unrealized Trading Post profit; listings=%s items=%s "
        "matched=%s chronology_blocked=%s",
        len(current_sells),
        len(result.items),
        result.total_quantity,
        chronology_blocked_listings,
    )
    return result


def group_open_buy_orders(
    orders: list[Transaction],
) -> tuple[OpenBuyOrder, ...]:
    """Collapse outstanding buy orders into one row per item and price."""
    grouped: dict[tuple[int, int], _MutableOrder] = {}
    for order in orders:
        key = (order.item_id, order.price)
        existing = grouped.get(key)
        if existing is None:
            grouped[key] = _MutableOrder(
                order.quantity,
                1,
                order.occurred_at,
            )
            continue
        existing.quantity += order.quantity
        existing.order_count += 1
        existing.placed_at = min(existing.placed_at, order.occurred_at)
    collapsed = tuple(
        OpenBuyOrder(
            item_id=item_id,
            unit_price=unit_price,
            quantity=totals.quantity,
            order_count=totals.order_count,
            placed_at=totals.placed_at,
        )
        for (item_id, unit_price), totals in sorted(grouped.items())
    )
    LOGGER.debug(
        "Grouped open Trading Post buy orders; orders=%s rows=%s items=%s "
        "units=%s",
        len(orders),
        len(collapsed),
        len({row.item_id for row in collapsed}),
        sum(row.quantity for row in collapsed),
    )
    return collapsed
