from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import discord
from discord import app_commands

from gw2bot.discord_utils import user_has_role
from gw2bot.guild_members import (
    TrialMemberReportEntry,
    format_pending_invite_report,
    get_pending_invite_members,
)

if TYPE_CHECKING:
    from gw2bot.bot import Gw2Bot

LOGGER = logging.getLogger(__name__)


async def build_pending_invite_entries(
    bot: Gw2Bot,
) -> list[TrialMemberReportEntry]:
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
            "Built pending invite list; members=%s pending=0 matched=0",
            len(members),
        )
        return []
    entries = await bot._resolve_trial_member_discord_statuses(usernames)
    LOGGER.debug(
        "Built pending invite list; members=%s pending=%s matched=%s",
        len(members),
        len(entries),
        sum(1 for entry in entries if entry.discord_user_id is not None),
    )
    return entries


async def build_pending_invite_messages(bot: Gw2Bot) -> list[str]:
    entries = await build_pending_invite_entries(bot)
    messages = format_pending_invite_report(entries)
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
    messages = await bot._build_pending_invite_messages()
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
    for message in messages:
        await interaction.followup.send(message, ephemeral=True)
