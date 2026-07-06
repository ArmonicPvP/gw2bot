from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

import aiohttp
import discord
from discord import app_commands

from gw2bot import guild_log, guild_storage, member_count, notifications
from gw2bot.config import Config
from gw2bot.discord_utils import user_has_role
from gw2bot.guild_members import GuildMemberCache, TrialMemberReportEntry
from gw2bot.gw2_api import Gw2ApiClient
from gw2bot.poll_status import PollStatusTracker
from gw2bot.raffle import (
    RaffleAudit,
    RaffleContribution,
    RaffleResult,
    RaffleRunSummary,
    RaffleStore,
    RaffleTotal,
)
from gw2bot.raffle import reports as raffle_reports
from gw2bot.raffle.commands import RaffleCommands
from gw2bot.trials.commands import (
    create_check_command,
    create_track_command,
    handle_check_command,
    handle_track_command,
    track_member_autocomplete,
)
from gw2bot.trials.forum import (
    apply_trial_forum_in_review_tag,
    refresh_trial_forum_index,
    resolve_trial_forum_tags,
)
from gw2bot.trials.reports import (
    build_trial_report_messages,
    check_overdue_trials,
    poll_overdue_trials,
    resolve_trial_member_discord_statuses,
)

LOGGER = logging.getLogger(__name__)


