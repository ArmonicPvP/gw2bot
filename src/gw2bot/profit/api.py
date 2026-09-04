from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Mapping
from datetime import datetime, timedelta

import aiohttp

from gw2bot.profit.models import (
    DeliveryItem,
    MarketPrice,
    Transaction,
    parse_gw2_time,
)
from gw2bot.profit.prices import MarketPriceCache

LOGGER = logging.getLogger(__name__)

PAGE_SIZE = 200
ITEM_CHUNK_SIZE = 200

# How many pages of one collection are read at once during a backfill. The
# whole history is sixty pages for an active trader, and reading them one after
# another cost about forty seconds of a page load; reading them together costs
# about three. The ceiling keeps a single member's backfill from opening sixty
# sockets against the GW2 API at once.
PAGE_CONCURRENCY = 8

# How far behind the stored watermark an incremental walk keeps reading before
# it stops. Transactions arrive newest first, so one page reaching behind the
# watermark already means there is nothing newer left; the overlap only guards
# against entries that landed out of order around the boundary.
SYNC_OVERLAP = timedelta(minutes=5)
TRANSACTION_PATHS = {
    "history_buys": "/v2/commerce/transactions/history/buys",
    "history_sells": "/v2/commerce/transactions/history/sells",
    "current_sells": "/v2/commerce/transactions/current/sells",
    "current_buys": "/v2/commerce/transactions/current/buys",
}
DELIVERY_PATH = "/v2/commerce/delivery"
REQUIRED_PROFIT_PATHS = frozenset((*TRANSACTION_PATHS.values(), DELIVERY_PATH))


class ProfitApiError(RuntimeError):
    """A sanitized GW2 response failure safe to show without its payload."""


class ProfitApiAuthorizationError(ProfitApiError):
    """The supplied key cannot access one requested GW2 API route."""


