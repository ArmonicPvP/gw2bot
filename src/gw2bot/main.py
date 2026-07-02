from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, cast

import aiohttp
import discord
from discord import app_commands
from sqlalchemy.exc import SQLAlchemyError

from gw2bot.config import Config, ConfigurationError
from gw2bot.feast_stock import FeastAlert, get_due_low_stock_alerts
from gw2bot.logging_setup import (
    RedactingFormatter as RedactingFormatter,
    configure_logging as configure_logging,
    redact_log_text,
)
from gw2bot.gw2_api import Gw2ApiClient
from gw2bot.guild_members import (
    DISCORD_MESSAGE_LIMIT,
    TRIAL_BEFORE_MARK_HEADER,
    TRIAL_WARNING_MARK_HEADER,
    GuildMemberCache,
    TrialMemberReportEntry,
    filter_sunborne_discord_entries,
    format_overdue_trial_report,
    get_overdue_trial_members,
    get_recent_trial_members,
    partition_tracked_overdue_members,
    seconds_until_trial_report,
    select_warned_overdue_members,
)
from gw2bot.raffle import (
    RAFFLE_REWARD_TIERS,
    GuildInvite,
    GuildJoin,
    GuildLeave,
    GuildRankChange,
    OFFICER_RANK,
    RaffleContribution,
    RaffleDeposit,
    RaffleMilestone,
    RaffleResult,
    RaffleRewardTier,
    RaffleStore,
    RaffleTotal,
    TrialForumPost,
    parse_gold_deposit,
)

LOGGER = logging.getLogger(__name__)


class TopicEditableChannel(Protocol):
    async def edit(
        self,
        *,
        topic: str,
        reason: str | None = None,
    ) -> Any: ...


RAFFLE_DRAW_ROLE_ID = 1317124663847157880
RAFFLE_ADDTICKET_ROLE_ID = 1318357141521825872
RAFFLE_OFFICER_ROLE_ID = 1317359168285573171
RAFFLE_TICKETS_PAGE_SIZE = 10
RAFFLE_BULK_SUMMARY_SAMPLE_SIZE = 10
RAFFLE_BULK_SUMMARY_NAME_LENGTH = 42
RAFFLE_BULK_MODAL_MAX_LENGTH = 4_000
RAFFLE_CONTRIBUTION_CHANNEL_ID = 856343628984746014
RAFFLE_CONTRIBUTION_REPORT_HOURS = 6
GW2_GUILD_MEMBER_LIMIT = 500
GW2_GUILD_INVITED_RANK = "invited"
GUILD_MEMBER_COUNT_TOPIC_UPDATE_SECONDS = 60
TRIAL_FORUM_CHANNEL_ID = 1317206104727621693
TRIAL_ROLE_ID = 1450164501696741597
SUNBORNE_ROLE_ID = 1317140660188352584
TRIAL_ACCEPTED_TAG_ID = 1317349209619562587
TRIAL_IN_REVIEW_TAG_ID = 1317349421821726790
TRIAL_FORUM_INDEX_GRACE = timedelta(hours=1)


def user_has_role(user: Any, required_role_id: int) -> bool:
    return any(
        role.id == required_role_id
        for role in getattr(user, "roles", ())
    )


def format_addticket_audit(discord_user_id: int, username: str) -> str:
    return f"<@{discord_user_id}> added 1 raffle ticket to {username}."


def format_track_audit(
    username: str,
    discord_user_id: int,
    *,
    tracked: bool,
) -> str:
    verb = "tracked" if tracked else "untracked"
    return f"{username} warning {verb} by <@{discord_user_id}>"


def parse_squad_attendance_usernames(value: str) -> list[str]:
    lines = value.splitlines()
    usernames: list[str] = []
    skipped_lines = 0
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        account_name = line.partition(",")[0].strip()
        if account_name.startswith(":"):
            account_name = account_name[1:].strip()
        if not account_name:
            skipped_lines += 1
            continue
        usernames.append(account_name)
    LOGGER.debug(
        "Parsed squad attendance text; characters=%s lines=%s usernames=%s "
        "skipped_lines=%s",
        len(value),
        len(lines),
        len(usernames),
        skipped_lines,
    )
    return usernames


def _format_bulk_username_sample(label: str, usernames: list[str]) -> str:
    displayed = [
        (
            username
            if len(username) <= RAFFLE_BULK_SUMMARY_NAME_LENGTH
            else username[: RAFFLE_BULK_SUMMARY_NAME_LENGTH - 3] + "..."
        )
        for username in usernames[:RAFFLE_BULK_SUMMARY_SAMPLE_SIZE]
    ]
    remaining = len(usernames) - len(displayed)
    suffix = f" (+{remaining} more)" if remaining else ""
    return f"{label}: " + ", ".join(f"**{name}**" for name in displayed) + suffix


