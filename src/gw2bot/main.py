from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import aiohttp
import discord
from discord import app_commands
from sqlalchemy.exc import SQLAlchemyError

from gw2bot.config import Config, ConfigurationError
from gw2bot.discord_utils import (
    TopicEditableChannel,
    discord_failure_reason,
    discord_failure_signature,
    log_discord_failure,
    user_has_role as user_has_role,
)
from gw2bot.feast_stock import FeastAlert, get_due_low_stock_alerts
from gw2bot.logging_setup import (
    RedactingFormatter as RedactingFormatter,
    configure_logging as configure_logging,
    redact_log_text as redact_log_text,
)
from gw2bot.poll_status import PollStatusTracker
from gw2bot.gw2_api import Gw2ApiClient
from gw2bot.guild_members import (
    TRIAL_WARNING_MARK_HEADER,
    GuildMemberCache,
    TrialMemberReportEntry,
    format_overdue_trial_report,
)
from gw2bot.raffle import (
    GuildInvite,
    GuildJoin,
    GuildLeave,
    GuildRankChange,
    OFFICER_RANK,
    RaffleContribution,
    RaffleDeposit,
    RaffleResult,
    RaffleStore,
    RaffleTotal,
    parse_gold_deposit,
)

from gw2bot.raffle.commands import (
    RAFFLE_ADDTICKET_ROLE_ID as RAFFLE_ADDTICKET_ROLE_ID,
    RAFFLE_DRAW_ROLE_ID as RAFFLE_DRAW_ROLE_ID,
    RAFFLE_OFFICER_ROLE_ID as RAFFLE_OFFICER_ROLE_ID,
    RaffleCommands as RaffleCommands,
)
from gw2bot.raffle.formatting import (
    RAFFLE_TICKETS_PAGE_SIZE as RAFFLE_TICKETS_PAGE_SIZE,
    format_addticket_audit as format_addticket_audit,
    format_bulk_addtickets_summary as format_bulk_addtickets_summary,
    format_raffle_milestone_preview as format_raffle_milestone_preview,
    format_raffle_result as format_raffle_result,
    format_removetickets_audit as format_removetickets_audit,
    parse_squad_attendance_usernames as parse_squad_attendance_usernames,
    raffle_contribution_report_embed as raffle_contribution_report_embed,
    raffle_ticket_embed as raffle_ticket_embed,
    raffle_ticket_list_embed as raffle_ticket_list_embed,
    raffle_tier_summary_embed as raffle_tier_summary_embed,
)
from gw2bot.raffle.reports import (
    RAFFLE_CONTRIBUTION_CHANNEL_ID as RAFFLE_CONTRIBUTION_CHANNEL_ID,
    RAFFLE_CONTRIBUTION_REPORT_HOURS as RAFFLE_CONTRIBUTION_REPORT_HOURS,
    raffle_contribution_report_end as raffle_contribution_report_end,
    seconds_until_raffle_contribution_report as seconds_until_raffle_contribution_report,
)
from gw2bot.raffle.views import (
    RaffleAccountLinkModal as RaffleAccountLinkModal,
    RaffleBulkAddTicketsModal as RaffleBulkAddTicketsModal,
    RaffleContributionReportView as RaffleContributionReportView,
    RaffleTicketTableView as RaffleTicketTableView,
    RaffleTicketsListView as RaffleTicketsListView,
)

from gw2bot.trials.commands import (
    create_check_command,
    create_track_command,
    handle_check_command,
    handle_track_command,
    track_member_autocomplete,
)
from gw2bot.trials.forum import (
    TRIAL_ACCEPTED_TAG_ID as TRIAL_ACCEPTED_TAG_ID,
    TRIAL_FORUM_CHANNEL_ID as TRIAL_FORUM_CHANNEL_ID,
    TRIAL_IN_REVIEW_TAG_ID as TRIAL_IN_REVIEW_TAG_ID,
    apply_trial_forum_in_review_tag,
    refresh_trial_forum_index,
    resolve_trial_forum_tags,
)
from gw2bot.trials.reports import (
    SUNBORNE_ROLE_ID as SUNBORNE_ROLE_ID,
    TRIAL_ROLE_ID as TRIAL_ROLE_ID,
    build_trial_report_messages,
    check_overdue_trials,
    contains_normalized_account_name as contains_normalized_account_name,
    format_track_audit as format_track_audit,
    get_trial_member_discord_status as get_trial_member_discord_status,
    poll_overdue_trials,
    resolve_trial_member_discord_statuses,
)

