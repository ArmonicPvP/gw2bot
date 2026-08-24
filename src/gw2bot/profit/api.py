from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping

import aiohttp

from gw2bot.profit.models import MarketPrice, Transaction, parse_gw2_time

LOGGER = logging.getLogger(__name__)

PAGE_SIZE = 200
ITEM_CHUNK_SIZE = 200
TRANSACTION_PATHS = {
    "history_buys": "/v2/commerce/transactions/history/buys",
    "history_sells": "/v2/commerce/transactions/history/sells",
    "current_sells": "/v2/commerce/transactions/current/sells",
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
    ) -> None:
        self._session = session
        self._base_url = base_url.rstrip("/")

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

    async def fetch_transactions(
        self,
        path: str,
        api_key: str,
    ) -> list[Transaction]:
        transactions: list[Transaction] = []
        page = 0
        page_total: int | None = None
        while True:
            payload, headers = await self._get(
                path,
                api_key=api_key,
                params={"page": str(page), "page_size": str(PAGE_SIZE)},
            )
            if not isinstance(payload, list):
                raise ProfitApiError(
                    "GW2 transaction response was not a collection"
                )
            transactions.extend(
                _transaction_from_payload(item) for item in payload
            )
            if page_total is None:
                raw_page_total = headers.get("X-Page-Total")
                if raw_page_total is not None:
                    try:
                        page_total = int(raw_page_total)
                    except ValueError as exc:
                        raise ProfitApiError(
                            "GW2 pagination metadata was invalid"
                        ) from exc
                    if page_total < 0:
                        raise ProfitApiError(
                            "GW2 pagination metadata was invalid"
                        )
            page += 1
            if page_total is not None:
                if page >= page_total:
                    break
            elif len(payload) < PAGE_SIZE:
                break
        LOGGER.debug(
            "Fetched GW2 profit transactions; path=%s pages=%s records=%s",
            path,
            page,
            len(transactions),
        )
        return transactions

    async def fetch_delivery_coins(self, api_key: str) -> int:
        """Return copper waiting for the member in Trading Post delivery."""
        payload, _ = await self._get(DELIVERY_PATH, api_key=api_key)
        if not isinstance(payload, dict):
            raise ProfitApiError("GW2 delivery response was not an object")
        coins = payload.get("coins")
        if not isinstance(coins, int) or isinstance(coins, bool) or coins < 0:
            raise ProfitApiError("GW2 delivery response had invalid coins")
        LOGGER.debug(
            "Fetched GW2 Trading Post delivery; coins_available=%s",
            coins > 0,
        )
        return coins

    async def fetch_item_names(self, item_ids: set[int]) -> dict[int, str]:
        names: dict[int, str] = {}
        ordered_ids = sorted(item_ids)
        for offset in range(0, len(ordered_ids), ITEM_CHUNK_SIZE):
            chunk = ordered_ids[offset : offset + ITEM_CHUNK_SIZE]
            try:
                payload, _ = await self._get(
                    "/v2/items",
                    params={"ids": ",".join(str(item_id) for item_id in chunk)},
                )
                if not isinstance(payload, list):
                    raise ProfitApiError(
                        "GW2 item response was not a collection"
                    )
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
                    sum(1 for item_id in chunk if item_id in names),
                )
            except (aiohttp.ClientError, TimeoutError, ProfitApiError) as exc:
                # Names are presentation only. One failed chunk falls back to
                # item ids and must not hide the otherwise complete report or
                # prevent later chunks from being attempted.
                LOGGER.warning(
                    "Could not fetch a profit item-name chunk; requested=%s "
                    "error_type=%s",
                    len(chunk),
                    type(exc).__name__,
                )
        return names

    async def fetch_market_prices(
        self,
        item_ids: set[int],
    ) -> dict[int, MarketPrice]:
        """Return usable current buy-order and sell-listing prices."""
        prices: dict[int, MarketPrice] = {}
        ordered_ids = sorted(item_ids)
        for offset in range(0, len(ordered_ids), ITEM_CHUNK_SIZE):
            chunk = ordered_ids[offset : offset + ITEM_CHUNK_SIZE]
            payload, _ = await self._get(
                "/v2/commerce/prices",
                params={"ids": ",".join(str(item_id) for item_id in chunk)},
            )
            if not isinstance(payload, list):
                raise ProfitApiError("GW2 price response was not a collection")
            for item in payload:
                price = _market_price_from_payload(item)
                if price is not None:
                    prices[price[0]] = price[1]
            LOGGER.debug(
                "Fetched GW2 market prices; requested=%s usable=%s",
                len(chunk),
                sum(1 for item_id in chunk if item_id in prices),
            )
        return prices

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
