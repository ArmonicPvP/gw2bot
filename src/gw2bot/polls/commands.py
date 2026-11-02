"""The /poll command tree: post an anonymous poll, and end one."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.utils import escape_markdown
from sqlalchemy.exc import SQLAlchemyError

from gw2bot.core.command_access import RoleSetting, access_extras
from gw2bot.core.discord_utils import (
    log_discord_failure,
    send_interaction_notice,
    user_has_role,
)
from gw2bot.polls.formatting import (
    closed_poll_embed,
    open_poll_embed,
    poll_message_link,
)
from gw2bot.polls.models import (
    MAX_POLL_OPTIONS,
    MIN_POLL_OPTIONS,
    POLL_DESCRIPTION_MAX_LENGTH,
    POLL_NAME_MAX_LENGTH,
    POLL_OPTIONS_INPUT_MAX_LENGTH,
    Poll,
    PollOptionsError,
    PollResults,
    parse_poll_options,
)
from gw2bot.polls.views import NO_MENTIONS, poll_message, poll_vote_view

if TYPE_CHECKING:
    from gw2bot.bot import Gw2Bot

LOGGER = logging.getLogger(__name__)

POLL_ROLE_SETTING = "raffle_officer_role_id"
# Discord's limits on an autocomplete answer.
POLL_CHOICE_NAME_LIMIT = 100
POLL_AUTOCOMPLETE_LIMIT = 25
# How long the "Try again" button under a refused form keeps working.
RETRY_TIMEOUT_SECONDS = 600

POST_FAILED_NOTICE = (
    "The poll could not be posted in this channel, so nothing was created. "
    "Check that the bot can View Channel, Send Messages and Embed Links here, "
    "then try again."
)


def _is_poll_officer(bot: Gw2Bot, user: object) -> bool:
    return user_has_role(user, getattr(bot._config, POLL_ROLE_SETTING))


async def authorize(bot: Gw2Bot, interaction: discord.Interaction) -> bool:
    if _is_poll_officer(bot, interaction.user):
        LOGGER.debug("Authorized poll command; user_id=%s", interaction.user.id)
        return True
    LOGGER.warning(
        "Rejected poll command from Discord user %s; required role %s",
        interaction.user.id,
        getattr(bot._config, POLL_ROLE_SETTING),
    )
    await send_interaction_notice(
        interaction,
        "You do not have the required role for this command.",
    )
    return False


@dataclass(frozen=True, slots=True)
class PollDraft:
    """What the officer typed, kept so a refused form can be reopened filled in."""

    name: str = ""
    options: str = ""
    description: str = ""


class PollCreateModal(discord.ui.Modal, title="Create a poll"):
    def __init__(self, bot: Gw2Bot, draft: PollDraft | None = None):
        super().__init__()
        self._bot = bot
        draft = draft or PollDraft()
        self.name_input = discord.ui.TextInput["PollCreateModal"](
            default=draft.name or None,
            max_length=POLL_NAME_MAX_LENGTH,
            placeholder="Next raid night",
        )
        self.add_item(
            discord.ui.Label(text="Poll name", component=self.name_input)
        )
        self.options_input = discord.ui.TextInput["PollCreateModal"](
            style=discord.TextStyle.paragraph,
            default=draft.options or None,
            max_length=POLL_OPTIONS_INPUT_MAX_LENGTH,
            placeholder="Monday, Wednesday, Friday",
        )
        self.add_item(
            discord.ui.Label(
                text="Options, separated by commas",
                description=(
                    f"Between {MIN_POLL_OPTIONS} and {MAX_POLL_OPTIONS} "
                    "options."
                ),
                component=self.options_input,
            )
        )
        self.description_input = discord.ui.TextInput["PollCreateModal"](
            style=discord.TextStyle.paragraph,
            default=draft.description or None,
            max_length=POLL_DESCRIPTION_MAX_LENGTH,
            required=False,
        )
        self.add_item(
            discord.ui.Label(
                text="Description (optional)",
                description="Shown above the options.",
                component=self.description_input,
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        draft = PollDraft(
            name=self.name_input.value,
            options=self.options_input.value,
            description=self.description_input.value,
        )
        LOGGER.debug(
            "Poll creation form submitted; user_id=%s name_characters=%s "
            "options_characters=%s description_characters=%s",
            interaction.user.id,
            len(draft.name),
            len(draft.options),
            len(draft.description),
        )
        # The form can sit open for a while, so the role is checked again
        # before anything is posted.
        if not await authorize(self._bot, interaction):
            return
        name = " ".join(draft.name.split())
        try:
            if not name:
                raise PollOptionsError("The poll needs a name.")
            options = parse_poll_options(draft.options)
        except PollOptionsError as error:
            LOGGER.debug(
                "Refused poll creation form; user_id=%s",
                interaction.user.id,
            )
            await _send_retry(self._bot, interaction, str(error), draft)
            return
        await post_poll(
            self._bot,
            interaction,
            name=name,
            description=draft.description.strip(),
            options=options,
        )


class PollRetryView(discord.ui.View):
    """Reopens the create form with what was typed, after it was refused."""

    def __init__(self, bot: Gw2Bot, draft: PollDraft):
        super().__init__(timeout=RETRY_TIMEOUT_SECONDS)
        self._bot = bot
        self._draft = draft

    @discord.ui.button(label="Try again", style=discord.ButtonStyle.primary)
    async def retry(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[PollRetryView],
    ) -> None:
        LOGGER.debug("Reopening poll creation form; user_id=%s", interaction.user.id)
        try:
            await interaction.response.send_modal(
                PollCreateModal(self._bot, self._draft)
            )
        except discord.HTTPException as error:
            log_discord_failure("Could not reopen the poll creation form", error)


async def _send_retry(
    bot: Gw2Bot,
    interaction: discord.Interaction,
    reason: str,
    draft: PollDraft,
) -> None:
    try:
        await interaction.response.send_message(
            f"{reason}\nNothing was posted.",
            view=PollRetryView(bot, draft),
            ephemeral=True,
            allowed_mentions=NO_MENTIONS,
        )
    except discord.HTTPException as error:
        log_discord_failure("Could not explain a refused poll form", error)


async def post_poll(
    bot: Gw2Bot,
    interaction: discord.Interaction,
    *,
    name: str,
    description: str,
    options: tuple[str, ...],
) -> None:
    """Store the poll and post it, with its dropdown, in the caller's channel."""
    channel = interaction.channel
    channel_id = interaction.channel_id
    if not isinstance(channel, discord.abc.Messageable) or channel_id is None:
        LOGGER.debug(
            "Refused to post a poll outside a text channel; user_id=%s",
            interaction.user.id,
        )
        await send_interaction_notice(
            interaction,
            "Polls can only be posted in a text channel or thread.",
        )
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        poll = bot.poll_store.create_poll(
            name=name,
            description=description,
            options=options,
            channel_id=channel_id,
            created_by=interaction.user.id,
        )
    except SQLAlchemyError as error:
        LOGGER.error(
            "Could not store a new poll; error_type=%s",
            type(error).__name__,
        )
        await send_interaction_notice(
            interaction,
            "The poll could not be saved to the database, so nothing was "
            "posted. Check the console log.",
        )
        return
    try:
        message = await channel.send(
            embed=open_poll_embed(poll, 0),
            view=poll_vote_view(poll),
            allowed_mentions=NO_MENTIONS,
        )
    except discord.HTTPException as error:
        log_discord_failure(
            "Could not post a poll; poll_id=%s channel_id=%s",
            error,
            poll.poll_id,
            channel_id,
        )
        _discard_poll(bot, poll.poll_id)
        await send_interaction_notice(interaction, POST_FAILED_NOTICE)
        return
    try:
        bot.poll_store.set_message(poll.poll_id, message.id)
    except SQLAlchemyError as error:
        # Without the message id /poll end could not close the post, so a
        # post that cannot be tracked is taken down again.
        LOGGER.error(
            "Could not record a poll's message; poll_id=%s error_type=%s",
            poll.poll_id,
            type(error).__name__,
        )
        try:
            await message.delete()
        except discord.HTTPException as delete_error:
            log_discord_failure(
                "Could not remove an untracked poll post; poll_id=%s",
                delete_error,
                poll.poll_id,
            )
        _discard_poll(bot, poll.poll_id)
        await send_interaction_notice(
            interaction,
            "The poll could not be saved to the database, so it was not "
            "posted. Check the console log.",
        )
        return
    LOGGER.debug(
        "Posted poll; poll_id=%s channel_id=%s options=%s",
        poll.poll_id,
        channel_id,
        len(options),
    )
    link = message.jump_url
    await send_interaction_notice(
        interaction,
        f"Posted **{escape_markdown(name)}** with {len(options)} options: "
        f"{link}\nEnd it with `/poll end` to show the results.",
    )


