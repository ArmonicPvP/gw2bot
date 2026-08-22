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
    ProfitReport,
    Transaction,
    calculate_realized_profit,
    calculate_unrealized_profit,
)
from gw2bot.profit.store import ProfitStore

LOGGER = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 300
MIN_REPORT_DAYS = 1
MAX_REPORT_DAYS = 90

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
        api_key = await self._refresh_transactions(discord_user_id, loaded_at)
        try:
            unclaimed_coins = await self._api.fetch_delivery_coins(api_key)
        except ProfitApiAuthorizationError:
            # Keys accepted before delivery reporting existed may be subtokens
            # restricted to the original three transaction routes. Preserve
            # that member's established report and degrade only the new field.
            unclaimed_coins = None
            LOGGER.warning(
                "Could not load Trading Post delivery; user_id=%s "
                "reason=unauthorized",
                discord_user_id,
            )

        cutoff = window_start
        buys, sells, current_sells = await asyncio.to_thread(
            self._read_transactions,
            discord_user_id,
            cutoff,
        )
        realized = await asyncio.to_thread(
            calculate_realized_profit,
            buys,
            sells,
        )
        unrealized = await asyncio.to_thread(
            calculate_unrealized_profit,
            realized.unmatched_buys,
            current_sells,
        )
        item_ids = set(realized.items) | set(unrealized.items)
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
            item_names=item_names,
        )
        LOGGER.debug(
            "Loaded profit report; user_id=%s days=%s realized_items=%s "
            "unrealized_items=%s unclaimed_gold=%s",
            discord_user_id,
            days,
            len(realized.items),
            len(unrealized.items),
            (
                "unavailable"
                if unclaimed_coins is None
                else "available" if unclaimed_coins > 0 else "empty"
            ),
        )
        return report

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

    async def _refresh_transactions(
        self,
        discord_user_id: int,
        now: datetime,
    ) -> str:
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
                return snapshot.api_key
            fetched = await asyncio.gather(
                *(
                    self._api.fetch_transactions(
                        TRANSACTION_PATHS[kind],
                        snapshot.api_key,
                    )
                    for kind in stale_kinds
                )
            )
            accepted = await asyncio.to_thread(
                self._store.store_transaction_snapshot,
                discord_user_id,
                snapshot.generation,
                list(zip(stale_kinds, fetched, strict=True)),
                now=now,
            )
            if accepted:
                return snapshot.api_key
            LOGGER.info(
                "Discarded stale profit transaction snapshot; user_id=%s "
                "attempt=%s retry=%s",
                discord_user_id,
                attempt + 1,
                attempt == 0,
            )
        raise ProfitApiError("GW2 API key changed during profit refresh")

    def _read_transactions(
        self,
        discord_user_id: int,
        cutoff: datetime,
    ) -> tuple[list[Transaction], list[Transaction], list[Transaction]]:
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
        "delivery": {"coins": report.unclaimed_coins},
    }
