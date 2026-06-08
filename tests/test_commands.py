import unittest
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock, call, patch

import aiohttp
import discord
from discord import app_commands

from gw2bot.config import Config
from gw2bot.main import (
    RAFFLE_ADDTICKET_ROLE_ID,
    RAFFLE_DRAW_ROLE_ID,
    Gw2Bot,
    RaffleCommands,
    format_addticket_audit,
    user_has_role,
)


class CommandTests(unittest.TestCase):
    def test_registers_raffle_command_group(self) -> None:
        group = RaffleCommands(object())  # type: ignore[arg-type]
        commands = {command.name: command for command in group.commands}

        self.assertEqual(group.name, "raffle")
        self.assertTrue(group.guild_only)
        self.assertEqual(set(commands), {"draw", "addticket"})
        self.assertNotIn("win", commands)
        addticket = commands["addticket"]
        self.assertIsInstance(addticket, app_commands.Command)
        assert isinstance(addticket, app_commands.Command)
        self.assertEqual(
            [parameter.name for parameter in addticket.parameters],
            ["username"],
        )

    def test_checks_required_raffle_roles(self) -> None:
        draw_user = SimpleNamespace(roles=[SimpleNamespace(id=RAFFLE_DRAW_ROLE_ID)])
        add_user = SimpleNamespace(
            roles=[SimpleNamespace(id=RAFFLE_ADDTICKET_ROLE_ID)]
        )
        no_roles_user = SimpleNamespace()

        self.assertTrue(user_has_role(draw_user, RAFFLE_DRAW_ROLE_ID))
        self.assertFalse(user_has_role(draw_user, RAFFLE_ADDTICKET_ROLE_ID))
        self.assertTrue(user_has_role(add_user, RAFFLE_ADDTICKET_ROLE_ID))
        self.assertFalse(user_has_role(no_roles_user, RAFFLE_DRAW_ROLE_ID))

    def test_formats_addticket_audit_with_discord_mention(self) -> None:
        self.assertEqual(
            format_addticket_audit(123456789, "Username.1234"),
            "<@123456789> added 1 raffle ticket to Username.1234.",
        )


class RaffleDrawCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_defers_before_running_raffle_and_uses_followup(self) -> None:
        events: list[str] = []
        bot = SimpleNamespace(
            authorize_raffle_command=AsyncMock(return_value=True),
            get_pending_raffle_result=MagicMock(return_value=None),
            refresh_guild_log=AsyncMock(
                side_effect=lambda: events.append("refresh")
            ),
            run_raffle=MagicMock(
                side_effect=lambda: (
                    events.append("run"),
                    SimpleNamespace(
                        run_id=7,
                        winner="Winner.1234",
                        total_tickets=10,
                    ),
                )[1]
            ),
            mark_raffle_announcement_sent=MagicMock(),
        )
        interaction = SimpleNamespace(
            response=SimpleNamespace(
                defer=AsyncMock(side_effect=lambda: events.append("defer")),
                send_message=AsyncMock(),
            ),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        group = RaffleCommands(bot)  # type: ignore[arg-type]
        draw = next(command for command in group.commands if command.name == "draw")

        await draw.callback(group, interaction)  # type: ignore[arg-type]

        self.assertEqual(events, ["defer", "refresh", "run"])
        interaction.response.send_message.assert_not_awaited()
        interaction.followup.send.assert_awaited_once_with(
            "Raffle winner: **Winner.1234**! Selected from 10 tickets. "
            "All current raffle tickets have been reset."
        )
        bot.mark_raffle_announcement_sent.assert_called_once_with(7)

    async def test_retries_pending_announcement_without_refreshing_or_redrawing(
        self,
    ) -> None:
        pending = SimpleNamespace(
            run_id=7,
            winner="Winner.1234",
            total_tickets=10,
        )
        bot = SimpleNamespace(
            authorize_raffle_command=AsyncMock(return_value=True),
            get_pending_raffle_result=MagicMock(return_value=pending),
            refresh_guild_log=AsyncMock(),
            run_raffle=MagicMock(),
            mark_raffle_announcement_sent=MagicMock(),
        )
        interaction = SimpleNamespace(
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        group = RaffleCommands(bot)  # type: ignore[arg-type]
        draw = next(command for command in group.commands if command.name == "draw")

        await draw.callback(group, interaction)  # type: ignore[arg-type]

        bot.refresh_guild_log.assert_not_awaited()
        bot.run_raffle.assert_not_called()
        bot.mark_raffle_announcement_sent.assert_called_once_with(7)

    async def test_preserves_pending_announcement_when_discord_send_fails(
        self,
    ) -> None:
        bot = SimpleNamespace(
            authorize_raffle_command=AsyncMock(return_value=True),
            get_pending_raffle_result=MagicMock(return_value=None),
            refresh_guild_log=AsyncMock(),
            run_raffle=MagicMock(
                return_value=SimpleNamespace(
                    run_id=7,
                    winner="Winner.1234",
                    total_tickets=10,
                )
            ),
            mark_raffle_announcement_sent=MagicMock(),
        )
        interaction = SimpleNamespace(
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(
                send=AsyncMock(
                    side_effect=discord.ClientException("Discord unavailable")
                )
            ),
        )
        group = RaffleCommands(bot)  # type: ignore[arg-type]
        draw = next(command for command in group.commands if command.name == "draw")

        with self.assertRaises(discord.ClientException):
            await draw.callback(group, interaction)  # type: ignore[arg-type]

        bot.mark_raffle_announcement_sent.assert_not_called()

    async def test_does_not_draw_when_guild_log_refresh_fails(self) -> None:
        bot = SimpleNamespace(
            authorize_raffle_command=AsyncMock(return_value=True),
            get_pending_raffle_result=MagicMock(return_value=None),
            refresh_guild_log=AsyncMock(
                side_effect=TimeoutError("GW2 API unavailable")
            ),
            run_raffle=MagicMock(),
            mark_raffle_announcement_sent=MagicMock(),
        )
        interaction = SimpleNamespace(
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        group = RaffleCommands(bot)  # type: ignore[arg-type]
        draw = next(command for command in group.commands if command.name == "draw")

        with self.assertLogs("gw2bot.main", level="ERROR"):
            await draw.callback(group, interaction)  # type: ignore[arg-type]

        bot.run_raffle.assert_not_called()
        interaction.followup.send.assert_awaited_once_with(
            "Could not refresh guild deposits. No raffle was drawn.",
            ephemeral=True,
        )


class CommandSyncTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.config = Config.from_env(
            {
                "DISCORD_TOKEN": "discord-token",
                "DISCORD_COMMAND_GUILD_ID": "5678",
                "DISCORD_NOTIFICATION_CHANNEL_ID": "9012",
                "GW2_API_KEY": "gw2-key",
                "GW2_GUILD_ID": "guild-id",
            }
        )
        self.tree = MagicMock()

    async def test_missing_guild_access_does_not_stop_monitoring(self) -> None:
        self.tree.sync = AsyncMock(side_effect=_forbidden_error(50001))
        bot = SimpleNamespace(_config=self.config, tree=self.tree)

        with self.assertLogs("gw2bot.main", level="ERROR") as logs:
            await Gw2Bot._sync_commands(bot)  # type: ignore[arg-type]

        self.assertIn("Missing Access", logs.output[0])
        self.assertIn("Monitoring will continue", logs.output[0])
        self.tree.clear_commands.assert_not_called()

    async def test_other_command_sync_permission_errors_are_raised(self) -> None:
        self.tree.sync = AsyncMock(side_effect=_forbidden_error(50013))
        bot = SimpleNamespace(_config=self.config, tree=self.tree)

        with self.assertRaises(discord.Forbidden):
            await Gw2Bot._sync_commands(bot)  # type: ignore[arg-type]


class BotIntentTests(unittest.TestCase):
    @patch("gw2bot.main.RaffleStore")
    def test_enables_guild_intent_to_resolve_interaction_roles(
        self,
        raffle_store: MagicMock,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Config.from_env(
                {
                    "DISCORD_TOKEN": "discord-token",
                    "DISCORD_COMMAND_GUILD_ID": "5678",
                    "DISCORD_NOTIFICATION_CHANNEL_ID": "9012",
                    "GW2_API_KEY": "gw2-key",
                    "GW2_GUILD_ID": "guild-id",
                    "RAFFLE_DB_PATH": str(Path(directory) / "raffle.db"),
                }
            )

            bot = Gw2Bot(config)

        self.assertTrue(bot.intents.guilds)
        self.assertFalse(bot.intents.members)
        raffle_store.assert_called_once()


class GuildLogRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def test_processes_new_events_before_returning(self) -> None:
        events = [{"id": 101, "type": "stash"}]
        api = SimpleNamespace(get_guild_log=AsyncMock(return_value=events))
        store = MagicMock()
        store.get_cursor.return_value = 100
        bot = SimpleNamespace(
            _api=api,
            _raffle_store=store,
            _config=SimpleNamespace(gw2_guild_id="guild-id"),
        )

        await Gw2Bot.refresh_guild_log(cast(Gw2Bot, bot))

        api.get_guild_log.assert_awaited_once_with("guild-id", 100)
        store.process_events.assert_called_once_with(events)
        store.initialize_cursor.assert_not_called()


class FeastNotificationTests(unittest.IsolatedAsyncioTestCase):
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

        self.assertTrue(sent)
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
        self.assertEqual(private_user.send.await_args_list, [call(message)] * 2)

    async def test_skips_private_message_when_not_configured(self) -> None:
        bot = SimpleNamespace(
            _config=SimpleNamespace(discord_feast_notification_user_id=None),
            _try_send_notification=AsyncMock(return_value=True),
        )

        sent = await Gw2Bot._try_send_feast_notification(
            cast(Gw2Bot, bot),
            "food alert",
        )

        self.assertTrue(sent)
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

        self.assertFalse(sent)
        private_message.assert_not_awaited()

    async def test_private_message_failure_does_not_repeat_channel_alert(self) -> None:
        bot = SimpleNamespace(
            _config=SimpleNamespace(discord_feast_notification_user_id=3456),
            _try_send_notification=AsyncMock(return_value=True),
            _send_feast_private_message=AsyncMock(
                side_effect=discord.ClientException("DM unavailable")
            ),
        )

        with self.assertLogs("gw2bot.main", level="ERROR"):
            sent = await Gw2Bot._try_send_feast_notification(
                cast(Gw2Bot, bot),
                "food alert",
            )

        self.assertTrue(sent)


class TrialMemberNotificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_posts_overdue_trial_report_to_notification_channel(self) -> None:
        now = datetime(2026, 6, 7, 17, 0, tzinfo=UTC)
        api = SimpleNamespace(
            get_guild_members=AsyncMock(
                return_value=[
                    {
                        "name": "Overdue.1234",
                        "rank": "Trial",
                        "joined": (now - timedelta(days=14)).isoformat(),
                    },
                    {
                        "name": "Recent.1234",
                        "rank": "Trial",
                        "joined": (now - timedelta(days=1)).isoformat(),
                    },
                ]
            )
        )
        bot = SimpleNamespace(
            _api=api,
            _config=SimpleNamespace(gw2_guild_id="guild-id"),
            _try_send_notification=AsyncMock(return_value=True),
        )

        delivered = await Gw2Bot._check_overdue_trials(cast(Gw2Bot, bot), now)

        self.assertTrue(delivered)
        api.get_guild_members.assert_awaited_once_with("guild-id")
        message = bot._try_send_notification.await_args.args[0]
        self.assertIn("Overdue.1234", message)
        self.assertNotIn("Recent.1234", message)
        self.assertIn("ranked up to Sunborne", message)

    async def test_does_not_post_when_no_trials_are_overdue(self) -> None:
        bot = SimpleNamespace(
            _api=SimpleNamespace(get_guild_members=AsyncMock(return_value=[])),
            _config=SimpleNamespace(gw2_guild_id="guild-id"),
            _try_send_notification=AsyncMock(return_value=True),
        )

        delivered = await Gw2Bot._check_overdue_trials(
            cast(Gw2Bot, bot),
            datetime(2026, 6, 7, tzinfo=UTC),
        )

        self.assertTrue(delivered)
        bot._try_send_notification.assert_not_awaited()

    async def test_reports_failed_delivery_to_poller(self) -> None:
        now = datetime(2026, 6, 7, 17, 0, tzinfo=UTC)
        bot = SimpleNamespace(
            _api=SimpleNamespace(
                get_guild_members=AsyncMock(
                    return_value=[
                        {
                            "name": "Overdue.1234",
                            "rank": "Trial",
                            "joined": (now - timedelta(days=14)).isoformat(),
                        }
                    ]
                )
            ),
            _config=SimpleNamespace(gw2_guild_id="guild-id"),
            _try_send_notification=AsyncMock(return_value=False),
        )

        delivered = await Gw2Bot._check_overdue_trials(cast(Gw2Bot, bot), now)

        self.assertFalse(delivered)

    @patch("gw2bot.main.seconds_until_trial_report", return_value=123)
    @patch("gw2bot.main.asyncio.sleep", new_callable=AsyncMock)
    async def test_poller_checks_immediately_then_waits_for_daily_run(
        self,
        sleep: AsyncMock,
        seconds_until_report: MagicMock,
    ) -> None:
        bot = SimpleNamespace(
            wait_until_ready=AsyncMock(),
            is_closed=MagicMock(side_effect=[False, True]),
            _check_overdue_trials=AsyncMock(return_value=True),
            _handle_poll_error=AsyncMock(),
            _handle_poll_success=AsyncMock(),
            _config=SimpleNamespace(poll_interval_seconds=30),
        )

        await Gw2Bot._poll_overdue_trials(bot)  # type: ignore[arg-type]

        bot.wait_until_ready.assert_awaited_once()
        bot._check_overdue_trials.assert_awaited_once()
        bot._handle_poll_success.assert_awaited_once_with("Trial Members")
        seconds_until_report.assert_called_once()
        sleep.assert_awaited_once_with(123)

    @patch("gw2bot.main.seconds_until_trial_report")
    @patch("gw2bot.main.asyncio.sleep", new_callable=AsyncMock)
    async def test_poller_retries_failed_delivery_after_poll_interval(
        self,
        sleep: AsyncMock,
        seconds_until_report: MagicMock,
    ) -> None:
        bot = SimpleNamespace(
            wait_until_ready=AsyncMock(),
            is_closed=MagicMock(side_effect=[False, True]),
            _check_overdue_trials=AsyncMock(return_value=False),
            _handle_poll_error=AsyncMock(),
            _handle_poll_success=AsyncMock(),
            _config=SimpleNamespace(poll_interval_seconds=30),
        )

        await Gw2Bot._poll_overdue_trials(bot)  # type: ignore[arg-type]

        seconds_until_report.assert_not_called()
        sleep.assert_awaited_once_with(30)


class PollStatusNotificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_bad_gateway_does_not_leak_api_key(self) -> None:
        api_key = "secret-api-key"
        bot = SimpleNamespace(
            _config=SimpleNamespace(
                gw2_api_key=api_key,
                discord_token="secret-discord-token",
            ),
            _last_errors={},
            _try_send_notification=AsyncMock(return_value=True),
        )
        error = aiohttp.ClientResponseError(
            SimpleNamespace(
                real_url=f"https://example.test/log?access_token={api_key}"
            ),  # type: ignore[arg-type]
            (),
            status=502,
            message="Bad Gateway",
        )

        with self.assertLogs("gw2bot.main", level="WARNING") as logs:
            await Gw2Bot._handle_poll_error(cast(Gw2Bot, bot), "Guild Log", error)

        bot._try_send_notification.assert_not_awaited()
        self.assertEqual(bot._last_errors, {"Guild Log": "HTTP 502: Bad Gateway"})
        self.assertNotIn(api_key, "\n".join(logs.output))

    async def test_redacts_configured_credentials_from_poll_error(self) -> None:
        api_key = "secret-api-key"
        bot = SimpleNamespace(
            _config=SimpleNamespace(
                gw2_api_key=api_key,
                discord_token="secret-discord-token",
            ),
            _last_errors={},
            _try_send_notification=AsyncMock(return_value=True),
        )

        with self.assertLogs("gw2bot.main", level="WARNING") as logs:
            await Gw2Bot._handle_poll_error(
                cast(Gw2Bot, bot),
                "Guild Log",
                TimeoutError(f"Request failed with Bearer {api_key}"),
            )

        bot._try_send_notification.assert_not_awaited()
        self.assertIn(
            "Guild Log polling failed: Request failed with Bearer [REDACTED]",
            "\n".join(logs.output),
        )

    async def test_retries_same_poll_error_after_delivery_failure(self) -> None:
        bot = SimpleNamespace(
            _last_errors={},
            _try_send_notification=AsyncMock(side_effect=[False, True]),
        )
        error = TimeoutError("API unavailable")

        await Gw2Bot._handle_poll_error(cast(Gw2Bot, bot), "Guild Storage", error)
        await Gw2Bot._handle_poll_error(cast(Gw2Bot, bot), "Guild Storage", error)

        self.assertEqual(
            bot._try_send_notification.await_args_list,
            [call("Guild Storage polling failed: API unavailable")] * 2,
        )
        self.assertEqual(bot._last_errors, {"Guild Storage": "API unavailable"})

    async def test_retries_recovery_notification_after_delivery_failure(self) -> None:
        bot = SimpleNamespace(
            _last_errors={"Guild Storage": "API unavailable"},
            _try_send_notification=AsyncMock(side_effect=[False, True]),
        )

        await Gw2Bot._handle_poll_success(cast(Gw2Bot, bot), "Guild Storage")
        await Gw2Bot._handle_poll_success(cast(Gw2Bot, bot), "Guild Storage")

        self.assertEqual(
            bot._try_send_notification.await_args_list,
            [call("Guild Storage polling recovered.")] * 2,
        )
        self.assertEqual(bot._last_errors, {})

    async def test_guild_log_recovery_is_console_only(self) -> None:
        bot = SimpleNamespace(
            _last_errors={"Guild Log": "API unavailable"},
            _try_send_notification=AsyncMock(),
        )

        with self.assertLogs("gw2bot.main", level="INFO") as logs:
            await Gw2Bot._handle_poll_success(cast(Gw2Bot, bot), "Guild Log")

        bot._try_send_notification.assert_not_awaited()
        self.assertIn("Guild Log polling recovered.", "\n".join(logs.output))
        self.assertEqual(bot._last_errors, {})


def _forbidden_error(code: int) -> discord.Forbidden:
    response = SimpleNamespace(status=403, reason="Forbidden")
    return discord.Forbidden(
        response,  # type: ignore[arg-type]
        {"code": code, "message": "Missing Access"},
    )


if __name__ == "__main__":
    unittest.main()