def _discard_poll(bot: Gw2Bot, poll_id: int) -> None:
    try:
        bot.poll_store.delete_poll(poll_id)
    except SQLAlchemyError as error:
        LOGGER.error(
            "Could not remove a poll that was never posted; poll_id=%s "
            "error_type=%s",
            poll_id,
            type(error).__name__,
        )


def _poll_choice_name(poll: Poll) -> str:
    # The id stays visible even when a long name is cut, since it is the
    # value the officer is really picking.
    suffix = f" — id {poll.poll_id}"
    budget = POLL_CHOICE_NAME_LIMIT - len(suffix)
    name = poll.name
    if len(name) > budget:
        name = name[: budget - 1].rstrip() + "…"
    return f"{name}{suffix}"


def _results_summary(results: PollResults) -> str:
    leaders = results.leaders
    options = results.poll.options
    if not leaders:
        return "No votes were cast."
    top = results.counts[leaders[0]]
    votes = f"{top} vote" + ("" if top == 1 else "s")
    if len(leaders) == 1:
        return f"🏆 Winner: **{escape_markdown(options[leaders[0]])}** with {votes}."
    tied = ", ".join(f"**{escape_markdown(options[position])}**" for position in leaders)
    return f"🤝 Tie between {tied} with {votes} each."


