from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import discord
from sqlalchemy.exc import SQLAlchemyError

from gw2bot.discord_utils import user_has_role
from gw2bot.polls.formatting import (
    POLL_DURATION_PLACEHOLDER,
    POLL_TITLE_MAX_LENGTH,
    build_poll_embed,
    format_poll_duration_input,
    format_poll_options_input,
    parse_poll_duration,
    parse_poll_options,
    parse_poll_title,
)
from gw2bot.polls.models import Poll
from gw2bot.polls.reactions import (
    enforce_single_choice,
    post_poll,
    render_poll_message,
    repost_poll,
    reseed_poll_reactions,
    resolve_channel,
    finalize_poll,
)
from gw2bot.polls.roles import POLL_MANAGE_ROLE_ID

if TYPE_CHECKING:
    from gw2bot.bot import Gw2Bot

LOGGER = logging.getLogger(__name__)

FLOW_TIMEOUT_SECONDS = 600
# 10 options * (100 chars + newline) leaves comfortable headroom.
POLL_OPTIONS_INPUT_MAX_LENGTH = 2000
PREVIEW_FOOTER_TEXT = "pollID: assigned when posted"


@dataclass
class PollDraft:
    creator_discord_id: int
    title: str = ""
    options: tuple[str, ...] = field(default_factory=tuple)
    options_text: str = ""
    allow_multiple: bool = False
    channel_id: int | None = None
    duration_minutes: int | None = None
    duration_text: str = ""
    # Set when the draft edits an existing poll rather than creating one.
    editing_poll_id: int | None = None
    posted: bool = False
    edit_applied: bool = False

    def is_complete(self) -> bool:
        return (
            bool(self.title)
            and len(self.options) >= 2
            and self.channel_id is not None
            and self.duration_minutes is not None
        )


def poll_duration_minutes(poll: Poll) -> int:
    return max(
        1,
        round((poll.end_time - poll.created_at).total_seconds() / 60),
    )


def draft_from_poll(poll: Poll) -> PollDraft:
    return PollDraft(
        creator_discord_id=poll.creator_discord_id,
        title=poll.title,
        options=poll.options,
        options_text=format_poll_options_input(poll.options),
        allow_multiple=poll.allow_multiple,
        channel_id=poll.channel_id,
        duration_minutes=poll_duration_minutes(poll),
        duration_text=format_poll_duration_input(poll.created_at, poll.end_time),
        editing_poll_id=poll.poll_id,
    )


async def ensure_manage_role(
    interaction: discord.Interaction,
    action: str,
) -> bool:
    """True when the user may manage polls; otherwise sends an ephemeral refusal
    and returns False. Only call before the interaction has been responded to."""
    if user_has_role(interaction.user, POLL_MANAGE_ROLE_ID):
        return True
    LOGGER.warning(
        "Rejected poll %s from Discord user %s; required role %s",
        action,
        interaction.user.id,
        POLL_MANAGE_ROLE_ID,
    )
    await interaction.response.send_message(
        f"You do not have the required role to {action} polls.",
        ephemeral=True,
    )
    return False


def _yes_no_options(selected: bool) -> list[discord.SelectOption]:
    return [
        discord.SelectOption(
            label="One choice per person",
            value="no",
            default=not selected,
        ),
        discord.SelectOption(
            label="Allow multiple choices",
            value="yes",
            default=selected,
        ),
    ]


def _is_ephemeral_component_interaction(
    interaction: discord.Interaction,
) -> bool:
    message = interaction.message
    return message is not None and message.flags.ephemeral


def _preview_poll(draft: PollDraft, now: datetime) -> Poll:
    duration = draft.duration_minutes or 0
    return Poll(
        poll_id=draft.editing_poll_id or 0,
        guild_id=0,
        channel_id=draft.channel_id or 0,
        creator_discord_id=draft.creator_discord_id,
        title=draft.title,
        options=draft.options,
        allow_multiple=draft.allow_multiple,
        created_at=now,
        end_time=now + timedelta(minutes=duration),
    )


def build_poll_preview(
    bot: Gw2Bot,
    draft: PollDraft,
) -> tuple[list[discord.Embed], discord.ui.View]:
    now = datetime.now(UTC)
    preview = build_poll_embed(_preview_poll(draft, now), {}, now=now)
    view: discord.ui.View
    if draft.editing_poll_id is not None:
        confirmation = discord.Embed(
            title="Edit poll",
            description=(
                "Above is how the poll will look after your changes. Save them "
                "or change something else?"
            ),
        )
        view = PollEditConfirmView(bot, draft)
    else:
        preview.set_footer(text=PREVIEW_FOOTER_TEXT)
        confirmation = discord.Embed(
            title="Create poll",
            description=(
                "Above is how the poll will look. Post it or change something?"
            ),
        )
        view = PollConfirmView(bot, draft)
    return [preview, confirmation], view


