from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from math import ceil
from typing import TYPE_CHECKING, Any, cast
from zoneinfo import ZoneInfo

import discord
from discord.utils import MISSING
from sqlalchemy.exc import SQLAlchemyError

from gw2bot.discord_utils import (
    resolve_display_names,
    resolve_guild_memberships,
    safe_int,
    send_direct_message,
    user_has_role,
)
from gw2bot.events.formatting import (
    EVENT_DATETIME_PLACEHOLDER,
    compute_status,
    confirm_embed,
    describe_repeat,
    details_confirm_embed,
    details_preview_embed,
    edit_confirm_embed,
    event_embed,
    format_duration_input,
    format_event_datetime,
    format_repeat_days,
    parse_event_datetime,
    parse_event_duration,
    parse_repeat_days,
    roster_edit_embed,
    signup_edit_limit_message,
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
    RosterUpdate,
    available_edit_tokens,
    fitting_roles,
    is_roster_full,
)
from gw2bot.events.roles import EVENT_CREATE_ROLE_ID

if TYPE_CHECKING:
    from gw2bot.bot import Gw2Bot
    from gw2bot.events.posting import (
        AutoSignupDisableResult,
        OccurrenceCancellation,
    )

LOGGER = logging.getLogger(__name__)

EVENT_TITLE_MAX_LENGTH = 256
EVENT_DESCRIPTION_MAX_LENGTH = 4000
FLOW_TIMEOUT_SECONDS = 600

# An event whose occurrence has started is live, so its details are frozen:
# re-rendering it from an edit can persist OVER without seeding a recurring
# series' next occurrence, and moving or rescheduling a run people are already
# in is not an edit anyone wants. Its roster is still in play, though - members
# sign out of it and leaders take them off it - so that stays editable.
ONGOING_EDIT_REJECTION = (
    "That event has already started, so its details can no longer be changed. "
    "Run `/event edit` again to manage its roster, or `/event delete` to "
    "remove it."
)
PREVIEW_EVENT_ID_TEXT = "—"

# Where an event may be posted. A text channel takes the event as a message with
# a signup thread under it. A forum post takes the event as a message inside the
# post, which stands in for that signup thread because a forum post cannot hold
# threads of its own. Forum *channels* are deliberately absent: the bot posts
# into posts that already exist and never opens new ones.
#
# A forum post is a public thread, and Discord's picker cannot narrow that to
# posts alone, so a thread under a text channel can be picked here too and is
# rejected on submission by _destination_error.
EVENT_CHANNEL_TYPES = [
    discord.ChannelType.text,
    discord.ChannelType.public_thread,
]
# The parents that make a picked thread a forum post.
FORUM_CHANNEL_TYPES = (
    discord.ChannelType.forum,
    discord.ChannelType.media,
)
# Discord caps a Label's text at 45 characters, so the forum hint rides along in
# the Label description instead of the prompt itself.
EVENT_CHANNEL_PROMPT = "Where should your event be posted?"
EVENT_CHANNEL_HINT = "A text channel, or an existing forum post."
EVENT_CHANNEL_REJECTION = (
    "Events can only be posted in a text channel or an existing forum post. "
    "A thread under a channel is not supported."
)

# Discord's hard cap on how many options one select may hold, which is also the
# most it may return. A WvW roster seats 50 plus a waitlist, so the removal
# picker pages the roster at this size.
REMOVE_SELECT_PAGE_SIZE = 25

# Discord's cap on a select option's label; a longer display name is truncated
# rather than rejected by the API.
REMOVE_OPTION_LABEL_MAX_LENGTH = 100

# How many characters the list of departed members may take. Discord refuses a
# message body over 2,000 characters, and this line is prefixed to the removal
# picker's own prompt, so the budget leaves room for that and for the sentence
# wrapped around the names.
DEPARTED_SUMMARY_BUDGET = 1_000


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
    # Set when the event being edited is already in progress. Its stored
    # details are frozen at that point, so the preview offers the roster
    # controls alone and no path from it can reach apply_event_edit.
    roster_only: bool = False
    # The occurrence a roster-only session was opened for. A running event is
    # minutes from ending, and the moment it does the scheduler retires it and
    # seeds the series' next occurrence - which would become the soonest live
    # one. Re-resolving the occurrence on each click would then silently move
    # the session onto next week's roster, so it is pinned here instead and the
    # flow refuses it once it has ended. Only roster-only sessions pin: the
    # editor for an upcoming event edits the whole series, and apply_event_edit
    # walks every live occurrence by design.
    editing_occurrence_id: int | None = None
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
    roster_only: bool = False,
    editing_occurrence_id: int | None = None,
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
        roster_only=roster_only,
        editing_occurrence_id=editing_occurrence_id,
    )


@dataclass(frozen=True)
class RepeatAttempt:
    """Repeat answers that failed validation, kept only to refill the modal.

    They are deliberately never written to the draft. A preview opened from an
    earlier step shares that draft and can complete it, so a frequency stored
    beside unparsed days could be posted as a weekly or monthly event with no
    days to repeat on. Such an event raises out of next_occurrence_start when
    its occurrence ends, which aborts the whole maintenance pass, not just
    that event's.
    """

    frequency: RepeatFrequency
    days_text: str
    delete_previous: bool


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


def build_details_preview(
    bot: Gw2Bot,
    draft: EventDraft,
) -> tuple[list[discord.Embed], discord.ui.View]:
    # The step-one preview of a draft whose schedule has not been entered yet.
    # It carries the same "Change something" flow as the final preview, so a
    # correction can be made before answering three more questions.
    preview = details_preview_embed(
        draft.category,
        draft.title,
        draft.description,
        draft.channel_id,
        draft.leader_discord_id,
        PREVIEW_EVENT_ID_TEXT,
    )
    return [preview, details_confirm_embed()], EventDetailsConfirmView(
        bot, draft
    )


def _editing_occurrence(
    bot: Gw2Bot,
    draft: EventDraft,
    primary: EventOccurrence | None = None,
) -> EventOccurrence | None:
    """The occurrence an edit session is working on.

    A roster-only session pins its occurrence, so it keeps editing the run the
    commander opened even after that run ends and the series seeds its
    successor. Every other session tracks the soonest live occurrence, which is
    what a date change reschedules.
    """
    if primary is not None:
        return primary
    if draft.editing_occurrence_id is not None:
        return bot.event_store.get_occurrence(draft.editing_occurrence_id)
    if draft.editing_event_id is None:
        return None
    return _primary_live_occurrence(bot, draft.editing_event_id)


def _preview_status(
    event: Event,
    signups: list[EventSignup],
    roster_only: bool,
) -> EventStatus:
    # A pending edit has no status of its own - it describes an event that does
    # not exist yet - so the "before you save" preview renders neutrally as
    # OPEN. A roster-only preview mirrors an event that is already running, so
    # it shows the status that event actually has.
    if not roster_only:
        return EventStatus.OPEN
    return compute_status(
        event.start_time,
        event.duration_minutes,
        datetime.now(UTC),
        is_roster_full(event.capacity, signups),
    )