class ProfitApiClient:
    """Trading Post calls made with one member's API key."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        base_url: str,
        prices: MarketPriceCache | None = None,
    ) -> None:
        self._session = session
        self._base_url = base_url.rstrip("/")
        self._prices = MarketPriceCache() if prices is None else prices

    async def validate_key(self, api_key: str) -> bool:
        payload, _ = await self._get("/v2/tokeninfo", api_key=api_key)
        if not isinstance(payload, dict):
            raise ProfitApiError("GW2 token information was not an object")
        permissions = payload.get("permissions")
        if not isinstance(permissions, list):
            raise ProfitApiError("GW2 token information had no permissions")
        has_tradingpost = "tradingpost" in {
            permission
            for permission in permissions
            if isinstance(permission, str)
        }
        raw_urls = payload.get("urls")
        route_access = True
        route_restricted = raw_urls is not None
        if route_restricted:
            if not isinstance(raw_urls, list) or not all(
                isinstance(path, str) for path in raw_urls
            ):
                raise ProfitApiError(
                    "GW2 token information had invalid URL restrictions"
                )
            route_access = REQUIRED_PROFIT_PATHS.issubset(raw_urls)
        valid = has_tradingpost and route_access
        LOGGER.debug(
            "Validated profit API key; tradingpost=%s "
            "route_restricted=%s required_routes=%s",
            has_tradingpost,
            route_restricted,
            route_access,
        )
        return valid

    async def _fetch_transaction_page(
        self,
        path: str,
        api_key: str,
        page: int,
    ) -> tuple[list[Transaction], int | None]:
        payload, headers = await self._get(
            path,
            api_key=api_key,
            params={"page": str(page), "page_size": str(PAGE_SIZE)},
        )
        if not isinstance(payload, list):
            raise ProfitApiError(
                "GW2 transaction response was not a collection"
            )
        transactions = [_transaction_from_payload(item) for item in payload]
        raw_page_total = headers.get("X-Page-Total")
        page_total: int | None = None
        if raw_page_total is not None:
            try:
                page_total = int(raw_page_total)
            except ValueError as exc:
                raise ProfitApiError(
                    "GW2 pagination metadata was invalid"
                ) from exc
            if page_total < 0:
                raise ProfitApiError("GW2 pagination metadata was invalid")
        return transactions, page_total

    async def fetch_transactions(
        self,
        path: str,
        api_key: str,
        *,
        since: datetime | None = None,
    ) -> list[Transaction]:
        """Read one collection, stopping early once it reaches ``since``.

        Without a watermark every page is read, and the pages behind the first
        are read together rather than one after another. With one, the walk is
        sequential and normally ends on the first page, because a member's
        newest page is the only one that can hold anything new.
        """
        first, page_total = await self._fetch_transaction_page(path, api_key, 0)
        transactions = list(first)
        pages_read = 1
        if page_total is not None and page_total <= 1:
            LOGGER.debug(
                "Fetched GW2 profit transactions; path=%s mode=single-page "
                "pages=%s records=%s",
                path,
                pages_read,
                len(transactions),
            )
            return transactions
        if since is not None:
            cutoff = since - SYNC_OVERLAP
            if _reaches_behind(first, cutoff):
                LOGGER.debug(
                    "Fetched GW2 profit transactions; path=%s "
                    "mode=incremental pages=%s records=%s",
                    path,
                    pages_read,
                    len(transactions),
                )
                return transactions
            page = 1
            while page_total is None or page < page_total:
                rows, _ = await self._fetch_transaction_page(
                    path, api_key, page
                )
                transactions.extend(rows)
                pages_read += 1
                page += 1
                if not rows or len(rows) < PAGE_SIZE:
                    break
                if _reaches_behind(rows, cutoff):
                    break
            LOGGER.debug(
                "Fetched GW2 profit transactions; path=%s mode=incremental "
                "pages=%s records=%s",
                path,
                pages_read,
                len(transactions),
            )
            return transactions

        if page_total is None:
            # No pagination metadata to plan around, so fall back to walking
            # until a short page ends the collection.
            page = 1
            while len(first) == PAGE_SIZE:
                first, _ = await self._fetch_transaction_page(
                    path, api_key, page
                )
                transactions.extend(first)
                pages_read += 1
                page += 1
        else:
            gate = asyncio.Semaphore(PAGE_CONCURRENCY)

            async def page_rows(number: int) -> list[Transaction]:
                async with gate:
                    rows, _ = await self._fetch_transaction_page(
                        path, api_key, number
                    )
                    return rows

            remaining = await asyncio.gather(
                *(page_rows(number) for number in range(1, page_total))
            )
            for rows in remaining:
                transactions.extend(rows)
            pages_read = page_total
        LOGGER.debug(
            "Fetched GW2 profit transactions; path=%s mode=backfill "
            "pages=%s records=%s",
            path,
            pages_read,
            len(transactions),
        )
        return transactions

    async def fetch_delivery(
        self,
        api_key: str,
    ) -> tuple[int, tuple[DeliveryItem, ...]]:
        """Return the copper and per-item stacks waiting in TP delivery."""
        payload, _ = await self._get(DELIVERY_PATH, api_key=api_key)
        if not isinstance(payload, dict):
            raise ProfitApiError("GW2 delivery response was not an object")
        coins = payload.get("coins")
        if not isinstance(coins, int) or isinstance(coins, bool) or coins < 0:
            raise ProfitApiError("GW2 delivery response had invalid coins")
        items = payload.get("items")
        if not isinstance(items, list):
            raise ProfitApiError("GW2 delivery response had invalid items")
        # One item can be delivered as several stacks - a bought order that
        # filled in pieces, or the leftovers of separate purchases - so the
        # counts are added together before the dashboard draws a row per item.
        quantities: dict[int, int] = {}
        for item in items:
            if not isinstance(item, dict):
                raise ProfitApiError("GW2 delivery response had invalid items")
            item_id = item.get("id")
            count = item.get("count")
            if (
                not isinstance(item_id, int)
                or isinstance(item_id, bool)
                or item_id <= 0
                or not isinstance(count, int)
                or isinstance(count, bool)
                or count < 0
            ):
                raise ProfitApiError("GW2 delivery response had invalid items")
            quantities[item_id] = quantities.get(item_id, 0) + count
        delivered = tuple(
            DeliveryItem(item_id, quantity)
            for item_id, quantity in sorted(quantities.items())
        )
        LOGGER.debug(
            "Fetched GW2 Trading Post delivery; coins_available=%s "
            "item_rows=%s item_quantity=%s",
            coins > 0,
            len(delivered),
            sum(row.quantity for row in delivered),
        )
        return coins, delivered

    async def fetch_all_item_ids(self) -> list[int]:
        """Every tradeable item id the game knows about."""
        payload, _ = await self._get("/v2/items")
        if not isinstance(payload, list):
            raise ProfitApiError("GW2 item id response was not a collection")
        item_ids = [
            item_id
            for item_id in payload
            if isinstance(item_id, int) and not isinstance(item_id, bool)
        ]
        LOGGER.debug("Fetched GW2 item id list; items=%s", len(item_ids))
        return item_ids

    async def _fetch_item_name_chunk(
        self,
        chunk: list[int],
        gate: asyncio.Semaphore,
    ) -> dict[int, str]:
        names: dict[int, str] = {}
        try:
            async with gate:
                payload, _ = await self._get(
                    "/v2/items",
                    params={"ids": ",".join(str(item_id) for item_id in chunk)},
                )
            if not isinstance(payload, list):
                raise ProfitApiError("GW2 item response was not a collection")
            for item in payload:
                if not isinstance(item, dict):
                    continue
                item_id = item.get("id")
                name = item.get("name")
                if (
                    isinstance(item_id, int)
                    and not isinstance(item_id, bool)
                    and isinstance(name, str)
                ):
                    names[item_id] = name
            LOGGER.debug(
                "Fetched GW2 profit item names; requested=%s found=%s",
                len(chunk),
                len(names),
            )
        except (aiohttp.ClientError, TimeoutError, ProfitApiError) as exc:
            # Names are presentation only. One failed chunk falls back to
            # item ids and must not hide the otherwise complete report or
            # prevent the other chunks from being attempted.
            LOGGER.warning(
                "Could not fetch a profit item-name chunk; requested=%s "
                "error_type=%s",
                len(chunk),
                type(exc).__name__,
            )
        return names

    async def fetch_item_names(self, item_ids: set[int]) -> dict[int, str]:
        """Name the requested items, reading the chunks together.

        The whole catalogue is nearly four hundred chunks. Read one after
        another that is five minutes of the event loop; read eight at a time
        it is well under one, and the ceiling keeps the burst polite.
        """
        ordered_ids = sorted(item_ids)
        chunks = [
            ordered_ids[offset : offset + ITEM_CHUNK_SIZE]
            for offset in range(0, len(ordered_ids), ITEM_CHUNK_SIZE)
        ]
        gate = asyncio.Semaphore(PAGE_CONCURRENCY)
        results = await asyncio.gather(
            *(self._fetch_item_name_chunk(chunk, gate) for chunk in chunks)
        )
        names: dict[int, str] = {}
        for found in results:
            names.update(found)
        return names

    def forget_stale_prices(self) -> int:
        """Drop expired price readings the reads have not happened to touch."""
        return self._prices.prune()

    async def fetch_market_prices(
        self,
        item_ids: set[int],
        *,
        force: bool = False,
    ) -> dict[int, MarketPrice]:
        """Return usable current buy-order and sell-listing prices.

        Readings are shared with every other member through the cache, so an
        item several people are watching costs one lookup. ``force`` skips
        the cache for a member who asked for a refresh by hand.
        """
        cached: dict[int, MarketPrice] = {}
        wanted = set(item_ids)
        if force:
            self._prices.forget(wanted)
        else:
            cached, wanted = self._prices.read(item_ids)
        prices: dict[int, MarketPrice] = {}
        ordered_ids = sorted(wanted)
        # Chunks are independent lookups, so they go out together.
        chunks = [
            ordered_ids[offset : offset + ITEM_CHUNK_SIZE]
            for offset in range(0, len(ordered_ids), ITEM_CHUNK_SIZE)
        ]
        payloads = await asyncio.gather(
            *(
                self._get(
                    "/v2/commerce/prices",
                    params={
                        "ids": ",".join(str(item_id) for item_id in chunk)
                    },
                )
                for chunk in chunks
            )
        )
        for payload, _headers in payloads:
            if not isinstance(payload, list):
                raise ProfitApiError("GW2 price response was not a collection")
            for item in payload:
                price = _market_price_from_payload(item)
                if price is not None:
                    prices[price[0]] = price[1]
        if prices:
            self._prices.write(prices)
        LOGGER.debug(
            "Fetched GW2 market prices; requested=%s cached=%s fetched=%s "
            "usable=%s forced=%s",
            len(item_ids),
            len(cached),
            len(ordered_ids),
            len(prices),
            force,
        )
        return {**cached, **prices}

    async def _get(
        self,
        path: str,
        *,
        api_key: str | None = None,
        params: Mapping[str, str] | None = None,
    ) -> tuple[object, Mapping[str, str]]:
        headers = (
            {"Authorization": f"Bearer {api_key}"}
            if api_key is not None
            else None
        )
        LOGGER.debug("Sending profit GW2 API GET request; path=%s", path)
        async with self._session.get(
            f"{self._base_url}{path}",
            headers=headers,
            params=params,
        ) as response:
            LOGGER.debug(
                "Profit GW2 API GET completed; path=%s status=%s",
                path,
                response.status,
            )
            if response.status in {401, 403}:
                raise ProfitApiAuthorizationError(
                    f"GW2 API request returned HTTP {response.status}"
                )
            if response.status >= 400:
                raise ProfitApiError(
                    f"GW2 API request returned HTTP {response.status}"
                )
            try:
                payload = await response.json()
            except (aiohttp.ContentTypeError, json.JSONDecodeError) as exc:
                raise ProfitApiError("GW2 API response was not JSON") from exc
            LOGGER.debug(
                "Decoded profit GW2 API response; path=%s result_type=%s "
                "result_count=%s",
                path,
                type(payload).__name__,
                len(payload) if isinstance(payload, (dict, list)) else "n/a",
            )
            return payload, response.headers


def _reaches_behind(
    transactions: list[Transaction],
    cutoff: datetime,
) -> bool:
    """Whether a page has run past the point the store already knows."""
    return bool(transactions) and min(
        transaction.occurred_at for transaction in transactions
    ) <= cutoff


def _transaction_from_payload(payload: object) -> Transaction:
    if not isinstance(payload, dict):
        raise ProfitApiError("GW2 transaction entry was not an object")
    item_id = _positive_int(payload, "item_id")
    price = _nonnegative_int(payload, "price")
    quantity = _positive_int(payload, "quantity")
    occurred = payload.get("purchased") or payload.get("created")
    if not isinstance(occurred, str):
        raise ProfitApiError("GW2 transaction entry had no timestamp")
    try:
        occurred_at = parse_gw2_time(occurred)
    except (TypeError, ValueError) as exc:
        raise ProfitApiError(
            "GW2 transaction entry had an invalid timestamp"
        ) from exc
    raw_id = payload.get("id")
    if isinstance(raw_id, (str, int)) and not isinstance(raw_id, bool):
        transaction_id = str(raw_id)
    else:
        # GW2 currently supplies ids. The stable digest preserves the source
        # bot's defensive fallback without persisting the full raw response.
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        transaction_id = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return Transaction(
        transaction_id=transaction_id,
        item_id=item_id,
        price=price,
        quantity=quantity,
        occurred_at=occurred_at,
    )


def _market_price_from_payload(
    payload: object,
) -> tuple[int, MarketPrice] | None:
    if not isinstance(payload, dict):
        return None
    item_id = payload.get("id")
    buys = payload.get("buys")
    sells = payload.get("sells")
    if (
        not isinstance(item_id, int)
        or isinstance(item_id, bool)
        or not isinstance(buys, dict)
        or not isinstance(sells, dict)
    ):
        return None
    buy_price = buys.get("unit_price")
    sell_price = sells.get("unit_price")
    if (
        not isinstance(buy_price, int)
        or isinstance(buy_price, bool)
        or buy_price <= 0
        or not isinstance(sell_price, int)
        or isinstance(sell_price, bool)
        or sell_price <= 0
    ):
        return None
    return item_id, MarketPrice(buy_price, sell_price)


def _positive_int(payload: dict[object, object], key: str) -> int:
    value = _nonnegative_int(payload, key)
    if value == 0:
        raise ProfitApiError(f"GW2 transaction entry had invalid {key}")
    return value


def _nonnegative_int(payload: dict[object, object], key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ProfitApiError(f"GW2 transaction entry had invalid {key}")
    return value
