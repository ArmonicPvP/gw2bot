"""`/help`: every command the caller may run, and what it does.

Nothing here lists the bot's commands by hand. The registered command tree is
walked on every call, and each command's declared access (see
`gw2bot.core.command_access`) is judged against the caller's roles and the
role settings as they stand right now, so a new command, or a role moved with
`/settings`, is reflected without touching this module.

The reply is a single private embed; when the list is too long for one, it
pages with arrow buttons rather than sending more messages.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import discord
from discord import app_commands

from gw2bot.core.command_access import Everyone, access_extras
from gw2bot.core.discord_utils import log_discord_failure
from gw2bot.help.pages import build_help_messages, registered_commands
from gw2bot.help.views import help_embed, help_pager_view

if TYPE_CHECKING:
    from gw2bot.bot import Gw2Bot

LOGGER = logging.getLogger(__name__)


def create_help_command(bot: Gw2Bot) -> app_commands.Command[Any, ..., None]:
    @app_commands.command(
        name="help",
        description="Show the commands you can use and what they do",
        extras=access_extras(Everyone()),
    )
    @app_commands.guild_only()
    async def help_command(interaction: discord.Interaction) -> None:
        await handle_help_command(bot, interaction)

    return help_command


async def handle_help_command(
    bot: Gw2Bot,
    interaction: discord.Interaction,
) -> None:
    user_id = getattr(getattr(interaction, "user", None), "id", "unknown")
    LOGGER.debug("Help command invoked by Discord user %s", user_id)
    pages = build_help_messages(
        registered_commands(bot.tree, interaction.guild),
        interaction.user,
        interaction.guild,
        bot._config,
    )
    view = help_pager_view(len(pages), 0)
    try:
        if view is None:
            await interaction.response.send_message(
                embed=help_embed(pages, 0),
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                embed=help_embed(pages, 0),
                view=view,
                ephemeral=True,
            )
    except discord.DiscordException as error:
        log_discord_failure("Could not deliver the help reply", error)
        return
    LOGGER.debug(
        "Help command completed for Discord user %s; pages=%s",
        user_id,
        len(pages),
    )
