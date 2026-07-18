import logging
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, call

import discord
import pytest

from gw2bot.bot import Gw2Bot
from gw2bot.guild_storage import handle_storage


class TestFeastNotification:
    async def test_sends_same_feast_message_to_channel_and_private_user(self) -> None:
        message = "Guild Storage is low on **Food**: 5 left"
        private_message = AsyncMock()
        bot = SimpleNamespace(
            _config=SimpleNamespace(discord_feast_notification_user_id=3456),
            _try_send_notification=AsyncMock(return_value=True),
            _send_feast_private_message=private_message,
        )

        sent = await Gw2Bot._try_send_feast_notification(
            cast(Gw2Bot, bot),
            message,
        )

        assert sent
        bot._try_send_notification.assert_awaited_once_with(message)
        private_message.assert_awaited_once_with(message)

    async def test_feast_private_message_fetches_configured_user_once(self) -> None:
        message = "Guild Storage is low on **Food**: 5 left"
        private_user = SimpleNamespace(send=AsyncMock())
        bot = SimpleNamespace(
            _config=SimpleNamespace(discord_feast_notification_user_id=3456),
            _feast_notification_user=None,
            fetch_user=AsyncMock(return_value=private_user),
        )

        await Gw2Bot._send_feast_private_message(cast(Gw2Bot, bot), message)
        await Gw2Bot._send_feast_private_message(cast(Gw2Bot, bot), message)

        bot.fetch_user.assert_awaited_once_with(3456)
        assert private_user.send.await_args_list == [call(message)] * 2

    async def test_skips_private_message_when_not_configured(self) -> None:
        bot = SimpleNamespace(
            _config=SimpleNamespace(discord_feast_notification_user_id=None),
            _try_send_notification=AsyncMock(return_value=True),
        )

        sent = await Gw2Bot._try_send_feast_notification(
            cast(Gw2Bot, bot),
            "food alert",
        )

        assert sent
        bot._try_send_notification.assert_awaited_once_with("food alert")

    async def test_does_not_private_message_when_channel_send_fails(self) -> None:
        private_message = AsyncMock()
        bot = SimpleNamespace(
            _config=SimpleNamespace(discord_feast_notification_user_id=3456),
            _try_send_notification=AsyncMock(return_value=False),
            _send_feast_private_message=private_message,
        )

        sent = await Gw2Bot._try_send_feast_notification(
            cast(Gw2Bot, bot),
            "food alert",
        )

        assert not sent
        private_message.assert_not_awaited()

    async def test_private_message_failure_does_not_repeat_channel_alert(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        bot = SimpleNamespace(
            _config=SimpleNamespace(discord_feast_notification_user_id=3456),
            _try_send_notification=AsyncMock(return_value=True),
            _send_feast_private_message=AsyncMock(
                side_effect=discord.ClientException("DM unavailable")
            ),
        )

        with caplog.at_level(logging.ERROR, logger="gw2bot"):
            sent = await Gw2Bot._try_send_feast_notification(
                cast(Gw2Bot, bot),
                "food alert",
            )

        assert sent


class FakeFeastStore:
    """Records how the feast-count cache seeds and writes are exercised."""

    def __init__(self, initial_counts: dict[int, int]) -> None:
        self._counts = dict(initial_counts)
        self.seed_reads = 0
        self.recorded: list[dict[int, int]] = []

    def get_feast_alert_times(self) -> dict[int, float]:
        return {}

    def clear_feast_alert(self, guild_storage_id: int) -> None:
        pass

    def mark_feast_alert_sent(
        self,
        guild_storage_id: int,
        notification_time: float,
    ) -> None:
        pass

    def get_last_feast_counts(self) -> dict[int, int]:
        self.seed_reads += 1
        return dict(self._counts)

    def record_feast_counts(
        self,
        counts: dict[int, int],
        recorded_at: float,
    ) -> None:
        self.recorded.append(dict(counts))
        self._counts.update(counts)


def _storage(**counts: int) -> list[dict[str, Any]]:
    return [
        {"id": int(feast_id), "count": count}
        for feast_id, count in counts.items()
    ]


class TestHandleStorageFeastCounts:
    async def test_seeds_once_then_logs_only_changed_counts(self) -> None:
        # The database already knows one feast's count; the rest are new.
        store = FakeFeastStore({1078: 50})
        bot = cast(
            Gw2Bot,
            SimpleNamespace(
                _raffle_store=store,
                _feast_counts=None,
                _try_send_feast_notification=AsyncMock(return_value=True),
            ),
        )
        full = _storage(**{"1078": 50, "1089": 40, "1102": 30, "1112": 20})

        # First poll: seed from the DB, log every feast that differs from it.
        await handle_storage(bot, full)
        # Second poll with identical counts: nothing changed, nothing logged.
        await handle_storage(bot, full)
        # Third poll: only one feast drops, so only that feast is logged.
        await handle_storage(
            bot,
            _storage(**{"1078": 0, "1089": 40, "1102": 30, "1112": 20}),
        )

        # Seeded from the database exactly once; later polls use the cache.
        assert store.seed_reads == 1
        assert store.recorded == [
            {1089: 40, 1102: 30, 1112: 20},
            {1078: 0},
        ]
        assert bot._feast_counts == {1078: 0, 1089: 40, 1102: 30, 1112: 20}

    async def test_missing_feast_is_ignored_not_logged_as_zero(self) -> None:
        store = FakeFeastStore({})
        bot = cast(
            Gw2Bot,
            SimpleNamespace(
                _raffle_store=store,
                _feast_counts=None,
                _try_send_feast_notification=AsyncMock(return_value=True),
            ),
        )

        await handle_storage(
            bot,
            _storage(**{"1078": 50, "1089": 40, "1102": 30, "1112": 20}),
        )
        # A later poll omits one feast entirely (unknown, not empty).
        await handle_storage(
            bot,
            _storage(**{"1078": 50, "1089": 40, "1102": 30}),
        )

        # The missing feast produced no new row, and its last-known count stays
        # cached rather than being overwritten with 0.
        assert store.recorded == [{1078: 50, 1089: 40, 1102: 30, 1112: 20}]
        assert bot._feast_counts == {1078: 50, 1089: 40, 1102: 30, 1112: 20}
