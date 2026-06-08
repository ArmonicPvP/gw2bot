from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime
from typing import Any

import aiohttp
import discord
from discord import app_commands
from sqlalchemy.exc import SQLAlchemyError

from gw2bot.config import Config, ConfigurationError
from gw2bot.feast_stock import get_due_low_stock_alerts
from gw2bot.gw2_api import Gw2ApiClient
from gw2bot.guild_members import (
    GuildMemberCache,
    format_overdue_trial_report,
    get_overdue_trial_members,
    seconds_until_trial_report,
)
from gw2bot.raffle import RaffleResult, RaffleStore, RaffleTotal

LOGGER = logging.getLogger(__name__)

RAFFLE_DRAW_ROLE_ID = 1317124663847157880
RAFFLE_ADDTICKET_ROLE_ID = 1318357141521825872


def user_has_role(user: Any, required_role_id: int) -> bool:
    return any(
        role.id == required_role_id
        for role in getattr(user, "roles", ())
    )


def format_addticket_audit(discord_user_id: int, username: str) -> str:
    return f"<@{discord_user_id}> added 1 raffle ticket to {username}."


def format_poll_error(error: Exception, secrets: tuple[str, ...] = ()) -> str:
    if isinstance(error, aiohttp.ClientResponseError):
        status = f"HTTP {error.status}" if error.status else type(error).__name__
        detail = error.message.strip()
        message = f"{status}: {detail}" if detail else status
    else:
        message = str(error) or type(error).__name__

    for secret in secrets:
        if secret:
            message = message.replace(secret, "[REDACTED]")
    return message


class RaffleCommands(app_commands.Group):
    def __init__(self, bot: Gw2Bot):
        super().__init__(
            name="raffle",
            description="Manage the guild raffle",
            guild_only=True,
        )
        self._bot = bot

    @app_commands.command(name="draw", description="Draw a weighted raffle winner")
    async def draw(self, interaction: discord.Interaction) -> None:
        if not await self._bot.authorize_raffle_command(
            interaction,
            RAFFLE_DRAW_ROLE_ID,
        ):
            return

        await interaction.response.defer()
        result = self._bot.get_pending_raffle_result()
        if result is None:
            try:
                await self._bot.refresh_guild_log()
            except (aiohttp.ClientError, asyncio.TimeoutError, SQLAlchemyError):
                LOGGER.exception("Could not refresh the guild log before raffle draw")
                await interaction.followup.send(
                    "Could not refresh guild deposits. No raffle was drawn.",
                    ephemeral=True,
                )
                return
            result = self._bot.run_raffle()
        if result is None:
            await interaction.followup.send(
                "The raffle has no tickets.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"Raffle winner: **{result.winner}**! "
            f"Selected from {result.total_tickets} tickets. "
            "All current raffle tickets have been reset."
        )
        self._bot.mark_raffle_announcement_sent(result.run_id)

    @app_commands.command(
        name="addticket",
        description="Add one raffle ticket to a guild member",
    )
    @app_commands.describe(
        username="Guild Wars 2 account name, including the four digits",
    )
    async def addticket(
        self,
        interaction: discord.Interaction,
        username: str,
    ) -> None:
        if not await self._bot.authorize_raffle_command(
            interaction,
            RAFFLE_ADDTICKET_ROLE_ID,
        ):
            return

        await interaction.response.defer(ephemeral=True)
        try:
            canonical_username = await self._bot.resolve_guild_member(username)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            LOGGER.exception("Could not refresh the guild member cache")
            await interaction.followup.send(
                "Could not verify guild membership. Try again later.",
                ephemeral=True,
            )
            return

        if canonical_username is None:
            await interaction.followup.send(
                f"`{username}` is not a member of the configured guild.",
                ephemeral=True,
            )
            return

        try:
            total = self._bot.add_manual_raffle_ticket(canonical_username)
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        audit_message = format_addticket_audit(
            interaction.user.id,
            canonical_username,
        )
        LOGGER.info("%s", audit_message)
        audit_sent = await self._bot.send_notification(audit_message)
        await interaction.followup.send(
            f"Added one raffle ticket to **{canonical_username}**. "
            f"They now have {total.raffle_tickets} current tickets."
            + ("" if audit_sent else " The audit log could not be delivered."),
            ephemeral=True,
        )


