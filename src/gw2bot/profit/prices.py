"""A shared cache of current Trading Post prices.

Prices are public: the highest buy order for Wool Scrap is the same number
whoever asks. One cache therefore serves every member, and a guild watching
the same handful of items pays for one lookup between them rather than one
each.

The Guild Wars 2 API declares ``Cache-Control: public, max-age=120`` on
``/v2/commerce/prices``, so an entry held for a minute is never staler than
the upstream would have served anyway.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from gw2bot.profit.models import MarketPrice

LOGGER = logging.getLogger(__name__)

# How long one reading is served for. The dashboard refreshes its prices on
# the same beat, so a member watching a page sees a number at most this old.
PRICE_TTL_SECONDS = 60


class MarketPriceCache:
    """Current prices held briefly and shared across every member."""

    def __init__(self, ttl_seconds: int = PRICE_TTL_SECONDS) -> None:
        self._ttl = timedelta(seconds=ttl_seconds)
        self._entries: dict[int, tuple[MarketPrice, datetime]] = {}

    def read(
        self,
        item_ids: set[int],
        *,
        now: datetime | None = None,
    ) -> tuple[dict[int, MarketPrice], set[int]]:
        """Return the prices still fresh, and the ids that must be fetched."""
        checked_at = datetime.now(UTC) if now is None else now
        fresh: dict[int, MarketPrice] = {}
        stale: set[int] = set()
        for item_id in item_ids:
            entry = self._entries.get(item_id)
            if entry is None or entry[1] <= checked_at:
                stale.add(item_id)
                # An expired reading is of no use to anyone, and this cache
                # outlives every request that touched it. Dropping it here
                # keeps the cache the size of what is being watched now
                # rather than of everything ever asked about.
                self._entries.pop(item_id, None)
                continue
            fresh[item_id] = entry[0]
        LOGGER.debug(
            "Read cached market prices; requested=%s fresh=%s stale=%s",
            len(item_ids),
            len(fresh),
            len(stale),
        )
        return fresh, stale

    def write(
        self,
        prices: dict[int, MarketPrice],
        *,
        now: datetime | None = None,
    ) -> None:
        stored_at = datetime.now(UTC) if now is None else now
        expires_at = stored_at + self._ttl
        for item_id, price in prices.items():
            self._entries[item_id] = (price, expires_at)
        LOGGER.debug("Stored cached market prices; records=%s", len(prices))

    def forget(self, item_ids: set[int]) -> None:
        """Drop these readings so the next request goes upstream."""
        for item_id in item_ids:
            self._entries.pop(item_id, None)
        LOGGER.debug("Dropped cached market prices; records=%s", len(item_ids))

    def prune(self, *, now: datetime | None = None) -> int:
        """Forget expired readings so the cache tracks live interest only."""
        checked_at = datetime.now(UTC) if now is None else now
        expired = [
            item_id
            for item_id, (_price, expires_at) in self._entries.items()
            if expires_at <= checked_at
        ]
        for item_id in expired:
            del self._entries[item_id]
        if expired:
            LOGGER.debug("Pruned cached market prices; records=%s", len(expired))
        return len(expired)
