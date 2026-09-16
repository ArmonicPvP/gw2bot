"""The HTML documents the site serves, one module per page.

Each page is a fixed string assembled at import time from the chrome in
`shared`, so serving one is a lookup rather than a render.
"""

from gw2bot.web.pages.calendar import CALENDAR_PAGE as CALENDAR_PAGE
from gw2bot.web.pages.food import FOOD_PAGE as FOOD_PAGE
from gw2bot.web.pages.gold import GOLD_PAGE as GOLD_PAGE
from gw2bot.web.pages.notices import (
    GOLD_OFFICER_ONLY_PAGE as GOLD_OFFICER_ONLY_PAGE,
    LOGIN_FAILED_PAGE as LOGIN_FAILED_PAGE,
    MEMBERS_ONLY_PAGE as MEMBERS_ONLY_PAGE,
    OFFICER_ONLY_PAGE as OFFICER_ONLY_PAGE,
    ROSTER_OFFICER_ONLY_PAGE as ROSTER_OFFICER_ONLY_PAGE,
    SERVICE_UNAVAILABLE_PAGE as SERVICE_UNAVAILABLE_PAGE,
    SIGN_IN_PAGE as SIGN_IN_PAGE,
    SIGNED_OUT_PAGE as SIGNED_OUT_PAGE,
    sign_in_page as sign_in_page,
)
from gw2bot.web.pages.profit import PROFIT_PAGE as PROFIT_PAGE
from gw2bot.web.pages.roster import ROSTER_PAGE as ROSTER_PAGE
