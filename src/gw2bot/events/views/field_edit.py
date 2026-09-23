"""The \"Change something\" flow: pick a field, then edit or re-pick it."""

from __future__ import annotations

import logging
from dataclasses import field
from datetime import UTC, datetime
from typing import Any, TYPE_CHECKING

import discord

from gw2bot.core.discord_utils import user_has_role
from gw2bot.events.formatting import (
    EVENT_DATETIME_PLACEHOLDER,
    parse_event_datetime,
    parse_event_duration,
)
from gw2bot.events.models import Event, EventCategory, MAX_PING_ROLES
from gw2bot.events.views.create import RepeatChoiceView
from gw2bot.events.views.preview import send_event_preview
from gw2bot.events.views.shared import (
    EVENT_CHANNEL_HINT,
    EVENT_CHANNEL_PROMPT,
    EVENT_CHANNEL_TYPES,
    EVENT_DESCRIPTION_MAX_LENGTH,
    EVENT_REQUIREMENTS_HINT,
    EVENT_REQUIREMENTS_MAX_LENGTH,
    EVENT_REQUIREMENTS_PROMPT,
    EVENT_TITLE_MAX_LENGTH,
    EventDraft,
    FLOW_TIMEOUT_SECONDS,
    PING_ROLE_HINT,
    PING_ROLE_NONE_AVAILABLE,
    PING_ROLE_PROMPT,
    _CHANGE_FIELDS,
    _ModalOpenView,
    _category_options,
    _destination_error,
    _picked_ping_role_ids,
    _ping_role_options,
    _send_validation_error,
)

if TYPE_CHECKING:
    from gw2bot.bot import Gw2Bot

LOGGER = logging.getLogger(__name__)


