"""Deriving a running total from an observed value and the changes around it.

Two pages draw the same shape of graph: a quantity the bot observes now and
then - the guild member count, the guild bank's coin balance - and a stream of
changes that say how it moved. Neither stream is complete on its own. The
observations are sparse, and the guild log returns only about a hundred events
of each type, so a long outage leaves changes the bot will never see.

The way out is the same for both, and it is what this module implements: take
the *newest* observation as the anchor and walk the changes outwards from it -
backwards, subtracting, to recover the past, and forwards, adding, for anything
recorded since. Measuring from the newest observation rather than the oldest is
what keeps the right-hand edge of a graph true: a stretch of missed changes
falls behind the anchor, where it can shift the older history but can no longer
be replayed on top of a total that already includes it.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol


class Change(Protocol):
    """One recorded change to the quantity being tracked."""

    @property
    def occurred_at(self) -> float: ...

    @property
    def delta(self) -> int: ...


def derive_values(
    ordered: Sequence[Change],
    anchor_at: float,
    anchor_value: int,
) -> tuple[list[int], int]:
    """Return the value after each change, and the one before the oldest.

    ``ordered`` is oldest first. Changes at or before the anchor are walked
    backwards from it, subtracting each one to recover the value that stood
    before it; changes after the anchor are walked forwards, adding each one.
    """
    values_after = [0] * len(ordered)
    running = anchor_value
    for index in range(len(ordered) - 1, -1, -1):
        if ordered[index].occurred_at > anchor_at:
            continue
        values_after[index] = running
        running -= ordered[index].delta
    baseline = running
    running = anchor_value
    for index, change in enumerate(ordered):
        if change.occurred_at <= anchor_at:
            continue
        running += change.delta
        values_after[index] = running
    return values_after, baseline


def value_before(
    ordered: Sequence[Change],
    values_after: Sequence[int],
    baseline: int,
    moment: float,
) -> int:
    """The value as it stood just before ``moment``."""
    result = baseline
    for index, change in enumerate(ordered):
        if change.occurred_at >= moment:
            break
        result = values_after[index]
    return result


def value_through(
    ordered: Sequence[Change],
    values_after: Sequence[int],
    baseline: int,
    moment: float,
) -> int:
    """The value once every change up to and including ``moment`` landed.

    Unlike :func:`value_before` a change dated exactly ``moment`` counts,
    because this places a line's closing vertex and that vertex sits on the
    same x as the change's own dot.
    """
    result = baseline
    for index, change in enumerate(ordered):
        if change.occurred_at > moment:
            break
        result = values_after[index]
    return result
