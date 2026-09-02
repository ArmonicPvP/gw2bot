from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Awaitable, Callable
from urllib.parse import urlencode

import aiohttp
import discord
from aiohttp import web
from sqlalchemy.exc import SQLAlchemyError

from gw2bot.config import Config
from gw2bot.discord_utils import resolve_display_name, user_has_role
from gw2bot.feast_stock import (
    FEAST_USAGE_RANGES,
    TRACKED_FEASTS,
    Feast,
    FeastStockSeries,
    feast_removals,
)
from gw2bot.gold import GOLD_RANGES, GoldEvent, build_gold_series
from gw2bot.profit.api import ProfitApiError
from gw2bot.profit.service import (
    MissingProfitApiKey,
    serialize_profit_report,
)
from gw2bot.roster import ROSTER_RANGES, RosterEvent, build_roster_series
from gw2bot.web import auth
from gw2bot.web.calendar import CalendarEntry, calendar_entries
from gw2bot.web.page import (
    CALENDAR_PAGE,
    FOOD_PAGE,
    GOLD_OFFICER_ONLY_PAGE,
    GOLD_PAGE,
    LOGIN_FAILED_PAGE,
    MEMBERS_ONLY_PAGE,
    OFFICER_ONLY_PAGE,
    ROSTER_OFFICER_ONLY_PAGE,
    ROSTER_PAGE,
    SERVICE_UNAVAILABLE_PAGE,
    SIGNED_OUT_PAGE,
    sign_in_page,
)
from gw2bot.web.profit_page import PROFIT_PAGE

if TYPE_CHECKING:
    from gw2bot.bot import Gw2Bot

LOGGER = logging.getLogger(__name__)

MAX_RANGE_DAYS = 62
NAME_CACHE_TTL_SECONDS = 3600

# The ``range`` value both dashboards send when the reader picked their own
# dates instead of one of the preset windows.
CUSTOM_RANGE = "custom"

# The longest custom window either dashboard will serve. A hand-typed range is
# otherwise unbounded, and every row between its edges is read into memory
# before anything is drawn.
MAX_CUSTOM_WINDOW_SECONDS = 366 * 24 * 60 * 60

# A session cookie only proves the holder was a guild member when they signed
# in, so membership is re-checked on later requests too. The cache keeps that
# off Discord's API on every request while bounding how long a departed or
# banned member keeps access.
MEMBERSHIP_CACHE_TTL_SECONDS = 300

# How long a stale membership answer keeps being served while Discord cannot be
# reached. Without it an outage turns every request into another fetch_member
# call against a rate-limited endpoint, because the failed lookup never re-arms
# the cache entry.
MEMBERSHIP_FAILURE_BACKOFF_SECONDS = 60

UNKNOWN_NAME = "Unknown"

# How long one build of the pending-invite list is served to the roster page.
# Building it costs a GW2 API call and a refresh of the Trial application forum
# index, and an invite that has not been accepted yet changes far more slowly
# than a page is reloaded, so the list is cached rather than rebuilt per
# request.
PENDING_INVITE_CACHE_TTL_SECONDS = 300

# The feast usage dashboard is gated behind the role /settings roles food_page
# names, which follows /raffle removetickets' role until it is set apart, the
# roster history behind /settings roles roster_page, and the guild bank's gold
# history behind /settings roles gold_page; both of those start from the same
# role.

# Every response this server sends is scoped to one signed-in member, so none
# of it may be kept by the reverse proxy the README asks operators to run, by a
# shared cache, or by the browser's back/forward cache.
NO_STORE = "no-store, private"

# Paths reachable without a session; everything else is members-only.
PUBLIC_PATHS = frozenset(
    {"/login", "/oauth/callback", "/logout", "/favicon.ico"}
)

_Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]

# Request mappings accept string keys. aiohttp 3.13 removed the RequestKey
# helper that used to add generic typing around this entry.
SESSION_KEY = "web_session"


@dataclass(frozen=True, slots=True)
class _Window:
    """The stretch of history one dashboard request asks to be drawn.

    ``key`` is echoed back to the page as the range it got, which is the
    preset's own name or ``CUSTOM_RANGE``.
    """

    key: str
    since: float
    until: float


def _redirect(location: str) -> web.Response:
    return web.Response(
        status=302,
        headers={"Location": location, "Cache-Control": NO_STORE},
    )


