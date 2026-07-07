from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

import discord

from gw2bot.guild_members import DISCORD_MESSAGE_LIMIT
from gw2bot.raffle.models import (
    RAFFLE_REWARD_TIERS,
    RaffleAudit,
    RaffleAuditDraw,
    RaffleAuditRange,
    RaffleContribution,
    RaffleDeposit,
    RaffleMilestone,
    RaffleResult,
    RaffleRewardTier,
    RaffleRunSummary,
    RaffleTotal,
    RaffleWinner,
)

LOGGER = logging.getLogger(__name__)

RAFFLE_TICKETS_PAGE_SIZE = 10
RAFFLE_BULK_SUMMARY_SAMPLE_SIZE = 10
RAFFLE_BULK_SUMMARY_NAME_LENGTH = 42
# Discord caps embed field values at 1,024 characters.
RAFFLE_AUDIT_FIELD_CHAR_LIMIT = 1_024
RAFFLE_AUDIT_RANGES_PAGE_SIZE = 20
RAFFLE_AUDIT_RUN_ID_SAMPLE_SIZE = 15
RAFFLE_AUDIT_VERIFY_FOOTER = (
    "Verify: find each drawn ticket number in the ranges above. "
    "Ranges are alphabetical by username."
)


def format_addticket_audit(discord_user_id: int, username: str) -> str:
    return f"<@{discord_user_id}> added 1 raffle ticket to {username}."


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


def _format_winner_line(position: int, winner: RaffleWinner) -> str:
    line = f"{position}. **{winner.username}**"
    chance = winner.win_chance
    if chance is not None:
        line += f" ({chance:.1%} chance)"
    return line


def raffle_result_embed(result: RaffleResult) -> discord.Embed:
    winners = "\n".join(
        _format_winner_line(position, winner)
        for position, winner in enumerate(result.winners, start=1)
    )
    embed = discord.Embed(
        title="Raffle Winners",
        description=(
            f"{winners}\n"
            f"Selected {len(result.winners)} winners from "
            f"{result.purchased_tickets} purchased tickets and "
            f"{result.free_tickets} free tickets. "
            "All current raffle tickets have been reset."
        ),
    )
    embed.set_footer(text=f"Run ID: {result.run_id}")
    return embed


def _format_ticket_range(first_ticket: int, last_ticket: int) -> str:
    if first_ticket == last_ticket:
        return f"#{first_ticket}"
    return f"#{first_ticket}–#{last_ticket}"


def _format_audit_entrant_line(entrant: RaffleAuditRange) -> str:
    noun = "ticket" if entrant.tickets == 1 else "tickets"
    return (
        f"**{entrant.username}** — "
        f"{_format_ticket_range(entrant.first_ticket, entrant.last_ticket)} "
        f"({entrant.tickets} {noun})"
    )


def _format_audit_draw_line(draw: RaffleAuditDraw) -> str:
    line = (
        f"Draw {draw.draw_position}: ticket #{draw.winning_ticket} of "
        f"{draw.tickets_before_draw} — **{draw.username}**"
    )
    details: list[str] = []
    winner_range = next(
        (
            entrant_range
            for entrant_range in draw.ranges
            if entrant_range.username == draw.username
        ),
        None,
    )
    if winner_range is not None:
        details.append(
            "held "
            + _format_ticket_range(
                winner_range.first_ticket,
                winner_range.last_ticket,
            )
        )
    chance = draw.win_chance
    if chance is not None:
        details.append(f"{chance:.1%} chance")
    if details:
        line += f" ({', '.join(details)})"
    return line


def _chunk_field_lines(lines: list[str], limit: int) -> list[str]:
    chunks: list[str] = []
    current = ""
    for line in lines:
        line = line[:limit]
        if not current:
            current = line
        elif len(current) + 1 + len(line) <= limit:
            current += "\n" + line
        else:
            chunks.append(current)
            current = line
    if current:
        chunks.append(current)
    return chunks


def format_unknown_raffle_run_message(
    run_id: int,
    summaries: list[RaffleRunSummary],
) -> str:
    if not summaries:
        return (
            f"Raffle run {run_id} was not found. "
            "No raffle draws have been recorded yet."
        )
    shown = ", ".join(
        str(summary.run_id)
        for summary in summaries[:RAFFLE_AUDIT_RUN_ID_SAMPLE_SIZE]
    )
    remaining = len(summaries) - RAFFLE_AUDIT_RUN_ID_SAMPLE_SIZE
    message = f"Raffle run {run_id} was not found. Valid run ids: {shown}"
    if remaining > 0:
        message += f" (+{remaining} more)"
    return message + "."


