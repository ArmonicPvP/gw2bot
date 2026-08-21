from __future__ import annotations

import logging
from collections.abc import Sequence

from gw2bot.anchored_series import (
    derive_values,
    value_before,
    value_through,
)
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
    ordered = sorted(movements, key=lambda movement: movement.occurred_at)
    in_window = [
        index
        for index, movement in enumerate(ordered)
        if since <= movement.occurred_at <= until
    ]
    if anchor is None:
        LOGGER.debug(
            "Built gold series without an observed balance; "
            "movements=%s in_window=%s",
            len(ordered),
            len(in_window),
        )
        return GoldSeries(
            points=(),
            movements=tuple(_plot(ordered[index], None) for index in in_window),
        )

    coins_after, baseline = derive_values(
        ordered, anchor.recorded_at, anchor.coins
    )
    coins_at_end = value_through(ordered, coins_after, baseline, until)
    points = [
        GoldPoint(
            at=since,
            coins=value_before(ordered, coins_after, baseline, since),
        )
    ]
    points.extend(
        GoldPoint(at=ordered[index].occurred_at, coins=coins_after[index])
        for index in in_window
    )
    points.append(GoldPoint(at=until, coins=coins_at_end))
    series = GoldSeries(
        points=tuple(points),
        movements=tuple(
            _plot(ordered[index], coins_after[index]) for index in in_window
        ),
    )
    LOGGER.debug(
        "Built gold series; movements=%s in_window=%s points=%s "
        "anchor_offset_seconds=%s end_coins=%s",
        len(ordered),
        len(in_window),
        len(series.points),
        int(until - anchor.recorded_at),
        coins_at_end,
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
