from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import aiohttp
from sqlalchemy.exc import SQLAlchemyError

from gw2bot.profit.api import (
    TRANSACTION_PATHS,
    ProfitApiClient,
    ProfitApiAuthorizationError,
    ProfitApiError,
)
from gw2bot.profit.models import (
    MIN_FLIP_QUANTITY,
    DeliveryItem,
    DeliveryReport,
    OpenBuyOrder,
    OpenOrdersReport,
    ProfitReport,
    Transaction,
    RealizedProfit,
    aggregate_rollups,
    calculate_realized_profit,
    calculate_unrealized_profit,
    group_open_buy_orders,
    sale_fee_total,
)
from gw2bot.profit.store import (
    HISTORY_KINDS,
    ITEM_NAME_TTL_SECONDS,
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

# The collections the realized report is built from. Open orders and delivery
# are loaded on their own so neither waits on this one.
HISTORY_REPORT_KINDS = ("history_buys", "history_sells", "current_sells")


@dataclass(frozen=True, slots=True)
class ReportWindow:
    """The window a request resolved to, and whether it was a stored choice."""

    days: int
    remembered: bool


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
    ) -> ReportWindow:
        """Return the window to report, remembering an explicit choice.

        A member who picks a window keeps it: the page opens on it again on
        their next visit, from any browser, because the choice is stored
        against their Discord ID rather than left in the URL they came from.
        """
        if requested_days is not None:
            if not MIN_REPORT_DAYS <= requested_days <= MAX_REPORT_DAYS:
                raise ValueError(
                    "Profit report days must be between "
                    f"{MIN_REPORT_DAYS} and {MAX_REPORT_DAYS}"
                )
            await asyncio.to_thread(
                self._store.set_report_days,
                discord_user_id,
                requested_days,
            )
            return ReportWindow(requested_days, True)
        stored = await asyncio.to_thread(
            self._store.get_report_days,
            discord_user_id,
        )
        LOGGER.debug(
            "Resolved profit report window; user_id=%s remembered=%s",
            discord_user_id,
            stored is not None,
        )
        # Saying which answer this is lets the page tell a member's stored
        # window from the fallback, and put a lost one back from the copy the
        # browser keeps.
        if stored is None:
            return ReportWindow(DEFAULT_REPORT_DAYS, False)
        return ReportWindow(stored, True)

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

    async def load_delivery(
        self,
        discord_user_id: int,
        *,
        now: datetime | None = None,
    ) -> DeliveryReport:
        """Load what is waiting for pickup, and nothing else.

        Delivery depends on no transaction collection, so this answers in one
        request while the history sections are still being read.
        """
        loaded_at = datetime.now(UTC) if now is None else now
        snapshot = await self._require_api_key(discord_user_id)
        coins, items = await self._fetch_delivery(
            discord_user_id,
            snapshot.api_key,
        )
        item_names = await self._resolve_item_names(
            {row.item_id for row in items or ()},
            loaded_at,
        )
        LOGGER.debug(
            "Loaded Trading Post delivery; user_id=%s coins=%s items=%s",
            discord_user_id,
            (
                "unavailable"
                if coins is None
                else "available" if coins > 0 else "empty"
            ),
            "unavailable" if items is None else len(items),
        )
        return DeliveryReport(coins=coins, items=items, item_names=item_names)

    async def load_open_orders(
        self,
        discord_user_id: int,
        *,
        force: bool = False,
        now: datetime | None = None,
    ) -> OpenOrdersReport:
        """Load the member's outstanding buy orders and their current spread.

        This needs one short collection rather than the whole trade history,
        so it lands long before the realized report does.
        """
        loaded_at = datetime.now(UTC) if now is None else now
        _key, unavailable = await self._refresh_transactions(
            discord_user_id,
            loaded_at,
            ("current_buys",),
            force=force,
        )
        current_buys, excluded_items = await asyncio.to_thread(
            self._read_order_data,
            discord_user_id,
        )
        orders = await asyncio.to_thread(group_open_buy_orders, current_buys)
        order_item_ids = {order.item_id for order in orders}
        # Prices and names are independent lookups, so they go out together.
        market_prices, item_names = await asyncio.gather(
            self._api.fetch_market_prices(order_item_ids, force=force),
            self._resolve_item_names(
                # An item hidden with no open order left still needs its name,
                # or the member cannot tell what they are restoring.
                order_item_ids | set(excluded_items),
                loaded_at,
            ),
        )
        available = "current_buys" not in unavailable
        LOGGER.debug(
            "Loaded open Trading Post orders; user_id=%s rows=%s prices=%s "
            "hidden=%s availability=%s",
            discord_user_id,
            len(orders),
            len(market_prices),
            len(excluded_items),
            "available" if available else "unavailable",
        )
        return OpenOrdersReport(
            orders=orders,
            available=available,
            excluded_items=excluded_items,
            market_prices=market_prices,
            item_names=item_names,
        )

    async def load_report(
        self,
        discord_user_id: int,
        days: int,
        *,
        force: bool = False,
        now: datetime | None = None,
    ) -> ProfitReport:
        if not MIN_REPORT_DAYS <= days <= MAX_REPORT_DAYS:
            raise ValueError(
                "Profit report days must be between "
                f"{MIN_REPORT_DAYS} and {MAX_REPORT_DAYS}"
            )
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
        await self._refresh_transactions(
            discord_user_id,
            loaded_at,
            HISTORY_REPORT_KINDS,
            force=force,
        )

        await self._ensure_rollups(discord_user_id, loaded_at)
        (
            realized,
            current_sells,
            history_start,
            buy_count,
            sell_count,
        ) = await asyncio.to_thread(
            self._read_windowed_report,
            discord_user_id,
            window_start,
        )
        unrealized = await asyncio.to_thread(
            calculate_unrealized_profit,
            realized.unmatched_buys,
            current_sells,
        )
        market_prices, item_names = await asyncio.gather(
            self._api.fetch_market_prices(set(realized.items), force=force),
            self._resolve_item_names(
                set(realized.items) | set(unrealized.items),
                loaded_at,
            ),
        )

        report = ProfitReport(
            days=days,
            window_start=window_start,
            window_end=loaded_at,
            buy_transaction_count=buy_count,
            sell_transaction_count=sell_count,
            realized=realized,
            unrealized=unrealized,
            item_names=item_names,
            market_prices=market_prices,
            history_start=history_start,
        )
        LOGGER.debug(
            "Loaded profit report; user_id=%s days=%s realized_items=%s "
            "unrealized_items=%s market_prices=%s history_start=%s",
            discord_user_id,
            days,
            len(realized.items),
            len(unrealized.items),
            len(market_prices),
            history_start is not None,
        )
        return report

    async def _resolve_item_names(
        self,
        item_ids: set[int],
        now: datetime,
    ) -> dict[int, str]:
        """Name every item, asking GW2 only about ones not already stored."""
        if not item_ids:
            return {}
        item_names = await asyncio.to_thread(
            self._store.get_item_names,
            item_ids,
            ITEM_NAME_TTL_SECONDS,
            now=now,
        )
        missing_ids = item_ids - set(item_names)
        if missing_ids:
            fetched_names = await self._api.fetch_item_names(missing_ids)
            if fetched_names:
                await asyncio.to_thread(
                    self._store.store_item_names,
                    fetched_names,
                    now=now,
                )
                item_names.update(fetched_names)
        for item_id in item_ids:
            item_names.setdefault(item_id, f"Item {item_id}")
        LOGGER.debug(
            "Resolved profit item names; requested=%s fetched=%s",
            len(item_ids),
            len(missing_ids),
        )
        return item_names

    async def _fetch_delivery(
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

    def _read_order_data(
        self,
        discord_user_id: int,
    ) -> tuple[list[Transaction], frozenset[int]]:
        return (
            self._store.get_transactions(discord_user_id, "current_buys"),
            self._store.get_excluded_order_items(discord_user_id),
        )

    async def _ensure_rollups(
        self,
        discord_user_id: int,
        now: datetime,
    ) -> bool:
        """Rebuild this member's rollups when new trades have landed."""
        computed_through, newest = await asyncio.to_thread(
            self._rollup_freshness,
            discord_user_id,
        )
        if newest is None:
            return False
        if computed_through is not None and computed_through >= newest:
            LOGGER.debug(
                "Profit rollups are current; user_id=%s",
                discord_user_id,
            )
            return False
        await asyncio.to_thread(
            self._advance_rollups,
            discord_user_id,
            computed_through,
            newest,
            now,
        )
        return True

    def _rollup_freshness(
        self,
        discord_user_id: int,
    ) -> tuple[datetime | None, datetime | None]:
        return (
            self._store.get_rollup_state(discord_user_id),
            self._store.get_newest_transaction_at(discord_user_id),
        )

    def _advance_rollups(
        self,
        discord_user_id: int,
        computed_through: datetime | None,
        newest: datetime,
        now: datetime,
    ) -> None:
        """Bring the rollups up to the newest trade, reading as little as can be.

        With a watermark and the lots carried from the last pass, only trades
        newer than the watermark need matching, so a member who sold something
        five minutes ago costs a handful of rows rather than their whole
        history. Without one - a first pass, or trades that arrived out of
        order behind the watermark - everything is matched again.
        """
        if computed_through is None:
            self._rebuild_rollups(discord_user_id, newest, now)
            return
        buys = self._store.get_transactions(
            discord_user_id, "history_buys", after=computed_through
        )
        sells = self._store.get_transactions(
            discord_user_id, "history_sells", after=computed_through
        )
        opening_lots = self._store.get_open_lots(discord_user_id)
        realized = calculate_realized_profit(
            buys,
            sells,
            with_item_days=True,
            opening_lots=opening_lots,
        )
        self._store.merge_rollups(
            discord_user_id,
            realized.item_days,
            realized.unmatched_buys,
            newest,
            now=now,
        )
        LOGGER.debug(
            "Advanced profit rollups; user_id=%s transactions=%s rows=%s "
            "carried_items=%s",
            discord_user_id,
            len(buys) + len(sells),
            len(realized.item_days),
            len(opening_lots),
        )

    def _rebuild_rollups(
        self,
        discord_user_id: int,
        newest: datetime,
        now: datetime,
    ) -> None:
        """Match a member's whole history once and store the result.

        Matching runs over everything held rather than over one window, so a
        sale of stock bought before the window is costed from the purchase
        that actually paid for it instead of being dropped for having no
        match inside the window.
        """
        buys = self._store.get_transactions(discord_user_id, "history_buys")
        sells = self._store.get_transactions(discord_user_id, "history_sells")
        # No flip threshold here: it belongs to the window a member asked
        # for, not to their whole history, and is applied when the rollups
        # are read back.
        realized = calculate_realized_profit(buys, sells, with_item_days=True)
        self._store.store_rollups(
            discord_user_id,
            realized.item_days,
            realized.unmatched_buys,
            newest,
            now=now,
        )
        LOGGER.info(
            "Rebuilt profit rollups; user_id=%s transactions=%s rows=%s",
            discord_user_id,
            len(buys) + len(sells),
            len(realized.item_days),
        )

    def _read_windowed_report(
        self,
        discord_user_id: int,
        cutoff: datetime,
    ) -> tuple[
        RealizedProfit,
        list[Transaction],
        datetime | None,
        int,
        int,
    ]:
        rollups = self._store.get_rollups(discord_user_id, cutoff)
        realized = aggregate_rollups(
            rollups,
            self._store.get_open_lots(discord_user_id),
            minimum_flip_quantity=MIN_FLIP_QUANTITY,
        )
        return (
            realized,
            self._store.get_transactions(discord_user_id, "current_sells"),
            self._store.get_earliest_transaction_at(discord_user_id),
            self._store.count_transactions(
                discord_user_id, "history_buys", cutoff
            ),
            self._store.count_transactions(
                discord_user_id, "history_sells", cutoff
            ),
        )


    async def warm_item_names(self) -> int:
        """Store the name of every item the game has, ahead of any report.

        Names are the one lookup a report cannot avoid and cannot guess, and
        they never change within a game build. Reading the whole catalogue in
        the background once a day means no member ever waits on /v2/items;
        only ids that are missing or a month stale are asked for, so the run
        after the first costs almost nothing.
        """
        try:
            item_ids = await self._api.fetch_all_item_ids()
        except (aiohttp.ClientError, TimeoutError, ProfitApiError) as exc:
            # Names are presentation only, and a report falls back to item
            # ids. A failed warm must never take the bot down with it.
            LOGGER.warning(
                "Could not list GW2 items to warm names; error_type=%s",
                type(exc).__name__,
            )
            return 0
        known = await asyncio.to_thread(
            self._store.get_known_item_ids,
            ITEM_NAME_TTL_SECONDS,
        )
        missing = set(item_ids) - known
        if not missing:
            LOGGER.debug(
                "Item name cache already warm; items=%s", len(item_ids)
            )
            return 0
        LOGGER.info(
            "Warming the profit item name cache; catalogue=%s missing=%s",
            len(item_ids),
            len(missing),
        )
        names = await self._api.fetch_item_names(missing)
        if names:
            await asyncio.to_thread(self._store.store_item_names, names)
        LOGGER.info(
            "Warmed the profit item name cache; stored=%s missing=%s",
            len(names),
            len(missing),
        )
        return len(names)

    async def sync_member(self, discord_user_id: int) -> bool:
        """Bring one member's stored collections up to date in the background.

        A page load then reads the database rather than the GW2 API. Failures
        are logged and swallowed: one member's expired key must not stop the
        pass over everyone else.
        """
        try:
            await self._refresh_transactions(
                discord_user_id,
                datetime.now(UTC),
                tuple(TRANSACTION_PATHS),
                force=True,
            )
        except MissingProfitApiKey:
            LOGGER.debug(
                "Skipped profit sync; user_id=%s reason=api-key-unset",
                discord_user_id,
            )
            return False
        except (
            aiohttp.ClientError,
            TimeoutError,
            ProfitApiError,
            SQLAlchemyError,
        ) as exc:
            LOGGER.warning(
                "Could not sync a member's Trading Post data; user_id=%s "
                "error_type=%s",
                discord_user_id,
                type(exc).__name__,
            )
            return False
        try:
            # Matching the whole history is the expensive half of a report.
            # Doing it here means a page load finds it already done.
            await self._ensure_rollups(discord_user_id, datetime.now(UTC))
        except SQLAlchemyError as exc:
            LOGGER.warning(
                "Could not rebuild a member's profit rollups; user_id=%s "
                "error_type=%s",
                discord_user_id,
                type(exc).__name__,
            )
            return False
        LOGGER.debug("Synced Trading Post data; user_id=%s", discord_user_id)
        return True

    async def sync_all_members(self) -> tuple[int, int]:
        """Sync every member holding a key; return (synced, attempted)."""
        try:
            members = await asyncio.to_thread(
                self._store.get_members_with_api_key
            )
        except SQLAlchemyError as exc:
            LOGGER.error(
                "Could not list profit key holders; error_type=%s",
                type(exc).__name__,
            )
            return 0, 0
        synced = 0
        for discord_user_id in members:
            if await self.sync_member(discord_user_id):
                synced += 1
        LOGGER.info(
            "Completed the daily Trading Post sync; synced=%s members=%s",
            synced,
            len(members),
        )
        return synced, len(members)

    async def _require_api_key(self, discord_user_id: int):
        snapshot = await asyncio.to_thread(
            self._store.get_api_key_snapshot,
            discord_user_id,
        )
        if snapshot is None:
            LOGGER.debug(
                "Skipped profit request; user_id=%s reason=api-key-unset",
                discord_user_id,
            )
            raise MissingProfitApiKey
        return snapshot

    def _stale_kinds(
        self,
        discord_user_id: int,
        kinds: tuple[str, ...],
        now: datetime,
    ) -> list[str]:
        return [
            kind
            for kind in kinds
            if not self._store.is_cache_fresh(
                discord_user_id,
                kind,
                CACHE_TTL_SECONDS,
                now=now,
            )
        ]

    def _sync_plan(
        self,
        discord_user_id: int,
        kinds: list[str],
    ) -> dict[str, datetime | None]:
        """Say where each collection should resume reading from.

        A history collection that has been walked end to end resumes at its
        watermark, which normally makes the refresh one page instead of sixty.
        Anything else - a first sync, or a store written before the watermark
        existed - is read in full, once, and marked so it need not be again.
        The current collections are replacing snapshots of a single page, so
        they are always read whole.
        """
        plan: dict[str, datetime | None] = {}
        for kind in kinds:
            if kind not in HISTORY_KINDS:
                plan[kind] = None
                continue
            state = self._store.get_sync_state(discord_user_id, kind)
            plan[kind] = (
                state.synced_through if state.backfilled else None
            )
        return plan

    async def _fetch_collection(
        self,
        transaction_kind: str,
        api_key: str,
        discord_user_id: int,
        since: datetime | None,
    ) -> list[Transaction] | None:
        """Fetch one collection; None when only Open Orders is out of reach."""
        try:
            return await self._api.fetch_transactions(
                TRANSACTION_PATHS[transaction_kind],
                api_key,
                since=since,
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
        kinds: tuple[str, ...],
        *,
        force: bool = False,
    ) -> tuple[str, frozenset[str]]:
        """Bring the named collections up to date; report what was refused.

        ``force`` ignores the snapshot's five-minute life, which is what the
        page's Load button asks for: a member who presses it wants the current
        state, not the one from four minutes ago. The read is still
        incremental, so forcing costs one page rather than sixty.
        """
        for attempt in range(2):
            snapshot = await self._require_api_key(discord_user_id)
            stale_kinds = (
                list(kinds)
                if force
                else await asyncio.to_thread(
                    self._stale_kinds,
                    discord_user_id,
                    kinds,
                    now,
                )
            )
            LOGGER.debug(
                "Resolved profit cache refresh; user_id=%s asked=%s stale=%s "
                "attempt=%s",
                discord_user_id,
                len(kinds),
                len(stale_kinds),
                attempt + 1,
            )
            if not stale_kinds:
                return snapshot.api_key, frozenset()
            plan = await asyncio.to_thread(
                self._sync_plan,
                discord_user_id,
                stale_kinds,
            )
            # Every stale collection is read at the same time; nothing here
            # depends on anything else here.
            fetched = await asyncio.gather(
                *(
                    self._fetch_collection(
                        kind,
                        snapshot.api_key,
                        discord_user_id,
                        plan[kind],
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
            unavailable = frozenset(
                kind
                for kind, transactions in zip(stale_kinds, fetched, strict=True)
                if transactions is None
            )
            # A collection read from page zero to the end is complete now,
            # whatever it was before.
            backfilled = {
                kind: plan[kind] is None for kind, _ in collections
            }
            accepted = await asyncio.to_thread(
                self._store.store_transaction_snapshot,
                discord_user_id,
                snapshot.generation,
                collections,
                backfilled=backfilled,
                now=now,
            )
            if accepted:
                return snapshot.api_key, unavailable
            LOGGER.info(
                "Discarded stale profit transaction snapshot; user_id=%s "
                "attempt=%s retry=%s",
                discord_user_id,
                attempt + 1,
                attempt == 0,
            )
        raise ProfitApiError("GW2 API key changed during profit refresh")


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
            "hold_seconds": totals.hold_seconds,
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
        "history_start_date": (
            None
            if report.history_start is None
            else report.history_start.date().isoformat()
        ),
        "max_days": MAX_REPORT_DAYS,
    }


def serialize_delivery(report: DeliveryReport) -> dict[str, object]:
    return {
        "coins": report.coins,
        "items": (
            None
            if report.items is None
            else [
                {
                    "item_id": row.item_id,
                    "name": report.item_names.get(
                        row.item_id,
                        f"Item {row.item_id}",
                    ),
                    "quantity": row.quantity,
                }
                for row in report.items
            ]
        ),
    }


def serialize_open_orders(report: OpenOrdersReport) -> dict[str, object]:
    orders, excluded = _open_order_rows(report)
    return {
        "available": report.available,
        "orders": orders,
        "excluded": excluded,
    }


def _open_order_rows(
    report: OpenOrdersReport,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Split the member's open buy orders into kept and excluded rows.

    Both lists carry the same shape so the page can move a row between them
    when the member excludes or restores an item without reloading the report.
    """
    orders: list[dict[str, object]] = []
    excluded: list[dict[str, object]] = []
    for order in report.orders:
        row = _open_order_row(report, order)
        if order.item_id in report.excluded_items:
            excluded.append(row)
        else:
            orders.append(row)
    # An item excluded while it had an order keeps its entry after the order
    # fills or is cancelled; without it the member has no way to restore it.
    listed = {order.item_id for order in report.orders}
    for item_id in sorted(report.excluded_items - listed):
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
    report: OpenOrdersReport,
    order: OpenBuyOrder,
) -> dict[str, object]:
    price = report.market_prices.get(order.item_id)
    cost = order.unit_price * order.quantity
    net_revenue: int | None = None
    profit: int | None = None
    total_profit: int | None = None
    roi_percent: float | None = None
    if price is not None:
        # The exit assumed here is the one the member controls: undercutting
        # nothing and selling at the current lowest listing, after both fees.
        # Both fees round up against the gross value of the whole order, the
        # way a realized sale of that quantity is charged; rounding each unit
        # on its own would overcharge every multi-unit order and understate
        # the return. The per-unit figure is then derived from that total.
        net_revenue = price.sell_unit_price * order.quantity - sale_fee_total(
            price.sell_unit_price,
            order.quantity,
        )
        total_profit = net_revenue - cost
        profit = round(total_profit / order.quantity)
        roi_percent = total_profit / cost * 100 if cost else None
    return {
        "item_id": order.item_id,
        "name": report.item_names.get(
            order.item_id,
            f"Item {order.item_id}",
        ),
        "quantity": order.quantity,
        "order_count": order.order_count,
        "unit_price": order.unit_price,
        "cost": cost,
        "buy_price": None if price is None else price.buy_unit_price,
        "sell_price": None if price is None else price.sell_unit_price,
        "net_revenue": net_revenue,
        "profit": profit,
        "total_profit": total_profit,
        "roi_percent": roi_percent,
        "has_order": True,
    }