class ChangeFieldSelect(discord.ui.Select["ChangeFieldView"]):
    def __init__(self, fields: tuple[tuple[str, str], ...] = _CHANGE_FIELDS):
        super().__init__(
            placeholder="What would you like to change?",
            options=[
                discord.SelectOption(label=label, value=value)
                for value, label in fields
            ],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if view is not None:
            await view.handle_choice(interaction, self.values[0])


class ChangeFieldView(discord.ui.View):
    def __init__(
        self,
        bot: Gw2Bot,
        draft: EventDraft,
        *,
        fields: tuple[tuple[str, str], ...] = _CHANGE_FIELDS,
    ):
        super().__init__(timeout=FLOW_TIMEOUT_SECONDS)
        self._bot = bot
        self._draft = draft
        self.add_item(ChangeFieldSelect(fields))

    async def handle_choice(
        self,
        interaction: discord.Interaction,
        choice: str,
    ) -> None:
        LOGGER.debug(
            "Event change field selected; user_id=%s change_field=%s",
            interaction.user.id,
            choice,
        )
        if choice in (
            "title",
            "description",
            "start",
            "duration",
            "requirements",
        ):
            await interaction.response.send_modal(
                EventFieldEditModal(self._bot, self._draft, choice)
            )
            return
        if choice == "category":
            await interaction.response.edit_message(
                content="Which category is your event",
                view=CategoryPickView(self._bot, self._draft),
            )
            return
        if choice == "channel":
            await interaction.response.edit_message(
                content=f"{EVENT_CHANNEL_PROMPT} {EVENT_CHANNEL_HINT}",
                view=ChannelPickView(self._bot, self._draft),
            )
            return
        if choice == "leader":
            await interaction.response.edit_message(
                content="Who should lead this event?",
                view=LeaderPickView(self._bot, self._draft),
            )
            return
        if choice == "ping_roles":
            options = _ping_role_options(
                interaction.guild,
                self._draft.ping_role_ids,
            )
            if not options:
                # Nothing to offer, and a select with no options is a payload
                # Discord refuses. Say why and go back to the preview rather
                # than leaving the commander on a dead end.
                LOGGER.debug(
                    "Ping role picker has nothing to offer; user_id=%s",
                    interaction.user.id,
                )
                await send_event_preview(
                    self._bot,
                    interaction,
                    self._draft,
                    content=PING_ROLE_NONE_AVAILABLE,
                )
                return
            await interaction.response.edit_message(
                content=f"{PING_ROLE_PROMPT} {PING_ROLE_HINT}",
                view=PingRolesPickView(self._bot, self._draft, options),
            )
            return
        await interaction.response.edit_message(
            content="Would you like this event to repeat?",
            view=RepeatChoiceView(self._bot, self._draft),
        )


class EventFieldEditModal(discord.ui.Modal, title="Change something"):
    def __init__(self, bot: Gw2Bot, draft: EventDraft, field_name: str):
        super().__init__()
        self._bot = bot
        self._draft = draft
        self._field_name = field_name
        description: str | None = None
        if field_name == "title":
            label = "Enter the event title"
            self.field_input = discord.ui.TextInput["EventFieldEditModal"](
                default=draft.title or None,
                max_length=EVENT_TITLE_MAX_LENGTH,
            )
        elif field_name == "description":
            label = "Enter the event description"
            self.field_input = discord.ui.TextInput["EventFieldEditModal"](
                style=discord.TextStyle.paragraph,
                default=draft.description or None,
                max_length=EVENT_DESCRIPTION_MAX_LENGTH,
            )
        elif field_name == "requirements":
            label = EVENT_REQUIREMENTS_PROMPT
            description = EVENT_REQUIREMENTS_HINT
            self.field_input = discord.ui.TextInput["EventFieldEditModal"](
                style=discord.TextStyle.paragraph,
                default=draft.requirements or None,
                max_length=EVENT_REQUIREMENTS_MAX_LENGTH,
                required=False,
            )
        elif field_name == "start":
            label = f"When will your event be? ({EVENT_DATETIME_PLACEHOLDER})"
            self.field_input = discord.ui.TextInput["EventFieldEditModal"](
                default=draft.start_text or None,
                placeholder=EVENT_DATETIME_PLACEHOLDER,
                max_length=16,
            )
        else:
            label = "How long will your event be? (HH:mm)"
            self.field_input = discord.ui.TextInput["EventFieldEditModal"](
                default=draft.duration_text or None,
                placeholder="HH:mm",
                max_length=6,
            )
        self.add_item(
            discord.ui.Label(
                text=label,
                description=description,
                component=self.field_input,
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        value = self.field_input.value.strip()
        try:
            if self._field_name == "title":
                if not value:
                    raise ValueError("The event title cannot be empty.")
                self._draft.title = value
            elif self._field_name == "description":
                if not value:
                    raise ValueError("The event description cannot be empty.")
                self._draft.description = value
            elif self._field_name == "requirements":
                # Blank is a real answer: it clears the requirements.
                self._draft.requirements = value
            elif self._field_name == "start":
                self._draft.start_text = value
                start_time = parse_event_datetime(
                    value,
                    self._bot.event_timezone,
                )
                if start_time <= datetime.now(UTC):
                    raise ValueError("The event start must be in the future.")
                self._draft.start_time = start_time
            else:
                self._draft.duration_text = value
                self._draft.duration_minutes = parse_event_duration(value)
        except ValueError as error:
            await _send_validation_error(
                interaction,
                error,
                RetryFieldEditView(self._bot, self._draft, self._field_name),
            )
            return
        await send_event_preview(self._bot, interaction, self._draft)


class RetryFieldEditView(_ModalOpenView):
    def __init__(self, bot: Gw2Bot, draft: EventDraft, field_name: str):
        super().__init__(bot, draft, "Try again")
        self._field_name = field_name

    def build_modal(self) -> discord.ui.Modal:
        return EventFieldEditModal(self._bot, self._draft, self._field_name)


class CategoryPickSelect(discord.ui.Select["CategoryPickView"]):
    def __init__(self, draft: EventDraft):
        super().__init__(options=_category_options(draft.category))

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if view is not None:
            await view.pick(interaction, EventCategory(self.values[0]))


class CategoryPickView(discord.ui.View):
    def __init__(self, bot: Gw2Bot, draft: EventDraft):
        super().__init__(timeout=FLOW_TIMEOUT_SECONDS)
        self._bot = bot
        self._draft = draft
        self.add_item(CategoryPickSelect(draft))

    async def pick(
        self,
        interaction: discord.Interaction,
        category: EventCategory,
    ) -> None:
        self._draft.category = category
        await send_event_preview(self._bot, interaction, self._draft)


class ChannelPickSelect(discord.ui.ChannelSelect["ChannelPickView"]):
    def __init__(self):
        super().__init__(channel_types=EVENT_CHANNEL_TYPES)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if view is not None:
            await view.pick(interaction, self.values[0])


class ChannelPickView(discord.ui.View):
    def __init__(self, bot: Gw2Bot, draft: EventDraft):
        super().__init__(timeout=FLOW_TIMEOUT_SECONDS)
        self._bot = bot
        self._draft = draft
        self.add_item(ChannelPickSelect())

    async def pick(
        self,
        interaction: discord.Interaction,
        channel: Any,
    ) -> None:
        rejection = await _destination_error(self._bot, channel)
        if rejection is not None:
            await interaction.response.edit_message(
                content=f"{rejection} Pick somewhere else.",
                view=ChannelPickView(self._bot, self._draft),
            )
            return
        self._draft.channel_id = channel.id
        await send_event_preview(self._bot, interaction, self._draft)


class PingRolesSelect(discord.ui.Select["PingRolesPickView"]):
    def __init__(self, options: list[discord.SelectOption]):
        super().__init__(
            placeholder=PING_ROLE_PROMPT,
            options=options,
            # Nothing picked is a valid answer: it is how the roles are taken
            # off an event that used to ping them.
            min_values=0,
            max_values=min(MAX_PING_ROLES, len(options)),
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if view is not None:
            await view.pick(
                interaction,
                _picked_ping_role_ids(self.values, self.options),
            )


class PingRolesPickView(discord.ui.View):
    def __init__(
        self,
        bot: Gw2Bot,
        draft: EventDraft,
        options: list[discord.SelectOption],
    ):
        super().__init__(timeout=FLOW_TIMEOUT_SECONDS)
        self._bot = bot
        self._draft = draft
        self.add_item(PingRolesSelect(options))

    async def pick(
        self,
        interaction: discord.Interaction,
        role_ids: tuple[int, ...],
    ) -> None:
        self._draft.ping_role_ids = role_ids
        LOGGER.debug(
            "Event ping roles picked; user_id=%s ping_roles=%s",
            interaction.user.id,
            len(role_ids),
        )
        await send_event_preview(self._bot, interaction, self._draft)


class LeaderPickSelect(discord.ui.UserSelect["LeaderPickView"]):
    def __init__(self):
        super().__init__(placeholder="Search for the new event leader")

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if view is not None:
            await view.pick(interaction, self.values[0])


class LeaderPickView(discord.ui.View):
    def __init__(self, bot: Gw2Bot, draft: EventDraft):
        super().__init__(timeout=FLOW_TIMEOUT_SECONDS)
        self._bot = bot
        self._draft = draft
        self.add_item(LeaderPickSelect())

    async def pick(
        self,
        interaction: discord.Interaction,
        user: discord.Member | discord.User,
    ) -> None:
        if not user_has_role(user, self._bot._config.event_create_role_id):
            LOGGER.debug(
                "Rejected event leader change; user_id=%s candidate_id=%s "
                "authorized=false",
                interaction.user.id,
                user.id,
            )
            await interaction.response.edit_message(
                content=(
                    "That member does not have the required role to lead "
                    "events. Pick someone else."
                ),
                view=LeaderPickView(self._bot, self._draft),
            )
            return
        self._draft.leader_discord_id = user.id
        await send_event_preview(self._bot, interaction, self._draft)
