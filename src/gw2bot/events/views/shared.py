"""The pieces every event flow is built from.

The draft a flow carries, the option builders its selects are filled from, the
base views its modals and confirmations subclass, and the limits Discord
imposes on all of them. This module is the leaf of the package: it never
imports a flow back.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, TYPE_CHECKING
from zoneinfo import ZoneInfo

import discord
from sqlalchemy.exc import SQLAlchemyError

from gw2bot.core.discord_utils import safe_int
from gw2bot.events.formatting import (
    compute_status,
    describe_ping_roles,
    format_duration_input,
    format_event_datetime,
    format_repeat_days,
    message_link,
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
    MAX_PING_ROLES,
    PING_ROLE_PREFIX,
    RepeatFrequency,
    is_pingable_role_name,
    is_roster_full,
)

if TYPE_CHECKING:
    from gw2bot.bot import Gw2Bot

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


FINISHED_EDIT_REJECTION = (
    "That event has no runs left, so its details can no longer be changed. "
    "Its finished runs keep the details they were run under."
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


# The same cap applied to the ping-role picker. Every offered role is opt-in
# and named for a kind of run, so a server is not expected to hold more than
# a handful; a server that somehow does gets the first page of them rather
# than a picker Discord would refuse outright.
PING_ROLE_OPTION_LIMIT = 25


PING_ROLE_PROMPT = "Which roles should the post ping?"


# Discord caps a Label's text at 45 characters, so the details ride along in
# the Label description, as the channel picker's hint does.
PING_ROLE_HINT = (
    f"Optional. Up to {MAX_PING_ROLES} roles named {PING_ROLE_PREFIX}…"
)


PING_ROLE_NONE_AVAILABLE = (
    f"No roles in this server start with `{PING_ROLE_PREFIX}`, so there is "
    "nothing to ping. Ask an admin to create one, then try again."
)


# Discord's cap on a select option's label; a longer display name is truncated
# rather than rejected by the API.
REMOVE_OPTION_LABEL_MAX_LENGTH = 100


# Discord's cap on how many members one user select may return, which is what
# bounds a single pass of the manual sign-up picker. A commander needing more
# than this adds them over several passes.
ADD_SELECT_MAX_MEMBERS = 25


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
    # Roles the event post pings, in the order they were picked.
    ping_role_ids: tuple[int, ...] = field(default_factory=tuple)
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
            ping_role_ids=self.ping_role_ids,
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
        ping_role_ids=event.ping_role_ids,
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


def pingable_roles(guild: discord.Guild | None) -> list[discord.Role]:
    """The server's opt-in ping roles, in the order the picker offers them.

    Only roles whose name carries the marker are offered, so a commander cannot
    aim an event post at an admin role or at @everyone. Sorting by name keeps
    the picker stable as the server's role hierarchy is rearranged.
    """
    if guild is None:
        return []
    roles = [role for role in guild.roles if is_pingable_role_name(role.name)]
    roles.sort(key=lambda role: (role.name.casefold(), role.id))
    return roles


def _ping_role_options(
    guild: discord.Guild | None,
    selected: Sequence[int],
) -> list[discord.SelectOption]:
    """Options for the ping-role picker, with the draft's roles pre-selected.

    A role already on the draft is offered even when it no longer carries the
    marker, because it was a valid choice when it was picked and an unrelated
    edit must not quietly drop it. One that no longer exists in the server is
    dropped: Discord would refuse a mention of it anyway.
    """
    roles = pingable_roles(guild)
    known = {role.id for role in roles}
    # A retained role is listed first so the truncation below can never be what
    # drops one of the event's own roles.
    retained: list[discord.Role] = []
    for role_id in selected:
        if role_id in known or guild is None:
            continue
        role = guild.get_role(role_id)
        if role is not None:
            retained.append(role)
    offered = retained + roles
    if len(offered) > PING_ROLE_OPTION_LIMIT:
        LOGGER.debug(
            "Truncating the ping role picker; roles=%s offered=%s",
            len(offered),
            PING_ROLE_OPTION_LIMIT,
        )
        offered = offered[:PING_ROLE_OPTION_LIMIT]
    chosen = set(selected)
    return [
        discord.SelectOption(
            label=role.name[:REMOVE_OPTION_LABEL_MAX_LENGTH],
            value=str(role.id),
            default=role.id in chosen,
        )
        for role in offered
    ]


def _picked_ping_role_ids(
    values: Sequence[str],
    options: Sequence[discord.SelectOption],
) -> tuple[int, ...]:
    """Read a ping-role pick, keeping only what the picker actually offered.

    Every value was minted from a role id by _ping_role_options, so anything
    else is a payload the bot did not build - a submission is a client-supplied
    message, and this is the boundary that decides which roles an event may
    carry rather than trusting what came back. The check is against the offered
    options, not the marker: a role that was retained because the event already
    pings it stays pickable here, and whether it may still be *notified* is
    settled against the server at send time.
    """
    offered = {option.value for option in options}
    picked: list[int] = []
    for value in values:
        if value not in offered:
            LOGGER.warning("Ignoring a picked role that was not offered")
            continue
        role_id = safe_int(value)
        if role_id is None:
            LOGGER.warning("Ignoring an unreadable picked role value")
            continue
        picked.append(role_id)
    return tuple(picked[:MAX_PING_ROLES])


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


def _append_ping_note(embed: discord.Embed, draft: EventDraft) -> None:
    # The mentions render as role chips here without notifying anybody: Discord
    # never pings for a mention inside an embed, which is exactly why the real
    # post carries them as message content instead.
    embed.description = (
        f"{embed.description}\n\n*{describe_ping_roles(draft.ping_role_ids)}.*"
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


class _EditFlowView(discord.ui.View):
    """Base for the views that sit on a draft, carrying no buttons of its own.

    The in-progress roster editor deliberately does not inherit the
    "Change something" button, so that lives one level down rather than here.
    """

    def __init__(self, bot: Gw2Bot, draft: EventDraft):
        super().__init__(timeout=FLOW_TIMEOUT_SECONDS)
        self._bot = bot
        self._draft = draft


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


def _event_message_link(
    guild_id: int | None,
    event: Event,
    occurrence: EventOccurrence,
) -> str | None:
    """The jump link to an occurrence's public message, when it has one.

    An occurrence the scheduler has not posted yet - a recurring series' next
    run - has no message to point at, and an interaction from outside a server
    carries no guild to address one with, so both give up the link rather than
    build a broken one.
    """
    from gw2bot.events.posting import occurrence_channel_id

    if guild_id is None or occurrence.message_id is None:
        return None
    return message_link(
        guild_id,
        occurrence_channel_id(event, occurrence),
        occurrence.message_id,
    )


def _link_label(title: str) -> str:
    # A ] anywhere in the title would close the link text early and spill the
    # raw URL onto the member's screen, so the brackets are escaped.
    return (
        title.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
    )


def _is_waitlisted(bot: Gw2Bot, occurrence_id: int, user_id: int) -> bool:
    seat = bot.event_store.get_signup(occurrence_id, user_id)
    return seat is not None and seat.waitlisted


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
            ping_role_ids=event.ping_role_ids,
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


async def _send_flow_decline(
    interaction: discord.Interaction,
    content: str,
    *,
    workflow: str,
    event_id: int,
) -> None:
    """Answer a confirmation that was declined, tolerating a Discord failure.

    Nothing was changed, so a failure here is only the wording. It must not
    escape the callback: discord.py's default handler logs the exception with
    its full text, and an HTTPException's text carries Discord's raw response
    body. Reporting the decline is also what puts it in the diagnostic trail,
    so that is logged either way.
    """
    try:
        await interaction.response.edit_message(
            content=content,
            embeds=[],
            view=None,
        )
    except discord.HTTPException as exc:
        LOGGER.error(
            "Could not answer a declined %s; event_id=%s error_type=%s",
            workflow,
            event_id,
            type(exc).__name__,
        )


async def _send_flow_result(
    interaction: discord.Interaction,
    content: str,
    *,
    workflow: str,
    event_id: int,
) -> None:
    """Replace a confirmation's own message with what became of the request.

    The work these report on is already done, so a Discord failure here costs
    the commander the wording and nothing else. It must not escape the button
    callback either: discord.py's default handler logs the exception's full
    text, which for an HTTPException carries Discord's raw response body, and
    only sanitized identifiers and exception types may reach the log.
    """
    try:
        await interaction.edit_original_response(content=content, view=None)
    except discord.HTTPException as exc:
        LOGGER.error(
            "Could not report the %s result; event_id=%s error_type=%s",
            workflow,
            event_id,
            type(exc).__name__,
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
    ("ping_roles", "Roles to ping"),
)


# What the step-one preview may change: the details that have been entered by
# then, in the same order as the full list.
_DETAILS_CHANGE_FIELDS = tuple(
    entry
    for entry in _CHANGE_FIELDS
    if entry[0]
    in ("category", "title", "description", "channel", "leader", "ping_roles")
)


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