async def _close_post(bot: Gw2Bot, results: PollResults) -> bool:
    """Swap the post's dropdown for the results; False if it could not be."""
    message = poll_message(bot, results.poll)
    if message is None:
        return False
    try:
        await message.edit(embed=closed_poll_embed(results), view=None)
    except discord.HTTPException as error:
        log_discord_failure(
            "Could not show a closed poll's results on its post; poll_id=%s",
            error,
            results.poll.poll_id,
        )
        return False
    LOGGER.debug("Closed poll post; poll_id=%s", results.poll.poll_id)
    return True


async def _announce_results(bot: Gw2Bot, results: PollResults) -> bool:
    """Reply to the post, so the channel hears the poll ended."""
    message = poll_message(bot, results.poll)
    if message is None:
        return False
    content = (
        f"📊 **{escape_markdown(results.poll.name)}** has ended. "
        f"{_results_summary(results)} The full results are on the poll."
    )
    try:
        await message.reply(content, allowed_mentions=NO_MENTIONS)
    except discord.HTTPException as error:
        log_discord_failure(
            "Could not announce a poll's results; poll_id=%s",
            error,
            results.poll.poll_id,
        )
        return False
    LOGGER.debug("Announced poll results; poll_id=%s", results.poll.poll_id)
    return True


class PollCommands(app_commands.Group):
    def __init__(self, bot: Gw2Bot):
        super().__init__(
            name="poll",
            description="Anonymous polls",
            guild_only=True,
            # What authorize() checks, for /help.
            extras=access_extras(RoleSetting(POLL_ROLE_SETTING)),
        )
        self._bot = bot

    @app_commands.command(
        name="create",
        description="Post an anonymous poll in this channel",
    )
    async def create(self, interaction: discord.Interaction) -> None:
        LOGGER.debug(
            "Poll creation command invoked by Discord user %s",
            interaction.user.id,
        )
        if not await authorize(self._bot, interaction):
            return
        try:
            await interaction.response.send_modal(PollCreateModal(self._bot))
        except discord.HTTPException as error:
            log_discord_failure("Could not open the poll creation form", error)
            return
        LOGGER.debug("Poll creation form opened; user_id=%s", interaction.user.id)

    async def open_poll_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[int]]:
        # Autocomplete must never error out; a member without the role is
        # offered nothing, and the command still enforces it.
        if not _is_poll_officer(self._bot, interaction.user):
            return []
        try:
            polls = self._bot.poll_store.open_polls()
        except SQLAlchemyError as error:
            LOGGER.error(
                "Could not list open polls for autocomplete; error_type=%s",
                type(error).__name__,
            )
            return []
        query = current.strip().casefold()
        choices: list[app_commands.Choice[int]] = []
        for poll in polls:
            if query and (
                query not in poll.name.casefold() and query not in str(poll.poll_id)
            ):
                continue
            choices.append(
                app_commands.Choice(name=_poll_choice_name(poll), value=poll.poll_id)
            )
            if len(choices) >= POLL_AUTOCOMPLETE_LIMIT:
                break
        LOGGER.debug("Returning open poll autocomplete choices; choices=%s", len(choices))
        return choices

    @app_commands.command(
        name="end",
        description="End a poll and post its results",
    )
    @app_commands.describe(poll="The poll to end (its id is in the post's footer)")
    @app_commands.autocomplete(poll=open_poll_autocomplete)
    async def end(self, interaction: discord.Interaction, poll: int) -> None:
        LOGGER.debug(
            "Poll end command invoked by Discord user %s; poll_id=%s",
            interaction.user.id,
            poll,
        )
        if not await authorize(self._bot, interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            existing = self._bot.poll_store.get_poll(poll)
            results = (
                self._bot.poll_store.close_poll(poll, interaction.user.id)
                if existing is not None and existing.is_open
                else None
            )
        except SQLAlchemyError as error:
            LOGGER.error(
                "Could not close a poll; poll_id=%s error_type=%s",
                poll,
                type(error).__name__,
            )
            await send_interaction_notice(
                interaction,
                "The poll could not be ended in the database. Check the "
                "console log.",
            )
            return
        if existing is None:
            await send_interaction_notice(
                interaction,
                f"There is no poll with id {poll}.",
            )
            return
        if results is None:
            await send_interaction_notice(
                interaction,
                f"**{escape_markdown(existing.name)}** has already ended.",
            )
            return
        posted = await _close_post(self._bot, results)
        if posted:
            await _announce_results(self._bot, results)
        content = f"Ended **{escape_markdown(existing.name)}**."
        link = poll_message_link(
            getattr(interaction.guild, "id", None),
            results.poll,
        )
        if posted and link is not None:
            content += f" The results are on the poll: {link}"
        else:
            content += (
                " Its post could not be updated - it may have been deleted - "
                "so the results are only shown here."
            )
        try:
            await interaction.followup.send(
                content,
                embed=closed_poll_embed(results),
                ephemeral=True,
                allowed_mentions=NO_MENTIONS,
            )
        except discord.HTTPException as error:
            log_discord_failure(
                "Could not send a closed poll's results to the officer; "
                "poll_id=%s",
                error,
                poll,
            )
