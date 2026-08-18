from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable, Coroutine, Mapping
from datetime import datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import aiohttp
import discord
from discord import app_commands

from gw2bot import guild_log, guild_storage, member_count, notifications
from gw2bot.config import (
    NOTIFICATION_CHANNEL_SETTING,
    BootstrapConfig,
    Config,
    missing_configuration_message,
)
from gw2bot.discord_utils import send_interaction_notice, user_has_role
from gw2bot.events import scheduler as event_scheduler
from gw2bot.events.commands import EventCommands
from gw2bot.events.store import EventStore
from gw2bot.events.views import (
    EventSettingsButton,
    EventSignOutButton,
    EventSignUpButton,
)
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
from gw2bot.logging_setup import SecretRegistry
from gw2bot.raffle.commands import RaffleCommands
from gw2bot.raffle.views import (
    RaffleAuditRangesButton,
    RaffleContributionReportButton,
    RaffleLeaderboardButton,
    RaffleTicketsListButton,
)
from gw2bot.settings.commands import SettingsCommands
from gw2bot.settings.composition import compose_from_store
from gw2bot.settings.crypto import SettingsCipher
from gw2bot.settings.definitions import SettingDefinition, setting_key
from gw2bot.settings.migration import legacy_variables_present
from gw2bot.settings.store import SettingsStore
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

if TYPE_CHECKING:
    from gw2bot.web.server import WebServer

LOGGER = logging.getLogger(__name__)


def _bootstrap_from(config: Config) -> BootstrapConfig:
    """Recover the bootstrap half of a Config that was handed over whole.

    Only used when a caller builds a bot from a Config alone - the tests, and
    anything embedding the bot - so a settings change still has the values it
    must recompose on top of.
    """
    return BootstrapConfig(
        discord_token=config.discord_token,
        discord_command_guild_id=config.discord_command_guild_id,
        debug=config.debug,
        raffle_db_path=config.raffle_db_path,
        gw2_api_base_url=config.gw2_api_base_url,
        web_enabled=config.web_enabled,
        web_port=config.web_port,
    )


