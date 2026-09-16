"""The one-card pages: sign-in, sign-out, and the access refusals.

Every document here is a fixed string except the sign-in page's login URL,
which is escaped after the server has validated it as a local path.
"""

from __future__ import annotations

from html import escape

from gw2bot.web.pages.shared import simple_page

_SIGN_IN_ACTION = '<a class="button" href="/login">Sign in with Discord</a>'


def sign_in_page(login_url: str = "/login") -> str:
    action = (
        '<a class="button" href="'
        + escape(login_url, quote=True)
        + '">Sign in with Discord</a>'
    )
    return simple_page(
        "Guild Events",
        "Guild Events Calendar",
        "Sign in with Discord to view the guild event calendar.",
        action,
    )


SIGN_IN_PAGE = sign_in_page()

SIGNED_OUT_PAGE = simple_page(
    "Signed out",
    "You are signed out",
    "Sign back in with Discord to view the guild event calendar.",
    _SIGN_IN_ACTION,
)

MEMBERS_ONLY_PAGE = simple_page(
    "Members only",
    "Members only",
    "This calendar is only available to members of the Discord server.",
    _SIGN_IN_ACTION,
)

LOGIN_FAILED_PAGE = simple_page(
    "Sign-in failed",
    "Sign-in failed",
    "The Discord sign-in could not be completed. Please try again.",
    _SIGN_IN_ACTION,
)

SERVICE_UNAVAILABLE_PAGE = simple_page(
    "Temporarily unavailable",
    "Temporarily unavailable",
    "The calendar cannot reach Discord right now. Please try again in a "
    "moment.",
    _SIGN_IN_ACTION,
)

OFFICER_ONLY_PAGE = simple_page(
    "Officers only",
    "Officers only",
    "The feast usage dashboard is only available to raffle officers.",
    '<a class="button" href="/">Back to the calendar</a>',
)

ROSTER_OFFICER_ONLY_PAGE = simple_page(
    "Officers only",
    "Officers only",
    "The guild roster history is only available to raffle officers.",
    '<a class="button" href="/">Back to the calendar</a>',
)

GOLD_OFFICER_ONLY_PAGE = simple_page(
    "Officers only",
    "Officers only",
    "The guild bank gold history is only available to raffle officers.",
    '<a class="button" href="/">Back to the calendar</a>',
)
