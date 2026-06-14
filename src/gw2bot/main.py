from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import UTC, datetime
from typing import Any, cast

import aiohttp
import discord
from discord import app_commands
from discord.http import Route
from sqlalchemy.exc import SQLAlchemyError

from gw2bot.config import Config, ConfigurationError
from gw2bot.feast_stock import get_due_low_stock_alerts
from gw2bot.gw2_api import Gw2ApiClient
from gw2bot.guild_members import (
    GuildMemberCache,
    TrialMemberReportEntry,
    format_overdue_trial_report,
    get_overdue_trial_members,
    seconds_until_trial_report,
)
from gw2bot.raffle import RaffleResult, RaffleStore, RaffleTotal

LOGGER = logging.getLogger(__name__)

RAFFLE_DRAW_ROLE_ID = 1317124663847157880
RAFFLE_ADDTICKET_ROLE_ID = 1318357141521825872
TRIAL_FORUM_CHANNEL_ID = 1317206104727621693
TRIAL_ROLE_ID = 1450164501696741597
SUNBORNE_ROLE_ID = 1317140660188352584
TRIAL_ACCEPTED_TAG = "Accepted"
TRIAL_SEARCH_INDEX_RETRIES = 3
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
LOG_URL_QUERY_PATTERN = re.compile(
    r"(?i)\b(https?://[^\s?\"'<>]+)\?[^\s\"'<>]*"
)
LOG_SECRET_PATTERNS = (
    re.compile(
        r"(?i)([?&](?:access_token|api[_-]?key|discord_token|gw2_api_key|"
        r"subtoken|token)=)[^&\s]+"
    ),
    re.compile(
        r"""(?ix)
        (
            ["']?
            (?:authorization|access_token|api[_-]?key|discord_token|
               gw2_api_key|subtoken|token)
            ["']?
            \s*[:=]\s*
            ["']?
            (?:(?:bearer|bot)\s+)?
        )
        [^"',}\s&]+
        """
    ),
)


def user_has_role(user: Any, required_role_id: int) -> bool:
    return any(
        role.id == required_role_id
        for role in getattr(user, "roles", ())
    )


def format_addticket_audit(discord_user_id: int, username: str) -> str:
    return f"<@{discord_user_id}> added 1 raffle ticket to {username}."


def get_trial_member_discord_status(member: Any) -> str | None:
    role_ids = {role.id for role in getattr(member, "roles", ())}
    if SUNBORNE_ROLE_ID in role_ids:
        return "Sunborne"
    if TRIAL_ROLE_ID in role_ids:
        return "Trial"
    return None


def log_discord_failure(message: str, error: discord.DiscordException, *args: object) -> None:
    LOGGER.error(
        message + " (type=%s status=%s code=%s)",
        *args,
        type(error).__name__,
        getattr(error, "status", "unknown"),
        getattr(error, "code", "unknown"),
    )


def format_poll_error(error: Exception, secrets: tuple[str, ...] = ()) -> str:
    if isinstance(error, aiohttp.ClientResponseError):
        status = f"HTTP {error.status}" if error.status else type(error).__name__
        detail = error.message.strip()
        message = f"{status}: {detail}" if detail else status
    else:
        message = str(error) or type(error).__name__

    return redact_log_text(message, secrets)


def redact_log_text(message: str, secrets: tuple[str, ...] = ()) -> str:
    message = LOG_URL_QUERY_PATTERN.sub(r"\1?[REDACTED]", message)
    for secret in sorted(
        (secret for secret in secrets if secret),
        key=len,
        reverse=True,
    ):
        message = message.replace(secret, "[REDACTED]")
    for pattern in LOG_SECRET_PATTERNS:
        message = pattern.sub(r"\1[REDACTED]", message)
    return message


class RedactingFormatter(logging.Formatter):
    def __init__(self, fmt: str, secrets: tuple[str, ...] = ()):
        super().__init__(fmt)
        self._secrets = secrets

    def format(self, record: logging.LogRecord) -> str:
        return redact_log_text(super().format(record), self._secrets)


