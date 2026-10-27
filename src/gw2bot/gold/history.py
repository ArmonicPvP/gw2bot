from __future__ import annotations

import logging
from collections.abc import Sequence

from gw2bot.core.anchored_series import build_window
from gw2bot.gold.models import (
    GoldBalanceSample,
    GoldEvent,
    GoldMovement,
    GoldPoint,
    GoldSeries,
)

LOGGER = logging.getLogger(__name__)


def build_gold_series(
    movements: Sequence[GoldMovement],
    anchor: GoldBalanceSample | None,
    since: float,
    until: float,
) -> GoldSeries:
    """Derive the balance line and the plotted movements for one window.

    ``movements`` must hold every known coin movement from the earlier of
    ``since`` and the anchor's own moment onwards. The balance at any moment
    is measured from ``anchor``, the last balance the bot actually observed,
    by walking the movements between the two - which is the same reversal the
    one-time import relies on, and is why importing the guild log alongside
    one stash reading recovers a history nobody was recording at the time.

    Only ``since``..``until`` is plotted, but every movement is walked,
    including the ones after ``until``. A custom window that ends in the past
    sits behind the anchor, so the movements between the two are exactly what
    has to be unwound to recover the balance that stood at the window's
    right-hand edge.

    Without an anchor nothing can be placed on a balance axis, so the
    movements come back with no balance and the line is empty rather than
    invented.
    """
    window = build_window(
        movements,
        None if anchor is None else (anchor.recorded_at, anchor.coins),
        since,
        until,
    )
    series = GoldSeries(
        points=tuple(GoldPoint(at=at, coins=value) for at, value in window.points),
        movements=tuple(_plot(change, value) for change, value in window.changes),
    )
    if anchor is None:
        LOGGER.debug(
            "Built gold series without an observed balance; "
            "movements=%s in_window=%s",
            len(movements),
            len(window.changes),
        )
        return series

    LOGGER.debug(
        "Built gold series; movements=%s in_window=%s points=%s "
        "anchor_offset_seconds=%s",
        len(movements),
        len(window.changes),
        len(series.points),
        int(until - anchor.recorded_at),
    )
    return series


def _plot(movement: GoldMovement, coins_after: int | None) -> GoldEvent:
    return GoldEvent(
        occurred_at=movement.occurred_at,
        operation=movement.operation,
        username=movement.username,
        coins=movement.coins,
        coins_after=coins_after,
    )