class WebServer:
    def __init__(
        self,
        bot: Gw2Bot,
        config: Config,
        http: aiohttp.ClientSession,
    ):
        if (
            config.web_base_url is None
            or config.discord_oauth_client_id is None
            or config.discord_oauth_client_secret is None
            or config.web_session_secret is None
        ):
            raise ValueError(
                "WebServer requires the web configuration values"
            )
        self._bot = bot
        self._config = config
        self._http = http
        self._base_url = config.web_base_url
        self._client_id = config.discord_oauth_client_id
        self._client_secret = config.discord_oauth_client_secret
        self._session_secret = config.web_session_secret
        self._session_ttl = config.web_session_ttl_seconds
        self._redirect_uri = f"{config.web_base_url}/oauth/callback"
        self._secure_cookies = config.web_base_url.startswith("https://")
        self._runner: web.AppRunner | None = None
        self._names: dict[int, tuple[str, float]] = {}
        # user id -> (is_member, monotonic time the answer stops being trusted)
        self._members: dict[int, tuple[bool, float]] = {}
        # (role id, user id) -> (holds the role, monotonic expiry). Cached
        # with the same TTL and outage backoff as _members. Keyed by the role
        # as well as the user because three pages are gated by three settings,
        # and an operator may point them at different roles.
        self._role_members: dict[tuple[int, int], tuple[bool, float]] = {}
        # The last built pending-invite payload and the monotonic time it
        # stops being served, or None while none has been built.
        self._pending_invites: tuple[list[dict[str, object]], float] | None = (
            None
        )
        self.app = web.Application(
            middlewares=[self._log_middleware, self._auth_middleware]
        )
        self.app.add_routes(
            [
                web.get("/", self._index),
                web.get("/login", self._login),
                web.get("/oauth/callback", self._callback),
                # POST, not GET: a GET sign-out is a CSRF any third-party page
                # could fire with an <img> tag. SameSite=Lax withholds the
                # session cookie from a cross-site POST, so this cannot be
                # triggered from off-site.
                web.post("/logout", self._logout),
                web.get("/api/me", self._me),
                web.get("/api/events", self._events),
                web.get("/food", self._food),
                web.get("/api/food", self._food_data),
                web.get("/roster", self._roster),
                web.get("/api/roster", self._roster_data),
                web.get("/api/pending", self._pending_data),
                web.get("/gold", self._gold),
                web.get("/api/gold", self._gold_data),
                web.get("/profit", self._profit),
                web.get("/api/profit", self._profit_data),
            ]
        )

    async def start(self) -> None:
        # aiohttp's built-in access log prints full request targets with
        # query strings, which would leak OAuth codes; the log middleware
        # records sanitized paths instead.
        runner = web.AppRunner(self.app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", self._config.web_port)
        try:
            await site.start()
        except OSError:
            # The port is taken or unusable. setup() already allocated the
            # runner's server infrastructure, and stop() keys off _runner, so
            # release it here or it leaks for the life of the process.
            await runner.cleanup()
            raise
        self._runner = runner
        LOGGER.info(
            "Web calendar server listening; port=%s",
            self._config.web_port,
        )

    async def stop(self) -> None:
        if self._runner is None:
            return
        await self._runner.cleanup()
        self._runner = None
        LOGGER.debug("Web calendar server stopped")

    @web.middleware
    async def _log_middleware(
        self,
        request: web.Request,
        handler: _Handler,
    ) -> web.StreamResponse:
        started = time.monotonic()
        try:
            response = await handler(request)
        except web.HTTPException as exc:
            LOGGER.debug(
                "Web request; method=%s path=%s status=%s duration_ms=%s",
                request.method,
                request.path,
                exc.status,
                int((time.monotonic() - started) * 1000),
            )
            raise
        LOGGER.debug(
            "Web request; method=%s path=%s status=%s duration_ms=%s",
            request.method,
            request.path,
            response.status,
            int((time.monotonic() - started) * 1000),
        )
        return response

    @web.middleware
    async def _auth_middleware(
        self,
        request: web.Request,
        handler: _Handler,
    ) -> web.StreamResponse:
        if request.path in PUBLIC_PATHS:
            return await handler(request)
        cookie = request.cookies.get(auth.SESSION_COOKIE, "")
        session = auth.verify_session(
            self._session_secret,
            cookie,
            datetime.now(UTC),
        )
        if session is None:
            LOGGER.debug(
                "Rejected unauthenticated web request; path=%s",
                request.path,
            )
            if request.path.startswith("/api/"):
                return self._json({"error": "unauthorized"}, status=401)
            return_to = auth.sanitize_return_target(str(request.rel_url))
            login_url = f"/login?{urlencode({'next': return_to})}"
            LOGGER.debug(
                "Offering web sign-in; return_path=%s",
                return_to.partition("?")[0],
            )
            return self._html(sign_in_page(login_url), status=401)
        if await self._cached_membership(session.user_id) is False:
            LOGGER.info(
                "Revoked web session; signer is no longer a guild member; "
                "user_id=%s",
                session.user_id,
            )
            return self._members_only(request)
        request[SESSION_KEY] = session
        return await handler(request)

    def _members_only(self, request: web.Request) -> web.Response:
        if request.path.startswith("/api/"):
            response = self._json({"error": "forbidden"}, status=403)
        else:
            response = self._html(MEMBERS_ONLY_PAGE, status=403)
        response.del_cookie(auth.SESSION_COOKIE, path="/")
        return response

    @staticmethod
    def _html(document: str, status: int = 200) -> web.Response:
        return web.Response(
            text=document,
            status=status,
            content_type="text/html",
            headers={"Cache-Control": NO_STORE},
        )

    @staticmethod
    def _json(payload: dict[str, object], status: int = 200) -> web.Response:
        return web.json_response(
            payload,
            status=status,
            headers={"Cache-Control": NO_STORE},
        )

    def _set_cookie(
        self,
        response: web.StreamResponse,
        name: str,
        value: str,
        max_age: int,
    ) -> None:
        response.set_cookie(
            name,
            value,
            max_age=max_age,
            path="/",
            httponly=True,
            samesite="Lax",
            secure=self._secure_cookies,
        )

    async def _index(self, request: web.Request) -> web.StreamResponse:
        return self._html(CALENDAR_PAGE)

    async def _login(self, request: web.Request) -> web.StreamResponse:
        return_to = auth.sanitize_return_target(request.query.get("next"))
        state, cookie = auth.sign_state(
            self._session_secret,
            datetime.now(UTC),
            return_to=return_to,
        )
        response = _redirect(
            auth.authorize_url(self._client_id, self._redirect_uri, state)
        )
        self._set_cookie(
            response,
            auth.STATE_COOKIE,
            cookie,
            auth.STATE_TTL_SECONDS,
        )
        LOGGER.debug(
            "Redirecting web login to Discord authorization; return_path=%s",
            return_to.partition("?")[0],
        )
        return response

    async def _callback(self, request: web.Request) -> web.StreamResponse:
        code = request.query.get("code", "")
        state = request.query.get("state", "")
        state_cookie = request.cookies.get(auth.STATE_COOKIE, "")
        now = datetime.now(UTC)
        error = request.query.get("error", "")
        if error:
            # Discord returned an error instead of a code. A silent
            # (prompt=none) attempt does this when it may not show a screen; a
            # one-time interactive retry resolves it.
            return self._retry_or_fail_authorization(
                error,
                state,
                state_cookie,
                now,
            )
        if not code or not auth.verify_state(
            self._session_secret,
            state_cookie,
            state,
            now,
        ):
            LOGGER.warning("OAuth state validation failed")
            response = self._html(LOGIN_FAILED_PAGE, status=403)
            response.del_cookie(auth.STATE_COOKIE, path="/")
            return response
        return_to = auth.state_return_target(
            self._session_secret,
            state_cookie,
        )
        try:
            token = await auth.exchange_code(
                self._http,
                self._client_id,
                self._client_secret,
                self._redirect_uri,
                code,
            )
            identity = await auth.fetch_identity(self._http, token)
        except auth.OAuthExchangeError:
            return self._html(SERVICE_UNAVAILABLE_PAGE, status=502)
        except aiohttp.ClientError as exc:
            LOGGER.warning(
                "OAuth exchange transport failure; error_type=%s",
                type(exc).__name__,
            )
            return self._html(SERVICE_UNAVAILABLE_PAGE, status=502)

        is_member = await self._check_guild_member(identity.user_id)
        if is_member is None:
            return self._html(SERVICE_UNAVAILABLE_PAGE, status=503)
        LOGGER.info(
            "Web login membership check; user_id=%s member=%s",
            identity.user_id,
            is_member,
        )
        if not is_member:
            response = self._html(MEMBERS_ONLY_PAGE, status=403)
            response.del_cookie(auth.STATE_COOKIE, path="/")
            return response

        session_value = auth.sign_session(
            self._session_secret,
            identity.user_id,
            identity.name,
            now + timedelta(seconds=self._session_ttl),
        )
        response = _redirect(return_to)
        self._set_cookie(
            response,
            auth.SESSION_COOKIE,
            session_value,
            self._session_ttl,
        )
        response.del_cookie(auth.STATE_COOKIE, path="/")
        LOGGER.debug(
            "Completed web login redirect; return_path=%s",
            return_to.partition("?")[0],
        )
        return response

    def _retry_or_fail_authorization(
        self,
        error: str,
        state: str,
        state_cookie: str,
        now: datetime,
    ) -> web.Response:
        safe_error = auth.sanitize_authorize_error(error)
        # Only retry an error a real prompt would resolve, only for a request
        # we actually started (the state cookie proves it and blocks a crafted
        # error from forcing a redirect), and only when this is not already the
        # retry, so a user is never bounced through consent more than once.
        promptable = error in auth.PROMPTABLE_AUTHORIZE_ERRORS
        valid_state = auth.verify_state(
            self._session_secret,
            state_cookie,
            state,
            now,
        )
        already_retried = auth.state_is_consent_retry(
            self._session_secret,
            state_cookie,
        )
        return_to = (
            auth.state_return_target(self._session_secret, state_cookie)
            if valid_state
            else "/"
        )
        if promptable and valid_state and not already_retried:
            LOGGER.info(
                "Silent Discord authorization needs a prompt; retrying with "
                "consent; error=%s",
                safe_error,
            )
            retry_state, cookie = auth.sign_state(
                self._session_secret,
                now,
                consent_retry=True,
                return_to=return_to,
            )
            response = _redirect(
                auth.authorize_url(
                    self._client_id,
                    self._redirect_uri,
                    retry_state,
                    prompt_none=False,
                )
            )
            self._set_cookie(
                response,
                auth.STATE_COOKIE,
                cookie,
                auth.STATE_TTL_SECONDS,
            )
            return response
        LOGGER.warning(
            "Discord authorization failed; error=%s promptable=%s "
            "valid_state=%s already_retried=%s",
            safe_error,
            promptable,
            valid_state,
            already_retried,
        )
        response = self._html(LOGIN_FAILED_PAGE, status=403)
        response.del_cookie(auth.STATE_COOKIE, path="/")
        return response

    async def _cached_membership(self, user_id: int) -> bool | None:
        """Membership for a signed-in user, cached for a short TTL."""
        cached = self._members.get(user_id)
        if cached is not None and time.monotonic() < cached[1]:
            return cached[0]
        membership = await self._check_guild_member(user_id)
        if membership is None:
            if cached is None:
                return None
            # Discord is unreachable. Fall back to the last known answer
            # rather than locking every signed-in member out of a calendar
            # that is read-only anyway; an unknown state is not evidence the
            # user left. Re-arm the entry for a short backoff so the outage
            # costs one lookup per window instead of one per request: the bot
            # runs without the members intent, so every check that misses the
            # cache is a fetch_member call against a rate-limited endpoint.
            self._members[user_id] = (
                cached[0],
                time.monotonic() + MEMBERSHIP_FAILURE_BACKOFF_SECONDS,
            )
            return cached[0]
        return membership

    async def _check_guild_member(self, user_id: int) -> bool | None:
        """Return membership, or None when Discord cannot be checked."""
        membership = await self._is_guild_member(user_id)
        if membership is not None:
            self._members[user_id] = (
                membership,
                time.monotonic() + MEMBERSHIP_CACHE_TTL_SECONDS,
            )
        return membership

    async def _is_guild_member(self, user_id: int) -> bool | None:
        guild = self._bot.get_guild(self._config.discord_command_guild_id)
        if guild is None:
            LOGGER.warning(
                "Membership check skipped; guild unavailable"
            )
            return None
        if guild.get_member(user_id) is not None:
            return True
        try:
            await guild.fetch_member(user_id)
        except discord.NotFound:
            return False
        except discord.HTTPException as exc:
            LOGGER.warning(
                "Membership check failed; error_type=%s",
                type(exc).__name__,
            )
            return None
        return True

    async def _cached_role(self, user_id: int, role_id: int) -> bool | None:
        """Whether a signed-in user holds a gated role, cached for a TTL."""
        key = (role_id, user_id)
        cached = self._role_members.get(key)
        if cached is not None and time.monotonic() < cached[1]:
            return cached[0]
        has_role = await self._check_role(user_id, role_id)
        if has_role is None:
            if cached is None:
                return None
            # Discord is unreachable. Ride the last known answer rather than
            # bouncing an officer off the page, and re-arm the entry for a
            # short backoff so the outage costs one lookup per window instead
            # of one per request (the bot runs without the members intent, so
            # every cache miss is a rate-limited fetch_member call).
            self._role_members[key] = (
                cached[0],
                time.monotonic() + MEMBERSHIP_FAILURE_BACKOFF_SECONDS,
            )
            return cached[0]
        return has_role

    async def _check_role(self, user_id: int, role_id: int) -> bool | None:
        """Return role membership, or None when Discord cannot be checked."""
        has_role = await self._member_holds_role(user_id, role_id)
        if has_role is not None:
            self._role_members[(role_id, user_id)] = (
                has_role,
                time.monotonic() + MEMBERSHIP_CACHE_TTL_SECONDS,
            )
        return has_role

    async def _member_holds_role(
        self,
        user_id: int,
        role_id: int,
    ) -> bool | None:
        """Whether the member holds ``role_id``, or None when unreachable."""
        guild = self._bot.get_guild(self._config.discord_command_guild_id)
        if guild is None:
            LOGGER.warning("Role check skipped; guild unavailable")
            return None
        member = guild.get_member(user_id)
        if member is None:
            try:
                member = await guild.fetch_member(user_id)
            except discord.NotFound:
                return False
            except discord.HTTPException as exc:
                LOGGER.warning(
                    "Role check failed; error_type=%s",
                    type(exc).__name__,
                )
                return None
        return user_has_role(member, role_id)

    async def _require_role_access(
        self,
        request: web.Request,
        role_id: int,
        denial_page: str,
        feature: str,
    ) -> web.Response | None:
        """Return a denial response, or None when the caller may proceed.

        The auth middleware has already proven a valid session and current
        guild membership; this only adds the role check the gated pages need
        on top. ``feature`` names the page in the log line, so a refusal can
        be traced to the page that made it.
        """
        session = request[SESSION_KEY]
        authorized = await self._cached_role(session.user_id, role_id)
        if authorized is None:
            LOGGER.info(
                "Page authorization unavailable; Discord unreachable; "
                "page=%s user_id=%s",
                feature,
                session.user_id,
            )
            if request.path.startswith("/api/"):
                return self._json({"error": "unavailable"}, status=503)
            return self._html(SERVICE_UNAVAILABLE_PAGE, status=503)
        if not authorized:
            LOGGER.info(
                "Rejected page request; missing role; page=%s user_id=%s",
                feature,
                session.user_id,
            )
            if request.path.startswith("/api/"):
                return self._json({"error": "forbidden"}, status=403)
            return self._html(denial_page, status=403)
        return None

    async def _require_food_access(
        self,
        request: web.Request,
    ) -> web.Response | None:
        return await self._require_role_access(
            request,
            self._config.food_page_role_id,
            OFFICER_ONLY_PAGE,
            "food",
        )

    async def _require_roster_access(
        self,
        request: web.Request,
    ) -> web.Response | None:
        return await self._require_role_access(
            request,
            self._config.roster_page_role_id,
            ROSTER_OFFICER_ONLY_PAGE,
            "roster",
        )

    async def _require_gold_access(
        self,
        request: web.Request,
    ) -> web.Response | None:
        return await self._require_role_access(
            request,
            self._config.gold_page_role_id,
            GOLD_OFFICER_ONLY_PAGE,
            "gold",
        )

    async def _logout(self, request: web.Request) -> web.StreamResponse:
        response = self._html(SIGNED_OUT_PAGE)
        response.del_cookie(auth.SESSION_COOKIE, path="/")
        LOGGER.debug("Cleared web session cookie on logout")
        return response

    async def _me(self, request: web.Request) -> web.StreamResponse:
        session = request[SESSION_KEY]
        return self._json({"name": session.name})

    async def _profit(self, request: web.Request) -> web.StreamResponse:
        LOGGER.debug("Serving profit dashboard page")
        return self._html(PROFIT_PAGE)

    async def _profit_data(self, request: web.Request) -> web.StreamResponse:
        raw_days = request.query.get("days", "30")
        try:
            days = int(raw_days)
        except ValueError:
            LOGGER.debug("Rejected profit report; reason=days-malformed")
            return self._json({"error": "invalid days"}, status=400)
        if not 1 <= days <= 90:
            LOGGER.debug("Rejected profit report; reason=days-range")
            return self._json({"error": "invalid days"}, status=400)
        service = self._bot.profit_service
        if service is None:
            LOGGER.error("Could not serve profit report; service=unavailable")
            return self._json({"error": "unavailable"}, status=503)
        session = request[SESSION_KEY]
        try:
            report = await service.load_report(session.user_id, days)
        except MissingProfitApiKey:
            LOGGER.debug(
                "Rejected profit report; user_id=%s reason=api-key-unset",
                session.user_id,
            )
            return self._json({"error": "api_key_missing"}, status=409)
        except (aiohttp.ClientError, TimeoutError, ProfitApiError) as exc:
            LOGGER.warning(
                "Could not serve profit report; user_id=%s error_type=%s",
                session.user_id,
                type(exc).__name__,
            )
            return self._json({"error": "upstream unavailable"}, status=502)
        except (SQLAlchemyError, ValueError) as exc:
            LOGGER.error(
                "Could not build profit report; user_id=%s error_type=%s",
                session.user_id,
                type(exc).__name__,
            )
            return self._json({"error": "report unavailable"}, status=500)
        payload = serialize_profit_report(report)
        LOGGER.debug(
            "Served profit report; user_id=%s days=%s realized_items=%s "
            "unrealized_items=%s",
            session.user_id,
            days,
            len(report.realized.items),
            len(report.unrealized.items),
        )
        return self._json(payload)

    async def _food(self, request: web.Request) -> web.StreamResponse:
        denied = await self._require_food_access(request)
        if denied is not None:
            return denied
        return self._html(FOOD_PAGE)

    def _resolve_window(
        self,
        request: web.Request,
        ranges: Mapping[str, int],
        subject: str,
    ) -> _Window | web.Response:
        """The window a dashboard request asks for, or the refusal to serve it.

        ``subject`` names the dashboard in the debug trace and nothing else; no
        part of the query reaches the log.
        """
        range_key = request.query.get("range", "24h")
        now = datetime.now(UTC).timestamp()
        if range_key == CUSTOM_RANGE:
            return self._custom_window(request, now, subject)
        window = ranges.get(range_key)
        if window is None:
            LOGGER.debug("Rejected %s request; reason=range", subject)
            return self._json({"error": "invalid range"}, status=400)
        return _Window(key=range_key, since=now - window, until=now)

    def _custom_window(
        self,
        request: web.Request,
        now: float,
        subject: str,
    ) -> _Window | web.Response:
        """The window a pair of ``start`` and ``end`` epoch seconds asks for.

        Both bounds are whole seconds the page computed from the dates the
        reader picked, so the pair is read as integers and anything else is
        refused rather than coerced. A whole number too large to be a float is
        refused with them: Python holds it happily, and the window is carried
        as seconds from the epoch the rest of the way.
        """
        try:
            since = float(int(request.query["start"]))
            requested_end = float(int(request.query["end"]))
        except (KeyError, ValueError, OverflowError):
            LOGGER.debug("Rejected %s request; reason=custom-bounds", subject)
            return self._json({"error": "invalid range"}, status=400)
        # Nothing has been recorded for a moment that has not happened, so a
        # window running past the present stops there instead of drawing a flat
        # run out to it.
        until = min(requested_end, now)
        if until <= since:
            LOGGER.debug("Rejected %s request; reason=custom-order", subject)
            return self._json({"error": "invalid range"}, status=400)
        if until - since > MAX_CUSTOM_WINDOW_SECONDS:
            LOGGER.debug("Rejected %s request; reason=custom-span", subject)
            return self._json({"error": "invalid range"}, status=400)
        LOGGER.debug(
            "Accepted a custom %s window; days=%s ended_early=%s",
            subject,
            int((until - since) // 86400),
            until < requested_end,
        )
        return _Window(key=CUSTOM_RANGE, since=since, until=until)

    async def _food_data(self, request: web.Request) -> web.StreamResponse:
        denied = await self._require_food_access(request)
        if denied is not None:
            return denied
        window = self._resolve_window(
            request, FEAST_USAGE_RANGES, "feast usage"
        )
        if isinstance(window, web.Response):
            return window

        # get_feast_stock_series is synchronous SQLite sharing the Discord
        # client's event loop, so run it off-loop like the calendar query.
        series = await asyncio.to_thread(
            self._bot.raffle_store.get_feast_stock_series,
            window.since,
            window.until,
        )
        payload = [
            self._serialize_feast(feast, series) for feast in TRACKED_FEASTS
        ]
        LOGGER.debug(
            "Served feast usage; range=%s feasts=%s samples=%s removals=%s",
            window.key,
            len(payload),
            sum(len(item.samples) for item in series.values()),
            sum(len(feast_removals(item)) for item in series.values()),
        )
        return self._json(
            {
                "range": window.key,
                "since": window.since,
                "now": window.until,
                "feasts": payload,
            }
        )

    @staticmethod
    def _serialize_feast(
        feast: Feast,
        series: dict[int, FeastStockSeries],
    ) -> dict[str, object]:
        item = series.get(feast.guild_storage_id)
        if item is None:
            points: list[dict[str, object]] = []
            removals: list[dict[str, object]] = []
        else:
            points = [
                {"t": sample.recorded_at, "count": sample.count}
                for sample in item.samples
            ]
            # feast_removals returns oldest first; the table shows newest first.
            removals = [
                {
                    "t": removal.recorded_at,
                    "amount": removal.amount,
                    "remaining": removal.remaining,
                }
                for removal in reversed(feast_removals(item))
            ]
        return {
            "id": feast.guild_storage_id,
            "name": feast.name,
            "points": points,
            "removals": removals,
        }

    async def _roster(self, request: web.Request) -> web.StreamResponse:
        denied = await self._require_roster_access(request)
        if denied is not None:
            return denied
        return self._html(ROSTER_PAGE)

    async def _roster_data(self, request: web.Request) -> web.StreamResponse:
        denied = await self._require_roster_access(request)
        if denied is not None:
            return denied
        window = self._resolve_window(
            request, ROSTER_RANGES, "roster history"
        )
        if isinstance(window, web.Response):
            return window

        # Both reads are synchronous SQLite on the Discord client's event
        # loop, so they go to a worker thread like the calendar's query. The
        # store pools its connections per thread, so one thread is safe.
        anchor = await asyncio.to_thread(
            self._bot.raffle_store.get_last_member_count
        )
        # Events are read from the earlier of the window and the anchor: the
        # count at any moment is measured by walking the events between it and
        # the anchor, so an anchor older than the window still needs the
        # events that stand between them. An anchor newer than the window's end
        # needs the ones after it just as much, and those are read anyway
        # because the query has no upper bound.
        lookback = (
            window.since
            if anchor is None
            else min(window.since, anchor.recorded_at)
        )
        events = await asyncio.to_thread(
            self._bot.raffle_store.get_membership_events,
            lookback,
        )
        series = build_roster_series(
            events, anchor, window.since, window.until
        )
        LOGGER.debug(
            "Served roster history; range=%s events=%s joins=%s leaves=%s "
            "kicks=%s points=%s counted=%s",
            window.key,
            len(series.events),
            series.joins,
            series.leaves,
            series.kicks,
            len(series.points),
            anchor is not None,
        )
        return self._json(
            {
                "range": window.key,
                "since": window.since,
                "now": window.until,
                # The count as it stood at the window's end, or None when none
                # has ever been observed and the line is therefore empty.
                "member_count": (
                    series.points[-1].member_count if series.points else None
                ),
                "points": [
                    {"t": point.at, "count": point.member_count}
                    for point in series.points
                ],
                # Newest first: the table below the chart reads like the
                # channel it was built from.
                "events": [
                    self._serialize_roster_event(event)
                    for event in reversed(series.events)
                ],
                "joins": series.joins,
                "leaves": series.leaves,
                "kicks": series.kicks,
            }
        )

    def clear_pending_invites(self) -> None:
        """Drop the cached pending-invite payload.

        The guild the list is read from and the forum its Discord matches come
        from are settings, and this server outlives a change to either, so the
        bot calls this rather than letting the old guild's invites be served
        for the rest of the TTL.
        """
        had_cache = self._pending_invites is not None
        self._pending_invites = None
        LOGGER.debug("Cleared the pending invite cache; had_cache=%s", had_cache)

    async def _pending_data(self, request: web.Request) -> web.StreamResponse:
        denied = await self._require_roster_access(request)
        if denied is not None:
            return denied
        if not self._bot.gw2_api_enabled:
            # The startup warning already named the settings this needs, so
            # the page is told the section is off rather than shown an error.
            missing = self._bot._config.missing_gw2_api_settings
            LOGGER.debug(
                "Served pending invites; available=false reason=gw2-api-unset "
                "missing=%s",
                len(missing),
            )
            # The settings are named, not merely counted, so the section can
            # tell the reader which /settings subcommands turn it on - the same
            # thing a command that needs the GW2 API replies with.
            return self._json(
                {"available": False, "invites": [], "missing": list(missing)}
            )

        cached = self._pending_invites
        if cached is not None and time.monotonic() < cached[1]:
            LOGGER.debug(
                "Served pending invites; available=true cached=true "
                "invites=%s",
                len(cached[0]),
            )
            return self._json({"available": True, "invites": cached[0]})

        try:
            entries = await self._bot.build_pending_invite_entries()
        except (aiohttp.ClientError, TimeoutError, RuntimeError) as exc:
            LOGGER.warning(
                "Could not serve pending invites; error_type=%s",
                type(exc).__name__,
            )
            return self._json({"error": "unavailable"}, status=503)

        names = await self._display_names(
            {
                entry.discord_user_id
                for entry in entries
                if entry.discord_user_id is not None
            }
        )
        invites: list[dict[str, object]] = [
            {
                "name": entry.username,
                # The mention a Discord message carries is unreadable on a web
                # page, so the matched account is named instead. An account
                # with no matching application post has no Discord name to
                # show, and the page says so in its own words.
                "discord_name": (
                    None
                    if entry.discord_user_id is None
                    else names.get(entry.discord_user_id, UNKNOWN_NAME)
                ),
            }
            for entry in sorted(
                entries, key=lambda entry: (entry.username.casefold(), entry.username)
            )
        ]
        self._pending_invites = (
            invites,
            time.monotonic() + PENDING_INVITE_CACHE_TTL_SECONDS,
        )
        LOGGER.debug(
            "Served pending invites; available=true cached=false invites=%s "
            "matched=%s",
            len(invites),
            sum(1 for invite in invites if invite["discord_name"] is not None),
        )
        return self._json({"available": True, "invites": invites})

    @staticmethod
    def _serialize_roster_event(event: RosterEvent) -> dict[str, object]:
        return {
            "t": event.occurred_at,
            "kind": event.kind,
            "name": event.username,
            "actor": event.actor,
            "count": event.member_count,
            "imported": event.imported,
        }

    async def _gold(self, request: web.Request) -> web.StreamResponse:
        denied = await self._require_gold_access(request)
        if denied is not None:
            return denied
        return self._html(GOLD_PAGE)

    async def _gold_data(self, request: web.Request) -> web.StreamResponse:
        denied = await self._require_gold_access(request)
        if denied is not None:
            return denied
        window = self._resolve_window(request, GOLD_RANGES, "gold history")
        if isinstance(window, web.Response):
            return window

        # Both reads are synchronous SQLite on the Discord client's event
        # loop, so they go to a worker thread like the calendar's query. The
        # store pools its connections per thread, so one thread is safe.
        anchor = await asyncio.to_thread(
            self._bot.raffle_store.get_last_stash_balance
        )
        # Movements are read from the earlier of the window and the anchor:
        # the balance at any moment is measured by walking the movements
        # between it and the anchor, so an anchor older than the window still
        # needs the movements that stand between them. An anchor newer than
        # the window's end needs the ones after it just as much, and those are
        # read anyway because the query has no upper bound.
        lookback = (
            window.since
            if anchor is None
            else min(window.since, anchor.recorded_at)
        )
        movements = await asyncio.to_thread(
            self._bot.raffle_store.get_gold_movements,
            lookback,
        )
        series = build_gold_series(
            movements, anchor, window.since, window.until
        )
        LOGGER.debug(
            "Served gold history; range=%s movements=%s points=%s "
            "anchored=%s",
            window.key,
            len(series.movements),
            len(series.points),
            anchor is not None,
        )
        return self._json(
            {
                "range": window.key,
                "since": window.since,
                "now": window.until,
                # The balance as it stood at the window's end, or None when
                # none has ever been observed and the line is therefore empty.
                "coins": (
                    series.points[-1].coins if series.points else None
                ),
                "points": [
                    {"t": point.at, "coins": point.coins}
                    for point in series.points
                ],
                # Newest first: the table below the chart reads like a bank
                # statement.
                "movements": [
                    self._serialize_gold_movement(movement)
                    for movement in reversed(series.movements)
                ],
                "deposited": series.deposited,
                "withdrawn": series.withdrawn,
                "net": series.net,
            }
        )

    @staticmethod
    def _serialize_gold_movement(movement: GoldEvent) -> dict[str, object]:
        return {
            "t": movement.occurred_at,
            "operation": movement.operation,
            "name": movement.username,
            "coins": movement.coins,
            "after": movement.coins_after,
        }

    async def _events(self, request: web.Request) -> web.StreamResponse:
        try:
            range_start = datetime.fromtimestamp(
                int(request.query["start"]),
                UTC,
            )
            range_end = datetime.fromtimestamp(int(request.query["end"]), UTC)
        except (KeyError, ValueError, OverflowError, OSError):
            LOGGER.debug("Rejected calendar range request; reason=malformed")
            return self._json({"error": "invalid range"}, status=400)
        if range_end <= range_start or range_end - range_start > timedelta(
            days=MAX_RANGE_DAYS
        ):
            LOGGER.debug("Rejected calendar range request; reason=span")
            return self._json({"error": "invalid range"}, status=400)

        # calendar_entries is synchronous SQLite plus a bounded but non-trivial
        # recurrence projection, and this server shares the Discord client's
        # event loop. Running it inline stalls the gateway, the signup buttons
        # and the event scheduler for the length of the query, so hand it to a
        # worker thread. The store's SQLite connections are pooled per thread,
        # so it is safe to touch from one.
        entries = await asyncio.to_thread(
            calendar_entries,
            self._bot.event_store,
            self._bot.event_timezone,
            range_start,
            range_end,
            datetime.now(UTC),
        )
        names = await self._display_names(
            {entry.leader_discord_id for entry in entries}
        )
        payload = [self._serialize_entry(entry, names) for entry in entries]
        LOGGER.debug(
            "Served calendar range; days=%s entries=%s projected=%s",
            (range_end - range_start).days,
            len(payload),
            sum(1 for entry in entries if entry.projected),
        )
        return self._json({"entries": payload})

    def _serialize_entry(
        self,
        entry: CalendarEntry,
        names: dict[int, str],
    ) -> dict[str, object]:
        return {
            "event_id": entry.event_id,
            "occurrence_id": entry.occurrence_id,
            "title": entry.title,
            "category": entry.category,
            "description": entry.description,
            "start_epoch": entry.start_epoch,
            "duration_minutes": entry.duration_minutes,
            "leader_name": names.get(entry.leader_discord_id, UNKNOWN_NAME),
            "status": entry.status,
            "projected": entry.projected,
            "active_count": entry.active_count,
            "waitlist_count": entry.waitlist_count,
            "healers": entry.healers,
            "dps": entry.dps,
            "quickness": entry.quickness,
            "alacrity": entry.alacrity,
            "capacity_total": entry.capacity_total,
            "has_roles": entry.has_roles,
        }

    async def _display_names(self, user_ids: set[int]) -> dict[int, str]:
        """Resolve every leader name for one response.

        Cache misses are resolved concurrently so a cold cache costs one
        round trip rather than one per leader in series.
        """
        now = time.monotonic()
        resolved: dict[int, str] = {}
        missing: list[int] = []
        for user_id in user_ids:
            cached = self._names.get(user_id)
            if (
                cached is not None
                and now - cached[1] < NAME_CACHE_TTL_SECONDS
            ):
                resolved[user_id] = cached[0]
            else:
                missing.append(user_id)
        if not missing:
            return resolved
        names = await asyncio.gather(
            *(self._resolve_display_name(user_id) for user_id in missing)
        )
        for user_id, name in zip(missing, names, strict=True):
            if name is None:
                # A failed lookup is never cached, so one transient Discord
                # error cannot pin a leader to "Unknown" for the whole TTL.
                resolved[user_id] = UNKNOWN_NAME
                continue
            self._names[user_id] = (name, time.monotonic())
            resolved[user_id] = name
        LOGGER.debug(
            "Resolved leader display names; cached=%s fetched=%s",
            len(resolved) - len(missing),
            len(missing),
        )
        return resolved

    async def _resolve_display_name(self, user_id: int) -> str | None:
        """Return the display name, or None when Discord cannot be reached."""
        return await resolve_display_name(
            self._bot,
            self._bot.get_guild(self._config.discord_command_guild_id),
            user_id,
        )
