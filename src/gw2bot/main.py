from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import aiohttp
import discord
from discord import app_commands
from discord.http import Route
from sqlalchemy.exc import SQLAlchemyError

from gw2bot.config import Config, ConfigurationError
from gw2bot.feast_stock import FeastAlert, get_due_low_stock_alerts
from gw2bot.gw2_api import Gw2ApiClient
from gw2bot.guild_members import (
    GuildMemberCache,
    TrialMemberReportEntry,
    format_overdue_trial_report,
    get_overdue_trial_members,
    seconds_until_trial_report,
)
from gw2bot.raffle import (
    RAFFLE_REWARD_TIERS,
    GuildJoin,
    GuildLeave,
    OFFICER_RANK,
    RaffleContribution,
    RaffleDeposit,
    RaffleMilestone,
    RaffleResult,
    RaffleRewardTier,
    RaffleStore,
    RaffleTotal,
    parse_gold_deposit,
)

LOGGER = logging.getLogger(__name__)

RAFFLE_DRAW_ROLE_ID = 1317124663847157880
RAFFLE_ADDTICKET_ROLE_ID = 1318357141521825872
RAFFLE_TICKETS_PAGE_SIZE = 10
RAFFLE_CONTRIBUTION_CHANNEL_ID = 856343628984746014
RAFFLE_CONTRIBUTION_REPORT_HOURS = 6
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


def format_removetickets_audit(
    discord_user_id: int,
    username: str,
    amount: int,
) -> str:
    noun = "ticket" if amount == 1 else "tickets"
    return (
        f"<@{discord_user_id}> removed {amount} purchased raffle {noun} "
        f"from {username}."
    )


def format_raffle_result(result: RaffleResult) -> str:
    winners = "\n".join(
        f"{position}. **{winner.username}**"
        for position, winner in enumerate(result.winners, start=1)
    )
    return (
        f"Raffle winners:\n{winners}\n"
        f"Selected {len(result.winners)} winners from "
        f"{result.total_tickets} tickets. "
        "One winning ticket was removed from the pool after each draw. "
        "All current raffle tickets have been reset."
    )


def raffle_ticket_embed(total: RaffleTotal) -> discord.Embed:
    embed = discord.Embed(title=f"Raffle Tickets: {total.username}")
    embed.add_field(
        name="Purchased Tickets",
        value=str(total.gold_raffle_tickets),
    )
    embed.add_field(
        name="Free Tickets",
        value=str(total.manual_raffle_tickets),
    )
    embed.add_field(
        name="Total Tickets",
        value=str(total.raffle_tickets),
    )
    return embed


@dataclass(frozen=True, slots=True)
class RaffleTicketTableRow:
    name: str
    purchased: int
    free: int
    total: int


def raffle_total_table_rows(totals: list[RaffleTotal]) -> list[RaffleTicketTableRow]:
    return [
        RaffleTicketTableRow(
            name=total.username,
            purchased=total.gold_raffle_tickets,
            free=total.manual_raffle_tickets,
            total=total.raffle_tickets,
        )
        for total in totals
    ]


def order_raffle_totals(totals: list[RaffleTotal]) -> list[RaffleTotal]:
    ordered = sorted(
        totals,
        key=lambda total: (
            -total.raffle_tickets,
            total.username.casefold(),
            total.username,
        ),
    )
    LOGGER.debug("Ordered raffle totals for display; players=%s", len(ordered))
    return ordered


def raffle_contribution_table_rows(
    contributions: list[RaffleContribution],
) -> list[RaffleTicketTableRow]:
    return [
        RaffleTicketTableRow(
            name=contribution.username,
            purchased=contribution.purchased_tickets,
            free=contribution.event_tickets,
            total=contribution.purchased_tickets + contribution.event_tickets,
        )
        for contribution in contributions
    ]


def format_raffle_ticket_blocks(rows: list[RaffleTicketTableRow]) -> str:
    return "\n\n".join(
        f"**{row.name}**\n"
        f"Purchased: {row.purchased}\n"
        f"Free: {row.free}\n"
        f"Total: {row.total}"
        for row in rows
    )


def raffle_ticket_table_embed(
    rows: list[RaffleTicketTableRow],
    title: str,
    page: int,
) -> discord.Embed:
    page_count = max(
        1,
        (len(rows) + RAFFLE_TICKETS_PAGE_SIZE - 1) // RAFFLE_TICKETS_PAGE_SIZE,
    )
    page = max(0, min(page, page_count - 1))
    first = page * RAFFLE_TICKETS_PAGE_SIZE
    page_rows = rows[first : first + RAFFLE_TICKETS_PAGE_SIZE]
    embed = discord.Embed(
        title=title,
        description=format_raffle_ticket_blocks(page_rows),
    )
    embed.set_footer(text=f"Page {page + 1} of {page_count}")
    return embed


