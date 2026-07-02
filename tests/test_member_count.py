import logging
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import discord
import pytest

from gw2bot.bot import Gw2Bot
from gw2bot.member_count import (
    GUILD_MEMBER_COUNT_TOPIC_UPDATE_SECONDS,
    count_active_guild_members,
    format_guild_member_count_topic,
)


class GuildMemberCountTopicBot(SimpleNamespace):
    async def _try_update_logging_channel_topic(self, topic: str) -> bool:
        return await Gw2Bot._try_update_logging_channel_topic(
            cast(Gw2Bot, self),
            topic,
        )

    async def _get_notification_channel(self) -> Any:
        return await Gw2Bot._get_notification_channel(cast(Gw2Bot, self))


class TestGuildMemberCountTopic:
    def test_formats_guild_member_count_topic(self) -> None:
        assert format_guild_member_count_topic(493, 5) == "493/500 (5 pending)"

    def test_counts_invited_guild_records_as_pending(self) -> None:
        assert count_active_guild_members(
            [
                {"name": "One.1234", "rank": "Member"},
                {"name": "Two.5678", "rank": " invited "},
                {"name": "Three.9012", "rank": "Invited"},
            ]
        ) == (1, 2)

    async def test_updates_logging_channel_description_with_member_count(self) -> None:
        updated_channel = SimpleNamespace(topic="2/500 (1 pending)")
        channel = SimpleNamespace(
            id=9012,
            guild=SimpleNamespace(id=5678),
            topic="old",
            edit=AsyncMock(return_value=updated_channel),
        )
        api = SimpleNamespace(
            get_guild_members=AsyncMock(
                return_value=[
                    {"name": "One.1234", "rank": "Member"},
                    {"name": "Two.5678", "rank": "Officer"},
                    {"name": "Pending.9012", "rank": "invited"},
                ]
            )
        )
        bot = GuildMemberCountTopicBot(
            _api=api,
            _config=SimpleNamespace(
                gw2_guild_id="guild-id",
                discord_notification_channel_id=9012,
                discord_command_guild_id=5678,
            ),
            _notification_channel=channel,
            _last_guild_member_count=None,
            _last_pending_guild_invite_count=None,
            _last_topic_update_failure=None,
        )

        updated = await Gw2Bot._update_guild_member_count_topic(
            cast(Gw2Bot, bot)
        )

        assert updated
        assert bot._last_guild_member_count == 2
        assert bot._last_pending_guild_invite_count == 1
        api.get_guild_members.assert_awaited_once_with("guild-id")
        channel.edit.assert_awaited_once_with(
            topic="2/500 (1 pending)",
            reason="Update GW2 guild member count",
        )
        assert bot._notification_channel is updated_channel

    async def test_skips_logging_channel_update_when_description_is_current(
        self,
    ) -> None:
        channel = SimpleNamespace(
            id=9012,
            guild=SimpleNamespace(id=5678),
            topic="3/500 (1 pending)",
            edit=AsyncMock(),
        )
        api = SimpleNamespace(
            get_guild_members=AsyncMock(
                return_value=[
                    {"name": "One.1234", "rank": "Member"},
                    {"name": "Two.5678", "rank": "Member"},
                    {"name": "Three.9012", "rank": "Officer"},
                    {"name": "Pending.1234", "rank": "invited"},
                ]
            )
        )
        bot = GuildMemberCountTopicBot(
            _api=api,
            _config=SimpleNamespace(
                gw2_guild_id="guild-id",
                discord_notification_channel_id=9012,
                discord_command_guild_id=5678,
            ),
            _notification_channel=channel,
            _last_guild_member_count=None,
            _last_pending_guild_invite_count=None,
            _last_topic_update_failure=None,
        )

        updated = await Gw2Bot._update_guild_member_count_topic(
            cast(Gw2Bot, bot)
        )

        assert updated
        assert bot._last_guild_member_count == 3
        assert bot._last_pending_guild_invite_count == 1
        channel.edit.assert_not_awaited()

    async def test_channel_update_failure_logging_omits_raw_exception_body(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        secret = "raw-topic-update-secret"

        class DiscordFailure(discord.DiscordException):
            status = 403
            code = 50013

            def __str__(self) -> str:
                return secret

        channel = SimpleNamespace(
            id=9012,
            guild=SimpleNamespace(id=5678),
            topic="old",
            edit=AsyncMock(side_effect=DiscordFailure()),
        )
        api = SimpleNamespace(
            get_guild_members=AsyncMock(
                return_value=[
                    {"name": "One.1234", "rank": "Member"},
                    {"name": "Pending.1234", "rank": "invited"},
                ]
            )
        )
        bot = GuildMemberCountTopicBot(
            _api=api,
            _config=SimpleNamespace(
                gw2_guild_id="guild-id",
                discord_notification_channel_id=9012,
                discord_command_guild_id=5678,
            ),
            _notification_channel=channel,
            _last_guild_member_count=None,
            _last_pending_guild_invite_count=None,
            _last_topic_update_failure=None,
        )

        with caplog.at_level(logging.ERROR, logger="gw2bot"):
            updated = await Gw2Bot._update_guild_member_count_topic(
                cast(Gw2Bot, bot)
            )

        assert not updated
        assert bot._last_guild_member_count == 1
        assert bot._last_pending_guild_invite_count == 1
        assert secret not in caplog.text
        assert "type=DiscordFailure status=403 code=50013" in caplog.text

    async def test_repeated_topic_update_failures_log_once(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        class DiscordFailure(discord.DiscordException):
            status = 403
            code = 50013

        channel = SimpleNamespace(
            id=9012,
            guild=SimpleNamespace(id=5678),
            topic="old",
            edit=AsyncMock(side_effect=DiscordFailure()),
        )
        bot = GuildMemberCountTopicBot(
            _config=SimpleNamespace(
                discord_notification_channel_id=9012,
                discord_command_guild_id=5678,
            ),
            _notification_channel=channel,
            _last_topic_update_failure=None,
        )

        with caplog.at_level(logging.ERROR, logger="gw2bot"):
            assert not await Gw2Bot._try_update_logging_channel_topic(
                cast(Gw2Bot, bot), "1/500 (0 pending)"
            )
            assert not await Gw2Bot._try_update_logging_channel_topic(
                cast(Gw2Bot, bot), "1/500 (0 pending)"
            )

        assert channel.edit.await_count == 2
        assert (
            caplog.text.count("Could not update logging channel description") == 1
        )

    async def test_topic_update_recovery_is_logged_after_failure(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        class DiscordFailure(discord.DiscordException):
            status = 403
            code = 50013

        updated_channel = SimpleNamespace(topic="1/500 (0 pending)")
        channel = SimpleNamespace(
            id=9012,
            guild=SimpleNamespace(id=5678),
            topic="old",
            edit=AsyncMock(side_effect=[DiscordFailure(), updated_channel]),
        )
        bot = GuildMemberCountTopicBot(
            _config=SimpleNamespace(
                discord_notification_channel_id=9012,
                discord_command_guild_id=5678,
            ),
            _notification_channel=channel,
            _last_topic_update_failure=None,
        )

        assert not await Gw2Bot._try_update_logging_channel_topic(
            cast(Gw2Bot, bot), "1/500 (0 pending)"
        )
        assert bot._last_topic_update_failure is not None

        with caplog.at_level(logging.INFO, logger="gw2bot"):
            assert await Gw2Bot._try_update_logging_channel_topic(
                cast(Gw2Bot, bot), "1/500 (0 pending)"
            )

        assert bot._last_topic_update_failure is None
        assert "Logging channel description update recovered" in caplog.text

    @patch("gw2bot.member_count.asyncio.sleep", new_callable=AsyncMock)
    async def test_poller_updates_topic_every_minute(self, sleep: AsyncMock) -> None:
        bot = SimpleNamespace(
            wait_until_ready=AsyncMock(),
            is_closed=MagicMock(side_effect=[False, True]),
            _api=object(),
            _update_guild_member_count_topic=AsyncMock(return_value=True),
            _poll_status=SimpleNamespace(
                record_error=MagicMock(),
                record_success=MagicMock(),
            ),
        )

        await Gw2Bot._poll_guild_member_count_topic(cast(Gw2Bot, bot))

        bot._update_guild_member_count_topic.assert_awaited_once()
        bot._poll_status.record_success.assert_called_once_with("Guild Member Count")
        bot._poll_status.record_error.assert_not_called()
        sleep.assert_awaited_once_with(GUILD_MEMBER_COUNT_TOPIC_UPDATE_SECONDS)

    @patch("gw2bot.member_count.asyncio.sleep", new_callable=AsyncMock)
    async def test_poller_reports_member_count_api_failure(
        self,
        sleep: AsyncMock,
    ) -> None:
        error = aiohttp.ClientError("GW2 unavailable")
        bot = SimpleNamespace(
            wait_until_ready=AsyncMock(),
            is_closed=MagicMock(side_effect=[False, True]),
            _api=object(),
            _update_guild_member_count_topic=AsyncMock(side_effect=error),
            _poll_status=SimpleNamespace(
                record_error=MagicMock(),
                record_success=MagicMock(),
            ),
        )

        await Gw2Bot._poll_guild_member_count_topic(cast(Gw2Bot, bot))

        bot._poll_status.record_error.assert_called_once_with(
            "Guild Member Count",
            error,
        )
        bot._poll_status.record_success.assert_not_called()
        sleep.assert_awaited_once_with(GUILD_MEMBER_COUNT_TOPIC_UPDATE_SECONDS)
