from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

import discord
from sqlalchemy.exc import SQLAlchemyError

from gw2bot.discord_utils import user_has_role
from gw2bot.events.formatting import (
    EVENT_DATETIME_PLACEHOLDER,
    confirm_embed,
    describe_repeat,
    event_embed,
    parse_event_datetime,
    parse_event_duration,
    parse_repeat_days,
)
from gw2bot.events.models import (
    AutoSignupChoice,
    Event,
    EventCategory,
    EventOccurrence,
    EventRole,
    EventSignup,
    EventStatus,
    PreferenceMode,
    RepeatFrequency,
    fitting_roles,
)
from gw2bot.events.roles import EVENT_CREATE_ROLE_ID

if TYPE_CHECKING:
    from gw2bot.bot import Gw2Bot

LOGGER = logging.getLogger(__name__)

EVENT_TITLE_MAX_LENGTH = 256
EVENT_DESCRIPTION_MAX_LENGTH = 4000
FLOW_TIMEOUT_SECONDS = 600
PREVIEW_EVENT_ID_TEXT = "—"


@dataclass
class EventDraft:
    leader_discord_id: int
    category: EventCategory | None = None
    title: str = ""
    description: str = ""
    channel_id: int | None = None
    start_time: datetime | None = None
    start_text: str = ""
    duration_minutes: int | None = None
    duration_text: str = ""
    repeat_frequency: RepeatFrequency = RepeatFrequency.NONE
    repeat_days: tuple[int, ...] = field(default_factory=tuple)
    repeat_days_text: str = ""
    posted: bool = False

    def is_complete(self) -> bool:
        return (
            self.category is not None
            and bool(self.title)
            and bool(self.description)
            and self.channel_id is not None
            and self.start_time is not None
            and self.duration_minutes is not None
        )

    def to_event(self, event_id: int = 0) -> Event:
        if (
            self.category is None
            or self.channel_id is None
            or self.start_time is None
            or self.duration_minutes is None
        ):
            raise ValueError("The event draft is missing required fields.")
        return Event(
            event_id=event_id,
            category=self.category,
            title=self.title,
            description=self.description,
            channel_id=self.channel_id,
            leader_discord_id=self.leader_discord_id,
            start_time=self.start_time,
            duration_minutes=self.duration_minutes,
            repeat_frequency=self.repeat_frequency,
            repeat_days=self.repeat_days,
        )


def _category_options(
    selected: EventCategory | None,
) -> list[discord.SelectOption]:
    return [
        discord.SelectOption(
            label=category.value,
            value=category.value,
            default=category is selected,
        )
        for category in EventCategory
    ]


def _yes_no_options(selected: bool | None) -> list[discord.SelectOption]:
    return [
        discord.SelectOption(
            label="Yes",
            value="yes",
            default=selected is True,
        ),
        discord.SelectOption(
            label="No",
            value="no",
            default=selected is False,
        ),
    ]


def _frequency_options(
    selected: RepeatFrequency,
) -> list[discord.SelectOption]:
    return [
        discord.SelectOption(
            label=frequency.value.capitalize(),
            value=frequency.value,
            default=frequency is selected,
        )
        for frequency in (
            RepeatFrequency.DAILY,
            RepeatFrequency.WEEKLY,
            RepeatFrequency.MONTHLY,
        )
    ]


def _is_ephemeral_component_interaction(
    interaction: discord.Interaction,
) -> bool:
    message = interaction.message
    return message is not None and message.flags.ephemeral


async def send_event_preview(
    bot: Gw2Bot,
    interaction: discord.Interaction,
    draft: EventDraft,
) -> None:
    preview = event_embed(
        draft.to_event(),
        [],
        EventStatus.OPEN,
        event_id_text=PREVIEW_EVENT_ID_TEXT,
    )
    confirmation = confirm_embed()
    repeat_text = describe_repeat(draft.repeat_frequency, draft.repeat_days)
    confirmation.description = (
        f"{confirmation.description}\n\n*{repeat_text}.*"
    )
    view = EventConfirmView(bot, draft)
    LOGGER.debug(
        "Sending event preview; user_id=%s category=%s repeat=%s "
        "title_characters=%s in_place=%s",
        draft.leader_discord_id,
        draft.category.value if draft.category is not None else None,
        draft.repeat_frequency.value,
        len(draft.title),
        _is_ephemeral_component_interaction(interaction),
    )
    if _is_ephemeral_component_interaction(interaction):
        await interaction.response.edit_message(
            content=None,
            embeds=[preview, confirmation],
            view=view,
        )
    else:
        await interaction.response.send_message(
            embeds=[preview, confirmation],
            view=view,
            ephemeral=True,
        )