class Gw2Bot(discord.Client):
    def __init__(self, config: Config):
        intents = discord.Intents.none()
        # Discord.py needs the guild role cache to resolve interaction member roles.
        intents.guilds = True
        super().__init__(intents=intents)
        self._config = config
        self._session: aiohttp.ClientSession | None = None
        self._poll_tasks: list[asyncio.Task[None]] = []
        self._notification_channel: Any | None = None
        self._feast_notification_user: Any | None = None
        self._last_errors: dict[str, str] = {}
        self._raffle_store = RaffleStore(config.raffle_db_path, config.gw2_guild_id)
        self._api: Gw2ApiClient | None = None
        self._guild_members: GuildMemberCache | None = None
        self._ready_announced = False
        self.tree = app_commands.CommandTree(self)
        self.tree.add_command(RaffleCommands(self))

    async def setup_hook(self) -> None:
        timeout = aiohttp.ClientTimeout(total=30)
        self._session = aiohttp.ClientSession(timeout=timeout)
        self._api = Gw2ApiClient(
            self._session,
            self._config.gw2_api_base_url,
            self._config.gw2_api_key,
        )
        self._guild_members = GuildMemberCache(
            self._api,
            self._config.gw2_guild_id,
            self._config.guild_member_cache_seconds,
        )
        await self._sync_commands()
        self._poll_tasks = [
            asyncio.create_task(
                self._poll_guild_storage(),
                name="gw2-guild-storage-poller",
            ),
            asyncio.create_task(
                self._poll_guild_log(),
                name="gw2-guild-log-poller",
            ),
            asyncio.create_task(
                self._poll_overdue_trials(),
                name="gw2-overdue-trial-poller",
            ),
        ]

    async def close(self) -> None:
        for task in self._poll_tasks:
            task.cancel()
        await asyncio.gather(*self._poll_tasks, return_exceptions=True)
        if self._session is not None:
            await self._session.close()
        self._raffle_store.close()
        await super().close()

    async def on_ready(self) -> None:
        LOGGER.info("Discord bot connected as %s", self.user)
        if self._ready_announced:
            return
        if await self._try_send_notification(
            "GW2 bot connected to Discord. "
            f"Storage polling every {self._config.poll_interval_seconds} seconds; "
            "guild log polling every "
            f"{self._config.guild_log_poll_interval_seconds} seconds; "
            "overdue Trial member reporting daily at 17:00 UTC."
        ):
            self._ready_announced = True

    async def authorize_raffle_command(
        self,
        interaction: discord.Interaction,
        required_role_id: int,
    ) -> bool:
        if user_has_role(interaction.user, required_role_id):
            return True
        LOGGER.warning(
            "Rejected raffle command from Discord user %s; required role %s, "
            "resolved member roles: %s",
            interaction.user.id,
            required_role_id,
            [role.id for role in getattr(interaction.user, "roles", ())],
        )
        await interaction.response.send_message(
            "You do not have the required role for this raffle command.",
            ephemeral=True,
        )
        return False

    async def send_notification(self, message: str) -> bool:
        return await self._try_send_notification(message)

    async def resolve_guild_member(self, username: str) -> str | None:
        if self._guild_members is None:
            raise RuntimeError("Guild member cache was not initialized")
        return await self._guild_members.resolve(username)

    def add_manual_raffle_ticket(
        self,
        username: str,
    ) -> RaffleTotal:
        return self._raffle_store.add_manual_ticket(username)

    def run_raffle(self) -> RaffleResult | None:
        return self._raffle_store.run_raffle()

    def get_pending_raffle_result(self) -> RaffleResult | None:
        return self._raffle_store.get_pending_raffle_result()

    def mark_raffle_announcement_sent(self, run_id: int) -> None:
        self._raffle_store.mark_raffle_announcement_sent(run_id)

    async def refresh_guild_log(self) -> None:
        if self._api is None:
            raise RuntimeError("GW2 API client was not initialized")
        cursor = self._raffle_store.get_cursor()
        events = await self._api.get_guild_log(
            self._config.gw2_guild_id,
            cursor,
        )
        if cursor is None:
            latest_event_id = max(
                (int(event["id"]) for event in events),
                default=0,
            )
            self._raffle_store.initialize_cursor(latest_event_id)
            LOGGER.info(
                "Initialized guild log cursor at event %s",
                latest_event_id,
            )
            return
        self._raffle_store.process_events(events)

    async def _sync_commands(self) -> None:
        guild_id = self._config.discord_command_guild_id
        guild = discord.Object(id=guild_id)
        self.tree.copy_global_to(guild=guild)
        try:
            commands = await self.tree.sync(guild=guild)
        except discord.Forbidden as exc:
            if exc.code != 50001:
                raise
            LOGGER.error(
                "Could not register application commands in Discord guild %s: "
                "Missing Access. Verify DISCORD_COMMAND_GUILD_ID and install the "
                "application in that server with the bot and "
                "applications.commands scopes. Monitoring will continue without "
                "slash commands.",
                guild_id,
            )
            return
        self.tree.clear_commands(guild=None)
        await self.tree.sync()
        LOGGER.info(
            "Synced %s application commands to Discord guild %s and cleared globals",
            len(commands),
            guild_id,
        )

    async def _poll_guild_storage(self) -> None:
        await self.wait_until_ready()
        if self._session is None:
            raise RuntimeError("HTTP session was not initialized")

        if self._api is None:
            raise RuntimeError("GW2 API client was not initialized")
        while not self.is_closed():
            try:
                storage = await self._api.get_guild_storage(self._config.gw2_guild_id)
                await self._handle_storage(storage)
            except (aiohttp.ClientError, asyncio.TimeoutError, SQLAlchemyError) as exc:
                await self._handle_poll_error("Guild Storage", exc)
            else:
                await self._handle_poll_success("Guild Storage")

            await asyncio.sleep(self._config.poll_interval_seconds)

    async def _handle_storage(self, storage: list[dict[str, Any]]) -> None:
        now = time.time()
        last_alerted_at = self._raffle_store.get_feast_alert_times()
        alerts, currently_low = get_due_low_stock_alerts(
            storage,
            last_alerted_at,
            now,
        )
        for feast_id in last_alerted_at.keys() - currently_low:
            self._raffle_store.clear_feast_alert(feast_id)
        for alert in alerts:
            if await self._try_send_feast_notification(alert.message):
                self._raffle_store.mark_feast_alert_sent(
                    alert.guild_storage_id,
                    now,
                )

    async def _try_send_feast_notification(self, message: str) -> bool:
        if not await self._try_send_notification(message):
            return False
        if self._config.discord_feast_notification_user_id is None:
            return True
        try:
            await self._send_feast_private_message(message)
        except discord.DiscordException:
            LOGGER.exception("Could not send private feast notification")
        return True

    async def _send_feast_private_message(self, message: str) -> None:
        user_id = self._config.discord_feast_notification_user_id
        if user_id is None:
            return
        if self._feast_notification_user is None:
            self._feast_notification_user = await self.fetch_user(user_id)
        await self._feast_notification_user.send(message)

    async def _poll_overdue_trials(self) -> None:
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                delivered = await self._check_overdue_trials()
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                await self._handle_poll_error("Trial Members", exc)
                delay = self._config.poll_interval_seconds
            else:
                await self._handle_poll_success("Trial Members")
                delay = (
                    seconds_until_trial_report(datetime.now(UTC))
                    if delivered
                    else self._config.poll_interval_seconds
                )

            await asyncio.sleep(delay)

    async def _check_overdue_trials(self, now: datetime | None = None) -> bool:
        if self._api is None:
            raise RuntimeError("GW2 API client was not initialized")
        members = await self._api.get_guild_members(self._config.gw2_guild_id)
        overdue = get_overdue_trial_members(members, now or datetime.now(UTC))
        for message in format_overdue_trial_report(overdue):
            if not await self._try_send_notification(message):
                return False
        return True

    async def _poll_guild_log(self) -> None:
        await self.wait_until_ready()
        if self._session is None:
            raise RuntimeError("HTTP session was not initialized")

        if self._api is None:
            raise RuntimeError("GW2 API client was not initialized")
        while not self.is_closed():
            try:
                await self.refresh_guild_log()
                await self._send_pending_raffle_notifications()
                await self._send_pending_leave_notifications()
            except (aiohttp.ClientError, asyncio.TimeoutError, SQLAlchemyError) as exc:
                await self._handle_poll_error("Guild Log", exc)
            else:
                await self._handle_poll_success("Guild Log")

            await asyncio.sleep(self._config.guild_log_poll_interval_seconds)

    async def _send_pending_raffle_notifications(self) -> None:
        for deposit in self._raffle_store.get_pending_notifications():
            if await self._try_send_notification(deposit.message):
                self._raffle_store.mark_notification_sent(deposit.event_id)

    async def _send_pending_leave_notifications(self) -> None:
        for leave in self._raffle_store.get_pending_leave_notifications():
            if await self._try_send_notification(leave.message):
                self._raffle_store.mark_leave_notification_sent(leave.event_id)

    async def _handle_poll_success(self, source: str) -> None:
        if source in self._last_errors:
            if source == "Guild Log":
                LOGGER.info("%s polling recovered.", source)
                del self._last_errors[source]
                return
            if await self._try_send_notification(f"{source} polling recovered."):
                del self._last_errors[source]

    async def _handle_poll_error(self, source: str, error: Exception) -> None:
        config = getattr(self, "_config", None)
        message = format_poll_error(
            error,
            (
                getattr(config, "gw2_api_key", ""),
                getattr(config, "discord_token", ""),
            ),
        )
        LOGGER.warning("%s polling failed: %s", source, message)
        if source == "Guild Log":
            self._last_errors[source] = message
            return
        if message != self._last_errors.get(source):
            if await self._try_send_notification(
                f"{source} polling failed: {message}"
            ):
                self._last_errors[source] = message

    async def _try_send_notification(self, message: str) -> bool:
        try:
            await self._send_notification(message)
        except discord.DiscordException:
            LOGGER.exception("Could not send Discord notification")
            return False
        return True

    async def _send_notification(self, message: str) -> None:
        if self._notification_channel is None:
            channel = await self.fetch_channel(
                self._config.discord_notification_channel_id
            )
            if (
                getattr(getattr(channel, "guild", None), "id", None)
                != self._config.discord_command_guild_id
            ):
                raise discord.ClientException(
                    "DISCORD_NOTIFICATION_CHANNEL_ID must belong to "
                    "DISCORD_COMMAND_GUILD_ID"
                )
            self._notification_channel = channel
        await self._notification_channel.send(message)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        config = Config.from_env()
    except ConfigurationError as exc:
        raise SystemExit(f"Configuration error: {exc}") from exc

    bot = Gw2Bot(config)
    bot.run(config.discord_token, log_handler=None)
