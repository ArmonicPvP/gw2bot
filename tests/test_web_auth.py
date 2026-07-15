from datetime import UTC, datetime, timedelta

import pytest

from gw2bot.web.auth import (
    PROMPTABLE_AUTHORIZE_ERRORS,
    OAuthExchangeError,
    authorize_url,
    exchange_code,
    fetch_identity,
    sanitize_authorize_error,
    sign_session,
    sign_state,
    state_is_consent_retry,
    verify_session,
    verify_state,
)

SECRET = "unit-test-session-secret-0123456789abcdef"
NOW = datetime(2027, 1, 1, 0, 0, tzinfo=UTC)


class FakeResponse:
    def __init__(self, status: int, payload: object):
        self.status = status
        self._payload = payload

    async def __aenter__(self) -> "FakeResponse":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def json(self) -> object:
        return self._payload


class FakeSession:
    def __init__(self, response: FakeResponse):
        self._response = response
        self.calls: list[tuple[str, str, dict]] = []

    def post(self, url: str, *, data: dict) -> FakeResponse:
        self.calls.append(("POST", url, data))
        return self._response

    def get(self, url: str, *, headers: dict) -> FakeResponse:
        self.calls.append(("GET", url, headers))
        return self._response


class TestSessionCookie:
    def test_round_trip(self) -> None:
        value = sign_session(SECRET, 1234, "Kitty", NOW + timedelta(days=7))

        session = verify_session(SECRET, value, NOW)

        assert session is not None
        assert session.user_id == 1234
        assert session.name == "Kitty"
        assert session.expires_at == NOW + timedelta(days=7)

    def test_truncates_long_names(self) -> None:
        value = sign_session(SECRET, 1, "n" * 500, NOW + timedelta(days=1))

        session = verify_session(SECRET, value, NOW)

        assert session is not None
        assert len(session.name) == 64

    def test_rejects_expired_session(self) -> None:
        value = sign_session(SECRET, 1, "Kitty", NOW)

        assert verify_session(SECRET, value, NOW) is None

    def test_rejects_wrong_secret(self) -> None:
        value = sign_session(SECRET, 1, "Kitty", NOW + timedelta(days=1))

        assert verify_session("x" * 32, value, NOW) is None

    def test_rejects_tampered_payload(self) -> None:
        value = sign_session(SECRET, 1, "Kitty", NOW + timedelta(days=1))
        payload, signature = value.split(".")
        tampered = payload[:-2] + "AA" + "." + signature

        assert verify_session(SECRET, tampered, NOW) is None

    @pytest.mark.parametrize(
        "garbage",
        [
            "",
            "a",
            "a.b",
            "a.b.c",
            "!!!.???",
            "eyJhIjoxfQ",
        ],
    )
    def test_rejects_garbage_without_raising(self, garbage: str) -> None:
        assert verify_session(SECRET, garbage, NOW) is None


class TestStateToken:
    def test_round_trip(self) -> None:
        state, cookie = sign_state(SECRET, NOW)

        assert verify_state(SECRET, cookie, state, NOW)

    def test_rejects_mismatched_state(self) -> None:
        _, cookie = sign_state(SECRET, NOW)

        assert not verify_state(SECRET, cookie, "other-state", NOW)

    def test_rejects_expired_state(self) -> None:
        state, cookie = sign_state(SECRET, NOW)

        assert not verify_state(
            SECRET,
            cookie,
            state,
            NOW + timedelta(minutes=11),
        )

    def test_rejects_garbage_cookie(self) -> None:
        assert not verify_state(SECRET, "garbage", "state", NOW)

    @pytest.mark.parametrize("state", ["é", "стейт", "🙂", "mixed-é-ascii"])
    def test_rejects_non_ascii_state_without_raising(self, state: str) -> None:
        # The state comes straight from the callback's query string, so it can
        # hold any code point. hmac.compare_digest raises TypeError when handed
        # a non-ASCII str, which would escape the callback as a 500 instead of
        # a rejected sign-in.
        _, cookie = sign_state(SECRET, NOW)

        assert not verify_state(SECRET, cookie, state, NOW)

    def test_default_state_is_not_a_consent_retry(self) -> None:
        _, cookie = sign_state(SECRET, NOW)

        assert not state_is_consent_retry(SECRET, cookie)

    def test_consent_retry_state_is_marked_and_still_verifies(self) -> None:
        state, cookie = sign_state(SECRET, NOW, consent_retry=True)

        # The retry flag rides alongside a still-valid state token.
        assert verify_state(SECRET, cookie, state, NOW)
        assert state_is_consent_retry(SECRET, cookie)

    def test_consent_retry_flag_survives_only_a_valid_signature(self) -> None:
        _, cookie = sign_state(SECRET, NOW, consent_retry=True)

        assert not state_is_consent_retry("x" * 32, cookie)
        assert not state_is_consent_retry(SECRET, "garbage")


