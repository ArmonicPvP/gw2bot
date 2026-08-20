from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import discord
from discord import app_commands
from sqlalchemy.exc import SQLAlchemyError

from gw2bot.discord_utils import (
    log_discord_failure,
    send_interaction_notice,
    user_has_role,
)
from gw2bot.roster.import_log import (
    format_import_result,
    import_membership_history,
)

if TYPE_CHECKING:
    from gw2bot.bot import Gw2Bot

LOGGER = logging.getLogger(__name__)


class RosterCommands(app_commands.Group):
    """The /roster command tree.

    One subcommand so far: the one-time import that recovers the membership
    history from the log channel the bot has been announcing it in.
    """

    def __init__(self, bot: Gw2Bot):
        super().__init__(
            name="roster",
            description="Guild roster history",
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
                "Read the log channel's history into the roster graph "
                "(one-time)"
            ),
            callback=callback,  # type: ignore[arg-type]
        )

    async def authorize(self, interaction: discord.Interaction) -> bool:
        officer_role_id = self._bot._config.raffle_officer_role_id
        if user_has_role(interaction.user, officer_role_id):
            LOGGER.debug(
                "Authorized roster command; user_id=%s",
                interaction.user.id,
            )
            return True
        LOGGER.warning(
            "Rejected roster command from Discord user %s; required role %s",
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
            "Roster history import invoked by Discord user %s",
            getattr(getattr(interaction, "user", None), "id", "unknown"),
        )
        if not await self.authorize(interaction):
            return
        if not self._bot._config.notifications_enabled:
            await send_interaction_notice(
                interaction,
                "This command reads the history of the notification channel. "
                "Run /settings discord_notification_channel_id first.",
            )
            return
        # Reading years of channel history takes far longer than the three
        # seconds Discord allows a command to answer in.
        await interaction.response.defer(ephemeral=True)
        try:
            channel = await self._bot._get_notification_channel()
        except discord.DiscordException as error:
            log_discord_failure(
                "Could not open the log channel for the roster import; "
                "channel_id=%s",
                error,
                self._bot._config.discord_notification_channel_id,
            )
            await send_interaction_notice(
                interaction,
                "The log channel could not be opened. Check that the bot can "
                "see it, then try again.",
            )
            return
        try:
            result = await import_membership_history(self._bot, channel)
        except SQLAlchemyError as error:
            LOGGER.error(
                "The roster history import could not be stored; error_type=%s",
                type(error).__name__,
            )
            await send_interaction_notice(
                interaction,
                "The roster history could not be written to the database. "
                "Nothing was imported; check the console log.",
            )
            return
        await send_interaction_notice(interaction, format_import_result(result))
