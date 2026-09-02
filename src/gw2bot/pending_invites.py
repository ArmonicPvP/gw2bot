from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import aiohttp
import discord
from discord import app_commands
from sqlalchemy.exc import SQLAlchemyError

from gw2bot.discord_utils import log_discord_failure, user_has_role
from gw2bot.guild_members import (
    TrialMemberReportEntry,
    format_pending_invite_report,
    get_pending_invite_members,
)

if TYPE_CHECKING:
    from gw2bot.bot import Gw2Bot

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PendingInvites:
    """The accounts still holding an invite, and how well they were matched.

    ``forum_read`` is False when the Trial application forum could not be
    read. Every entry is unmatched then, which says nothing about whether the
    account applied, so a caller must not report those as confirmed
    non-matches or keep them as an answer.
    """

    entries: list[TrialMemberReportEntry]
    forum_read: bool


async def build_pending_invite_entries(bot: Gw2Bot) -> PendingInvites:
    """Every account invited in-game that has not accepted yet.

    The names come from the same guild member list the member count topic is
    built from, and each is matched to a Discord account through the Trial
    application forum index - the same matching the Trial reports use, so an
    invitee who applied is named by their mention rather than by their account
    name alone.
    """
    guild_id = bot._config.gw2_guild_id
    if bot._api is None or guild_id is None:
        raise RuntimeError("GW2 API client was not initialized")
    members = await bot._api.get_guild_members(guild_id)
    usernames = get_pending_invite_members(members)
    if not usernames:
        LOGGER.debug(
            "Built pending invite list; members=%s pending=0 matched=0 "
            "forum_read=true",
            len(members),
        )
        return PendingInvites([], True)
    # Only the match matters here: the report drops the in-game status label
    # an invited account has no rank for, and the roster page names the
    # matched Discord account itself. Asking for the status would cost a
    # member fetch per matched invite for a value nothing reads.
    matches = await bot._resolve_trial_forum_matches(
        usernames, resolve_status=False
    )
    LOGGER.debug(
        "Built pending invite list; members=%s pending=%s matched=%s "
        "forum_read=%s",
        len(members),
        len(matches.entries),
        sum(
            1
            for entry in matches.entries
            if entry.discord_user_id is not None
        ),
        matches.forum_read,
    )
    return PendingInvites(matches.entries, matches.forum_read)


async def build_pending_invite_messages(bot: Gw2Bot) -> list[str]:
    pending = await build_pending_invite_entries(bot)
    messages = format_pending_invite_report(
        pending.entries, forum_read=pending.forum_read
    )
    LOGGER.debug("Formatted pending invite report into %s messages", len(messages))
    return messages


def create_pending_command(bot: Gw2Bot) -> app_commands.Command[Any, ..., None]:
    @app_commands.command(
        name="pending",
        description="Privately post the accounts invited in-game that have "
        "not accepted yet",
    )
    @app_commands.guild_only()
    async def pending(interaction: discord.Interaction) -> None:
        await bot._handle_pending_command(interaction)

    return pending


async def handle_pending_command(
    bot: Gw2Bot,
    interaction: discord.Interaction,
) -> None:
    LOGGER.debug(
        "Pending invite command invoked by Discord user %s",
        getattr(getattr(interaction, "user", None), "id", "unknown"),
    )
    if not user_has_role(
        interaction.user,
        bot._config.raffle_officer_role_id,
    ):
        LOGGER.warning(
            "Rejected pending invite command from Discord user %s; "
            "required role %s",
            getattr(getattr(interaction, "user", None), "id", "unknown"),
            bot._config.raffle_officer_role_id,
        )
        await interaction.response.send_message(
            "You do not have the required role for this command.",
            ephemeral=True,
        )
        return

    if await bot.reject_without_gw2_api(interaction, "Pending invite command"):
        return

    await interaction.response.defer(ephemeral=True)
    try:
        messages = await bot._build_pending_invite_messages()
    except (
        aiohttp.ClientError,
        asyncio.TimeoutError,
        SQLAlchemyError,
    ) as exc:
        # The interaction is already deferred, so an unhandled failure would
        # leave the officer looking at a reply that never arrives. Only the
        # exception's type is logged; no request, response or account reaches
        # the console.
        LOGGER.error(
            "Could not build the pending invite report; error_type=%s",
            type(exc).__name__,
        )
        await interaction.followup.send(
            "Could not read the guild's pending invites. Try again later.",
            ephemeral=True,
        )
        return
    if not messages:
        LOGGER.debug("Pending invite command found no invites to report")
        await interaction.followup.send(
            "No pending invites to report.",
            ephemeral=True,
        )
        return

    LOGGER.debug(
        "Pending invite command delivering %s messages privately",
        len(messages),
    )
    delivered = 0
    for message in messages:
        try:
            await interaction.followup.send(message, ephemeral=True)
        except discord.DiscordException as error:
            # One refused page must not swallow the rest of the report, and
            # the refusal is logged by its sanitized identity rather than by
            # the page it was carrying.
            log_discord_failure(
                "Could not deliver a pending invite report page; page=%s of %s",
                error,
                delivered + 1,
                len(messages),
            )
            continue
        delivered += 1
    LOGGER.debug(
        "Pending invite command delivery completed; delivered=%s of %s",
        delivered,
        len(messages),
    )