def raffle_audit_embeds(
    audit: RaffleAudit,
    page: int = 0,
) -> list[discord.Embed]:
    title = f"Raffle Run #{audit.run_id} Audit"
    description_lines = [f"Drawn at {audit.run_time} UTC."]
    if audit.has_entrant_snapshot:
        description_lines.append(
            "Every entrant's tickets were laid out in one numbered line, "
            "alphabetical by username, and a random ticket number picked "
            "each winner."
        )
        if len(audit.draws) > 1:
            description_lines.append(
                "After each draw one ticket was removed from that winner "
                "and the line was renumbered, so each draw shows the "
                "winner's range at that moment."
            )
    else:
        description_lines.append(
            "The full entrant snapshot isn't available for this run because "
            "it was drawn before entrant snapshots were added. Showing the "
            "recorded results only."
        )

    ranges_embed = discord.Embed(
        title=title,
        description="\n".join(description_lines),
    )
    page_count = 1
    if audit.has_entrant_snapshot:
        page_count = (
            len(audit.entrants) + RAFFLE_AUDIT_RANGES_PAGE_SIZE - 1
        ) // RAFFLE_AUDIT_RANGES_PAGE_SIZE
        page = max(0, min(page, page_count - 1))
        first = page * RAFFLE_AUDIT_RANGES_PAGE_SIZE
        page_entrants = audit.entrants[
            first : first + RAFFLE_AUDIT_RANGES_PAGE_SIZE
        ]
        entrant_noun = "entrant" if len(audit.entrants) == 1 else "entrants"
        entrant_label = f"Ticket Ranges ({len(audit.entrants)} {entrant_noun})"
        for index, chunk in enumerate(
            _chunk_field_lines(
                [
                    _format_audit_entrant_line(entrant)
                    for entrant in page_entrants
                ],
                RAFFLE_AUDIT_FIELD_CHAR_LIMIT,
            )
        ):
            ranges_embed.add_field(
                name=(
                    entrant_label if index == 0
                    else "Ticket Ranges (continued)"
                ),
                value=chunk,
                inline=False,
            )
        ranges_embed.set_footer(text=f"Page {page + 1} of {page_count}")

    results_embed = discord.Embed()
    results_embed.add_field(
        name="Ticket Pool",
        value=(
            f"Total tickets: {audit.total_tickets}\n"
            f"Purchased: {audit.purchased_tickets}\n"
            f"Free: {audit.free_tickets}"
        ),
        inline=False,
    )
    draw_label = "Draw" if len(audit.draws) == 1 else "Draws"
    for index, chunk in enumerate(
        _chunk_field_lines(
            [_format_audit_draw_line(draw) for draw in audit.draws],
            RAFFLE_AUDIT_FIELD_CHAR_LIMIT,
        )
    ):
        results_embed.add_field(
            name=draw_label if index == 0 else f"{draw_label} (continued)",
            value=chunk,
            inline=False,
        )
    if audit.has_entrant_snapshot:
        # The verify instructions reference the ranges embed, which legacy
        # runs without an entrant snapshot do not include.
        results_embed.set_footer(text=RAFFLE_AUDIT_VERIFY_FOOTER)

    LOGGER.debug(
        "Rendered raffle audit embeds; entrants=%s draws=%s page=%s "
        "page_count=%s snapshot_available=%s",
        len(audit.entrants),
        len(audit.draws),
        page + 1,
        page_count,
        audit.has_entrant_snapshot,
    )
    return [ranges_embed, results_embed]


def raffle_deposit_embed(deposit: RaffleDeposit) -> discord.Embed:
    # The contribution channel shows the classic deposit sentence inside an
    # embed so it stands out; the audit log keeps the plain-text message.
    return discord.Embed(description=deposit.message)


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


RAFFLE_TICKET_ROW_SORT_KEYS: dict[
    str,
    Callable[[RaffleTicketTableRow], int],
] = {
    "purchased": lambda row: row.purchased,
    "free": lambda row: row.free,
    "total": lambda row: row.total,
}


def order_raffle_ticket_rows(
    rows: list[RaffleTicketTableRow],
    sort_key: str,
) -> list[RaffleTicketTableRow]:
    sort_value = RAFFLE_TICKET_ROW_SORT_KEYS[sort_key]
    ordered = sorted(
        rows,
        key=lambda row: (
            -sort_value(row),
            row.name.casefold(),
            row.name,
        ),
    )
    LOGGER.debug(
        "Ordered raffle ticket rows; rows=%s sort_key=%s",
        len(ordered),
        sort_key,
    )
    return ordered


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
