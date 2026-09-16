"""Static browser dashboard for a member's Trading Post profit reports.

The document is assembled once at import time from its three parts: the
stylesheet, the empty markup, and the script that fills it.
"""

from gw2bot.web.pages.profit.markup import (
    PAGE_SIZE_DEFAULT as PAGE_SIZE_DEFAULT,
    PAGE_SIZE_LIMIT as PAGE_SIZE_LIMIT,
    PROFIT_MARKUP,
    _hidden_items_dialog,
    _pagination_nav,
)
from gw2bot.web.pages.profit.script import PROFIT_SCRIPT
from gw2bot.web.pages.profit.style import PROFIT_STYLE

_PROFIT_PAGE_TEMPLATE = (
    """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="data:,">
<title>Trading Post Profit</title>
<style>"""
    + PROFIT_STYLE
    + """</style>
</head>
<body>
"""
    + PROFIT_MARKUP
    + PROFIT_SCRIPT
    + """</body>
</html>
"""
)

PROFIT_PAGE = (
    _PROFIT_PAGE_TEMPLATE.replace(
        "__ITEMS_PAGES_TOP__",
        _pagination_nav("items", "top", "Realized profit by item pages"),
    )
    .replace(
        "__ITEMS_PAGES_BOTTOM__",
        _pagination_nav(
            "items",
            "bottom",
            "Realized profit by item pages and page size",
        ),
    )
    .replace(
        "__DAYS_PAGES_TOP__",
        _pagination_nav("days", "top", "Daily profit pages"),
    )
    .replace(
        "__DAYS_PAGES_BOTTOM__",
        _pagination_nav("days", "bottom", "Daily profit pages and page size"),
    )
    .replace(
        "__ORDERS_HIDDEN_DIALOG__",
        _hidden_items_dialog("orders", "open order"),
    )
    .replace(
        "__ITEMS_HIDDEN_DIALOG__",
        _hidden_items_dialog("items", "realized profit"),
    )
)
