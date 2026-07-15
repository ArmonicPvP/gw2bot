from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlencode

import aiohttp

LOGGER = logging.getLogger(__name__)

SESSION_COOKIE = "gw2bot_session"
STATE_COOKIE = "gw2bot_oauth_state"
STATE_TTL_SECONDS = 600

DISCORD_AUTHORIZE_URL = "https://discord.com/oauth2/authorize"
DISCORD_TOKEN_URL = "https://discord.com/api/v10/oauth2/token"
DISCORD_ME_URL = "https://discord.com/api/v10/users/@me"

_MAX_SESSION_NAME_LENGTH = 64

# Authorization errors that a silent (prompt=none) attempt raises purely
# because it was not allowed to show a screen. An interactive retry that lets
# the user sign in and grant consent resolves every one of them, so the
# callback re-runs the flow once with the prompt enabled instead of failing.
PROMPTABLE_AUTHORIZE_ERRORS = frozenset(
    {
        "login_required",
        "consent_required",
        "interaction_required",
        "account_selection_required",
    }
)

# The OAuth2 authorization error codes worth naming in a log line. The error
# arrives in the callback's query string, so an unrecognized value is logged
# as "other" rather than echoed verbatim.
_KNOWN_AUTHORIZE_ERRORS = PROMPTABLE_AUTHORIZE_ERRORS | frozenset(
    {
        "access_denied",
        "invalid_request",
        "invalid_scope",
        "unauthorized_client",
        "unsupported_response_type",
        "server_error",
        "temporarily_unavailable",
    }
)


def sanitize_authorize_error(error: str) -> str:
    """Reduce a callback error code to a bounded, log-safe label."""
    return error if error in _KNOWN_AUTHORIZE_ERRORS else "other"


class OAuthExchangeError(Exception):
    """Raised when Discord rejects a token exchange or identity lookup.

    The message contains only a sanitized description and status code,
    never tokens or response bodies.
    """


@dataclass(frozen=True, slots=True)
class SessionData:
    user_id: int
    name: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class DiscordIdentity:
    user_id: int
    name: str


def _signature(secret: str, payload: bytes) -> bytes:
    return hmac.new(secret.encode(), payload, hashlib.sha256).digest()


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _sign_payload(secret: str, payload: dict[str, object]) -> str:
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return f"{_encode(raw)}.{_encode(_signature(secret, raw))}"


def _verify_payload(secret: str, value: str) -> dict[str, object] | None:
    parts = value.split(".")
    if len(parts) != 2:
        return None
    try:
        raw = _decode(parts[0])
        signature = _decode(parts[1])
    except (binascii.Error, ValueError):
        return None
    if not hmac.compare_digest(signature, _signature(secret, raw)):
        return None
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def sign_session(
    secret: str,
    user_id: int,
    name: str,
    expires_at: datetime,
) -> str:
    return _sign_payload(
        secret,
        {
            "uid": str(user_id),
            "name": name[:_MAX_SESSION_NAME_LENGTH],
            "exp": int(expires_at.timestamp()),
        },
    )


def verify_session(
    secret: str,
    value: str,
    now: datetime,
) -> SessionData | None:
    payload = _verify_payload(secret, value)
    if payload is None:
        return None
    user_id = payload.get("uid")
    name = payload.get("name")
    expires = payload.get("exp")
    if (
        not isinstance(user_id, str)
        or not user_id.isdigit()
        or not isinstance(name, str)
        or not isinstance(expires, int)
    ):
        return None
    expires_at = datetime.fromtimestamp(expires, UTC)
    if now >= expires_at:
        return None
    return SessionData(
        user_id=int(user_id),
        name=name,
        expires_at=expires_at,
    )


