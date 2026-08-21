from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import aiohttp
import discord
from discord import app_commands
from sqlalchemy.exc import SQLAlchemyError

from gw2bot.discord_utils import send_interaction_notice
from gw2bot.profit.api import ProfitApiError

if TYPE_CHECKING:
    from gw2bot.bot import Gw2Bot

LOGGER = logging.getLogger(__name__)


class ProfitApiKeyModal(discord.ui.Modal, title="Save GW2 Profit API Key"):
    api_key = discord.ui.TextInput(
        label="GW2 API Key",
        placeholder="Paste a key with the tradingpost permission",
        style=discord.TextStyle.short,
        required=True,
        max_length=200,
    )

    def __init__(self, bot: Gw2Bot) -> None:
        super().__init__()
        self._bot = bot

    async def on_submit(self, interaction: discord.Interaction) -> None:
        api_key = str(self.api_key.value).strip()
        # The candidate is about to be put in an Authorization header. Arm the
        # process-wide redactor before the validation request or any log line.
        self._bot.secrets.add(api_key)
        await interaction.response.defer(ephemeral=True, thinking=True)
        service = self._bot.profit_service
        if service is None:
            LOGGER.error(
                "Could not validate profit API key; service=unavailable "
                "user_id=%s",
                interaction.user.id,
            )
            await send_interaction_notice(
                interaction,
                "The profit service is not ready. Try again in a moment.",
            )
            return
        try:
            valid = await service.validate_api_key(api_key)
        except (aiohttp.ClientError, TimeoutError, ProfitApiError) as exc:
            LOGGER.warning(
                "Could not validate profit API key; user_id=%s error_type=%s",
                interaction.user.id,
                type(exc).__name__,
            )
            await send_interaction_notice(
                interaction,
                "The Guild Wars 2 API could not validate that key. Check the "
                "key and try again in a moment.",
            )
            return
        if not valid:
            LOGGER.debug(
                "Rejected profit API key; user_id=%s "
                "reason=scope-or-route-access",
                interaction.user.id,
            )
            await send_interaction_notice(
                interaction,
                "That key must grant the `tradingpost` permission and access "
                "to all Trading Post transaction routes. Check any subtoken "
                "URL restrictions.",
            )
            return
        try:
            await asyncio.to_thread(
                self._bot.profit_store.set_api_key,
                interaction.user.id,
                api_key,
            )
        except SQLAlchemyError as exc:
            LOGGER.error(
                "Could not store profit API key; user_id=%s error_type=%s",
                interaction.user.id,
                type(exc).__name__,
            )
            await send_interaction_notice(
                interaction,
                "The API key was valid, but it could not be saved. Check the "
                "console log and try again.",
            )
            return
        LOGGER.info("Stored profit API key; user_id=%s", interaction.user.id)
        await send_interaction_notice(
            interaction,
            "Saved your GW2 API key with the `tradingpost` permission.",
        )

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
    ) -> None:
        LOGGER.error(
            "Profit API key modal failed; user_id=%s error_type=%s",
            getattr(getattr(interaction, "user", None), "id", "unknown"),
            type(error).__name__,
        )
        await send_interaction_notice(
            interaction,
            "The API key could not be saved. Check the console log and try "
            "again.",
        )


class ProfitCommands(app_commands.Group):
    """Member commands for the personal Trading Post profit dashboard."""

    def __init__(self, bot: Gw2Bot) -> None:
        super().__init__(
            name="profit",
            description="Your Trading Post profit dashboard",
            guild_only=True,
        )
        self._bot = bot

    @app_commands.command(
        name="setkey",
        description="Save or replace your encrypted GW2 Trading Post API key",
    )
    async def set_key(self, interaction: discord.Interaction) -> None:
        LOGGER.debug(
            "Profit set-key command invoked; user_id=%s",
            interaction.user.id,
        )
        await interaction.response.send_modal(ProfitApiKeyModal(self._bot))

    @app_commands.command(
        name="deletekey",
        description="Delete your stored GW2 Trading Post API key",
    )
    async def delete_key(self, interaction: discord.Interaction) -> None:
        LOGGER.debug(
            "Profit delete-key command invoked; user_id=%s",
            interaction.user.id,
        )
        await interaction.response.defer(ephemeral=True)
        try:
            removed = await asyncio.to_thread(
                self._bot.profit_store.delete_api_key,
                interaction.user.id,
            )
        except SQLAlchemyError as exc:
            LOGGER.error(
                "Could not delete profit API key; user_id=%s error_type=%s",
                interaction.user.id,
                type(exc).__name__,
            )
            await send_interaction_notice(
                interaction,
                "Your API key could not be deleted. Check the console log.",
            )
            return
        LOGGER.info(
            "Profit API key deletion completed; user_id=%s removed=%s",
            interaction.user.id,
            removed,
        )
        await send_interaction_notice(
            interaction,
            (
                "Deleted your stored GW2 API key."
                if removed
                else "You did not have a stored GW2 API key."
            ),
        )

    @app_commands.command(
        name="view",
        description="Open your Trading Post profit dashboard",
    )
    @app_commands.describe(days="Number of days to report, from 1 through 90")
    async def view(
        self,
        interaction: discord.Interaction,
        days: app_commands.Range[int, 1, 90] = 30,
    ) -> None:
        LOGGER.debug(
            "Profit dashboard command invoked; user_id=%s days=%s",
            interaction.user.id,
            days,
        )
        if (
            not self._bot._config.web_calendar_enabled
            or self._bot._config.web_base_url is None
        ):
            LOGGER.debug(
                "Skipped profit dashboard link; user_id=%s reason=web-disabled",
                interaction.user.id,
            )
            await send_interaction_notice(
                interaction,
                "The profit dashboard is disabled. Enable `WEB_ENABLED` and "
                "configure the four web `/settings` values first.",
            )
            return
        url = (
            f"{self._bot._config.web_base_url.rstrip('/')}/profit?days={days}"
        )
        await send_interaction_notice(
            interaction,
            f"[Open your {days}-day profit dashboard]({url})",
        )
        LOGGER.debug(
            "Delivered profit dashboard link; user_id=%s days=%s",
            interaction.user.id,
            days,
        )