LOGGER = logging.getLogger(__name__)


GW2_GUILD_MEMBER_LIMIT = 500
GW2_GUILD_INVITED_RANK = "invited"
GUILD_MEMBER_COUNT_TOPIC_UPDATE_SECONDS = 60


def format_automated_message_diagnostics(
    contributions: list[RaffleContribution],
    purchased_tickets: int,
    member_count: int | None = None,
    pending_invite_count: int | None = None,
) -> list[str]:
    messages = [
        (
            "**Automated message diagnostics**\n"
            "These previews are read-only and do not change scheduled or pending "
            "notifications."
        )
    ]
    if not contributions:
        messages.append(
            "No raffle contributions are currently recorded for the next "
            "six-hour report, so it would not send a message yet."
        )

    if member_count is None or pending_invite_count is None:
        guild_member_count_preview = (
            "The guild member count has not been retrieved yet, so the "
            "channel description is not set."
        )
    else:
        guild_member_count_preview = format_guild_member_count_topic(
            member_count,
            pending_invite_count,
        )

    messages.extend(
        (
            (
                "**Gold donation purchase notification (test)**\n"
                + RaffleDeposit(
                    event_id=0,
                    username="DiagnosticUser.1234",
                    coins_deposited=30_000,
                    raffle_tickets=3,
                    event_time="",
                ).message
            ),
            (
                "**Guild join notification (test)**\n"
                + GuildJoin(
                    event_id=0,
                    username="DiagnosticUser.1234",
                    event_time="",
                ).message
            ),
            (
                "**Guild leave notification (test)**\n"
                + GuildLeave(
                    event_id=0,
                    username="DiagnosticUser.1234",
                    event_time="",
                ).message
            ),
            (
                "**Guild invite notification (test)**\n"
                + GuildInvite(
                    event_id=0,
                    username="DiagnosticUser.1234",
                    event_time="",
                    invited_by="Officer.5678",
                ).message
            ),
            (
                "**Guild rank change notification (test)**\n"
                + GuildRankChange(
                    event_id=0,
                    username="DiagnosticUser.1234",
                    old_rank="Trial",
                    new_rank="Sunborne",
                    event_time="",
                    changed_by="Officer.5678",
                ).message
            ),
            (
                "**Next raffle reward tier notification (test)**\n"
                + format_raffle_milestone_preview(purchased_tickets)
            ),
            (
                "**Low feast stock notification (test)**\n"
                + FeastAlert(
                    guild_storage_id=0,
                    name="Diagnostic Feast",
                    count=5,
                ).message
                + "\nThis alert may also be sent by private message when configured."
            ),
            (
                "**Overdue Trial member report (test)**\n"
                + format_overdue_trial_report(["DiagnosticUser.1234"])[0]
            ),
            (
                "**Trial 7-day warning report (test)**\n"
                + format_overdue_trial_report(
                    ["DiagnosticUser.1234"],
                    header=TRIAL_WARNING_MARK_HEADER,
                )[0]
            ),
            (
                "**Guild member count channel description (current)**\n"
                + guild_member_count_preview
            ),
        )
    )
    return messages


async def _try_send_automated_diagnostic(
    channel: Any,
    kind: str,
    *,
    message: str | None = None,
    embed: discord.Embed | None = None,
    view: discord.ui.View | None = None,
) -> bool:
    characters = len(message or "")
    if embed is not None:
        characters += len(embed.description or "")
    LOGGER.debug(
        "Attempting automated diagnostic delivery; kind=%s characters=%s "
        "embed=%s view=%s",
        kind,
        characters,
        embed is not None,
        view is not None,
    )
    try:
        if message is not None:
            await channel.send(message)
        elif view is None:
            await channel.send(embed=embed)
        else:
            await channel.send(embed=embed, view=view)
    except Exception as exc:
        LOGGER.error(
            "Automated diagnostic delivery failed; kind=%s error_type=%s",
            kind,
            type(exc).__name__,
        )
        return False
    LOGGER.debug("Automated diagnostic delivery succeeded; kind=%s", kind)
    return True