def sign_state(
    secret: str,
    now: datetime,
    *,
    consent_retry: bool = False,
) -> tuple[str, str]:
    """Return an opaque state token and its signed cookie value.

    ``consent_retry`` marks the state minted for the interactive retry after a
    silent authorization failed, so the callback can tell a first prompt from
    the retry and never bounce a user through the consent screen more than once.
    """
    token = secrets.token_urlsafe(32)
    cookie = _sign_payload(
        secret,
        {
            "state": token,
            "exp": int(now.timestamp()) + STATE_TTL_SECONDS,
            "cr": consent_retry,
        },
    )
    return token, cookie


def verify_state(
    secret: str,
    cookie_value: str,
    state: str,
    now: datetime,
) -> bool:
    payload = _verify_payload(secret, cookie_value)
    if payload is None:
        return False
    token = payload.get("state")
    expires = payload.get("exp")
    if not isinstance(token, str) or not isinstance(expires, int):
        return False
    if now >= datetime.fromtimestamp(expires, UTC):
        return False
    # state arrives straight from the query string, so it can hold any code
    # point. hmac.compare_digest raises TypeError on a non-ASCII str, which
    # would escape the callback as a 500 instead of a rejected sign-in, so
    # compare the encoded bytes.
    return hmac.compare_digest(token.encode(), state.encode())


def state_is_consent_retry(secret: str, cookie_value: str) -> bool:
    """Whether this signed state cookie was minted for the consent retry.

    Only the signature is re-checked here; expiry and the state match are the
    caller's job (via verify_state) before this is trusted.
    """
    payload = _verify_payload(secret, cookie_value)
    return bool(payload is not None and payload.get("cr") is True)


def authorize_url(
    client_id: str,
    redirect_uri: str,
    state: str,
    *,
    prompt_none: bool = True,
) -> str:
    params = {
        "client_id": client_id,
        "response_type": "code",
        "scope": "identify",
        "redirect_uri": redirect_uri,
        "state": state,
    }
    # prompt=none makes returning visits silent, but Discord then refuses a
    # first-time or logged-out user with a *_required error instead of showing
    # the screen. The callback retries with the prompt enabled, and that retry
    # must omit prompt=none so Discord actually renders the consent page.
    if prompt_none:
        params["prompt"] = "none"
    return f"{DISCORD_AUTHORIZE_URL}?{urlencode(params)}"


async def exchange_code(
    session: aiohttp.ClientSession,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    code: str,
) -> str:
    """Trade the authorization code for an access token.

    The token is returned to the caller for a single identity lookup and
    must never be persisted or logged.
    """
    async with session.post(
        DISCORD_TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
        },
    ) as response:
        if response.status != 200:
            LOGGER.warning(
                "Discord token exchange failed; status=%s",
                response.status,
            )
            raise OAuthExchangeError(
                f"Token exchange failed with status {response.status}"
            )
        payload = await response.json()
    token = payload.get("access_token") if isinstance(payload, dict) else None
    if not isinstance(token, str) or not token:
        LOGGER.warning("Discord token exchange returned no access token")
        raise OAuthExchangeError("Token exchange returned no access token")
    LOGGER.debug("Discord token exchange succeeded")
    return token


async def fetch_identity(
    session: aiohttp.ClientSession,
    access_token: str,
) -> DiscordIdentity:
    async with session.get(
        DISCORD_ME_URL,
        headers={"Authorization": f"Bearer {access_token}"},
    ) as response:
        if response.status != 200:
            LOGGER.warning(
                "Discord identity lookup failed; status=%s",
                response.status,
            )
            raise OAuthExchangeError(
                f"Identity lookup failed with status {response.status}"
            )
        payload = await response.json()
    if not isinstance(payload, dict):
        raise OAuthExchangeError("Identity lookup returned no user object")
    user_id = payload.get("id")
    if not isinstance(user_id, str) or not user_id.isdigit():
        raise OAuthExchangeError("Identity lookup returned no user id")
    name = payload.get("global_name") or payload.get("username") or "Unknown"
    if not isinstance(name, str):
        name = "Unknown"
    LOGGER.debug("Discord identity lookup succeeded; user_id=%s", user_id)
    return DiscordIdentity(user_id=int(user_id), name=name)
