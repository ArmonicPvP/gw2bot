"""What a member does with a posted event: signing up, out, and the settings.

The buttons on the posted message, the sign-up flow behind them, and the
remembered-role and auto-sign-up choices that flow offers.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Any, TYPE_CHECKING, cast

import discord
from sqlalchemy.exc import SQLAlchemyError

from gw2bot.events.formatting import signup_edit_limit_message
from gw2bot.events.models import (
    AutoSignupChoice,
    Event,
    EventOccurrence,
    EventRole,
    EventSignup,
    EventStatus,
    PreferenceMode,
    ROLE_EMOJI,
    RepeatFrequency,
    available_edit_tokens,
    fitting_roles,
    normalize_stored_roles,
    supported_roles,
)
from gw2bot.events.views.shared import (
    FLOW_TIMEOUT_SECONDS,
    _auto_signup_enabled,
    _is_ephemeral_component_interaction,
    _load_event_context,
    _role_pick_label,
    _still_seated_note,
    occurrence_has_ended,
)

if TYPE_CHECKING:
    from gw2bot.bot import Gw2Bot

LOGGER = logging.getLogger(__name__)


def build_signup_view(occurrence_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(EventSignUpButton(occurrence_id))
    view.add_item(EventSignOutButton(occurrence_id))
    view.add_item(EventSettingsButton(occurrence_id))
    return view


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


def _role_memory_offered(event: Event) -> bool:
    """Whether remembered roles are in play for this event.

    Remembered roles are per event, so they only pay off where the member
    signs up more than once - which is what a repeat is. Automatic sign-up is
    gated the same way, and for the same reason a stored choice on a one-off
    event is inert: an event that has stopped repeating must not go on seating
    members from a memory they can no longer see or reset.
    """
    return event.repeat_frequency is not RepeatFrequency.NONE


def _describe_signup_settings(
    bot: Gw2Bot,
    event: Event,
    discord_user_id: int,
) -> str:
    lines = ["**Your sign-up settings**"]
    if event.repeat_frequency is RepeatFrequency.NONE:
        lines.append(
            "This event does not repeat, so it has no automatic sign-up "
            "or role memory."
        )
        return "\n".join(lines)
    if not _series_has_runs_left(bot, event):
        # The panel offers no controls in this state, so it has to say why.
        lines.append(
            "This event has no runs left, so its automatic sign-up and role "
            "memory no longer apply."
        )
        return "\n".join(lines)
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
    preference = bot.event_store.get_signup_preference(
        event.event_id,
        discord_user_id,
    )
    if preference is not None and preference.mode is PreferenceMode.REMEMBER:
        remembered = (
            preference.role.value if preference.role is not None else "none"
        )
        lines.append(f"Remembered role for this event: **{remembered}**")
    elif preference is not None and preference.mode is PreferenceMode.NEVER_ASK:
        lines.append("Role memory for this event: **never ask**")
    else:
        lines.append("Role memory for this event: **ask every time**")
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


def _series_has_runs_left(bot: Gw2Bot, event: Event) -> bool:
    """Whether a series still has a run for its sign-up settings to serve.

    Automatic sign-up and role memory only ever feed a run still to come, and
    `/event delete` clears both while keeping the event row when it has
    finished runs to show - which leaves those posts standing, and the ⚙️
    button on them. The event being there is therefore not enough: a choice
    made from one of those posts would be stored for a series that will never
    run again, under a panel telling the member they are signed up for runs
    that are not coming.

    Judged on the stored status rather than the clock, because that is what
    retirement sets: a live series always holds a successor that has not
    reached OVER, while a run whose end no maintenance pass has caught up
    with yet is still one this event is about.
    """
    return any(
        occurrence.status is not EventStatus.OVER
        for occurrence in bot.event_store.get_event_occurrences(
            event.event_id
        )
    )


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
        offer_series_settings = (
            event.repeat_frequency is not RepeatFrequency.NONE
            and _series_has_runs_left(bot, event)
        )
        if event.repeat_frequency is not RepeatFrequency.NONE and (
            not offer_series_settings
        ):
            # A panel opened from the post of a run a deletion kept. Said out
            # loud because the member sees a panel with controls missing, and
            # an operator reading the log otherwise has nothing to connect
            # that to.
            LOGGER.debug(
                "Offering no sign-up settings for a series with no runs "
                "left; event_id=%s occurrence_id=%s user_id=%s",
                event.event_id,
                occurrence.occurrence_id,
                discord_user_id,
            )
        if offer_series_settings:
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
            # Role memory follows automatic sign-up: nothing to reset on an
            # event that only happens once.
            self.add_item(
                _SignupSettingsButton(
                    "Reset role memory for this event",
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

    async def _series_ended(self, interaction: discord.Interaction) -> bool:
        # The panel is ephemeral but can sit open, so its controls can outlive
        # the runs they were offered for: a deletion keeping an event's
        # finished runs takes every run still to come with it.
        if _series_has_runs_left(self._bot, self._event):
            return False
        LOGGER.debug(
            "Refused a sign-up setting for a series with no runs left; "
            "event_id=%s user_id=%s",
            self._event.event_id,
            interaction.user.id,
        )
        await interaction.response.edit_message(
            content=(
                "This event has no runs left, so its automatic sign-up and "
                "role memory no longer apply."
            ),
            view=None,
        )
        return True

    async def _enable_auto(self, interaction: discord.Interaction) -> None:
        if await self._series_ended(interaction):
            return
        signup = self._bot.event_store.get_signup(
            self._occurrence.occurrence_id,
            interaction.user.id,
        )
        preference = self._bot.event_store.get_signup_preference(
            self._event.event_id,
            interaction.user.id,
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

        if await self._series_ended(interaction):
            return
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
        if await self._series_ended(interaction):
            return
        self._bot.event_store.set_signup_preference(
            self._event.event_id,
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

        if not _series_has_runs_left(self._bot, self._event):
            # The series ended while this sat open, taking its automatic
            # sign-ups with it. There is nothing left to switch off, and
            # storing the choice would put back a row the deletion cleared.
            LOGGER.debug(
                "Skipped switching off automatic sign-up for a series with "
                "no runs left; event_id=%s user_id=%s",
                self._event.event_id,
                self._discord_user_id,
            )
            await interaction.response.edit_message(
                content=(
                    "This event has no runs left, so it will not sign you up "
                    "again anyway."
                ),
                view=None,
            )
            return
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
        # Declining writes nothing, but it does promise a next occurrence, so
        # it needs the same check as the button beside it: the series can have
        # been deleted while this sat open, taking both the setting and the
        # runs the promise is about.
        if not _series_has_runs_left(self._bot, self._event):
            LOGGER.debug(
                "Kept auto signup for a series with no runs left; "
                "event_id=%s user_id=%s",
                self._event.event_id,
                self._discord_user_id,
            )
            await interaction.response.edit_message(
                content=(
                    "This event has no runs left, so it will not sign you up "
                    "again either way."
                ),
                view=None,
            )
            return
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
        from gw2bot.events.posting import (
            RosterUnreadable,
            occurrence_finished,
            remove_signup,
        )

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
        try:
            removed, update = await remove_signup(
                self._bot,
                self._event,
                self._occurrence,
                interaction.user.id,
            )
        except RosterUnreadable:
            # Not the same as not being on the roster, which is what the
            # None below means: the store would not say, and this member's
            # signup is very likely still there.
            LOGGER.error(
                "Could not read the run for a sign out; occurrence_id=%s",
                self._occurrence.occurrence_id,
            )
            await interaction.edit_original_response(
                content=(
                    "The roster could not be read just now. Try again in a "
                    "moment."
                ),
                view=None,
            )
            return
        if removed is None:
            # The run can also end inside the removal itself, whose roster
            # check is Discord I/O: a roster that is history is left alone,
            # which is not the same as never having been on it. Read the row
            # back and count its stored status, because that check can retire
            # the occurrence outright - refreshing a message somebody deleted
            # answers NotFound - well before its scheduled end.
            # The event comes back with it, because a duration saved while
            # the lookups were in flight is what made the removal refuse:
            # judging by the one this view opened with would tell the member
            # they were never signed up while their signup is still there.
            try:
                current = self._bot.event_store.get_occurrence(
                    self._occurrence.occurrence_id
                )
                edited = self._bot.event_store.get_event(
                    self._event.event_id
                )
            except SQLAlchemyError as exc:
                # The removal can refuse because the store would not answer
                # it either, and these reads then fail the same way. Telling
                # the member nothing was wrong with their signup would be a
                # guess, and the wrong one.
                LOGGER.error(
                    "Could not read the run back after a sign out; "
                    "occurrence_id=%s error_type=%s",
                    self._occurrence.occurrence_id,
                    type(exc).__name__,
                )
                content = (
                    "The roster could not be read just now. Try again in a "
                    "moment."
                )
            else:
                content = (
                    "This event has already ended, so its roster can no "
                    "longer be changed."
                    if current is None
                    or edited is None
                    or occurrence_finished(edited, current)
                    else "You were not signed up for the event."
                )
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
    # Remembered roles are per event: a member signing up for an event they
    # have never signed up for has no preference row for it and is asked for
    # their roles, however many other events they have memories for.
    preference = (
        bot.event_store.get_signup_preference(
            event.event_id,
            interaction.user.id,
        )
        if _role_memory_offered(event)
        else None
    )
    if (
        preference is not None
        and preference.mode is PreferenceMode.REMEMBER
        and preference.role is not None
    ):
        flow.role, flow.flex_roles = normalize_stored_roles(
            event.capacity,
            preference.role,
            preference.flex_roles,
        )
        if (
            flow.role is not preference.role
            or flow.flex_roles != preference.flex_roles
        ):
            bot.event_store.set_signup_preference(
                event.event_id,
                interaction.user.id,
                flow.role,
                flow.flex_roles,
                PreferenceMode.REMEMBER,
            )
            LOGGER.debug(
                "Normalized remembered roles for the current category; "
                "event_id=%s user_id=%s stored_role=%s normalized_role=%s "
                "normalized_flex_count=%s",
                event.event_id,
                interaction.user.id,
                preference.role.value,
                flow.role.value,
                len(flow.flex_roles),
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

    def series_has_runs_left(self) -> bool:
        """Whether a choice answered now still has a run to serve.

        Both prompts below the picker can sit open until they time out, and
        `/event delete` clears the automatic sign-ups and remembered roles
        while keeping the event row - and the run this flow is about, when
        that run has finished. Neither the event nor the run being there is
        therefore proof that the choice has anywhere to land; the series
        having a run that has not reached OVER is (see
        _series_has_runs_left). A run called off while the series carries on
        is the other way round: the successor is already seeded, so what the
        member asked to remember still serves it.
        """
        if _series_has_runs_left(self.bot, self.event):
            return True
        LOGGER.debug(
            "Skipped a signup choice for a series with no runs left; "
            "event_id=%s occurrence_id=%s user_id=%s",
            self.event.event_id,
            self.occurrence.occurrence_id,
            self.discord_user_id,
        )
        return False

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
        if self.skip_remember_prompt or not _role_memory_offered(self.event):
            await self.finalize(interaction)
            return
        await interaction.response.edit_message(
            content=(
                "Would you like to remember your selection for future "
                "sign-ups for this event?"
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
        # A picker can remain open while a commander edits the event. Re-read
        # both records immediately before seating so the role is validated
        # against the current category rather than the stale picker snapshot.
        event = self.bot.event_store.get_event(self.event.event_id)
        occurrence = self.bot.event_store.get_occurrence(
            self.occurrence.occurrence_id
        )
        if event is None or occurrence is None:
            await edit(content="This event no longer exists.", view=None)
            return
        self.event = event
        self.occurrence = occurrence
        previous_role = self.role
        previous_flex_roles = self.flex_roles
        if event.capacity.has_roles and self.role is not None:
            self.role, self.flex_roles = normalize_stored_roles(
                event.capacity,
                self.role,
                self.flex_roles,
            )
        elif not event.capacity.has_roles:
            self.role = None
            self.flex_roles = ()
        if (
            self.role is not previous_role
            or self.flex_roles != previous_flex_roles
        ):
            LOGGER.debug(
                "Normalized signup roles after an event changed category; "
                "event_id=%s occurrence_id=%s user_id=%s category=%s "
                "normalized_role=%s normalized_flex_count=%s",
                event.event_id,
                occurrence.occurrence_id,
                self.discord_user_id,
                event.category.value,
                self.role.value if self.role is not None else None,
                len(self.flex_roles),
            )
        try:
            signup = await complete_signup(
                self.bot,
                event,
                occurrence,
                self.discord_user_id,
                self.role,
                self.flex_roles,
            )
        except ValueError as error:
            await edit(content=str(error), view=None)
            return
        # The seating awaits its own membership lookups and re-reads the
        # event across them, so it can normalise this selection again after
        # the normalisation above. What it stored is what the prompts below
        # have to carry: the automatic sign-up is written from these fields,
        # and a role the category no longer supports would be seeded into
        # every future run of the series. The event goes with them, since
        # the same save decides whether that prompt is offered at all.
        self.role = signup.role
        self.flex_roles = signup.flex_roles
        try:
            seated_event = self.bot.event_store.get_event(
                self.event.event_id
            )
        except SQLAlchemyError as exc:
            # The seat is committed and the prompts are what is left, so a
            # refusal costs the freshest description of the event rather
            # than the answer the member is waiting for.
            LOGGER.error(
                "Could not read the event back after a signup; "
                "event_id=%s error_type=%s",
                self.event.event_id,
                type(exc).__name__,
            )
            seated_event = None
        if seated_event is not None:
            self.event = seated_event
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
        if event.capacity.has_roles:
            normalized_role, normalized_flex_roles = normalize_stored_roles(
                event.capacity,
                self.role,
                self.flex_roles,
            )
            if (
                normalized_role is not self.role
                or normalized_flex_roles != self.flex_roles
            ):
                LOGGER.debug(
                    "Normalized signup-edit roles after an event changed "
                    "category; event_id=%s occurrence_id=%s user_id=%s "
                    "category=%s normalized_role=%s "
                    "normalized_flex_count=%s",
                    event.event_id,
                    occurrence.occurrence_id,
                    self.discord_user_id,
                    event.category.value,
                    normalized_role.value,
                    len(normalized_flex_roles),
                )
                self.role = normalized_role
                self.flex_roles = normalized_flex_roles
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
        # The edit re-reads the event across its membership lookups and can
        # normalise this selection against a category saved since, so what it
        # stored is what the prompt below must offer: the remembered roles
        # are written from these fields, and a role the event can no longer
        # seat would be handed back to the member's next sign-up. The event
        # goes with them, since it decides whether that prompt appears.
        self.role = signup.role
        self.flex_roles = signup.flex_roles
        try:
            edited_event = self.bot.event_store.get_event(self.event.event_id)
        except SQLAlchemyError as exc:
            # The edit is committed and only the prompt is left, so this
            # costs the freshest description of the event rather than the
            # answer the member is waiting for.
            LOGGER.error(
                "Could not read the event back after a signup edit; "
                "event_id=%s error_type=%s",
                self.event.event_id,
                type(exc).__name__,
            )
            edited_event = None
        if edited_event is not None:
            self.event = edited_event
        content = _signup_edit_summary(signup)
        if result.auto_signup_stale:
            # The edit is on this roster, but the snapshot that seeds the
            # next run kept the old selection and nothing retries it. Said
            # here so the member can put it right rather than find next
            # week's roster holding roles they changed.
            content = (
                f"{content}\n\nYour automatic sign-up for this event still "
                "holds your previous roles. Edit your signup again later to "
                "bring it across."
            )
        preference = (
            self.bot.event_store.get_signup_preference(
                self.event.event_id,
                self.discord_user_id,
            )
            if _role_memory_offered(self.event)
            else None
        )
        # A member with remembered roles for this event just declared a
        # different selection; offer to bring the memory along so their next
        # signup for it does not resurrect the old roles.
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
                    f"{content}\n\nYour remembered roles for this event "
                    "still hold your old selection. Update them to this "
                    "new one?"
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
        if not self._flow.series_has_runs_left():
            # Same as the prompts below a fresh signup: a memory stored now
            # would put back what a deletion cleared, for runs that are not
            # coming.
            await interaction.response.edit_message(
                content=(
                    "This event has no runs left, so nothing was saved."
                ),
                view=None,
            )
            return
        self._flow.bot.event_store.set_signup_preference(
            self._flow.event.event_id,
            self._flow.discord_user_id,
            self._flow.role,
            self._flow.flex_roles,
            PreferenceMode.REMEMBER,
        )
        LOGGER.debug(
            "Updated remembered roles after a signup edit; event_id=%s "
            "user_id=%s role=%s flex_count=%s",
            self._flow.event.event_id,
            self._flow.discord_user_id,
            self._flow.role.value if self._flow.role is not None else None,
            len(self._flow.flex_roles),
        )
        await interaction.response.edit_message(
            content="Your remembered roles for this event were updated.",
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
        # Nothing is written either way, but "left unchanged" would describe
        # roles a deletion has already cleared, so it answers the same
        # question the button beside it does.
        if not self._flow.series_has_runs_left():
            await interaction.response.edit_message(
                content=(
                    "This event has no runs left, so it no longer remembers "
                    "any roles."
                ),
                view=None,
            )
            return
        await interaction.response.edit_message(
            content=(
                "Your remembered roles for this event were left unchanged."
            ),
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


class RolePickSelect(discord.ui.Select["RolePickView"]):
    def __init__(self, flow: SignupFlow):
        signups = flow.roster_for_labels()
        supported = supported_roles(flow.event.capacity)
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
            if role in supported
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
        supported = supported_roles(flow.event.capacity)
        options = [
            discord.SelectOption(
                label=role.value,
                value=role.value,
                emoji=ROLE_EMOJI[role],
            )
            for role in EventRole
            if role != flow.role and role in supported
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
    # Every choice here is stored against the flow's event alone, so a member
    # who never wants to be asked about one event is still asked the first
    # time they sign up for another.

    def __init__(self, flow: SignupFlow):
        super().__init__(timeout=FLOW_TIMEOUT_SECONDS)
        self._flow = flow

    async def _answer(
        self,
        interaction: discord.Interaction,
        role: EventRole | None,
        flex_roles: tuple[EventRole, ...],
        mode: PreferenceMode,
    ) -> None:
        # Nothing is remembered for a series that ended while the prompt sat
        # open; the seating below is what reports that to the member.
        if self._flow.series_has_runs_left():
            self._flow.bot.event_store.set_signup_preference(
                self._flow.event.event_id,
                self._flow.discord_user_id,
                role,
                flex_roles,
                mode,
            )
        await self._flow.finalize(interaction)

    @discord.ui.button(label="Yes", style=discord.ButtonStyle.success)
    async def remember_yes(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[RememberChoiceView],
    ) -> None:
        await self._answer(
            interaction,
            self._flow.role,
            self._flow.flex_roles,
            PreferenceMode.REMEMBER,
        )

    @discord.ui.button(label="No", style=discord.ButtonStyle.secondary)
    async def remember_no(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[RememberChoiceView],
    ) -> None:
        await self._answer(interaction, None, (), PreferenceMode.ASK)

    @discord.ui.button(
        label="No, never ask again for this event",
        style=discord.ButtonStyle.secondary,
    )
    async def remember_never(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[RememberChoiceView],
    ) -> None:
        await self._answer(interaction, None, (), PreferenceMode.NEVER_ASK)


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
        if not self._flow.series_has_runs_left():
            # The series ended while this prompt sat open - deleting an event
            # takes its automatic sign-ups with the runs still to come.
            # Storing the choice would put back what the deletion cleared and
            # promise sign-ups for runs that are never coming, so say what
            # happened instead.
            await interaction.response.edit_message(
                content=(
                    "This event has no runs left, so nothing was saved."
                ),
                view=None,
            )
            return
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
