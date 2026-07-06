from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

import aiohttp
import discord
from discord import app_commands

from gw2bot.discord_utils import user_has_role
from gw2bot.raffle.roles import RAFFLE_OFFICER_ROLE_ID
from gw2bot.trials.reports import format_track_audit

if TYPE_CHECKING:
    from gw2bot.bot import Gw2Bot

LOGGER = logging.getLogger(__name__)


def create_check_command(bot: Gw2Bot) -> app_commands.Command[Any, ..., None]:
    @app_commands.command(
        name="check",
        description="Privately post the Trial member report on demand",
    )
    @app_commands.guild_only()
    async def check(interaction: discord.Interaction) -> None:
        await bot._handle_check_command(interaction)

    return check

async def handle_check_command(
    bot: Gw2Bot,
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
    messages = await bot._build_trial_report_messages()
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

def create_track_command(bot: Gw2Bot) -> app_commands.Command[Any, ..., None]:
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
        await bot._handle_track_command(interaction, username)

    track.autocomplete("username")(bot._track_member_autocomplete)
    return track

async def track_member_autocomplete(
    bot: Gw2Bot,
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    if not user_has_role(interaction.user, RAFFLE_OFFICER_ROLE_ID):
        LOGGER.debug("Skipped track guild member autocomplete; authorized=false")
        return []
    try:
        usernames = await bot.search_guild_members(current, limit=25)
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

async def handle_track_command(
    bot: Gw2Bot,
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
        canonical_username = await bot.resolve_guild_member(username)
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

    now_tracked = bot.toggle_trial_member_tracking(
        canonical_username,
        interaction.user.id,
    )
    audit_message = format_track_audit(
        canonical_username,
        interaction.user.id,
        tracked=now_tracked,
    )
    LOGGER.info("%s", audit_message)
    audit_sent = await bot.send_notification(audit_message)
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
