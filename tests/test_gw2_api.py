import logging
from datetime import UTC, datetime
from typing import Any

import pytest

from gw2bot.gw2_api import Gw2ApiClient


class FakeResponse:
    def __init__(self, payload: Any):
        self.payload = payload
        self.raise_for_status_called = False

    async def __aenter__(self) -> "FakeResponse":
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    def raise_for_status(self) -> None:
        self.raise_for_status_called = True

    async def json(self) -> Any:
        return self.payload


class FakeSession:
    def __init__(self, payload: Any):
        self.response = FakeResponse(payload)
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"url": url, **kwargs})
        return self.response


class TestGw2ApiClient:
    async def test_get_guild_storage_uses_configured_guild_id(self) -> None:
        session = FakeSession([{"id": 1078, "count": 9}])
        client = Gw2ApiClient(session, "https://example.test", "secret")  # type: ignore[arg-type]

        result = await client.get_guild_storage("guild-id")

        assert result == [{"id": 1078, "count": 9}]
        assert session.calls[0] == {
            "url": "https://example.test/v2/guild/guild-id/storage",
            "headers": {"Authorization": "Bearer secret"},
            "params": None,
        }

    async def test_guild_log_uses_since_and_authorization_header(self) -> None:
        session = FakeSession([{"id": 43, "type": "joined"}])
        client = Gw2ApiClient(session, "https://example.test", "secret")  # type: ignore[arg-type]

        result = await client.get_guild_log("guild/id", since=42)

        assert result == [{"id": 43, "type": "joined"}]
        assert session.calls == [
            {
                "url": "https://example.test/v2/guild/guild%2Fid/log",
                "headers": {"Authorization": "Bearer secret"},
                "params": {"since": "42"},
            }
        ]
        assert session.response.raise_for_status_called

    async def test_guild_diagnostics_log_route_without_credentials_or_query(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        guild_id = "9EF53BB4-C16C-EA11-81A8-A25FC8B1A2FE"
        api_key = "secret-api-key"
        session = FakeSession([])
        client = Gw2ApiClient(session, "https://example.test", api_key)  # type: ignore[arg-type]

        with caplog.at_level(logging.DEBUG, logger="gw2bot.gw2_api"):
            await client.get_guild_log(guild_id, since=42)

        assert f"/v2/guild/{guild_id}/log" in caplog.text
        assert api_key not in caplog.text
        assert "since=42" not in caplog.text

    async def test_create_subtoken_formats_restrictions(self) -> None:
        session = FakeSession({"subtoken": "value"})
        client = Gw2ApiClient(session, "https://example.test", "secret")  # type: ignore[arg-type]

        result = await client.create_subtoken(
            datetime(2027, 1, 1, tzinfo=UTC),
            permissions=("account", "guilds"),
            urls=("/v2/account", "/v2/guild/example/log"),
        )

        assert result == {"subtoken": "value"}
        assert session.calls[0]["params"] == {
            "expire": "2027-01-01T00:00:00+00:00",
            "permissions": "account,guilds",
            "urls": "/v2/account,/v2/guild/example/log",
        }
