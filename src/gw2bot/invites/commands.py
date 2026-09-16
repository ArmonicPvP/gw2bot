"""`/pending`: the officer-only, ephemeral pending invite report."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

import aiohttp
import discord
from discord import app_commands
from sqlalchemy.exc import SQLAlchemyError

from gw2bot.core.discord_utils import (
    log_discord_failure,
    send_interaction_notice,
    user_has_role,
)

if TYPE_CHECKING:
    from gw2bot.bot import Gw2Bot

LOGGER = logging.getLogger(__name__)


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
        # Discord can refuse this notice too, and a guarded command must not
        # turn into an unhandled error on the way to reporting a failure.
        await send_interaction_notice(
            interaction,
            "Could not read the guild's pending invites. Try again later.",
        )
        return
    if not messages:
        LOGGER.debug("Pending invite command found no invites to report")
        await send_interaction_notice(
            interaction,
            "No pending invites to report.",
        )
        return

    LOGGER.debug(
        "Pending invite command delivering %s messages privately",
        len(messages),
    )
    delivered = 0
    # The page is numbered by where it sits in the report, not by how many
    # went out before it: with an earlier page refused those differ, and the
    # trace has to name the page that actually failed.
    for page, message in enumerate(messages, start=1):
        try:
            await interaction.followup.send(message, ephemeral=True)
        except discord.DiscordException as error:
            # One refused page must not swallow the rest of the report, and
            # the refusal is logged by its sanitized identity rather than by
            # the page it was carrying.
            log_discord_failure(
                "Could not deliver a pending invite report page; page=%s of %s",
                error,
                page,
                len(messages),
            )
            continue
        delivered += 1
    LOGGER.debug(
        "Pending invite command delivery completed; delivered=%s of %s",
        delivered,
        len(messages),
    )