class Gw2Bot(discord.Client):
    def __init__(self, config: Config):
        intents = discord.Intents.none()
        # Discord.py needs the guild role cache to resolve interaction member roles.
        intents.guilds = True
        intents.guild_messages = True
        intents.message_content = True
        super().__init__(intents=intents)
        self._config = config
        self._session: aiohttp.ClientSession | None = None
        self._poll_tasks: list[asyncio.Task[None]] = []
        self._notification_channel: Any | None = None
        self._raffle_contribution_channel: Any | None = None
        self._feast_notification_user: Any | None = None
        self._poll_status = PollStatusTracker(
            (config.gw2_api_key, config.discord_token)
        )
        self._raffle_store = RaffleStore(config.raffle_db_path, config.gw2_guild_id)
        self._api: Gw2ApiClient | None = None
        self._guild_members: GuildMemberCache | None = None
        self._last_guild_member_count: int | None = None
        self._last_pending_guild_invite_count: int | None = None
        self._last_topic_update_failure: str | None = None
        self._ready_announced = False
        self.tree = app_commands.CommandTree(self)
        self.tree.add_command(RaffleCommands(self))
        self.tree.add_command(self._create_check_command())
        self.tree.add_command(self._create_track_command())

    async def setup_hook(self) -> None:
        LOGGER.debug("Initializing HTTP session and GW2 API client")
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
        self._guild_members.start_background_refresh()
        await self._sync_commands()
        LOGGER.debug("Starting background poll tasks")
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
            asyncio.create_task(
                self._poll_raffle_contributions(),
                name="gw2-raffle-contribution-poller",
            ),
            asyncio.create_task(
                self._poll_guild_member_count_topic(),
                name="gw2-guild-member-count-topic-poller",
            ),
        ]

    async def close(self) -> None:
        LOGGER.debug("Closing bot and cancelling %s poll tasks", len(self._poll_tasks))
        for task in self._poll_tasks:
            task.cancel()
        await asyncio.gather(*self._poll_tasks, return_exceptions=True)
        if self._guild_members is not None:
            await self._guild_members.close()
        if self._session is not None:
            await self._session.close()
        self._raffle_store.close()
        await super().close()

    async def on_ready(self) -> None:
        LOGGER.info("Discord bot connected as %s", self.user)
        if self._ready_announced:
            return
        LOGGER.info(
            "GW2 bot connected to Discord. "
            f"Storage polling every {self._config.poll_interval_seconds} seconds; "
            "guild log polling every "
            f"{self._config.guild_log_poll_interval_seconds} seconds; "
            "overdue Trial member reporting daily at 17:00 UTC; "
            "raffle contribution reporting every 6 hours UTC; "
            "guild member count topic updates every 60 seconds."
        )
        self._ready_announced = True

    async def on_message(self, message: discord.Message) -> None:
        author_is_bot = bool(getattr(message.author, "bot", False))
        content = message.content.strip()
        diag_candidate = content.casefold() == "diag"
        channel_matches = (
            getattr(message.channel, "id", None)
            == self._config.discord_notification_channel_id
        )
        LOGGER.debug(
            "Discord message received; author_is_bot=%s notification_channel=%s "
            "characters=%s diag_candidate=%s",
            author_is_bot,
            channel_matches,
            len(message.content),
            diag_candidate,
        )
        if author_is_bot:
            LOGGER.debug("Ignoring Discord message from bot author")
            return
        if not diag_candidate:
            LOGGER.debug("Ignoring Discord message that is not a diag request")
            return
        if not channel_matches:
            LOGGER.debug("Ignoring diag request outside notification channel")
            return
        LOGGER.debug("Starting automated message diagnostics request")
        try:
            await self._send_automated_message_diagnostics(message.channel)
        except Exception as exc:
            LOGGER.error(
                "Automated message diagnostics request failed; error_type=%s",
                type(exc).__name__,
            )
            return
        LOGGER.debug("Automated message diagnostics request completed")

    async def on_thread_create(self, thread: discord.Thread) -> None:
        await self._apply_trial_forum_in_review_tag(thread)

    async def _apply_trial_forum_in_review_tag(
        self,
        thread: discord.Thread,
    ) -> None:
        await apply_trial_forum_in_review_tag(self, thread)

    async def _resolve_trial_forum_tags(
        self,
        thread: discord.Thread,
        tag_ids: set[int],
    ) -> dict[int, discord.ForumTag]:
        return await resolve_trial_forum_tags(self, thread, tag_ids)

    async def _send_automated_message_diagnostics(
        self,
        channel: Any,
        now: datetime | None = None,
    ) -> None:
        await notifications.send_automated_message_diagnostics(self, channel, now)

    async def authorize_raffle_command(
        self,
        interaction: discord.Interaction,
        required_role_id: int,
    ) -> bool:
        if user_has_role(interaction.user, required_role_id):
            LOGGER.debug(
                "Authorized raffle command for Discord user %s with role %s",
                interaction.user.id,
                required_role_id,
            )
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

    async def resolve_guild_member(
        self,
        username: str,
        *,
        force_refresh: bool = False,
    ) -> str | None:
        if self._guild_members is None:
            raise RuntimeError("Guild member cache was not initialized")
        resolved = await self._guild_members.resolve(
            username,
            force_refresh=force_refresh,
        )
        LOGGER.debug("Guild member resolution completed; matched=%s", resolved is not None)
        return resolved

    async def search_guild_members(
        self,
        query: str,
        *,
        limit: int = 25,
    ) -> list[str]:
        if self._guild_members is None:
            raise RuntimeError("Guild member cache was not initialized")
        results = await self._guild_members.search(query, limit=limit)
        LOGGER.debug("Guild member search completed; results=%s", len(results))
        return results

    def get_tracked_trial_members(self) -> set[str]:
        return self._raffle_store.get_tracked_trial_members()

    def get_tracked_trial_member_times(self) -> dict[str, datetime]:
        return self._raffle_store.get_tracked_trial_member_times()

    def is_trial_member_tracked(self, username: str) -> bool:
        return self._raffle_store.is_trial_member_tracked(username)

    def toggle_trial_member_tracking(
        self,
        username: str,
        discord_user_id: int,
    ) -> bool:
        return self._raffle_store.toggle_trial_member_tracking(
            username,
            discord_user_id,
        )

    def untrack_trial_member(self, username: str) -> None:
        self._raffle_store.untrack_trial_member(username)

    def add_manual_raffle_ticket(
        self,
        username: str,
    ) -> RaffleTotal:
        return self._raffle_store.add_manual_ticket(username)

    async def add_officer_raffle_purchase(
        self,
        username: str,
        amount: int,
    ) -> RaffleTotal:
        total = self._raffle_store.add_officer_purchase(username, amount)
        LOGGER.debug(
            "Delivering officer raffle purchase notifications; amount=%s",
            amount,
        )
        await self._send_pending_raffle_notifications()
        await self._send_pending_deposit_audit_notifications()
        await self._send_pending_raffle_milestones()
        LOGGER.debug(
            "Officer raffle purchase notification attempts completed; amount=%s",
            amount,
        )
        return total

    def remove_gold_raffle_tickets(
        self,
        username: str,
        amount: int = 1,
    ) -> RaffleTotal:
        return self._raffle_store.remove_gold_tickets(username, amount)

    def get_raffle_total(self, username: str) -> RaffleTotal:
        return self._raffle_store.get_total(username)

    def get_raffle_totals(self) -> list[RaffleTotal]:
        return self._raffle_store.get_totals()

    def get_raffle_contributions(
        self,
        start: datetime,
        end: datetime,
    ) -> list[RaffleContribution]:
        return self._raffle_store.get_contributions(start, end)

    def get_lifetime_raffle_contributions(self) -> list[RaffleContribution]:
        return self._raffle_store.get_lifetime_contributions()

    def get_linked_raffle_username(self, discord_user_id: int) -> str | None:
        return self._raffle_store.get_linked_username(discord_user_id)

    def link_raffle_account(self, discord_user_id: int, username: str) -> None:
        self._raffle_store.link_account(discord_user_id, username)

    def run_raffle(self) -> RaffleResult | None:
        return self._raffle_store.run_raffle()

    def get_pending_raffle_result(self) -> RaffleResult | None:
        return self._raffle_store.get_pending_raffle_result()

    def get_raffle_audit(self, run_id: int) -> RaffleAudit | None:
        return self._raffle_store.get_raffle_audit(run_id)

    def get_raffle_run_summaries(self) -> list[RaffleRunSummary]:
        return self._raffle_store.get_raffle_run_summaries()

    def mark_raffle_announcement_sent(self, run_id: int) -> None:
        self._raffle_store.mark_raffle_announcement_sent(run_id)

    async def refresh_guild_log(self) -> None:
        await guild_log.refresh_guild_log(self)

    async def _sync_commands(self) -> None:
        guild_id = self._config.discord_command_guild_id
        LOGGER.debug("Synchronizing application commands for guild %s", guild_id)
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
        await guild_storage.poll_guild_storage(self)

    async def _handle_storage(self, storage: list[dict[str, Any]]) -> None:
        await guild_storage.handle_storage(self, storage)

    async def _try_send_feast_notification(self, message: str) -> bool:
        return await guild_storage.try_send_feast_notification(self, message)

    async def _send_feast_private_message(self, message: str) -> None:
        await guild_storage.send_feast_private_message(self, message)

    async def _poll_overdue_trials(self) -> None:
        await poll_overdue_trials(self)

    async def _build_trial_report_messages(
        self,
        now: datetime | None = None,
    ) -> list[str]:
        return await build_trial_report_messages(self, now)

    async def _check_overdue_trials(self, now: datetime | None = None) -> bool:
        return await check_overdue_trials(self, now)

    def _create_check_command(self) -> app_commands.Command[Any, ..., None]:
        return create_check_command(self)

    async def _handle_check_command(
        self,
        interaction: discord.Interaction,
    ) -> None:
        await handle_check_command(self, interaction)

    def _create_track_command(self) -> app_commands.Command[Any, ..., None]:
        return create_track_command(self)

    async def _track_member_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        return await track_member_autocomplete(self, interaction, current)

    async def _handle_track_command(
        self,
        interaction: discord.Interaction,
        username: str,
    ) -> None:
        await handle_track_command(self, interaction, username)
    async def _poll_guild_member_count_topic(self) -> None:
        await member_count.poll_guild_member_count_topic(self)

    async def _update_guild_member_count_topic(self) -> bool:
        return await member_count.update_guild_member_count_topic(self)

    async def _try_update_logging_channel_topic(self, topic: str) -> bool:
        return await member_count.try_update_logging_channel_topic(self, topic)

    async def _poll_raffle_contributions(self) -> None:
        await raffle_reports.poll_raffle_contributions(self)

    async def _send_raffle_contribution_report(self, report_end: datetime) -> None:
        await raffle_reports.send_raffle_contribution_report(self, report_end)

    async def _send_raffle_contribution_message(self, message: str) -> None:
        await raffle_reports.send_raffle_contribution_message(self, message)

    async def _send_raffle_contribution_embed(
        self,
        embed: discord.Embed,
        view: discord.ui.View | None,
    ) -> None:
        await raffle_reports.send_raffle_contribution_embed(self, embed, view)

    async def _get_raffle_contribution_channel(self) -> Any:
        return await raffle_reports.get_raffle_contribution_channel(self)

    async def _resolve_trial_member_discord_statuses(
        self,
        usernames: list[str],
    ) -> list[TrialMemberReportEntry]:
        return await resolve_trial_member_discord_statuses(self, usernames)

    async def _refresh_trial_forum_index(
        self,
        forum: discord.ForumChannel,
    ) -> None:
        await refresh_trial_forum_index(self, forum)

    async def _poll_guild_log(self) -> None:
        await guild_log.poll_guild_log(self)

    async def _send_pending_raffle_notifications(self) -> None:
        await guild_log.send_pending_raffle_notifications(self)

    async def _send_pending_deposit_audit_notifications(self) -> None:
        await guild_log.send_pending_deposit_audit_notifications(self)

    async def _send_pending_raffle_milestones(self) -> None:
        await guild_log.send_pending_raffle_milestones(self)

    async def _send_pending_leave_notifications(self) -> None:
        await guild_log.send_pending_leave_notifications(self)

    async def _send_pending_join_notifications(self) -> None:
        await guild_log.send_pending_join_notifications(self)

    async def _send_pending_invite_notifications(self) -> None:
        await guild_log.send_pending_invite_notifications(self)

    async def _send_pending_rank_change_notifications(self) -> None:
        await guild_log.send_pending_rank_change_notifications(self)

    async def _try_send_notification(self, message: str) -> bool:
        return await notifications.try_send_notification(self, message)

    async def _try_send_raffle_contribution_message(self, message: str) -> bool:
        return await raffle_reports.try_send_raffle_contribution_message(self, message)

    async def _try_send_raffle_contribution_embed(
        self,
        embed: discord.Embed,
    ) -> bool:
        return await raffle_reports.try_send_raffle_contribution_embed(self, embed)

    async def _send_notification(self, message: str) -> None:
        await notifications.send_notification(self, message)

    async def _get_notification_channel(self) -> Any:
        return await notifications.get_notification_channel(self)
