from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

import aiohttp
import discord
from discord import app_commands
from sqlalchemy.exc import SQLAlchemyError

from gw2bot.discord_utils import send_interaction_notice, user_has_role
from gw2bot.gold.import_log import format_import_result, import_gold_history

if TYPE_CHECKING:
    from gw2bot.bot import Gw2Bot

LOGGER = logging.getLogger(__name__)


class GoldCommands(app_commands.Group):
    """The /gold command tree.

    One subcommand so far: the one-time import that recovers the guild bank's
    balance history from the guild log and the stash the API reports.
    """

    def __init__(self, bot: Gw2Bot):
        super().__init__(
            name="gold",
            description="Guild bank gold history",
            guild_only=True,
        )
        self._bot = bot
        self.add_command(self._build_import_command())

    def _build_import_command(self) -> app_commands.Command[Any, ..., None]:
        async def callback(interaction: discord.Interaction) -> None:
            await self._handle_import(interaction)

        return app_commands.Command(
            name="import",
            description=(
                "Read the guild log and the stash into the gold graph "
                "(one-time)"
            ),
            callback=callback,  # type: ignore[arg-type]
        )

    async def authorize(self, interaction: discord.Interaction) -> bool:
        officer_role_id = self._bot._config.raffle_officer_role_id
        if user_has_role(interaction.user, officer_role_id):
            LOGGER.debug(
                "Authorized gold command; user_id=%s",
                interaction.user.id,
            )
            return True
        LOGGER.warning(
            "Rejected gold command from Discord user %s; required role %s",
            interaction.user.id,
            officer_role_id,
        )
        await send_interaction_notice(
            interaction,
            "You do not have the required role for this command.",
        )
        return False

    async def _handle_import(self, interaction: discord.Interaction) -> None:
        LOGGER.debug(
            "Gold history import invoked by Discord user %s",
            getattr(getattr(interaction, "user", None), "id", "unknown"),
        )
        if not await self.authorize(interaction):
            return
        if await self._bot.reject_without_gw2_api(
            interaction,
            "gold history import command",
        ):
            return
        # Two Guild Wars 2 calls and a batch of writes take longer than the
        # three seconds Discord allows a command to answer in.
        await interaction.response.defer(ephemeral=True)
        try:
            result = await import_gold_history(self._bot)
        except (aiohttp.ClientError, asyncio.TimeoutError) as error:
            LOGGER.error(
                "The gold history import could not read the Guild Wars 2 "
                "API; error_type=%s",
                type(error).__name__,
            )
            await send_interaction_notice(
                interaction,
                "The Guild Wars 2 API could not be read. Nothing was "
                "imported; try again in a moment.",
            )
            return
        except SQLAlchemyError as error:
            LOGGER.error(
                "The gold history import could not be stored; error_type=%s",
                type(error).__name__,
            )
            await send_interaction_notice(
                interaction,
                "The gold history could not be written to the database. "
                "Check the console log.",
            )
            return
        await send_interaction_notice(interaction, format_import_result(result))
