import logging
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from factories import forbidden_error
from gw2bot.bot import Gw2Bot
from gw2bot.notifications import format_automated_message_diagnostics
from gw2bot.raffle import RaffleContribution, RaffleTotal
from gw2bot.raffle.formatting import format_raffle_milestone_preview


class TestAutomatedMessageDiagnostics:
    def test_formats_all_non_command_automated_message_previews(self) -> None:
        messages = format_automated_message_diagnostics(
            [RaffleContribution("Free Only.1234", 0, 1)],
            purchased_tickets=125,
        )
        output = "\n".join(messages)

        assert (
            "DiagnosticUser.1234 deposited 3 gold and purchased 3 raffle tickets"
            in output
        )
        assert "DiagnosticUser.1234 has joined the guild." in output
        assert "DiagnosticUser.1234 has left the guild." in output
        assert (
            "Officer.5678 invited DiagnosticUser.1234 to the guild." in output
        )
        assert (
            "Officer.5678 changed DiagnosticUser.1234's guild rank "
            "from Trial to Sunborne." in output
        )
        assert (
            "150 total tickets have been purchased for this raffle. "
            "Tier 3 rewards have been reached!"
        ) in output
        assert "Guild Storage is low on **Diagnostic Feast**: 5 left" in output
        assert "Trial members past the 14-day mark" in output
        assert "Trial members past the 7-day warning mark (to be kicked)" in output
        assert (
            "The guild member count has not been retrieved yet, so the "
            "channel description is not set."
        ) in output
        assert "polling failed" not in output
        assert "polling recovered" not in output

    def test_includes_current_guild_member_count_description(self) -> None:
        messages = format_automated_message_diagnostics(
            [],
            purchased_tickets=0,
            member_count=493,
            pending_invite_count=5,
        )
        output = "\n".join(messages)

        assert (
            "**Guild member count channel description (current)**\n"
            "493/500 (5 pending)"
        ) in output

    def test_highest_tier_preview_notes_that_it_is_already_reached(self) -> None:
        assert format_raffle_milestone_preview(200) == (
            "200 total tickets have been purchased for this raffle. "
            "Tier 4 rewards have been reached! "
            "This raffle is already at the highest configured tier."
        )

    async def test_diag_in_notification_channel_sends_read_only_previews(
        self,
    ) -> None:
        channel = SimpleNamespace(id=9012, send=AsyncMock())
        bot = SimpleNamespace(
            _config=SimpleNamespace(discord_notification_channel_id=9012),
            _send_automated_message_diagnostics=AsyncMock(),
        )
        message = SimpleNamespace(
            author=SimpleNamespace(bot=False),
            channel=channel,
            content=" DiAg ",
        )

        await Gw2Bot.on_message(cast(Gw2Bot, bot), message)  # type: ignore[arg-type]

        bot._send_automated_message_diagnostics.assert_awaited_once_with(channel)

    async def test_diag_debug_logging_does_not_include_message_details(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        author_secret = "author-secret"
        channel_secret = "channel-secret"
        channel = SimpleNamespace(
            id=9012,
            name=channel_secret,
            send=AsyncMock(),
        )
        bot = SimpleNamespace(
            _config=SimpleNamespace(discord_notification_channel_id=9012),
            _send_automated_message_diagnostics=AsyncMock(),
        )
        message = SimpleNamespace(
            author=SimpleNamespace(bot=False, name=author_secret),
            channel=channel,
            content="diag",
        )

        with caplog.at_level(logging.DEBUG, logger="gw2bot"):
            await Gw2Bot.on_message(cast(Gw2Bot, bot), message)  # type: ignore[arg-type]

        assert author_secret not in caplog.text
        assert channel_secret not in caplog.text
        assert (
            "Discord message received; author_is_bot=False "
            "notification_channel=True characters=4 diag_candidate=True"
            in caplog.text
        )
        assert "Starting automated message diagnostics request" in caplog.text
        assert "Automated message diagnostics request completed" in caplog.text

    async def test_diag_request_failure_logs_only_error_type(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        secret = "diag-request-secret"
        bot = SimpleNamespace(
            _config=SimpleNamespace(discord_notification_channel_id=9012),
            _send_automated_message_diagnostics=AsyncMock(
                side_effect=RuntimeError(secret)
            ),
        )
        message = SimpleNamespace(
            author=SimpleNamespace(bot=False),
            channel=SimpleNamespace(id=9012),
            content="diag",
        )

        with caplog.at_level(logging.DEBUG, logger="gw2bot"):
            await Gw2Bot.on_message(cast(Gw2Bot, bot), message)  # type: ignore[arg-type]

        assert secret not in caplog.text
        assert (
            "Automated message diagnostics request failed; error_type=RuntimeError"
            in caplog.text
        )
        assert "Automated message diagnostics request completed" not in caplog.text

    @pytest.mark.parametrize(
        ("author_is_bot", "channel_id", "content"),
        (
            (True, 9012, "diag"),
            (False, 3456, "diag"),
            (False, 9012, "diagnostic"),
        ),
    )
    async def test_ignores_bot_wrong_channel_and_non_exact_diag_messages(
        self,
        author_is_bot: bool,
        channel_id: int,
        content: str,
    ) -> None:
        bot = SimpleNamespace(
            _config=SimpleNamespace(discord_notification_channel_id=9012),
            _send_automated_message_diagnostics=AsyncMock(),
        )
        message = SimpleNamespace(
            author=SimpleNamespace(bot=author_is_bot),
            channel=SimpleNamespace(id=channel_id),
            content=content,
        )

        await Gw2Bot.on_message(cast(Gw2Bot, bot), message)  # type: ignore[arg-type]

        bot._send_automated_message_diagnostics.assert_not_awaited()

    async def test_preview_reads_current_interval_without_changing_schedule(
        self,
    ) -> None:
        now = datetime(2026, 6, 12, 14, 30, tzinfo=UTC)
        channel = SimpleNamespace(send=AsyncMock())
        bot = SimpleNamespace(
            get_raffle_contributions=MagicMock(
                return_value=[RaffleContribution("Free Only.1234", 0, 1)]
            ),
            get_raffle_totals=MagicMock(
                return_value=[
                    RaffleTotal(
                        username="Buyer.1234",
                        coins_deposited=750_000,
                        raffle_tickets=76,
                        gold_raffle_tickets=75,
                        manual_raffle_tickets=1,
                    )
                ]
            ),
            _last_guild_member_count=None,
            _last_pending_guild_invite_count=None,
        )

        await Gw2Bot._send_automated_message_diagnostics(
            cast(Gw2Bot, bot),
            channel,
            now,
        )

        bot.get_raffle_contributions.assert_called_once_with(
            datetime(2026, 6, 12, 12, tzinfo=UTC),
            now,
        )
        bot.get_raffle_totals.assert_called_once_with()
        output = "\n".join(
            call_.args[0]
            for call_ in channel.send.await_args_list
            if call_.args
        )
        report_embed = next(
            call_.kwargs["embed"]
            for call_ in channel.send.await_args_list
            if "embed" in call_.kwargs
        )
        assert (
            report_embed.description
            == "**Free Only.1234**\nPurchased: 0\nFree: 1\nTotal: 1"
        )
        assert (
            "100 total tickets have been purchased for this raffle. "
            "Tier 2 rewards have been reached!"
        ) in output

    async def test_preview_logging_does_not_include_contributor_content(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        secret = "contributor-secret"
        bot = SimpleNamespace(
            get_raffle_contributions=MagicMock(
                return_value=[RaffleContribution(secret, 1, 0)]
            ),
            get_raffle_totals=MagicMock(return_value=[]),
            _last_guild_member_count=None,
            _last_pending_guild_invite_count=None,
        )

        with caplog.at_level(logging.DEBUG, logger="gw2bot"):
            await Gw2Bot._send_automated_message_diagnostics(
                cast(Gw2Bot, bot),
                SimpleNamespace(send=AsyncMock()),
                datetime(2026, 6, 12, 14, 30, tzinfo=UTC),
            )

        assert secret not in caplog.text
        assert (
            "Prepared automated message diagnostics; messages=11 contributors=1"
            in caplog.text
        )
        assert caplog.text.count("Attempting automated diagnostic delivery") == 12
        assert caplog.text.count("Automated diagnostic delivery succeeded") == 12
        assert (
            "Automated message diagnostics completed; attempted=12 delivered=12 "
            "failed=0"
            in caplog.text
        )

    async def test_preview_failure_is_logged_and_remaining_previews_continue(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        secret = "diagnostic-failure-secret"
        channel = SimpleNamespace(
            send=AsyncMock(
                side_effect=[
                    None,
                    RuntimeError(secret),
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                ]
            )
        )
        bot = SimpleNamespace(
            get_raffle_contributions=MagicMock(
                return_value=[RaffleContribution("Buyer.1234", 1, 0)]
            ),
            get_raffle_totals=MagicMock(return_value=[]),
            _last_guild_member_count=None,
            _last_pending_guild_invite_count=None,
        )

        with caplog.at_level(logging.DEBUG, logger="gw2bot"):
            await Gw2Bot._send_automated_message_diagnostics(
                cast(Gw2Bot, bot),
                channel,
                datetime(2026, 6, 12, 14, 30, tzinfo=UTC),
            )

        assert channel.send.await_count == 12
        assert secret not in caplog.text
        assert (
            "Automated diagnostic delivery failed; kind=contribution-report "
            "error_type=RuntimeError"
            in caplog.text
        )
        assert (
            "Automated message diagnostics completed; attempted=12 delivered=11 "
            "failed=1"
            in caplog.text
        )


class TestDiscordNotificationDelivery:
    async def test_forbidden_logs_actionable_permission_diagnostics(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        bot = SimpleNamespace(
            _config=SimpleNamespace(discord_notification_channel_id=9012),
            _send_notification=AsyncMock(side_effect=forbidden_error(50013)),
        )

        with caplog.at_level(logging.ERROR, logger="gw2bot"):
            sent = await Gw2Bot._try_send_notification(
                cast(Gw2Bot, bot),
                "purchase message",
            )

        assert not sent
        assert (
            "Could not send Discord notification; reason=missing_permissions "
            "channel_id=9012 required_permissions=view_channel,send_messages "
            "(type=Forbidden status=403 code=50013)"
            in caplog.text
        )

    async def test_failure_logging_omits_raw_discord_exception_body(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        secret = "discord-raw-response-secret"

        class DiscordFailure(discord.DiscordException):
            status = 403
            code = 50001

            def __str__(self) -> str:
                return secret

        bot = SimpleNamespace(
            _config=SimpleNamespace(discord_notification_channel_id=9012),
            _send_notification=AsyncMock(side_effect=DiscordFailure()),
        )

        with caplog.at_level(logging.ERROR, logger="gw2bot"):
            sent = await Gw2Bot._try_send_notification(
                cast(Gw2Bot, bot),
                "purchase message",
            )

        assert not sent
        assert secret not in caplog.text
        assert "reason=missing_access" in caplog.text
        assert "type=DiscordFailure status=403 code=50001" in caplog.text