def build_event_preview(
    bot: Gw2Bot,
    draft: EventDraft,
    *,
    primary: EventOccurrence | None = None,
) -> tuple[list[discord.Embed], discord.ui.View]:
    # Split out from send_event_preview so a flow that has already answered the
    # interaction (the roster removal below awaits Discord I/O first) can still
    # re-render the same preview through edit_original_response.
    if not draft.is_complete():
        # Only a creation draft mid-flow lands here: an edit draft is built
        # from a stored event and is complete from the start. Every "Change
        # something" path funnels back through this function, so the step-one
        # preview is what a change made before the schedule returns to.
        return build_details_preview(bot, draft)
    editing_event_id = draft.editing_event_id
    view: discord.ui.View
    if editing_event_id is not None:
        # Show the live roster so the preview mirrors the posted message, but
        # render the pending date/time from the draft (to_event uses the draft's
        # start_time), not the occurrence's stored time. The initial /event edit
        # call passes the occurrence it already fetched; change-flow re-renders
        # do not have it, so look it up.
        occurrence = _editing_occurrence(bot, draft, primary)
        signups = (
            bot.event_store.get_signups(occurrence.occurrence_id)
            if occurrence is not None
            else []
        )
        edited = draft.to_event(editing_event_id)
        preview = event_embed(
            edited,
            signups,
            _preview_status(edited, signups, draft.roster_only),
            event_id_text=str(editing_event_id),
        )
        if draft.roster_only:
            # The event is running: its details are frozen, so the preview is a
            # live roster with the roster controls under it rather than a
            # "before you save" picture of pending changes.
            confirmation = roster_edit_embed()
            view = EventRosterEditView(bot, draft)
        else:
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
    deferred: bool = False,
    content: str | None = None,
) -> None:
    embeds, view = build_event_preview(bot, draft, primary=primary)
    LOGGER.debug(
        "Sending event preview; user_id=%s category=%s repeat=%s "
        "title_characters=%s in_place=%s editing=%s roster_only=%s "
        "deferred=%s complete=%s",
        draft.leader_discord_id,
        draft.category.value if draft.category is not None else None,
        draft.repeat_frequency.value,
        len(draft.title),
        _is_ephemeral_component_interaction(interaction),
        draft.editing_event_id is not None,
        draft.roster_only,
        deferred,
        draft.is_complete(),
    )
    if deferred:
        # The caller already acknowledged the interaction because it had to
        # await Discord I/O (a roster membership check) before it could answer,
        # so the preview goes out as a follow-up rather than a first response.
        await interaction.followup.send(
            # A follow-up refuses a None content, unlike an edit or a first
            # response, so the "no note to show" case is the API's own
            # sentinel rather than an empty message.
            content=content if content is not None else MISSING,
            embeds=embeds,
            view=view,
            ephemeral=True,
        )
    elif _is_ephemeral_component_interaction(interaction):
        await interaction.response.edit_message(
            content=content,
            embeds=embeds,
            view=view,
        )
    else:
        await interaction.response.send_message(
            content=content,
            embeds=embeds,
            view=view,
            ephemeral=True,
        )