def raffle_ticket_list_embed(
    totals: list[RaffleTotal],
    page: int,
) -> discord.Embed:
    ordered_totals = order_raffle_totals(totals)
    page_count = max(
        1,
        (len(ordered_totals) + RAFFLE_TICKETS_PAGE_SIZE - 1)
        // RAFFLE_TICKETS_PAGE_SIZE,
    )
    page = max(0, min(page, page_count - 1))
    first = page * RAFFLE_TICKETS_PAGE_SIZE
    page_totals = ordered_totals[first : first + RAFFLE_TICKETS_PAGE_SIZE]
    LOGGER.debug(
        "Rendering raffle ticket list page; page=%s page_count=%s players=%s",
        page + 1,
        page_count,
        len(page_totals),
    )
    description = format_raffle_ticket_blocks(raffle_total_table_rows(page_totals))
    embed = discord.Embed(
        title="Raffle Tickets",
        description=description or "No players have raffle ticket records.",
    )
    embed.set_footer(text=f"Page {page + 1} of {page_count}")
    return embed


def raffle_contribution_report_embed(
    contributions: list[RaffleContribution],
    page: int,
) -> discord.Embed:
    return raffle_ticket_table_embed(
        raffle_contribution_table_rows(contributions),
        "Raffle contributions from the last 6 hours",
        page,
    )


def format_raffle_milestone_preview(
    purchased_tickets: int,
    reward_tiers: tuple[RaffleRewardTier, ...] = RAFFLE_REWARD_TIERS,
) -> str:
    if not reward_tiers:
        return "No raffle reward tiers are configured."

    next_tier = next(
        (
            tier
            for tier in reward_tiers
            if tier.threshold > purchased_tickets
        ),
        reward_tiers[-1],
    )
    message = RaffleMilestone(next_tier.threshold, next_tier.name).message
    if purchased_tickets >= reward_tiers[-1].threshold:
        message += " This raffle is already at the highest configured tier."
    return message


def format_automated_message_diagnostics(
    contributions: list[RaffleContribution],
    purchased_tickets: int,
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
                "**Polling status notifications (test)**\n"
                "Guild Storage polling failed: API unavailable\n"
                "Guild Storage polling recovered."
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


def raffle_contribution_report_end(now: datetime) -> datetime:
    now_utc = now.astimezone(UTC)
    return now_utc.replace(
        hour=(
            now_utc.hour // RAFFLE_CONTRIBUTION_REPORT_HOURS
        ) * RAFFLE_CONTRIBUTION_REPORT_HOURS,
        minute=0,
        second=0,
        microsecond=0,
    )


def seconds_until_raffle_contribution_report(now: datetime) -> float:
    report_end = raffle_contribution_report_end(now)
    next_report = report_end + timedelta(hours=RAFFLE_CONTRIBUTION_REPORT_HOURS)
    return (next_report - now.astimezone(UTC)).total_seconds()


class RaffleTicketTablePageButton(discord.ui.Button["RaffleTicketTableView"]):
    def __init__(self, direction: int):
        super().__init__(
            label="<" if direction < 0 else ">",
            style=discord.ButtonStyle.secondary,
        )
        self._direction = direction

    async def callback(self, interaction: discord.Interaction) -> None:
        if self.view is not None:
            await self.view.change_page(interaction, self._direction)


class RaffleTicketTableView(discord.ui.View):
    def __init__(self, rows: list[RaffleTicketTableRow], title: str):
        super().__init__(timeout=180)
        self._rows = rows
        self._title = title
        self._page = 0
        self._previous = RaffleTicketTablePageButton(-1)
        self._next = RaffleTicketTablePageButton(1)
        self.add_item(self._previous)
        self.add_item(self._next)
        self._sync_buttons()

    @property
    def embed(self) -> discord.Embed:
        return raffle_ticket_table_embed(self._rows, self._title, self._page)

    async def change_page(
        self,
        interaction: discord.Interaction,
        direction: int,
    ) -> None:
        page_count = (
            len(self._rows) + RAFFLE_TICKETS_PAGE_SIZE - 1
        ) // RAFFLE_TICKETS_PAGE_SIZE
        self._page = max(0, min(self._page + direction, page_count - 1))
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.embed, view=self)

    def _sync_buttons(self) -> None:
        page_count = (
            len(self._rows) + RAFFLE_TICKETS_PAGE_SIZE - 1
        ) // RAFFLE_TICKETS_PAGE_SIZE
        self._previous.disabled = self._page == 0
        self._next.disabled = self._page >= page_count - 1