async def send_poll_preview(
    bot: Gw2Bot,
    interaction: discord.Interaction,
    draft: PollDraft,
) -> None:
    embeds, view = build_poll_preview(bot, draft)
    LOGGER.debug(
        "Sending poll preview; user_id=%s options=%s allow_multiple=%s "
        "in_place=%s editing=%s",
        draft.creator_discord_id,
        len(draft.options),
        draft.allow_multiple,
        _is_ephemeral_component_interaction(interaction),
        draft.editing_poll_id is not None,
    )
    if _is_ephemeral_component_interaction(interaction):
        await interaction.response.edit_message(
            content=None,
            embeds=embeds,
            view=view,
        )
    else:
        await interaction.response.send_message(
            embeds=embeds,
            view=view,
            ephemeral=True,
        )


async def _send_validation_error(
    interaction: discord.Interaction,
    error: ValueError,
    retry_view: discord.ui.View,
) -> None:
    LOGGER.debug(
        "Poll input validation failed; error_type=%s",
        type(error).__name__,
    )
    message = f"{error} Press **Try again** to correct it."
    if _is_ephemeral_component_interaction(interaction):
        await interaction.response.edit_message(
            content=message,
            embeds=[],
            view=retry_view,
        )
    else:
        await interaction.response.send_message(
            message,
            view=retry_view,
            ephemeral=True,
        )