class Gw2Bot(discord.Client):
    def __init__(
        self,
        config: Config,
        *,
        bootstrap: BootstrapConfig | None = None,
        settings_store: SettingsStore | None = None,
        secrets: SecretRegistry | None = None,
    ):
        intents = discord.Intents.none()
        # Discord.py needs the guild role cache to resolve interaction member roles.
        intents.guilds = True
        intents.guild_messages = True
        intents.message_content = True
        super().__init__(intents=intents)
        self._config = config
        # Kept so a settings change can be recomposed onto the same bootstrap
        # values rather than re-read from an environment that no longer owns
        # them.
        self._bootstrap = bootstrap or _bootstrap_from(config)
        self._settings_store = settings_store or SettingsStore(
            config.raffle_db_path,
            SettingsCipher.for_database(config.raffle_db_path),
        )
        self._secrets = secrets or SecretRegistry(
            (
                config.gw2_api_key,
                config.discord_token,
                config.discord_oauth_client_secret,
                config.web_session_secret,
            )
        )
        self._session: aiohttp.ClientSession | None = None
        self._poll_tasks: list[asyncio.Task[None]] = []
        self._notification_channel: Any | None = None
        self._raffle_contribution_channel: Any | None = None
        self._feast_notification_user: Any | None = None
        self._feast_counts: dict[int, int] | None = None
        self._legacy_variables_announced = False
        self._poll_status = PollStatusTracker(self._secrets)
        self._raffle_store = RaffleStore(config.raffle_db_path, config.gw2_guild_id)
        self._event_store = EventStore(config.raffle_db_path)
        self._event_timezone = ZoneInfo(config.event_timezone)
        self._api: Gw2ApiClient | None = None
        self._web_server: WebServer | None = None
        self._guild_members: GuildMemberCache | None = None
        self._last_guild_member_count: int | None = None
        self._last_pending_guild_invite_count: int | None = None
        self._last_topic_update_failure: str | None = None
        self._ready_announced = False
        self.tree = app_commands.CommandTree(self)
        self.tree.add_command(RaffleCommands(self))
        self.tree.add_command(SettingsCommands(self))
        self.tree.add_command(EventCommands(self))
        self.tree.add_command(self._create_check_command())
        self.tree.add_command(self._create_track_command())
        # Rebuild raffle audit pager buttons from their custom_ids so old
        # audit messages keep paging after view timeouts and restarts.
        self.add_dynamic_items(RaffleAuditRangesButton)
        # The raffle ticket, leaderboard and contribution report pagers reload
        # their rows on click, so their arrows keep working for as long as the
        # message is on screen instead of dying with a view timeout.
        self.add_dynamic_items(
            RaffleTicketsListButton,
            RaffleLeaderboardButton,
            RaffleContributionReportButton,
        )
        # Event signup buttons live on long-lived event messages and must
        # keep working after view timeouts and bot restarts.
        self.add_dynamic_items(
            EventSignUpButton,
            EventSignOutButton,
            EventSettingsButton,
        )

    async def setup_hook(self) -> None:
        LOGGER.debug("Initializing HTTP session and GW2 API client")
        timeout = aiohttp.ClientTimeout(total=30)
        self._session = aiohttp.ClientSession(timeout=timeout)
        self._log_disabled_features()
        self._log_legacy_variables()
        self._rebuild_gw2_api_client()
        await self._sync_commands()
        self._reconcile_poll_tasks()
        await self._reconcile_web_server()

    def _rebuild_gw2_api_client(self) -> None:
        """Point the API client and roster cache at the current credentials.

        Both capture the key and guild id they were built with, so a change to
        either has to replace them rather than let the old pair keep polling.
        """
        api_key = self._config.gw2_api_key
        guild_id = self._config.gw2_guild_id
        if self._session is None or api_key is None or guild_id is None:
            self._api = None
            self._guild_members = None
            LOGGER.debug("Guild Wars 2 API client not built; credentials unset")
            return
        self._api = Gw2ApiClient(
            self._session,
            self._config.gw2_api_base_url,
            api_key,
        )
        self._guild_members = GuildMemberCache(
            self._api,
            guild_id,
            self._config.guild_member_cache_seconds,
        )
        self._guild_members.start_background_refresh()
        LOGGER.debug("Guild Wars 2 API client and guild member cache rebuilt")

    def _wanted_poll_tasks(self) -> dict[str, Callable[[], Coroutine[Any, Any, None]]]:
        """The pollers this configuration should be running, by task name."""
        gw2_api_enabled = self._config.gw2_api_enabled
        notifications_enabled = self._config.notifications_enabled
        wanted: dict[str, Callable[[], Coroutine[Any, Any, None]]] = {
            "gw2-raffle-contribution-poller": self._poll_raffle_contributions,
            "gw2-event-scheduler": self._poll_event_updates,
        }
        if gw2_api_enabled:
            # These read the GW2 API on every pass, so without a key and a
            # guild id they have nothing to poll.
            wanted["gw2-guild-storage-poller"] = self._poll_guild_storage
            wanted["gw2-guild-log-poller"] = self._poll_guild_log
        if gw2_api_enabled and notifications_enabled:
            # Both read the GW2 API and exist only to write to the
            # notification channel, so either half being unset stops them.
            wanted["gw2-overdue-trial-poller"] = self._poll_overdue_trials
            wanted["gw2-guild-member-count-topic-poller"] = (
                self._poll_guild_member_count_topic
            )
        return wanted

    def _reconcile_poll_tasks(self, restart: frozenset[str] = frozenset()) -> list[str]:
        """Start the pollers this configuration wants and stop the rest.

        Run at startup and again whenever /settings changes something a poller
        depends on, so enabling a feature no longer waits for a restart.
        Names in ``restart`` are cancelled even when they are still wanted,
        which is how a poller that captured a credential picks up a new one.
        """
        wanted = self._wanted_poll_tasks()
        running = {
            task.get_name(): task
            for task in self._poll_tasks
            if not task.done()
        }
        changed: list[str] = []
        for name, task in running.items():
            if name in wanted and name not in restart:
                continue
            task.cancel()
            changed.append(name)
        surviving = [
            task
            for name, task in running.items()
            if name in wanted and name not in restart
        ]
        for name, factory in wanted.items():
            if name in running and name not in restart:
                continue
            surviving.append(asyncio.create_task(factory(), name=name))
            if name not in changed:
                changed.append(name)
        self._poll_tasks = surviving
        LOGGER.debug(
            "Reconciled background poll tasks; running=%s changed=%s",
            len(surviving),
            sorted(changed),
        )
        return sorted(changed)

    async def _reconcile_web_server(self, restart: bool = False) -> bool:
        """Bring the calendar up, down, or back up on new settings.

        Returns whether anything actually changed, so /settings can say the
        calendar was restarted only when it was.
        """
        wanted = self._config.web_calendar_enabled
        running = self._web_server is not None
        if wanted and running and not restart:
            return False
        if running:
            assert self._web_server is not None
            await self._web_server.stop()
            self._web_server = None
            LOGGER.debug("Web calendar stopped")
            if not wanted:
                return True
        if not wanted:
            LOGGER.debug(
                "Web calendar disabled; web_enabled=%s missing_settings=%s",
                self._config.web_enabled,
                ", ".join(self._config.missing_web_settings) or "none",
            )
            return running
        if self._session is None:
            return False
        # Imported lazily so the web layer only loads when enabled.
        from gw2bot.web.server import WebServer

        web_server = WebServer(self, self._config, self._session)
        try:
            await web_server.start()
        except OSError as exc:
            # The calendar is an optional read-only extra. A port conflict
            # must not cost the guild its raffles, trials and events, so
            # log loudly and carry on without it.
            LOGGER.error(
                "Could not start the web calendar; the bot continues "
                "without it; port=%s error_type=%s",
                self._config.web_port,
                type(exc).__name__,
            )
            return True
        self._web_server = web_server
        return True

    @property
    def settings_store(self) -> SettingsStore:
        return self._settings_store

    @property
    def secrets(self) -> SecretRegistry:
        return self._secrets

    async def apply_settings_change(self, changed: set[str]) -> list[str]:
        """Reload the configuration and reconcile whatever captured a value.

        Most of the bot reads ``self._config`` when it needs something, so
        swapping the frozen Config makes those reads live for free. Only the
        things that cache or capture a value need touching, and only those
        named here. The return value is what /settings reports back, so it
        lists what was actually restarted rather than everything considered.
        """
        LOGGER.debug("Applying settings change; fields=%s", sorted(changed))
        self._config = compose_from_store(self._bootstrap, self._settings_store)
        # A credential set while the bot is running has to be redacted by the
        # handler that is already installed, so it is registered before
        # anything can log it.
        self._secrets.update(
            (
                self._config.gw2_api_key,
                self._config.discord_oauth_client_secret,
                self._config.web_session_secret,
            )
        )
        restarted: list[str] = []
        restart_tasks: set[str] = set()

        if changed & {"gw2_api_key", "gw2_guild_id", "guild_member_cache_seconds"}:
            if self._guild_members is not None:
                await self._guild_members.close()
            self._rebuild_gw2_api_client()
            restarted.append("the Guild Wars 2 API client")
            # Both pollers read the guild id once before their loop, so a new
            # one only reaches them through a fresh task.
            restart_tasks |= {
                "gw2-guild-storage-poller",
                "gw2-guild-log-poller",
            }

        if "gw2_guild_id" in changed:
            self._raffle_store.bind_guild(self._config.gw2_guild_id)

        if "discord_notification_channel_id" in changed:
            self._notification_channel = None
            restarted.append("the notification channel")

        if "raffle_contribution_channel_id" in changed:
            self._raffle_contribution_channel = None
            restarted.append("the raffle contribution channel")

        if "discord_feast_notification_user_id" in changed:
            self._feast_notification_user = None

        if "event_timezone" in changed:
            self._event_timezone = ZoneInfo(self._config.event_timezone)
            restarted.append("the event timezone")

        changed_tasks = self._reconcile_poll_tasks(frozenset(restart_tasks))
        if changed_tasks:
            restarted.append(f"{len(changed_tasks)} background task(s)")

        # The calendar captures its credentials and caches per-user role
        # answers, so both a credential change and a role change need it
        # rebuilt rather than merely reconfigured.
        web_fields = {
            "web_base_url",
            "discord_oauth_client_id",
            "discord_oauth_client_secret",
            "web_session_secret",
            "web_session_ttl_seconds",
            "food_page_role_id",
            "raffle_draw_role_id",
        }
        if await self._reconcile_web_server(bool(changed & web_fields)):
            restarted.append("the web calendar")

        LOGGER.debug(
            "Applied settings change; fields=%s restarted=%s",
            sorted(changed),
            restarted,
        )
        return restarted

    def _log_legacy_variables(
        self,
        env: Mapping[str, str] | None = None,
    ) -> tuple[str, ...]:
        """Warn about environment variables the settings have taken over.

        The bot cannot edit the operator's .env, so the only thing it can do
        about a stale variable is say so on every startup until it is removed.
        The values are never named, only the variables.
        """
        present = legacy_variables_present(os.environ if env is None else env)
        if not present:
            LOGGER.debug("No legacy configuration variables are set")
            return ()
        LOGGER.warning(
            "These environment variables no longer configure the bot and are "
            "ignored; set the values with /settings and remove the variables "
            "from the environment: %s",
            ", ".join(present),
        )
        return present

    def _log_disabled_features(self) -> None:
        missing_gw2 = self._config.missing_gw2_api_settings
        if missing_gw2:
            LOGGER.warning(
                "Guild Wars 2 features are disabled because %s %s not set: "
                "Guild Storage polling, feast stock alerts, guild log polling, "
                "guild join, leave, invite and rank-change notifications, "
                "raffle deposit tracking, the overdue Trial member report, the "
                "guild member count channel description, and guild member "
                "lookups in /raffle, /check and /track",
                ", ".join(f"/settings {name}" for name in missing_gw2),
                "is" if len(missing_gw2) == 1 else "are",
            )
        if not self._config.notifications_enabled:
            LOGGER.warning(
                "Notification channel delivery is disabled because %s is not "
                "set: no guild membership, raffle audit, feast stock or Trial "
                "member message is delivered there, the guild member count "
                "channel description is not updated, and the diag previews do "
                "not run; the raffle contribution channel is unaffected",
                f"/settings {NOTIFICATION_CHANNEL_SETTING}",
            )

    @property
    def gw2_api_enabled(self) -> bool:
        return self._config.gw2_api_enabled

    async def reject_without_gw2_api(
        self,
        interaction: discord.Interaction,
        action: str,
    ) -> bool:
        """Tell a caller how to enable a command that needs the GW2 API."""
        missing = self._config.missing_gw2_api_settings
        if not missing:
            return False
        LOGGER.warning(
            "Rejected %s from Discord user %s; unset settings: %s",
            action,
            getattr(getattr(interaction, "user", None), "id", "unknown"),
            ", ".join(missing),
        )
        delivered = await send_interaction_notice(
            interaction,
            missing_configuration_message(missing),
        )
        LOGGER.debug(
            "Reported disabled command configuration; action=%s delivered=%s",
            action,
            delivered,
        )
        return True

    async def close(self) -> None:
        LOGGER.debug("Closing bot and cancelling %s poll tasks", len(self._poll_tasks))
        for task in self._poll_tasks:
            task.cancel()
        await asyncio.gather(*self._poll_tasks, return_exceptions=True)
        if self._web_server is not None:
            await self._web_server.stop()
        if self._guild_members is not None:
            await self._guild_members.close()
        if self._session is not None:
            await self._session.close()
        self._raffle_store.close()
        self._event_store.close()
        self._settings_store.close()
        await super().close()

    async def on_ready(self) -> None:
        LOGGER.info("Discord bot connected as %s", self.user)
        if self._ready_announced:
            return
        LOGGER.info("GW2 bot connected to Discord. %s", self._startup_status())
        self._ready_announced = True
        await self._announce_legacy_variables()

    async def _announce_legacy_variables(self) -> None:
        """Tell the notification channel about variables /settings replaced.

        Posted once per startup rather than on every poll: it is a migration
        notice, and repeating it would bury the messages the channel exists
        for. The console warning stands whether or not this is delivered.
        """
        if self._legacy_variables_announced:
            return
        self._legacy_variables_announced = True
        present = legacy_variables_present(os.environ)
        if not present:
            LOGGER.debug("No legacy configuration warning to deliver")
            return
        delivered = await notifications.send_legacy_configuration_warning(
            self,
            present,
        )
        LOGGER.debug(
            "Legacy configuration warning delivery finished; variables=%s "
            "delivered=%s",
            len(present),
            delivered,
        )

    def _startup_status(self) -> str:
        """Describe only the schedules that this configuration actually runs."""
        gw2_api_enabled = self._config.gw2_api_enabled
        reporting_enabled = gw2_api_enabled and self._config.notifications_enabled
        schedules: list[str] = []
        if gw2_api_enabled:
            schedules.append(
                "Storage polling every "
                f"{self._config.poll_interval_seconds} seconds"
            )
            schedules.append(
                "guild log polling every "
                f"{self._config.guild_log_poll_interval_seconds} seconds"
            )
        if reporting_enabled:
            schedules.append("overdue Trial member reporting daily at 17:00 UTC")
        schedules.append("raffle contribution reporting every 6 hours UTC")
        if reporting_enabled:
            schedules.append("guild member count topic updates every 60 seconds")
        status = "; ".join(schedules) + "."
        return status[0].upper() + status[1:]

    async def on_message(self, message: discord.Message) -> None:
        author_is_bot = bool(getattr(message.author, "bot", False))
        content = message.content.strip()
        diag_candidate = content.casefold() == "diag"
        notification_channel_id = self._config.discord_notification_channel_id
        channel_matches = notification_channel_id is not None and (
            getattr(message.channel, "id", None) == notification_channel_id
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

    async def refresh_guild_log(self) -> bool:
        return await guild_log.refresh_guild_log(self)

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
    @property
    def event_store(self) -> EventStore:
        return self._event_store

    @property
    def raffle_store(self) -> RaffleStore:
        return self._raffle_store

    @property
    def event_timezone(self) -> ZoneInfo:
        return self._event_timezone

    async def _poll_event_updates(self) -> None:
        await event_scheduler.poll_event_updates(self)

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
