from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

from gw2bot.bot import Gw2Bot


class TestGuildLogRefresh:
    async def test_processes_new_events_before_returning(self) -> None:
        events = [
            {
                "id": 101,
                "type": "stash",
                "operation": "deposit",
                "user": "Officer.1234",
                "coins": 110_000,
            }
        ]
        api = SimpleNamespace(get_guild_log=AsyncMock(return_value=events))
        store = MagicMock()
        store.get_cursor.return_value = 100
        guild_members = SimpleNamespace(
            usernames_with_rank=AsyncMock(return_value={"Officer.1234"})
        )
        bot = SimpleNamespace(
            _api=api,
            _raffle_store=store,
            _guild_members=guild_members,
            _config=SimpleNamespace(gw2_guild_id="guild-id"),
        )

        await Gw2Bot.refresh_guild_log(cast(Gw2Bot, bot))

        api.get_guild_log.assert_awaited_once_with("guild-id", 100)
        guild_members.usernames_with_rank.assert_awaited_once_with(
            "Officer",
            force_refresh=True,
        )
        store.process_events.assert_called_once_with(events, {"Officer.1234"})
        store.initialize_cursor.assert_not_called()

    async def test_does_not_refresh_member_ranks_without_new_deposits(self) -> None:
        events = [{"id": 101, "type": "joined", "user": "Member.1234"}]
        api = SimpleNamespace(get_guild_log=AsyncMock(return_value=events))
        store = MagicMock()
        store.get_cursor.return_value = 100
        guild_members = SimpleNamespace(usernames_with_rank=AsyncMock())
        bot = SimpleNamespace(
            _api=api,
            _raffle_store=store,
            _guild_members=guild_members,
            _config=SimpleNamespace(gw2_guild_id="guild-id"),
        )

        await Gw2Bot.refresh_guild_log(cast(Gw2Bot, bot))

        guild_members.usernames_with_rank.assert_not_awaited()
        store.process_events.assert_called_once_with(events, set())

    @patch("gw2bot.guild_log.asyncio.sleep", new_callable=AsyncMock)
    async def test_guild_log_poller_sends_deposits_to_main_and_audit_channels(
        self,
        sleep: AsyncMock,
    ) -> None:
        bot = SimpleNamespace(
            wait_until_ready=AsyncMock(),
            is_closed=MagicMock(side_effect=[False, True]),
            _session=object(),
            _api=object(),
            refresh_guild_log=AsyncMock(),
            _send_pending_raffle_notifications=AsyncMock(),
            _send_pending_deposit_audit_notifications=AsyncMock(),
            _send_pending_raffle_milestones=AsyncMock(),
            _send_pending_join_notifications=AsyncMock(),
            _send_pending_leave_notifications=AsyncMock(),
            _send_pending_invite_notifications=AsyncMock(),
            _send_pending_rank_change_notifications=AsyncMock(),
            _poll_status=SimpleNamespace(
                record_error=MagicMock(),
                record_success=MagicMock(),
            ),
            _config=SimpleNamespace(guild_log_poll_interval_seconds=60),
        )

        await Gw2Bot._poll_guild_log(cast(Gw2Bot, bot))

        bot._send_pending_raffle_notifications.assert_awaited_once()
        bot._send_pending_deposit_audit_notifications.assert_awaited_once()
        bot._send_pending_invite_notifications.assert_awaited_once()
        bot._send_pending_rank_change_notifications.assert_awaited_once()
        bot._poll_status.record_success.assert_called_once_with("Guild Log")
        sleep.assert_awaited_once_with(60)
