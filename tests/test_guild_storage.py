import logging
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, call

import discord
import pytest

from gw2bot.bot import Gw2Bot


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