class RaffleTicketsPageButton(discord.ui.Button["RaffleTicketsListView"]):
    def __init__(self, direction: int):
        super().__init__(
            label="<" if direction < 0 else ">",
            style=discord.ButtonStyle.secondary,
        )
        self._direction = direction

    async def callback(self, interaction: discord.Interaction) -> None:
        if self.view is not None:
            await self.view.change_page(interaction, self._direction)


class RaffleTicketsListView(discord.ui.View):
    def __init__(self, totals: list[RaffleTotal]):
        super().__init__(timeout=180)
        self._totals = order_raffle_totals(totals)
        self._page = 0
        self._previous = RaffleTicketsPageButton(-1)
        self._next = RaffleTicketsPageButton(1)
        self.add_item(self._previous)
        self.add_item(self._next)
        self._sync_buttons()

    async def change_page(
        self,
        interaction: discord.Interaction,
        direction: int,
    ) -> None:
        page_count = (
            len(self._totals) + RAFFLE_TICKETS_PAGE_SIZE - 1
        ) // RAFFLE_TICKETS_PAGE_SIZE
        self._page = max(0, min(self._page + direction, page_count - 1))
        LOGGER.debug(
            "Changing raffle ticket list page; direction=%s page=%s page_count=%s",
            direction,
            self._page + 1,
            page_count,
        )
        self._sync_buttons()
        await interaction.response.edit_message(
            embed=raffle_ticket_list_embed(self._totals, self._page),
            view=self,
        )

    def _sync_buttons(self) -> None:
        page_count = (
            len(self._totals) + RAFFLE_TICKETS_PAGE_SIZE - 1
        ) // RAFFLE_TICKETS_PAGE_SIZE
        self._previous.disabled = self._page == 0
        self._next.disabled = self._page >= page_count - 1


class RaffleContributionReportView(RaffleTicketTableView):
    def __init__(self, contributions: list[RaffleContribution]):
        super().__init__(
            raffle_contribution_table_rows(contributions),
            "Raffle contributions from the last 6 hours",
        )


class RaffleAccountLinkModal(discord.ui.Modal):
    def __init__(self, bot: Gw2Bot):
        super().__init__(title="Link GW2 Account")
        self._bot = bot
        self.username = discord.ui.TextInput(
            label="GW2 account name",
            placeholder="Username.1234",
            min_length=6,
            max_length=42,
        )
        self.add_item(self.username)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            canonical_username = await self._bot.resolve_guild_member(
                self.username.value,
                force_refresh=True,
            )
        except (aiohttp.ClientError, asyncio.TimeoutError):
            LOGGER.error("Could not refresh the guild member cache")
            await interaction.followup.send(
                "Could not verify guild membership. Try again later.",
                ephemeral=True,
            )
            return

        if canonical_username is None:
            await interaction.followup.send(
                f"`{self.username.value}` is not a member of the configured guild.",
                ephemeral=True,
            )
            return

        self._bot.link_raffle_account(interaction.user.id, canonical_username)
        await interaction.followup.send(
            f"Linked your Discord account to **{canonical_username}**.",
            embed=raffle_ticket_embed(
                self._bot.get_raffle_total(canonical_username)
            ),
            ephemeral=True,
        )