class PollDetailsModal(discord.ui.Modal):
    def __init__(self, bot: Gw2Bot, draft: PollDraft):
        editing = draft.editing_poll_id is not None
        super().__init__(title="Edit poll" if editing else "Create poll")
        self._bot = bot
        self._draft = draft
        self.title_input = discord.ui.TextInput["PollDetailsModal"](
            default=draft.title or None,
            placeholder="Ask your question",
            max_length=POLL_TITLE_MAX_LENGTH,
        )
        self.add_item(
            discord.ui.Label(
                text="Poll question (title)",
                component=self.title_input,
            )
        )
        self.options_input = discord.ui.TextInput["PollDetailsModal"](
            style=discord.TextStyle.paragraph,
            default=draft.options_text or None,
            placeholder="One option per line",
            max_length=POLL_OPTIONS_INPUT_MAX_LENGTH,
        )
        self.add_item(
            discord.ui.Label(
                text="Options",
                description="One option per line, from 2 up to 10.",
                component=self.options_input,
            )
        )
        self.allow_multiple = discord.ui.Select["PollDetailsModal"](
            options=_yes_no_options(draft.allow_multiple),
        )
        self.add_item(
            discord.ui.Label(
                text="Can a member pick more than one option?",
                component=self.allow_multiple,
            )
        )
        default_channel: list[discord.SelectDefaultValue] = []
        if draft.channel_id is not None:
            default_channel = [
                discord.SelectDefaultValue(
                    id=draft.channel_id,
                    type=discord.SelectDefaultValueType.channel,
                )
            ]
        self.channel = discord.ui.ChannelSelect["PollDetailsModal"](
            channel_types=[discord.ChannelType.text],
            required=True,
            default_values=default_channel,
        )
        self.add_item(
            discord.ui.Label(
                text="Which channel should the poll post in?",
                component=self.channel,
            )
        )
        self.duration_input = discord.ui.TextInput["PollDetailsModal"](
            default=draft.duration_text or None,
            placeholder=POLL_DURATION_PLACEHOLDER,
            max_length=8,
        )
        self.add_item(
            discord.ui.Label(
                text="How long should the poll run? (HH:mm)",
                description="Use a large hour value for multi-day polls.",
                component=self.duration_input,
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        self._draft.title = self.title_input.value.strip()
        self._draft.options_text = self.options_input.value
        self._draft.duration_text = self.duration_input.value.strip()
        self._draft.allow_multiple = self.allow_multiple.values[0] == "yes"
        self._draft.channel_id = self.channel.values[0].id
        try:
            title = parse_poll_title(self.title_input.value)
            options = parse_poll_options(self.options_input.value)
            duration = parse_poll_duration(self.duration_input.value)
        except ValueError as error:
            await _send_validation_error(
                interaction,
                error,
                PollRetryView(self._bot, self._draft),
            )
            return
        self._draft.title = title
        self._draft.options = options
        self._draft.duration_minutes = duration
        LOGGER.debug(
            "Poll details submitted; user_id=%s options=%s allow_multiple=%s "
            "editing=%s",
            interaction.user.id,
            len(options),
            self._draft.allow_multiple,
            self._draft.editing_poll_id is not None,
        )
        await send_poll_preview(self._bot, interaction, self._draft)


class PollRetryView(discord.ui.View):
    def __init__(self, bot: Gw2Bot, draft: PollDraft):
        super().__init__(timeout=FLOW_TIMEOUT_SECONDS)
        self._bot = bot
        self._draft = draft

    @discord.ui.button(label="Try again", style=discord.ButtonStyle.primary)
    async def retry(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[PollRetryView],
    ) -> None:
        await interaction.response.send_modal(
            PollDetailsModal(self._bot, self._draft)
        )


class _PollPreviewView(discord.ui.View):
    def __init__(self, bot: Gw2Bot, draft: PollDraft):
        super().__init__(timeout=FLOW_TIMEOUT_SECONDS)
        self._bot = bot
        self._draft = draft

    @discord.ui.button(
        label="Change something",
        style=discord.ButtonStyle.secondary,
    )
    async def change_something(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[_PollPreviewView],
    ) -> None:
        await interaction.response.send_modal(
            PollDetailsModal(self._bot, self._draft)
        )


class PollConfirmView(_PollPreviewView):
    @discord.ui.button(label="Post poll", style=discord.ButtonStyle.success)
    async def post(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[PollConfirmView],
    ) -> None:
        if not await ensure_manage_role(interaction, "create"):
            return
        if self._draft.posted:
            await interaction.response.send_message(
                "This poll was already posted.",
                ephemeral=True,
            )
            return
        if not self._draft.is_complete():
            await interaction.response.send_message(
                "The poll is missing required details. Use **Change something** "
                "to fill them in.",
                ephemeral=True,
            )
            return
        self._draft.posted = True
        await interaction.response.edit_message(view=None)
        try:
            poll = self._bot.poll_store.create_poll(
                guild_id=interaction.guild_id or 0,
                channel_id=self._draft.channel_id or 0,
                creator_discord_id=self._draft.creator_discord_id,
                title=self._draft.title,
                options=self._draft.options,
                allow_multiple=self._draft.allow_multiple,
                duration_minutes=self._draft.duration_minutes or 0,
            )
        except SQLAlchemyError as exc:
            self._draft.posted = False
            await self._restore_controls(interaction)
            LOGGER.error(
                "Could not store poll; user_id=%s error_type=%s",
                interaction.user.id,
                type(exc).__name__,
            )
            await interaction.followup.send(
                "The poll could not be saved. Try again later.",
                ephemeral=True,
            )
            return
        try:
            posted = await post_poll(self._bot, poll)
        except (discord.HTTPException, SQLAlchemyError, RuntimeError) as exc:
            self._draft.posted = False
            await self._restore_controls(interaction)
            LOGGER.error(
                "Could not post poll; user_id=%s error_type=%s",
                interaction.user.id,
                type(exc).__name__,
            )
            try:
                self._bot.poll_store.delete_poll(poll.poll_id)
            except SQLAlchemyError as cleanup_exc:
                LOGGER.error(
                    "Could not clean up unposted poll; poll_id=%s error_type=%s",
                    poll.poll_id,
                    type(cleanup_exc).__name__,
                )
            await interaction.followup.send(
                "The poll could not be posted to the selected channel. Check "
                "the bot's permissions there and try again.",
                ephemeral=True,
            )
            return
        LOGGER.debug(
            "Poll posted from preview; poll_id=%s user_id=%s",
            posted.poll_id,
            interaction.user.id,
        )
        await interaction.followup.send(
            f"Poll **{posted.poll_id}** was posted in "
            f"<#{posted.channel_id}>.",
            ephemeral=True,
        )

    async def _restore_controls(
        self,
        interaction: discord.Interaction,
    ) -> None:
        try:
            await interaction.edit_original_response(view=self)
        except discord.HTTPException as exc:
            LOGGER.error(
                "Could not restore poll post controls; user_id=%s "
                "error_type=%s",
                interaction.user.id,
                type(exc).__name__,
            )


class PollEditConfirmView(_PollPreviewView):
    @discord.ui.button(label="Save changes", style=discord.ButtonStyle.success)
    async def save_changes(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[PollEditConfirmView],
    ) -> None:
        editing_poll_id = self._draft.editing_poll_id
        if editing_poll_id is None:
            await interaction.response.send_message(
                "This edit session is no longer valid.",
                ephemeral=True,
            )
            return
        if not await ensure_manage_role(interaction, "edit"):
            return
        if not self._draft.is_complete():
            await interaction.response.send_message(
                "The poll is missing required details. Use **Change something** "
                "to fill them in.",
                ephemeral=True,
            )
            return
        stored = self._bot.poll_store.get_poll(editing_poll_id)
        if stored is None:
            await interaction.response.send_message(
                "This poll no longer exists.",
                ephemeral=True,
            )
            return
        channel_changed = stored.channel_id != self._draft.channel_id
        if channel_changed and stored.message_id is not None:
            await interaction.response.edit_message(
                content=(
                    "Changing the channel will **delete the current poll "
                    "message and clear every reaction and vote**, then re-post "
                    "it in the new channel. Continue?"
                ),
                embeds=[],
                view=PollChannelMoveConfirmView(
                    self._bot,
                    self._draft,
                    stored.channel_id,
                ),
            )
            return
        await apply_poll_edit(
            self._bot,
            interaction,
            self._draft,
            stored,
            repost=False,
        )


class PollChannelMoveConfirmView(discord.ui.View):
    def __init__(
        self,
        bot: Gw2Bot,
        draft: PollDraft,
        old_channel_id: int,
    ):
        super().__init__(timeout=FLOW_TIMEOUT_SECONDS)
        self._bot = bot
        self._draft = draft
        self._old_channel_id = old_channel_id

    @discord.ui.button(label="Move poll", style=discord.ButtonStyle.danger)
    async def move(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[PollChannelMoveConfirmView],
    ) -> None:
        if not await ensure_manage_role(interaction, "edit"):
            return
        editing_poll_id = self._draft.editing_poll_id
        stored = (
            self._bot.poll_store.get_poll(editing_poll_id)
            if editing_poll_id is not None
            else None
        )
        if stored is None:
            await interaction.response.edit_message(
                content="This poll no longer exists.",
                embeds=[],
                view=None,
            )
            return
        await apply_poll_edit(
            self._bot,
            interaction,
            self._draft,
            stored,
            repost=True,
        )

    @discord.ui.button(
        label="Keep current channel",
        style=discord.ButtonStyle.secondary,
    )
    async def keep(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[PollChannelMoveConfirmView],
    ) -> None:
        self._draft.channel_id = self._old_channel_id
        await send_poll_preview(self._bot, interaction, self._draft)


async def apply_poll_edit(
    bot: Gw2Bot,
    interaction: discord.Interaction,
    draft: PollDraft,
    stored: Poll,
    *,
    repost: bool,
) -> None:
    editing_poll_id = draft.editing_poll_id
    if editing_poll_id is None:
        raise ValueError("apply_poll_edit requires an editing draft")
    # Guard a double click racing two callbacks before the first clears the
    # buttons; the check and set are synchronous, so the second observes it.
    if draft.edit_applied:
        await interaction.response.send_message(
            "This poll was already updated.",
            ephemeral=True,
        )
        return
    draft.edit_applied = True
    await interaction.response.edit_message(
        content="Saving your changes…",
        embeds=[],
        view=None,
    )
    duration = draft.duration_minutes or 0
    new_end_time = stored.created_at + timedelta(minutes=duration)
    try:
        updated = bot.poll_store.update_poll(
            poll_id=editing_poll_id,
            title=draft.title,
            options=draft.options,
            allow_multiple=draft.allow_multiple,
            end_time=new_end_time,
        )
    except SQLAlchemyError as exc:
        draft.edit_applied = False
        LOGGER.error(
            "Could not save poll edit; poll_id=%s error_type=%s",
            editing_poll_id,
            type(exc).__name__,
        )
        await interaction.edit_original_response(
            content="The changes could not be saved. Try again later.",
            view=None,
        )
        return
    options_count_changed = len(stored.options) != len(updated.options)
    became_single = stored.allow_multiple and not updated.allow_multiple
    try:
        if repost:
            updated = await repost_poll(
                bot,
                updated,
                stored.channel_id,
                draft.channel_id or stored.channel_id,
            )
        else:
            if options_count_changed:
                # Reactions map to option positions, so a changed option count
                # invalidates existing votes; clear them and re-seed reactions.
                bot.poll_store.clear_votes(updated.poll_id)
                await reseed_poll_reactions(bot, updated)
            elif became_single:
                await enforce_single_choice(bot, updated)
            await render_poll_message(bot, updated)
    except (discord.HTTPException, SQLAlchemyError) as exc:
        LOGGER.error(
            "Poll edit saved but its message could not be updated; poll_id=%s "
            "error_type=%s",
            updated.poll_id,
            type(exc).__name__,
        )
        await interaction.edit_original_response(
            content=(
                f"Poll **{updated.poll_id}** was saved, but its message could "
                "not be updated and may be out of date."
            ),
            view=None,
        )
        return
    LOGGER.debug(
        "Applied poll edit; poll_id=%s repost=%s options_count_changed=%s "
        "became_single=%s",
        updated.poll_id,
        repost,
        options_count_changed,
        became_single,
    )
    await interaction.edit_original_response(
        content=f"Poll **{updated.poll_id}** was updated.",
        view=None,
    )


class PollDeleteConfirmView(discord.ui.View):
    def __init__(self, bot: Gw2Bot, poll: Poll):
        super().__init__(timeout=FLOW_TIMEOUT_SECONDS)
        self._bot = bot
        self._poll = poll
        self._deleting = False

    @discord.ui.button(label="Delete poll", style=discord.ButtonStyle.danger)
    async def delete(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[PollDeleteConfirmView],
    ) -> None:
        if not await ensure_manage_role(interaction, "delete"):
            return
        if self._deleting:
            await interaction.response.send_message(
                "This poll is already being deleted.",
                ephemeral=True,
            )
            return
        self._deleting = True
        await interaction.response.edit_message(
            content="Deleting the poll…",
            embeds=[],
            view=None,
        )
        poll = self._bot.poll_store.get_poll(self._poll.poll_id)
        if poll is not None and poll.message_id is not None:
            try:
                channel = await resolve_channel(self._bot, poll.channel_id)
                await channel.get_partial_message(poll.message_id).delete()
            except discord.HTTPException as exc:
                LOGGER.error(
                    "Could not delete poll message; poll_id=%s error_type=%s",
                    poll.poll_id,
                    type(exc).__name__,
                )
        try:
            self._bot.poll_store.delete_poll(self._poll.poll_id)
        except SQLAlchemyError as exc:
            self._deleting = False
            LOGGER.error(
                "Could not delete poll; poll_id=%s error_type=%s",
                self._poll.poll_id,
                type(exc).__name__,
            )
            await interaction.edit_original_response(
                content="The poll could not be deleted. Try again later.",
                view=None,
            )
            return
        LOGGER.debug(
            "Deleted poll; poll_id=%s user_id=%s",
            self._poll.poll_id,
            interaction.user.id,
        )
        await interaction.edit_original_response(
            content=f"Poll **{self._poll.poll_id}** was deleted.",
            view=None,
        )

    @discord.ui.button(label="Keep poll", style=discord.ButtonStyle.secondary)
    async def keep(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[PollDeleteConfirmView],
    ) -> None:
        await interaction.response.edit_message(
            content="The poll was not deleted.",
            embeds=[],
            view=None,
        )


class PollCompleteConfirmView(discord.ui.View):
    def __init__(self, bot: Gw2Bot, poll: Poll):
        super().__init__(timeout=FLOW_TIMEOUT_SECONDS)
        self._bot = bot
        self._poll = poll
        self._completing = False

    @discord.ui.button(label="End poll now", style=discord.ButtonStyle.danger)
    async def complete(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[PollCompleteConfirmView],
    ) -> None:
        if not await ensure_manage_role(interaction, "complete"):
            return
        if self._completing:
            await interaction.response.send_message(
                "This poll is already being ended.",
                ephemeral=True,
            )
            return
        self._completing = True
        await interaction.response.edit_message(
            content="Ending the poll…",
            embeds=[],
            view=None,
        )
        poll = self._bot.poll_store.get_poll(self._poll.poll_id)
        if poll is None:
            await interaction.edit_original_response(
                content="That poll no longer exists.",
                view=None,
            )
            return
        finished = await finalize_poll(self._bot, poll, reason="manual")
        if not finished:
            self._completing = False
            await interaction.edit_original_response(
                content=(
                    "The poll could not be ended right now. Try again shortly."
                ),
                view=None,
            )
            return
        LOGGER.debug(
            "Completed poll from command; poll_id=%s user_id=%s",
            poll.poll_id,
            interaction.user.id,
        )
        await interaction.edit_original_response(
            content=f"Poll **{poll.poll_id}** was ended.",
            view=None,
        )

    @discord.ui.button(label="Keep running", style=discord.ButtonStyle.secondary)
    async def keep(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[PollCompleteConfirmView],
    ) -> None:
        await interaction.response.edit_message(
            content="The poll is still running.",
            embeds=[],
            view=None,
        )
