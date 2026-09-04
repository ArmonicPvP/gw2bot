import base64
import json
import logging
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from factories import config_from_env, forbidden_error, not_found_error
from gw2bot.bot import Gw2Bot
from gw2bot.config import Config
from gw2bot.events.models import EventCategory, RepeatFrequency
from gw2bot.events.store import EventStore
from gw2bot.raffle import RaffleStore
from gw2bot.web import auth
from gw2bot.web import server as server_module
from gw2bot.config import DEFAULT_RAFFLE_DRAW_ROLE_ID as FOOD_PAGE_ROLE_ID
from gw2bot.gold import DEPOSIT, WITHDRAW, GoldLedgerEntry
from gw2bot.profit import (
    DayProfit,
    DeliveryItem,
    DeliveryReport,
    ItemProfit,
    MarketPrice,
    OpenBuyOrder,
    OpenOrdersReport,
    ProfitReport,
    RealizedProfit,
    UnrealizedItemProfit,
    UnrealizedProfit,
)
from gw2bot.profit.api import ProfitApiError
from gw2bot.profit.service import MissingProfitApiKey, ReportWindow
from gw2bot.guild_members import TrialMemberReportEntry
from gw2bot.pending_invites import PendingInvites
from gw2bot.roster import JOIN, KICK, LEAVE, ImportedMembershipEvent
from sqlalchemy.exc import SQLAlchemyError
from gw2bot.web.server import WebServer

from unittest.mock import AsyncMock, MagicMock

GUILD_ID = 5678
CLIENT_SECRET = "client-secret-value"
SESSION_SECRET = "session-secret-value-0123456789abcdef"
SESSION_USER_ID = 1


def profit_report(days: int = 30) -> ProfitReport:
    window_end = datetime(2026, 8, 21, 18, 30, tzinfo=UTC)
    return ProfitReport(
        days=days,
        window_start=(
            window_end.replace(hour=0, minute=0) - timedelta(days=days - 1)
        ),
        window_end=window_end,
        buy_transaction_count=4,
        sell_transaction_count=2,
        realized=RealizedProfit(
            items={1: ItemProfit(2, 200, 340, 140, 86_400)},
            days={"2026-08-20": DayProfit(2, 200, 340, 140)},
            unmatched_buys={},
            total_cost=200,
            total_net_revenue=340,
            total_profit=140,
            total_matched_quantity=2,
        ),
        unrealized=UnrealizedProfit(
            items={2: UnrealizedItemProfit(1, 100, 255, 155)},
            total_quantity=1,
            total_cost=100,
            total_projected_net_revenue=255,
            total_projected_profit=155,
        ),
        item_names={1: "Realized Item", 2: "Listed Item"},
        market_prices={1: MarketPrice(100, 200)},
        history_start=datetime(2026, 5, 1, tzinfo=UTC),
    )


def delivery_report() -> DeliveryReport:
    return DeliveryReport(
        coins=12_345,
        items=(DeliveryItem(3, 7),),
        item_names={3: "Delivered Item"},
    )


def orders_report() -> OpenOrdersReport:
    return OpenOrdersReport(
        orders=(
            OpenBuyOrder(
                item_id=4,
                unit_price=70,
                quantity=10,
                order_count=2,
                placed_at=datetime(2026, 8, 21, 18, 30, tzinfo=UTC),
            ),
        ),
        available=True,
        excluded_items=frozenset({5}),
        market_prices={4: MarketPrice(80, 160)},
        item_names={4: "Ordered Item", 5: "Excluded Item"},
    )


def member(display_name: str = "Kitty", *, officer: bool = False) -> object:
    roles = [SimpleNamespace(id=FOOD_PAGE_ROLE_ID)] if officer else []
    return SimpleNamespace(display_name=display_name, roles=roles)