def format_bulk_addtickets_summary(
    added_usernames: list[str],
    invalid_count: int,
    duplicate_usernames: list[str],
    failed_usernames: list[str],
    audit_failures: int,
) -> str:
    summary = [
        f"Added one raffle ticket to {len(added_usernames)} guild "
        f"{'member' if len(added_usernames) == 1 else 'members'}."
    ]
    if added_usernames:
        summary.append(_format_bulk_username_sample("Added", added_usernames))
    if invalid_count:
        summary.append(f"Not in the configured guild: {invalid_count}")
    if duplicate_usernames:
        summary.append(
            _format_bulk_username_sample(
                "Duplicate selections skipped",
                duplicate_usernames,
            )
        )
    if failed_usernames:
        summary.append(
            _format_bulk_username_sample("Could not add", failed_usernames)
        )
    if audit_failures:
        summary.append(
            f"{audit_failures} audit "
            f"{'delivery' if audit_failures == 1 else 'deliveries'} failed."
        )
    return "\n".join(summary)[:DISCORD_MESSAGE_LIMIT]


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
        f"{result.purchased_tickets} purchased tickets and "
        f"{result.free_tickets} free tickets. "
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
        (total for total in totals if total.raffle_tickets > 0),
        key=lambda total: (
            -total.raffle_tickets,
            total.username.casefold(),
            total.username,
        ),
    )
    LOGGER.debug(
        "Ordered raffle totals for display; records=%s active_players=%s",
        len(totals),
        len(ordered),
    )
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
        description=description or "No players currently have raffle tickets.",
    )
    embed.set_footer(text=f"Page {page + 1} of {page_count}")
    return embed


def raffle_tier_summary_embed(
    totals: list[RaffleTotal],
    reward_tiers: tuple[RaffleRewardTier, ...] = RAFFLE_REWARD_TIERS,
) -> discord.Embed:
    purchased_tickets = sum(total.gold_raffle_tickets for total in totals)
    current_tier = next(
        (
            tier
            for tier in reversed(reward_tiers)
            if tier.threshold <= purchased_tickets
        ),
        None,
    )
    next_tier = next(
        (
            tier
            for tier in reward_tiers
            if tier.threshold > purchased_tickets
        ),
        None,
    )
    embed = discord.Embed(title="Raffle Tier Summary")
    embed.add_field(
        name="Current Tier",
        value=(
            current_tier.name
            if current_tier is not None
            else ("No tier reached" if reward_tiers else "No tiers configured")
        ),
    )
    embed.add_field(
        name="Total Tickets Purchased",
        value=str(purchased_tickets),
    )
    embed.add_field(
        name="Tickets Until Next Tier",
        value=(
            str(next_tier.threshold - purchased_tickets)
            if next_tier is not None
            else ("0 (highest tier reached)" if reward_tiers else "N/A")
        ),
    )
    LOGGER.debug(
        "Rendered raffle tier summary; purchased_tickets=%s "
        "current_tier_reached=%s next_tier_exists=%s",
        purchased_tickets,
        current_tier is not None,
        next_tier is not None,
    )
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
        self._summary_embed = raffle_tier_summary_embed(totals)
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
            embeds=[
                self._summary_embed,
                raffle_ticket_list_embed(self._totals, self._page),
            ],
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


def contains_normalized_account_name(value: object, key: str) -> bool:
    normalized = str(value).strip().casefold()
    return (
        re.search(
            rf"(?<![\w.]){re.escape(key)}(?![\w.])",
            normalized,
        )
        is not None
    )


def _log_discord_failure(message: str, error: discord.DiscordException, *args: object) -> None:
    LOGGER.error(
        message + " (type=%s status=%s code=%s)",
        *args,
        type(error).__name__,
        getattr(error, "status", "unknown"),
        getattr(error, "code", "unknown"),
    )


def _discord_failure_reason(error: discord.DiscordException) -> str:
    code = getattr(error, "code", None)
    if code == 50001:
        return "missing_access"
    if code == 50013:
        return "missing_permissions"
    return "discord_error"


def _discord_failure_signature(error: discord.DiscordException) -> str:
    # Sanitized identity (no raw response body) used to deduplicate repeated
    # failure logs; mirrors the fields emitted by _log_discord_failure.
    return (
        f"{type(error).__name__}:"
        f"{getattr(error, 'status', 'unknown')}:"
        f"{getattr(error, 'code', 'unknown')}"
    )


def _safe_int(value: object) -> int | None:
    if isinstance(value, int):
        return value
    if not isinstance(value, str):
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _thread_applied_tag_ids(thread: object) -> list[int]:
    tag_ids: list[int] = []
    for tag in getattr(thread, "applied_tags", ()):
        tag_id = _safe_int(getattr(tag, "id", None))
        if tag_id is not None and tag_id not in tag_ids:
            tag_ids.append(tag_id)
    for value in getattr(thread, "_applied_tags", ()):
        tag_id = _safe_int(value)
        if tag_id is not None and tag_id not in tag_ids:
            tag_ids.append(tag_id)
    return tag_ids


