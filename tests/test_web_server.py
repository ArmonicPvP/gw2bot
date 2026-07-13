from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import aiohttp
import pytest
from aiohttp.test_utils import TestClient, TestServer

from factories import forbidden_error, not_found_error
from gw2bot.bot import Gw2Bot
from gw2bot.config import Config
from gw2bot.events.models import EventCategory, RepeatFrequency
from gw2bot.events.store import EventStore
from gw2bot.web import auth
from gw2bot.web import server as server_module
from gw2bot.web.server import WebServer

from unittest.mock import AsyncMock, MagicMock

GUILD_ID = 5678
CLIENT_SECRET = "client-secret-value"
SESSION_SECRET = "session-secret-value-0123456789abcdef"
SESSION_USER_ID = 1


def make_config() -> Config:
    return Config.from_env(
        {
            "DISCORD_TOKEN": "discord-token",
            "DISCORD_COMMAND_GUILD_ID": str(GUILD_ID),
            "DISCORD_NOTIFICATION_CHANNEL_ID": "9012",
            "GW2_API_KEY": "gw2-key",
            "GW2_GUILD_ID": "guild-id",
            "WEB_ENABLED": "true",
            "WEB_BASE_URL": "http://localhost:8080",
            "DISCORD_OAUTH_CLIENT_ID": "client-id",
            "DISCORD_OAUTH_CLIENT_SECRET": CLIENT_SECRET,
            "WEB_SESSION_SECRET": SESSION_SECRET,
        }
    )


class FakeGuild:
    def __init__(self):
        self.members: dict[int, object] = {}
        self.fetch_member = AsyncMock(side_effect=not_found_error())

    def get_member(self, user_id: int) -> object | None:
        return self.members.get(user_id)


class FakeBot:
    def __init__(self, store: EventStore, guild: FakeGuild | None):
        self.event_store = store
        self.event_timezone = ZoneInfo("UTC")
        self._guild = guild
        self.fetch_user = AsyncMock(side_effect=not_found_error())

    def get_guild(self, guild_id: int) -> FakeGuild | None:
        assert guild_id == GUILD_ID
        return self._guild


@pytest.fixture
def store(tmp_path: Path):
    store = EventStore(str(tmp_path / "gw2bot.db"))
    yield store
    store.close()


@pytest.fixture
def guild() -> FakeGuild:
    guild = FakeGuild()
    # The holder of the default session cookie is a current guild member;
    # every request re-checks that, not just the sign-in.
    guild.members[SESSION_USER_ID] = SimpleNamespace(display_name="Kitty")
    return guild


@pytest.fixture
def bot(store: EventStore, guild: FakeGuild) -> FakeBot:
    return FakeBot(store, guild)


@pytest.fixture
async def client(bot: FakeBot):
    server = WebServer(
        cast(Gw2Bot, bot),
        make_config(),
        cast(aiohttp.ClientSession, None),
    )
    test_client = TestClient(TestServer(server.app))
    await test_client.start_server()
    yield test_client
    await test_client.close()


def session_cookie(user_id: int = SESSION_USER_ID, name: str = "Kitty") -> str:
    return auth.sign_session(
        SESSION_SECRET,
        user_id,
        name,
        datetime.now(UTC) + timedelta(days=1),
    )


async def begin_login(client: TestClient) -> str:
    """Start the OAuth flow and return the state Discord would echo back."""
    response = await client.get("/login", allow_redirects=False)
    assert response.status == 302
    location = response.headers["Location"]
    assert location.startswith("https://discord.com/oauth2/authorize?")
    query = parse_qs(urlsplit(location).query)
    assert query["scope"] == ["identify"]
    return query["state"][0]