def count_active_guild_members(
    members: list[dict[str, Any]],
) -> tuple[int, int]:
    pending_invite_count = sum(
        1
        for member in members
        if str(member.get("rank", "")).strip().casefold()
        == GW2_GUILD_INVITED_RANK
    )
    return len(members) - pending_invite_count, pending_invite_count


def format_guild_member_count_topic(
    member_count: int,
    pending_invite_count: int,
) -> str:
    return f"{member_count}/{GW2_GUILD_MEMBER_LIMIT} ({pending_invite_count} pending)"


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
        current_time = (now or datetime.now(UTC)).astimezone(UTC)
        report_start = raffle_contribution_report_end(current_time)
        contributions = self.get_raffle_contributions(report_start, current_time)
        purchased_tickets = sum(
            total.gold_raffle_tickets for total in self.get_raffle_totals()
        )
        messages = format_automated_message_diagnostics(
            contributions,
            purchased_tickets,
            self._last_guild_member_count,
            self._last_pending_guild_invite_count,
        )
        LOGGER.debug(
            "Prepared automated message diagnostics; messages=%s contributors=%s",
            len(messages),
            len(contributions),
        )
        attempted = 0
        delivered = 0
        attempted += 1
        delivered += await _try_send_automated_diagnostic(
            channel,
            "introduction",
            message=messages[0],
        )
        if contributions:
            report_view = (
                RaffleContributionReportView(contributions)
                if len(contributions) > RAFFLE_TICKETS_PAGE_SIZE
                else None
            )
            attempted += 1
            delivered += await _try_send_automated_diagnostic(
                channel,
                "contribution-report",
                embed=(
                    raffle_contribution_report_embed(contributions, 0)
                    if report_view is None
                    else report_view.embed
                ),
                view=report_view,
            )
        for index, diagnostic_message in enumerate(messages[1:], start=1):
            attempted += 1
            delivered += await _try_send_automated_diagnostic(
                channel,
                f"text-preview-{index}",
                message=diagnostic_message,
            )
        LOGGER.debug(
            "Automated message diagnostics completed; attempted=%s delivered=%s "
            "failed=%s",
            attempted,
            delivered,
            attempted - delivered,
        )

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
        LOGGER.debug(
            "Fetched %s guild log events after cursor %s",
            len(events),
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
        officer_usernames: set[str] = set()
        if any(
            int(event["id"]) > cursor and parse_gold_deposit(event) is not None
            for event in events
        ):
            if self._guild_members is None:
                raise RuntimeError("Guild member cache was not initialized")
            officer_usernames = await self._guild_members.usernames_with_rank(
                OFFICER_RANK,
                force_refresh=True,
            )
        self._raffle_store.process_events(events, officer_usernames)
        LOGGER.debug("Processed %s fetched guild log events", len(events))

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
        await self.wait_until_ready()
        LOGGER.debug("Guild Storage poller started")
        if self._session is None:
            raise RuntimeError("HTTP session was not initialized")

        if self._api is None:
            raise RuntimeError("GW2 API client was not initialized")
        while not self.is_closed():
            LOGGER.debug("Starting Guild Storage poll")
            try:
                storage = await self._api.get_guild_storage(self._config.gw2_guild_id)
                await self._handle_storage(storage)
            except (aiohttp.ClientError, asyncio.TimeoutError, SQLAlchemyError) as exc:
                self._poll_status.record_error("Guild Storage", exc)
            else:
                self._poll_status.record_success("Guild Storage")
                LOGGER.debug("Guild Storage poll completed successfully")

            await asyncio.sleep(self._config.poll_interval_seconds)

    async def _handle_storage(self, storage: list[dict[str, Any]]) -> None:
        now = time.time()
        last_alerted_at = self._raffle_store.get_feast_alert_times()
        alerts, currently_low = get_due_low_stock_alerts(
            storage,
            last_alerted_at,
            now,
        )
        LOGGER.debug(
            "Evaluated %s storage entries; low=%s due_alerts=%s",
            len(storage),
            len(currently_low),
            len(alerts),
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
        LOGGER.debug("Sending feast alert to notification channel")
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
            LOGGER.debug("Fetching feast notification user %s", user_id)
            self._feast_notification_user = await self.fetch_user(user_id)
        await self._feast_notification_user.send(message)
        LOGGER.debug("Sent feast private notification to user %s", user_id)

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
        await self.wait_until_ready()
        LOGGER.debug("Guild Member Count poller started")
        if self._api is None:
            raise RuntimeError("GW2 API client was not initialized")
        while not self.is_closed():
            LOGGER.debug("Starting Guild Member Count poll")
            try:
                updated = await self._update_guild_member_count_topic()
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                self._poll_status.record_error("Guild Member Count", exc)
            else:
                if updated:
                    self._poll_status.record_success("Guild Member Count")
                LOGGER.debug(
                    "Guild Member Count poll completed; topic_updated=%s",
                    updated,
                )

            await asyncio.sleep(GUILD_MEMBER_COUNT_TOPIC_UPDATE_SECONDS)

    async def _update_guild_member_count_topic(self) -> bool:
        if self._api is None:
            raise RuntimeError("GW2 API client was not initialized")
        members = await self._api.get_guild_members(self._config.gw2_guild_id)
        member_count, pending_invite_count = count_active_guild_members(members)
        self._last_guild_member_count = member_count
        self._last_pending_guild_invite_count = pending_invite_count
        topic = format_guild_member_count_topic(member_count, pending_invite_count)
        LOGGER.debug(
            "Fetched guild member count; records=%s members=%s "
            "pending_invites=%s topic_characters=%s",
            len(members),
            member_count,
            pending_invite_count,
            len(topic),
        )
        return await self._try_update_logging_channel_topic(topic)

    async def _try_update_logging_channel_topic(self, topic: str) -> bool:
        LOGGER.debug(
            "Updating logging channel description; characters=%s",
            len(topic),
        )
        try:
            channel = await self._get_notification_channel()
            current_topic = getattr(channel, "topic", None)
            if current_topic == topic:
                LOGGER.debug("Logging channel description already current")
                if self._last_topic_update_failure is not None:
                    self._last_topic_update_failure = None
                    LOGGER.info("Logging channel description update recovered")
                return True
            edit = getattr(channel, "edit", None)
            if not callable(edit):
                if self._last_topic_update_failure != "not_editable":
                    self._last_topic_update_failure = "not_editable"
                    LOGGER.error(
                        "Could not update logging channel description; "
                        "channel_id=%s supports_topic=false",
                        self._config.discord_notification_channel_id,
                    )
                return False
            editable_channel = cast(TopicEditableChannel, channel)
            updated_channel = await editable_channel.edit(
                topic=topic,
                reason="Update GW2 guild member count",
            )
        except discord.DiscordException as exc:
            signature = discord_failure_signature(exc)
            if self._last_topic_update_failure != signature:
                self._last_topic_update_failure = signature
                log_discord_failure(
                    "Could not update logging channel description; reason=%s "
                    "channel_id=%s "
                    "required_permissions=view_channel,manage_channels",
                    exc,
                    discord_failure_reason(exc),
                    self._config.discord_notification_channel_id,
                )
            return False
        if updated_channel is not None:
            self._notification_channel = updated_channel
        if self._last_topic_update_failure is not None:
            self._last_topic_update_failure = None
            LOGGER.info("Logging channel description update recovered")
        LOGGER.debug(
            "Updated logging channel description; characters=%s",
            len(topic),
        )
        return True

    async def _poll_raffle_contributions(self) -> None:
        await self.wait_until_ready()
        LOGGER.debug("Raffle Contributions poller started")
        while not self.is_closed():
            delay = seconds_until_raffle_contribution_report(datetime.now(UTC))
            LOGGER.debug("Raffle Contributions poll scheduled in %s seconds", delay)
            await asyncio.sleep(delay)
            if self.is_closed():
                return

            report_end = raffle_contribution_report_end(datetime.now(UTC))
            refreshed = True
            try:
                await self.refresh_guild_log()
            except (aiohttp.ClientError, asyncio.TimeoutError, SQLAlchemyError) as exc:
                refreshed = False
                LOGGER.warning(
                    "Raffle Contributions guild-log refresh failed; posting "
                    "persisted report; error_type=%s",
                    type(exc).__name__,
                )

            try:
                await self._send_raffle_contribution_report(report_end)
            except (
                asyncio.TimeoutError,
                discord.DiscordException,
                SQLAlchemyError,
            ) as exc:
                self._poll_status.record_error("Raffle Contributions", exc)
            else:
                self._poll_status.record_success("Raffle Contributions")
                LOGGER.debug(
                    "Raffle Contributions poll completed successfully; "
                    "guild_log_refreshed=%s",
                    refreshed,
                )

    async def _send_raffle_contribution_report(self, report_end: datetime) -> None:
        report_start = report_end - timedelta(
            hours=RAFFLE_CONTRIBUTION_REPORT_HOURS
        )
        contributions = self.get_raffle_contributions(report_start, report_end)
        LOGGER.debug(
            "Formatted raffle contribution report; contributors=%s",
            len(contributions),
        )
        if not contributions:
            return
        view = (
            RaffleContributionReportView(contributions)
            if len(contributions) > RAFFLE_TICKETS_PAGE_SIZE
            else None
        )
        await self._send_raffle_contribution_embed(
            raffle_contribution_report_embed(contributions, 0),
            view,
        )

    async def _send_raffle_contribution_message(self, message: str) -> None:
        LOGGER.debug(
            "Sending raffle contribution text message; characters=%s",
            len(message),
        )
        channel = await self._get_raffle_contribution_channel()
        await channel.send(message)
        LOGGER.debug("Raffle contribution text message sent")

    async def _send_raffle_contribution_embed(
        self,
        embed: discord.Embed,
        view: discord.ui.View | None,
    ) -> None:
        LOGGER.debug(
            "Sending raffle contribution embed; characters=%s view=%s",
            len(embed.description or ""),
            view is not None,
        )
        channel = await self._get_raffle_contribution_channel()
        if view is None:
            await channel.send(embed=embed)
        else:
            await channel.send(embed=embed, view=view)
        LOGGER.debug("Raffle contribution embed sent")

    async def _get_raffle_contribution_channel(self) -> Any:
        if self._raffle_contribution_channel is None:
            LOGGER.debug(
                "Fetching raffle contribution channel %s",
                RAFFLE_CONTRIBUTION_CHANNEL_ID,
            )
            channel = await self.fetch_channel(RAFFLE_CONTRIBUTION_CHANNEL_ID)
            if (
                getattr(getattr(channel, "guild", None), "id", None)
                != self._config.discord_command_guild_id
            ):
                raise discord.ClientException(
                    "Raffle contribution channel must belong to "
                    "DISCORD_COMMAND_GUILD_ID"
                )
            self._raffle_contribution_channel = channel
        return self._raffle_contribution_channel

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
        await self.wait_until_ready()
        LOGGER.debug("Guild Log poller started")
        if self._session is None:
            raise RuntimeError("HTTP session was not initialized")

        if self._api is None:
            raise RuntimeError("GW2 API client was not initialized")
        while not self.is_closed():
            LOGGER.debug("Starting Guild Log poll")
            try:
                await self.refresh_guild_log()
                await self._send_pending_raffle_notifications()
                await self._send_pending_deposit_audit_notifications()
                await self._send_pending_raffle_milestones()
                await self._send_pending_join_notifications()
                await self._send_pending_leave_notifications()
                await self._send_pending_invite_notifications()
                await self._send_pending_rank_change_notifications()
            except (aiohttp.ClientError, asyncio.TimeoutError, SQLAlchemyError) as exc:
                self._poll_status.record_error("Guild Log", exc)
            else:
                self._poll_status.record_success("Guild Log")
                LOGGER.debug("Guild Log poll completed successfully")

            await asyncio.sleep(self._config.guild_log_poll_interval_seconds)

    async def _send_pending_raffle_notifications(self) -> None:
        pending = self._raffle_store.get_pending_notifications()
        LOGGER.debug("Found %s pending raffle notifications", len(pending))
        for deposit in pending:
            if await self._try_send_raffle_contribution_message(deposit.message):
                self._raffle_store.mark_notification_sent(deposit.event_id)

    async def _send_pending_deposit_audit_notifications(self) -> None:
        pending = self._raffle_store.get_pending_deposit_audit_notifications()
        LOGGER.debug("Found %s pending raffle deposit audit notifications", len(pending))
        for deposit in pending:
            if await self._try_send_notification(deposit.message):
                self._raffle_store.mark_deposit_audit_notification_sent(
                    deposit.event_id
                )

    async def _send_pending_raffle_milestones(self) -> None:
        pending = self._raffle_store.get_pending_milestones()
        LOGGER.debug("Found %s pending raffle milestones", len(pending))
        for milestone in pending:
            if await self._try_send_raffle_contribution_message(milestone.message):
                self._raffle_store.mark_milestone_notification_sent(
                    milestone.threshold
                )

    async def _send_pending_leave_notifications(self) -> None:
        pending = self._raffle_store.get_pending_leave_notifications()
        LOGGER.debug("Found %s pending guild-leave notifications", len(pending))
        for leave in pending:
            if await self._try_send_notification(leave.message):
                self._raffle_store.mark_leave_notification_sent(leave.event_id)

    async def _send_pending_join_notifications(self) -> None:
        pending = self._raffle_store.get_pending_join_notifications()
        LOGGER.debug("Found %s pending guild-join notifications", len(pending))
        for join in pending:
            if await self._try_send_notification(join.message):
                self._raffle_store.mark_join_notification_sent(join.event_id)

    async def _send_pending_invite_notifications(self) -> None:
        pending = self._raffle_store.get_pending_invite_notifications()
        LOGGER.debug("Found %s pending guild-invite notifications", len(pending))
        for invite in pending:
            if await self._try_send_notification(invite.message):
                self._raffle_store.mark_invite_notification_sent(invite.event_id)

    async def _send_pending_rank_change_notifications(self) -> None:
        pending = self._raffle_store.get_pending_rank_change_notifications()
        LOGGER.debug(
            "Found %s pending guild-rank-change notifications",
            len(pending),
        )
        for rank_change in pending:
            if await self._try_send_notification(rank_change.message):
                self._raffle_store.mark_rank_change_notification_sent(
                    rank_change.event_id
                )

    async def _try_send_notification(self, message: str) -> bool:
        LOGGER.debug("Sending Discord notification; characters=%s", len(message))
        try:
            await self._send_notification(message)
        except discord.DiscordException as exc:
            log_discord_failure(
                "Could not send Discord notification; reason=%s channel_id=%s "
                "required_permissions=view_channel,send_messages",
                exc,
                discord_failure_reason(exc),
                self._config.discord_notification_channel_id,
            )
            return False
        LOGGER.debug("Discord notification sent")
        return True

    async def _try_send_raffle_contribution_message(self, message: str) -> bool:
        LOGGER.debug(
            "Attempting raffle contribution message delivery; characters=%s",
            len(message),
        )
        try:
            await self._send_raffle_contribution_message(message)
        except discord.DiscordException as exc:
            LOGGER.error(
                "Could not send raffle contribution message; error_type=%s",
                type(exc).__name__,
            )
            return False
        LOGGER.debug("Raffle contribution message delivery succeeded")
        return True

    async def _send_notification(self, message: str) -> None:
        channel = await self._get_notification_channel()
        await channel.send(message)

    async def _get_notification_channel(self) -> Any:
        if self._notification_channel is None:
            LOGGER.debug(
                "Fetching Discord notification channel %s",
                self._config.discord_notification_channel_id,
            )
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
            LOGGER.debug("Cached Discord notification channel")
        return self._notification_channel


def main() -> None:
    try:
        config = Config.from_env()
    except ConfigurationError as exc:
        raise SystemExit(f"Configuration error: {exc}") from exc

    configure_logging(
        config.debug,
        (config.gw2_api_key, config.discord_token),
    )
    LOGGER.debug("Debug logging enabled")
    bot = Gw2Bot(config)
    bot.run(config.discord_token, log_handler=None)