async def _destination_error(bot: Gw2Bot, channel: Any) -> str | None:
    """Reject a picked destination the bot will not post an event to.

    Returns the message to show the commander, or None when the destination is
    allowed. Only a thread can be wrong here: the picker offers text channels
    and public threads, and a public thread is only a forum post when its parent
    is a forum. A parent the bot cannot resolve is accepted rather than refused,
    because posting there works either way and a lookup failure must not block a
    legitimate forum post.
    """
    from gw2bot.events.posting import is_thread_channel, resolve_channel

    if not is_thread_channel(channel):
        return None
    parent_id = getattr(channel, "parent_id", None)
    if parent_id is None:
        LOGGER.debug("Picked thread reports no parent; accepting it")
        return None
    try:
        parent = await resolve_channel(bot, parent_id)
    except discord.DiscordException as exc:
        LOGGER.debug(
            "Could not resolve the parent of a picked thread; accepting it; "
            "error_type=%s",
            type(exc).__name__,
        )
        return None
    parent_type = getattr(parent, "type", None)
    if parent_type in FORUM_CHANNEL_TYPES:
        return None
    LOGGER.debug(
        "Rejected an event destination that is a thread under a channel; "
        "parent_type=%s",
        parent_type,
    )
    return EVENT_CHANNEL_REJECTION


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
    def __init__(
        self,
        bot: Gw2Bot,
        draft: EventDraft,
        attempt: RepeatAttempt | None = None,
    ):
        super().__init__(bot, draft, "Try again")
        self._attempt = attempt

    def build_modal(self) -> discord.ui.Modal:
        return EventRepeatModal(self._bot, self._draft, self._attempt)


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
            channel_types=EVENT_CHANNEL_TYPES,
            required=True,
        )
        self.add_item(
            discord.ui.Label(
                text=EVENT_CHANNEL_PROMPT,
                description=EVENT_CHANNEL_HINT,
                component=self.channel,
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        self._draft.category = EventCategory(self.category.values[0])
        self._draft.title = self.title_input.value.strip()
        self._draft.description = self.description_input.value.strip()
        destination = self.channel.values[0]
        rejection = await _destination_error(self._bot, destination)
        if rejection is not None:
            # The title, description and category are already on the draft, so
            # the retry modal opens pre-filled and only the destination has to be
            # picked again.
            await _send_validation_error(
                interaction,
                ValueError(rejection),
                RetryDetailsView(self._bot, self._draft),
            )
            return
        self._draft.channel_id = destination.id
        LOGGER.debug(
            "Event details step submitted; user_id=%s category=%s "
            "title_characters=%s description_characters=%s",
            interaction.user.id,
            self._draft.category.value,
            len(self._draft.title),
            len(self._draft.description),
        )
        await send_event_preview(self._bot, interaction, self._draft)


class RetryDetailsView(_ModalOpenView):
    def __init__(self, bot: Gw2Bot, draft: EventDraft):
        super().__init__(bot, draft, "Try again")

    def build_modal(self) -> discord.ui.Modal:
        return EventDetailsModal(self._bot, self._draft)


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
        # Answering "yes" here does not write a frequency onto the draft: only
        # the repeat modal does. A draft that never reaches that modal must not
        # carry a frequency nobody chose, because a preview reopened from an
        # earlier step would then offer to post it.
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
    def __init__(
        self,
        bot: Gw2Bot,
        draft: EventDraft,
        attempt: RepeatAttempt | None = None,
    ):
        super().__init__()
        self._bot = bot
        self._draft = draft
        # A rejected attempt refills the modal from itself rather than from the
        # draft, which never took those answers on.
        frequency = (
            attempt.frequency if attempt is not None else draft.repeat_frequency
        )
        days_text = (
            attempt.days_text if attempt is not None else draft.repeat_days_text
        )
        delete_previous = (
            attempt.delete_previous
            if attempt is not None
            else draft.delete_previous_on_repeat
        )
        self.frequency = discord.ui.Select["EventRepeatModal"](
            options=_frequency_options(frequency),
        )
        self.add_item(
            discord.ui.Label(
                text="How often?",
                component=self.frequency,
            )
        )
        self.days_input = discord.ui.TextInput["EventRepeatModal"](
            required=False,
            default=days_text or None,
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
            options=_yes_no_options(delete_previous),
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
        days_text = self.days_input.value.strip()
        delete_previous = self.delete_previous.values[0] == "yes"
        try:
            repeat_days = parse_repeat_days(frequency, days_text)
        except ValueError as error:
            # The draft keeps the repeat settings it already had: a rejected
            # frequency must not reach it, because a still-open preview from an
            # earlier step could complete and post it without any days.
            await _send_validation_error(
                interaction,
                error,
                RetryRepeatView(
                    self._bot,
                    self._draft,
                    RepeatAttempt(frequency, days_text, delete_previous),
                ),
            )
            return
        self._draft.repeat_frequency = frequency
        self._draft.repeat_days_text = days_text
        self._draft.delete_previous_on_repeat = delete_previous
        self._draft.repeat_days = repeat_days
        LOGGER.debug(
            "Event repeat step submitted; user_id=%s frequency=%s days=%s",
            interaction.user.id,
            frequency.value,
            len(repeat_days),
        )
        await send_event_preview(self._bot, interaction, self._draft)


class _EditFlowView(discord.ui.View):
    """Base for the views that sit on a draft, carrying no buttons of its own.

    The in-progress roster editor deliberately does not inherit the
    "Change something" button, so that lives one level down rather than here.
    """

    def __init__(self, bot: Gw2Bot, draft: EventDraft):
        super().__init__(timeout=FLOW_TIMEOUT_SECONDS)
        self._bot = bot
        self._draft = draft


class _PreviewConfirmView(_EditFlowView):
    def change_fields(self) -> tuple[tuple[str, str], ...]:
        # Resolved at call time rather than as a class attribute, because the
        # field lists are defined further down with the change flow itself.
        return _CHANGE_FIELDS

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
            view=ChangeFieldView(
                self._bot,
                self._draft,
                fields=self.change_fields(),
            ),
            ephemeral=True,
        )


class EventDetailsConfirmView(_PreviewConfirmView):
    """Step-one preview: continue to the schedule, or correct the details."""

    def change_fields(self) -> tuple[tuple[str, str], ...]:
        # The schedule and repeat settings have not been asked yet, so offering
        # them here would let a commander skip past the questions that follow.
        return _DETAILS_CHANGE_FIELDS

    @discord.ui.button(label="Next", style=discord.ButtonStyle.primary)
    async def next_step(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[EventDetailsConfirmView],
    ) -> None:
        LOGGER.debug(
            "Event details preview continued to the schedule step; "
            "user_id=%s",
            interaction.user.id,
        )
        await interaction.response.send_modal(
            EventScheduleModal(self._bot, self._draft)
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
                    "message**, along with any signup thread the bot opened "
                    "for it and every message in that thread. A forum post the "
                    "event was posted into is left in place. The roster is kept "
                    "and re-posted at the new destination. Continue?"
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
        await open_roster_removal(self._bot, interaction, self._draft)


class EventRosterEditView(_EditFlowView):
    """The whole editor for an event that is already in progress.

    It carries the roster button alone: the event's stored details are frozen
    once it starts, so there is nothing here to save and no way from this view
    into apply_event_edit.
    """

    @discord.ui.button(
        label="Remove sign-ups",
        style=discord.ButtonStyle.danger,
    )
    async def remove_signups(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[EventRosterEditView],
    ) -> None:
        await open_roster_removal(self._bot, interaction, self._draft)


async def open_roster_removal(
    bot: Gw2Bot,
    interaction: discord.Interaction,
    draft: EventDraft,
) -> None:
    """Show the roster removal picker for the draft's event.

    Shared by the upcoming-event editor and the in-progress roster editor, so
    both reach the same picker over the same freshly read roster.
    """
    editing_event_id = draft.editing_event_id
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
    # fresh: members can sign up or out while the preview sits open. A
    # roster-only session stays on the occurrence it pinned rather than
    # following the series onto its successor.
    event = bot.event_store.get_event(editing_event_id)
    occurrence = _editing_occurrence(bot, draft)
    if event is None or occurrence is None:
        LOGGER.debug(
            "Roster removal opened for a missing event; event_id=%s "
            "user_id=%s exists=%s",
            editing_event_id,
            interaction.user.id,
            event is not None,
        )
        await interaction.response.send_message(
            "This event no longer exists.",
            ephemeral=True,
        )
        return
    # The preview can sit open for minutes, and a roster-only session opens on
    # an event that is already running, so it can reach its end while the
    # commander reads it. An ended roster is history: it cannot be pruned or
    # removed from, so refuse before the picker is drawn rather than offering
    # controls that would all be rejected.
    if occurrence_has_ended(event, occurrence, datetime.now(UTC)):
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
    signups = bot.event_store.get_signups(occurrence.occurrence_id)
    if not signups:
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
        "roster=%s roster_only=%s",
        editing_event_id,
        occurrence.occurrence_id,
        interaction.user.id,
        len(signups),
        draft.roster_only,
    )
    # The picker lists the roster by name, and the bot runs without the
    # members intent, so every member is a Discord fetch. That cannot finish
    # inside the three-second interaction window on a large roster, so
    # acknowledge first and fill the picker in on the follow-up edit.
    await interaction.response.edit_message(
        content="Loading the roster…",
        embeds=[],
        view=None,
    )
    # One lookup per member answers both questions the picker needs: what to
    # call them, and whether they are still in the server at all. Anyone who
    # has left goes before the picker is drawn, so a leader is never offered a
    # seat holder who cannot see the event.
    departed_note, names = await prune_departed_members(
        bot,
        interaction.guild,
        event,
        occurrence,
    )
    if departed_note is not None:
        signups = bot.event_store.get_signups(occurrence.occurrence_id)
    if not signups:
        LOGGER.debug(
            "Roster removal emptied the roster by pruning departed members; "
            "occurrence_id=%s",
            occurrence.occurrence_id,
        )
        await interaction.edit_original_response(
            content=departed_note,
            embeds=[],
            view=None,
        )
        return
    # Keep the roster embed on screen while the picker is open: it shows
    # the seating the picker's one-line descriptions cannot. Mirror how
    # build_event_preview renders the editing preview so the two views show
    # the same roster.
    edited = draft.to_event(editing_event_id)
    roster = event_embed(
        edited,
        signups,
        _preview_status(edited, signups, draft.roster_only),
        event_id_text=str(editing_event_id),
    )
    view = RemoveSignupsView(
        bot,
        draft,
        occurrence,
        signups,
        names,
    )
    prompt = view.prompt()
    if departed_note is not None:
        prompt = f"{departed_note}\n\n{prompt}"
    await interaction.edit_original_response(
        content=prompt,
        embeds=[roster],
        view=view,
    )


async def prune_departed_members(
    bot: Gw2Bot,
    guild: discord.Guild | None,
    event: Event,
    occurrence: EventOccurrence,
) -> tuple[str | None, dict[int, str | None]]:
    """Take everyone who has left the server off the roster.

    Returns the line describing who went (None when nobody did) and the
    display names of the whole roster, which the caller reuses so one round of
    member lookups serves both the check and whatever it renders next.
    """
    from gw2bot.events.posting import (
        notify_roster_update,
        prune_departed_signups,
    )

    signups = bot.event_store.get_signups(occurrence.occurrence_id)
    if not signups:
        return None, {}
    memberships = await resolve_guild_memberships(
        bot,
        guild,
        [signup.discord_user_id for signup in signups],
    )
    names = {
        user_id: membership.display_name
        for user_id, membership in memberships.items()
    }
    departed, update = await prune_departed_signups(
        bot,
        event,
        occurrence,
        memberships,
    )
    if not departed:
        return None, names
    await notify_roster_update(bot, occurrence, update)
    return _departed_summary(departed, names), names


def _departed_summary(
    departed: Sequence[int],
    names: Mapping[int, str | None],
) -> str | None:
    """Report the members a prune took off the roster, or None when it took none."""
    if not departed:
        return None
    labels = [
        _removal_option_label(user_id, names.get(user_id))
        for user_id in departed
    ]
    # A 50-seat WvW roster whose members have all left would run to thousands
    # of characters, and the message carrying this line would be refused by
    # Discord *after* the removals were already committed - leaving the
    # commander with no answer at all. Name as many as the budget holds and
    # count the rest; the removals themselves are unaffected either way.
    shown: list[str] = []
    used = 0
    for label in labels:
        if shown and used + len(label) + 2 > DEPARTED_SUMMARY_BUDGET:
            break
        shown.append(label)
        used += len(label) + 2
    listed = ", ".join(shown)
    remaining = len(labels) - len(shown)
    if remaining:
        listed += f" and {remaining} other"
        if remaining > 1:
            listed += "s"
    # Names rather than mentions: these members are gone from the server, so a
    # mention would render as a raw id nobody can place. "They have left" reads
    # correctly for one member and for many, so the sentence needs no plural.
    return f"Removed {listed} from the roster: they have left the server."


def _removal_option_label(
    discord_user_id: int,
    display_name: str | None,
) -> str:
    # Discord could not be reached for this member, so fall back to the id:
    # an unnamed row is still removable, which a dropped option would not be.
    # An empty label is rejected by the API, so it takes the fallback too.
    if not display_name:
        return f"Member {discord_user_id}"
    return display_name[:REMOVE_OPTION_LABEL_MAX_LENGTH]


def _removal_option_description(signup: EventSignup) -> str:
    if signup.waitlisted:
        return "Waitlisted"
    role = signup.assigned_role or signup.role
    if role is None:
        return "Signed up"
    return f"Signed up as {role.value}"


class RemoveSignupsSelect(discord.ui.Select["RemoveSignupsView"]):
    def __init__(
        self,
        signups: list[EventSignup],
        names: dict[int, str | None],
    ):
        # Built from the roster rather than the guild, so the commander can
        # only pick members who are actually signed up. Discord caps a select
        # at 25 options while a WvW roster holds 50 plus a waitlist, so the
        # caller pages the roster and passes one page at a time.
        options = [
            discord.SelectOption(
                label=_removal_option_label(
                    signup.discord_user_id,
                    names.get(signup.discord_user_id),
                ),
                value=str(signup.discord_user_id),
                description=_removal_option_description(signup),
            )
            for signup in signups
        ]
        super().__init__(
            placeholder="Select the members to remove",
            min_values=1,
            # Discord refuses max_values below one, so an empty page (which
            # the roster checks upstream already rule out) stays constructible.
            max_values=max(1, len(options)),
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if view is None:
            return
        # Option values are the ids this view put there, so they parse; a
        # malformed one is dropped rather than failing the whole removal.
        user_ids = [
            user_id
            for user_id in (safe_int(value) for value in self.values)
            if user_id is not None
        ]
        await view.remove(interaction, user_ids)


class _RemoveNavButton(discord.ui.Button["RemoveSignupsView"]):
    def __init__(self, label: str, action: str):
        super().__init__(label=label, style=discord.ButtonStyle.secondary)
        self._action = action

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if view is None:
            return
        if self._action == "back":
            await view.back(interaction)
        else:
            await view.show_page(
                interaction,
                view.page + (1 if self._action == "next" else -1),
            )


class RemoveSignupsView(discord.ui.View):
    def __init__(
        self,
        bot: Gw2Bot,
        draft: EventDraft,
        occurrence: EventOccurrence,
        signups: list[EventSignup],
        names: dict[int, str | None],
        page: int = 0,
    ):
        super().__init__(timeout=FLOW_TIMEOUT_SECONDS)
        self._bot = bot
        self._draft = draft
        self._occurrence = occurrence
        self._signups = signups
        self._names = names
        self.page = max(0, min(page, self.page_count - 1))
        self.add_item(RemoveSignupsSelect(self.page_signups(), names))
        if self.page_count > 1:
            previous = _RemoveNavButton("Previous", "previous")
            previous.disabled = self.page == 0
            self.add_item(previous)
            following = _RemoveNavButton("Next", "next")
            following.disabled = self.page >= self.page_count - 1
            self.add_item(following)
        self.add_item(_RemoveNavButton("Back", "back"))

    @property
    def page_count(self) -> int:
        return max(1, ceil(len(self._signups) / REMOVE_SELECT_PAGE_SIZE))

    def page_signups(self) -> list[EventSignup]:
        start = self.page * REMOVE_SELECT_PAGE_SIZE
        return self._signups[start : start + REMOVE_SELECT_PAGE_SIZE]

    def prompt(self) -> str:
        lines = ["Select the members to remove from this event's roster."]
        if self.page_count > 1:
            lines.append(
                f"Showing {len(self.page_signups())} of "
                f"{len(self._signups)} members "
                f"(page {self.page + 1} of {self.page_count}). "
                "Removals apply to the members selected on this page."
            )
        return "\n".join(lines)

    async def show_page(
        self,
        interaction: discord.Interaction,
        page: int,
    ) -> None:
        view = RemoveSignupsView(
            self._bot,
            self._draft,
            self._occurrence,
            self._signups,
            self._names,
            page,
        )
        LOGGER.debug(
            "Rendered removal picker page; occurrence_id=%s page=%s pages=%s "
            "options=%s",
            self._occurrence.occurrence_id,
            view.page + 1,
            view.page_count,
            len(view.page_signups()),
        )
        await interaction.response.edit_message(
            content=view.prompt(),
            view=view,
        )

    async def back(self, interaction: discord.Interaction) -> None:
        await send_event_preview(self._bot, interaction, self._draft)

    async def _notify_removed_member(
        self,
        event: Event,
        occurrence: EventOccurrence,
        discord_user_id: int,
    ) -> bool:
        """Disable auto sign-up and DM the member; report whether it landed.

        A removal the member never hears about looks like a bug to them, and
        leaving automatic sign-up on would put them straight back onto the next
        occurrence of a recurring event.

        The preceding remove_signup refreshes the public message, which seeds
        the next occurrence when the removal crosses the occurrence's end or
        finds the message gone - seating this member from the preference that
        is still enabled at that point. disable_auto_signup withdraws that
        seat, so the DM's promise holds however the two interleave.
        """
        from gw2bot.events.posting import (
            AutoSignupDisableResult,
            disable_auto_signup,
        )

        auto_disabled = _auto_signup_enabled(self._bot, event, discord_user_id)
        auto_result = AutoSignupDisableResult()
        if auto_disabled:
            auto_result = disable_auto_signup(
                self._bot,
                event,
                occurrence,
                discord_user_id,
            )
            LOGGER.debug(
                "Disabled auto signup on removal; event_id=%s user_id=%s "
                "withdrawn=%s still_seated=%s",
                event.event_id,
                discord_user_id,
                len(auto_result.withdrawn),
                len(auto_result.still_seated),
            )
        return await send_direct_message(
            self._bot,
            discord_user_id,
            _removal_dm_content(
                event,
                occurrence,
                auto_disabled,
                auto_result,
            ),
        )

    async def remove(
        self,
        interaction: discord.Interaction,
        user_ids: list[int],
    ) -> None:
        from gw2bot.events.posting import (
            merge_roster_updates,
            notify_roster_update,
            remove_signup,
        )

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
        if occurrence_has_ended(event, occurrence, datetime.now(UTC)):
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
        updates: list[RosterUpdate] = []
        kept_after_end: list[int] = []
        undelivered: list[int] = []
        for index, user_id in enumerate(user_ids):
            # The picker holds several members and remove_signup awaits Discord
            # I/O between each, so the event can cross its end partway through
            # the loop even though the pre-loop check passed. Re-check every
            # iteration and stop the moment it has ended, so no removal (and no
            # waitlist promotion behind it) ever lands on a finished roster.
            if occurrence_has_ended(event, occurrence, datetime.now(UTC)):
                kept_after_end = list(user_ids[index:])
                LOGGER.debug(
                    "Event ended mid-removal; stopping; occurrence_id=%s "
                    "user_id=%s kept=%s",
                    occurrence.occurrence_id,
                    interaction.user.id,
                    len(kept_after_end),
                )
                break
            # Notification is deferred to a single merged announcement after
            # the loop: per-removal pings would post one thread message per
            # member for what the leader sees as a single edit.
            signup, update = await remove_signup(
                self._bot,
                event,
                occurrence,
                user_id,
                notify=False,
            )
            if signup is None:
                skipped.append(user_id)
                continue
            removed.append(user_id)
            updates.append(update)
            # The thread announcement below never reaches this member: the
            # removal already took them out of the event thread. One member
            # with closed DMs must not stop the rest of the removals, so a
            # failed delivery is only recorded for the summary.
            if not await self._notify_removed_member(
                event,
                occurrence,
                user_id,
            ):
                undelivered.append(user_id)
        # Each removal can promote waitlisted members and flex seated ones, and
        # a member picked alongside their own promoter can be promoted by an
        # earlier iteration and then removed by a later one. Merging collapses
        # each user's changes into one line and drops the ones who ended up off
        # the roster, so the announcement and the summary below both describe
        # the net result.
        merged = merge_roster_updates(updates, removed)
        await notify_roster_update(self._bot, occurrence, merged)
        promoted = [signup.discord_user_id for signup in merged.promoted]
        LOGGER.debug(
            "Applied roster removal; event_id=%s occurrence_id=%s user_id=%s "
            "picked=%s removed=%s not_signed_up=%s promoted=%s kept=%s "
            "undelivered=%s",
            event.event_id,
            occurrence.occurrence_id,
            interaction.user.id,
            len(user_ids),
            len(removed),
            len(skipped),
            len(promoted),
            len(kept_after_end),
            len(undelivered),
        )
        summary = _removal_summary(
            removed,
            skipped,
            promoted,
            kept_after_end,
            undelivered,
        )
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


def _still_seated_note(
    still_seated: Sequence[EventOccurrence],
) -> str | None:
    """Name the already-posted occurrences that keep the member seated.

    disable_auto_signup will not unseat a member from an occurrence that has
    already been posted, because that seat may have been taken deliberately.
    Saying nothing would leave the member believing they are off every future
    roster, so point them at the sign-out button on those posts instead.
    """
    if not still_seated:
        return None
    # Discord timestamps rather than formatted times: these lines are read
    # outside the event channel, so they render in the member's own timezone.
    starts = ", ".join(
        f"<t:{int(occurrence.start_time.timestamp())}:F>"
        for occurrence in still_seated
    )
    if len(still_seated) == 1:
        return (
            "You are still signed up for the next occurrence on "
            f"{starts}, which had already been posted. Use the sign-out "
            "button on its event message if you do not want that spot."
        )
    return (
        f"You are still signed up for later occurrences on {starts}, which "
        "had already been posted. Use the sign-out button on their event "
        "messages if you do not want those spots."
    )


def _removal_dm_content(
    event: Event,
    occurrence: EventOccurrence,
    auto_signup_disabled: bool,
    auto_result: AutoSignupDisableResult | None = None,
) -> str:
    # A Discord timestamp rather than a formatted time: the DM is read outside
    # the event channel, so it renders in the member's own timezone.
    start_epoch = int(occurrence.start_time.timestamp())
    lines = [
        f"An event leader removed you from **{event.title}**, which starts "
        f"on <t:{start_epoch}:F>."
    ]
    if auto_signup_disabled:
        lines.append(
            "Automatic sign-up for this event has been turned off as well, "
            "so you will not be signed up again for its next occurrence. You "
            "can turn it back on with the ⚙️ button on the event message."
        )
        seated = _still_seated_note(
            auto_result.still_seated if auto_result is not None else ()
        )
        if seated is not None:
            lines.append(seated)
    return "\n\n".join(lines)


def _removal_summary(
    removed: list[int],
    skipped: list[int],
    promoted: list[int],
    kept_after_end: list[int] | None = None,
    undelivered: list[int] | None = None,
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
    if undelivered:
        # The removal itself went through; only the courtesy DM did not, which
        # the commander needs to know so they can pass the word along.
        lines.append(
            "Could not send a direct message to "
            + _mention_list(undelivered)
            + ", so they were not notified."
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
        notify_roster_update,
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
        roster_update = RosterUpdate()
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
            # checks both describe the new category, and announce the moves in
            # the occurrence's thread so members learn their new seat.
            try:
                _, roster_update = rebalance_occurrence_roster(
                    bot, updated, current
                )
            except (SQLAlchemyError, ValueError) as exc:
                # A stale roster must not block the rest of the edit.
                LOGGER.error(
                    "Could not rebalance roster after a category change; "
                    "occurrence_id=%s error_type=%s",
                    current.occurrence_id,
                    type(exc).__name__,
                )
                roster_update = RosterUpdate()
            else:
                if not moving:
                    # For an in-place refresh the thread is stable, so announce
                    # the reseat now. A channel move deletes this thread and
                    # opens a new one, so its ping is deferred to after the
                    # repost below and re-targeted at the new thread.
                    await notify_roster_update(bot, current, roster_update)
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
                # reads off the occurrence itself. It deletes the old thread and
                # returns the occurrence carrying the new one, so the deferred
                # roster ping goes there - the old thread the members were
                # notified in no longer exists.
                reposted = await repost_occurrence(bot, updated, current)
                await notify_roster_update(bot, reposted, roster_update)
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
    # normally leaves the status alone (apply_event_edit refuses an event that
    # has started, so the occurrence is always still upcoming here). The post
    # would stay stale until the event actually started. Flag it so the next
    # maintenance pass re-renders it in place, against the channel it really
    # lives in.
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


class EventCancelConfirmView(discord.ui.View):
    """Confirmation for calling off one occurrence of a repeating event.

    Only a repeating event reaches this view: cancelling the single run of an
    event that does not repeat leaves nothing behind, so `/event cancel` sends
    the delete confirmation above for one of those instead.
    """

    def __init__(
        self,
        bot: Gw2Bot,
        event: Event,
        occurrence: EventOccurrence,
    ):
        super().__init__(timeout=FLOW_TIMEOUT_SECONDS)
        self._bot = bot
        self._event = event
        self._occurrence = occurrence
        self._cancelling = False

    @discord.ui.button(
        label="Cancel occurrence",
        style=discord.ButtonStyle.danger,
    )
    async def cancel_occurrence(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[EventCancelConfirmView],
    ) -> None:
        from gw2bot.events.posting import cancel_occurrence

        # The confirmation can sit open for minutes; recheck the role before
        # the irreversible cancel, mirroring the delete/edit/post paths.
        if not user_has_role(interaction.user, EVENT_CREATE_ROLE_ID):
            LOGGER.warning(
                "Rejected event cancel from Discord user %s; required role %s",
                interaction.user.id,
                EVENT_CREATE_ROLE_ID,
            )
            await interaction.response.send_message(
                "You do not have the required role to cancel events.",
                ephemeral=True,
            )
            return
        # Guard a double click racing two callbacks before the first removes
        # the buttons; the check and set are synchronous, so the second
        # observes it.
        if self._cancelling:
            await interaction.response.send_message(
                "This occurrence is already being cancelled.",
                ephemeral=True,
            )
            return
        self._cancelling = True
        # The confirmation holds the event and the occurrence as they were when
        # it was opened, and either can be gone by the time it is answered: the
        # event deleted, or the occurrence pruned or retired. Cancelling from
        # those stale copies would seed a successor for an event that no longer
        # exists, so re-read both and stop if the run has already gone.
        event = self._bot.event_store.get_event(self._event.event_id)
        occurrence = self._bot.event_store.get_occurrence(
            self._occurrence.occurrence_id
        )
        if event is None or event.cancelled or occurrence is None:
            self._cancelling = False
            LOGGER.debug(
                "Event cancel rejected for a run that is already gone; "
                "user_id=%s event_id=%s occurrence_id=%s event_exists=%s",
                interaction.user.id,
                self._event.event_id,
                self._occurrence.occurrence_id,
                event is not None,
            )
            await interaction.response.edit_message(
                content=(
                    "That occurrence is no longer there, so there is nothing "
                    "left to cancel."
                ),
                embeds=[],
                view=None,
            )
            return
        if occurrence.status is EventStatus.OVER or occurrence_has_ended(
            event,
            occurrence,
            datetime.now(UTC),
        ):
            # The run ended while this confirmation sat open. Its row survives
            # a series that keeps its history, but it is a record of something
            # that happened now, not an upcoming run: cancelling would delete
            # the roster and the post of an event people already attended.
            self._cancelling = False
            LOGGER.debug(
                "Event cancel rejected for a finished occurrence; user_id=%s "
                "event_id=%s occurrence_id=%s",
                interaction.user.id,
                event.event_id,
                occurrence.occurrence_id,
            )
            await interaction.response.edit_message(
                content=(
                    "That occurrence has already run, so it can no longer be "
                    "cancelled. Run `/event cancel` again for the next one."
                ),
                embeds=[],
                view=None,
            )
            return
        if event.repeat_frequency is RepeatFrequency.NONE:
            # An edit turned the series into a one-off while this confirmation
            # sat open. Cancelling now would delete the only occurrence and
            # seed nothing, leaving an event row that no occurrence-based
            # lookup can reach any more. Send the commander back to
            # `/event cancel`, which offers deletion for a one-off event.
            self._cancelling = False
            LOGGER.debug(
                "Event cancel rejected for an event that no longer repeats; "
                "user_id=%s event_id=%s occurrence_id=%s",
                interaction.user.id,
                event.event_id,
                occurrence.occurrence_id,
            )
            await interaction.response.edit_message(
                content=(
                    "That event no longer repeats, so this occurrence is all "
                    "there is of it. Run `/event cancel` again to delete the "
                    "event instead."
                ),
                embeds=[],
                view=None,
            )
            return
        self._event = event
        self._occurrence = occurrence
        await interaction.response.edit_message(
            content="Cancelling the occurrence…",
            embeds=[],
            view=None,
        )
        try:
            cancellation = await cancel_occurrence(
                self._bot,
                self._event,
                self._occurrence,
            )
        except (SQLAlchemyError, ValueError) as exc:
            # Nothing is removed until the successor is secured, so the
            # occurrence is still there and the commander can try again. A
            # ValueError only reaches here for a stored event whose repeat
            # settings have no day to repeat on, which has no next start to
            # compute and so can never be cancelled this way.
            self._cancelling = False
            LOGGER.error(
                "Could not cancel event occurrence; event_id=%s "
                "occurrence_id=%s error_type=%s",
                self._event.event_id,
                self._occurrence.occurrence_id,
                type(exc).__name__,
            )
            await interaction.edit_original_response(
                content=(
                    "The occurrence could not be cancelled. Try again later."
                ),
                view=None,
            )
            return
        LOGGER.debug(
            "Cancelled event occurrence from confirmation; event_id=%s "
            "occurrence_id=%s user_id=%s successor_posted=%s",
            self._event.event_id,
            self._occurrence.occurrence_id,
            interaction.user.id,
            cancellation.successor_posted,
        )
        await interaction.edit_original_response(
            content=self._result_message(cancellation),
            view=None,
        )

    def _result_message(self, cancellation: OccurrenceCancellation) -> str:
        title = self._event.title
        cancelled_on = format_event_datetime(
            self._occurrence.start_time,
            self._bot.event_timezone,
        )
        successor = cancellation.successor
        if successor is None:
            # A repeating series always seeds its next run, so this only
            # happens when the store lost it; say so rather than promising a
            # post that is not coming.
            return (
                f"**{title}** on {cancelled_on} was cancelled, but no next "
                "occurrence could be found. Use `/event new` to schedule it "
                "again."
            )
        next_on = format_event_datetime(
            successor.start_time,
            self._bot.event_timezone,
        )
        if not cancellation.successor_posted:
            return (
                f"**{title}** on {cancelled_on} was cancelled, but its next "
                f"occurrence on {next_on} could not be posted in "
                f"<#{self._event.channel_id}>. Check the bot's permissions "
                "there; posting is retried automatically every minute until "
                "it goes through."
            )
        return (
            f"**{title}** on {cancelled_on} was cancelled. The event "
            f"continues on {next_on} in <#{self._event.channel_id}>."
        )

    @discord.ui.button(
        label="Keep occurrence",
        style=discord.ButtonStyle.secondary,
    )
    async def keep(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[EventCancelConfirmView],
    ) -> None:
        await interaction.response.edit_message(
            content="The occurrence was not cancelled.",
            embeds=[],
            view=None,
        )
        LOGGER.debug(
            "Event cancel declined; user_id=%s event_id=%s occurrence_id=%s",
            interaction.user.id,
            self._event.event_id,
            self._occurrence.occurrence_id,
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

# What the step-one preview may change: the details that have been entered by
# then, in the same order as the full list.
_DETAILS_CHANGE_FIELDS = tuple(
    entry
    for entry in _CHANGE_FIELDS
    if entry[0] in ("category", "title", "description", "channel", "leader")
)


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
        # As in the schedule step, the frequency is the repeat modal's to set.
        # Dismissing that modal leaves the previous setting standing, so the
        # still-open preview keeps offering to post what it already shows.
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


def occurrence_has_ended(
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
        if occurrence_has_ended(event, occurrence, datetime.now(UTC)):
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
            view=SignupSettingsView(bot, event, occurrence, interaction.user.id),
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
        if self._action == "edit_signup":
            await view._edit_signup(interaction)
        elif self._action == "enable_auto":
            await view._enable_auto(interaction)
        elif self._action == "disable_auto":
            await view._disable_auto(interaction)
        else:
            await view._reset_preference(interaction)


class SignupSettingsView(discord.ui.View):
    def __init__(
        self,
        bot: Gw2Bot,
        event: Event,
        occurrence: EventOccurrence,
        discord_user_id: int,
    ):
        super().__init__(timeout=FLOW_TIMEOUT_SECONDS)
        self._bot = bot
        self._event = event
        self._occurrence = occurrence
        # The settings message is ephemeral to the clicking member, so the
        # view can be tailored to them: editing a signup only makes sense for
        # someone who has one, and only role-based events have roles to edit.
        if event.capacity.has_roles and (
            bot.event_store.get_signup(
                occurrence.occurrence_id,
                discord_user_id,
            )
            is not None
        ):
            self.add_item(
                _SignupSettingsButton(
                    "Edit my signup",
                    discord.ButtonStyle.primary,
                    "edit_signup",
                )
            )
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

    async def _edit_signup(self, interaction: discord.Interaction) -> None:
        # The settings message can sit open, so re-check the signup and the
        # event's end before opening the pickers.
        signup = self._bot.event_store.get_signup(
            self._occurrence.occurrence_id,
            interaction.user.id,
        )
        if signup is None:
            await interaction.response.edit_message(
                content="You are no longer signed up for this event.",
                view=None,
            )
            return
        if occurrence_has_ended(
            self._event, self._occurrence, datetime.now(UTC)
        ):
            await interaction.response.edit_message(
                content=(
                    "This event has already ended, so your signup can no "
                    "longer be changed."
                ),
                view=None,
            )
            return
        # Pre-check the edit rate limit so a member out of tokens is told
        # before walking through the pickers. apply_signup_edit re-checks
        # authoritatively when the edit lands.
        tokens = available_edit_tokens(signup, datetime.now(UTC))
        if tokens < 1.0:
            LOGGER.debug(
                "Refused signup edit flow over the rate limit; "
                "occurrence_id=%s user_id=%s tokens=%.2f",
                self._occurrence.occurrence_id,
                interaction.user.id,
                tokens,
            )
            await interaction.response.edit_message(
                content=signup_edit_limit_message(tokens),
                view=None,
            )
            return
        LOGGER.debug(
            "Opened signup edit flow; occurrence_id=%s user_id=%s",
            self._occurrence.occurrence_id,
            interaction.user.id,
        )
        flow = EditSignupFlow(
            self._bot,
            self._event,
            self._occurrence,
            interaction.user.id,
        )
        await interaction.response.edit_message(
            content="Pick your new role for this event.",
            view=RolePickView(flow),
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
        # Same reconciliation as the sign-out prompt: the next occurrence may
        # already have been seeded with an automatic signup for this member,
        # and the settings panel would otherwise report automatic sign-up as
        # off while that seat stands.
        from gw2bot.events.posting import disable_auto_signup

        result = disable_auto_signup(
            self._bot,
            self._event,
            self._occurrence,
            interaction.user.id,
        )
        LOGGER.debug(
            "Disabled auto signup from settings; event_id=%s "
            "occurrence_id=%s user_id=%s withdrawn=%s still_seated=%s",
            self._event.event_id,
            self._occurrence.occurrence_id,
            interaction.user.id,
            len(result.withdrawn),
            len(result.still_seated),
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


def _auto_signup_enabled(
    bot: Gw2Bot,
    event: Event,
    discord_user_id: int,
) -> bool:
    # Only a repeating event has future occurrences to be signed up for, so a
    # stored choice on a one-off event is inert. NEVER_ASK and NO both leave
    # automatic sign-up off, and there is nothing to disable in either case.
    if event.repeat_frequency is RepeatFrequency.NONE:
        return False
    auto = bot.event_store.get_auto_signup(event.event_id, discord_user_id)
    return auto is not None and auto.choice is AutoSignupChoice.YES


class DisableAutoSignupView(discord.ui.View):
    """Offers to switch off automatic sign-up after a member signs out.

    Without this, signing out of a recurring event looks like it did nothing:
    apply_auto_signups seats the member again as soon as the next occurrence is
    seeded, and the only way to stop it is the settings gear.
    """

    def __init__(
        self,
        bot: Gw2Bot,
        event: Event,
        occurrence: EventOccurrence,
        discord_user_id: int,
    ):
        super().__init__(timeout=FLOW_TIMEOUT_SECONDS)
        self._bot = bot
        self._event = event
        self._occurrence = occurrence
        self._discord_user_id = discord_user_id

    @discord.ui.button(
        label="Yes, turn it off",
        style=discord.ButtonStyle.primary,
    )
    async def disable_auto(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[DisableAutoSignupView],
    ) -> None:
        # This prompt can sit open long enough for the scheduler to seed the
        # next occurrence, and the sign-out that opened it can itself have
        # seeded one by crossing this occurrence's end. Either way the next
        # roster already holds the automatic signup this button is meant to
        # prevent, so storing the choice alone would confirm something untrue.
        # disable_auto_signup reconciles those occurrences with the choice.
        from gw2bot.events.posting import disable_auto_signup

        result = disable_auto_signup(
            self._bot,
            self._event,
            self._occurrence,
            self._discord_user_id,
        )
        LOGGER.debug(
            "Disabled auto signup after sign out; event_id=%s "
            "occurrence_id=%s user_id=%s withdrawn=%s still_seated=%s",
            self._event.event_id,
            self._occurrence.occurrence_id,
            self._discord_user_id,
            len(result.withdrawn),
            len(result.still_seated),
        )
        lines = [
            "Automatic sign-up is off for this event. You can turn it "
            "back on with the ⚙️ button on the event message."
        ]
        if result.withdrawn:
            lines.append(
                "The next occurrence had already been created and had signed "
                "you up automatically, so you were taken off it too."
            )
        seated = _still_seated_note(result.still_seated)
        if seated is not None:
            lines.append(seated)
        await interaction.response.edit_message(
            content="\n\n".join(lines),
            view=None,
        )

    @discord.ui.button(
        label="No, keep it on",
        style=discord.ButtonStyle.secondary,
    )
    async def keep_auto(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[DisableAutoSignupView],
    ) -> None:
        LOGGER.debug(
            "Kept auto signup after sign out; event_id=%s user_id=%s",
            self._event.event_id,
            self._discord_user_id,
        )
        await interaction.response.edit_message(
            content=(
                "Automatic sign-up stays on, so you will be signed up again "
                "for the next occurrence of this event."
            ),
            view=None,
        )


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
        if occurrence_has_ended(
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
        removed, update = await remove_signup(
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
            "promoted=%s reassigned=%s",
            self._occurrence.occurrence_id,
            interaction.user.id,
            removed is not None,
            len(update.promoted),
            len(update.reassigned),
        )
        # Signing out only clears this occurrence. Leaving automatic sign-up on
        # would quietly re-seat the member on the next one, so offer to switch
        # it off while they are still looking at the confirmation.
        prompt: DisableAutoSignupView | None = None
        if removed is not None and _auto_signup_enabled(
            self._bot,
            self._event,
            interaction.user.id,
        ):
            prompt = DisableAutoSignupView(
                self._bot,
                self._event,
                self._occurrence,
                interaction.user.id,
            )
            content += (
                "\n\nAutomatic sign-up is still on for this event, so you "
                "will be signed up again for its next occurrence. Would you "
                "like to turn it off?"
            )
            LOGGER.debug(
                "Offered auto signup disable after sign out; event_id=%s "
                "occurrence_id=%s user_id=%s",
                self._event.event_id,
                self._occurrence.occurrence_id,
                interaction.user.id,
            )
        await interaction.edit_original_response(content=content, view=prompt)

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
    if occurrence_has_ended(event, occurrence, now):
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

    def roster_for_labels(self) -> list[EventSignup]:
        # The signups the role-picker labels are computed against; an edit
        # flow narrows this (see EditSignupFlow).
        return self.bot.event_store.get_signups(
            self.occurrence.occurrence_id
        )

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


class EditSignupFlow(SignupFlow):
    # Drives the same role and flex pickers as a fresh signup, but ends in
    # apply_signup_edit: the member's signup row (and its signed_up_at, which
    # decides seating priority) survives, so editing never costs the seat or
    # queue position that signing out and rejoining would.

    def roster_for_labels(self) -> list[EventSignup]:
        # The editor's own seat is being re-picked, so it must not count
        # against the labels: a role that reads "(full)" only because the
        # editor currently holds (or blocks) it is one they can freely pick.
        return [
            signup
            for signup in super().roster_for_labels()
            if signup.discord_user_id != self.discord_user_id
        ]

    async def continue_after_roles(
        self,
        interaction: discord.Interaction,
    ) -> None:
        # No remember-my-roles or auto-signup prompts on an edit; those
        # belong to the first-time signup flow.
        await self.finalize(interaction)

    async def finalize(self, interaction: discord.Interaction) -> None:
        await self.apply(interaction, allow_waitlist=False)

    async def apply(
        self,
        interaction: discord.Interaction,
        *,
        allow_waitlist: bool,
    ) -> None:
        from gw2bot.events.posting import apply_signup_edit

        if interaction.response.is_done():
            edit = interaction.edit_original_response
        elif _is_ephemeral_component_interaction(interaction):
            await interaction.response.edit_message(
                content="Updating your signup…",
                view=None,
            )
            edit = interaction.edit_original_response
        else:
            await interaction.response.defer(ephemeral=True, thinking=True)
            edit = interaction.edit_original_response
        if self.role is None:
            # The pickers always set a role before finalize; a missing one
            # means the flow was driven out of order.
            await edit(
                content="Pick a role first, then apply the change.",
                view=None,
            )
            return
        # A leader can move or edit the event while this flow sits open, which
        # repoints the occurrence at a new message and thread (a channel move)
        # or changes the capacity the roster is seated against (a category
        # change). Re-load both by their stable ids so the edit lands on live
        # state rather than, say, refreshing a message that was already deleted
        # - which refresh_occurrence_message handles by retiring the live
        # occurrence as gone.
        event = self.bot.event_store.get_event(self.event.event_id)
        occurrence = self.bot.event_store.get_occurrence(
            self.occurrence.occurrence_id
        )
        if event is None or occurrence is None:
            await edit(content="This event no longer exists.", view=None)
            return
        self.event = event
        self.occurrence = occurrence
        try:
            result = await apply_signup_edit(
                self.bot,
                event,
                occurrence,
                self.discord_user_id,
                self.role,
                self.flex_roles,
                allow_waitlist=allow_waitlist,
            )
        except ValueError as error:
            await edit(content=str(error), view=None)
            return
        if result.needs_waitlist_confirmation:
            await edit(
                content=(
                    "Your new selection does not fit the current roster, so "
                    "applying it would move you from your seat to the "
                    "**waitlist**. Apply it anyway?"
                ),
                view=EditWaitlistConfirmView(self),
            )
            return
        signup = result.signup
        if signup is None:
            # apply_signup_edit returns a row whenever it applied; this
            # branch only exists to satisfy the optional type.
            await edit(content="Your signup was updated.", view=None)
            return
        content = _signup_edit_summary(signup)
        preference = self.bot.event_store.get_signup_preference(
            self.discord_user_id
        )
        # A member with remembered roles just declared a different
        # selection; offer to bring the memory along so their next signup
        # does not resurrect the old roles.
        if (
            preference is not None
            and preference.mode is PreferenceMode.REMEMBER
            and (
                preference.role is not self.role
                or set(preference.flex_roles) != set(self.flex_roles)
            )
        ):
            await edit(
                content=(
                    f"{content}\n\nYour remembered roles still hold your "
                    "old selection. Update them to this new one?"
                ),
                view=UpdateRememberedRolesView(self),
            )
            return
        await edit(content=content, view=None)


class EditWaitlistConfirmView(discord.ui.View):
    def __init__(self, flow: EditSignupFlow):
        super().__init__(timeout=FLOW_TIMEOUT_SECONDS)
        self._flow = flow

    @discord.ui.button(
        label="Apply and join the waitlist",
        style=discord.ButtonStyle.danger,
    )
    async def apply_anyway(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[EditWaitlistConfirmView],
    ) -> None:
        # The roster may have changed while this confirmation sat open;
        # apply re-reads it, so a selection that fits by now keeps the seat.
        await self._flow.apply(interaction, allow_waitlist=True)

    @discord.ui.button(
        label="Keep my current signup",
        style=discord.ButtonStyle.secondary,
    )
    async def keep(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[EditWaitlistConfirmView],
    ) -> None:
        LOGGER.debug(
            "Signup edit abandoned at the waitlist confirmation; "
            "occurrence_id=%s user_id=%s",
            self._flow.occurrence.occurrence_id,
            self._flow.discord_user_id,
        )
        await interaction.response.edit_message(
            content="Your signup was left unchanged.",
            view=None,
        )


class UpdateRememberedRolesView(discord.ui.View):
    def __init__(self, flow: EditSignupFlow):
        super().__init__(timeout=FLOW_TIMEOUT_SECONDS)
        self._flow = flow

    @discord.ui.button(
        label="Yes, remember these roles",
        style=discord.ButtonStyle.success,
    )
    async def update(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[UpdateRememberedRolesView],
    ) -> None:
        self._flow.bot.event_store.set_signup_preference(
            self._flow.discord_user_id,
            self._flow.role,
            self._flow.flex_roles,
            PreferenceMode.REMEMBER,
        )
        LOGGER.debug(
            "Updated remembered roles after a signup edit; user_id=%s "
            "role=%s flex_count=%s",
            self._flow.discord_user_id,
            self._flow.role.value if self._flow.role is not None else None,
            len(self._flow.flex_roles),
        )
        await interaction.response.edit_message(
            content="Your remembered roles were updated.",
            view=None,
        )

    @discord.ui.button(
        label="No, keep the old ones",
        style=discord.ButtonStyle.secondary,
    )
    async def keep(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[UpdateRememberedRolesView],
    ) -> None:
        await interaction.response.edit_message(
            content="Your remembered roles were left unchanged.",
            view=None,
        )


def _signup_edit_summary(signup: EventSignup) -> str:
    if signup.waitlisted:
        return (
            "Your signup was updated. Your new selection does not currently "
            "fit, so you are on the **waitlist** - you keep your original "
            "sign-up priority."
        )
    if signup.assigned_role is not None:
        summary = (
            "Your signup was updated. You are seated as "
            f"**{signup.assigned_role.value}**."
        )
        if signup.role is not None and signup.assigned_role != signup.role:
            summary += (
                f" Your preferred role **{signup.role.value}** is taken, so "
                "one of your flex roles is used."
            )
        return summary
    return "Your signup was updated."


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
        signups = flow.roster_for_labels()
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
