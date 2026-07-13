from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Awaitable, Callable

import aiohttp
import discord
from aiohttp import web

from gw2bot.config import Config
from gw2bot.web import auth
from gw2bot.web.calendar import CalendarEntry, calendar_entries
from gw2bot.web.page import (
    CALENDAR_PAGE,
    LOGIN_FAILED_PAGE,
    MEMBERS_ONLY_PAGE,
    SERVICE_UNAVAILABLE_PAGE,
    SIGN_IN_PAGE,
    SIGNED_OUT_PAGE,
)

if TYPE_CHECKING:
    from gw2bot.bot import Gw2Bot

LOGGER = logging.getLogger(__name__)

MAX_RANGE_DAYS = 62
NAME_CACHE_TTL_SECONDS = 3600

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

# Every response this server sends is scoped to one signed-in member, so none
# of it may be kept by the reverse proxy the README asks operators to run, by a
# shared cache, or by the browser's back/forward cache.
NO_STORE = "no-store, private"

# Paths reachable without a session; everything else is members-only.
PUBLIC_PATHS = frozenset(
    {"/login", "/oauth/callback", "/logout", "/favicon.ico"}
)

_Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]

SESSION_KEY = web.RequestKey("web_session", auth.SessionData)


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
            return self._html(SIGN_IN_PAGE, status=401)
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
        state, cookie = auth.sign_state(
            self._session_secret,
            datetime.now(UTC),
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
        LOGGER.debug("Redirecting web login to Discord authorization")
        return response

    async def _callback(self, request: web.Request) -> web.StreamResponse:
        code = request.query.get("code", "")
        state = request.query.get("state", "")
        state_cookie = request.cookies.get(auth.STATE_COOKIE, "")
        now = datetime.now(UTC)
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
        response = _redirect("/")
        self._set_cookie(
            response,
            auth.SESSION_COOKIE,
            session_value,
            self._session_ttl,
        )
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

    async def _logout(self, request: web.Request) -> web.StreamResponse:
        response = self._html(SIGNED_OUT_PAGE)
        response.del_cookie(auth.SESSION_COOKIE, path="/")
        LOGGER.debug("Cleared web session cookie on logout")
        return response

    async def _me(self, request: web.Request) -> web.StreamResponse:
        session = request[SESSION_KEY]
        return self._json({"name": session.name})

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
        guild = self._bot.get_guild(self._config.discord_command_guild_id)
        if guild is not None:
            member = guild.get_member(user_id)
            if member is not None:
                return member.display_name
            try:
                member = await guild.fetch_member(user_id)
            except discord.HTTPException:
                member = None
            if member is not None:
                return member.display_name
        try:
            user = await self._bot.fetch_user(user_id)
        except discord.HTTPException as exc:
            LOGGER.debug(
                "Display name lookup failed; user_id=%s error_type=%s",
                user_id,
                type(exc).__name__,
            )
            return None
        return user.display_name