def configure_logging(debug: bool, secrets: tuple[str, ...] = ()) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(RedactingFormatter(LOG_FORMAT, secrets))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    logging.getLogger("gw2bot").setLevel(logging.DEBUG if debug else logging.INFO)


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
        LOGGER.debug(
            "Raffle draw command invoked by Discord user %s",
            getattr(getattr(interaction, "user", None), "id", "unknown"),
        )
        if not await self._bot.authorize_raffle_command(
            interaction,
            RAFFLE_DRAW_ROLE_ID,
        ):
            return

        await interaction.response.defer()
        result = self._bot.get_pending_raffle_result()
        if result is None:
            LOGGER.debug("No pending raffle result; refreshing guild log")
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
            LOGGER.debug("Raffle draw command found no tickets")
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
        LOGGER.debug("Raffle draw command announced run %s", result.run_id)

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
        LOGGER.debug(
            "Manual raffle ticket command invoked by Discord user %s",
            getattr(getattr(interaction, "user", None), "id", "unknown"),
        )
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
            LOGGER.debug("Manual raffle ticket rejected; guild member was not found")
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
        LOGGER.debug("Manual raffle ticket audit delivered=%s", audit_sent)
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
        intents.message_content = True
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
        ]

    async def close(self) -> None:
        LOGGER.debug("Closing bot and cancelling %s poll tasks", len(self._poll_tasks))
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
        LOGGER.info(
            "GW2 bot connected to Discord. "
            f"Storage polling every {self._config.poll_interval_seconds} seconds; "
            "guild log polling every "
            f"{self._config.guild_log_poll_interval_seconds} seconds; "
            "overdue Trial member reporting daily at 17:00 UTC."
        )
        self._ready_announced = True

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

    async def resolve_guild_member(self, username: str) -> str | None:
        if self._guild_members is None:
            raise RuntimeError("Guild member cache was not initialized")
        resolved = await self._guild_members.resolve(username)
        LOGGER.debug("Guild member resolution completed; matched=%s", resolved is not None)
        return resolved

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
        self._raffle_store.process_events(events)
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
                await self._handle_poll_error("Guild Storage", exc)
            else:
                await self._handle_poll_success("Guild Storage")
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
        await self.wait_until_ready()
        LOGGER.debug("Trial Members poller started")
        while not self.is_closed():
            LOGGER.debug("Starting Trial Members poll")
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
                LOGGER.debug(
                    "Trial Members poll completed; delivered=%s next_delay=%s",
                    delivered,
                    delay,
                )

            await asyncio.sleep(delay)

    async def _check_overdue_trials(self, now: datetime | None = None) -> bool:
        if self._api is None:
            raise RuntimeError("GW2 API client was not initialized")
        members = await self._api.get_guild_members(self._config.gw2_guild_id)
        overdue = get_overdue_trial_members(members, now or datetime.now(UTC))
        LOGGER.debug(
            "Found %s overdue Trial members from %s guild members",
            len(overdue),
            len(members),
        )
        entries = await self._resolve_trial_member_discord_statuses(overdue)
        messages = format_overdue_trial_report(entries)
        LOGGER.debug("Formatted overdue Trial report into %s messages", len(messages))
        for message in messages:
            if not await self._try_send_notification(message):
                return False
        return True

    async def _resolve_trial_member_discord_statuses(
        self,
        usernames: list[str],
    ) -> list[TrialMemberReportEntry]:
        entries = [TrialMemberReportEntry(username) for username in usernames]
        unresolved = {username.casefold(): username for username in usernames}
        if not unresolved:
            return entries

        LOGGER.debug("Resolving %s Trial members from application forum", len(unresolved))
        try:
            forum = await self.fetch_channel(TRIAL_FORUM_CHANNEL_ID)
        except discord.DiscordException as error:
            log_discord_failure("Could not access the Trial application forum", error)
            return entries
        if not hasattr(forum, "archived_threads") or not hasattr(forum, "guild"):
            LOGGER.error(
                "Trial application channel %s is not a forum channel",
                TRIAL_FORUM_CHANNEL_ID,
            )
            return entries
        forum = cast(discord.ForumChannel, forum)
        accepted_tag_ids = {
            tag.id
            for tag in getattr(forum, "available_tags", ())
            if str(getattr(tag, "name", "")).casefold()
            == TRIAL_ACCEPTED_TAG.casefold()
        }
        LOGGER.debug(
            "Resolved %s Accepted forum tag IDs",
            len(accepted_tag_ids),
        )

        resolved: dict[str, TrialMemberReportEntry] = {}
        owner_statuses: dict[int, str | None] = {}
        seen_thread_ids: set[int] = set()

        def as_int(value: Any) -> int | None:
            try:
                return int(value)
            except (TypeError, ValueError):
                return None

        def has_accepted_tag(thread: Any) -> bool:
            return any(
                str(getattr(tag, "name", "")).casefold()
                == TRIAL_ACCEPTED_TAG.casefold()
                for tag in getattr(thread, "applied_tags", ())
            ) or bool(accepted_tag_ids & set(getattr(thread, "_applied_tags", ())))

        def raw_thread_has_accepted_tag(thread: dict[str, Any]) -> bool:
            applied_tag_ids = {
                tag_id
                for value in thread.get("applied_tags", ())
                if (tag_id := as_int(value)) is not None
            }
            return bool(accepted_tag_ids & applied_tag_ids)

        def contains_normalized_account_name(value: object, key: str) -> bool:
            normalized = str(value).strip().casefold()
            return (
                re.search(
                    rf"(?<![\w.]){re.escape(key)}(?![\w.])",
                    normalized,
                )
                is not None
            )

        async def resolve_owner_status(owner_id: int, owner: Any) -> str | None:
            if owner_id in owner_statuses:
                return owner_statuses[owner_id]

            status = get_trial_member_discord_status(owner)
            get_member = getattr(forum.guild, "get_member", None)
            if status is None and callable(get_member):
                status = get_trial_member_discord_status(get_member(owner_id))
            if status is None:
                LOGGER.debug(
                    "Fetching role data for matched Trial application creator %s",
                    owner_id,
                )
                try:
                    member = await forum.guild.fetch_member(owner_id)
                except discord.NotFound:
                    LOGGER.debug(
                        "Trial application creator %s is no longer a guild member",
                        owner_id,
                    )
                except discord.DiscordException as error:
                    log_discord_failure(
                        "Could not resolve Trial application creator %s",
                        error,
                        owner_id,
                    )
                else:
                    status = get_trial_member_discord_status(member)

            owner_statuses[owner_id] = status
            LOGGER.debug(
                "Resolved creator %s status=%s",
                owner_id,
                status or "unknown",
            )
            return status

        async def record_matches(
            matches: set[str],
            owner_id: int,
            owner: Any,
            owner_status: str | None = None,
        ) -> None:
            if owner_status is None:
                owner_status = await resolve_owner_status(owner_id, owner)
            for key in matches:
                resolved[key] = TrialMemberReportEntry(
                    unresolved[key],
                    discord_user_id=owner_id,
                    discord_status=owner_status,
                )
                del unresolved[key]

        async def inspect_thread(thread: Any, *, inspect_history: bool) -> None:
            thread_id = getattr(thread, "id", None)
            if thread_id in seen_thread_ids:
                return
            if thread_id is not None:
                seen_thread_ids.add(thread_id)
            if getattr(thread, "parent_id", None) != getattr(forum, "id", None):
                return
            if not has_accepted_tag(thread):
                return

            owner_id = getattr(thread, "owner_id", None)
            if owner_id is None:
                return
            owner = getattr(thread, "owner", None)
            owner_status = get_trial_member_discord_status(owner)

            thread_name = getattr(thread, "name", "")
            matches = {
                key
                for key in unresolved
                if contains_normalized_account_name(thread_name, key)
            }
            if inspect_history:
                try:
                    async for message in thread.history(limit=None, oldest_first=True):
                        author = getattr(message, "author", None)
                        if (
                            owner_status is None
                            and getattr(author, "id", None) == owner_id
                        ):
                            owner_status = get_trial_member_discord_status(author)
                        content = getattr(message, "content", "")
                        matches.update(
                            key
                            for key in unresolved
                            if contains_normalized_account_name(content, key)
                        )
                        if len(matches) == len(unresolved) and owner_status is not None:
                            break
                except discord.DiscordException as error:
                    log_discord_failure(
                        "Could not inspect Trial application forum thread %s",
                        error,
                        thread_id,
                    )

            if not matches:
                if inspect_history:
                    LOGGER.debug(
                        "Accepted forum thread %s had no username matches",
                        thread_id,
                    )
                return
            await record_matches(matches, owner_id, owner, owner_status)
            LOGGER.debug(
                "Accepted forum thread %s resolved %s usernames; remaining=%s",
                thread_id,
                len(matches),
                len(unresolved),
            )

        async def search_username(key: str, position: int, total: int) -> bool:
            try:
                request = self.http.request
            except AttributeError:
                return False

            route = Route(
                "GET",
                "/guilds/{guild_id}/messages/search",
                guild_id=forum.guild.id,
            )
            params = [
                ("content", unresolved[key]),
                ("channel_id", str(forum.id)),
                ("limit", "25"),
                ("sort_by", "relevance"),
            ]
            offset = 0
            while True:
                page_params = [*params, ("offset", str(offset))]
                response: Any = None
                for attempt in range(TRIAL_SEARCH_INDEX_RETRIES):
                    LOGGER.debug(
                        "Discord indexed search checking Trial member %s (%s/%s; attempt %s/%s)",
                        unresolved[key],
                        position,
                        total,
                        attempt + 1,
                        TRIAL_SEARCH_INDEX_RETRIES,
                    )
                    try:
                        response = await request(route, params=page_params)
                    except discord.DiscordException as error:
                        log_discord_failure(
                            "Discord message search failed for Trial member %s",
                            error,
                            unresolved[key],
                        )
                        return False
                    if not (
                        isinstance(response, dict)
                        and response.get("code") == 110000
                    ):
                        break
                    retry_after = max(float(response.get("retry_after") or 1), 0.1)
                    LOGGER.debug(
                        "Discord search index unavailable for %s; retrying in %.1f seconds",
                        unresolved[key],
                        retry_after,
                    )
                    if attempt + 1 < TRIAL_SEARCH_INDEX_RETRIES:
                        await asyncio.sleep(retry_after)
                else:
                    LOGGER.warning("Discord message search index is still unavailable")
                    return False

                if not isinstance(response, dict):
                    LOGGER.error("Discord message search returned an invalid response")
                    return False

                raw_threads = {
                    thread_id: thread
                    for thread in response.get("threads", ())
                    if isinstance(thread, dict)
                    and (thread_id := as_int(thread.get("id"))) is not None
                }
                for message_group in response.get("messages", ()):
                    if not isinstance(message_group, list):
                        continue
                    for message in message_group:
                        if not isinstance(message, dict):
                            continue
                        if not contains_normalized_account_name(
                            message.get("content", ""), key
                        ):
                            continue
                        channel_id = as_int(message.get("channel_id"))
                        if channel_id is None:
                            continue
                        thread = raw_threads.get(channel_id)
                        if thread is None:
                            continue
                        if as_int(thread.get("parent_id")) != forum.id:
                            continue
                        if not raw_thread_has_accepted_tag(thread):
                            continue
                        owner_id = as_int(thread.get("owner_id"))
                        if owner_id is None:
                            continue
                        username = unresolved[key]
                        await record_matches({key}, owner_id, None)
                        LOGGER.debug(
                            "Discord indexed search resolved %s from forum thread %s",
                            username,
                            thread.get("id"),
                        )
                        return True
                offset += 25
                if offset >= int(response.get("total_results") or 0):
                    break
            LOGGER.debug(
                "Discord indexed search found no Accepted match for %s",
                unresolved[key],
            )
            return True

        forum_threads: list[Any] = []
        try:
            active_threads = await forum.guild.active_threads()
        except discord.DiscordException as error:
            log_discord_failure(
                "Could not inspect active Trial application threads",
                error,
            )
            active_threads = []
        LOGGER.debug("Inspecting metadata for %s active forum threads", len(active_threads))
        forum_threads.extend(active_threads)
        for thread in active_threads:
            await inspect_thread(thread, inspect_history=False)
            if not unresolved:
                break

        if unresolved:
            try:
                archived_count = 0
                async for thread in forum.archived_threads(limit=None):
                    archived_count += 1
                    forum_threads.append(thread)
                    await inspect_thread(thread, inspect_history=False)
                    if not unresolved:
                        break
                LOGGER.debug(
                    "Inspected metadata for %s archived forum threads",
                    archived_count,
                )
            except discord.DiscordException as error:
                log_discord_failure(
                    "Could not inspect archived Trial application threads",
                    error,
                )
            except AttributeError:
                LOGGER.error("Could not inspect archived Trial application threads")

        if unresolved:
            indexed_search_total = len(unresolved)
            LOGGER.debug(
                "Forum title scan left %s Trial members unresolved; "
                "checking Discord indexed search without a per-member delay",
                indexed_search_total,
            )
            search_available = True
            for position, key in enumerate(list(unresolved), start=1):
                if not await search_username(key, position, indexed_search_total):
                    search_available = False
                    break
        else:
            search_available = True

        if not search_available and unresolved:
            LOGGER.warning(
                "Discord indexed search unavailable; falling back to forum history scan"
            )
            seen_thread_ids.clear()
            for thread in forum_threads:
                await inspect_thread(thread, inspect_history=True)
                if not unresolved:
                    break

        LOGGER.debug(
            "Forum resolution completed; resolved=%s unresolved=%s",
            len(resolved),
            len(unresolved),
        )
        return [resolved.get(entry.username.casefold(), entry) for entry in entries]

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
                await self._send_pending_leave_notifications()
            except (aiohttp.ClientError, asyncio.TimeoutError, SQLAlchemyError) as exc:
                await self._handle_poll_error("Guild Log", exc)
            else:
                await self._handle_poll_success("Guild Log")
                LOGGER.debug("Guild Log poll completed successfully")

            await asyncio.sleep(self._config.guild_log_poll_interval_seconds)

    async def _send_pending_raffle_notifications(self) -> None:
        pending = self._raffle_store.get_pending_notifications()
        LOGGER.debug("Found %s pending raffle notifications", len(pending))
        for deposit in pending:
            if await self._try_send_notification(deposit.message):
                self._raffle_store.mark_notification_sent(deposit.event_id)

    async def _send_pending_leave_notifications(self) -> None:
        pending = self._raffle_store.get_pending_leave_notifications()
        LOGGER.debug("Found %s pending guild-leave notifications", len(pending))
        for leave in pending:
            if await self._try_send_notification(leave.message):
                self._raffle_store.mark_leave_notification_sent(leave.event_id)

    async def _handle_poll_success(self, source: str) -> None:
        LOGGER.debug("%s poll reported success", source)
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
        LOGGER.debug("Sending Discord notification; characters=%s", len(message))
        try:
            await self._send_notification(message)
        except discord.DiscordException:
            LOGGER.exception("Could not send Discord notification")
            return False
        LOGGER.debug("Discord notification sent")
        return True

    async def _send_notification(self, message: str) -> None:
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
        await self._notification_channel.send(message)


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
