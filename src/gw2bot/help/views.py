"""The page arrows on a `/help` reply.

The pager is persistent the way the raffle pagers are: the page rides in each
button's custom_id and the pages are rebuilt on click, so the arrows keep
working for as long as the message is on screen and across bot restarts.
Rebuilding for whoever clicked is safe because the reply is ephemeral - only
the member who ran `/help` can see it, let alone press its buttons - and it
means a role granted in the meantime shows up on the next page turn.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, cast

import discord

from gw2bot.core.discord_utils import log_discord_failure
from gw2bot.help.pages import build_help_messages, registered_commands

if TYPE_CHECKING:
    from gw2bot.bot import Gw2Bot

LOGGER = logging.getLogger(__name__)

HELP_TITLE = "Commands you can use"


def help_embed(pages: Sequence[str], page: int) -> discord.Embed:
    """The embed showing ``pages[page]``, numbered when there is more than one."""
    embed = discord.Embed(title=HELP_TITLE, description=pages[page])
    if len(pages) > 1:
        embed.set_footer(text=f"Page {page + 1} of {len(pages)}")
    return embed


def help_pager_view(page_count: int, page: int) -> HelpPagerView | None:
    """The arrows for a reply of ``page_count`` pages, or none for one page."""
    if page_count <= 1:
        return None
    return HelpPagerView(page_count, page)


class HelpPageButton(
    discord.ui.DynamicItem[discord.ui.Button[Any]],
    template=r"gw2bot:help:(?P<page>[0-9]+):(?P<direction>-?1)",
):
    def __init__(self, page: int, direction: int, *, disabled: bool = False):
        self.page = page
        self.direction = direction
        super().__init__(
            discord.ui.Button(
                label="<" if direction < 0 else ">",
                style=discord.ButtonStyle.secondary,
                custom_id=f"gw2bot:help:{page}:{direction}",
                disabled=disabled,
            )
        )

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Item[Any],
        match: re.Match[str],
        /,
    ) -> HelpPageButton:
        return cls(int(match["page"]), int(match["direction"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        bot = cast("Gw2Bot", interaction.client)
        pages = build_help_messages(
            registered_commands(bot.tree, interaction.guild),
            interaction.user,
            interaction.guild,
            bot._config,
        )
        # The page count can shrink between clicks if a role was taken away,
        # so the target is clamped to what exists now.
        page = max(0, min(self.page + self.direction, len(pages) - 1))
        LOGGER.debug(
            "Changing help page; user_id=%s direction=%s page=%s page_count=%s",
            interaction.user.id,
            self.direction,
            page + 1,
            len(pages),
        )
        try:
            await interaction.response.edit_message(
                embed=help_embed(pages, page),
                view=help_pager_view(len(pages), page),
            )
        except discord.DiscordException as error:
            log_discord_failure("Could not change the help page", error)


class HelpPagerView(discord.ui.View):
    def __init__(self, page_count: int, page: int = 0):
        # timeout=None marks the view persistent; each button carries a
        # custom_id the bot's dynamic items dispatch after a restart.
        super().__init__(timeout=None)
        page = max(0, min(page, page_count - 1))
        self.add_item(HelpPageButton(page, -1, disabled=page == 0))
        self.add_item(
            HelpPageButton(page, 1, disabled=page >= page_count - 1)
        )
