"""Creating an event, and confirming the details of one being edited.

Three modals collect the draft a step at a time, each retryable on its own,
and the confirm views are where a finished draft is written. The leader is
signed up for the event as it is posted, so a role-based one asks for their
roles first.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import discord
from sqlalchemy.exc import SQLAlchemyError

from gw2bot.core.discord_utils import user_has_role
from gw2bot.events.audit import event_created_audit, send_event_audit
from gw2bot.events.formatting import (
    EVENT_DATETIME_PLACEHOLDER,
    parse_event_datetime,
    parse_event_duration,
    parse_repeat_days,
)
from gw2bot.events.models import (
    CategoryCapacity,
    Event,
    EventCategory,
    EventRole,
    EventSignup,
    MAX_PING_ROLES,
    ROLE_EMOJI,
    RepeatFrequency,
    supported_roles,
)
from gw2bot.events.views.lifecycle import (
    ChannelMoveConfirmView,
    apply_event_edit,
)
from gw2bot.events.views.preview import _PreviewConfirmView, send_event_preview
from gw2bot.events.views.roster import (
    open_roster_addition,
    open_roster_removal,
)
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
    MENTEE_SLOT_HINT,
    MENTEE_SLOT_PROMPT,
    PING_ROLE_HINT,
    PING_ROLE_PROMPT,
    RepeatAttempt,
    _DETAILS_CHANGE_FIELDS,
    _ModalOpenView,
    _category_options,
    _destination_error,
    _frequency_options,
    _is_ephemeral_component_interaction,
    _live_occurrences,
    _picked_ping_role_ids,
    _ping_role_options,
    _send_validation_error,
    _yes_no_options,
)

if TYPE_CHECKING:
    from gw2bot.bot import Gw2Bot

LOGGER = logging.getLogger(__name__)


LEADER_ROLE_PROMPT = (
    "You lead this event, so you are signed up for it when it posts. Pick "
    "your role for it."
)


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
    def __init__(
        self,
        bot: Gw2Bot,
        draft: EventDraft,
        guild: discord.Guild | None = None,
    ):
        super().__init__()
        self._bot = bot
        self._draft = draft
        self._guild = guild
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
        # The fifth and last component Discord allows a modal, which is what
        # lets the ping roles be asked here rather than behind
        # "Change something". With no pingable roles there is no picker at
        # all: Discord refuses a select with no options, and there is nothing
        # to choose from anyway.
        options = _ping_role_options(bot, guild, draft.ping_role_ids)
        self.ping_roles: discord.ui.Select[EventDetailsModal] | None = None
        if options:
            self.ping_roles = discord.ui.Select["EventDetailsModal"](
                options=options,
                min_values=0,
                max_values=min(MAX_PING_ROLES, len(options)),
                required=False,
            )
            self.add_item(
                discord.ui.Label(
                    text=PING_ROLE_PROMPT,
                    description=PING_ROLE_HINT,
                    component=self.ping_roles,
                )
            )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        self._draft.category = EventCategory(self.category.values[0])
        self._draft.title = self.title_input.value.strip()
        self._draft.description = self.description_input.value.strip()
        if self.ping_roles is not None:
            # The picker was shown, so what it returns is the whole answer -
            # including an empty one, which clears roles picked earlier.
            self._draft.ping_role_ids = _picked_ping_role_ids(
                self.ping_roles.values,
                self.ping_roles.options,
            )
        destination = self.channel.values[0]
        rejection = await _destination_error(self._bot, destination)
        if rejection is not None:
            # The title, description and category are already on the draft, so
            # the retry modal opens pre-filled and only the destination has to be
            # picked again.
            await _send_validation_error(
                interaction,
                ValueError(rejection),
                RetryDetailsView(self._bot, self._draft, self._guild),
            )
            return
        self._draft.channel_id = destination.id
        LOGGER.debug(
            "Event details step submitted; user_id=%s category=%s "
            "title_characters=%s description_characters=%s ping_roles=%s",
            interaction.user.id,
            self._draft.category.value,
            len(self._draft.title),
            len(self._draft.description),
            len(self._draft.ping_role_ids),
        )
        await send_event_preview(self._bot, interaction, self._draft)


class RetryDetailsView(_ModalOpenView):
    def __init__(
        self,
        bot: Gw2Bot,
        draft: EventDraft,
        guild: discord.Guild | None = None,
    ):
        super().__init__(bot, draft, "Try again")
        self._guild = guild

    def build_modal(self) -> discord.ui.Modal:
        return EventDetailsModal(self._bot, self._draft, self._guild)


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
        # Asked here rather than with the details: that modal already holds
        # the five components Discord allows once ping roles are offered.
        self.requirements_input = discord.ui.TextInput["EventScheduleModal"](
            style=discord.TextStyle.paragraph,
            default=draft.requirements or None,
            max_length=EVENT_REQUIREMENTS_MAX_LENGTH,
            required=False,
        )
        self.add_item(
            discord.ui.Label(
                text=EVENT_REQUIREMENTS_PROMPT,
                description=EVENT_REQUIREMENTS_HINT,
                component=self.requirements_input,
            )
        )
        # The fifth and last component Discord allows a modal. Pre-selected
        # from the draft, so a commander who wants no mentee can pass it by.
        self.mentee = discord.ui.Select["EventScheduleModal"](
            options=_yes_no_options(draft.mentee_enabled),
        )
        self.add_item(
            discord.ui.Label(
                text=MENTEE_SLOT_PROMPT,
                description=MENTEE_SLOT_HINT,
                component=self.mentee,
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        self._draft.start_text = self.start_input.value.strip()
        self._draft.duration_text = self.duration_input.value.strip()
        self._draft.requirements = self.requirements_input.value.strip()
        self._draft.mentee_enabled = self.mentee.values[0] == "yes"
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
            "duration_minutes=%s requirements_characters=%s "
            "mentee_enabled=%s",
            interaction.user.id,
            repeats,
            duration_minutes,
            len(self._draft.requirements),
            self._draft.mentee_enabled,
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
        if not await self._ready_to_post(interaction):
            return
        # The leader is signed up for their own event, so a role-based one
        # asks for their roles first; the post goes out once they answer.
        if self._posted_by_leader(interaction) and (
            self._draft.to_event().capacity.has_roles
        ):
            await self._ask_leader_roles(interaction)
            return
        await self.post_with_leader(interaction, None, ())

    def _posted_by_leader(self, interaction: discord.Interaction) -> bool:
        # Only the leader can say which roles they will play, so a commander
        # who handed the draft to somebody else from "Change something" posts
        # it without signing anybody up.
        return self._draft.leader_discord_id == interaction.user.id

    async def _ask_leader_roles(
        self,
        interaction: discord.Interaction,
        content: str = LEADER_ROLE_PROMPT,
    ) -> None:
        LOGGER.debug(
            "Asking the leader for their roles before posting; user_id=%s "
            "category=%s",
            interaction.user.id,
            self._draft.category.value
            if self._draft.category is not None
            else None,
        )
        # Only the controls change: the preview stays above them, so the
        # leader still sees what they are about to post.
        await interaction.response.edit_message(
            content=content,
            view=LeaderRolePickView(self),
        )

    async def _ready_to_post(
        self,
        interaction: discord.Interaction,
        *,
        from_picker: bool = False,
    ) -> bool:
        """Whether the draft may be posted now, answering the click if not.

        Asked when Post event is pressed and again when the leader's roles
        come back, because those pickers can sit open as long as the preview.
        A picker has taken the preview's own controls, so a refusal answered
        from one puts them back where the commander can still put it right.
        """

        async def refuse(content: str, *, correctable: bool) -> bool:
            if not from_picker:
                await interaction.response.send_message(
                    content,
                    ephemeral=True,
                )
                return False
            LOGGER.debug(
                "Refused a post from the leader's role picker; user_id=%s "
                "controls_restored=%s",
                interaction.user.id,
                correctable,
            )
            await interaction.response.edit_message(
                content=content,
                view=self._live_controls() if correctable else None,
            )
            return False

        # The preview can sit open for minutes; the creator role may have
        # been revoked since /event new, so recheck before the irreversible
        # save/post path.
        if not user_has_role(
            interaction.user,
            self._bot._config.event_create_role_id,
        ):
            LOGGER.warning(
                "Rejected event post from Discord user %s; required role %s",
                interaction.user.id,
                self._bot._config.event_create_role_id,
            )
            return await refuse(
                "You do not have the required role to create events.",
                correctable=False,
            )
        if self._draft.posted:
            return await refuse(
                "This event was already posted.",
                correctable=False,
            )
        if not self._draft.is_complete():
            return await refuse(
                "The event is missing required details. Use "
                "**Change something** to fill them in.",
                correctable=True,
            )
        start_time = self._draft.start_time
        if start_time is not None and start_time <= datetime.now(UTC):
            return await refuse(
                "The event start is no longer in the future. Use "
                "**Change something** to update the date and time.",
                correctable=True,
            )
        return True

    async def post_with_leader(
        self,
        interaction: discord.Interaction,
        role: EventRole | None,
        flex_roles: tuple[EventRole, ...],
        *,
        from_picker: bool = False,
    ) -> None:
        """Save and post the draft with its leader already signed up."""
        from gw2bot.events.posting import (
            delete_event_posts,
            post_occurrence,
            seat_event_leader,
            update_thread_membership,
        )

        if not await self._ready_to_post(interaction, from_picker=from_picker):
            return
        signs_up_leader = self._posted_by_leader(interaction)
        if (
            signs_up_leader
            and self._draft.to_event().capacity.has_roles
            and role is None
        ):
            # The category was changed to a role-based one from the preview
            # while the leader had the post on its way without roles.
            await self._ask_leader_roles(
                interaction,
                "This event now has roles. " + LEADER_ROLE_PROMPT,
            )
            return
        self._draft.posted = True
        # Clears the role prompt as well as the controls, so a failure below
        # puts the preview back exactly as Post event left it.
        await interaction.response.edit_message(content=None, view=None)
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
                ping_role_ids=draft_event.ping_role_ids,
                requirements=draft_event.requirements,
                mentee_enabled=draft_event.mentee_enabled,
                max_participants=draft_event.max_participants,
            )
            occurrence = self._bot.event_store.create_occurrence(
                event.event_id,
                event.start_time,
            )
            # Before the post, so the first render already lists the leader
            # and nobody sees a roster without them.
            leader_signup = (
                seat_event_leader(
                    self._bot,
                    event,
                    occurrence,
                    role,
                    flex_roles,
                )
                if signs_up_leader
                else None
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
            posted = await post_occurrence(self._bot, event, occurrence)
        except (discord.HTTPException, SQLAlchemyError, ValueError) as exc:
            # A ValueError means this event's rows went away while the message
            # was in flight, because someone deleted or cancelled it in that
            # window. post_occurrence has already removed the message it sent,
            # and the cleanup below still applies: whatever is left of the
            # event goes, so nothing half-posted survives.
            self._draft.posted = False
            await self._restore_post_controls(interaction)
            LOGGER.error(
                "Could not post event; user_id=%s error_type=%s",
                interaction.user.id,
                type(exc).__name__,
            )
            # Remove the stored rows so retrying cannot create duplicate
            # events and the scheduler cannot resurrect this occurrence. Read
            # the occurrences first: a cancellation racing this post can have
            # seeded and posted a successor, whose message would otherwise be
            # left in the channel with its rows gone.
            occurrences = self._bot.event_store.get_event_occurrences(
                event.event_id
            )
            try:
                self._bot.event_store.delete_event(event.event_id)
            except SQLAlchemyError as cleanup_exc:
                LOGGER.error(
                    "Could not clean up unposted event; event_id=%s "
                    "error_type=%s",
                    event.event_id,
                    type(cleanup_exc).__name__,
                )
            else:
                await delete_event_posts(self._bot, event, occurrences)
            await interaction.followup.send(
                "The event could not be posted to the selected channel. "
                "Check the bot's permissions there and try again.",
                ephemeral=True,
            )
            return
        if leader_signup is not None:
            # The thread was opened by the post, so the leader is subscribed
            # to it now, the way a member pressing Sign up would be.
            await update_thread_membership(
                self._bot,
                posted,
                event.leader_discord_id,
                add=True,
            )
        LOGGER.debug(
            "Event posted from preview; event_id=%s occurrence_id=%s "
            "user_id=%s leader_signed_up=%s",
            event.event_id,
            occurrence.occurrence_id,
            interaction.user.id,
            leader_signup is not None,
        )
        # Before the reply rather than after it: a reply Discord refuses would
        # otherwise take the record of an event that is up with it.
        await send_event_audit(
            self._bot,
            event_created_audit(
                interaction.user.id,
                event,
                occurrence.start_time,
            ),
        )
        await interaction.followup.send(
            f"Event **{event.event_id}** was posted in "
            f"<#{event.channel_id}>. "
            f"{_leader_signup_note(event, leader_signup)}",
            ephemeral=True,
        )

    def _live_controls(self) -> EventConfirmView:
        """This view, or a fresh one over the same draft once it timed out.

        The leader's pickers carry their own timeout, so the preview they
        replaced can expire while they are open, and discord.py never routes
        a click to a view that has. Putting that one back would show buttons
        that do nothing.
        """
        if not self.is_finished():
            return self
        LOGGER.debug(
            "Rebuilt the post controls after their view timed out; "
            "user_id=%s",
            self._draft.leader_discord_id,
        )
        return EventConfirmView(self._bot, self._draft)

    async def _restore_post_controls(
        self,
        interaction: discord.Interaction,
    ) -> None:
        # The preview buttons are removed before saving/posting; on failure
        # put them back so the user can retry from the same message instead
        # of restarting /event new. A failure here is logged but must not
        # mask the original error being reported to the user.
        try:
            await interaction.edit_original_response(
                view=self._live_controls()
            )
        except discord.HTTPException as exc:
            LOGGER.error(
                "Could not restore post controls; user_id=%s error_type=%s",
                interaction.user.id,
                type(exc).__name__,
            )


def _leader_signup_note(event: Event, signup: EventSignup | None) -> str:
    if signup is None:
        return (
            f"<@{event.leader_discord_id}> leads it and was not signed up "
            "for it, since only they can pick their roles. They can sign up "
            "with **Sign up** on the post."
        )
    if signup.assigned_role is not None:
        note = (
            "You lead it, so you are signed up for it as "
            f"**{signup.assigned_role.value}**."
        )
    else:
        note = "You lead it, so you are signed up for it."
    if event.repeat_frequency is RepeatFrequency.NONE:
        return note
    return (
        f"{note} You will be signed up for every run of it automatically; "
        "the ⚙️ button on the event message turns that off."
    )


class LeaderRoleSelect(discord.ui.Select["LeaderRolePickView"]):
    def __init__(self, capacity: CategoryCapacity):
        # The roster is empty until the post, so every role the category
        # seats is open and none needs the "(full)" labels a sign-up shows.
        supported = supported_roles(capacity)
        super().__init__(
            placeholder="Pick your role",
            options=[
                discord.SelectOption(
                    label=role.value,
                    value=role.value,
                    emoji=ROLE_EMOJI[role],
                )
                for role in EventRole
                if role in supported
            ],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if view is not None:
            await view.pick(interaction, EventRole(self.values[0]))


class LeaderRolePickView(discord.ui.View):
    """The leader's role for a new event, asked before it is posted."""

    def __init__(self, confirm: EventConfirmView):
        super().__init__(timeout=FLOW_TIMEOUT_SECONDS)
        self._confirm = confirm
        self.add_item(LeaderRoleSelect(confirm._draft.to_event().capacity))

    async def pick(
        self,
        interaction: discord.Interaction,
        role: EventRole,
    ) -> None:
        LOGGER.debug(
            "Leader role picked before posting; user_id=%s role=%s",
            interaction.user.id,
            role.value,
        )
        capacity = self._confirm._draft.to_event().capacity
        if not _flex_role_options(capacity, role):
            # Nothing else to flex to, and Discord refuses an empty select.
            await self._confirm.post_with_leader(
                interaction,
                role,
                (),
                from_picker=True,
            )
            return
        await interaction.response.edit_message(
            content="Select flex roles",
            view=LeaderFlexRolesView(self._confirm, capacity, role),
        )


