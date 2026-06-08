import asyncio
import unittest
from datetime import UTC, datetime
from typing import Any

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


class Gw2ApiClientTests(unittest.TestCase):
    def test_get_guild_storage_uses_configured_guild_id(self) -> None:
        session = FakeSession([{"id": 1078, "count": 9}])
        client = Gw2ApiClient(session, "https://example.test", "secret")  # type: ignore[arg-type]

        result = asyncio.run(client.get_guild_storage("guild-id"))

        self.assertEqual(result, [{"id": 1078, "count": 9}])
        self.assertEqual(
            session.calls[0],
            {
                "url": "https://example.test/v2/guild/guild-id/storage",
                "headers": {"Authorization": "Bearer secret"},
                "params": None,
            },
        )

    def test_guild_log_uses_since_and_authorization_header(self) -> None:
        session = FakeSession([{"id": 43, "type": "joined"}])
        client = Gw2ApiClient(session, "https://example.test", "secret")  # type: ignore[arg-type]

        result = asyncio.run(client.get_guild_log("guild/id", since=42))

        self.assertEqual(result, [{"id": 43, "type": "joined"}])
        self.assertEqual(
            session.calls,
            [
                {
                    "url": "https://example.test/v2/guild/guild%2Fid/log",
                    "headers": {"Authorization": "Bearer secret"},
                    "params": {"since": "42"},
                }
            ],
        )
        self.assertTrue(session.response.raise_for_status_called)

    def test_create_subtoken_formats_restrictions(self) -> None:
        session = FakeSession({"subtoken": "value"})
        client = Gw2ApiClient(session, "https://example.test", "secret")  # type: ignore[arg-type]

        result = asyncio.run(
            client.create_subtoken(
                datetime(2027, 1, 1, tzinfo=UTC),
                permissions=("account", "guilds"),
                urls=("/v2/account", "/v2/guild/example/log"),
            )
        )

        self.assertEqual(result, {"subtoken": "value"})
        self.assertEqual(
            session.calls[0]["params"],
            {
                "expire": "2027-01-01T00:00:00+00:00",
                "permissions": "account,guilds",
                "urls": "/v2/account,/v2/guild/example/log",
            },
        )


if __name__ == "__main__":
    unittest.main()
