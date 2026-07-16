from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands

from gw2bot.discord_utils import user_has_role
from gw2bot.polls.models import Poll
from gw2bot.polls.roles import POLL_MANAGE_ROLE_ID
from gw2bot.polls.views import (
    PollCompleteConfirmView,
    PollDeleteConfirmView,
    PollDetailsModal,
    PollDraft,
    draft_from_poll,
    ensure_manage_role,
    send_poll_preview,
)

if TYPE_CHECKING:
    from gw2bot.bot import Gw2Bot

LOGGER = logging.getLogger(__name__)

POLL_AUTOCOMPLETE_LIMIT = 25
POLL_CHOICE_NAME_LIMIT = 100


def _poll_choice_name(poll: Poll) -> str:
    # Keep the id visible even when a long title has to be truncated, since it
    # is the value the user is really picking.
    suffix = f" — id {poll.poll_id}"
    budget = POLL_CHOICE_NAME_LIMIT - len(suffix)
    title = poll.title
    if budget < 1:
        return suffix[:POLL_CHOICE_NAME_LIMIT]
    if len(title) > budget:
        title = title[: budget - 1].rstrip() + "…"
    return f"{title}{suffix}"


class PollCommands(app_commands.Group):
    def __init__(self, bot: Gw2Bot):
        super().__init__(
            name="poll",
            description="Create and manage reaction polls",
            guild_only=True,
        )
        self._bot = bot

    @app_commands.command(
        name="create",
        description="Create a new reaction poll",
    )
    async def create(self, interaction: discord.Interaction) -> None:
        LOGGER.debug(
            "Poll creation command invoked by Discord user %s",
            interaction.user.id,
        )
        if not await ensure_manage_role(interaction, "create"):
            return
        draft = PollDraft(creator_discord_id=interaction.user.id)
        await interaction.response.send_modal(
            PollDetailsModal(self._bot, draft)
        )
        LOGGER.debug(
            "Poll creation modal opened; user_id=%s",
            interaction.user.id,
        )

    async def active_poll_id_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[int]]:
        # Shared by edit/delete/complete. Autocomplete must never error out;
        # unauthorized users simply see no suggestions (each command still
        # enforces the role).
        if not user_has_role(interaction.user, POLL_MANAGE_ROLE_ID):
            return []
        query = current.strip().casefold()
        choices: list[app_commands.Choice[int]] = []
        for poll in self._bot.poll_store.get_active_polls():
            if poll.message_id is None:
                continue
            if query and (
                query not in poll.title.casefold()
                and query not in str(poll.poll_id)
            ):
                continue
            choices.append(
                app_commands.Choice(
                    name=_poll_choice_name(poll),
                    value=poll.poll_id,
                )
            )
            if len(choices) >= POLL_AUTOCOMPLETE_LIMIT:
                break
        LOGGER.debug(
            "Returning active poll autocomplete choices; choices=%s",
            len(choices),
        )
        return choices

    @app_commands.command(name="edit", description="Edit a running poll")
    @app_commands.describe(poll_id="The poll to edit (shown as pollID in its footer)")
    @app_commands.autocomplete(poll_id=active_poll_id_autocomplete)
    async def edit(
        self,
        interaction: discord.Interaction,
        poll_id: int,
    ) -> None:
        LOGGER.debug(
            "Poll edit command invoked by Discord user %s; poll_id=%s",
            interaction.user.id,
            poll_id,
        )
        if not await ensure_manage_role(interaction, "edit"):
            return
        poll = self._bot.poll_store.get_poll(poll_id)
        if poll is None or poll.message_id is None:
            await interaction.response.send_message(
                "That poll does not exist.",
                ephemeral=True,
            )
            return
        draft = draft_from_poll(poll)
        await send_poll_preview(self._bot, interaction, draft)
        LOGGER.debug(
            "Poll edit preview opened; user_id=%s poll_id=%s",
            interaction.user.id,
            poll_id,
        )

    @app_commands.command(name="delete", description="Delete a running poll")
    @app_commands.describe(poll_id="The poll to delete (shown as pollID in its footer)")
    @app_commands.autocomplete(poll_id=active_poll_id_autocomplete)
    async def delete(
        self,
        interaction: discord.Interaction,
        poll_id: int,
    ) -> None:
        LOGGER.debug(
            "Poll delete command invoked by Discord user %s; poll_id=%s",
            interaction.user.id,
            poll_id,
        )
        if not await ensure_manage_role(interaction, "delete"):
            return
        poll = self._bot.poll_store.get_poll(poll_id)
        if poll is None:
            await interaction.response.send_message(
                "That poll does not exist.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            f"Delete poll **{poll.poll_id}** (**{poll.title}**)? This removes "
            "its message and every vote, and cannot be undone.",
            view=PollDeleteConfirmView(self._bot, poll),
            ephemeral=True,
        )
        LOGGER.debug(
            "Poll delete confirmation opened; user_id=%s poll_id=%s",
            interaction.user.id,
            poll_id,
        )

    @app_commands.command(
        name="complete",
        description="End a running poll immediately",
    )
    @app_commands.describe(poll_id="The poll to end (shown as pollID in its footer)")
    @app_commands.autocomplete(poll_id=active_poll_id_autocomplete)
    async def complete(
        self,
        interaction: discord.Interaction,
        poll_id: int,
    ) -> None:
        LOGGER.debug(
            "Poll complete command invoked by Discord user %s; poll_id=%s",
            interaction.user.id,
            poll_id,
        )
        if not await ensure_manage_role(interaction, "complete"):
            return
        poll = self._bot.poll_store.get_poll(poll_id)
        if poll is None:
            await interaction.response.send_message(
                "That poll does not exist.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            f"End poll **{poll.poll_id}** (**{poll.title}**) now? Its final "
            "results will be shown and voting will close.",
            view=PollCompleteConfirmView(self._bot, poll),
            ephemeral=True,
        )
        LOGGER.debug(
            "Poll complete confirmation opened; user_id=%s poll_id=%s",
            interaction.user.id,
            poll_id,
        )
