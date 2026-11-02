"""The voting dropdown on a poll's post, and the private confirmation behind it.

The dropdown is persistent the way the event signup buttons are: the poll id
rides in its custom_id and everything else is read from the store on each
pick, so it keeps working for as long as the post is up and across restarts.
Changing that custom_id would break every poll already posted - see
docs/versioning.md.

A pick is never counted on its own. It opens a private confirmation, and only
the Confirm button stores the vote. Nothing about a vote is ever posted or
logged against the member who cast it.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, cast

import discord
from discord.utils import escape_markdown
from sqlalchemy.exc import SQLAlchemyError

from gw2bot.core.discord_utils import log_discord_failure, send_interaction_notice
from gw2bot.polls.formatting import open_poll_embed
from gw2bot.polls.models import OPTION_EMOJIS, Poll, VoteOutcome, option_display

if TYPE_CHECKING:
    from gw2bot.bot import Gw2Bot

LOGGER = logging.getLogger(__name__)

VOTE_PLACEHOLDER = "Choose your option…"
# Long enough to read the prompt and decide, short enough that a confirmation
# left on screen does not sit there for the rest of the session.
CONFIRM_TIMEOUT_SECONDS = 180

POLL_ENDED_NOTICE = "This poll has ended, so it is no longer taking votes."

# Every message carrying a poll's name or options is sent with mentions off:
# both are free text an officer typed, and an option reading "@everyone" must
# not ping the server.
NO_MENTIONS = discord.AllowedMentions.none()


def vote_custom_id(poll_id: int) -> str:
    return f"gw2bot:poll:{poll_id}:vote"


class PollVoteSelect(
    discord.ui.DynamicItem[discord.ui.Select[Any]],
    template=r"gw2bot:poll:(?P<poll_id>[0-9]+):vote",
):
    def __init__(self, poll_id: int, options: Sequence[str] = ()):
        self.poll_id = poll_id
        super().__init__(
            discord.ui.Select(
                custom_id=vote_custom_id(poll_id),
                placeholder=VOTE_PLACEHOLDER,
                min_values=1,
                max_values=1,
                options=[
                    discord.SelectOption(
                        label=label,
                        value=str(position),
                        emoji=OPTION_EMOJIS[position],
                    )
                    for position, label in enumerate(options)
                ],
            )
        )

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Item[Any],
        match: re.Match[str],
        /,
    ) -> PollVoteSelect:
        return cls(int(match["poll_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        bot = cast("Gw2Bot", interaction.client)
        await handle_vote_pick(bot, interaction, self.poll_id, self.item.values)


def poll_vote_view(poll: Poll) -> discord.ui.View:
    """The dropdown a poll is posted with."""
    # timeout=None marks the view persistent; the bot's dynamic items
    # dispatch the dropdown after a restart.
    view = discord.ui.View(timeout=None)
    view.add_item(PollVoteSelect(poll.poll_id, poll.options))
    return view


def _bold_option(poll: Poll, position: int) -> str:
    return f"**{escape_markdown(option_display(poll.options, position))}**"


def confirm_prompt(poll: Poll, position: int, current: int | None) -> str:
    lines = [
        f"You picked {_bold_option(poll, position)} in "
        f"**{escape_markdown(poll.name)}**."
    ]
    if current is not None:
        lines.append(
            f"Your current vote is {_bold_option(poll, current)}; confirming "
            "replaces it."
        )
    lines.append("Confirm your vote? Nobody else can see what you chose.")
    return "\n".join(lines)


async def handle_vote_pick(
    bot: Gw2Bot,
    interaction: discord.Interaction,
    poll_id: int,
    values: Sequence[str],
) -> None:
    user_id = interaction.user.id
    LOGGER.debug("Poll option picked; poll_id=%s user_id=%s", poll_id, user_id)
    try:
        poll = bot.poll_store.get_poll(poll_id)
        current = bot.poll_store.current_vote(poll_id, user_id)
    except SQLAlchemyError as error:
        LOGGER.error(
            "Could not read a poll for a vote; poll_id=%s error_type=%s",
            poll_id,
            type(error).__name__,
        )
        await send_interaction_notice(
            interaction,
            "Your pick could not be read from the database. Try again in a "
            "moment.",
        )
        return
    if poll is None or not poll.accepts_votes(datetime.now(UTC)):
        LOGGER.debug(
            "Refused a pick on a poll that is not open; poll_id=%s found=%s",
            poll_id,
            poll is not None,
        )
        await send_interaction_notice(interaction, POLL_ENDED_NOTICE)
        return
    position = _picked_position(values, len(poll.options))
    if position is None:
        LOGGER.warning(
            "Refused a pick naming no option of the poll; poll_id=%s values=%s",
            poll_id,
            len(values),
        )
        await send_interaction_notice(
            interaction,
            "That option is not on this poll. Pick again from the menu.",
        )
        return
    # Answering with the same dropdown clears the pick on the member's
    # screen, so cancelling and then choosing the same option again still
    # sends a new pick instead of nothing.
    try:
        await interaction.response.edit_message(view=poll_vote_view(poll))
    except discord.HTTPException as error:
        log_discord_failure(
            "Could not acknowledge a poll pick; poll_id=%s",
            error,
            poll_id,
        )
        return
    if current == position:
        LOGGER.debug(
            "Pick matches the member's current vote; poll_id=%s user_id=%s",
            poll_id,
            user_id,
        )
        content = (
            f"You have already voted for {_bold_option(poll, position)} in "
            f"**{escape_markdown(poll.name)}**. Your vote is unchanged."
        )
        view: VoteConfirmView | None = None
    else:
        content = confirm_prompt(poll, position, current)
        view = VoteConfirmView(bot, poll, position, user_id, current)
    try:
        if view is None:
            await interaction.followup.send(
                content,
                ephemeral=True,
                allowed_mentions=NO_MENTIONS,
            )
        else:
            view.message = await interaction.followup.send(
                content,
                view=view,
                ephemeral=True,
                allowed_mentions=NO_MENTIONS,
                wait=True,
            )
    except discord.HTTPException as error:
        log_discord_failure(
            "Could not send a poll vote confirmation; poll_id=%s",
            error,
            poll_id,
        )
        return
    LOGGER.debug(
        "Sent poll vote confirmation; poll_id=%s user_id=%s needs_confirm=%s "
        "replacing=%s",
        poll_id,
        user_id,
        view is not None,
        current is not None,
    )


def _picked_position(values: Sequence[str], option_count: int) -> int | None:
    if len(values) != 1:
        return None
    try:
        position = int(values[0])
    except ValueError:
        return None
    return position if 0 <= position < option_count else None


class VoteConfirmView(discord.ui.View):
    """Confirm or cancel one pick, shown only to the member who made it."""

    def __init__(
        self,
        bot: Gw2Bot,
        poll: Poll,
        position: int,
        voter_id: int,
        current: int | None = None,
    ):
        super().__init__(timeout=CONFIRM_TIMEOUT_SECONDS)
        self._bot = bot
        self._poll = poll
        self._position = position
        self._voter_id = voter_id
        self._current = current
        # Set once either button is pressed, so a double click cannot record
        # twice and the expiry notice cannot overwrite the answer.
        self._answered = False
        self.message: discord.WebhookMessage | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # The prompt is private, so this only matters if Discord ever routes
        # someone else's click here.
        return interaction.user.id == self._voter_id

    @discord.ui.button(label="Confirm vote", style=discord.ButtonStyle.success)
    async def confirm(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[VoteConfirmView],
    ) -> None:
        if self._answered:
            return
        self._answered = True
        self.stop()
        poll_id = self._poll.poll_id
        try:
            outcome = self._bot.poll_store.record_vote(
                poll_id,
                self._voter_id,
                self._position,
            )
        except SQLAlchemyError as error:
            LOGGER.error(
                "Could not store a poll vote; poll_id=%s error_type=%s",
                poll_id,
                type(error).__name__,
            )
            await self._answer(
                interaction,
                "Your vote could not be saved. Nothing was recorded; pick "
                "your option again to retry.",
            )
            return
        await self._answer(interaction, self._outcome_message(outcome))
        if outcome is VoteOutcome.RECORDED:
            # Only a first vote moves the count on the post; a changed one
            # leaves it where it was.
            await refresh_poll_message(self._bot, poll_id)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[VoteConfirmView],
    ) -> None:
        if self._answered:
            return
        self._answered = True
        self.stop()
        LOGGER.debug(
            "Poll vote cancelled; poll_id=%s user_id=%s",
            self._poll.poll_id,
            self._voter_id,
        )
        content = "Vote cancelled. Nothing was recorded."
        if self._current is not None:
            content += (
                f" Your vote for {_bold_option(self._poll, self._current)} "
                "still stands."
            )
        await self._answer(interaction, content)

    async def on_timeout(self) -> None:
        if self._answered or self.message is None:
            return
        LOGGER.debug(
            "Poll vote confirmation expired; poll_id=%s user_id=%s",
            self._poll.poll_id,
            self._voter_id,
        )
        try:
            await self.message.edit(
                content=(
                    "This confirmation expired, so nothing was recorded. Pick "
                    "your option again to vote."
                ),
                view=None,
                allowed_mentions=NO_MENTIONS,
            )
        except discord.HTTPException as error:
            log_discord_failure(
                "Could not mark a poll vote confirmation expired; poll_id=%s",
                error,
                self._poll.poll_id,
            )

    def _outcome_message(self, outcome: VoteOutcome) -> str:
        picked = _bold_option(self._poll, self._position)
        name = f"**{escape_markdown(self._poll.name)}**"
        if outcome is VoteOutcome.CLOSED:
            return f"{name} ended before you confirmed, so your vote was not counted."
        if outcome is VoteOutcome.CHANGED:
            return f"✅ Your vote in {name} is now {picked}. Only you can see this."
        if outcome is VoteOutcome.UNCHANGED:
            return f"Your vote in {name} was already {picked}. Nothing changed."
        return f"✅ Your vote for {picked} in {name} is recorded. Only you can see this."

    async def _answer(self, interaction: discord.Interaction, content: str) -> None:
        try:
            await interaction.response.edit_message(
                content=content,
                view=None,
                allowed_mentions=NO_MENTIONS,
            )
        except discord.HTTPException as error:
            log_discord_failure(
                "Could not answer a poll vote confirmation; poll_id=%s",
                error,
                self._poll.poll_id,
            )


def poll_message(bot: Gw2Bot, poll: Poll) -> discord.PartialMessage | None:
    """The poll's post, addressed by id so no channel has to be cached."""
    if poll.message_id is None:
        return None
    return bot.get_partial_messageable(poll.channel_id).get_partial_message(
        poll.message_id
    )


async def refresh_poll_message(bot: Gw2Bot, poll_id: int) -> None:
    """Bring the vote count on an open poll's post up to date.

    A failure here costs only a stale count - the vote is already stored - so
    it is logged and dropped rather than reported to the voter.
    """
    try:
        poll = bot.poll_store.get_poll(poll_id)
        if poll is None or not poll.is_open:
            return
        count = bot.poll_store.voter_count(poll_id)
    except SQLAlchemyError as error:
        LOGGER.error(
            "Could not read a poll to refresh its post; poll_id=%s error_type=%s",
            poll_id,
            type(error).__name__,
        )
        return
    message = poll_message(bot, poll)
    if message is None:
        return
    try:
        await message.edit(embed=open_poll_embed(poll, count))
    except discord.HTTPException as error:
        log_discord_failure(
            "Could not refresh a poll's vote count; poll_id=%s",
            error,
            poll_id,
        )
        return
    LOGGER.debug("Refreshed poll post; poll_id=%s votes=%s", poll_id, count)
