from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast
from zoneinfo import ZoneInfo

import discord
from sqlalchemy.exc import SQLAlchemyError

from gw2bot.discord_utils import user_has_role
from gw2bot.events.formatting import (
    EVENT_DATETIME_PLACEHOLDER,
    confirm_embed,
    describe_repeat,
    edit_confirm_embed,
    event_embed,
    format_duration_input,
    format_event_datetime,
    format_repeat_days,
    parse_event_datetime,
    parse_event_duration,
    parse_repeat_days,
)
from gw2bot.events.models import (
    AutoSignupChoice,
    CATEGORY_EMOJI,
    Event,
    EventCategory,
    EventOccurrence,
    EventRole,
    EventSignup,
    EventStatus,
    PreferenceMode,
    ROLE_EMOJI,
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

# An event whose occurrence has started is live and cannot be edited: re-rendering
# it can persist OVER without seeding a recurring series' next occurrence, and its
# roster is already in play. Deleting is the only remaining action.
ONGOING_EDIT_REJECTION = (
    "That event has already started, so it can no longer be edited. "
    "Use `/event delete` to remove it."
)
PREVIEW_EVENT_ID_TEXT = "—"

# Discord's hard cap on how many users one select may return.
REMOVE_SELECT_MAX_VALUES = 25


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
    delete_previous_on_repeat: bool = False
    posted: bool = False
    # Set when the draft edits an existing event rather than creating one. The
    # whole "Change something" flow reuses this draft, so a single flag steers
    # every editor back to the edit preview and Save-changes path.
    editing_event_id: int | None = None
    # Guards the Save-changes / Move-event terminal actions against a double
    # click that would otherwise apply the edit (or re-post) twice.
    edit_applied: bool = False

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
            delete_previous_on_repeat=self.delete_previous_on_repeat,
        )


def draft_from_event(
    event: Event,
    timezone: ZoneInfo,
    *,
    start_time_override: datetime | None = None,
) -> EventDraft:
    # Pre-fill the text mirror fields so the reused edit modals show the current
    # values (start/duration/repeat are re-parsed from these strings). For a
    # recurring event the live occurrence's start diverges from the series
    # origin (event.start_time), so callers pass that occurrence's start: the
    # preview then shows the date the commander sees, and leaving it unchanged
    # does not spuriously reschedule the occurrence back to the series origin.
    start_time = (
        start_time_override
        if start_time_override is not None
        else event.start_time
    )
    return EventDraft(
        leader_discord_id=event.leader_discord_id,
        category=event.category,
        title=event.title,
        description=event.description,
        channel_id=event.channel_id,
        start_time=start_time,
        start_text=format_event_datetime(start_time, timezone),
        duration_minutes=event.duration_minutes,
        duration_text=format_duration_input(event.duration_minutes),
        repeat_frequency=event.repeat_frequency,
        repeat_days=event.repeat_days,
        repeat_days_text=format_repeat_days(
            event.repeat_frequency, event.repeat_days
        ),
        delete_previous_on_repeat=event.delete_previous_on_repeat,
        editing_event_id=event.event_id,
    )