def _flex_role_options(
    capacity: CategoryCapacity,
    role: EventRole,
) -> list[discord.SelectOption]:
    supported = supported_roles(capacity)
    return [
        discord.SelectOption(
            label=flex.value,
            value=flex.value,
            emoji=ROLE_EMOJI[flex],
        )
        for flex in EventRole
        if flex != role and flex in supported
    ]


class LeaderFlexRolesSelect(discord.ui.Select["LeaderFlexRolesView"]):
    def __init__(self, options: list[discord.SelectOption]):
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


class LeaderFlexRolesView(discord.ui.View):
    def __init__(
        self,
        confirm: EventConfirmView,
        capacity: CategoryCapacity,
        role: EventRole,
    ):
        super().__init__(timeout=FLOW_TIMEOUT_SECONDS)
        self._confirm = confirm
        self._role = role
        self.add_item(LeaderFlexRolesSelect(_flex_role_options(capacity, role)))

    async def pick(
        self,
        interaction: discord.Interaction,
        flex_roles: tuple[EventRole, ...],
    ) -> None:
        LOGGER.debug(
            "Leader flex roles picked before posting; user_id=%s "
            "flex_count=%s",
            interaction.user.id,
            len(flex_roles),
        )
        await self._confirm.post_with_leader(
            interaction,
            self._role,
            flex_roles,
            from_picker=True,
        )

    @discord.ui.button(
        label="Skip selecting flex roles",
        style=discord.ButtonStyle.secondary,
    )
    async def skip(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[LeaderFlexRolesView],
    ) -> None:
        await self._confirm.post_with_leader(
            interaction,
            self._role,
            (),
            from_picker=True,
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
        if not user_has_role(
            interaction.user,
            self._bot._config.event_create_role_id,
        ):
            LOGGER.warning(
                "Rejected event edit save from Discord user %s; required "
                "role %s",
                interaction.user.id,
                self._bot._config.event_create_role_id,
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
        label="Add sign-ups",
        style=discord.ButtonStyle.primary,
    )
    async def add_signups(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[EventEditConfirmView],
    ) -> None:
        await open_roster_addition(self._bot, interaction, self._draft)

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
