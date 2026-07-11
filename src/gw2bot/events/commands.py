from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands

from gw2bot.discord_utils import user_has_role
from gw2bot.events.roles import EVENT_CREATE_ROLE_ID
from gw2bot.events.views import EventDetailsModal, EventDraft

if TYPE_CHECKING:
    from gw2bot.bot import Gw2Bot

LOGGER = logging.getLogger(__name__)


class EventCommands(app_commands.Group):
    def __init__(self, bot: Gw2Bot):
        super().__init__(
            name="event",
            description="Manage guild events",
            guild_only=True,
        )
        self._bot = bot

    @app_commands.command(name="new", description="Create a new guild event")
    async def new(self, interaction: discord.Interaction) -> None:
        LOGGER.debug(
            "Event creation command invoked by Discord user %s",
            interaction.user.id,
        )
        if not user_has_role(interaction.user, EVENT_CREATE_ROLE_ID):
            LOGGER.warning(
                "Rejected event creation command from Discord user %s; "
                "required role %s",
                interaction.user.id,
                EVENT_CREATE_ROLE_ID,
            )
            await interaction.response.send_message(
                "You do not have the required role to create events.",
                ephemeral=True,
            )
            return
        draft = EventDraft(leader_discord_id=interaction.user.id)
        await interaction.response.send_modal(
            EventDetailsModal(self._bot, draft)
        )
        LOGGER.debug(
            "Event creation modal opened; user_id=%s",
            interaction.user.id,
        )
