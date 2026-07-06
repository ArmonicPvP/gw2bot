from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import discord

from gw2bot.discord_utils import discord_failure_reason, log_discord_failure
from gw2bot.feast_stock import FeastAlert
from gw2bot.guild_members import (
    TRIAL_WARNING_MARK_HEADER,
    format_overdue_trial_report,
)
from gw2bot.member_count import format_guild_member_count_topic
from gw2bot.raffle.formatting import (
    RAFFLE_TICKETS_PAGE_SIZE,
    format_addticket_audit,
    format_raffle_milestone_preview,
    format_removetickets_audit,
    raffle_audit_embeds,
    raffle_contribution_report_embed,
    raffle_deposit_embed,
    raffle_result_embed,
)
from gw2bot.raffle.models import (
    GuildInvite,
    GuildJoin,
    GuildLeave,
    GuildRankChange,
    RaffleAudit,
    RaffleAuditDraw,
    RaffleAuditRange,
    RaffleContribution,
    RaffleDeposit,
    RaffleResult,
    RaffleWinner,
)
from gw2bot.raffle.reports import raffle_contribution_report_end
from gw2bot.raffle.views import RaffleContributionReportView

if TYPE_CHECKING:
    from gw2bot.bot import Gw2Bot

LOGGER = logging.getLogger(__name__)


def _diagnostic_raffle_result() -> RaffleResult:
    # Mirrors _diagnostic_raffle_audit so both previews tell the same story.
    return RaffleResult(
        run_id=0,
        winners=(
            RaffleWinner(
                username="DiagnosticWinner.5678",
                winning_ticket=11,
                tickets_before_draw=16,
                tickets_held=6,
            ),
            RaffleWinner(
                username="DiagnosticUser.1234",
                winning_ticket=4,
                tickets_before_draw=15,
                tickets_held=10,
            ),
        ),
        total_tickets=16,
        purchased_tickets=15,
        free_tickets=1,
    )


def _diagnostic_raffle_audit() -> RaffleAudit:
    first_ranges = (
        RaffleAuditRange("DiagnosticUser.1234", 10, 1, 10),
        RaffleAuditRange("DiagnosticWinner.5678", 6, 11, 16),
    )
    second_ranges = (
        RaffleAuditRange("DiagnosticUser.1234", 10, 1, 10),
        RaffleAuditRange("DiagnosticWinner.5678", 5, 11, 15),
    )
    return RaffleAudit(
        run_id=0,
        run_time="2026-01-01 00:00:00",
        total_tickets=16,
        purchased_tickets=15,
        free_tickets=1,
        entrants=first_ranges,
        draws=(
            RaffleAuditDraw(
                draw_position=1,
                username="DiagnosticWinner.5678",
                winning_ticket=11,
                tickets_before_draw=16,
                tickets_held=6,
                ranges=first_ranges,
            ),
            RaffleAuditDraw(
                draw_position=2,
                username="DiagnosticUser.1234",
                winning_ticket=4,
                tickets_before_draw=15,
                tickets_held=10,
                ranges=second_ranges,
            ),
        ),
    )


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
                "**Gold donation audit log (test)**\n"
                + RaffleDeposit(
                    event_id=0,
                    username="DiagnosticUser.1234",
                    coins_deposited=30_000,
                    raffle_tickets=3,
                    event_time="",
                ).message
            ),
            (
                "**Manual raffle ticket audit (test)**\n"
                + format_addticket_audit(0, "DiagnosticUser.1234")
            ),
            (
                "**Purchased ticket removal audit (test)**\n"
                + format_removetickets_audit(0, "DiagnosticUser.1234", 2)
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


async def try_send_automated_diagnostic(
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
        if message is not None and embed is not None:
            await channel.send(message, embed=embed)
        elif message is not None:
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


async def send_automated_message_diagnostics(
    bot: Gw2Bot,
    channel: Any,
    now: datetime | None = None,
) -> None:
    current_time = (now or datetime.now(UTC)).astimezone(UTC)
    report_start = raffle_contribution_report_end(current_time)
    contributions = bot.get_raffle_contributions(report_start, current_time)
    purchased_tickets = sum(
        total.gold_raffle_tickets for total in bot.get_raffle_totals()
    )
    messages = format_automated_message_diagnostics(
        contributions,
        purchased_tickets,
        bot._last_guild_member_count,
        bot._last_pending_guild_invite_count,
    )
    LOGGER.debug(
        "Prepared automated message diagnostics; messages=%s contributors=%s",
        len(messages),
        len(contributions),
    )
    attempted = 0
    delivered = 0
    attempted += 1
    delivered += await try_send_automated_diagnostic(
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
        delivered += await try_send_automated_diagnostic(
            channel,
            "contribution-report",
            embed=(
                raffle_contribution_report_embed(contributions, 0)
                if report_view is None
                else report_view.embed
            ),
            view=report_view,
        )
    attempted += 1
    delivered += await try_send_automated_diagnostic(
        channel,
        "deposit-embed-preview",
        message="**Gold donation purchase notification (test)**",
        embed=raffle_deposit_embed(
            RaffleDeposit(
                event_id=0,
                username="DiagnosticUser.1234",
                coins_deposited=30_000,
                raffle_tickets=3,
                event_time="",
            )
        ),
    )
    attempted += 1
    delivered += await try_send_automated_diagnostic(
        channel,
        "raffle-draw-preview",
        message="**Raffle draw announcement (test)**",
        embed=raffle_result_embed(_diagnostic_raffle_result()),
    )
    attempted += 1
    delivered += await try_send_automated_diagnostic(
        channel,
        "raffle-audit-preview",
        message="**Raffle audit (test)**",
        embed=raffle_audit_embeds(_diagnostic_raffle_audit())[0],
    )
    for index, diagnostic_message in enumerate(messages[1:], start=1):
        attempted += 1
        delivered += await try_send_automated_diagnostic(
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


async def try_send_notification(bot: Gw2Bot, message: str) -> bool:
    LOGGER.debug("Sending Discord notification; characters=%s", len(message))
    try:
        await bot._send_notification(message)
    except discord.DiscordException as exc:
        log_discord_failure(
            "Could not send Discord notification; reason=%s channel_id=%s "
            "required_permissions=view_channel,send_messages",
            exc,
            discord_failure_reason(exc),
            bot._config.discord_notification_channel_id,
        )
        return False
    LOGGER.debug("Discord notification sent")
    return True


async def send_notification(bot: Gw2Bot, message: str) -> None:
    channel = await bot._get_notification_channel()
    await channel.send(message)

async def get_notification_channel(bot: Gw2Bot) -> Any:
    if bot._notification_channel is None:
        LOGGER.debug(
            "Fetching Discord notification channel %s",
            bot._config.discord_notification_channel_id,
        )
        channel = await bot.fetch_channel(
            bot._config.discord_notification_channel_id
        )
        if (
            getattr(getattr(channel, "guild", None), "id", None)
            != bot._config.discord_command_guild_id
        ):
            raise discord.ClientException(
                "DISCORD_NOTIFICATION_CHANNEL_ID must belong to "
                "DISCORD_COMMAND_GUILD_ID"
            )
        bot._notification_channel = channel
        LOGGER.debug("Cached Discord notification channel")
    return bot._notification_channel
