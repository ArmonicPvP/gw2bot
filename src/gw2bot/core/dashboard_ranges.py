"""The window a dashboard draws, and the one a member last picked.

Every dashboard offers the same three preset windows and a pair of dates the
reader picks instead. What they last chose is kept against their Discord
account rather than in the page's URL, so a dashboard reopens on that window
from any browser they sign in from - the way the profit dashboard's window has
always been remembered.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

# The ``range`` value every dashboard sends when the reader picked their own
# dates instead of one of the preset windows.
CUSTOM_RANGE = "custom"

# The window a dashboard opens on when its reader has never picked one.
DEFAULT_RANGE = "24h"

# The names the remembered window is stored under, one per dashboard. The
# profit dashboard is not here: its window is kept beside the rest of that
# member's profit preferences, so ``/profit deletekey`` takes it with them.
FOOD_DASHBOARD = "food"
ROSTER_DASHBOARD = "roster"
GOLD_DASHBOARD = "gold"


@dataclass(frozen=True, slots=True)
class StoredRange:
    """One member's remembered window on one dashboard.

    ``key`` is a preset's own name or ``CUSTOM_RANGE``; a custom window also
    carries the whole epoch seconds the reader's two dates worked out to, and
    a preset carries neither bound because it is measured back from whenever
    the page is opened.
    """

    key: str
    start: int | None = None
    end: int | None = None

    @property
    def custom(self) -> bool:
        return self.key == CUSTOM_RANGE


def is_servable(window: StoredRange, ranges: Mapping[str, int]) -> bool:
    """Whether a stored window can still be drawn by the page that kept it.

    A row written by another release, or edited by hand, may name a preset
    this dashboard no longer offers or a custom window missing a bound. Both
    are read as no choice at all rather than served as something else.
    """
    if window.custom:
        return (
            window.start is not None
            and window.end is not None
            and window.end > window.start
        )
    return window.key in ranges