def _category_options(
    selected: EventCategory | None,
) -> list[discord.SelectOption]:
    return [
        discord.SelectOption(
            label=category.value,
            value=category.value,
            default=category is selected,
            emoji=CATEGORY_EMOJI[category],
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


def _live_occurrences(
    bot: Gw2Bot,
    event_id: int,
) -> list[EventOccurrence]:
    # get_event_occurrences is ordered by start_time, so the result stays in
    # chronological order for callers that want the soonest.
    return [
        occurrence
        for occurrence in bot.event_store.get_event_occurrences(event_id)
        if occurrence.status is not EventStatus.OVER
    ]


def _primary_live_occurrence(
    bot: Gw2Bot,
    event_id: int,
) -> EventOccurrence | None:
    # The soonest non-OVER occurrence is the one the commander is editing: it is
    # what the preview mirrors and what a date change reschedules. It may still
    # be unposted (a recurring series' next occurrence), in which case a
    # reschedule still applies and the scheduler posts it later.
    live = _live_occurrences(bot, event_id)
    return live[0] if live else None


def build_event_preview(
    bot: Gw2Bot,
    draft: EventDraft,
    *,
    primary: EventOccurrence | None = None,
) -> tuple[list[discord.Embed], discord.ui.View]:
    # Split out from send_event_preview so a flow that has already answered the
    # interaction (the roster removal below awaits Discord I/O first) can still
    # re-render the same preview through edit_original_response.
    editing_event_id = draft.editing_event_id
    view: discord.ui.View
    if editing_event_id is not None:
        # Show the live roster so the preview mirrors the posted message, but
        # render the pending date/time from the draft (to_event uses the draft's
        # start_time), not the occurrence's stored time. The initial /event edit
        # call passes the occurrence it already fetched; change-flow re-renders
        # do not have it, so look it up.
        occurrence = (
            primary
            if primary is not None
            else _primary_live_occurrence(bot, editing_event_id)
        )
        signups = (
            bot.event_store.get_signups(occurrence.occurrence_id)
            if occurrence is not None
            else []
        )
        preview = event_embed(
            draft.to_event(editing_event_id),
            signups,
            EventStatus.OPEN,
            event_id_text=str(editing_event_id),
        )
        confirmation = edit_confirm_embed()
        view = EventEditConfirmView(bot, draft)
    else:
        preview = event_embed(
            draft.to_event(),
            [],
            EventStatus.OPEN,
            event_id_text=PREVIEW_EVENT_ID_TEXT,
        )
        confirmation = confirm_embed()
        view = EventConfirmView(bot, draft)
    repeat_text = describe_repeat(draft.repeat_frequency, draft.repeat_days)
    if (
        draft.repeat_frequency is not RepeatFrequency.NONE
        and draft.delete_previous_on_repeat
    ):
        repeat_text += ", removing the previous post each time"
    confirmation.description = (
        f"{confirmation.description}\n\n*{repeat_text}.*"
    )
    return [preview, confirmation], view


async def send_event_preview(
    bot: Gw2Bot,
    interaction: discord.Interaction,
    draft: EventDraft,
    *,
    primary: EventOccurrence | None = None,
) -> None:
    embeds, view = build_event_preview(bot, draft, primary=primary)
    LOGGER.debug(
        "Sending event preview; user_id=%s category=%s repeat=%s "
        "title_characters=%s in_place=%s editing=%s",
        draft.leader_discord_id,
        draft.category.value if draft.category is not None else None,
        draft.repeat_frequency.value,
        len(draft.title),
        _is_ephemeral_component_interaction(interaction),
        draft.editing_event_id is not None,
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
            self._draft.delete_previous_on_repeat = False
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
        self.delete_previous = discord.ui.Select["EventRepeatModal"](
            options=_yes_no_options(draft.delete_previous_on_repeat),
        )
        self.add_item(
            discord.ui.Label(
                text="Delete the previous post on repeat?",
                description=(
                    "Keeps only the current occurrence in the channel."
                ),
                component=self.delete_previous,
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        frequency = RepeatFrequency(self.frequency.values[0])
        self._draft.repeat_frequency = frequency
        self._draft.repeat_days_text = self.days_input.value.strip()
        self._draft.delete_previous_on_repeat = (
            self.delete_previous.values[0] == "yes"
        )
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


class _PreviewConfirmView(discord.ui.View):
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
        button: discord.ui.Button[_PreviewConfirmView],
    ) -> None:
        await interaction.response.send_message(
            "What would you like to change?",
            view=ChangeFieldView(self._bot, self._draft),
            ephemeral=True,
        )


class EventConfirmView(_PreviewConfirmView):
    @discord.ui.button(label="Post event", style=discord.ButtonStyle.success)
    async def post_event(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[EventConfirmView],
    ) -> None:
        from gw2bot.events.posting import post_occurrence

        # The preview can sit open for minutes; the creator role may have
        # been revoked since /event new, so recheck before the irreversible
        # save/post path.
        if not user_has_role(interaction.user, EVENT_CREATE_ROLE_ID):
            LOGGER.warning(
                "Rejected event post from Discord user %s; required role %s",
                interaction.user.id,
                EVENT_CREATE_ROLE_ID,
            )
            await interaction.response.send_message(
                "You do not have the required role to create events.",
                ephemeral=True,
            )
            return
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
        event: Event | None = None
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
                delete_previous_on_repeat=(
                    draft_event.delete_previous_on_repeat
                ),
            )
            occurrence = self._bot.event_store.create_occurrence(
                event.event_id,
                event.start_time,
            )
        except SQLAlchemyError as exc:
            self._draft.posted = False
            await self._restore_post_controls(interaction)
            LOGGER.error(
                "Could not store event; user_id=%s error_type=%s",
                interaction.user.id,
                type(exc).__name__,
            )
            # If create_event committed before create_occurrence failed, the
            # event row is orphaned (no occurrence for the scheduler to post).
            # Remove it so retrying cannot leave duplicate, unpostable events.
            if event is not None:
                try:
                    self._bot.event_store.delete_event(event.event_id)
                except SQLAlchemyError as cleanup_exc:
                    LOGGER.error(
                        "Could not clean up partially stored event; "
                        "event_id=%s error_type=%s",
                        event.event_id,
                        type(cleanup_exc).__name__,
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
            await self._restore_post_controls(interaction)
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

    async def _restore_post_controls(
        self,
        interaction: discord.Interaction,
    ) -> None:
        # The preview buttons are removed before saving/posting; on failure
        # put them back so the user can retry from the same message instead
        # of restarting /event new. A failure here is logged but must not
        # mask the original error being reported to the user.
        try:
            await interaction.edit_original_response(view=self)
        except discord.HTTPException as exc:
            LOGGER.error(
                "Could not restore post controls; user_id=%s error_type=%s",
                interaction.user.id,
                type(exc).__name__,
            )


class EventEditConfirmView(_PreviewConfirmView):
    @discord.ui.button(label="Save changes", style=discord.ButtonStyle.success)
    async def save_changes(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[EventEditConfirmView],
    ) -> None:
        editing_event_id = self._draft.editing_event_id
        if editing_event_id is None:
            await interaction.response.send_message(
                "This edit session is no longer valid.",
                ephemeral=True,
            )
            return
        # The preview can sit open for minutes; recheck the role before the
        # save path, mirroring post_event.
        if not user_has_role(interaction.user, EVENT_CREATE_ROLE_ID):
            LOGGER.warning(
                "Rejected event edit save from Discord user %s; required "
                "role %s",
                interaction.user.id,
                EVENT_CREATE_ROLE_ID,
            )
            await interaction.response.send_message(
                "You do not have the required role to edit events.",
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
        stored = self._bot.event_store.get_event(editing_event_id)
        if stored is None:
            await interaction.response.send_message(
                "This event no longer exists.",
                ephemeral=True,
            )
            return
        channel_changed = stored.channel_id != self._draft.channel_id
        # Only a live occurrence that is actually posted has a message/thread to
        # delete and re-post; a channel change on an unposted event just retargets
        # where the scheduler posts it, so it needs no warning.
        has_posted_message = any(
            occurrence.message_id is not None
            for occurrence in _live_occurrences(self._bot, editing_event_id)
        )
        if channel_changed and has_posted_message:
            # Moving a posted event re-posts it, which deletes the current
            # message and its thread; confirm before doing anything.
            await interaction.response.edit_message(
                content=(
                    "Changing the channel will **delete the current event "
                    "message and its thread**, including every message posted "
                    "in that thread. The roster is kept and re-posted in the "
                    "new channel. Continue?"
                ),
                embeds=[],
                view=ChannelMoveConfirmView(
                    self._bot,
                    self._draft,
                    stored.channel_id,
                ),
            )
            return
        await apply_event_edit(
            self._bot,
            interaction,
            self._draft,
            stored.channel_id,
            repost=False,
        )

    @discord.ui.button(
        label="Remove sign-ups",
        style=discord.ButtonStyle.danger,
    )
    async def remove_signups(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[EventEditConfirmView],
    ) -> None:
        editing_event_id = self._draft.editing_event_id
        if editing_event_id is None:
            await interaction.response.send_message(
                "This edit session is no longer valid.",
                ephemeral=True,
            )
            return
        if not user_has_role(interaction.user, EVENT_CREATE_ROLE_ID):
            LOGGER.warning(
                "Rejected event roster removal from Discord user %s; required "
                "role %s",
                interaction.user.id,
                EVENT_CREATE_ROLE_ID,
            )
            await interaction.response.send_message(
                "You do not have the required role to edit events.",
                ephemeral=True,
            )
            return
        # The roster belongs to the occurrence, not the draft, so it is read
        # fresh: members can sign up or out while the preview sits open.
        occurrence = _primary_live_occurrence(self._bot, editing_event_id)
        signups = (
            self._bot.event_store.get_signups(occurrence.occurrence_id)
            if occurrence is not None
            else []
        )
        if occurrence is None or not signups:
            LOGGER.debug(
                "Roster removal opened with an empty roster; event_id=%s "
                "user_id=%s",
                editing_event_id,
                interaction.user.id,
            )
            await interaction.response.send_message(
                "Nobody is signed up for this event yet.",
                ephemeral=True,
            )
            return
        LOGGER.debug(
            "Opened roster removal; event_id=%s occurrence_id=%s user_id=%s "
            "roster=%s",
            editing_event_id,
            occurrence.occurrence_id,
            interaction.user.id,
            len(signups),
        )
        # Keep the roster embed on screen while the picker is open: the picker
        # is a guild-wide member search, not a list of the signed-up members,
        # so without the embed the commander would have to pick from memory.
        # Mirror how build_event_preview renders the editing preview so the two
        # views show the same roster.
        roster = event_embed(
            self._draft.to_event(editing_event_id),
            signups,
            EventStatus.OPEN,
            event_id_text=str(editing_event_id),
        )
        await interaction.response.edit_message(
            content=(
                "Pick the members to remove from this event's roster. The "
                "roster above lists everyone who is signed up."
            ),
            embeds=[roster],
            view=RemoveSignupsView(
                self._bot,
                self._draft,
                occurrence,
                len(signups),
            ),
        )


class RemoveSignupsSelect(discord.ui.UserSelect["RemoveSignupsView"]):
    def __init__(self, roster_size: int):
        # A member picker rather than a select built from the roster: the bot
        # runs without the members intent, so turning signup ids into names
        # would cost one Discord fetch per member, and a select is capped at 25
        # options while a WvW roster holds 50. Discord resolves the names
        # client-side instead; a pick that is not on the roster is refused
        # below.
        super().__init__(
            placeholder="Search for the members to remove",
            min_values=1,
            max_values=min(roster_size, REMOVE_SELECT_MAX_VALUES),
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if view is not None:
            await view.remove(interaction, list(self.values))


class RemoveSignupsView(discord.ui.View):
    def __init__(
        self,
        bot: Gw2Bot,
        draft: EventDraft,
        occurrence: EventOccurrence,
        roster_size: int,
    ):
        super().__init__(timeout=FLOW_TIMEOUT_SECONDS)
        self._bot = bot
        self._draft = draft
        self._occurrence = occurrence
        self.add_item(RemoveSignupsSelect(roster_size))

    @discord.ui.button(label="Back", style=discord.ButtonStyle.secondary)
    async def back(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[RemoveSignupsView],
    ) -> None:
        await send_event_preview(self._bot, interaction, self._draft)

    async def remove(
        self,
        interaction: discord.Interaction,
        users: list[discord.Member | discord.User],
    ) -> None:
        from gw2bot.events.posting import remove_signup

        editing_event_id = self._draft.editing_event_id
        # The picker can sit open for minutes, so re-check the role and re-read
        # the event and occurrence before mutating the roster.
        if not user_has_role(interaction.user, EVENT_CREATE_ROLE_ID):
            LOGGER.warning(
                "Rejected event roster removal from Discord user %s; required "
                "role %s",
                interaction.user.id,
                EVENT_CREATE_ROLE_ID,
            )
            await interaction.response.send_message(
                "You do not have the required role to edit events.",
                ephemeral=True,
            )
            return
        event = (
            self._bot.event_store.get_event(editing_event_id)
            if editing_event_id is not None
            else None
        )
        occurrence = self._bot.event_store.get_occurrence(
            self._occurrence.occurrence_id
        )
        if event is None or occurrence is None:
            await interaction.response.edit_message(
                content="This event no longer exists.",
                embeds=[],
                view=None,
            )
            return
        # An ended event's roster is history: removing from it would also
        # promote someone off the waitlist into a run that is already finished,
        # and re-rendering the message could persist OVER without seeding the
        # next occurrence of a recurring series. This mirrors the sign-out
        # button, which stays usable while an event is ongoing.
        if _occurrence_has_ended(event, occurrence, datetime.now(UTC)):
            LOGGER.debug(
                "Rejected roster removal for an ended event; occurrence_id=%s "
                "user_id=%s",
                occurrence.occurrence_id,
                interaction.user.id,
            )
            await interaction.response.edit_message(
                content=(
                    "This event has already ended, so its roster can no longer "
                    "be changed."
                ),
                embeds=[],
                view=None,
            )
            return
        await interaction.response.edit_message(
            content="Removing the selected members…",
            embeds=[],
            view=None,
        )
        removed: list[int] = []
        skipped: list[int] = []
        promoted: list[int] = []
        kept_after_end: list[int] = []
        for index, user in enumerate(users):
            # The picker holds several members and remove_signup awaits Discord
            # I/O between each, so the event can cross its end partway through
            # the loop even though the pre-loop check passed. Re-check every
            # iteration and stop the moment it has ended, so no removal (and no
            # waitlist promotion behind it) ever lands on a finished roster.
            if _occurrence_has_ended(event, occurrence, datetime.now(UTC)):
                kept_after_end = [pending.id for pending in users[index:]]
                LOGGER.debug(
                    "Event ended mid-removal; stopping; occurrence_id=%s "
                    "user_id=%s kept=%s",
                    occurrence.occurrence_id,
                    interaction.user.id,
                    len(kept_after_end),
                )
                break
            signup, promotion = await remove_signup(
                self._bot,
                event,
                occurrence,
                user.id,
            )
            if signup is None:
                skipped.append(user.id)
                continue
            removed.append(user.id)
            if promotion is not None:
                promoted.append(promotion.discord_user_id)
        # Removing a seated member promotes the first fitting waitlisted one, so
        # a member picked alongside their own promoter can be promoted by an
        # earlier iteration and then removed by a later one. They are off the
        # roster, so drop them from the promotions before reporting, or the
        # summary would claim they both left and moved up.
        removed_set = set(removed)
        promoted = [
            user_id for user_id in promoted if user_id not in removed_set
        ]
        LOGGER.debug(
            "Applied roster removal; event_id=%s occurrence_id=%s user_id=%s "
            "picked=%s removed=%s not_signed_up=%s promoted=%s kept=%s",
            event.event_id,
            occurrence.occurrence_id,
            interaction.user.id,
            len(users),
            len(removed),
            len(skipped),
            len(promoted),
            len(kept_after_end),
        )
        summary = _removal_summary(removed, skipped, promoted, kept_after_end)
        if kept_after_end:
            # The event ended partway through, so the edit session is no longer
            # valid (an ended event cannot be edited). Report what was applied
            # and stop, rather than re-showing an edit preview that can no
            # longer be saved.
            await interaction.edit_original_response(
                content=summary,
                embeds=[],
                view=None,
            )
            return
        embeds, view = build_event_preview(
            self._bot,
            self._draft,
            primary=occurrence,
        )
        await interaction.edit_original_response(
            content=summary,
            embeds=embeds,
            view=view,
        )


def _mention_list(discord_user_ids: list[int]) -> str:
    return ", ".join(f"<@{user_id}>" for user_id in discord_user_ids)


def _removal_summary(
    removed: list[int],
    skipped: list[int],
    promoted: list[int],
    kept_after_end: list[int] | None = None,
) -> str:
    lines: list[str] = []
    if removed:
        lines.append(f"Removed {_mention_list(removed)} from the roster.")
    else:
        lines.append("Nobody was removed from the roster.")
    if skipped:
        lines.append(
            f"{_mention_list(skipped)} was not signed up for this event."
            if len(skipped) == 1
            else f"{_mention_list(skipped)} were not signed up for this event."
        )
    if promoted:
        lines.append(f"{_mention_list(promoted)} moved up from the waitlist.")
    if kept_after_end:
        lines.append(
            "The event ended before the rest could be removed, so "
            + _mention_list(kept_after_end)
            + (" was kept." if len(kept_after_end) == 1 else " were kept.")
        )
    return "\n".join(lines)


class ChannelMoveConfirmView(discord.ui.View):
    def __init__(
        self,
        bot: Gw2Bot,
        draft: EventDraft,
        old_channel_id: int,
    ):
        super().__init__(timeout=FLOW_TIMEOUT_SECONDS)
        self._bot = bot
        self._draft = draft
        self._old_channel_id = old_channel_id

    @discord.ui.button(label="Move event", style=discord.ButtonStyle.danger)
    async def move(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[ChannelMoveConfirmView],
    ) -> None:
        if not user_has_role(interaction.user, EVENT_CREATE_ROLE_ID):
            LOGGER.warning(
                "Rejected event channel move from Discord user %s; required "
                "role %s",
                interaction.user.id,
                EVENT_CREATE_ROLE_ID,
            )
            await interaction.response.send_message(
                "You do not have the required role to edit events.",
                ephemeral=True,
            )
            return
        await apply_event_edit(
            self._bot,
            interaction,
            self._draft,
            self._old_channel_id,
            repost=True,
        )

    @discord.ui.button(
        label="Keep current channel",
        style=discord.ButtonStyle.secondary,
    )
    async def keep(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[ChannelMoveConfirmView],
    ) -> None:
        # Undo the pending channel change and return to the edit preview.
        self._draft.channel_id = self._old_channel_id
        await send_event_preview(self._bot, interaction, self._draft)


async def apply_event_edit(
    bot: Gw2Bot,
    interaction: discord.Interaction,
    draft: EventDraft,
    old_channel_id: int,
    *,
    repost: bool,
) -> None:
    from gw2bot.events.posting import (
        rebalance_occurrence_roster,
        refresh_occurrence_message,
        repost_occurrence,
    )

    editing_event_id = draft.editing_event_id
    if editing_event_id is None:
        raise ValueError("apply_event_edit requires an editing draft")
    # Guard against a double click racing two callbacks before the first removes
    # the buttons; without it a channel move would re-post twice and orphan a
    # duplicate message. The check and set are synchronous (no await between),
    # so the second callback always observes the flag.
    if draft.edit_applied:
        await interaction.response.send_message(
            "This event was already updated.",
            ephemeral=True,
        )
        return
    draft.edit_applied = True
    edited = draft.to_event(editing_event_id)
    await interaction.response.edit_message(
        content="Saving your changes…",
        embeds=[],
        view=None,
    )
    occurrences = [
        occurrence
        for occurrence in bot.event_store.get_event_occurrences(
            editing_event_id
        )
        if occurrence.status is not EventStatus.OVER
    ]
    # An occurrence that has already started is live: its roster is in play, and
    # re-rendering it from an edit can persist OVER (shortening the duration puts
    # start + duration behind now) without seeding the recurring series' next
    # occurrence the way the scheduler does, silently ending the series. Ongoing
    # events can only be deleted. The command refuses them too, but the preview
    # can sit open for minutes, so the event may have started since it opened.
    if any(
        occurrence.start_time <= datetime.now(UTC)
        for occurrence in occurrences
    ):
        LOGGER.warning(
            "Rejected edit of an ongoing event; event_id=%s user_id=%s",
            editing_event_id,
            interaction.user.id,
        )
        await interaction.edit_original_response(
            content=ONGOING_EDIT_REJECTION,
            view=None,
        )
        return
    previous = bot.event_store.get_event(editing_event_id)
    # The soonest non-OVER occurrence is what a date change reschedules, whether
    # it is already posted or still waiting for the scheduler to post it.
    primary = occurrences[0] if occurrences else None
    # The event row's start_time is the series origin. For a repeating event the
    # primary occurrence has long since advanced past it, and the draft is seeded
    # with *that occurrence's* start, so writing the draft's start straight back
    # would drag the origin forward on every edit until it no longer records when
    # the series began. Shift the origin by the delta the commander actually
    # applied instead: nothing moves when the date was left alone, and for a
    # non-repeating event (whose origin and only occurrence are the same instant)
    # it still lands exactly on the new start.
    origin_start = edited.start_time
    if previous is not None and primary is not None:
        origin_start = previous.start_time + (
            edited.start_time - primary.start_time
        )
    try:
        updated = bot.event_store.update_event(
            event_id=editing_event_id,
            category=edited.category,
            title=edited.title,
            description=edited.description,
            channel_id=edited.channel_id,
            leader_discord_id=edited.leader_discord_id,
            start_time=origin_start,
            duration_minutes=edited.duration_minutes,
            repeat_frequency=edited.repeat_frequency,
            repeat_days=edited.repeat_days,
            delete_previous_on_repeat=edited.delete_previous_on_repeat,
        )
    except SQLAlchemyError as exc:
        # The save did not happen, so clear the guard to allow a fresh retry.
        draft.edit_applied = False
        LOGGER.error(
            "Could not save event edit; event_id=%s error_type=%s",
            editing_event_id,
            type(exc).__name__,
        )
        await interaction.edit_original_response(
            content="The changes could not be saved. Try again later.",
            view=None,
        )
        return
    channel_changed = old_channel_id != updated.channel_id
    moving = repost and channel_changed
    category_changed = (
        previous is not None and previous.category is not updated.category
    )
    attempted = 0
    refreshed = 0
    for occurrence in occurrences:
        current = occurrence
        if (
            primary is not None
            and occurrence.occurrence_id == primary.occurrence_id
            and occurrence.start_time != edited.start_time
        ):
            # A date/time edit reschedules the occurrence the commander sees;
            # sync its own start_time so the embed and thread name update too.
            # This tracks the draft's start, not the event's: the event now
            # carries the series origin, which is a different instant.
            bot.event_store.set_occurrence_start_time(
                occurrence.occurrence_id,
                edited.start_time,
            )
            refetched = bot.event_store.get_occurrence(
                occurrence.occurrence_id
            )
            if refetched is not None:
                current = refetched
        if category_changed:
            # The category picks the capacity the roster was seated against, so
            # changing it invalidates every stored assignment. Re-seat the roster
            # before the message is re-rendered, so the embed and the capacity
            # checks both describe the new category.
            try:
                rebalance_occurrence_roster(bot, updated, current)
            except (SQLAlchemyError, ValueError) as exc:
                # A stale roster must not block the rest of the edit.
                LOGGER.error(
                    "Could not rebalance roster after a category change; "
                    "occurrence_id=%s error_type=%s",
                    current.occurrence_id,
                    type(exc).__name__,
                )
        if current.message_id is None:
            # Unposted (e.g. a recurring series' next occurrence): the
            # reschedule above is persisted and the scheduler will post it with
            # the new time; there is no live message to refresh now.
            continue
        attempted += 1
        try:
            if moving:
                # The old message is addressed through the channel the
                # occurrence was actually posted to, which repost_occurrence
                # reads off the occurrence itself.
                await repost_occurrence(bot, updated, current)
            else:
                await refresh_occurrence_message(
                    bot,
                    updated,
                    current,
                    force_thread_rename=True,
                )
                # refresh_occurrence_message absorbs Discord failures: it marks
                # the occurrence dirty for the scheduler to retry and returns
                # the old status rather than raising, so the handler below never
                # sees them. Re-read the row and treat a dirty occurrence as a
                # failed refresh, otherwise a message or thread name that is
                # still stale would be reported as successfully updated.
                saved = bot.event_store.get_occurrence(current.occurrence_id)
                if saved is not None and saved.needs_refresh:
                    LOGGER.error(
                        "Posted occurrence left stale after edit; "
                        "occurrence_id=%s",
                        current.occurrence_id,
                    )
                    continue
        except (discord.HTTPException, SQLAlchemyError) as exc:
            # One occurrence failing must not block the others.
            LOGGER.error(
                "Could not update posted occurrence after edit; "
                "occurrence_id=%s error_type=%s",
                current.occurrence_id,
                type(exc).__name__,
            )
            _mark_occurrence_stale(bot, current)
            continue
        refreshed += 1
    LOGGER.debug(
        "Applied event edit; event_id=%s repost=%s channel_changed=%s "
        "occurrences_attempted=%s occurrences_refreshed=%s",
        updated.event_id,
        repost,
        channel_changed,
        attempted,
        refreshed,
    )
    move_failed = moving and attempted > 0 and refreshed == 0
    if move_failed:
        updated = _restore_event_channel(bot, updated, old_channel_id)
        content = (
            f"Event **{updated.event_id}** was saved, but it could not be "
            "posted in the new channel, so it stays in the current one."
        )
    elif attempted > 0 and refreshed == 0:
        # Every posted occurrence failed to refresh, so the public message is
        # stale even though the stored event was updated; say so instead of
        # claiming the message reflects the change.
        content = (
            f"Event **{updated.event_id}** was saved, but its posted message "
            "could not be updated and may be out of date."
        )
    else:
        content = f"Event **{updated.event_id}** was updated."
    await interaction.edit_original_response(content=content, view=None)


def _mark_occurrence_stale(bot: Gw2Bot, occurrence: EventOccurrence) -> None:
    # The edit is committed, but this occurrence's public message was never
    # re-rendered, so it still shows the old title, category, time and roster.
    # A failed channel move is the clearest case: the new post never went out, so
    # the old message survives - untouched - in the previous channel.
    #
    # Nothing else would ever fix that. The scheduler only re-renders an
    # occurrence whose status changed or that is flagged dirty, and an edit
    # normally leaves the status alone (an ongoing event cannot be edited at all,
    # so the occurrence is always still upcoming here). The post would stay stale
    # until the event actually started. Flag it so the next maintenance pass
    # re-renders it in place, against the channel it really lives in.
    if occurrence.needs_refresh:
        return
    try:
        bot.event_store.set_occurrence_needs_refresh(
            occurrence.occurrence_id,
            True,
        )
    except SQLAlchemyError as exc:
        LOGGER.error(
            "Could not flag occurrence for refresh after a failed edit; "
            "occurrence_id=%s error_type=%s",
            occurrence.occurrence_id,
            type(exc).__name__,
        )


def _restore_event_channel(
    bot: Gw2Bot,
    event: Event,
    old_channel_id: int,
) -> Event:
    # Every repost into the new channel failed, so the live messages are still
    # in the old channel while the stored event already points at the new one.
    # An occurrence's message is always resolved through event.channel_id, so
    # leaving the move committed would make the next scheduler refresh look for
    # those messages in a channel they are not in, get NotFound and retire a
    # still-active occurrence. Put the stored channel back so the surviving
    # posts stay reachable; the rest of the edit is kept.
    try:
        restored = bot.event_store.update_event(
            event_id=event.event_id,
            category=event.category,
            title=event.title,
            description=event.description,
            channel_id=old_channel_id,
            leader_discord_id=event.leader_discord_id,
            start_time=event.start_time,
            duration_minutes=event.duration_minutes,
            repeat_frequency=event.repeat_frequency,
            repeat_days=event.repeat_days,
            delete_previous_on_repeat=event.delete_previous_on_repeat,
        )
    except SQLAlchemyError as exc:
        LOGGER.error(
            "Could not restore the event channel after a failed move; "
            "event_id=%s error_type=%s",
            event.event_id,
            type(exc).__name__,
        )
        return event
    LOGGER.debug(
        "Restored the event channel after a failed move; event_id=%s",
        event.event_id,
    )
    return restored


class EventDeleteConfirmView(discord.ui.View):
    def __init__(self, bot: Gw2Bot, event: Event):
        super().__init__(timeout=FLOW_TIMEOUT_SECONDS)
        self._bot = bot
        self._event = event
        self._deleting = False

    @discord.ui.button(label="Delete event", style=discord.ButtonStyle.danger)
    async def delete(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[EventDeleteConfirmView],
    ) -> None:
        from gw2bot.events.posting import delete_event_posts

        # The confirmation can sit open for minutes; recheck the role before the
        # irreversible delete, mirroring the edit/post paths.
        if not user_has_role(interaction.user, EVENT_CREATE_ROLE_ID):
            LOGGER.warning(
                "Rejected event delete from Discord user %s; required role %s",
                interaction.user.id,
                EVENT_CREATE_ROLE_ID,
            )
            await interaction.response.send_message(
                "You do not have the required role to delete events.",
                ephemeral=True,
            )
            return
        # Guard a double click racing two callbacks before the first removes the
        # buttons; the check and set are synchronous, so the second observes it.
        if self._deleting:
            await interaction.response.send_message(
                "This event is already being deleted.",
                ephemeral=True,
            )
            return
        self._deleting = True
        await interaction.response.edit_message(
            content="Deleting the event…",
            embeds=[],
            view=None,
        )
        # Read the occurrences before the store rows are removed so their
        # messages can still be cleaned up afterwards.
        occurrences = self._bot.event_store.get_event_occurrences(
            self._event.event_id
        )
        try:
            self._bot.event_store.delete_event(self._event.event_id)
        except SQLAlchemyError as exc:
            self._deleting = False
            LOGGER.error(
                "Could not delete event; event_id=%s error_type=%s",
                self._event.event_id,
                type(exc).__name__,
            )
            await interaction.edit_original_response(
                content="The event could not be deleted. Try again later.",
                view=None,
            )
            return
        await delete_event_posts(self._bot, self._event, occurrences)
        LOGGER.debug(
            "Deleted event; event_id=%s occurrences=%s user_id=%s",
            self._event.event_id,
            len(occurrences),
            interaction.user.id,
        )
        await interaction.edit_original_response(
            content=f"Event **{self._event.event_id}** was deleted.",
            view=None,
        )

    @discord.ui.button(
        label="Keep event",
        style=discord.ButtonStyle.secondary,
    )
    async def keep(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[EventDeleteConfirmView],
    ) -> None:
        await interaction.response.edit_message(
            content="The event was not deleted.",
            embeds=[],
            view=None,
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
        self._draft.delete_previous_on_repeat = False
        await send_event_preview(self._bot, interaction, self._draft)


def build_signup_view(occurrence_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(EventSignUpButton(occurrence_id))
    view.add_item(EventSignOutButton(occurrence_id))
    view.add_item(EventSettingsButton(occurrence_id))
    return view


def _occurrence_has_ended(
    event: Event,
    occurrence: EventOccurrence,
    now: datetime,
) -> bool:
    end_time = occurrence.start_time + timedelta(
        minutes=event.duration_minutes
    )
    return now >= end_time


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
        if _occurrence_has_ended(event, occurrence, datetime.now(UTC)):
            LOGGER.debug(
                "Sign out pressed after the event ended; occurrence_id=%s "
                "user_id=%s",
                occurrence.occurrence_id,
                interaction.user.id,
            )
            await interaction.response.send_message(
                "This event has already ended, so its roster can no longer "
                "be changed.",
                ephemeral=True,
            )
            return
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
                emoji="⚙️",
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

        # The event may have ended while this confirmation was open; never
        # mutate a historical roster (which could also promote a waitlisted
        # user into a past event).
        if _occurrence_has_ended(
            self._event, self._occurrence, datetime.now(UTC)
        ):
            LOGGER.debug(
                "Sign out confirmed after the event ended; occurrence_id=%s "
                "user_id=%s",
                self._occurrence.occurrence_id,
                interaction.user.id,
            )
            await interaction.response.edit_message(
                content=(
                    "This event has already ended, so its roster can no "
                    "longer be changed."
                ),
                view=None,
            )
            return
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
    if _occurrence_has_ended(event, occurrence, now):
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


def _role_pick_label(
    role: EventRole,
    fits: bool,
    waitlist_only: bool,
) -> str:
    # Every role is always offered so a user can pick a full preferred role
    # and fall back to an open flex role (or waitlist for a specific role
    # while others remain open). When the whole roster is full, picking any
    # role can only waitlist; otherwise a full role may still resolve to a
    # flex assignment, so it is labelled "full" rather than "waitlist".
    if waitlist_only:
        return f"{role.value} (waitlist)"
    if not fits:
        return f"{role.value} (full)"
    return role.value


class RolePickSelect(discord.ui.Select["RolePickView"]):
    def __init__(self, flow: SignupFlow):
        signups = flow.bot.event_store.get_signups(
            flow.occurrence.occurrence_id
        )
        available = set(fitting_roles(flow.event.capacity, signups))
        waitlist_only = not available
        options = [
            discord.SelectOption(
                label=_role_pick_label(
                    role, role in available, waitlist_only
                ),
                value=role.value,
                emoji=ROLE_EMOJI[role],
            )
            for role in EventRole
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
            discord.SelectOption(
                label=role.value,
                value=role.value,
                emoji=ROLE_EMOJI[role],
            )
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