class TestAuthorizeError:
    def test_promptable_errors_are_the_recoverable_set(self) -> None:
        assert "consent_required" in PROMPTABLE_AUTHORIZE_ERRORS
        assert "login_required" in PROMPTABLE_AUTHORIZE_ERRORS
        # A user declining consent is terminal, never retried.
        assert "access_denied" not in PROMPTABLE_AUTHORIZE_ERRORS

    def test_known_errors_pass_through_and_others_are_masked(self) -> None:
        assert sanitize_authorize_error("consent_required") == (
            "consent_required"
        )
        assert sanitize_authorize_error("access_denied") == "access_denied"
        # An arbitrary attacker-supplied value never reaches the logs verbatim.
        assert sanitize_authorize_error("<script>") == "other"


class TestAuthorizeUrl:
    def test_contains_identify_scope_and_state(self) -> None:
        url = authorize_url(
            "client-id",
            "https://calendar.example.test/oauth/callback",
            "the-state",
        )

        assert url.startswith("https://discord.com/oauth2/authorize?")
        assert "client_id=client-id" in url
        assert "scope=identify" in url
        assert "state=the-state" in url
        assert "response_type=code" in url

    def test_silent_by_default_but_omits_prompt_on_retry(self) -> None:
        silent = authorize_url("client-id", "https://c.test/cb", "s")
        prompted = authorize_url(
            "client-id",
            "https://c.test/cb",
            "s",
            prompt_none=False,
        )

        assert "prompt=none" in silent
        assert "prompt=" not in prompted


class TestTokenExchange:
    async def test_returns_access_token(self) -> None:
        session = FakeSession(
            FakeResponse(200, {"access_token": "the-token"})
        )

        token = await exchange_code(
            session,  # type: ignore[arg-type]  # FakeSession mimics post()
            "client-id",
            "client-secret",
            "https://calendar.example.test/oauth/callback",
            "the-code",
        )

        assert token == "the-token"
        method, url, data = session.calls[0]
        assert method == "POST"
        assert url == "https://discord.com/api/v10/oauth2/token"
        assert data["grant_type"] == "authorization_code"

    async def test_raises_on_error_status_without_leaking(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        session = FakeSession(FakeResponse(400, {"error": "invalid_grant"}))

        with caplog.at_level("DEBUG"):
            with pytest.raises(OAuthExchangeError):
                await exchange_code(
                    session,  # type: ignore[arg-type]  # FakeSession mimics post()
                    "client-id",
                    "client-secret",
                    "https://calendar.example.test/oauth/callback",
                    "the-code",
                )

        assert "the-code" not in caplog.text
        assert "client-secret" not in caplog.text

    async def test_raises_when_token_missing(self) -> None:
        session = FakeSession(FakeResponse(200, {}))

        with pytest.raises(OAuthExchangeError):
            await exchange_code(
                session,  # type: ignore[arg-type]  # FakeSession mimics post()
                "client-id",
                "client-secret",
                "https://calendar.example.test/oauth/callback",
                "the-code",
            )


class TestIdentityLookup:
    async def test_returns_identity(self) -> None:
        session = FakeSession(
            FakeResponse(
                200,
                {"id": "1234", "global_name": "Kitty", "username": "kitty"},
            )
        )

        identity = await fetch_identity(
            session,  # type: ignore[arg-type]  # FakeSession mimics get()
            "the-token",
        )

        assert identity.user_id == 1234
        assert identity.name == "Kitty"

    async def test_falls_back_to_username(self) -> None:
        session = FakeSession(
            FakeResponse(200, {"id": "1234", "username": "kitty"})
        )

        identity = await fetch_identity(
            session,  # type: ignore[arg-type]  # FakeSession mimics get()
            "the-token",
        )

        assert identity.name == "kitty"

    async def test_raises_on_error_status_without_leaking_token(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        session = FakeSession(FakeResponse(401, {}))

        with caplog.at_level("DEBUG"):
            with pytest.raises(OAuthExchangeError):
                await fetch_identity(
                    session,  # type: ignore[arg-type]  # FakeSession mimics get()
                    "the-token",
                )

        assert "the-token" not in caplog.text