async def _send_validation_error(
    interaction: discord.Interaction,
    error: ValueError,
    retry_view: discord.ui.View,
) -> None:
    LOGGER.debug(
        "Event input validation failed; error_type=%s",
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


class _ModalOpenButton(discord.ui.Button["_ModalOpenView"]):
    def __init__(self, label: str, style: discord.ButtonStyle):
        super().__init__(label=label, style=style)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if view is not None:
            await interaction.response.send_modal(view.build_modal())


class _ModalOpenView(discord.ui.View):
    def __init__(
        self,
        bot: Gw2Bot,
        draft: EventDraft,
        label: str,
        style: discord.ButtonStyle = discord.ButtonStyle.primary,
    ):
        super().__init__(timeout=FLOW_TIMEOUT_SECONDS)
        self._bot = bot
        self._draft = draft
        self.add_item(_ModalOpenButton(label, style))

    def build_modal(self) -> discord.ui.Modal:
        raise NotImplementedError


class ContinueToScheduleView(_ModalOpenView):
    def __init__(self, bot: Gw2Bot, draft: EventDraft):
        super().__init__(bot, draft, "Continue")

    def build_modal(self) -> discord.ui.Modal:
        return EventScheduleModal(self._bot, self._draft)


class RetryScheduleView(_ModalOpenView):
    def __init__(self, bot: Gw2Bot, draft: EventDraft):
        super().__init__(bot, draft, "Try again")

    def build_modal(self) -> discord.ui.Modal:
        return EventScheduleModal(self._bot, self._draft)


class ContinueToRepeatView(_ModalOpenView):
    def __init__(self, bot: Gw2Bot, draft: EventDraft):
        super().__init__(bot, draft, "Continue")

    def build_modal(self) -> discord.ui.Modal:
        return EventRepeatModal(self._bot, self._draft)


class RetryRepeatView(_ModalOpenView):
    def __init__(self, bot: Gw2Bot, draft: EventDraft):
        super().__init__(bot, draft, "Try again")

    def build_modal(self) -> discord.ui.Modal:
        return EventRepeatModal(self._bot, self._draft)


class EventDetailsModal(discord.ui.Modal, title="Create new event"):
    def __init__(self, bot: Gw2Bot, draft: EventDraft):
        super().__init__()
        self._bot = bot
        self._draft = draft
        self.category = discord.ui.Select["EventDetailsModal"](
            options=_category_options(draft.category),
        )
        self.add_item(
            discord.ui.Label(
                text="Which category is your event",
                component=self.category,
            )
        )
        self.title_input = discord.ui.TextInput["EventDetailsModal"](
            default=draft.title or None,
            max_length=EVENT_TITLE_MAX_LENGTH,
        )
        self.add_item(
            discord.ui.Label(
                text="Enter the event title",
                component=self.title_input,
            )
        )
        self.description_input = discord.ui.TextInput["EventDetailsModal"](
            style=discord.TextStyle.paragraph,
            default=draft.description or None,
            max_length=EVENT_DESCRIPTION_MAX_LENGTH,
        )
        self.add_item(
            discord.ui.Label(
                text="Enter the event description",
                component=self.description_input,
            )
        )
        self.channel = discord.ui.ChannelSelect["EventDetailsModal"](
            channel_types=[discord.ChannelType.text],
            required=True,
        )
        self.add_item(
            discord.ui.Label(
                text="What channel should your event be posted in?",
                component=self.channel,
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        self._draft.category = EventCategory(self.category.values[0])
        self._draft.title = self.title_input.value.strip()
        self._draft.description = self.description_input.value.strip()
        self._draft.channel_id = self.channel.values[0].id
        LOGGER.debug(
            "Event details step submitted; user_id=%s category=%s "
            "title_characters=%s description_characters=%s",
            interaction.user.id,
            self._draft.category.value,
            len(self._draft.title),
            len(self._draft.description),
        )
        await interaction.response.send_message(
            "**Step 2 of 3** — press Continue to enter the event schedule.",
            view=ContinueToScheduleView(self._bot, self._draft),
            ephemeral=True,
        )


class EventScheduleModal(discord.ui.Modal, title="Create new event"):
    def __init__(self, bot: Gw2Bot, draft: EventDraft):
        super().__init__()
        self._bot = bot
        self._draft = draft
        self.start_input = discord.ui.TextInput["EventScheduleModal"](
            placeholder=EVENT_DATETIME_PLACEHOLDER,
            default=draft.start_text or None,
            max_length=16,
        )
        self.add_item(
            discord.ui.Label(
                text=f"When will your event be? ({EVENT_DATETIME_PLACEHOLDER})",
                component=self.start_input,
            )
        )
        self.duration_input = discord.ui.TextInput["EventScheduleModal"](
            placeholder="HH:mm",
            default=draft.duration_text or None,
            max_length=6,
        )
        self.add_item(
            discord.ui.Label(
                text="How long will your event be? (HH:mm)",
                component=self.duration_input,
            )
        )
        repeats = (
            None
            if not draft.start_text
            else draft.repeat_frequency is not RepeatFrequency.NONE
        )
        self.repeat = discord.ui.Select["EventScheduleModal"](
            options=_yes_no_options(repeats),
        )
        self.add_item(
            discord.ui.Label(
                text="Would you like this event to repeat?",
                component=self.repeat,
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        self._draft.start_text = self.start_input.value.strip()
        self._draft.duration_text = self.duration_input.value.strip()
        repeats = self.repeat.values[0] == "yes"
        try:
            start_time = parse_event_datetime(
                self._draft.start_text,
                self._bot.event_timezone,
            )
            if start_time <= datetime.now(UTC):
                raise ValueError("The event start must be in the future.")
            duration_minutes = parse_event_duration(self._draft.duration_text)
        except ValueError as error:
            await _send_validation_error(
                interaction,
                error,
                RetryScheduleView(self._bot, self._draft),
            )
            return
        self._draft.start_time = start_time
        self._draft.duration_minutes = duration_minutes
        LOGGER.debug(
            "Event schedule step submitted; user_id=%s repeats=%s "
            "duration_minutes=%s",
            interaction.user.id,
            repeats,
            duration_minutes,
        )
        if not repeats:
            self._draft.repeat_frequency = RepeatFrequency.NONE
            self._draft.repeat_days = ()
            self._draft.repeat_days_text = ""
            await send_event_preview(self._bot, interaction, self._draft)
            return
        if self._draft.repeat_frequency is RepeatFrequency.NONE:
            self._draft.repeat_frequency = RepeatFrequency.DAILY
        message = "**Step 3 of 3** — press Continue to set how it repeats."
        view = ContinueToRepeatView(self._bot, self._draft)
        if _is_ephemeral_component_interaction(interaction):
            await interaction.response.edit_message(
                content=message,
                embeds=[],
                view=view,
            )
        else:
            await interaction.response.send_message(
                message,
                view=view,
                ephemeral=True,
            )


class EventRepeatModal(discord.ui.Modal, title="Create new event"):
    def __init__(self, bot: Gw2Bot, draft: EventDraft):
        super().__init__()
        self._bot = bot
        self._draft = draft
        self.frequency = discord.ui.Select["EventRepeatModal"](
            options=_frequency_options(draft.repeat_frequency),
        )
        self.add_item(
            discord.ui.Label(
                text="How often?",
                component=self.frequency,
            )
        )
        self.days_input = discord.ui.TextInput["EventRepeatModal"](
            required=False,
            default=draft.repeat_days_text or None,
            placeholder="Weekly: Sunday, Wednesday — Monthly: 1, 15, 30",
            max_length=120,
        )
        self.add_item(
            discord.ui.Label(
                text="What day(s)?",
                description=(
                    "Weekly: day names. Monthly: 1-31. Daily: leave blank."
                ),
                component=self.days_input,
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        frequency = RepeatFrequency(self.frequency.values[0])
        self._draft.repeat_frequency = frequency
        self._draft.repeat_days_text = self.days_input.value.strip()
        try:
            repeat_days = parse_repeat_days(
                frequency,
                self._draft.repeat_days_text,
            )
        except ValueError as error:
            await _send_validation_error(
                interaction,
                error,
                RetryRepeatView(self._bot, self._draft),
            )
            return
        self._draft.repeat_days = repeat_days
        LOGGER.debug(
            "Event repeat step submitted; user_id=%s frequency=%s days=%s",
            interaction.user.id,
            frequency.value,
            len(repeat_days),
        )
        await send_event_preview(self._bot, interaction, self._draft)


class EventConfirmView(discord.ui.View):
    def __init__(self, bot: Gw2Bot, draft: EventDraft):
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
        button: discord.ui.Button[EventConfirmView],
    ) -> None:
        await interaction.response.send_message(
            "What would you like to change?",
            view=ChangeFieldView(self._bot, self._draft),
            ephemeral=True,
        )

    @discord.ui.button(label="Post event", style=discord.ButtonStyle.success)
    async def post_event(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[EventConfirmView],
    ) -> None:
        from gw2bot.events.posting import post_occurrence

        if self._draft.posted:
            await interaction.response.send_message(
                "This event was already posted.",
                ephemeral=True,
            )
            return
        if not self._draft.is_complete():
            await interaction.response.send_message(
                "The event is missing required details. Use "
                "**Change something** to fill them in.",
                ephemeral=True,
            )
            return
        start_time = self._draft.start_time
        if start_time is not None and start_time <= datetime.now(UTC):
            await interaction.response.send_message(
                "The event start is no longer in the future. Use "
                "**Change something** to update the date and time.",
                ephemeral=True,
            )
            return
        self._draft.posted = True
        await interaction.response.edit_message(view=None)
        draft_event = self._draft.to_event()
        try:
            event = self._bot.event_store.create_event(
                category=draft_event.category,
                title=draft_event.title,
                description=draft_event.description,
                channel_id=draft_event.channel_id,
                leader_discord_id=draft_event.leader_discord_id,
                start_time=draft_event.start_time,
                duration_minutes=draft_event.duration_minutes,
                repeat_frequency=draft_event.repeat_frequency,
                repeat_days=draft_event.repeat_days,
            )
            occurrence = self._bot.event_store.create_occurrence(
                event.event_id,
                event.start_time,
            )
        except SQLAlchemyError as exc:
            self._draft.posted = False
            LOGGER.error(
                "Could not store event; user_id=%s error_type=%s",
                interaction.user.id,
                type(exc).__name__,
            )
            await interaction.followup.send(
                "The event could not be saved. Try again later.",
                ephemeral=True,
            )
            return
        try:
            await post_occurrence(self._bot, event, occurrence)
        except (discord.HTTPException, SQLAlchemyError) as exc:
            self._draft.posted = False
            LOGGER.error(
                "Could not post event; user_id=%s error_type=%s",
                interaction.user.id,
                type(exc).__name__,
            )
            # Remove the stored rows so retrying cannot create duplicate
            # events and the scheduler cannot resurrect this occurrence.
            try:
                self._bot.event_store.delete_event(event.event_id)
            except SQLAlchemyError as cleanup_exc:
                LOGGER.error(
                    "Could not clean up unposted event; event_id=%s "
                    "error_type=%s",
                    event.event_id,
                    type(cleanup_exc).__name__,
                )
            await interaction.followup.send(
                "The event could not be posted to the selected channel. "
                "Check the bot's permissions there and try again.",
                ephemeral=True,
            )
            return
        LOGGER.debug(
            "Event posted from preview; event_id=%s occurrence_id=%s "
            "user_id=%s",
            event.event_id,
            occurrence.occurrence_id,
            interaction.user.id,
        )
        await interaction.followup.send(
            f"Event **{event.event_id}** was posted in "
            f"<#{event.channel_id}>.",
            ephemeral=True,
        )


_CHANGE_FIELDS = (
    ("category", "Category"),
    ("title", "Title"),
    ("description", "Description"),
    ("channel", "Channel"),
    ("start", "Date & time"),
    ("duration", "Duration"),
    ("repeat", "Repeat settings"),
    ("leader", "Leader"),
)


class ChangeFieldSelect(discord.ui.Select["ChangeFieldView"]):
    def __init__(self):
        super().__init__(
            placeholder="What would you like to change?",
            options=[
                discord.SelectOption(label=label, value=value)
                for value, label in _CHANGE_FIELDS
            ],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if view is not None:
            await view.handle_choice(interaction, self.values[0])


class ChangeFieldView(discord.ui.View):
    def __init__(self, bot: Gw2Bot, draft: EventDraft):
        super().__init__(timeout=FLOW_TIMEOUT_SECONDS)
        self._bot = bot
        self._draft = draft
        self.add_item(ChangeFieldSelect())

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
        if choice in ("title", "description", "start", "duration"):
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
                content="What channel should your event be posted in?",
                view=ChannelPickView(self._bot, self._draft),
            )
            return
        if choice == "leader":
            await interaction.response.edit_message(
                content="Who should lead this event?",
                view=LeaderPickView(self._bot, self._draft),
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
            discord.ui.Label(text=label, component=self.field_input)
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
        super().__init__(channel_types=[discord.ChannelType.text])

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if view is not None:
            await view.pick(interaction, self.values[0].id)


class ChannelPickView(discord.ui.View):
    def __init__(self, bot: Gw2Bot, draft: EventDraft):
        super().__init__(timeout=FLOW_TIMEOUT_SECONDS)
        self._bot = bot
        self._draft = draft
        self.add_item(ChannelPickSelect())

    async def pick(
        self,
        interaction: discord.Interaction,
        channel_id: int,
    ) -> None:
        self._draft.channel_id = channel_id
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
        if not user_has_role(user, EVENT_CREATE_ROLE_ID):
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


class RepeatChoiceView(discord.ui.View):
    def __init__(self, bot: Gw2Bot, draft: EventDraft):
        super().__init__(timeout=FLOW_TIMEOUT_SECONDS)
        self._bot = bot
        self._draft = draft

    @discord.ui.button(label="Yes", style=discord.ButtonStyle.success)
    async def repeat_yes(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[RepeatChoiceView],
    ) -> None:
        if self._draft.repeat_frequency is RepeatFrequency.NONE:
            self._draft.repeat_frequency = RepeatFrequency.DAILY
        await interaction.response.send_modal(
            EventRepeatModal(self._bot, self._draft)
        )

    @discord.ui.button(label="No", style=discord.ButtonStyle.secondary)
    async def repeat_no(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[RepeatChoiceView],
    ) -> None:
        self._draft.repeat_frequency = RepeatFrequency.NONE
        self._draft.repeat_days = ()
        self._draft.repeat_days_text = ""
        await send_event_preview(self._bot, interaction, self._draft)


def build_signup_view(occurrence_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(EventSignUpButton(occurrence_id))
    view.add_item(EventSignOutButton(occurrence_id))
    view.add_item(EventSettingsButton(occurrence_id))
    return view


async def _load_event_context(
    bot: Gw2Bot,
    interaction: discord.Interaction,
    occurrence_id: int,
) -> tuple[Event, EventOccurrence] | None:
    occurrence = bot.event_store.get_occurrence(occurrence_id)
    event = (
        bot.event_store.get_event(occurrence.event_id)
        if occurrence is not None
        else None
    )
    if occurrence is None or event is None:
        LOGGER.debug(
            "Event interaction referenced a missing occurrence; "
            "occurrence_id=%s",
            occurrence_id,
        )
        await interaction.response.send_message(
            "This event is no longer available.",
            ephemeral=True,
        )
        return None
    return event, occurrence


class EventSignUpButton(
    discord.ui.DynamicItem[discord.ui.Button[Any]],
    template=r"gw2bot:event-signup:(?P<occurrence_id>[0-9]+)",
):
    def __init__(self, occurrence_id: int):
        self.occurrence_id = occurrence_id
        super().__init__(
            discord.ui.Button(
                label="Sign up",
                style=discord.ButtonStyle.success,
                custom_id=f"gw2bot:event-signup:{occurrence_id}",
            )
        )

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Item[Any],
        match: re.Match[str],
        /,
    ) -> EventSignUpButton:
        return cls(int(match["occurrence_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        bot = cast("Gw2Bot", interaction.client)
        await start_signup_flow(bot, interaction, self.occurrence_id)


class EventSignOutButton(
    discord.ui.DynamicItem[discord.ui.Button[Any]],
    template=r"gw2bot:event-signout:(?P<occurrence_id>[0-9]+)",
):
    def __init__(self, occurrence_id: int):
        self.occurrence_id = occurrence_id
        super().__init__(
            discord.ui.Button(
                label="Sign out",
                style=discord.ButtonStyle.secondary,
                custom_id=f"gw2bot:event-signout:{occurrence_id}",
            )
        )

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Item[Any],
        match: re.Match[str],
        /,
    ) -> EventSignOutButton:
        return cls(int(match["occurrence_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        bot = cast("Gw2Bot", interaction.client)
        context = await _load_event_context(
            bot,
            interaction,
            self.occurrence_id,
        )
        if context is None:
            return
        event, occurrence = context
        signup = bot.event_store.get_signup(
            occurrence.occurrence_id,
            interaction.user.id,
        )
        if signup is None:
            LOGGER.debug(
                "Sign out pressed without a signup; occurrence_id=%s "
                "user_id=%s",
                occurrence.occurrence_id,
                interaction.user.id,
            )
            await interaction.response.send_message(
                "You are not signed up for the event.",
                view=SignUpOfferView(bot, occurrence.occurrence_id),
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            "Would you like to be removed from this event?",
            view=SignOutConfirmView(bot, event, occurrence),
            ephemeral=True,
        )


class EventSettingsButton(
    discord.ui.DynamicItem[discord.ui.Button[Any]],
    template=r"gw2bot:event-settings:(?P<occurrence_id>[0-9]+)",
):
    def __init__(self, occurrence_id: int):
        self.occurrence_id = occurrence_id
        super().__init__(
            discord.ui.Button(
                label="⚙️",
                style=discord.ButtonStyle.secondary,
                custom_id=f"gw2bot:event-settings:{occurrence_id}",
            )
        )

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Item[Any],
        match: re.Match[str],
        /,
    ) -> EventSettingsButton:
        return cls(int(match["occurrence_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        bot = cast("Gw2Bot", interaction.client)
        context = await _load_event_context(
            bot,
            interaction,
            self.occurrence_id,
        )
        if context is None:
            return
        event, occurrence = context
        await interaction.response.send_message(
            _describe_signup_settings(bot, event, interaction.user.id),
            view=SignupSettingsView(bot, event, occurrence),
            ephemeral=True,
        )


def _describe_signup_settings(
    bot: Gw2Bot,
    event: Event,
    discord_user_id: int,
) -> str:
    lines = ["**Your sign-up settings**"]
    if event.repeat_frequency is not RepeatFrequency.NONE:
        auto = bot.event_store.get_auto_signup(
            event.event_id,
            discord_user_id,
        )
        if auto is not None and auto.choice is AutoSignupChoice.YES:
            auto_text = "enabled"
        elif auto is not None and auto.choice is AutoSignupChoice.NEVER_ASK:
            auto_text = "disabled (never ask again)"
        else:
            auto_text = "disabled"
        lines.append(f"Automatic sign-up for this event: **{auto_text}**")
    else:
        lines.append("This event does not repeat, so it has no automatic sign-up.")
    preference = bot.event_store.get_signup_preference(discord_user_id)
    if preference is not None and preference.mode is PreferenceMode.REMEMBER:
        remembered = (
            preference.role.value if preference.role is not None else "none"
        )
        lines.append(f"Remembered role: **{remembered}**")
    elif preference is not None and preference.mode is PreferenceMode.NEVER_ASK:
        lines.append("Role memory: **never ask**")
    else:
        lines.append("Role memory: **ask every time**")
    return "\n".join(lines)


class _SignupSettingsButton(discord.ui.Button["SignupSettingsView"]):
    def __init__(self, label: str, style: discord.ButtonStyle, action: str):
        super().__init__(label=label, style=style)
        self._action = action

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if view is None:
            return
        if self._action == "enable_auto":
            await view._enable_auto(interaction)
        elif self._action == "disable_auto":
            await view._disable_auto(interaction)
        else:
            await view._reset_preference(interaction)


class SignupSettingsView(discord.ui.View):
    def __init__(self, bot: Gw2Bot, event: Event, occurrence: EventOccurrence):
        super().__init__(timeout=FLOW_TIMEOUT_SECONDS)
        self._bot = bot
        self._event = event
        self._occurrence = occurrence
        if event.repeat_frequency is not RepeatFrequency.NONE:
            self.add_item(
                _SignupSettingsButton(
                    "Enable auto sign-up",
                    discord.ButtonStyle.success,
                    "enable_auto",
                )
            )
            self.add_item(
                _SignupSettingsButton(
                    "Disable auto sign-up",
                    discord.ButtonStyle.secondary,
                    "disable_auto",
                )
            )
        self.add_item(
            _SignupSettingsButton(
                "Reset role memory",
                discord.ButtonStyle.secondary,
                "reset_preference",
            )
        )

    async def _enable_auto(self, interaction: discord.Interaction) -> None:
        signup = self._bot.event_store.get_signup(
            self._occurrence.occurrence_id,
            interaction.user.id,
        )
        preference = self._bot.event_store.get_signup_preference(
            interaction.user.id
        )
        role: EventRole | None = None
        flex_roles: tuple[EventRole, ...] = ()
        if signup is not None:
            role = signup.role
            flex_roles = signup.flex_roles
        elif preference is not None:
            role = preference.role
            flex_roles = preference.flex_roles
        if self._event.capacity.has_roles and role is None:
            await interaction.response.edit_message(
                content=(
                    "Sign up once with a role first so automatic sign-up "
                    "knows what to sign you up as."
                ),
                view=self,
            )
            return
        self._bot.event_store.set_auto_signup(
            self._event.event_id,
            interaction.user.id,
            AutoSignupChoice.YES,
            role,
            flex_roles,
        )
        await interaction.response.edit_message(
            content=_describe_signup_settings(
                self._bot,
                self._event,
                interaction.user.id,
            ),
            view=self,
        )

    async def _disable_auto(self, interaction: discord.Interaction) -> None:
        self._bot.event_store.set_auto_signup(
            self._event.event_id,
            interaction.user.id,
            AutoSignupChoice.NO,
            None,
            (),
        )
        await interaction.response.edit_message(
            content=_describe_signup_settings(
                self._bot,
                self._event,
                interaction.user.id,
            ),
            view=self,
        )

    async def _reset_preference(
        self,
        interaction: discord.Interaction,
    ) -> None:
        self._bot.event_store.set_signup_preference(
            interaction.user.id,
            None,
            (),
            PreferenceMode.ASK,
        )
        await interaction.response.edit_message(
            content=_describe_signup_settings(
                self._bot,
                self._event,
                interaction.user.id,
            ),
            view=self,
        )


class SignUpOfferButton(discord.ui.Button["SignUpOfferView"]):
    def __init__(self):
        super().__init__(label="Sign up", style=discord.ButtonStyle.success)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if view is not None:
            await start_signup_flow(
                view.bot,
                interaction,
                view.occurrence_id,
            )


class SignUpOfferView(discord.ui.View):
    def __init__(self, bot: Gw2Bot, occurrence_id: int):
        super().__init__(timeout=FLOW_TIMEOUT_SECONDS)
        self.bot = bot
        self.occurrence_id = occurrence_id
        self.add_item(SignUpOfferButton())


class SignOutConfirmView(discord.ui.View):
    def __init__(self, bot: Gw2Bot, event: Event, occurrence: EventOccurrence):
        super().__init__(timeout=FLOW_TIMEOUT_SECONDS)
        self._bot = bot
        self._event = event
        self._occurrence = occurrence

    @discord.ui.button(label="Remove me", style=discord.ButtonStyle.danger)
    async def remove_me(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[SignOutConfirmView],
    ) -> None:
        from gw2bot.events.posting import remove_signup

        await interaction.response.edit_message(
            content="Removing you from the event…",
            view=None,
        )
        removed, promoted = await remove_signup(
            self._bot,
            self._event,
            self._occurrence,
            interaction.user.id,
        )
        if removed is None:
            content = "You were not signed up for the event."
        else:
            content = "You were removed from the event."
        LOGGER.debug(
            "Sign out completed; occurrence_id=%s user_id=%s removed=%s "
            "promoted_user=%s",
            self._occurrence.occurrence_id,
            interaction.user.id,
            removed is not None,
            promoted.discord_user_id if promoted is not None else None,
        )
        await interaction.edit_original_response(content=content, view=None)

    @discord.ui.button(label="Keep me signed up", style=discord.ButtonStyle.secondary)
    async def keep_me(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[SignOutConfirmView],
    ) -> None:
        await interaction.response.edit_message(
            content="You are still signed up for the event.",
            view=None,
        )


async def start_signup_flow(
    bot: Gw2Bot,
    interaction: discord.Interaction,
    occurrence_id: int,
) -> None:
    context = await _load_event_context(bot, interaction, occurrence_id)
    if context is None:
        return
    event, occurrence = context
    signups = bot.event_store.get_signups(occurrence.occurrence_id)
    now = datetime.now(UTC)
    if occurrence.start_time.timestamp() + event.duration_minutes * 60 <= (
        now.timestamp()
    ):
        await interaction.response.send_message(
            "This event is already over.",
            ephemeral=True,
        )
        return
    if any(
        signup.discord_user_id == interaction.user.id for signup in signups
    ):
        await interaction.response.send_message(
            "You are already signed up for this event.",
            ephemeral=True,
        )
        return
    LOGGER.debug(
        "Starting event signup flow; occurrence_id=%s user_id=%s "
        "category=%s",
        occurrence.occurrence_id,
        interaction.user.id,
        event.category.value,
    )
    flow = SignupFlow(bot, event, occurrence, interaction.user.id)
    if not event.capacity.has_roles:
        await flow.finalize(interaction)
        return
    preference = bot.event_store.get_signup_preference(interaction.user.id)
    if (
        preference is not None
        and preference.mode is PreferenceMode.REMEMBER
        and preference.role is not None
    ):
        flow.role = preference.role
        flow.flex_roles = tuple(
            role for role in preference.flex_roles if role != preference.role
        )
        flow.skip_remember_prompt = True
        await flow.finalize(interaction)
        return
    if preference is not None and preference.mode is PreferenceMode.NEVER_ASK:
        flow.skip_remember_prompt = True
    await interaction.response.send_message(
        "Pick your role for this event.",
        view=RolePickView(flow),
        ephemeral=True,
    )


class SignupFlow:
    def __init__(
        self,
        bot: Gw2Bot,
        event: Event,
        occurrence: EventOccurrence,
        discord_user_id: int,
    ):
        self.bot = bot
        self.event = event
        self.occurrence = occurrence
        self.discord_user_id = discord_user_id
        self.role: EventRole | None = None
        self.flex_roles: tuple[EventRole, ...] = ()
        self.skip_remember_prompt = False

    async def continue_after_roles(
        self,
        interaction: discord.Interaction,
    ) -> None:
        if self.skip_remember_prompt:
            await self.finalize(interaction)
            return
        await interaction.response.edit_message(
            content=(
                "Would you like to remember your selection for future "
                "events?"
            ),
            view=RememberChoiceView(self),
        )

    async def finalize(self, interaction: discord.Interaction) -> None:
        from gw2bot.events.posting import complete_signup

        if interaction.response.is_done():
            edit = interaction.edit_original_response
        elif _is_ephemeral_component_interaction(interaction):
            await interaction.response.edit_message(
                content="Signing you up…",
                view=None,
            )
            edit = interaction.edit_original_response
        else:
            await interaction.response.defer(ephemeral=True, thinking=True)
            edit = interaction.edit_original_response
        try:
            signup = await complete_signup(
                self.bot,
                self.event,
                self.occurrence,
                self.discord_user_id,
                self.role,
                self.flex_roles,
            )
        except ValueError as error:
            await edit(content=str(error), view=None)
            return
        content = _signup_summary(signup)
        auto = self.bot.event_store.get_auto_signup(
            self.event.event_id,
            self.discord_user_id,
        )
        # A plain "No" only declines for now; just "Yes" and "No, never
        # ask again" persist across future manual signups.
        if self.event.repeat_frequency is not RepeatFrequency.NONE and (
            auto is None or auto.choice is AutoSignupChoice.NO
        ):
            await edit(
                content=(
                    f"{content}\n\nWould you like to sign up for this "
                    "event automatically in the future?"
                ),
                view=AutoSignupChoiceView(self),
            )
            return
        await edit(content=content, view=None)


def _signup_summary(signup: EventSignup) -> str:
    if signup.waitlisted:
        return (
            "The event is currently full, so you were added to the "
            "**waitlist**."
        )
    if signup.assigned_role is not None:
        summary = f"You signed up as **{signup.assigned_role.value}**."
        if (
            signup.role is not None
            and signup.assigned_role != signup.role
        ):
            summary += (
                f" Your preferred role **{signup.role.value}** was full, "
                "so one of your flex roles was used."
            )
        return summary
    return "You signed up for the event."


class RolePickSelect(discord.ui.Select["RolePickView"]):
    def __init__(self, flow: SignupFlow):
        signups = flow.bot.event_store.get_signups(
            flow.occurrence.occurrence_id
        )
        available = fitting_roles(flow.event.capacity, signups)
        waitlist_only = not available
        roles = list(EventRole) if waitlist_only else available
        options = [
            discord.SelectOption(
                label=(
                    f"{role.value} (waitlist)" if waitlist_only else role.value
                ),
                value=role.value,
            )
            for role in roles
        ]
        super().__init__(placeholder="Pick your role", options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if view is not None:
            await view.pick(interaction, EventRole(self.values[0]))


class RolePickView(discord.ui.View):
    def __init__(self, flow: SignupFlow):
        super().__init__(timeout=FLOW_TIMEOUT_SECONDS)
        self._flow = flow
        self.add_item(RolePickSelect(flow))

    async def pick(
        self,
        interaction: discord.Interaction,
        role: EventRole,
    ) -> None:
        self._flow.role = role
        LOGGER.debug(
            "Event signup role picked; occurrence_id=%s user_id=%s role=%s",
            self._flow.occurrence.occurrence_id,
            self._flow.discord_user_id,
            role.value,
        )
        await interaction.response.edit_message(
            content="Select flex roles",
            view=FlexRolesView(self._flow),
        )


class FlexRolesSelect(discord.ui.Select["FlexRolesView"]):
    def __init__(self, flow: SignupFlow):
        options = [
            discord.SelectOption(label=role.value, value=role.value)
            for role in EventRole
            if role != flow.role
        ]
        super().__init__(
            placeholder="Pick any flex roles",
            options=options,
            min_values=1,
            max_values=len(options),
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if view is not None:
            await view.pick(
                interaction,
                tuple(EventRole(value) for value in self.values),
            )


class FlexRolesView(discord.ui.View):
    def __init__(self, flow: SignupFlow):
        super().__init__(timeout=FLOW_TIMEOUT_SECONDS)
        self._flow = flow
        self.add_item(FlexRolesSelect(flow))

    async def pick(
        self,
        interaction: discord.Interaction,
        flex_roles: tuple[EventRole, ...],
    ) -> None:
        self._flow.flex_roles = flex_roles
        LOGGER.debug(
            "Event signup flex roles picked; occurrence_id=%s user_id=%s "
            "flex_count=%s",
            self._flow.occurrence.occurrence_id,
            self._flow.discord_user_id,
            len(flex_roles),
        )
        await self._flow.continue_after_roles(interaction)

    @discord.ui.button(
        label="Skip selecting flex roles",
        style=discord.ButtonStyle.secondary,
    )
    async def skip(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[FlexRolesView],
    ) -> None:
        self._flow.flex_roles = ()
        await self._flow.continue_after_roles(interaction)


class RememberChoiceView(discord.ui.View):
    def __init__(self, flow: SignupFlow):
        super().__init__(timeout=FLOW_TIMEOUT_SECONDS)
        self._flow = flow

    @discord.ui.button(label="Yes", style=discord.ButtonStyle.success)
    async def remember_yes(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[RememberChoiceView],
    ) -> None:
        self._flow.bot.event_store.set_signup_preference(
            self._flow.discord_user_id,
            self._flow.role,
            self._flow.flex_roles,
            PreferenceMode.REMEMBER,
        )
        await self._flow.finalize(interaction)

    @discord.ui.button(label="No", style=discord.ButtonStyle.secondary)
    async def remember_no(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[RememberChoiceView],
    ) -> None:
        self._flow.bot.event_store.set_signup_preference(
            self._flow.discord_user_id,
            None,
            (),
            PreferenceMode.ASK,
        )
        await self._flow.finalize(interaction)

    @discord.ui.button(
        label="No, never ask again",
        style=discord.ButtonStyle.secondary,
    )
    async def remember_never(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[RememberChoiceView],
    ) -> None:
        self._flow.bot.event_store.set_signup_preference(
            self._flow.discord_user_id,
            None,
            (),
            PreferenceMode.NEVER_ASK,
        )
        await self._flow.finalize(interaction)


class AutoSignupChoiceView(discord.ui.View):
    def __init__(self, flow: SignupFlow):
        super().__init__(timeout=FLOW_TIMEOUT_SECONDS)
        self._flow = flow

    async def _store_choice(
        self,
        interaction: discord.Interaction,
        choice: AutoSignupChoice,
        confirmation: str,
    ) -> None:
        self._flow.bot.event_store.set_auto_signup(
            self._flow.event.event_id,
            self._flow.discord_user_id,
            choice,
            self._flow.role,
            self._flow.flex_roles,
        )
        await interaction.response.edit_message(
            content=confirmation,
            view=None,
        )

    @discord.ui.button(label="Yes", style=discord.ButtonStyle.success)
    async def auto_yes(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[AutoSignupChoiceView],
    ) -> None:
        await self._store_choice(
            interaction,
            AutoSignupChoice.YES,
            "You will be signed up automatically for future occurrences "
            "of this event.",
        )

    @discord.ui.button(label="No", style=discord.ButtonStyle.secondary)
    async def auto_no(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[AutoSignupChoiceView],
    ) -> None:
        await self._store_choice(
            interaction,
            AutoSignupChoice.NO,
            "You will not be signed up automatically for this event.",
        )

    @discord.ui.button(
        label="No, never ask again for this event",
        style=discord.ButtonStyle.secondary,
    )
    async def auto_never(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[AutoSignupChoiceView],
    ) -> None:
        await self._store_choice(
            interaction,
            AutoSignupChoice.NEVER_ASK,
            "You will not be asked about automatic sign-up for this "
            "event again.",
        )