def _forum_tags_for_ids(
    forum: object,
    tag_ids: set[int],
) -> dict[int, discord.ForumTag]:
    tags: dict[int, discord.ForumTag] = {}
    get_tag = getattr(forum, "get_tag", None)
    if callable(get_tag):
        for tag_id in tag_ids:
            tag = get_tag(tag_id)
            if tag is not None:
                tags[tag_id] = cast(discord.ForumTag, tag)

    for tag in getattr(forum, "available_tags", ()):
        tag_id = _safe_int(getattr(tag, "id", None))
        if tag_id in tag_ids and tag_id not in tags:
            tags[tag_id] = cast(discord.ForumTag, tag)
    return tags


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


def format_poll_error(error: Exception, secrets: tuple[str, ...] = ()) -> str:
    if isinstance(error, aiohttp.ClientResponseError):
        status = f"HTTP {error.status}" if error.status else type(error).__name__
        detail = error.message.strip()
        message = f"{status}: {detail}" if detail else status
    else:
        message = str(error) or type(error).__name__

    return redact_log_text(message, secrets)


class RaffleCommands(app_commands.Group):
    def __init__(self, bot: Gw2Bot):
        super().__init__(
            name="raffle",
            description="Manage the guild raffle",
            guild_only=True,
        )
        self._bot = bot

    async def guild_member_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        if not (
            user_has_role(interaction.user, RAFFLE_ADDTICKET_ROLE_ID)
            or user_has_role(interaction.user, RAFFLE_OFFICER_ROLE_ID)
        ):
            LOGGER.debug(
                "Skipped raffle guild member autocomplete; authorized=false"
            )
            return []
        try:
            usernames = await self._bot.search_guild_members(current, limit=25)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            LOGGER.error("Could not refresh the guild member cache for autocomplete")
            return []
        LOGGER.debug(
            "Returning raffle guild member autocomplete choices; choices=%s",
            len(usernames),
        )
        return [
            app_commands.Choice(name=username, value=username)
            for username in usernames
        ]

    async def _add_tickets_for_usernames(
        self,
        interaction: discord.Interaction,
        requested_usernames: list[str],
    ) -> None:
        LOGGER.debug(
            "Bulk manual raffle ticket processing started; requested=%s",
            len(requested_usernames),
        )
        canonical_usernames: list[str] = []
        invalid_usernames: list[str] = []
        duplicate_usernames: list[str] = []
        seen_usernames: set[str] = set()
        try:
            for username in requested_usernames:
                canonical_username = await self._bot.resolve_guild_member(username)
                if canonical_username is None:
                    invalid_usernames.append(username)
                    continue
                username_key = canonical_username.casefold()
                if username_key in seen_usernames:
                    duplicate_usernames.append(canonical_username)
                    continue
                seen_usernames.add(username_key)
                canonical_usernames.append(canonical_username)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            LOGGER.error("Could not refresh the guild member cache")
            await interaction.followup.send(
                "Could not verify guild membership. No tickets were added. "
                "Try again later.",
                ephemeral=True,
            )
            return

        added_usernames: list[str] = []
        failed_usernames: list[str] = []
        audit_failures = 0
        for canonical_username in canonical_usernames:
            try:
                self._bot.add_manual_raffle_ticket(canonical_username)
            except ValueError:
                failed_usernames.append(canonical_username)
                LOGGER.debug("Bulk manual raffle ticket addition skipped; added=false")
                continue

            added_usernames.append(canonical_username)
            audit_message = format_addticket_audit(
                interaction.user.id,
                canonical_username,
            )
            LOGGER.info("%s", audit_message)
            audit_sent = await self._bot.send_notification(audit_message)
            if not audit_sent:
                audit_failures += 1
            LOGGER.debug("Bulk manual raffle ticket audit delivered=%s", audit_sent)

        LOGGER.debug(
            "Bulk manual raffle ticket processing completed; requested=%s valid=%s "
            "added=%s invalid=%s duplicates=%s add_failures=%s audit_failures=%s",
            len(requested_usernames),
            len(canonical_usernames),
            len(added_usernames),
            len(invalid_usernames),
            len(duplicate_usernames),
            len(failed_usernames),
            audit_failures,
        )
        await interaction.followup.send(
            format_bulk_addtickets_summary(
                added_usernames,
                len(invalid_usernames),
                duplicate_usernames,
                failed_usernames,
                audit_failures,
            ),
            ephemeral=True,
        )

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
        description="Add a raffle ticket or record an Officer ticket purchase",
    )
    @app_commands.describe(
        username="Guild Wars 2 account name, including the four digits",
        amount="Purchased tickets to add; Officers only",
    )
    @app_commands.autocomplete(username=guild_member_autocomplete)
    async def addticket(
        self,
        interaction: discord.Interaction,
        username: str,
        amount: int | None = None,
    ) -> None:
        LOGGER.debug(
            "Raffle ticket addition invoked by Discord user %s; "
            "purchase_amount_supplied=%s",
            getattr(getattr(interaction, "user", None), "id", "unknown"),
            amount is not None,
        )
        required_role_id = (
            RAFFLE_OFFICER_ROLE_ID
            if amount is not None
            else RAFFLE_ADDTICKET_ROLE_ID
        )
        if not await self._bot.authorize_raffle_command(
            interaction,
            required_role_id,
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

        if amount is not None:
            try:
                total = await self._bot.add_officer_raffle_purchase(
                    canonical_username,
                    amount,
                )
            except ValueError as exc:
                LOGGER.debug("Officer raffle purchase rejected; added=false")
                await interaction.followup.send(str(exc), ephemeral=True)
                return
            LOGGER.debug(
                "Officer raffle purchase command completed; amount=%s "
                "current_purchased=%s current_total=%s",
                amount,
                total.gold_raffle_tickets,
                total.raffle_tickets,
            )
            await interaction.followup.send(
                f"Recorded **{amount} gold** deposited by "
                f"**{canonical_username}** and added {amount} purchased raffle "
                f"{'ticket' if amount == 1 else 'tickets'}. They now have "
                f"{total.gold_raffle_tickets} purchased and "
                f"{total.raffle_tickets} total current tickets.",
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
        name="addtickets",
        description="Add one raffle ticket to up to ten guild members",
    )
    @app_commands.describe(
        username1="First Guild Wars 2 account name",
        username2="Second Guild Wars 2 account name",
        username3="Third Guild Wars 2 account name",
        username4="Fourth Guild Wars 2 account name",
        username5="Fifth Guild Wars 2 account name",
        username6="Sixth Guild Wars 2 account name",
        username7="Seventh Guild Wars 2 account name",
        username8="Eighth Guild Wars 2 account name",
        username9="Ninth Guild Wars 2 account name",
        username10="Tenth Guild Wars 2 account name",
    )
    @app_commands.autocomplete(
        username1=guild_member_autocomplete,
        username2=guild_member_autocomplete,
        username3=guild_member_autocomplete,
        username4=guild_member_autocomplete,
        username5=guild_member_autocomplete,
        username6=guild_member_autocomplete,
        username7=guild_member_autocomplete,
        username8=guild_member_autocomplete,
        username9=guild_member_autocomplete,
        username10=guild_member_autocomplete,
    )
    async def addtickets(
        self,
        interaction: discord.Interaction,
        username1: str | None = None,
        username2: str | None = None,
        username3: str | None = None,
        username4: str | None = None,
        username5: str | None = None,
        username6: str | None = None,
        username7: str | None = None,
        username8: str | None = None,
        username9: str | None = None,
        username10: str | None = None,
    ) -> None:
        requested_usernames = [
            username
            for username in (
                username1,
                username2,
                username3,
                username4,
                username5,
                username6,
                username7,
                username8,
                username9,
                username10,
            )
            if username is not None and username.strip()
        ]
        LOGGER.debug(
            "Bulk manual raffle ticket command invoked; requested=%s",
            len(requested_usernames),
        )
        if not await self._bot.authorize_raffle_command(
            interaction,
            RAFFLE_ADDTICKET_ROLE_ID,
        ):
            return
        if not requested_usernames:
            LOGGER.debug("Bulk manual raffle ticket command rejected; requested=0")
            await interaction.response.send_message(
                "Select at least one guild member.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        await self._add_tickets_for_usernames(interaction, requested_usernames)

    @app_commands.command(
        name="bulkaddtickets",
        description="Paste squad attendance to add raffle tickets",
    )
    async def bulkaddtickets(self, interaction: discord.Interaction) -> None:
        LOGGER.debug("Bulk attendance raffle ticket command invoked")
        if not await self._bot.authorize_raffle_command(
            interaction,
            RAFFLE_ADDTICKET_ROLE_ID,
        ):
            return
        await interaction.response.send_modal(RaffleBulkAddTicketsModal(self))

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
        active_totals = [
            total for total in totals if total.raffle_tickets > 0
        ]
        LOGGER.debug(
            "Raffle list command invoked; records=%s active_players=%s",
            len(totals),
            len(active_totals),
        )
        view = (
            RaffleTicketsListView(active_totals)
            if len(active_totals) > RAFFLE_TICKETS_PAGE_SIZE
            else None
        )
        embeds = [
            raffle_tier_summary_embed(active_totals),
            raffle_ticket_list_embed(active_totals, 0),
        ]
        if view is None:
            await interaction.response.send_message(embeds=embeds)
        else:
            await interaction.response.send_message(embeds=embeds, view=view)

    @app_commands.command(
        name="leaderboard",
        description="List every user's lifetime earned and purchased tickets",
    )
    async def leaderboard(self, interaction: discord.Interaction) -> None:
        contributions = self._bot.get_lifetime_raffle_contributions()
        LOGGER.debug(
            "Raffle leaderboard command invoked; contributors=%s",
            len(contributions),
        )
        if not contributions:
            await interaction.response.send_message(
                "No lifetime raffle tickets have been recorded yet."
            )
            return
        rows = raffle_contribution_table_rows(contributions)
        title = "Lifetime raffle tickets"
        view = (
            RaffleTicketTableView(rows, title)
            if len(rows) > RAFFLE_TICKETS_PAGE_SIZE
            else None
        )
        embed = raffle_ticket_table_embed(rows, title, 0)
        if view is None:
            await interaction.response.send_message(embed=embed)
        else:
            await interaction.response.send_message(embed=embed, view=view)


class RaffleBulkAddTicketsModal(discord.ui.Modal):
    def __init__(self, commands: RaffleCommands):
        super().__init__(title="Bulk Add Raffle Tickets")
        self._commands = commands
        self.attendance = discord.ui.TextInput(
            label="Squad attendance",
            style=discord.TextStyle.paragraph,
            placeholder=":Username.1234, Character Name",
            min_length=1,
            max_length=RAFFLE_BULK_MODAL_MAX_LENGTH,
        )
        self.add_item(self.attendance)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        LOGGER.debug(
            "Bulk attendance raffle ticket modal submitted; characters=%s",
            len(self.attendance.value),
        )
        if not await self._commands._bot.authorize_raffle_command(
            interaction,
            RAFFLE_ADDTICKET_ROLE_ID,
        ):
            return

        await interaction.response.defer(ephemeral=True)
        requested_usernames = parse_squad_attendance_usernames(
            self.attendance.value
        )
        if not requested_usernames:
            LOGGER.debug(
                "Bulk attendance raffle ticket modal rejected; usernames=0"
            )
            await interaction.followup.send(
                "No GW2 account names were found in the pasted attendance text.",
                ephemeral=True,
            )
            return
        await self._commands._add_tickets_for_usernames(
            interaction,
            requested_usernames,
        )


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
        thread_id = getattr(thread, "id", "unknown")
        parent_id = getattr(thread, "parent_id", None)
        LOGGER.debug(
            "Discord thread created; thread_id=%s parent_id=%s",
            thread_id,
            parent_id,
        )
        if parent_id != TRIAL_FORUM_CHANNEL_ID:
            LOGGER.debug(
                "Ignoring created thread outside Trial application forum; "
                "thread_id=%s",
                thread_id,
            )
            return

        applied_tag_ids = _thread_applied_tag_ids(thread)
        if TRIAL_IN_REVIEW_TAG_ID in applied_tag_ids:
            LOGGER.debug(
                "Trial application forum thread %s already has In Review tag",
                thread_id,
            )
            return

        tag_ids_to_resolve = {*applied_tag_ids, TRIAL_IN_REVIEW_TAG_ID}
        resolved_tags = await self._resolve_trial_forum_tags(
            thread,
            tag_ids_to_resolve,
        )
        in_review_tag = resolved_tags.get(TRIAL_IN_REVIEW_TAG_ID)
        if in_review_tag is None:
            LOGGER.error(
                "Could not apply In Review tag to Trial application forum "
                "thread %s; tag_id=%s not found",
                thread_id,
                TRIAL_IN_REVIEW_TAG_ID,
            )
            return

        edit_tags = [
            resolved_tags[tag_id]
            for tag_id in applied_tag_ids
            if tag_id in resolved_tags
        ]
        unresolved_existing_tags = len(applied_tag_ids) - len(edit_tags)
        if unresolved_existing_tags:
            LOGGER.warning(
                "Could not apply In Review tag to Trial application forum "
                "thread %s; unresolved_existing_tags=%s",
                thread_id,
                unresolved_existing_tags,
            )
            return
        if len(edit_tags) >= 5:
            LOGGER.warning(
                "Could not apply In Review tag to Trial application forum "
                "thread %s; existing_tags=%s tag_limit=5",
                thread_id,
                len(edit_tags),
            )
            return

        LOGGER.debug(
            "Applying In Review tag to Trial application forum thread %s; "
            "existing_tags=%s",
            thread_id,
            len(edit_tags),
        )
        try:
            await thread.edit(
                applied_tags=[*edit_tags, in_review_tag],
                reason="Automatically apply In Review tag",
            )
        except discord.DiscordException as error:
            _log_discord_failure(
                "Could not apply In Review tag to Trial application forum thread %s",
                error,
                thread_id,
            )
            return
        LOGGER.debug(
            "Applied In Review tag to Trial application forum thread %s; "
            "tag_count=%s",
            thread_id,
            len(edit_tags) + 1,
        )

    async def _resolve_trial_forum_tags(
        self,
        thread: discord.Thread,
        tag_ids: set[int],
    ) -> dict[int, discord.ForumTag]:
        parent = getattr(thread, "parent", None)
        tags = _forum_tags_for_ids(parent, tag_ids)
        missing_tag_ids = tag_ids - set(tags)
        if not missing_tag_ids:
            LOGGER.debug(
                "Resolved %s Trial application forum tags from thread parent cache",
                len(tags),
            )
            return tags

        LOGGER.debug(
            "Trial application forum tag metadata missing from cache; "
            "missing_tags=%s",
            len(missing_tag_ids),
        )
        try:
            forum = await self.fetch_channel(TRIAL_FORUM_CHANNEL_ID)
        except discord.DiscordException as error:
            _log_discord_failure(
                "Could not fetch Trial application forum while resolving %s tag IDs",
                error,
                len(missing_tag_ids),
            )
            return tags

        fetched_tags = _forum_tags_for_ids(forum, missing_tag_ids)
        tags.update(fetched_tags)
        LOGGER.debug(
            "Resolved %s Trial application forum tags from fetched forum; "
            "unresolved_tags=%s",
            len(fetched_tags),
            len(tag_ids - set(tags)),
        )
        return tags

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

    async def _build_trial_report_messages(
        self,
        now: datetime | None = None,
    ) -> list[str]:
        if self._api is None:
            raise RuntimeError("GW2 API client was not initialized")
        now = now or datetime.now(UTC)
        members = await self._api.get_guild_members(self._config.gw2_guild_id)
        overdue = get_overdue_trial_members(members, now)
        recent = get_recent_trial_members(members, now)
        tracked_times = self.get_tracked_trial_member_times()
        untracked_overdue, tracked_overdue, stale_tracked = (
            partition_tracked_overdue_members(overdue, set(tracked_times))
        )
        for username in stale_tracked:
            self.untrack_trial_member(username)
        warned_overdue = select_warned_overdue_members(
            tracked_overdue,
            tracked_times,
            now,
        )
        LOGGER.debug(
            "Found %s overdue (%s tracked, %s past 7-day warning) and %s recent "
            "Trial members from %s guild members; auto_untracked=%s",
            len(overdue),
            len(tracked_overdue),
            len(warned_overdue),
            len(recent),
            len(members),
            len(stale_tracked),
        )
        recent_entries = await self._resolve_trial_member_discord_statuses(recent)
        before_mark_entries = filter_sunborne_discord_entries(recent_entries)
        overdue_entries = await self._resolve_trial_member_discord_statuses(
            untracked_overdue
        )
        warning_entries = await self._resolve_trial_member_discord_statuses(
            warned_overdue
        )
        messages = (
            format_overdue_trial_report(
                before_mark_entries,
                header=TRIAL_BEFORE_MARK_HEADER,
            )
            + format_overdue_trial_report(overdue_entries)
            + format_overdue_trial_report(
                warning_entries,
                header=TRIAL_WARNING_MARK_HEADER,
            )
        )
        LOGGER.debug("Formatted Trial report into %s messages", len(messages))
        return messages

    async def _check_overdue_trials(self, now: datetime | None = None) -> bool:
        messages = await self._build_trial_report_messages(now)
        for message in messages:
            if not await self._try_send_notification(message):
                return False
        return True

    def _create_check_command(self) -> app_commands.Command[Any, ..., None]:
        @app_commands.command(
            name="check",
            description="Privately post the Trial member report on demand",
        )
        @app_commands.guild_only()
        async def check(interaction: discord.Interaction) -> None:
            await self._handle_check_command(interaction)

        return check

    async def _handle_check_command(
        self,
        interaction: discord.Interaction,
    ) -> None:
        LOGGER.debug(
            "Trial member check command invoked by Discord user %s",
            getattr(getattr(interaction, "user", None), "id", "unknown"),
        )
        if not user_has_role(interaction.user, RAFFLE_OFFICER_ROLE_ID):
            LOGGER.warning(
                "Rejected Trial member check command from Discord user %s; "
                "required role %s",
                getattr(getattr(interaction, "user", None), "id", "unknown"),
                RAFFLE_OFFICER_ROLE_ID,
            )
            await interaction.response.send_message(
                "You do not have the required role for this command.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        messages = await self._build_trial_report_messages()
        if not messages:
            LOGGER.debug("Trial member check command found no members to report")
            await interaction.followup.send(
                "No Trial members to report.",
                ephemeral=True,
            )
            return

        LOGGER.debug(
            "Trial member check command delivering %s messages privately",
            len(messages),
        )
        for message in messages:
            await interaction.followup.send(message, ephemeral=True)

    def _create_track_command(self) -> app_commands.Command[Any, ..., None]:
        @app_commands.command(
            name="track",
            description="Toggle a Trial member's 7-day warning tracking",
        )
        @app_commands.describe(
            username="Guild Wars 2 account name, including the four digits",
        )
        @app_commands.guild_only()
        async def track(
            interaction: discord.Interaction,
            username: str,
        ) -> None:
            await self._handle_track_command(interaction, username)

        track.autocomplete("username")(self._track_member_autocomplete)
        return track

    async def _track_member_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        if not user_has_role(interaction.user, RAFFLE_OFFICER_ROLE_ID):
            LOGGER.debug("Skipped track guild member autocomplete; authorized=false")
            return []
        try:
            usernames = await self.search_guild_members(current, limit=25)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            LOGGER.error("Could not refresh the guild member cache for autocomplete")
            return []
        LOGGER.debug(
            "Returning track guild member autocomplete choices; choices=%s",
            len(usernames),
        )
        return [
            app_commands.Choice(name=username, value=username)
            for username in usernames
        ]

    async def _handle_track_command(
        self,
        interaction: discord.Interaction,
        username: str,
    ) -> None:
        LOGGER.debug(
            "Trial member track command invoked by Discord user %s",
            getattr(getattr(interaction, "user", None), "id", "unknown"),
        )
        if not user_has_role(interaction.user, RAFFLE_OFFICER_ROLE_ID):
            LOGGER.warning(
                "Rejected Trial member track command from Discord user %s; "
                "required role %s",
                getattr(getattr(interaction, "user", None), "id", "unknown"),
                RAFFLE_OFFICER_ROLE_ID,
            )
            await interaction.response.send_message(
                "You do not have the required role for this command.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        try:
            canonical_username = await self.resolve_guild_member(username)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            LOGGER.error("Could not refresh the guild member cache")
            await interaction.followup.send(
                "Could not verify guild membership. Try again later.",
                ephemeral=True,
            )
            return

        if canonical_username is None:
            LOGGER.debug("Trial member track rejected; guild member was not found")
            await interaction.followup.send(
                f"`{username}` is not a member of the configured guild.",
                ephemeral=True,
            )
            return

        now_tracked = self.toggle_trial_member_tracking(
            canonical_username,
            interaction.user.id,
        )
        audit_message = format_track_audit(
            canonical_username,
            interaction.user.id,
            tracked=now_tracked,
        )
        LOGGER.info("%s", audit_message)
        audit_sent = await self.send_notification(audit_message)
        LOGGER.debug(
            "Trial member track toggle completed; now_tracked=%s audit_delivered=%s",
            now_tracked,
            audit_sent,
        )
        if now_tracked:
            reply = (
                f"Now tracking **{canonical_username}** for the 7-day warning. "
                "They are removed from the past-14-day report and will appear on "
                "the 7-day warning report once 7 days have passed."
            )
        else:
            reply = (
                f"Stopped tracking **{canonical_username}**. They return to the "
                "past-14-day report."
            )
        if not audit_sent:
            reply += " The audit log could not be delivered."
        await interaction.followup.send(reply, ephemeral=True)

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
                await self._handle_poll_error("Guild Member Count", exc)
            else:
                if updated:
                    await self._handle_poll_success("Guild Member Count")
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
            signature = _discord_failure_signature(exc)
            if self._last_topic_update_failure != signature:
                self._last_topic_update_failure = signature
                _log_discord_failure(
                    "Could not update logging channel description; reason=%s "
                    "channel_id=%s "
                    "required_permissions=view_channel,manage_channels",
                    exc,
                    _discord_failure_reason(exc),
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
        except discord.DiscordException as error:
            _log_discord_failure("Could not access the Trial application forum", error)
            return entries
        if not hasattr(forum, "archived_threads") or not hasattr(forum, "guild"):
            LOGGER.error(
                "Trial application channel %s is not a forum channel",
                TRIAL_FORUM_CHANNEL_ID,
            )
            return entries
        forum = cast(discord.ForumChannel, forum)

        await self._refresh_trial_forum_index(forum)
        index = self._raffle_store.get_trial_forum_index()
        LOGGER.debug(
            "Matching %s unresolved Trial members against %s indexed forum posts",
            len(unresolved),
            len(index),
        )

        resolved: dict[str, TrialMemberReportEntry] = {}
        owner_statuses: dict[int, str | None] = {}

        async def resolve_owner_status(owner_id: int) -> str | None:
            if owner_id in owner_statuses:
                return owner_statuses[owner_id]

            status: str | None = None
            get_member = getattr(forum.guild, "get_member", None)
            if callable(get_member):
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
                    _log_discord_failure(
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

        for post in sorted(index.values(), key=lambda entry: entry.thread_id):
            if not unresolved:
                break
            if post.owner_id is None:
                continue
            matched_keys = [
                key
                for key in unresolved
                if contains_normalized_account_name(post.normalized_content, key)
            ]
            if not matched_keys:
                continue
            owner_status = await resolve_owner_status(post.owner_id)
            for key in matched_keys:
                resolved[key] = TrialMemberReportEntry(
                    unresolved[key],
                    discord_user_id=post.owner_id,
                    discord_status=owner_status,
                )
                del unresolved[key]
            LOGGER.debug(
                "Trial forum index post %s resolved %s usernames; remaining=%s",
                post.thread_id,
                len(matched_keys),
                len(unresolved),
            )

        LOGGER.debug(
            "Forum index resolution completed; resolved=%s unresolved=%s",
            len(resolved),
            len(unresolved),
        )
        return [resolved.get(entry.username.casefold(), entry) for entry in entries]

    async def _refresh_trial_forum_index(
        self,
        forum: discord.ForumChannel,
    ) -> None:
        cached = self._raffle_store.get_trial_forum_index()
        watermark = self._raffle_store.get_trial_forum_watermark()
        run_start = datetime.now(UTC)
        threshold = (
            watermark - TRIAL_FORUM_INDEX_GRACE if watermark is not None else None
        )
        cold_build = threshold is None

        upserts: list[TrialForumPost] = []
        deletions: set[int] = set()
        enumerated = 0
        indexed = 0
        reused = 0
        completed = True

        def thread_last_activity(thread: Any) -> datetime:
            candidates: list[datetime] = []
            last_message_id = _safe_int(getattr(thread, "last_message_id", None))
            if last_message_id:
                candidates.append(discord.utils.snowflake_time(last_message_id))
            for attribute in ("archive_timestamp", "created_at"):
                value = getattr(thread, attribute, None)
                if isinstance(value, datetime):
                    candidates.append(value)
            if not candidates:
                return run_start
            return max(candidate.astimezone(UTC) for candidate in candidates)

        async def index_thread(thread: Any) -> None:
            nonlocal indexed, reused, completed
            thread_id = _safe_int(getattr(thread, "id", None))
            if thread_id is None:
                return
            if getattr(thread, "parent_id", None) != getattr(forum, "id", None):
                return
            if TRIAL_ACCEPTED_TAG_ID not in _thread_applied_tag_ids(thread):
                if thread_id in cached:
                    deletions.add(thread_id)
                return
            last_activity = thread_last_activity(thread)
            existing = cached.get(thread_id)
            if (
                existing is not None
                and threshold is not None
                and last_activity < threshold
            ):
                reused += 1
                return
            owner_id = _safe_int(getattr(thread, "owner_id", None))
            content_parts = [str(getattr(thread, "name", ""))]
            try:
                async for message in thread.history(limit=None, oldest_first=True):
                    content_parts.append(str(getattr(message, "content", "")))
            except discord.DiscordException as error:
                completed = False
                _log_discord_failure(
                    "Could not index Trial application forum thread %s",
                    error,
                    thread_id,
                )
                return
            upserts.append(
                TrialForumPost(
                    thread_id=thread_id,
                    owner_id=owner_id,
                    normalized_content="\n".join(content_parts).casefold(),
                    last_activity=last_activity.isoformat(),
                )
            )
            indexed += 1

        try:
            active_threads = await forum.guild.active_threads()
        except discord.DiscordException as error:
            completed = False
            _log_discord_failure(
                "Could not enumerate active Trial application threads",
                error,
            )
            active_threads = []
        for thread in active_threads:
            enumerated += 1
            await index_thread(thread)

        try:
            async for thread in forum.archived_threads(limit=None):
                if not cold_build and threshold is not None:
                    archive_ts = getattr(thread, "archive_timestamp", None)
                    if (
                        isinstance(archive_ts, datetime)
                        and archive_ts.astimezone(UTC) < threshold
                    ):
                        break
                enumerated += 1
                await index_thread(thread)
        except discord.DiscordException as error:
            completed = False
            _log_discord_failure(
                "Could not enumerate archived Trial application threads",
                error,
            )
        except AttributeError:
            completed = False
            LOGGER.error("Could not enumerate archived Trial application threads")

        self._raffle_store.upsert_trial_forum_posts(upserts)
        self._raffle_store.delete_trial_forum_posts(deletions)
        if completed:
            self._raffle_store.set_trial_forum_watermark(run_start)
        LOGGER.debug(
            "Trial forum index refreshed; enumerated=%s indexed=%s reused=%s "
            "deleted=%s cold_build=%s completed=%s",
            enumerated,
            indexed,
            reused,
            len(deletions),
            cold_build,
            completed,
        )

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

    async def _handle_poll_success(self, source: str) -> None:
        # Poll status is operational noise (timeouts, transient API errors), so
        # it stays in the console and is never posted to the logging channel.
        LOGGER.debug("%s poll reported success", source)
        if source in self._last_errors:
            LOGGER.info("%s polling recovered.", source)
            del self._last_errors[source]

    async def _handle_poll_error(self, source: str, error: Exception) -> None:
        # Poll failures (including timeouts) are console-only diagnostics; they
        # are deliberately kept out of the logging channel.
        config = getattr(self, "_config", None)
        message = format_poll_error(
            error,
            (
                getattr(config, "gw2_api_key", ""),
                getattr(config, "discord_token", ""),
            ),
        )
        LOGGER.warning("%s polling failed: %s", source, message)
        self._last_errors[source] = message

    async def _try_send_notification(self, message: str) -> bool:
        LOGGER.debug("Sending Discord notification; characters=%s", len(message))
        try:
            await self._send_notification(message)
        except discord.DiscordException as exc:
            _log_discord_failure(
                "Could not send Discord notification; reason=%s channel_id=%s "
                "required_permissions=view_channel,send_messages",
                exc,
                _discord_failure_reason(exc),
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