def get_trial_member_discord_status(member: Any) -> str | None:
    role_ids = {role.id for role in getattr(member, "roles", ())}
    if SUNBORNE_ROLE_ID in role_ids:
        return "Sunborne"
    if TRIAL_ROLE_ID in role_ids:
        return "Trial"
    return None


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

        await interaction.followup.send(format_raffle_result(result))
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
            LOGGER.error("Could not refresh the guild member cache")
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

    @app_commands.command(
        name="removetickets",
        description="Remove purchased raffle tickets from a guild member",
    )
    @app_commands.describe(
        username="Guild Wars 2 account name, including the four digits",
        amount="Number of purchased tickets to remove",
    )
    async def removetickets(
        self,
        interaction: discord.Interaction,
        username: str,
        amount: int = 1,
    ) -> None:
        LOGGER.debug(
            "Purchased raffle ticket removal invoked by Discord user %s",
            getattr(getattr(interaction, "user", None), "id", "unknown"),
        )
        if not await self._bot.authorize_raffle_command(
            interaction,
            RAFFLE_DRAW_ROLE_ID,
        ):
            return

        await interaction.response.defer(ephemeral=True)
        try:
            canonical_username = await self._bot.resolve_guild_member(username)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            LOGGER.error("Could not refresh the guild member cache")
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
            total = self._bot.remove_gold_raffle_tickets(
                canonical_username,
                amount,
            )
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return

        audit_message = format_removetickets_audit(
            interaction.user.id,
            canonical_username,
            amount,
        )
        LOGGER.info("%s", audit_message)
        audit_sent = await self._bot.send_notification(audit_message)
        await interaction.followup.send(
            f"Removed {amount} purchased raffle "
            f"{'ticket' if amount == 1 else 'tickets'} from "
            f"**{canonical_username}**. They now have "
            f"{total.gold_raffle_tickets} purchased and "
            f"{total.raffle_tickets} total current tickets."
            + ("" if audit_sent else " The audit log could not be delivered."),
            ephemeral=True,
        )

    @app_commands.command(
        name="tickets",
        description="View your or another player's tickets",
    )
    @app_commands.describe(
        username="Enter your GW2 account name, including the four digits",
    )
    async def tickets(
        self,
        interaction: discord.Interaction,
        username: str | None = None,
    ) -> None:
        if username is None:
            linked_username = self._bot.get_linked_raffle_username(
                interaction.user.id
            )
            if linked_username is None:
                await interaction.response.send_modal(
                    RaffleAccountLinkModal(self._bot)
                )
                return
            await interaction.response.send_message(
                embed=raffle_ticket_embed(
                    self._bot.get_raffle_total(linked_username)
                ),
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        try:
            canonical_username = await self._bot.resolve_guild_member(username)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            LOGGER.error("Could not refresh the guild member cache")
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

        await interaction.followup.send(
            embed=raffle_ticket_embed(
                self._bot.get_raffle_total(canonical_username)
            ),
            ephemeral=True,
        )

    @app_commands.command(
        name="list",
        description="List all users' tickets",
    )
    async def list_tickets(self, interaction: discord.Interaction) -> None:
        totals = self._bot.get_raffle_totals()
        LOGGER.debug("Raffle list command invoked; players=%s", len(totals))
        view = (
            RaffleTicketsListView(totals)
            if len(totals) > RAFFLE_TICKETS_PAGE_SIZE
            else None
        )
        embed = raffle_ticket_list_embed(totals, 0)
        if view is None:
            await interaction.response.send_message(embed=embed)
        else:
            await interaction.response.send_message(embed=embed, view=view)


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
            asyncio.create_task(
                self._poll_raffle_contributions(),
                name="gw2-raffle-contribution-poller",
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
            "overdue Trial member reporting daily at 17:00 UTC; "
            "raffle contribution reporting every 6 hours UTC."
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

    def add_manual_raffle_ticket(
        self,
        username: str,
    ) -> RaffleTotal:
        return self._raffle_store.add_manual_ticket(username)

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
            delay = seconds_until_trial_report(datetime.now(UTC))
            LOGGER.debug("Trial Members poll scheduled in %s seconds", delay)
            await asyncio.sleep(delay)
            if self.is_closed():
                return

            LOGGER.debug("Starting Trial Members poll")
            try:
                delivered = await self._check_overdue_trials()
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                await self._handle_poll_error("Trial Members", exc)
            else:
                await self._handle_poll_success("Trial Members")
                LOGGER.debug(
                    "Trial Members poll completed; delivered=%s",
                    delivered,
                )

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
                await self._handle_poll_error("Raffle Contributions", exc)
            else:
                await self._handle_poll_success("Raffle Contributions")
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
        entries = [TrialMemberReportEntry(username) for username in usernames]
        unresolved = {username.casefold(): username for username in usernames}
        if not unresolved:
            return entries

        LOGGER.debug("Resolving %s Trial members from application forum", len(unresolved))
        try:
            forum = await self.fetch_channel(TRIAL_FORUM_CHANNEL_ID)
        except discord.DiscordException:
            LOGGER.exception("Could not access the Trial application forum")
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
                except discord.DiscordException:
                    LOGGER.exception(
                        "Could not resolve Trial application creator %s",
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
                except discord.DiscordException:
                    LOGGER.exception(
                        "Could not inspect Trial application forum thread %s",
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
                    except discord.DiscordException:
                        LOGGER.exception(
                            "Discord message search failed for Trial member %s",
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
        except discord.DiscordException:
            LOGGER.exception("Could not inspect active Trial application threads")
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
            except (discord.DiscordException, AttributeError):
                LOGGER.exception("Could not inspect archived Trial application threads")

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
                await self._send_pending_deposit_audit_notifications()
                await self._send_pending_raffle_milestones()
                await self._send_pending_join_notifications()
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