def make_config() -> Config:
    return config_from_env(
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


def make_config_without_gw2() -> Config:
    values = {
        key: value
        for key, value in {
            "DISCORD_TOKEN": "discord-token",
            "DISCORD_COMMAND_GUILD_ID": str(GUILD_ID),
            "DISCORD_NOTIFICATION_CHANNEL_ID": "9012",
            "WEB_ENABLED": "true",
            "WEB_BASE_URL": "http://localhost:8080",
            "DISCORD_OAUTH_CLIENT_ID": "client-id",
            "DISCORD_OAUTH_CLIENT_SECRET": CLIENT_SECRET,
            "WEB_SESSION_SECRET": SESSION_SECRET,
        }.items()
    }
    return config_from_env(values)


class FakeGuild:
    def __init__(self):
        self.members: dict[int, object] = {}
        self.fetch_member = AsyncMock(side_effect=not_found_error())

    def get_member(self, user_id: int) -> object | None:
        return self.members.get(user_id)


class FakeBot:
    def __init__(
        self,
        store: EventStore,
        guild: FakeGuild | None,
        raffle_store: RaffleStore | None = None,
    ):
        self.event_store = store
        self.event_timezone = ZoneInfo("UTC")
        self.raffle_store = raffle_store
        self.profit_service = SimpleNamespace(
            load_report=AsyncMock(return_value=profit_report()),
            load_delivery=AsyncMock(return_value=delivery_report()),
            load_open_orders=AsyncMock(return_value=orders_report()),
            resolve_report_days=AsyncMock(
                side_effect=lambda user_id, days: ReportWindow(
                    30 if days is None else days, days is not None
                )
            ),
            set_order_exclusion=AsyncMock(return_value=True),
        )
        self._guild = guild
        self.fetch_user = AsyncMock(side_effect=not_found_error())
        # The roster page's pending-invite section needs the GW2 API, and the
        # bot answers for it the way every other optional feature does.
        self.gw2_api_enabled = True
        self.build_pending_invite_entries = AsyncMock(
            return_value=PendingInvites([], True)
        )
        self._config = make_config()

    def get_guild(self, guild_id: int) -> FakeGuild | None:
        assert guild_id == GUILD_ID
        return self._guild


@pytest.fixture
def store(tmp_path: Path):
    store = EventStore(str(tmp_path / "gw2bot.db"))
    yield store
    store.close()


@pytest.fixture
def raffle_store(tmp_path: Path):
    store = RaffleStore(str(tmp_path / "raffle.db"), "guild-id")
    yield store
    store.close()


@pytest.fixture
def guild() -> FakeGuild:
    guild = FakeGuild()
    # The holder of the default session cookie is a current guild member;
    # every request re-checks that, not just the sign-in.
    guild.members[SESSION_USER_ID] = member("Kitty")
    return guild


@pytest.fixture
def bot(
    store: EventStore,
    guild: FakeGuild,
    raffle_store: RaffleStore,
) -> FakeBot:
    return FakeBot(store, guild, raffle_store)


async def quiet_test_server(app: web.Application) -> TestServer:
    """A test server whose runner is built the way ``WebServer.start`` builds
    its own.

    aiohttp's access log prints whole request targets, so every query string -
    an OAuth code, a custom window's bounds - would reach the console through
    it. ``WebServer.start`` passes ``access_log=None`` for exactly that
    reason, but the test server builds its own runner and only its
    ``start_server`` forwards runner keywords, so it is started here rather
    than by the client. Without this the credential-leak assertions below
    would be reading the harness's log instead of the bot's.
    """
    test_server = TestServer(app)
    await test_server.start_server(access_log=None)
    return test_server


@pytest.fixture
def web_server(bot: FakeBot) -> WebServer:
    return WebServer(
        cast(Gw2Bot, bot),
        make_config(),
        cast(aiohttp.ClientSession, None),
    )


@pytest.fixture
async def client(web_server: WebServer):
    test_client = TestClient(await quiet_test_server(web_server.app))
    yield test_client
    await test_client.close()


def session_cookie(user_id: int = SESSION_USER_ID, name: str = "Kitty") -> str:
    return auth.sign_session(
        SESSION_SECRET,
        user_id,
        name,
        datetime.now(UTC) + timedelta(days=1),
    )


async def begin_login(
    client: TestClient,
    return_to: str | None = None,
) -> str:
    """Start the OAuth flow and return the state Discord would echo back."""
    response = await client.get(
        "/login",
        params={} if return_to is None else {"next": return_to},
        allow_redirects=False,
    )
    assert response.status == 302
    location = response.headers["Location"]
    assert location.startswith("https://discord.com/oauth2/authorize?")
    query = parse_qs(urlsplit(location).query)
    assert query["scope"] == ["identify"]
    assert query["prompt"] == ["none"]
    return query["state"][0]


def state_token(state_cookie: str) -> str:
    """Read the opaque state token out of a signed state cookie.

    The retry echoes back a fresh state, so a test that follows the retry needs
    the token that pairs with the new cookie rather than the original one.
    """
    payload_b64 = state_cookie.split(".")[0]
    padding = "=" * (-len(payload_b64) % 4)
    raw = base64.urlsafe_b64decode(payload_b64 + padding)
    return json.loads(raw)["state"]


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
        test_client = TestClient(await quiet_test_server(server.app))
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
        return_target_secret = "return-target-secret"
        return_to = f"/profit?days=60&marker={return_target_secret}"
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
            state = await begin_login(client, return_to)
            response = await client.get(
                "/oauth/callback",
                params={"code": "secret-oauth-code", "state": state},
                allow_redirects=False,
            )

        assert response.status == 302
        assert response.headers["Location"] == return_to
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
        assert return_target_secret not in caplog.text

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


class TestSilentAuthorizationRetry:
    async def test_consent_required_retries_with_a_visible_prompt(
        self,
        client: TestClient,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # A first-time user is refused by the silent (prompt=none) attempt;
        # the callback bounces them back to Discord with the prompt enabled.
        state = await begin_login(client, "/profit?days=60")

        with caplog.at_level("INFO"):
            response = await client.get(
                "/oauth/callback",
                params={"error": "consent_required", "state": state},
                allow_redirects=False,
            )

        assert response.status == 302
        location = response.headers["Location"]
        assert location.startswith("https://discord.com/oauth2/authorize?")
        query = parse_qs(urlsplit(location).query)
        assert "prompt" not in query
        # The retry rides a fresh, retry-marked state cookie.
        retry_cookie = response.cookies[auth.STATE_COOKIE].value
        assert auth.state_is_consent_retry(SESSION_SECRET, retry_cookie)
        assert auth.state_return_target(
            SESSION_SECRET,
            retry_cookie,
        ) == "/profit?days=60"
        assert query["state"] == [
            state_token(retry_cookie),
        ]
        assert "consent_required" in caplog.text

    async def test_retry_does_not_loop_a_second_time(
        self,
        client: TestClient,
    ) -> None:
        # Follow the retry through: a consent error carrying the retry-marked
        # state must end on the failure page, never another redirect.
        state = await begin_login(client)
        first = await client.get(
            "/oauth/callback",
            params={"error": "consent_required", "state": state},
            allow_redirects=False,
        )
        retry_cookie = first.cookies[auth.STATE_COOKIE].value

        second = await client.get(
            "/oauth/callback",
            params={
                "error": "consent_required",
                "state": state_token(retry_cookie),
            },
            allow_redirects=False,
        )

        assert second.status == 403
        assert "Sign-in failed" in await second.text()
        assert second.cookies[auth.STATE_COOKIE].value == ""

    async def test_access_denied_is_not_retried(
        self,
        client: TestClient,
    ) -> None:
        # Declining the consent screen is a deliberate choice, not something a
        # further prompt would fix.
        state = await begin_login(client)

        response = await client.get(
            "/oauth/callback",
            params={"error": "access_denied", "state": state},
            allow_redirects=False,
        )

        assert response.status == 403
        assert "Sign-in failed" in await response.text()

    async def test_error_without_a_valid_state_is_not_retried(
        self,
        client: TestClient,
    ) -> None:
        # A crafted error with no matching state cookie must not be able to
        # bounce a visitor onward to Discord.
        await begin_login(client)

        response = await client.get(
            "/oauth/callback",
            params={"error": "consent_required", "state": "wrong-state"},
            allow_redirects=False,
        )

        assert response.status == 403


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


class TestProfitPage:
    @staticmethod
    def _headers(user_id: int = SESSION_USER_ID) -> dict[str, str]:
        return {
            "Cookie": (
                f"{auth.SESSION_COOKIE}="
                f"{session_cookie(user_id=user_id)}"
            )
        }

    async def test_unauthenticated_page_shows_sign_in(
        self,
        client: TestClient,
    ) -> None:
        response = await client.get("/profit")

        assert response.status == 401
        assert "Sign in with Discord" in await response.text()

    async def test_profit_sign_in_link_preserves_its_window(
        self,
        client: TestClient,
    ) -> None:
        response = await client.get("/profit", params={"days": "60"})

        assert response.status == 401
        assert (
            'href="/login?next=%2Fprofit%3Fdays%3D60"'
            in await response.text()
        )

    async def test_login_rejects_an_external_return_target(
        self,
        client: TestClient,
    ) -> None:
        response = await client.get(
            "/login",
            params={"next": "https://evil.example/profit"},
            allow_redirects=False,
        )

        state_cookie = response.cookies[auth.STATE_COOKIE].value
        assert auth.state_return_target(SESSION_SECRET, state_cookie) == "/"

    async def test_member_reaches_the_combined_profit_page(
        self,
        client: TestClient,
    ) -> None:
        response = await client.get("/profit", headers=self._headers())

        assert response.status == 200
        page = await response.text()
        assert "Trading Post Profit" in page
        assert "Summary" in page
        assert "Your Picks" in page
        assert "Unrealized projected profit" not in page
        assert "Realized Profit by Item" in page
        assert "Realized Profit by Day" in page
        assert "Unrealized Profit" in page
        assert "Open Orders" in page
        assert "Unclaimed Trading Post" in page

    async def test_api_loads_only_the_signed_in_members_report(
        self,
        client: TestClient,
        bot: FakeBot,
        guild: FakeGuild,
    ) -> None:
        other_user_id = 202
        guild.members[other_user_id] = member("Other Kitty")
        bot.profit_service.load_report.return_value = profit_report(60)

        response = await client.get(
            "/api/profit",
            params={"days": "60"},
            headers=self._headers(other_user_id),
        )

        assert response.status == 200
        payload = await response.json()
        assert payload["days"] == 60
        assert payload["summary"]["profit"] == 140
        assert payload["summary"]["roi_percent"] == 70
        assert payload["window"] == {
            "start_date": "2026-06-23",
            "end_date": "2026-08-21",
        }
        assert payload["items"][0]["name"] == "Realized Item"
        assert payload["items"][0]["median_hold_seconds"] == 86_400
        assert payload["items"][0]["profit_share_percent"] == 100
        assert payload["picks"][0]["name"] == "Realized Item"
        assert payload["picks"][0]["profit"] == 70
        assert payload["days_table"][0]["date"] == "2026-08-20"
        assert payload["unrealized"]["items"][0]["name"] == "Listed Item"
        assert payload["unrealized"]["roi_percent"] == 155
        # Delivery and open orders are their own requests now, so the report
        # carries neither and does not wait on either.
        assert "delivery" not in payload
        assert "open_orders" not in payload
        assert payload["history_start_date"] == "2026-05-01"
        assert payload["max_days"] == 3650
        bot.profit_service.load_delivery.assert_not_awaited()
        bot.profit_service.load_open_orders.assert_not_awaited()
        bot.profit_service.resolve_report_days.assert_awaited_once_with(
            other_user_id,
            60,
        )
        bot.profit_service.load_report.assert_awaited_once_with(
            other_user_id,
            60,
            force=False,
        )

    async def test_a_refresh_request_bypasses_the_snapshot(
        self,
        client: TestClient,
        bot: FakeBot,
    ) -> None:
        response = await client.get(
            "/api/profit",
            params={"days": "30", "refresh": "1"},
            headers=self._headers(),
        )

        assert response.status == 200
        bot.profit_service.load_report.assert_awaited_once_with(
            SESSION_USER_ID,
            30,
            force=True,
        )

    async def test_the_price_beat_reads_orders_without_forcing(
        self,
        client: TestClient,
        bot: FakeBot,
    ) -> None:
        await client.get("/api/profit/orders", headers=self._headers())
        await client.get(
            "/api/profit/orders",
            params={"refresh": "1"},
            headers=self._headers(),
        )

        assert [
            call.kwargs["force"]
            for call in bot.profit_service.load_open_orders.await_args_list
        ] == [False, True]

    async def test_delivery_is_served_without_the_history(
        self,
        client: TestClient,
        bot: FakeBot,
    ) -> None:
        response = await client.get(
            "/api/profit/delivery",
            headers=self._headers(),
        )

        assert response.status == 200
        assert await response.json() == {
            "coins": 12_345,
            "items": [{"item_id": 3, "name": "Delivered Item", "quantity": 7}],
        }
        bot.profit_service.load_delivery.assert_awaited_once_with(
            SESSION_USER_ID
        )
        bot.profit_service.load_report.assert_not_awaited()

    async def test_open_orders_are_served_without_the_history(
        self,
        client: TestClient,
        bot: FakeBot,
    ) -> None:
        response = await client.get(
            "/api/profit/orders",
            headers=self._headers(),
        )

        assert response.status == 200
        payload = await response.json()
        assert payload["available"] is True
        assert payload["orders"] == [
            {
                "item_id": 4,
                "name": "Ordered Item",
                "quantity": 10,
                "order_count": 2,
                "unit_price": 70,
                "cost": 700,
                "buy_price": 80,
                "sell_price": 160,
                "net_revenue": 1_360,
                "profit": 66,
                "total_profit": 660,
                "roi_percent": pytest.approx(94.285, rel=1e-3),
                "has_order": True,
            }
        ]
        assert payload["excluded"] == [
            {
                "item_id": 5,
                "name": "Excluded Item",
                "quantity": 0,
                "order_count": 0,
                "unit_price": None,
                "cost": 0,
                "buy_price": None,
                "sell_price": None,
                "net_revenue": None,
                "profit": None,
                "total_profit": None,
                "roi_percent": None,
                "has_order": False,
            }
        ]
        bot.profit_service.load_open_orders.assert_awaited_once_with(
            SESSION_USER_ID,
            force=False,
        )
        bot.profit_service.load_report.assert_not_awaited()

    @pytest.mark.parametrize(
        "path", ["/api/profit/delivery", "/api/profit/orders"]
    )
    async def test_a_section_prompts_for_a_missing_key(
        self,
        client: TestClient,
        bot: FakeBot,
        path: str,
    ) -> None:
        bot.profit_service.load_delivery.side_effect = MissingProfitApiKey
        bot.profit_service.load_open_orders.side_effect = MissingProfitApiKey

        response = await client.get(path, headers=self._headers())

        assert response.status == 409
        assert await response.json() == {"error": "api_key_missing"}

    @pytest.mark.parametrize(
        "path", ["/api/profit/delivery", "/api/profit/orders"]
    )
    async def test_a_section_failure_hides_its_upstream_detail(
        self,
        client: TestClient,
        bot: FakeBot,
        caplog: pytest.LogCaptureFixture,
        path: str,
    ) -> None:
        secret = "section-response-secret"
        bot.profit_service.load_delivery.side_effect = ProfitApiError(secret)
        bot.profit_service.load_open_orders.side_effect = ProfitApiError(
            secret
        )

        with caplog.at_level(logging.DEBUG, logger="gw2bot"):
            response = await client.get(path, headers=self._headers())
        body = await response.text()

        assert response.status == 502
        assert secret not in caplog.text
        assert secret not in body

    @pytest.mark.parametrize(
        "path", ["/api/profit/delivery", "/api/profit/orders"]
    )
    async def test_a_section_needs_a_session(
        self,
        client: TestClient,
        bot: FakeBot,
        path: str,
    ) -> None:
        response = await client.get(path)

        assert response.status == 401
        bot.profit_service.load_delivery.assert_not_awaited()
        bot.profit_service.load_open_orders.assert_not_awaited()

    async def test_api_without_a_window_serves_the_remembered_one(
        self,
        client: TestClient,
        bot: FakeBot,
    ) -> None:
        bot.profit_service.resolve_report_days.side_effect = None
        bot.profit_service.resolve_report_days.return_value = ReportWindow(
            14, True
        )
        bot.profit_service.load_report.return_value = profit_report(14)

        response = await client.get("/api/profit", headers=self._headers())

        assert response.status == 200
        payload = await response.json()
        assert payload["days"] == 14
        assert payload["remembered_days"] is True
        bot.profit_service.resolve_report_days.assert_awaited_once_with(
            SESSION_USER_ID,
            None,
        )
        bot.profit_service.load_report.assert_awaited_once_with(
            SESSION_USER_ID,
            14,
            force=False,
        )

    async def test_exclusion_is_stored_for_the_signed_in_member_only(
        self,
        client: TestClient,
        bot: FakeBot,
        guild: FakeGuild,
    ) -> None:
        other_user_id = 202
        guild.members[other_user_id] = member("Other Kitty")

        response = await client.post(
            "/api/profit/exclusions",
            json={"item_id": 4, "excluded": True},
            headers=self._headers(other_user_id),
        )

        assert response.status == 200
        assert await response.json() == {
            "item_id": 4,
            "excluded": True,
            "changed": True,
        }
        bot.profit_service.set_order_exclusion.assert_awaited_once_with(
            other_user_id,
            4,
            True,
        )

    async def test_exclusion_needs_a_session(
        self,
        client: TestClient,
        bot: FakeBot,
    ) -> None:
        response = await client.post(
            "/api/profit/exclusions",
            json={"item_id": 4, "excluded": True},
        )

        assert response.status == 401
        bot.profit_service.set_order_exclusion.assert_not_awaited()

    @pytest.mark.parametrize(
        "body",
        [
            {"item_id": 4},
            {"excluded": True},
            {"item_id": 0, "excluded": True},
            {"item_id": -3, "excluded": True},
            {"item_id": "4", "excluded": True},
            {"item_id": True, "excluded": True},
            {"item_id": 4, "excluded": "yes"},
            [],
        ],
        ids=(
            "no-excluded",
            "no-item",
            "zero-item",
            "negative-item",
            "string-item",
            "boolean-item",
            "string-excluded",
            "not-an-object",
        ),
    )
    async def test_exclusion_rejects_an_unusable_body_without_storing(
        self,
        client: TestClient,
        bot: FakeBot,
        body: object,
    ) -> None:
        response = await client.post(
            "/api/profit/exclusions",
            json=body,
            headers=self._headers(),
        )

        assert response.status == 400
        assert await response.json() == {"error": "invalid request"}
        bot.profit_service.set_order_exclusion.assert_not_awaited()

    async def test_exclusion_rejects_a_body_that_is_not_json(
        self,
        client: TestClient,
        bot: FakeBot,
    ) -> None:
        response = await client.post(
            "/api/profit/exclusions",
            data="item_id=4",
            headers=self._headers(),
        )

        assert response.status == 400
        bot.profit_service.set_order_exclusion.assert_not_awaited()

    async def test_exclusion_failure_reports_without_its_error_text(
        self,
        client: TestClient,
        bot: FakeBot,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        secret = "exclusion-failure-secret"
        bot.profit_service.set_order_exclusion.side_effect = SQLAlchemyError(
            secret
        )

        with caplog.at_level(logging.DEBUG, logger="gw2bot"):
            response = await client.post(
                "/api/profit/exclusions",
                json={"item_id": 4, "excluded": False},
                headers=self._headers(),
            )
        body = await response.text()

        assert response.status == 500
        assert secret not in caplog.text
        assert secret not in body

    @pytest.mark.parametrize("days", ["0", "3651", "abc", "1.5"])
    async def test_api_rejects_invalid_days_without_loading_data(
        self,
        client: TestClient,
        bot: FakeBot,
        days: str,
    ) -> None:
        response = await client.get(
            "/api/profit",
            params={"days": days},
            headers=self._headers(),
        )

        assert response.status == 400
        assert await response.json() == {"error": "invalid days"}
        bot.profit_service.load_report.assert_not_awaited()

    async def test_api_prompts_only_the_member_whose_key_is_missing(
        self,
        client: TestClient,
        bot: FakeBot,
    ) -> None:
        bot.profit_service.load_report.side_effect = MissingProfitApiKey

        response = await client.get(
            "/api/profit",
            headers=self._headers(),
        )

        assert response.status == 409
        assert await response.json() == {"error": "api_key_missing"}

    async def test_upstream_failure_does_not_log_or_return_secret_content(
        self,
        client: TestClient,
        bot: FakeBot,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        secret = "profit-response-secret"
        bot.profit_service.load_report.side_effect = ProfitApiError(secret)

        with caplog.at_level(logging.DEBUG, logger="gw2bot"):
            response = await client.get(
                "/api/profit",
                headers=self._headers(),
            )
        body = await response.text()

        assert response.status == 502
        assert secret not in caplog.text
        assert secret not in body


class TestFoodPageGate:
    async def test_unauthenticated_page_shows_sign_in(
        self,
        client: TestClient,
    ) -> None:
        response = await client.get("/food")

        assert response.status == 401
        assert "Sign in with Discord" in await response.text()

    async def test_member_without_role_gets_officers_only(
        self,
        client: TestClient,
    ) -> None:
        # The default session member is a plain guild member with no roles, so
        # the officer-gated dashboard must turn them away.
        response = await client.get(
            "/food",
            headers={"Cookie": f"{auth.SESSION_COOKIE}={session_cookie()}"},
        )

        assert response.status == 403
        assert "Officers only" in await response.text()

    async def test_officer_reaches_food_page(
        self,
        client: TestClient,
        guild: FakeGuild,
    ) -> None:
        guild.members[SESSION_USER_ID] = member("Kitty", officer=True)

        response = await client.get(
            "/food",
            headers={"Cookie": f"{auth.SESSION_COOKIE}={session_cookie()}"},
        )

        assert response.status == 200
        assert "Feast Usage" in await response.text()

    async def test_officer_role_is_cached_between_requests(
        self,
        client: TestClient,
        guild: FakeGuild,
    ) -> None:
        # Neither membership nor the role is in the gateway cache, so the first
        # request fetches for each check; both answers are then cached, so a
        # second request costs no further lookups.
        guild.members.clear()
        guild.fetch_member = AsyncMock(return_value=member(officer=True))
        headers = {"Cookie": f"{auth.SESSION_COOKIE}={session_cookie()}"}

        assert (await client.get("/food", headers=headers)).status == 200
        assert guild.fetch_member.await_count == 2

        assert (await client.get("/food", headers=headers)).status == 200
        assert guild.fetch_member.await_count == 2

    async def test_unreachable_discord_returns_503(
        self,
        client: TestClient,
        guild: FakeGuild,
    ) -> None:
        # An unknown role state is not evidence the member lacks the role, so
        # the page reports a temporary outage rather than a hard denial.
        guild.members.clear()
        guild.fetch_member = AsyncMock(side_effect=forbidden_error(50001))

        response = await client.get(
            "/food",
            headers={"Cookie": f"{auth.SESSION_COOKIE}={session_cookie()}"},
        )

        assert response.status == 503


class TestFoodApi:
    def _officer_headers(self, guild: FakeGuild) -> dict[str, str]:
        guild.members[SESSION_USER_ID] = member("Kitty", officer=True)
        return {"Cookie": f"{auth.SESSION_COOKIE}={session_cookie()}"}

    async def test_member_without_role_is_forbidden(
        self,
        client: TestClient,
    ) -> None:
        response = await client.get(
            "/api/food",
            headers={"Cookie": f"{auth.SESSION_COOKIE}={session_cookie()}"},
        )

        assert response.status == 403
        assert await response.json() == {"error": "forbidden"}

    async def test_rejects_unknown_range(
        self,
        client: TestClient,
        guild: FakeGuild,
    ) -> None:
        response = await client.get(
            "/api/food",
            params={"range": "90d"},
            headers=self._officer_headers(guild),
        )

        assert response.status == 400
        assert await response.json() == {"error": "invalid range"}

    async def test_returns_points_and_removals_for_each_feast(
        self,
        client: TestClient,
        guild: FakeGuild,
        raffle_store: RaffleStore,
    ) -> None:
        now = time.time()
        # Three counts inside the 24h window: 50 -> 44 -> 40, i.e. two drops.
        raffle_store.record_feast_counts({1078: 50}, now - 3000)
        raffle_store.record_feast_counts({1078: 44}, now - 2000)
        raffle_store.record_feast_counts({1078: 40}, now - 1000)

        response = await client.get(
            "/api/food",
            params={"range": "24h"},
            headers=self._officer_headers(guild),
        )

        assert response.status == 200
        payload = await response.json()
        assert payload["range"] == "24h"
        # All four tracked feasts appear, keyed by their guild storage id.
        assert [feast["id"] for feast in payload["feasts"]] == [
            1078,
            1089,
            1102,
            1112,
        ]
        tracked = payload["feasts"][0]
        assert tracked["name"] == "Bowl of Fruit Salad with Mint Garnish"
        assert [point["count"] for point in tracked["points"]] == [50, 44, 40]
        # Removals are newest first, each carrying the drop and what was left.
        assert [
            (removal["amount"], removal["remaining"])
            for removal in tracked["removals"]
        ] == [(4, 40), (6, 44)]
        # A feast with no records still appears with empty series.
        assert payload["feasts"][1]["points"] == []
        assert payload["feasts"][1]["removals"] == []

    async def test_defaults_to_the_24h_range(
        self,
        client: TestClient,
        guild: FakeGuild,
    ) -> None:
        response = await client.get(
            "/api/food",
            headers=self._officer_headers(guild),
        )

        assert response.status == 200
        assert (await response.json())["range"] == "24h"

    async def test_a_custom_window_is_drawn_between_its_own_edges(
        self,
        client: TestClient,
        guild: FakeGuild,
        raffle_store: RaffleStore,
    ) -> None:
        now = time.time()
        raffle_store.record_feast_counts({1078: 50}, now - 5000)
        raffle_store.record_feast_counts({1078: 44}, now - 3000)
        # After the window's end, so it belongs to no part of the chart.
        raffle_store.record_feast_counts({1078: 40}, now - 1000)

        response = await client.get(
            "/api/food",
            params={
                "range": "custom",
                "start": str(int(now - 4000)),
                "end": str(int(now - 2000)),
            },
            headers=self._officer_headers(guild),
        )

        assert response.status == 200
        payload = await response.json()
        assert payload["range"] == "custom"
        assert (payload["since"], payload["now"]) == (
            float(int(now - 4000)),
            float(int(now - 2000)),
        )
        tracked = payload["feasts"][0]
        assert [point["count"] for point in tracked["points"]] == [44]
        # The drop into the window is still measured against the count that
        # entered it, which was recorded before the left-hand edge.
        assert [
            (removal["amount"], removal["remaining"])
            for removal in tracked["removals"]
        ] == [(6, 44)]

    async def test_a_custom_window_stops_at_the_present(
        self,
        client: TestClient,
        guild: FakeGuild,
    ) -> None:
        now = time.time()

        response = await client.get(
            "/api/food",
            params={
                "range": "custom",
                "start": str(int(now - 3600)),
                "end": str(int(now + 90 * 86400)),
            },
            headers=self._officer_headers(guild),
        )

        assert response.status == 200
        payload = await response.json()
        # Nothing has been recorded for a day that has not happened, so the
        # window closes at the moment of the request instead.
        assert payload["now"] <= time.time()

    @pytest.mark.parametrize(
        "params",
        [
            pytest.param({"range": "custom"}, id="no-bounds"),
            pytest.param(
                {"range": "custom", "start": "100"}, id="one-bound"
            ),
            pytest.param(
                {"range": "custom", "start": "abc", "end": "200"},
                id="unreadable",
            ),
            pytest.param(
                {"range": "custom", "start": "200.5", "end": "400"},
                id="fractional",
            ),
            pytest.param(
                {"range": "custom", "start": "400", "end": "200"},
                id="backwards",
            ),
            pytest.param(
                {"range": "custom", "start": "200", "end": "200"},
                id="empty",
            ),
            pytest.param(
                {"range": "custom", "start": "1" * 310, "end": "200"},
                id="too-large-to-hold",
            ),
        ],
    )
    async def test_rejects_a_custom_window_it_cannot_draw(
        self,
        client: TestClient,
        guild: FakeGuild,
        params: dict[str, str],
    ) -> None:
        response = await client.get(
            "/api/food",
            params=params,
            headers=self._officer_headers(guild),
        )

        assert response.status == 400
        assert await response.json() == {"error": "invalid range"}


    async def test_a_custom_window_never_logs_the_query_it_came_from(
        self,
        client: TestClient,
        guild: FakeGuild,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # The custom range is the one query these dashboards read values out
        # of, and both the refusal and the acceptance log a line about it. A
        # marker is planted in every part of the query a caller controls, and
        # none of it may come back out of the console.
        marker = "s3cret-marker-never-log-me"
        now = int(time.time())

        with caplog.at_level("DEBUG"):
            refused = await client.get(
                "/api/food",
                params={
                    "range": "custom",
                    "start": marker,
                    "end": marker,
                    "token": marker,
                },
                headers=self._officer_headers(guild),
            )
            served = await client.get(
                "/api/food",
                params={
                    "range": "custom",
                    "start": str(now - 3600),
                    "end": str(now),
                    "token": marker,
                },
                headers=self._officer_headers(guild),
            )

        assert refused.status == 400
        assert served.status == 200
        # The refusal names a fixed reason and the acceptance a count of days;
        # neither carries any part of the query that produced it, and the
        # session cookie's signing secret never reaches a log either.
        assert marker not in caplog.text
        assert SESSION_SECRET not in caplog.text
        assert "reason=custom-bounds" in caplog.text
        assert "feast usage window; days=0" in caplog.text

    async def test_rejects_a_custom_window_wider_than_a_year(
        self,
        client: TestClient,
        guild: FakeGuild,
    ) -> None:
        now = int(time.time())

        response = await client.get(
            "/api/food",
            params={
                "range": "custom",
                "start": str(now - 400 * 86400),
                "end": str(now),
            },
            headers=self._officer_headers(guild),
        )

        assert response.status == 400
        assert await response.json() == {"error": "invalid range"}

    async def test_unreachable_discord_returns_503_json(
        self,
        client: TestClient,
        guild: FakeGuild,
    ) -> None:
        guild.members.clear()
        guild.fetch_member = AsyncMock(side_effect=forbidden_error(50001))

        response = await client.get(
            "/api/food",
            headers={"Cookie": f"{auth.SESSION_COOKIE}={session_cookie()}"},
        )

        assert response.status == 503
        assert await response.json() == {"error": "unavailable"}


class TestRosterPageGate:
    async def test_unauthenticated_page_shows_sign_in(
        self,
        client: TestClient,
    ) -> None:
        response = await client.get("/roster")

        assert response.status == 401
        assert "Sign in with Discord" in await response.text()

    async def test_member_without_role_gets_officers_only(
        self,
        client: TestClient,
    ) -> None:
        response = await client.get(
            "/roster",
            headers={"Cookie": f"{auth.SESSION_COOKIE}={session_cookie()}"},
        )

        assert response.status == 403
        assert "guild roster history" in await response.text()

    async def test_officer_reaches_roster_page(
        self,
        client: TestClient,
        guild: FakeGuild,
    ) -> None:
        guild.members[SESSION_USER_ID] = member("Kitty", officer=True)

        response = await client.get(
            "/roster",
            headers={"Cookie": f"{auth.SESSION_COOKIE}={session_cookie()}"},
        )

        assert response.status == 200
        assert "Guild Roster" in await response.text()

    async def test_unreachable_discord_returns_503(
        self,
        client: TestClient,
        guild: FakeGuild,
    ) -> None:
        guild.members.clear()
        guild.fetch_member = AsyncMock(side_effect=forbidden_error(50001))

        response = await client.get(
            "/roster",
            headers={"Cookie": f"{auth.SESSION_COOKIE}={session_cookie()}"},
        )

        assert response.status == 503


class TestRosterApi:
    def _officer_headers(self, guild: FakeGuild) -> dict[str, str]:
        guild.members[SESSION_USER_ID] = member("Kitty", officer=True)
        return {"Cookie": f"{auth.SESSION_COOKIE}={session_cookie()}"}

    async def test_member_without_role_is_forbidden(
        self,
        client: TestClient,
    ) -> None:
        response = await client.get(
            "/api/roster",
            headers={"Cookie": f"{auth.SESSION_COOKIE}={session_cookie()}"},
        )

        assert response.status == 403
        assert await response.json() == {"error": "forbidden"}

    async def test_rejects_unknown_range(
        self,
        client: TestClient,
        guild: FakeGuild,
    ) -> None:
        response = await client.get(
            "/api/roster",
            params={"range": "90d"},
            headers=self._officer_headers(guild),
        )

        assert response.status == 400
        assert await response.json() == {"error": "invalid range"}

    async def test_returns_the_line_and_the_changes(
        self,
        client: TestClient,
        guild: FakeGuild,
        raffle_store: RaffleStore,
    ) -> None:
        now = time.time()
        raffle_store.import_membership_events(
            [
                ImportedMembershipEvent(1, now - 3000, JOIN, "One.1234"),
                ImportedMembershipEvent(2, now - 2000, LEAVE, "Two.5678"),
                ImportedMembershipEvent(
                    3,
                    now - 1000,
                    KICK,
                    "Three.9012",
                    "Officer.3456",
                ),
            ]
        )
        raffle_store.record_member_count(400, 2, now - 500)

        response = await client.get(
            "/api/roster",
            params={"range": "24h"},
            headers=self._officer_headers(guild),
        )

        assert response.status == 200
        payload = await response.json()
        assert payload["range"] == "24h"
        assert payload["member_count"] == 400
        assert (payload["joins"], payload["leaves"], payload["kicks"]) == (
            1,
            1,
            1,
        )
        # Newest first, each carrying the count it left the guild at.
        assert [
            (change["kind"], change["name"], change["count"])
            for change in payload["events"]
        ] == [
            (KICK, "Three.9012", 400),
            (LEAVE, "Two.5678", 401),
            (JOIN, "One.1234", 402),
        ]
        assert payload["events"][0]["actor"] == "Officer.3456"
        # The line spans the whole window: an opening vertex, one per change,
        # and a closing one at the moment of the request that carries the
        # count forward to the right-hand edge.
        assert [point["count"] for point in payload["points"]] == [
            401,
            402,
            401,
            400,
            400,
        ]

    async def test_reports_no_line_before_a_count_is_observed(
        self,
        client: TestClient,
        guild: FakeGuild,
        raffle_store: RaffleStore,
    ) -> None:
        raffle_store.import_membership_events(
            [ImportedMembershipEvent(1, time.time() - 100, JOIN, "One.1234")]
        )

        response = await client.get(
            "/api/roster",
            headers=self._officer_headers(guild),
        )

        payload = await response.json()
        assert payload["points"] == []
        assert payload["member_count"] is None
        assert payload["events"][0]["count"] is None

    async def test_defaults_to_the_24h_range(
        self,
        client: TestClient,
        guild: FakeGuild,
    ) -> None:
        response = await client.get(
            "/api/roster",
            headers=self._officer_headers(guild),
        )

        assert response.status == 200
        assert (await response.json())["range"] == "24h"

    async def test_a_custom_window_is_counted_back_from_the_anchor(
        self,
        client: TestClient,
        guild: FakeGuild,
        raffle_store: RaffleStore,
    ) -> None:
        now = time.time()
        raffle_store.import_membership_events(
            [
                ImportedMembershipEvent(1, now - 5000, JOIN, "One.1234"),
                # Both after the window's end, so the count it closes at has
                # to be recovered by unwinding them from the anchor.
                ImportedMembershipEvent(2, now - 1500, LEAVE, "Two.5678"),
                ImportedMembershipEvent(3, now - 1200, LEAVE, "Three.9012"),
            ]
        )
        raffle_store.record_member_count(400, 2, now - 500)

        response = await client.get(
            "/api/roster",
            params={
                "range": "custom",
                "start": str(int(now - 6000)),
                "end": str(int(now - 2000)),
            },
            headers=self._officer_headers(guild),
        )

        assert response.status == 200
        payload = await response.json()
        assert payload["range"] == "custom"
        assert (payload["since"], payload["now"]) == (
            float(int(now - 6000)),
            float(int(now - 2000)),
        )
        # Only the join falls in the window, and it stands two members above
        # where the roster ended up.
        assert [
            (change["kind"], change["name"], change["count"])
            for change in payload["events"]
        ] == [(JOIN, "One.1234", 402)]
        assert (payload["joins"], payload["leaves"], payload["kicks"]) == (
            1,
            0,
            0,
        )
        assert payload["member_count"] == 402
        assert [point["count"] for point in payload["points"]] == [
            401,
            402,
            402,
        ]

    @pytest.mark.parametrize(
        "params",
        [
            pytest.param({"range": "custom"}, id="no-bounds"),
            pytest.param(
                {"range": "custom", "start": "abc", "end": "200"},
                id="unreadable",
            ),
            pytest.param(
                {"range": "custom", "start": "400", "end": "200"},
                id="backwards",
            ),
            pytest.param(
                {"range": "custom", "start": "200", "end": "9" * 310},
                id="too-large-to-hold",
            ),
        ],
    )
    async def test_rejects_a_custom_window_it_cannot_draw(
        self,
        client: TestClient,
        guild: FakeGuild,
        params: dict[str, str],
    ) -> None:
        response = await client.get(
            "/api/roster",
            params=params,
            headers=self._officer_headers(guild),
        )

        assert response.status == 400
        assert await response.json() == {"error": "invalid range"}


    async def test_a_custom_window_never_logs_the_query_it_came_from(
        self,
        client: TestClient,
        guild: FakeGuild,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # The custom range is the one query these dashboards read values out
        # of, and both the refusal and the acceptance log a line about it. A
        # marker is planted in every part of the query a caller controls, and
        # none of it may come back out of the console.
        marker = "s3cret-marker-never-log-me"
        now = int(time.time())

        with caplog.at_level("DEBUG"):
            refused = await client.get(
                "/api/roster",
                params={
                    "range": "custom",
                    "start": marker,
                    "end": marker,
                    "token": marker,
                },
                headers=self._officer_headers(guild),
            )
            served = await client.get(
                "/api/roster",
                params={
                    "range": "custom",
                    "start": str(now - 3600),
                    "end": str(now),
                    "token": marker,
                },
                headers=self._officer_headers(guild),
            )

        assert refused.status == 400
        assert served.status == 200
        # The refusal names a fixed reason and the acceptance a count of days;
        # neither carries any part of the query that produced it, and the
        # session cookie's signing secret never reaches a log either.
        assert marker not in caplog.text
        assert SESSION_SECRET not in caplog.text
        assert "reason=custom-bounds" in caplog.text
        assert "roster history window; days=0" in caplog.text

    async def test_rejects_a_custom_window_wider_than_a_year(
        self,
        client: TestClient,
        guild: FakeGuild,
    ) -> None:
        now = int(time.time())

        response = await client.get(
            "/api/roster",
            params={
                "range": "custom",
                "start": str(now - 400 * 86400),
                "end": str(now),
            },
            headers=self._officer_headers(guild),
        )

        assert response.status == 400
        assert await response.json() == {"error": "invalid range"}

    async def test_unreachable_discord_returns_503_json(
        self,
        client: TestClient,
        guild: FakeGuild,
    ) -> None:
        guild.members.clear()
        guild.fetch_member = AsyncMock(side_effect=forbidden_error(50001))

        response = await client.get(
            "/api/roster",
            headers={"Cookie": f"{auth.SESSION_COOKIE}={session_cookie()}"},
        )

        assert response.status == 503
        assert await response.json() == {"error": "unavailable"}


class TestPendingInviteApi:
    def _officer_headers(self, guild: FakeGuild) -> dict[str, str]:
        guild.members[SESSION_USER_ID] = member("Kitty", officer=True)
        return {"Cookie": f"{auth.SESSION_COOKIE}={session_cookie()}"}

    async def test_member_without_role_is_forbidden(
        self,
        client: TestClient,
        bot: FakeBot,
    ) -> None:
        response = await client.get(
            "/api/pending",
            headers={"Cookie": f"{auth.SESSION_COOKIE}={session_cookie()}"},
        )

        assert response.status == 403
        assert await response.json() == {"error": "forbidden"}
        bot.build_pending_invite_entries.assert_not_awaited()

    async def test_returns_each_invite_with_its_matched_discord_name(
        self,
        client: TestClient,
        guild: FakeGuild,
        bot: FakeBot,
    ) -> None:
        headers = self._officer_headers(guild)
        guild.members[77] = member("Applicant")
        bot.build_pending_invite_entries = AsyncMock(
            return_value=PendingInvites(
                [
                    TrialMemberReportEntry("Zebra.9999"),
                    TrialMemberReportEntry("Apple.1234", discord_user_id=77),
                ],
                True,
            )
        )

        response = await client.get("/api/pending", headers=headers)

        assert response.status == 200
        # Alphabetical, and an unmatched account carries no Discord name
        # rather than an empty one.
        assert await response.json() == {
            "available": True,
            "matched": True,
            "invites": [
                {"name": "Apple.1234", "discord_name": "Applicant"},
                {"name": "Zebra.9999", "discord_name": None},
            ],
        }

    async def test_is_built_once_and_served_from_the_cache(
        self,
        client: TestClient,
        guild: FakeGuild,
        bot: FakeBot,
    ) -> None:
        headers = self._officer_headers(guild)
        bot.build_pending_invite_entries = AsyncMock(
            return_value=PendingInvites(
                [TrialMemberReportEntry("Apple.1234")], True
            )
        )

        first = await client.get("/api/pending", headers=headers)
        second = await client.get("/api/pending", headers=headers)

        assert first.status == 200
        assert await first.json() == await second.json()
        # Building the list costs a GW2 API call and a forum index refresh, so
        # a second page load inside the TTL must not pay for it again.
        bot.build_pending_invite_entries.assert_awaited_once()

    async def test_an_unresolved_name_is_not_cached_as_unknown(
        self,
        client: TestClient,
        guild: FakeGuild,
        bot: FakeBot,
    ) -> None:
        # A name Discord could not answer for is deliberately not cached by
        # the name lookup, so caching the payload built from it would pin the
        # applicant to "Unknown" for the rest of the TTL anyway.
        headers = self._officer_headers(guild)
        bot.build_pending_invite_entries = AsyncMock(
            return_value=PendingInvites(
                [TrialMemberReportEntry("Apple.1234", discord_user_id=77)],
                True,
            )
        )

        unknown = await client.get("/api/pending", headers=headers)
        guild.members[77] = member("Applicant")
        recovered = await client.get("/api/pending", headers=headers)

        assert (await unknown.json())["invites"] == [
            {"name": "Apple.1234", "discord_name": "Unknown"}
        ]
        assert (await recovered.json())["invites"] == [
            {"name": "Apple.1234", "discord_name": "Applicant"}
        ]

    async def test_a_member_actually_called_unknown_is_still_cached(
        self,
        client: TestClient,
        guild: FakeGuild,
        bot: FakeBot,
    ) -> None:
        # Failure is carried by the resolved names themselves, not inferred
        # from the rendered name, so a member whose display name really is
        # "Unknown" does not cost a rebuild on every page load.
        headers = self._officer_headers(guild)
        guild.members[77] = member("Unknown")
        bot.build_pending_invite_entries = AsyncMock(
            return_value=PendingInvites(
                [TrialMemberReportEntry("Apple.1234", discord_user_id=77)],
                True,
            )
        )

        first = await client.get("/api/pending", headers=headers)
        second = await client.get("/api/pending", headers=headers)

        assert (await first.json())["invites"] == [
            {"name": "Apple.1234", "discord_name": "Unknown"}
        ]
        assert await second.json() == await first.json()
        bot.build_pending_invite_entries.assert_awaited_once()

    async def test_reports_the_section_off_without_the_gw2_settings(
        self,
        client: TestClient,
        guild: FakeGuild,
        bot: FakeBot,
    ) -> None:
        headers = self._officer_headers(guild)
        bot.gw2_api_enabled = False
        bot._config = make_config_without_gw2()

        response = await client.get("/api/pending", headers=headers)

        assert response.status == 200
        # The settings are named, so the page can tell the reader which
        # /settings subcommands turn the section on.
        assert await response.json() == {
            "available": False,
            "invites": [],
            "missing": ["gw2_api_key", "gw2_guild_id"],
        }
        bot.build_pending_invite_entries.assert_not_awaited()

    async def test_a_settings_change_drops_the_cached_list(
        self,
        client: TestClient,
        web_server: WebServer,
        guild: FakeGuild,
        bot: FakeBot,
    ) -> None:
        # The server outlives a change to the guild the list was read from, so
        # the bot drops the cache rather than letting the old guild's invites
        # be served for the rest of the TTL.
        headers = self._officer_headers(guild)
        bot.build_pending_invite_entries = AsyncMock(
            side_effect=[
                PendingInvites([TrialMemberReportEntry("Old.1234")], True),
                PendingInvites([TrialMemberReportEntry("New.5678")], True),
            ]
        )

        first = await client.get("/api/pending", headers=headers)
        web_server.clear_pending_invites()
        second = await client.get("/api/pending", headers=headers)

        assert (await first.json())["invites"] == [
            {"name": "Old.1234", "discord_name": None}
        ]
        assert (await second.json())["invites"] == [
            {"name": "New.5678", "discord_name": None}
        ]

    async def test_unreachable_gw2_api_returns_503_json(
        self,
        client: TestClient,
        guild: FakeGuild,
        bot: FakeBot,
    ) -> None:
        headers = self._officer_headers(guild)
        bot.build_pending_invite_entries = AsyncMock(
            side_effect=aiohttp.ClientError("boom")
        )

        response = await client.get("/api/pending", headers=headers)

        assert response.status == 503
        assert await response.json() == {"error": "unavailable"}

    async def test_a_failed_build_is_not_cached(
        self,
        client: TestClient,
        guild: FakeGuild,
        bot: FakeBot,
    ) -> None:
        headers = self._officer_headers(guild)
        bot.build_pending_invite_entries = AsyncMock(
            side_effect=[
                aiohttp.ClientError("boom"),
                PendingInvites([TrialMemberReportEntry("Apple.1234")], True),
            ]
        )

        failed = await client.get("/api/pending", headers=headers)
        recovered = await client.get("/api/pending", headers=headers)

        assert failed.status == 503
        assert await recovered.json() == {
            "available": True,
            "matched": True,
            "invites": [{"name": "Apple.1234", "discord_name": None}],
        }

    async def test_a_database_failure_is_reported_rather_than_raised(
        self,
        client: TestClient,
        guild: FakeGuild,
        bot: FakeBot,
    ) -> None:
        # The Trial forum index the match reads lives in SQLite, and a locked
        # database would otherwise reach the reader as an uncontrolled 500.
        headers = self._officer_headers(guild)
        bot.build_pending_invite_entries = AsyncMock(
            side_effect=SQLAlchemyError("database is locked")
        )

        response = await client.get("/api/pending", headers=headers)

        assert response.status == 503
        assert await response.json() == {"error": "unavailable"}

    async def test_an_unread_forum_is_neither_claimed_nor_cached(
        self,
        client: TestClient,
        guild: FakeGuild,
        bot: FakeBot,
    ) -> None:
        # Every entry comes back unmatched when Discord refuses the forum,
        # which is not evidence that nobody applied - so the page is told, and
        # the answer is not kept for the next five minutes.
        headers = self._officer_headers(guild)
        bot.build_pending_invite_entries = AsyncMock(
            side_effect=[
                PendingInvites([TrialMemberReportEntry("Apple.1234")], False),
                PendingInvites(
                    [TrialMemberReportEntry("Apple.1234", discord_user_id=77)],
                    True,
                ),
            ]
        )
        guild.members[77] = member("Applicant")

        unmatched = await client.get("/api/pending", headers=headers)
        rebuilt = await client.get("/api/pending", headers=headers)

        assert await unmatched.json() == {
            "available": True,
            "matched": False,
            "invites": [{"name": "Apple.1234", "discord_name": None}],
        }
        assert await rebuilt.json() == {
            "available": True,
            "matched": True,
            "invites": [{"name": "Apple.1234", "discord_name": "Applicant"}],
        }

    async def test_failure_logging_omits_the_upstream_detail(
        self,
        client: TestClient,
        guild: FakeGuild,
        bot: FakeBot,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        headers = self._officer_headers(guild)
        bot.build_pending_invite_entries = AsyncMock(
            side_effect=aiohttp.ClientError("key=secret-value")
        )

        with caplog.at_level(logging.DEBUG):
            await client.get("/api/pending", headers=headers)

        assert "secret-value" not in caplog.text
        assert "error_type=ClientError" in caplog.text


class TestGoldPageGate:
    async def test_unauthenticated_page_shows_sign_in(
        self,
        client: TestClient,
    ) -> None:
        response = await client.get("/gold")

        assert response.status == 401
        assert "Sign in with Discord" in await response.text()

    async def test_member_without_role_gets_officers_only(
        self,
        client: TestClient,
    ) -> None:
        response = await client.get(
            "/gold",
            headers={"Cookie": f"{auth.SESSION_COOKIE}={session_cookie()}"},
        )

        assert response.status == 403
        assert "guild bank gold history" in await response.text()

    async def test_officer_reaches_gold_page(
        self,
        client: TestClient,
        guild: FakeGuild,
    ) -> None:
        guild.members[SESSION_USER_ID] = member("Kitty", officer=True)

        response = await client.get(
            "/gold",
            headers={"Cookie": f"{auth.SESSION_COOKIE}={session_cookie()}"},
        )

        assert response.status == 200
        assert "Guild Bank" in await response.text()

    async def test_unreachable_discord_returns_503(
        self,
        client: TestClient,
        guild: FakeGuild,
    ) -> None:
        guild.members.clear()
        guild.fetch_member = AsyncMock(side_effect=forbidden_error(50001))

        response = await client.get(
            "/gold",
            headers={"Cookie": f"{auth.SESSION_COOKIE}={session_cookie()}"},
        )

        assert response.status == 503


class TestGoldApi:
    def _officer_headers(self, guild: FakeGuild) -> dict[str, str]:
        guild.members[SESSION_USER_ID] = member("Kitty", officer=True)
        return {"Cookie": f"{auth.SESSION_COOKIE}={session_cookie()}"}

    def _record(
        self,
        raffle_store: RaffleStore,
        now: float,
        entries: list[tuple[int, float, str, str, int]],
    ) -> None:
        raffle_store.import_gold_movements(
            [
                GoldLedgerEntry(
                    event_id=event_id,
                    username=username,
                    operation=operation,
                    coins=coins,
                    event_time=datetime.fromtimestamp(
                        now + offset, UTC
                    ).isoformat(),
                )
                for event_id, offset, operation, username, coins in entries
            ]
        )

    async def test_member_without_role_is_forbidden(
        self,
        client: TestClient,
    ) -> None:
        response = await client.get(
            "/api/gold",
            headers={"Cookie": f"{auth.SESSION_COOKIE}={session_cookie()}"},
        )

        assert response.status == 403
        assert await response.json() == {"error": "forbidden"}

    async def test_rejects_unknown_range(
        self,
        client: TestClient,
        guild: FakeGuild,
    ) -> None:
        response = await client.get(
            "/api/gold",
            params={"range": "90d"},
            headers=self._officer_headers(guild),
        )

        assert response.status == 400
        assert await response.json() == {"error": "invalid range"}

    async def test_returns_the_line_and_the_movements(
        self,
        client: TestClient,
        guild: FakeGuild,
        raffle_store: RaffleStore,
    ) -> None:
        now = time.time()
        self._record(
            raffle_store,
            now,
            [
                (1, -3000, DEPOSIT, "One.1234", 500_000),
                (2, -2000, WITHDRAW, "Two.5678", 200_000),
                (3, -1000, DEPOSIT, "Three.9012", 100_000),
            ],
        )
        raffle_store.record_stash_balance(900_000, now - 500)

        response = await client.get(
            "/api/gold",
            params={"range": "24h"},
            headers=self._officer_headers(guild),
        )

        assert response.status == 200
        payload = await response.json()
        assert payload["range"] == "24h"
        assert payload["coins"] == 900_000
        assert (payload["deposited"], payload["withdrawn"], payload["net"]) == (
            600_000,
            200_000,
            400_000,
        )
        # Newest first, each carrying the balance it left the bank at.
        assert [
            (item["operation"], item["name"], item["coins"], item["after"])
            for item in payload["movements"]
        ] == [
            (DEPOSIT, "Three.9012", 100_000, 900_000),
            (WITHDRAW, "Two.5678", 200_000, 800_000),
            (DEPOSIT, "One.1234", 500_000, 1_000_000),
        ]
        # The line spans the whole window: an opening vertex, one per
        # movement, and a closing one at the moment of the request that
        # carries the balance forward to the right-hand edge.
        assert [point["coins"] for point in payload["points"]] == [
            500_000,
            1_000_000,
            800_000,
            900_000,
            900_000,
        ]

    async def test_reports_no_line_before_a_balance_is_observed(
        self,
        client: TestClient,
        guild: FakeGuild,
        raffle_store: RaffleStore,
    ) -> None:
        now = time.time()
        self._record(
            raffle_store, now, [(1, -100, DEPOSIT, "One.1234", 500_000)]
        )

        response = await client.get(
            "/api/gold",
            headers=self._officer_headers(guild),
        )

        payload = await response.json()
        assert payload["points"] == []
        assert payload["coins"] is None
        assert payload["movements"][0]["after"] is None

    async def test_defaults_to_the_24h_range(
        self,
        client: TestClient,
        guild: FakeGuild,
    ) -> None:
        response = await client.get(
            "/api/gold",
            headers=self._officer_headers(guild),
        )

        assert response.status == 200
        assert (await response.json())["range"] == "24h"

    async def test_a_custom_window_is_measured_back_from_the_anchor(
        self,
        client: TestClient,
        guild: FakeGuild,
        raffle_store: RaffleStore,
    ) -> None:
        now = time.time()
        self._record(
            raffle_store,
            now,
            [
                (1, -5000, DEPOSIT, "One.1234", 500_000),
                # Both after the window's end, so the balance it closes at has
                # to be recovered by unwinding them from the anchor.
                (2, -1500, WITHDRAW, "Two.5678", 200_000),
                (3, -1200, WITHDRAW, "Three.9012", 100_000),
            ],
        )
        raffle_store.record_stash_balance(900_000, now - 500)

        response = await client.get(
            "/api/gold",
            params={
                "range": "custom",
                "start": str(int(now - 6000)),
                "end": str(int(now - 2000)),
            },
            headers=self._officer_headers(guild),
        )

        assert response.status == 200
        payload = await response.json()
        assert payload["range"] == "custom"
        # Only the deposit falls in the window, and it stands 300,000 above
        # where the bank ended up.
        assert [
            (item["operation"], item["name"], item["after"])
            for item in payload["movements"]
        ] == [(DEPOSIT, "One.1234", 1_200_000)]
        assert payload["coins"] == 1_200_000

    async def test_serving_names_no_account_or_balance(
        self,
        client: TestClient,
        guild: FakeGuild,
        raffle_store: RaffleStore,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # The guild's holdings and who moved them are the page's whole
        # subject; the console has no business carrying either.
        now = time.time()
        self._record(
            raffle_store, now, [(1, -100, WITHDRAW, "Secret.1234", 4_242_424)]
        )
        raffle_store.record_stash_balance(4_242_424, now - 50)

        with caplog.at_level(logging.DEBUG, logger="gw2bot"):
            response = await client.get(
                "/api/gold",
                headers=self._officer_headers(guild),
            )

        assert response.status == 200
        assert "Secret.1234" not in caplog.text
        assert "4242424" not in caplog.text
        assert "Served gold history; range=24h movements=1" in caplog.text

    async def test_rejects_a_custom_window_wider_than_a_year(
        self,
        client: TestClient,
        guild: FakeGuild,
    ) -> None:
        now = int(time.time())

        response = await client.get(
            "/api/gold",
            params={
                "range": "custom",
                "start": str(now - 400 * 86400),
                "end": str(now),
            },
            headers=self._officer_headers(guild),
        )

        assert response.status == 400
        assert await response.json() == {"error": "invalid range"}

    async def test_unreachable_discord_returns_503_json(
        self,
        client: TestClient,
        guild: FakeGuild,
    ) -> None:
        guild.members.clear()
        guild.fetch_member = AsyncMock(side_effect=forbidden_error(50001))

        response = await client.get(
            "/api/gold",
            headers={"Cookie": f"{auth.SESSION_COOKIE}={session_cookie()}"},
        )

        assert response.status == 503
        assert await response.json() == {"error": "unavailable"}