class TestAuthGate:
    async def test_unauthenticated_page_shows_sign_in(
        self,
        client: TestClient,
    ) -> None:
        response = await client.get("/")

        assert response.status == 401
        assert "Sign in with Discord" in await response.text()

    async def test_unauthenticated_api_returns_json_401(
        self,
        client: TestClient,
    ) -> None:
        response = await client.get(
            "/api/events",
            params={"start": "0", "end": "60"},
        )

        assert response.status == 401
        assert await response.json() == {"error": "unauthorized"}

    async def test_valid_session_reaches_calendar_page(
        self,
        client: TestClient,
    ) -> None:
        response = await client.get(
            "/",
            headers={"Cookie": f"{auth.SESSION_COOKIE}={session_cookie()}"},
        )

        assert response.status == 200
        assert "Guild Events" in await response.text()

    async def test_logout_clears_session_cookie(
        self,
        client: TestClient,
    ) -> None:
        response = await client.post(
            "/logout",
            headers={"Cookie": f"{auth.SESSION_COOKIE}={session_cookie()}"},
        )

        assert response.status == 200
        cleared = response.cookies[auth.SESSION_COOKIE]
        assert cleared.value == ""

    async def test_logout_rejects_get(self, client: TestClient) -> None:
        # A GET sign-out is a CSRF any third-party page could fire with an
        # <img> tag, so the route must not answer one.
        response = await client.get(
            "/logout",
            headers={"Cookie": f"{auth.SESSION_COOKIE}={session_cookie()}"},
        )

        assert response.status == 405
        assert auth.SESSION_COOKIE not in response.cookies

    @pytest.mark.parametrize(
        ("method", "path", "params"),
        [
            ("get", "/", {}),
            ("get", "/api/me", {}),
            (
                "get",
                "/api/events",
                {
                    "start": str(
                        int(datetime(2027, 1, 1, tzinfo=UTC).timestamp())
                    ),
                    "end": str(
                        int(datetime(2027, 2, 1, tzinfo=UTC).timestamp())
                    ),
                },
            ),
        ],
    )
    async def test_member_responses_are_never_cached(
        self,
        client: TestClient,
        method: str,
        path: str,
        params: dict[str, str],
    ) -> None:
        # Every response is scoped to one signed-in member. The README puts a
        # reverse proxy in front of this, so a cacheable /api/me would hand one
        # member's name to the next visitor on the same edge.
        response = await getattr(client, method)(
            path,
            params=params,
            headers={"Cookie": f"{auth.SESSION_COOKIE}={session_cookie()}"},
        )

        assert response.status == 200
        assert "no-store" in response.headers["Cache-Control"]

    async def test_unauthenticated_responses_are_never_cached(
        self,
        client: TestClient,
    ) -> None:
        assert "no-store" in (
            await client.get("/")
        ).headers["Cache-Control"]
        assert "no-store" in (
            await client.get("/login", allow_redirects=False)
        ).headers["Cache-Control"]

    async def test_departed_member_session_is_revoked(
        self,
        client: TestClient,
        guild: FakeGuild,
    ) -> None:
        # The signature and expiry are still valid, but the holder has left
        # or been banned, so an unexpired cookie must not keep them in.
        guild.members.clear()

        response = await client.get(
            "/",
            headers={"Cookie": f"{auth.SESSION_COOKIE}={session_cookie()}"},
        )

        assert response.status == 403
        assert "Members only" in await response.text()
        assert response.cookies[auth.SESSION_COOKIE].value == ""

    async def test_departed_member_api_returns_json_403(
        self,
        client: TestClient,
        guild: FakeGuild,
    ) -> None:
        guild.members.clear()

        response = await client.get(
            "/api/events",
            params={"start": "0", "end": "60"},
            headers={"Cookie": f"{auth.SESSION_COOKIE}={session_cookie()}"},
        )

        assert response.status == 403
        assert await response.json() == {"error": "forbidden"}

    async def test_membership_is_cached_between_requests(
        self,
        client: TestClient,
        guild: FakeGuild,
    ) -> None:
        headers = {"Cookie": f"{auth.SESSION_COOKIE}={session_cookie()}"}
        assert (await client.get("/", headers=headers)).status == 200

        # Membership was cached on the first request, so a departure that
        # Discord has not yet been re-polled for does not cost a lookup.
        guild.members.clear()

        assert (await client.get("/", headers=headers)).status == 200
        guild.fetch_member.assert_not_awaited()

    async def test_stale_membership_backs_off_while_discord_is_down(
        self,
        client: TestClient,
        guild: FakeGuild,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The bot runs without the members intent, so every membership check
        # that misses the cache is a fetch_member call against a rate-limited
        # endpoint. A failed lookup must still re-arm the cache entry, or a
        # Discord outage turns every single request into another one.
        monkeypatch.setattr(server_module, "MEMBERSHIP_CACHE_TTL_SECONDS", -1)
        headers = {"Cookie": f"{auth.SESSION_COOKIE}={session_cookie()}"}
        assert (await client.get("/", headers=headers)).status == 200
        assert guild.fetch_member.await_count == 0

        # Discord starts failing. The cached "yes" is already stale, so the
        # next request pays one lookup, gets nothing, and serves the stale
        # answer rather than signing the member out.
        guild.members.clear()
        guild.fetch_member.side_effect = forbidden_error(50001)

        assert (await client.get("/", headers=headers)).status == 200
        assert guild.fetch_member.await_count == 1

        # The failure re-armed the entry for the backoff window, so further
        # requests ride the stale answer instead of hammering Discord.
        assert (await client.get("/", headers=headers)).status == 200
        assert (await client.get("/", headers=headers)).status == 200
        assert guild.fetch_member.await_count == 1

    async def test_unreachable_discord_does_not_lock_out_members(
        self,
        store: EventStore,
    ) -> None:
        # An unknown membership state is not evidence the user left, so an
        # outage must not sign every member out of a read-only calendar.
        server = WebServer(
            cast(Gw2Bot, FakeBot(store, None)),
            make_config(),
            cast(aiohttp.ClientSession, None),
        )
        test_client = TestClient(TestServer(server.app))
        await test_client.start_server()
        try:
            response = await test_client.get(
                "/",
                headers={
                    "Cookie": f"{auth.SESSION_COOKIE}={session_cookie()}"
                },
            )
        finally:
            await test_client.close()

        assert response.status == 200


class TestServerLifecycle:
    async def test_failed_bind_releases_the_runner(
        self,
        bot: FakeBot,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # setup() has already allocated the runner's server infrastructure by
        # the time the bind fails, and stop() keys off _runner, so the runner
        # has to be released here or it leaks for the life of the process.
        server = WebServer(
            cast(Gw2Bot, bot),
            make_config(),
            cast(aiohttp.ClientSession, None),
        )
        runner = MagicMock()
        runner.setup = AsyncMock()
        runner.cleanup = AsyncMock()
        site = MagicMock()
        site.start = AsyncMock(side_effect=OSError("address already in use"))
        monkeypatch.setattr(
            server_module.web,
            "AppRunner",
            MagicMock(return_value=runner),
        )
        monkeypatch.setattr(
            server_module.web,
            "TCPSite",
            MagicMock(return_value=site),
        )

        with pytest.raises(OSError):
            await server.start()

        runner.cleanup.assert_awaited_once()

        # stop() must stay a no-op rather than cleaning up an already-released
        # runner a second time.
        await server.stop()

        runner.cleanup.assert_awaited_once()


class TestOAuthCallback:
    async def test_rejects_mismatched_state(
        self,
        client: TestClient,
    ) -> None:
        await begin_login(client)

        response = await client.get(
            "/oauth/callback",
            params={"code": "the-code", "state": "wrong-state"},
            allow_redirects=False,
        )

        assert response.status == 403
        assert auth.SESSION_COOKIE not in response.cookies
        # Every terminal path clears the consumed state cookie, so a failed
        # attempt does not leave one behind for its full TTL.
        assert response.cookies[auth.STATE_COOKIE].value == ""

    async def test_rejects_missing_state_cookie(
        self,
        client: TestClient,
    ) -> None:
        response = await client.get(
            "/oauth/callback",
            params={"code": "the-code", "state": "any-state"},
            allow_redirects=False,
        )

        assert response.status == 403

    @pytest.mark.parametrize(
        "state",
        ["é", "стейт", "🙂", "état-mixed-ascii"],
    )
    async def test_rejects_non_ascii_state_without_erroring(
        self,
        client: TestClient,
        state: str,
    ) -> None:
        # The state is echoed straight out of the query string, so it can hold
        # any code point. hmac.compare_digest raises TypeError on a non-ASCII
        # str, which would surface as a 500 rather than a rejected sign-in.
        await begin_login(client)

        response = await client.get(
            "/oauth/callback",
            params={"code": "the-code", "state": state},
            allow_redirects=False,
        )

        assert response.status == 403
        assert auth.SESSION_COOKIE not in response.cookies

    async def test_member_login_sets_session_cookie(
        self,
        client: TestClient,
        guild: FakeGuild,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        state = await begin_login(client)
        monkeypatch.setattr(
            auth,
            "exchange_code",
            AsyncMock(return_value="the-access-token"),
        )
        monkeypatch.setattr(
            auth,
            "fetch_identity",
            AsyncMock(
                return_value=auth.DiscordIdentity(user_id=77, name="Kitty")
            ),
        )
        guild.fetch_member = AsyncMock(
            return_value=SimpleNamespace(display_name="Kitty")
        )

        with caplog.at_level("DEBUG"):
            response = await client.get(
                "/oauth/callback",
                params={"code": "secret-oauth-code", "state": state},
                allow_redirects=False,
            )

        assert response.status == 302
        assert response.headers["Location"] == "/"
        cookie = response.cookies[auth.SESSION_COOKIE]
        assert cookie["httponly"]
        assert cookie["samesite"] == "Lax"
        session = auth.verify_session(
            SESSION_SECRET,
            cookie.value,
            datetime.now(UTC),
        )
        assert session is not None
        assert session.user_id == 77
        # Credential-safe logging: no OAuth code or secret may reach logs.
        assert "secret-oauth-code" not in caplog.text
        assert "the-access-token" not in caplog.text
        assert CLIENT_SECRET not in caplog.text
        assert SESSION_SECRET not in caplog.text

        me = await client.get("/api/me")
        assert me.status == 200
        assert await me.json() == {"name": "Kitty"}

    async def test_non_member_gets_members_only_page(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        state = await begin_login(client)
        monkeypatch.setattr(
            auth,
            "exchange_code",
            AsyncMock(return_value="the-access-token"),
        )
        monkeypatch.setattr(
            auth,
            "fetch_identity",
            AsyncMock(
                return_value=auth.DiscordIdentity(user_id=88, name="Nope")
            ),
        )

        response = await client.get(
            "/oauth/callback",
            params={"code": "the-code", "state": state},
            allow_redirects=False,
        )

        assert response.status == 403
        assert "Members only" in await response.text()
        assert auth.SESSION_COOKIE not in response.cookies

    async def test_failed_token_exchange_returns_502(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        state = await begin_login(client)
        monkeypatch.setattr(
            auth,
            "exchange_code",
            AsyncMock(side_effect=auth.OAuthExchangeError("status 400")),
        )

        response = await client.get(
            "/oauth/callback",
            params={"code": "the-code", "state": state},
            allow_redirects=False,
        )

        assert response.status == 502


class TestEventsApi:
    async def test_returns_entries_with_leader_name(
        self,
        client: TestClient,
        store: EventStore,
        guild: FakeGuild,
    ) -> None:
        guild.members[42] = SimpleNamespace(display_name="Leader Kitty")
        event = store.create_event(
            category=EventCategory.RAID,
            title="Weekly Raid",
            description="Bring snacks.",
            channel_id=1,
            leader_discord_id=42,
            start_time=datetime(2027, 1, 30, 20, 0, tzinfo=UTC),
            duration_minutes=90,
            repeat_frequency=RepeatFrequency.NONE,
            repeat_days=(),
        )
        store.create_occurrence(
            event.event_id,
            datetime(2027, 1, 30, 20, 0, tzinfo=UTC),
        )

        response = await client.get(
            "/api/events",
            params={
                "start": str(
                    int(datetime(2027, 1, 1, tzinfo=UTC).timestamp())
                ),
                "end": str(
                    int(datetime(2027, 2, 1, tzinfo=UTC).timestamp())
                ),
            },
            headers={"Cookie": f"{auth.SESSION_COOKIE}={session_cookie()}"},
        )

        assert response.status == 200
        payload = await response.json()
        assert len(payload["entries"]) == 1
        entry = payload["entries"][0]
        assert entry["title"] == "Weekly Raid"
        assert entry["leader_name"] == "Leader Kitty"
        # Snowflakes lose precision as JSON numbers, so raw Discord ids
        # must never be shipped.
        assert "leader_discord_id" not in entry

    async def test_failed_leader_lookup_is_not_cached(
        self,
        client: TestClient,
        store: EventStore,
        guild: FakeGuild,
    ) -> None:
        event = store.create_event(
            category=EventCategory.RAID,
            title="Weekly Raid",
            description="Bring snacks.",
            channel_id=1,
            leader_discord_id=42,
            start_time=datetime(2027, 1, 30, 20, 0, tzinfo=UTC),
            duration_minutes=90,
            repeat_frequency=RepeatFrequency.NONE,
            repeat_days=(),
        )
        store.create_occurrence(
            event.event_id,
            datetime(2027, 1, 30, 20, 0, tzinfo=UTC),
        )
        params = {
            "start": str(int(datetime(2027, 1, 1, tzinfo=UTC).timestamp())),
            "end": str(int(datetime(2027, 2, 1, tzinfo=UTC).timestamp())),
        }
        headers = {"Cookie": f"{auth.SESSION_COOKIE}={session_cookie()}"}

        # Discord cannot be reached for the leader, so the entry falls back
        # to "Unknown".
        first = await client.get("/api/events", params=params, headers=headers)
        assert first.status == 200
        payload = await first.json()
        assert payload["entries"][0]["leader_name"] == "Unknown"

        # Discord recovers. The failed lookup must not have been cached, or
        # the leader would stay "Unknown" for the whole cache TTL.
        guild.members[42] = SimpleNamespace(display_name="Leader Kitty")

        second = await client.get(
            "/api/events",
            params=params,
            headers=headers,
        )
        assert second.status == 200
        payload = await second.json()
        assert payload["entries"][0]["leader_name"] == "Leader Kitty"

    @pytest.mark.parametrize(
        "params",
        [
            {},
            {"start": "abc", "end": "123"},
            {"start": "100", "end": "100"},
            {"start": "200", "end": "100"},
            {"start": "0", "end": str(90 * 24 * 60 * 60)},
        ],
    )
    async def test_rejects_invalid_ranges(
        self,
        client: TestClient,
        params: dict[str, str],
    ) -> None:
        response = await client.get(
            "/api/events",
            params=params,
            headers={"Cookie": f"{auth.SESSION_COOKIE}={session_cookie()}"},
        )

        assert response.status == 400
