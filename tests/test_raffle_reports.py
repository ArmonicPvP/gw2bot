import logging
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, call, patch

import discord
import pytest

from gw2bot.bot import Gw2Bot
from gw2bot.raffle import RaffleContribution
from gw2bot.raffle.formatting import raffle_contribution_report_embed
from gw2bot.raffle.reports import (
    RAFFLE_CONTRIBUTION_CHANNEL_ID,
    raffle_contribution_report_end,
    seconds_until_raffle_contribution_report,
)
from gw2bot.raffle.views import RaffleContributionReportView

from factories import raffle_total


class TestRaffleContributionNotification:
    def test_schedules_fixed_six_hour_utc_boundaries(self) -> None:
        now = datetime(2026, 6, 7, 5, 30, tzinfo=UTC)

        assert raffle_contribution_report_end(now) == datetime(
            2026,
            6,
            7,
            0,
            tzinfo=UTC,
        )
        assert seconds_until_raffle_contribution_report(now) == 30 * 60
        assert seconds_until_raffle_contribution_report(
            datetime(2026, 6, 7, 6, tzinfo=UTC)
        ) == 6 * 60 * 60

    def test_formats_contributors_as_mobile_friendly_blocks(self) -> None:
        contributions = [
            RaffleContribution("Alpha.1234", 2, 1),
            RaffleContribution("Beta.1234", 0, 2),
        ]

        embed = raffle_contribution_report_embed(contributions, 0)
        description = embed.description or ""

        assert embed.title == "Raffle contributions from the last 6 hours"
        assert description == (
            "**Alpha.1234**\n"
            "Purchased: 2\n"
            "Free: 1\n"
            "Total: 3\n\n"
            "**Beta.1234**\n"
            "Purchased: 0\n"
            "Free: 2\n"
            "Total: 2"
        )

    async def test_empty_window_does_not_send_message(self) -> None:
        report_end = datetime(2026, 6, 7, 6, tzinfo=UTC)
        bot = SimpleNamespace(
            get_raffle_contributions=MagicMock(return_value=[]),
            _send_raffle_contribution_embed=AsyncMock(),
        )

        await Gw2Bot._send_raffle_contribution_report(
            cast(Gw2Bot, bot),
            report_end,
        )

        bot.get_raffle_contributions.assert_called_once_with(
            datetime(2026, 6, 7, 0, tzinfo=UTC),
            report_end,
        )
        bot._send_raffle_contribution_embed.assert_not_awaited()

    async def test_free_ticket_only_window_sends_embed(self) -> None:
        report_end = datetime(2026, 6, 7, 6, tzinfo=UTC)
        bot = SimpleNamespace(
            get_raffle_contributions=MagicMock(
                return_value=[RaffleContribution("Free Only.1234", 0, 1)]
            ),
            _send_raffle_contribution_embed=AsyncMock(),
        )

        await Gw2Bot._send_raffle_contribution_report(
            cast(Gw2Bot, bot),
            report_end,
        )

        bot._send_raffle_contribution_embed.assert_awaited_once()
        embed, view = bot._send_raffle_contribution_embed.await_args.args
        assert view is None
        assert (
            embed.description
            == "**Free Only.1234**\nPurchased: 0\nFree: 1\nTotal: 1"
        )

    async def test_contribution_report_paginates_ten_users_at_a_time(self) -> None:
        report_end = datetime(2026, 6, 7, 6, tzinfo=UTC)
        contributions = [
            RaffleContribution(f"Member {index:02d}.1234", index, 0)
            for index in range(11)
        ]
        bot = SimpleNamespace(
            get_raffle_contributions=MagicMock(return_value=contributions),
            _send_raffle_contribution_embed=AsyncMock(),
        )

        await Gw2Bot._send_raffle_contribution_report(
            cast(Gw2Bot, bot),
            report_end,
        )

        embed, view = bot._send_raffle_contribution_embed.await_args.args
        assert isinstance(view, RaffleContributionReportView)
        assert "Member 09.1234" in (embed.description or "")
        assert "Member 10.1234" not in (embed.description or "")

        interaction = SimpleNamespace(
            response=SimpleNamespace(edit_message=AsyncMock()),
        )
        await view.change_page(interaction, 1)  # type: ignore[arg-type]

        second_embed = interaction.response.edit_message.await_args.kwargs["embed"]
        assert "Member 10.1234" in (second_embed.description or "")
        assert "Member 09.1234" not in (second_embed.description or "")

    async def test_sends_pending_purchase_messages_to_raffle_channel(self) -> None:
        deposit = SimpleNamespace(event_id=101, message="purchase message")
        store = MagicMock()
        store.get_pending_notifications.return_value = [deposit]
        bot = SimpleNamespace(
            _raffle_store=store,
            _try_send_raffle_contribution_message=AsyncMock(return_value=True),
        )

        await Gw2Bot._send_pending_raffle_notifications(cast(Gw2Bot, bot))

        bot._try_send_raffle_contribution_message.assert_awaited_once_with(
            "purchase message"
        )
        store.mark_notification_sent.assert_called_once_with(101)

    async def test_officer_purchase_attempts_all_purchase_deliveries(self) -> None:
        total = raffle_total("Member.1234", purchased=3)
        store = MagicMock()
        store.add_officer_purchase.return_value = total
        bot = SimpleNamespace(
            _raffle_store=store,
            _send_pending_raffle_notifications=AsyncMock(),
            _send_pending_deposit_audit_notifications=AsyncMock(),
            _send_pending_raffle_milestones=AsyncMock(),
        )

        result = await Gw2Bot.add_officer_raffle_purchase(
            cast(Gw2Bot, bot),
            "Member.1234",
            3,
        )

        assert result == total
        store.add_officer_purchase.assert_called_once_with("Member.1234", 3)
        bot._send_pending_raffle_notifications.assert_awaited_once()
        bot._send_pending_deposit_audit_notifications.assert_awaited_once()
        bot._send_pending_raffle_milestones.assert_awaited_once()

    async def test_sends_pending_deposit_audits_to_notification_channel(self) -> None:
        deposit = SimpleNamespace(event_id=101, message="purchase message")
        store = MagicMock()
        store.get_pending_deposit_audit_notifications.return_value = [deposit]
        bot = SimpleNamespace(
            _raffle_store=store,
            _try_send_notification=AsyncMock(return_value=True),
        )

        await Gw2Bot._send_pending_deposit_audit_notifications(cast(Gw2Bot, bot))

        bot._try_send_notification.assert_awaited_once_with("purchase message")
        store.mark_deposit_audit_notification_sent.assert_called_once_with(101)

    async def test_retries_pending_deposit_audit_after_delivery_failure(self) -> None:
        deposit = SimpleNamespace(event_id=101, message="purchase message")
        store = MagicMock()
        store.get_pending_deposit_audit_notifications.return_value = [deposit]
        bot = SimpleNamespace(
            _raffle_store=store,
            _try_send_notification=AsyncMock(return_value=False),
        )

        await Gw2Bot._send_pending_deposit_audit_notifications(cast(Gw2Bot, bot))

        store.mark_deposit_audit_notification_sent.assert_not_called()

    async def test_sends_pending_join_messages_to_notification_channel(self) -> None:
        join = SimpleNamespace(event_id=101, message="join message")
        store = MagicMock()
        store.get_pending_join_notifications.return_value = [join]
        bot = SimpleNamespace(
            _raffle_store=store,
            _try_send_notification=AsyncMock(return_value=True),
        )

        await Gw2Bot._send_pending_join_notifications(cast(Gw2Bot, bot))

        bot._try_send_notification.assert_awaited_once_with("join message")
        store.mark_join_notification_sent.assert_called_once_with(101)

    async def test_retries_pending_join_after_delivery_failure(self) -> None:
        join = SimpleNamespace(event_id=101, message="join message")
        store = MagicMock()
        store.get_pending_join_notifications.return_value = [join]
        bot = SimpleNamespace(
            _raffle_store=store,
            _try_send_notification=AsyncMock(return_value=False),
        )

        await Gw2Bot._send_pending_join_notifications(cast(Gw2Bot, bot))

        store.mark_join_notification_sent.assert_not_called()

    async def test_sends_pending_invite_messages_to_notification_channel(
        self,
    ) -> None:
        invite = SimpleNamespace(event_id=101, message="invite message")
        store = MagicMock()
        store.get_pending_invite_notifications.return_value = [invite]
        bot = SimpleNamespace(
            _raffle_store=store,
            _try_send_notification=AsyncMock(return_value=True),
        )

        await Gw2Bot._send_pending_invite_notifications(cast(Gw2Bot, bot))

        bot._try_send_notification.assert_awaited_once_with("invite message")
        store.mark_invite_notification_sent.assert_called_once_with(101)

    async def test_retries_pending_invite_after_delivery_failure(self) -> None:
        invite = SimpleNamespace(event_id=101, message="invite message")
        store = MagicMock()
        store.get_pending_invite_notifications.return_value = [invite]
        bot = SimpleNamespace(
            _raffle_store=store,
            _try_send_notification=AsyncMock(return_value=False),
        )

        await Gw2Bot._send_pending_invite_notifications(cast(Gw2Bot, bot))

        store.mark_invite_notification_sent.assert_not_called()

    async def test_sends_pending_rank_change_messages_to_notification_channel(
        self,
    ) -> None:
        rank_change = SimpleNamespace(event_id=101, message="rank change message")
        store = MagicMock()
        store.get_pending_rank_change_notifications.return_value = [rank_change]
        bot = SimpleNamespace(
            _raffle_store=store,
            _try_send_notification=AsyncMock(return_value=True),
        )

        await Gw2Bot._send_pending_rank_change_notifications(cast(Gw2Bot, bot))

        bot._try_send_notification.assert_awaited_once_with("rank change message")
        store.mark_rank_change_notification_sent.assert_called_once_with(101)

    async def test_retries_pending_rank_change_after_delivery_failure(self) -> None:
        rank_change = SimpleNamespace(event_id=101, message="rank change message")
        store = MagicMock()
        store.get_pending_rank_change_notifications.return_value = [rank_change]
        bot = SimpleNamespace(
            _raffle_store=store,
            _try_send_notification=AsyncMock(return_value=False),
        )

        await Gw2Bot._send_pending_rank_change_notifications(cast(Gw2Bot, bot))

        store.mark_rank_change_notification_sent.assert_not_called()

    async def test_sends_pending_milestones_to_raffle_channel(self) -> None:
        milestone = SimpleNamespace(threshold=50, message="milestone message")
        store = MagicMock()
        store.get_pending_milestones.return_value = [milestone]
        bot = SimpleNamespace(
            _raffle_store=store,
            _try_send_raffle_contribution_message=AsyncMock(return_value=True),
        )

        await Gw2Bot._send_pending_raffle_milestones(cast(Gw2Bot, bot))

        bot._try_send_raffle_contribution_message.assert_awaited_once_with(
            "milestone message"
        )
        store.mark_milestone_notification_sent.assert_called_once_with(50)

    async def test_retries_pending_milestone_after_delivery_failure(self) -> None:
        milestone = SimpleNamespace(threshold=50, message="milestone message")
        store = MagicMock()
        store.get_pending_milestones.return_value = [milestone]
        bot = SimpleNamespace(
            _raffle_store=store,
            _try_send_raffle_contribution_message=AsyncMock(return_value=False),
        )

        await Gw2Bot._send_pending_raffle_milestones(cast(Gw2Bot, bot))

        store.mark_milestone_notification_sent.assert_not_called()

    async def test_retries_pending_purchase_after_raffle_channel_failure(
        self,
    ) -> None:
        deposit = SimpleNamespace(event_id=101, message="purchase message")
        store = MagicMock()
        store.get_pending_notifications.return_value = [deposit]
        bot = SimpleNamespace(
            _raffle_store=store,
            _try_send_raffle_contribution_message=AsyncMock(return_value=False),
        )

        await Gw2Bot._send_pending_raffle_notifications(cast(Gw2Bot, bot))

        store.mark_notification_sent.assert_not_called()

    async def test_raffle_channel_failure_does_not_log_credentials(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        secret = "raffle-channel-secret"
        bot = SimpleNamespace(
            _send_raffle_contribution_message=AsyncMock(
                side_effect=discord.ClientException(secret)
            ),
        )

        with caplog.at_level(logging.ERROR, logger="gw2bot"):
            sent = await Gw2Bot._try_send_raffle_contribution_message(
                cast(Gw2Bot, bot),
                "purchase message",
            )

        assert not sent
        assert secret not in caplog.text
        assert "Could not send raffle contribution message" in caplog.text

    async def test_sends_report_to_configured_gw2_chat_and_caches_channel(
        self,
    ) -> None:
        channel = SimpleNamespace(
            guild=SimpleNamespace(id=5678),
            send=AsyncMock(),
        )
        bot = SimpleNamespace(
            _raffle_contribution_channel=None,
            _config=SimpleNamespace(discord_command_guild_id=5678),
            fetch_channel=AsyncMock(return_value=channel),
        )

        async def get_channel() -> Any:
            return await Gw2Bot._get_raffle_contribution_channel(
                cast(Gw2Bot, bot)
            )

        bot._get_raffle_contribution_channel = get_channel

        await Gw2Bot._send_raffle_contribution_message(
            cast(Gw2Bot, bot),
            "first",
        )
        await Gw2Bot._send_raffle_contribution_message(
            cast(Gw2Bot, bot),
            "second",
        )

        bot.fetch_channel.assert_awaited_once_with(RAFFLE_CONTRIBUTION_CHANNEL_ID)
        assert channel.send.await_args_list == [call("first"), call("second")]

    async def test_sends_contribution_embed_with_pagination_view(self) -> None:
        channel = SimpleNamespace(
            guild=SimpleNamespace(id=5678),
            send=AsyncMock(),
        )
        bot = SimpleNamespace(
            _get_raffle_contribution_channel=AsyncMock(return_value=channel),
        )
        embed = discord.Embed(title="Report")
        view = discord.ui.View()

        await Gw2Bot._send_raffle_contribution_embed(
            cast(Gw2Bot, bot),
            embed,
            view,
        )

        channel.send.assert_awaited_once_with(embed=embed, view=view)
        bot._get_raffle_contribution_channel.assert_awaited_once_with()

    @patch("gw2bot.raffle.reports.raffle_contribution_report_end")
    @patch("gw2bot.raffle.reports.seconds_until_raffle_contribution_report", return_value=123)
    @patch("gw2bot.raffle.reports.asyncio.sleep", new_callable=AsyncMock)
    async def test_poller_refreshes_guild_log_at_scheduled_boundary(
        self,
        sleep: AsyncMock,
        seconds_until_report: MagicMock,
        report_end: MagicMock,
    ) -> None:
        boundary = datetime(2026, 6, 7, 6, tzinfo=UTC)
        report_end.return_value = boundary
        bot = SimpleNamespace(
            wait_until_ready=AsyncMock(),
            is_closed=MagicMock(side_effect=[False, False, True]),
            refresh_guild_log=AsyncMock(),
            _send_raffle_contribution_report=AsyncMock(),
            _poll_status=SimpleNamespace(
                record_error=MagicMock(),
                record_success=MagicMock(),
            ),
        )

        await Gw2Bot._poll_raffle_contributions(bot)  # type: ignore[arg-type]

        sleep.assert_awaited_once_with(123)
        bot.refresh_guild_log.assert_awaited_once()
        bot._send_raffle_contribution_report.assert_awaited_once_with(boundary)
        bot._poll_status.record_success.assert_called_once_with("Raffle Contributions")
        bot._poll_status.record_error.assert_not_called()

    @patch("gw2bot.raffle.reports.raffle_contribution_report_end")
    @patch("gw2bot.raffle.reports.seconds_until_raffle_contribution_report", return_value=123)
    @patch("gw2bot.raffle.reports.asyncio.sleep", new_callable=AsyncMock)
    async def test_poller_posts_persisted_report_after_refresh_timeout(
        self,
        sleep: AsyncMock,
        seconds_until_report: MagicMock,
        report_end: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        boundary = datetime(2026, 6, 7, 6, tzinfo=UTC)
        report_end.return_value = boundary
        bot = SimpleNamespace(
            wait_until_ready=AsyncMock(),
            is_closed=MagicMock(side_effect=[False, False, True]),
            refresh_guild_log=AsyncMock(side_effect=TimeoutError("secret-timeout")),
            _send_raffle_contribution_report=AsyncMock(),
            _poll_status=SimpleNamespace(
                record_error=MagicMock(),
                record_success=MagicMock(),
            ),
        )

        with caplog.at_level(logging.DEBUG, logger="gw2bot"):
            await Gw2Bot._poll_raffle_contributions(bot)  # type: ignore[arg-type]

        sleep.assert_awaited_once_with(123)
        bot.refresh_guild_log.assert_awaited_once()
        bot._send_raffle_contribution_report.assert_awaited_once_with(boundary)
        bot._poll_status.record_success.assert_called_once_with("Raffle Contributions")
        bot._poll_status.record_error.assert_not_called()
        assert "secret-timeout" not in caplog.text
        assert (
            "Raffle Contributions guild-log refresh failed; posting persisted "
            "report; error_type=TimeoutError"
            in caplog.text
        )
        assert (
            "Raffle Contributions poll completed successfully; "
            "guild_log_refreshed=False"
            in caplog.text
        )

    @patch("gw2bot.raffle.reports.raffle_contribution_report_end")
    @patch("gw2bot.raffle.reports.seconds_until_raffle_contribution_report", return_value=123)
    @patch("gw2bot.raffle.reports.asyncio.sleep", new_callable=AsyncMock)
    async def test_poller_reports_actual_contribution_delivery_timeout(
        self,
        sleep: AsyncMock,
        seconds_until_report: MagicMock,
        report_end: MagicMock,
    ) -> None:
        boundary = datetime(2026, 6, 7, 6, tzinfo=UTC)
        report_end.return_value = boundary
        error = TimeoutError("Discord unavailable")
        bot = SimpleNamespace(
            wait_until_ready=AsyncMock(),
            is_closed=MagicMock(side_effect=[False, False, True]),
            refresh_guild_log=AsyncMock(),
            _send_raffle_contribution_report=AsyncMock(side_effect=error),
            _poll_status=SimpleNamespace(
                record_error=MagicMock(),
                record_success=MagicMock(),
            ),
        )

        await Gw2Bot._poll_raffle_contributions(bot)  # type: ignore[arg-type]

        sleep.assert_awaited_once_with(123)
        bot._poll_status.record_error.assert_called_once_with(
            "Raffle Contributions",
            error,
        )
        bot._poll_status.record_success.assert_not_called()
