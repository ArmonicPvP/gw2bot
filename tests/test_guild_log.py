import logging
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gw2bot.bot import Gw2Bot
from gw2bot.gw2 import guild_log
from gw2bot.raffle import RaffleDeposit


@pytest.mark.parametrize(
    ("sender", "getter", "marker"),
    [
        (
            "send_pending_gold_withdrawal_notifications",
            "get_pending_gold_withdrawal_notifications",
            "mark_gold_withdrawal_notification_sent",
        ),
        (
            "send_pending_deposit_audit_notifications",
            "get_pending_deposit_audit_notifications",
            "mark_deposit_audit_notification_sent",
        ),
        (
            "send_pending_leave_notifications",
            "get_pending_leave_notifications",
            "mark_leave_notification_sent",
        ),
        (
            "send_pending_join_notifications",
            "get_pending_join_notifications",
            "mark_join_notification_sent",
        ),
        (
            "send_pending_invite_notifications",
            "get_pending_invite_notifications",
            "mark_invite_notification_sent",
        ),
        (
            "send_pending_rank_change_notifications",
            "get_pending_rank_change_notifications",
            "mark_rank_change_notification_sent",
        ),
    ],
)
async def test_pending_delivery_marks_only_successes_in_send_order(
    sender: str,
    getter: str,
    marker: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = MagicMock()
    private_message = "private-notification-content"
    pending = [
        SimpleNamespace(event_id=index, message=f"{private_message}-{index}")
        for index in (1, 2, 3)
    ]
    getattr(store, getter).return_value = pending
    activity: list[tuple[str, object]] = []

    async def send(message: str) -> bool:
        activity.append(("send", message))
        return message != pending[1].message

    def mark(event_id: int) -> None:
        activity.append(("mark", event_id))

    getattr(store, marker).side_effect = mark
    bot = SimpleNamespace(
        _raffle_store=store, _try_send_notification=AsyncMock(side_effect=send)
    )
    caplog.set_level(logging.DEBUG)

    await getattr(guild_log, sender)(cast(Gw2Bot, bot))

    assert activity == [
        ("send", pending[0].message), ("mark", 1),
        ("send", pending[1].message),
        ("send", pending[2].message), ("mark", 3),
    ]
    assert private_message not in caplog.text


async def test_pending_delivery_propagates_a_failure_before_marking() -> None:
    store = MagicMock()
    store.get_pending_join_notifications.return_value = [
        SimpleNamespace(event_id=1, message="first"),
        SimpleNamespace(event_id=2, message="second"),
    ]
    bot = SimpleNamespace(
        _raffle_store=store,
        _try_send_notification=AsyncMock(side_effect=RuntimeError("delivery failed")),
    )

    with pytest.raises(RuntimeError, match="delivery failed"):
        await guild_log.send_pending_join_notifications(cast(Gw2Bot, bot))

    store.mark_join_notification_sent.assert_not_called()
    bot._try_send_notification.assert_awaited_once_with("first")


class TestGuildLogRefresh:
    async def test_unset_gw2_credentials_skip_the_refresh(self) -> None:
        # /raffle draw and the six-hourly report both refresh first, so an
        # unconfigured API has to leave them reading the persisted ledger
        # instead of raising.
        store = MagicMock()
        bot = SimpleNamespace(
            _api=None,
            _raffle_store=store,
            _config=SimpleNamespace(
                gw2_guild_id=None,
                missing_gw2_api_settings=("gw2_api_key", "gw2_guild_id"),
            ),
        )

        refreshed = await Gw2Bot.refresh_guild_log(cast(Gw2Bot, bot))

        # The caller's diagnostics need to record that the report it goes on
        # to post came from persisted data alone.
        assert not refreshed
        store.get_cursor.assert_not_called()
        store.process_events.assert_not_called()

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
            _config=SimpleNamespace(
                gw2_guild_id="guild-id",
                raffle_excluded_accounts=(),
            ),
        )

        refreshed = await Gw2Bot.refresh_guild_log(cast(Gw2Bot, bot))

        assert refreshed
        api.get_guild_log.assert_awaited_once_with("guild-id", 100)
        guild_members.usernames_with_rank.assert_awaited_once_with(
            "Officer",
            force_refresh=True,
        )
        store.process_events.assert_called_once_with(
            events,
            {"Officer.1234"},
            excluded_usernames=(),
        )
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
            _config=SimpleNamespace(
                gw2_guild_id="guild-id",
                raffle_excluded_accounts=(),
            ),
        )

        await Gw2Bot.refresh_guild_log(cast(Gw2Bot, bot))

        guild_members.usernames_with_rank.assert_not_awaited()
        store.process_events.assert_called_once_with(
            events,
            set(),
            excluded_usernames=(),
        )

    @patch("gw2bot.gw2.guild_log.asyncio.sleep", new_callable=AsyncMock)
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
            _send_pending_gold_withdrawal_notifications=AsyncMock(),
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
        bot._send_pending_gold_withdrawal_notifications.assert_awaited_once()
        bot._send_pending_deposit_audit_notifications.assert_awaited_once()
        bot._send_pending_invite_notifications.assert_awaited_once()
        bot._send_pending_rank_change_notifications.assert_awaited_once()
        bot._poll_status.record_success.assert_called_once_with("Guild Log")
        sleep.assert_awaited_once_with(60)


class TestSendPendingRaffleNotifications:
    async def test_skips_sub_gold_deposit_embed_but_marks_sent(self) -> None:
        deposit = RaffleDeposit(
            event_id=101,
            username="Jotunn.7612",
            coins_deposited=1_000,
            raffle_tickets=0,
            event_time="2026-07-15T00:00:00Z",
        )
        store = MagicMock()
        store.get_pending_notifications.return_value = [deposit]
        bot = SimpleNamespace(
            _raffle_store=store,
            _try_send_raffle_contribution_embed=AsyncMock(return_value=True),
        )

        await Gw2Bot._send_pending_raffle_notifications(cast(Gw2Bot, bot))

        bot._try_send_raffle_contribution_embed.assert_not_awaited()
        store.mark_notification_sent.assert_called_once_with(101)

    async def test_posts_embed_for_whole_gold_deposit(self) -> None:
        deposit = RaffleDeposit(
            event_id=102,
            username="Member.1234",
            coins_deposited=30_000,
            raffle_tickets=3,
            event_time="2026-07-15T00:00:00Z",
        )
        store = MagicMock()
        store.get_pending_notifications.return_value = [deposit]
        bot = SimpleNamespace(
            _raffle_store=store,
            _try_send_raffle_contribution_embed=AsyncMock(return_value=True),
        )

        await Gw2Bot._send_pending_raffle_notifications(cast(Gw2Bot, bot))

        bot._try_send_raffle_contribution_embed.assert_awaited_once()
        store.mark_notification_sent.assert_called_once_with(102)
