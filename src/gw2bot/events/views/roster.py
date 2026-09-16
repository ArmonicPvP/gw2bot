"""Editing a posted event's roster: taking members off it and putting them on.

The two halves share the target they resolve and the preview they re-render,
and the removal picker pages a roster larger than one select can hold.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from math import ceil
from typing import TYPE_CHECKING

import discord
from sqlalchemy.exc import SQLAlchemyError

from gw2bot.core.discord_utils import (
    GuildMembership,
    resolve_guild_memberships,
    safe_int,
    send_direct_message,
    user_has_role,
)
from gw2bot.events.formatting import event_embed
from gw2bot.events.models import (
    Event,
    EventOccurrence,
    EventRole,
    EventSignup,
    EventStatus,
    ROLE_EMOJI,
    RosterUpdate,
    fitting_roles,
    normalize_stored_roles,
    supported_roles,
)
from gw2bot.events.views.preview import build_event_preview, send_event_preview
from gw2bot.events.views.shared import (
    ADD_SELECT_MAX_MEMBERS,
    DEPARTED_SUMMARY_BUDGET,
    EventDraft,
    FLOW_TIMEOUT_SECONDS,
    REMOVE_OPTION_LABEL_MAX_LENGTH,
    REMOVE_SELECT_PAGE_SIZE,
    _EditFlowView,
    _auto_signup_enabled,
    _editing_occurrence,
    _event_message_link,
    _is_waitlisted,
    _link_label,
    _mention_list,
    _preview_status,
    _role_pick_label,
    _still_seated_note,
    occurrence_has_ended,
)

if TYPE_CHECKING:
    from gw2bot.bot import Gw2Bot
    from gw2bot.events.posting import AutoSignupDisableResult

LOGGER = logging.getLogger(__name__)


class EventRosterEditView(_EditFlowView):
    """The whole editor for an event that is already in progress.

    It carries the roster buttons alone: the event's stored details are frozen
    once it starts, so there is nothing here to save and no way from this view
    into apply_event_edit.
    """

    @discord.ui.button(
        label="Add sign-ups",
        style=discord.ButtonStyle.primary,
    )
    async def add_signups(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[EventRosterEditView],
    ) -> None:
        await open_roster_addition(self._bot, interaction, self._draft)

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


async def _opened_roster_target(
    bot: Gw2Bot,
    interaction: discord.Interaction,
    draft: EventDraft,
    action: str,
) -> tuple[Event, EventOccurrence] | None:
    """Re-check an edit preview before one of its roster buttons acts.

    The preview can sit open for minutes, and a roster-only session opens on an
    event that is already running, so the role, the event, the occurrence and
    the event's end are all re-read here rather than trusted from when the
    button was drawn. Returns None once the commander has been told why nothing
    will happen, so the caller only has to stop.
    """
    editing_event_id = draft.editing_event_id
    if editing_event_id is None:
        await interaction.response.send_message(
            "This edit session is no longer valid.",
            ephemeral=True,
        )
        return None
    if not user_has_role(interaction.user, bot._config.event_create_role_id):
        LOGGER.warning(
            "Rejected event roster %s from Discord user %s; required role %s",
            action,
            interaction.user.id,
            bot._config.event_create_role_id,
        )
        await interaction.response.send_message(
            "You do not have the required role to edit events.",
            ephemeral=True,
        )
        return None
    # The roster belongs to the occurrence, not the draft, so it is read
    # fresh: members can sign up or out while the preview sits open. A
    # roster-only session stays on the occurrence it pinned rather than
    # following the series onto its successor.
    event = bot.event_store.get_event(editing_event_id)
    occurrence = _editing_occurrence(bot, draft)
    if event is None or occurrence is None:
        LOGGER.debug(
            "Roster %s opened for a missing event; event_id=%s user_id=%s "
            "exists=%s",
            action,
            editing_event_id,
            interaction.user.id,
            event is not None,
        )
        await interaction.response.send_message(
            "This event no longer exists.",
            ephemeral=True,
        )
        return None
    # An ended roster is history: it cannot be pruned, removed from or added
    # to, so refuse before the picker is drawn rather than offering controls
    # that would all be rejected.
    if occurrence_has_ended(event, occurrence, datetime.now(UTC)):
        LOGGER.debug(
            "Rejected roster %s for an ended event; occurrence_id=%s "
            "user_id=%s",
            action,
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
        return None
    return event, occurrence


async def _picked_roster_target(
    bot: Gw2Bot,
    interaction: discord.Interaction,
    draft: EventDraft,
    occurrence_id: int,
    action: str,
) -> tuple[Event, EventOccurrence] | None:
    """Re-check an open roster picker before it changes the roster.

    Same reasoning as _opened_roster_target, one step further along: the picker
    itself can sit open for minutes. It stays on the occurrence it was opened
    for rather than re-resolving the draft's, so a series that seeds its
    successor mid-session cannot redirect the change onto the next run.
    """
    if not user_has_role(interaction.user, bot._config.event_create_role_id):
        LOGGER.warning(
            "Rejected event roster %s from Discord user %s; required role %s",
            action,
            interaction.user.id,
            bot._config.event_create_role_id,
        )
        await interaction.response.send_message(
            "You do not have the required role to edit events.",
            ephemeral=True,
        )
        return None
    editing_event_id = draft.editing_event_id
    event = (
        bot.event_store.get_event(editing_event_id)
        if editing_event_id is not None
        else None
    )
    occurrence = bot.event_store.get_occurrence(occurrence_id)
    if event is None or occurrence is None:
        LOGGER.debug(
            "Roster %s picker outlived its target; event_id=%s "
            "occurrence_id=%s user_id=%s event_exists=%s occurrence_exists=%s",
            action,
            editing_event_id,
            occurrence_id,
            interaction.user.id,
            event is not None,
            occurrence is not None,
        )
        await interaction.response.edit_message(
            content="This event no longer exists.",
            embeds=[],
            view=None,
        )
        return None
    # An ended event's roster is history: changing it would also promote
    # someone off the waitlist into a run that is already finished, and
    # re-rendering the message could persist OVER without seeding the next
    # occurrence of a recurring series. This mirrors the sign-out button, which
    # stays usable while an event is ongoing.
    if occurrence_has_ended(event, occurrence, datetime.now(UTC)):
        LOGGER.debug(
            "Rejected roster %s for an ended event; occurrence_id=%s "
            "user_id=%s",
            action,
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
        return None
    return event, occurrence


def _roster_preview_embed(
    draft: EventDraft,
    event_id: int,
    signups: list[EventSignup],
) -> discord.Embed:
    """The roster embed a picker keeps on screen above its own controls.

    It shows the seating a picker's one-line options cannot, and mirrors how
    build_event_preview renders the editing preview so the two show the same
    roster.
    """
    edited = draft.to_event(event_id)
    return event_embed(
        edited,
        signups,
        _preview_status(edited, signups, draft.roster_only),
        event_id_text=str(event_id),
    )


async def open_roster_removal(
    bot: Gw2Bot,
    interaction: discord.Interaction,
    draft: EventDraft,
) -> None:
    """Show the roster removal picker for the draft's event.

    Shared by the upcoming-event editor and the in-progress roster editor, so
    both reach the same picker over the same freshly read roster.
    """
    context = await _opened_roster_target(bot, interaction, draft, "removal")
    if context is None:
        return
    event, occurrence = context
    editing_event_id = event.event_id
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
    # The check can also retire the run outright - its own removal refreshes a
    # message somebody may have deleted, and the NotFound behind that persists
    # OVER - so the occurrence is read back before a picker is drawn over a
    # roster that has become history. RemoveSignupsView.remove refuses one on
    # submission; there is no reason to offer it first.
    from gw2bot.events.posting import occurrence_finished

    # Both reads are store calls, and the response already says the roster is
    # loading: a refusal has to answer rather than escape and leave the
    # commander on it. The departures the check made are committed either way,
    # and the next look at this roster shows them.
    try:
        live_occurrence = bot.event_store.get_occurrence(
            occurrence.occurrence_id
        )
        # The event comes back with it. The end check below reads the run's
        # duration off the event, and a leader can shorten an ongoing one
        # while the lookups run: judged against the event this picker opened
        # with, the controls would be drawn over a roster that has already
        # ended, only to be refused on the commander's next click. The draft
        # is left as it is - it is this commander's unsaved edit, not the
        # other leader's save.
        live_event = bot.event_store.get_event(editing_event_id)
        signups = (
            bot.event_store.get_signups(occurrence.occurrence_id)
            if live_occurrence is not None
            else []
        )
    except SQLAlchemyError as exc:
        LOGGER.error(
            "Could not read the roster back after checking it; "
            "occurrence_id=%s error_type=%s",
            occurrence.occurrence_id,
            type(exc).__name__,
        )
        await interaction.edit_original_response(
            content=(
                "The roster could not be read just now. Try again in a "
                "moment."
            ),
            embeds=[],
            view=None,
        )
        return
    if (
        live_occurrence is None
        or live_event is None
        or occurrence_finished(live_event, live_occurrence)
    ):
        LOGGER.debug(
            "Roster removal found the occurrence retired while checking it; "
            "occurrence_id=%s user_id=%s exists=%s event_exists=%s",
            occurrence.occurrence_id,
            interaction.user.id,
            live_occurrence is not None,
            live_event is not None,
        )
        await interaction.edit_original_response(
            content=(
                "This event has already ended, so its roster can no longer "
                "be edited."
            ),
            embeds=[],
            view=None,
        )
        return
    # The roster was read back above whatever the check reported. A prune that
    # fails partway still commits the removals it had made and cannot report
    # them, so the list read before it can offer seats that are already vacant
    # - or be non-empty for a roster the prune has emptied.
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
    roster = _roster_preview_embed(draft, editing_event_id, signups)
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
    member lookups serves both the check and whatever it renders next. Those
    same lookups are handed to the shared check, which therefore spends none
    of its own.

    A commander who has just opened the roster is asking about it now, so the
    check is forced rather than answered from the one a sign-up may have made
    moments ago.
    """
    from gw2bot.events.posting import check_roster_membership

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
    departed, _ = await check_roster_membership(
        bot,
        event,
        occurrence,
        memberships=memberships,
        force=True,
    )
    if not departed:
        return None, names
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
            RosterUnreadable,
            check_roster_membership,
            merge_roster_updates,
            notify_roster_update,
            occurrence_finished,
            remove_signup,
        )

        context = await _picked_roster_target(
            self._bot,
            interaction,
            self._draft,
            self._occurrence.occurrence_id,
            "removal",
        )
        if context is None:
            return
        event, occurrence = context
        await interaction.response.edit_message(
            content="Removing the selected members…",
            embeds=[],
            view=None,
        )
        # The picker was drawn from a check, and that answer stands for a
        # minute: a member who left while it sat open would still be holding
        # a seat here, and the removals below would hand one to them off the
        # waitlist. Ask once for the batch; the removals are answered from
        # this rather than sweeping the roster per member. Quietly, for the
        # same reason the additions do: the removals below are announced once
        # at the end, and this half belongs in that.
        departed, checked = await check_roster_membership(
            self._bot,
            event,
            occurrence,
            force=True,
            notify=False,
        )
        # That check can retire the occurrence itself: the removal it makes
        # refreshes a message that may have been deleted by hand, and the
        # NotFound behind that persists OVER and seeds the series' next run.
        # Read the row back and stop, rather than reporting every pick as "not
        # signed up" - which is what each removal would answer - and rebuilding
        # the edit preview over a roster that is history.
        #
        # The event comes back with it. Another leader can save a shorter
        # duration or a different category while the lookups are in flight,
        # and the batch judges the run's end by that duration, hands the event
        # to every removal, and tells each member about it.
        try:
            current = self._bot.event_store.get_occurrence(
                occurrence.occurrence_id
            )
            edited = self._bot.event_store.get_event(event.event_id)
        except SQLAlchemyError as exc:
            # The message already says the removals are under way, so this
            # answers rather than escaping. The check's own removals are
            # committed and nothing below is going to announce them.
            LOGGER.error(
                "Could not re-read the run after a removal batch's check; "
                "occurrence_id=%s error_type=%s",
                occurrence.occurrence_id,
                type(exc).__name__,
            )
            await notify_roster_update(self._bot, occurrence, checked)
            await interaction.edit_original_response(
                content=(
                    "The roster could not be read just now, so nobody was "
                    "removed. Try again in a moment."
                ),
                embeds=[],
                view=None,
            )
            return
        if (
            current is None
            or edited is None
            or occurrence_finished(edited, current)
        ):
            LOGGER.debug(
                "Roster removal found the occurrence retired; "
                "occurrence_id=%s user_id=%s exists=%s event_exists=%s",
                occurrence.occurrence_id,
                interaction.user.id,
                current is not None,
                edited is not None,
            )
            await interaction.edit_original_response(
                content=(
                    "This event has already ended, so its roster can no "
                    "longer be changed."
                ),
                embeds=[],
                view=None,
            )
            return
        occurrence = current
        event = edited
        removed: list[int] = []
        skipped: list[int] = []
        # Picks the check took off because they had left the server. They are
        # off the roster, which is what the commander asked for, but reporting
        # them as never having been signed up would deny the removal this very
        # confirmation made. Taken out of the batch as well as counted: the
        # loop has nothing left to do for them, and leaving them in it would
        # let the remainder below claim they were kept for an event that ended
        # after they had already gone.
        departed_ids = set(departed)
        picked = list(user_ids)
        gone = [user_id for user_id in picked if user_id in departed_ids]
        user_ids = [user_id for user_id in picked if user_id not in gone]
        updates: list[RosterUpdate] = [checked]
        kept_after_end: list[int] = []
        unread: list[int] = []
        undelivered: list[int] = []
        for index, user_id in enumerate(user_ids):
            # The picker holds several members and remove_signup awaits Discord
            # I/O between each, so the event can cross its end partway through
            # the loop even though the pre-loop check passed. Re-read the row
            # every iteration and stop the moment it is history, so no removal
            # (and no waitlist promotion behind it) ever lands on a finished
            # roster. The clock is not the only way it gets there: a removal
            # that refreshes a message somebody deleted retires the run
            # outright, so one of these removals can be what ends it.
            try:
                live = self._bot.event_store.get_occurrence(
                    occurrence.occurrence_id
                )
                # The event comes back with it, because the run's end is
                # judged by its duration: a leader shortening an event while
                # these removals go out would otherwise be read against the
                # duration this batch opened with, and every removal past
                # the new end refused a level down and reported as a member
                # who was never signed up.
                live_event = self._bot.event_store.get_event(event.event_id)
            except SQLAlchemyError as exc:
                # A store that will not answer cannot be asked to remove the
                # rest either. Stop with what landed rather than letting this
                # escape and leave the commander on "Removing…".
                unread = list(user_ids[index:])
                LOGGER.error(
                    "Could not read the run mid-removal; stopping; "
                    "occurrence_id=%s kept=%s error_type=%s",
                    occurrence.occurrence_id,
                    len(unread),
                    type(exc).__name__,
                )
                break
            if (
                live is None
                or live_event is None
                or occurrence_finished(live_event, live)
            ):
                kept_after_end = list(user_ids[index:])
                LOGGER.debug(
                    "Event ended mid-removal; stopping; occurrence_id=%s "
                    "user_id=%s kept=%s exists=%s event_exists=%s",
                    occurrence.occurrence_id,
                    interaction.user.id,
                    len(kept_after_end),
                    live is not None,
                    live_event is not None,
                )
                break
            occurrence = live
            event = live_event
            # Notification is deferred to a single merged announcement after
            # the loop: per-removal pings would post one thread message per
            # member for what the leader sees as a single edit.
            try:
                signup, update = await remove_signup(
                    self._bot,
                    event,
                    occurrence,
                    user_id,
                    notify=False,
                )
            except RosterUnreadable:
                # The removal could not read the run, which says nothing
                # about this member's seat: counting them as never signed up
                # would deny a signup that is still there. Stops the batch
                # like the read above, since the rest would fare no better.
                unread = list(user_ids[index:])
                LOGGER.error(
                    "Could not read the run for a removal; stopping; "
                    "occurrence_id=%s user_id=%s kept=%s",
                    occurrence.occurrence_id,
                    interaction.user.id,
                    len(unread),
                )
                break
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
        merged = merge_roster_updates(updates, [*removed, *gone])
        await notify_roster_update(self._bot, occurrence, merged)
        promoted = [signup.discord_user_id for signup in merged.promoted]
        LOGGER.debug(
            "Applied roster removal; event_id=%s occurrence_id=%s user_id=%s "
            "picked=%s removed=%s not_signed_up=%s departed=%s promoted=%s "
            "kept=%s undelivered=%s",
            event.event_id,
            occurrence.occurrence_id,
            interaction.user.id,
            len(picked),
            len(removed),
            len(skipped),
            len(gone),
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
            gone,
            unread,
        )
        # The guard above runs before each removal, so the last one is not
        # covered by it: a removal that refreshes a message somebody deleted
        # retires the run, and with nothing left to iterate there is no next
        # pass to notice. Read the row once more before offering a preview. A
        # store that cannot answer is treated the same way, because a preview
        # drawn from what it could not read is worse than none.
        try:
            settled = self._bot.event_store.get_occurrence(
                occurrence.occurrence_id
            )
            retired = settled is None or occurrence_finished(event, settled)
        except SQLAlchemyError as exc:
            LOGGER.error(
                "Could not read the run back after a removal batch; "
                "occurrence_id=%s error_type=%s",
                occurrence.occurrence_id,
                type(exc).__name__,
            )
            retired = True
        if kept_after_end or unread or retired:
            # The event ended partway through, or one of these removals ended
            # it, so the edit session is no longer valid (an ended event
            # cannot be edited). Report what was applied and stop, rather than
            # re-showing an edit preview that can no longer be saved.
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
    departed: list[int] | None = None,
    unread: list[int] | None = None,
) -> str:
    lines: list[str] = []
    if removed:
        lines.append(f"Removed {_mention_list(removed)} from the roster.")
    elif not departed:
        lines.append("Nobody was removed from the roster.")
    if departed:
        # Off the roster either way, but by the membership check rather than
        # by this removal - so they were never sent the direct message the
        # others get, and the commander should know why.
        lines.append(
            _mention_list(departed)
            + " had left the server, so they were taken off the roster."
        )
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
    if unread:
        # A different stop from the one above: the run did not end, the store
        # simply would not answer, and trying again in a moment is the right
        # advice rather than "the event is over".
        lines.append(
            "The roster could not be read, so "
            + _mention_list(unread)
            + (" was kept." if len(unread) == 1 else " were kept.")
            + " Try again in a moment."
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


async def open_roster_addition(
    bot: Gw2Bot,
    interaction: discord.Interaction,
    draft: EventDraft,
) -> None:
    """Show the member picker that signs members up for the draft's event.

    Shared by the upcoming-event editor and the in-progress roster editor, so
    both reach the same picker over the same freshly read roster.
    """
    context = await _opened_roster_target(bot, interaction, draft, "addition")
    if context is None:
        return
    event, occurrence = context
    # The roster is seated against the *saved* category, because that is the
    # capacity the stored assignments describe, while the preview above already
    # shows the pending one. Adding under an unsaved category change would ask
    # the wrong question - a change to a headcount category would skip the role
    # step entirely - and the save behind it would then rebalance those members
    # into roles the commander never picked. Send them back to the preview to
    # save first; the change itself is untouched.
    if draft.category is not None and draft.category is not event.category:
        LOGGER.debug(
            "Rejected a roster addition with an unsaved category change; "
            "event_id=%s user_id=%s saved=%s pending=%s",
            event.event_id,
            interaction.user.id,
            event.category.value,
            draft.category.value,
        )
        await send_event_preview(
            bot,
            interaction,
            draft,
            primary=occurrence,
            content=(
                "This preview shows a category change that has not been saved "
                "yet, and members are seated against the saved category. "
                "Choose **Save changes** first, then add them."
            ),
        )
        return
    signups = bot.event_store.get_signups(occurrence.occurrence_id)
    LOGGER.debug(
        "Opened roster addition; event_id=%s occurrence_id=%s user_id=%s "
        "roster=%s roster_only=%s",
        event.event_id,
        occurrence.occurrence_id,
        interaction.user.id,
        len(signups),
        draft.roster_only,
    )
    # Unlike the removal picker this needs no member lookups: Discord's own
    # user select searches the server client-side, so the whole picker fits
    # inside the three-second interaction window without a defer.
    view = AddSignupsView(bot, draft, occurrence)
    await interaction.response.edit_message(
        content=view.prompt(),
        embeds=[_roster_preview_embed(draft, event.event_id, signups)],
        view=view,
    )


class AddSignupsSelect(discord.ui.UserSelect["AddSignupsView"]):
    def __init__(self):
        # Discord's own member search rather than a list the bot builds: the
        # commander is picking from the whole server, and the client filters it
        # as they type. It cannot be narrowed to members who are not signed up
        # yet, so an already-seated pick is reported afterwards rather than
        # prevented here.
        super().__init__(
            placeholder="Search for the members to add",
            min_values=1,
            max_values=ADD_SELECT_MAX_MEMBERS,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if view is None:
            return
        await view.pick(interaction, [user.id for user in self.values])


class _AddBackButton(discord.ui.Button["AddSignupsView"]):
    def __init__(self):
        super().__init__(label="Back", style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if view is not None:
            await view.back(interaction)


class AddSignupsView(discord.ui.View):
    def __init__(
        self,
        bot: Gw2Bot,
        draft: EventDraft,
        occurrence: EventOccurrence,
    ):
        super().__init__(timeout=FLOW_TIMEOUT_SECONDS)
        self._bot = bot
        self._draft = draft
        self._occurrence = occurrence
        self.add_item(AddSignupsSelect())
        self.add_item(_AddBackButton())

    def prompt(self) -> str:
        return (
            "Search for the members to add to this event's roster. They are "
            "seated exactly as the sign-up button would seat them, and each "
            "one is told by direct message.\n"
            f"Up to {ADD_SELECT_MAX_MEMBERS} members at a time."
        )

    async def back(self, interaction: discord.Interaction) -> None:
        await send_event_preview(self._bot, interaction, self._draft)

    async def pick(
        self,
        interaction: discord.Interaction,
        user_ids: list[int],
    ) -> None:
        context = await _picked_roster_target(
            self._bot,
            interaction,
            self._draft,
            self._occurrence.occurrence_id,
            "addition",
        )
        if context is None:
            return
        event, occurrence = context
        LOGGER.debug(
            "Picked members to add; event_id=%s occurrence_id=%s user_id=%s "
            "picked=%s has_roles=%s",
            event.event_id,
            occurrence.occurrence_id,
            interaction.user.id,
            len(user_ids),
            event.capacity.has_roles,
        )
        if not event.capacity.has_roles:
            # A headcount event seats by arrival order alone, so there is
            # nothing left to ask.
            await apply_roster_addition(
                self._bot,
                interaction,
                self._draft,
                occurrence,
                user_ids,
                None,
            )
            return
        signups = self._bot.event_store.get_signups(occurrence.occurrence_id)
        view = AddSignupsRoleView(
            self._bot,
            self._draft,
            occurrence,
            event,
            signups,
            user_ids,
        )
        await interaction.response.edit_message(
            content=view.prompt(),
            embeds=[],
            view=view,
        )


class AddSignupsRoleSelect(discord.ui.Select["AddSignupsRoleView"]):
    def __init__(self, event: Event, signups: list[EventSignup]):
        # The same labelling as the member-facing role picker, so a commander
        # can see which roles are already full before choosing.
        supported = supported_roles(event.capacity)
        available = set(fitting_roles(event.capacity, signups))
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
        super().__init__(
            placeholder="Pick the role to add them as",
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if view is not None:
            await view.pick(interaction, EventRole(self.values[0]))


class AddSignupsRoleView(discord.ui.View):
    """Picks the one role a batch of manually added members is seated as.

    A member signing themselves up names a preferred role and any flex roles
    they will fall back to; a commander adding someone else cannot answer that
    for them, so a manual add carries the picked role and no flex roles. Adding
    members in different roles is therefore one pass per role.
    """

    def __init__(
        self,
        bot: Gw2Bot,
        draft: EventDraft,
        occurrence: EventOccurrence,
        event: Event,
        signups: list[EventSignup],
        user_ids: list[int],
    ):
        super().__init__(timeout=FLOW_TIMEOUT_SECONDS)
        self._bot = bot
        self._draft = draft
        self._occurrence = occurrence
        self._user_ids = user_ids
        self.add_item(AddSignupsRoleSelect(event, signups))

    def prompt(self) -> str:
        return (
            f"Pick the role to add {_mention_list(self._user_ids)} as. It "
            "applies to everyone you picked, so add members in different "
            "roles one role at a time."
        )

    async def pick(
        self,
        interaction: discord.Interaction,
        role: EventRole,
    ) -> None:
        await apply_roster_addition(
            self._bot,
            interaction,
            self._draft,
            self._occurrence,
            self._user_ids,
            role,
        )


def _addition_dm_content(
    commander_discord_id: int,
    event: Event,
    link: str | None,
) -> str:
    """Tell a member a commander put them on an event's roster.

    The event name carries the jump link so the member can open the post and
    see what they were signed up for; without a posted message there is nothing
    to link to, so the name is emphasised instead.
    """
    name = (
        f"[{_link_label(event.title)}]({link})"
        if link is not None
        else f"**{event.title}**"
    )
    return f"<@{commander_discord_id}> added you to {name}."


class _AdditionStop(StrEnum):
    """Why a batch addition stopped before it reached every picked member.

    They read very differently to the commander: an event that finished is
    nobody's fault, a retired occurrence means its post was deleted and is
    worth chasing, a concurrent edit means the batch can simply be run again,
    and a roster the store would not read is worth trying again in a moment.
    """

    ENDED = "ended"
    RETIRED = "retired"
    CHANGED = "changed"
    UNREADABLE = "unreadable"


@dataclass
class _AdditionOutcome:
    """What a batch addition did with each member the commander picked."""

    added: list[int] = field(default_factory=list)
    waitlisted: list[int] = field(default_factory=list)
    skipped: list[int] = field(default_factory=list)
    departed: list[int] = field(default_factory=list)
    failed: list[int] = field(default_factory=list)
    left_off: list[int] = field(default_factory=list)
    undelivered: list[int] = field(default_factory=list)
    stop: _AdditionStop | None = None


@dataclass(frozen=True, slots=True)
class _AdditionTarget:
    """The rows a batch addition works on, and why it must stop, if it must.

    The two are not alternatives. A save that changes the category and the
    channel together stops the seating and moves the post in one go, and the
    notices still owed have to point at where it went - so the rows come back
    with the reason whenever they were readable at all.
    """

    event: Event | None = None
    occurrence: EventOccurrence | None = None
    stop: _AdditionStop | None = None


def _addition_target(
    bot: Gw2Bot,
    event: Event,
    occurrence: EventOccurrence,
) -> _AdditionTarget:
    """Re-read the rows a batch addition works on, and say if it must stop.

    seat_signup awaits Discord I/O, so both rows can go out from under a batch
    between one member and the next, in three ways worth telling apart:

    - the occurrence reaches its scheduled end;
    - seating a member refreshes the public message, and a message or channel
      that has been deleted retires the occurrence as OVER and seeds the
      series' successor;
    - another leader saves an edit. A category change is the one that matters,
      because the category picks the capacity every seat is computed against,
      and that edit re-seats the whole roster under the new one. Seating from
      the event a batch started with would quietly undo that with the old
      capacity.
    """
    fresh_event = bot.event_store.get_event(event.event_id)
    fresh_occurrence = bot.event_store.get_occurrence(
        occurrence.occurrence_id
    )
    if fresh_event is None or fresh_occurrence is None:
        return _AdditionTarget(stop=_AdditionStop.RETIRED)
    stop: _AdditionStop | None = None
    # A run that is over is reported as over even when the same save changed
    # the category: it decides whether the notices still owed carry a link at
    # all, while a category change only stops the seating. The clock is
    # checked before the stored status, so an occurrence that is OVER because
    # it genuinely finished is not reported as a deleted post.
    if occurrence_has_ended(fresh_event, fresh_occurrence, datetime.now(UTC)):
        stop = _AdditionStop.ENDED
    elif fresh_occurrence.status is EventStatus.OVER:
        stop = _AdditionStop.RETIRED
    elif fresh_event.category is not event.category:
        stop = _AdditionStop.CHANGED
    return _AdditionTarget(fresh_event, fresh_occurrence, stop)


async def _drop_departed_picks(
    bot: Gw2Bot,
    interaction: discord.Interaction,
    user_ids: list[int],
) -> tuple[list[int], list[int]]:
    """Split the picked members into those still here and those who have left.

    The picker, and the role step behind it, can sit open for minutes, so a
    picked member may have left the server since. Seating them would spend a
    roster slot on someone who cannot see the event, and the thread add behind
    it fails quietly, so the whole batch is checked in one round of lookups
    first. Only a definite "not a member" counts: a lookup that failed proves
    nothing, and treating it as a departure would refuse legitimate additions
    whenever Discord is unreachable.
    """
    memberships = await resolve_guild_memberships(
        bot,
        interaction.guild,
        user_ids,
    )
    departed = [
        user_id
        for user_id in user_ids
        if memberships.get(user_id, GuildMembership()).in_guild is False
    ]
    if not departed:
        return user_ids, []
    LOGGER.debug(
        "Dropped picked members who have left the server; user_id=%s "
        "picked=%s departed=%s",
        interaction.user.id,
        len(user_ids),
        len(departed),
    )
    remaining = set(departed)
    return [
        user_id for user_id in user_ids if user_id not in remaining
    ], departed


async def apply_roster_addition(
    bot: Gw2Bot,
    interaction: discord.Interaction,
    draft: EventDraft,
    occurrence: EventOccurrence,
    user_ids: list[int],
    role: EventRole | None,
) -> None:
    """Sign the picked members up, then report what the roster did."""
    from gw2bot.events.posting import (
        check_roster_membership,
        merge_roster_updates,
        notify_roster_update,
        seat_signup,
    )

    context = await _picked_roster_target(
        bot,
        interaction,
        draft,
        occurrence.occurrence_id,
        "addition",
    )
    if context is None:
        return
    event, current = context
    # The role step is answered against the category the picker was opened
    # for, and another leader changing the category between the two swaps the
    # capacity underneath it. A role chosen for a role-based event would then
    # be persisted onto a headcount signup, which is always role-less, and a
    # later rebalance would honour a role picked for a category the event no
    # longer has. The freshly read event decides, so this mismatch is exactly
    # that race - the flow itself always pairs them correctly.
    if event.capacity.has_roles != (role is not None):
        LOGGER.debug(
            "Rejected a roster addition whose role step no longer matches "
            "the event; event_id=%s user_id=%s has_roles=%s role_picked=%s",
            event.event_id,
            interaction.user.id,
            event.capacity.has_roles,
            role is not None,
        )
        await interaction.response.edit_message(
            content=(
                "Another leader changed this event's category while you were "
                "picking, so nobody was added. Run `/event edit` again to add "
                "them."
            ),
            embeds=[],
            view=None,
        )
        return
    if event.capacity.has_roles and role is not None:
        normalized_role, _ = normalize_stored_roles(
            event.capacity,
            role,
            (),
        )
        if normalized_role is not role:
            LOGGER.debug(
                "Normalized a roster-addition role after an event changed "
                "category; event_id=%s user_id=%s category=%s "
                "normalized_role=%s",
                event.event_id,
                interaction.user.id,
                event.category.value,
                normalized_role.value,
            )
            role = normalized_role
    await interaction.response.edit_message(
        content="Adding the selected members…",
        embeds=[],
        view=None,
    )
    outcome = _AdditionOutcome()
    updates: list[RosterUpdate] = []
    picked = list(user_ids)
    user_ids, outcome.departed = await _drop_departed_picks(
        bot,
        interaction,
        user_ids,
    )
    # The picks are checked above; this checks the roster they are being
    # seated alongside. The preview behind this picker already asked about it,
    # and its answer stands for a minute, so the seats would otherwise be
    # solved against a roster read before the commander opened the picker -
    # and a member who left while it sat open would hold one of them. Once
    # for the batch: the seatings below each ask too, and are answered from
    # this one rather than sweeping the roster per member.
    #
    # Quietly: the additions below collect their own moves for a single
    # announcement at the end, and a member the check promotes can be moved
    # again by a seating in the same batch. One commander action says one
    # thing about each member, so the check's half is folded in rather than
    # sent ahead of it.
    _, checked = await check_roster_membership(
        bot,
        event,
        current,
        force=True,
        notify=False,
    )
    updates.append(checked)
    for index, user_id in enumerate(user_ids):
        # Re-read both rows rather than trusting the ones the batch started
        # with. The member being seated when a change lands is already past
        # this check and keeps whatever seat the old state gave them; stopping
        # here bounds that to one member instead of the whole batch.
        #
        # A store that refuses those reads has to answer rather than escape:
        # the response already says the members are being added, and the
        # check's own departures are committed with nothing to announce them
        # if this call never reaches the end of the batch, which is where the
        # one merged announcement goes out.
        try:
            target = _addition_target(bot, event, current)
        except SQLAlchemyError as exc:
            LOGGER.error(
                "Could not read the run back during a roster addition; "
                "occurrence_id=%s error_type=%s",
                current.occurrence_id,
                type(exc).__name__,
            )
            outcome.stop = _AdditionStop.UNREADABLE
            outcome.left_off = list(user_ids[index:])
            break
        if target.event is not None and target.occurrence is not None:
            event, current = target.event, target.occurrence
        if target.stop is not None:
            outcome.stop = target.stop
            outcome.left_off = list(user_ids[index:])
            LOGGER.debug(
                "Stopped a roster addition; occurrence_id=%s user_id=%s "
                "reason=%s left_off=%s",
                current.occurrence_id,
                interaction.user.id,
                target.stop.value,
                len(outcome.left_off),
            )
            break
        # add_signup would overwrite an existing row, resetting the member's
        # signed-up time and with it their seating priority, so a member who is
        # already on the roster is left exactly as they are.
        #
        # Guarded like the read above it, and for the same reason: the seats
        # already taken and the check's own departures are committed, and an
        # escape here takes the notices, the announcement and the summary
        # with it.
        try:
            already_seated = bot.event_store.get_signup(
                current.occurrence_id, user_id
            )
        except SQLAlchemyError as exc:
            LOGGER.error(
                "Could not read a member's seat during a roster addition; "
                "occurrence_id=%s error_type=%s",
                current.occurrence_id,
                type(exc).__name__,
            )
            outcome.stop = _AdditionStop.UNREADABLE
            outcome.left_off = list(user_ids[index:])
            break
        if already_seated:
            outcome.skipped.append(user_id)
            continue
        try:
            # Notification is deferred to a single merged announcement
            # after the loop: per-addition pings would post one thread
            # message per member for what the leader sees as one edit.
            signup, update = await seat_signup(
                bot,
                event,
                current,
                user_id,
                role,
                (),
                notify=False,
            )
        except ValueError as error:
            # The roster refused this member (the occurrence ended between
            # the check above and the write). One refusal must not abandon
            # the rest of the batch.
            LOGGER.debug(
                "Could not add a member to the roster; occurrence_id=%s "
                "error_type=%s",
                current.occurrence_id,
                type(error).__name__,
            )
            outcome.failed.append(user_id)
            continue
        outcome.added.append(user_id)
        updates.append(update)
        if signup.waitlisted:
            outcome.waitlisted.append(user_id)
    if outcome.stop is None:
        # The last seat refreshed the public message too, and no iteration is
        # left to notice that the refresh retired the occurrence. Run the same
        # check once more, so the final member is not sent a link to a message
        # that is gone and the preview below is not offered for a roster that
        # can no longer be edited.
        # Guarded like the one inside the loop: the seats are committed by
        # now, and a store that will not answer must not take the notices,
        # the announcement and the summary down with it.
        try:
            target = _addition_target(bot, event, current)
        except SQLAlchemyError as exc:
            LOGGER.error(
                "Could not read the run back after a roster addition; "
                "occurrence_id=%s error_type=%s",
                current.occurrence_id,
                type(exc).__name__,
            )
            target = _AdditionTarget(stop=_AdditionStop.UNREADABLE)
        if target.event is not None and target.occurrence is not None:
            event, current = target.event, target.occurrence
        if target.stop is not None:
            outcome.stop = target.stop
            LOGGER.debug(
                "Roster addition finished on an event that is no longer "
                "live; occurrence_id=%s user_id=%s reason=%s",
                current.occurrence_id,
                interaction.user.id,
                target.stop.value,
            )
    # Sent once the roster's final state is known, so a seat that retired the
    # occurrence cannot send anyone a link to the deleted message. One member
    # with closed DMs must not stop the rest, so a failed delivery is only
    # recorded for the summary.
    def addition_notice() -> str:
        link = (
            None
            if outcome.stop is _AdditionStop.RETIRED
            else _event_message_link(interaction.guild_id, event, current)
        )
        return _addition_dm_content(interaction.user.id, event, link)

    content = addition_notice()
    for user_id in outcome.added:
        # Both rows are read again before each delivery, because each one
        # awaits Discord: a channel move landing between two notices would
        # otherwise send the rest of them a jump link to the post the run
        # has just left, and a run retired mid-loop would keep handing out a
        # link to a message that has gone. What the notice says is rebuilt
        # from whatever comes back. A store that will not answer says
        # nothing about the message, so the notice keeps the last link known
        # to be good rather than dropping it.
        try:
            live = _addition_target(bot, event, current)
        except SQLAlchemyError as exc:
            LOGGER.error(
                "Could not read the run back between addition notices; "
                "occurrence_id=%s error_type=%s",
                current.occurrence_id,
                type(exc).__name__,
            )
            live = _AdditionTarget(stop=_AdditionStop.UNREADABLE)
        # Adopted whether or not it also reports a stop: a save that changes
        # the category and the channel together answers CHANGED and moves
        # the post in the same breath, and the notices left would otherwise
        # carry a link to the message that move deleted.
        if live.event is not None and live.occurrence is not None:
            event, current = live.event, live.occurrence
        if live.stop is not None and outcome.stop is None:
            outcome.stop = live.stop
            LOGGER.debug(
                "Roster addition target went away between its notices; "
                "occurrence_id=%s user_id=%s reason=%s",
                current.occurrence_id,
                interaction.user.id,
                live.stop.value,
            )
        content = addition_notice()
        # These are sequential external deliveries, so /event delete can land
        # between two of them and cascade the whole roster away. The member's
        # own row is the exact question being answered: a row that has gone
        # means they are on nothing, so telling them they joined it would be
        # wrong, while a row that survives an occurrence retired above still
        # earns its notice (without the link the check above already dropped).
        # Either way the commander is told they went unnotified.
        #
        # A store that will not answer is not evidence the seat has gone, and
        # everything after this loop - the announcement and the summary - is
        # still owed to the commander. The notices left are recorded as
        # undelivered, which is what they are, and the rest carries on.
        try:
            seated = bot.event_store.get_signup(
                current.occurrence_id, user_id
            )
        except SQLAlchemyError as exc:
            LOGGER.error(
                "Could not read a seat before its addition notice; "
                "occurrence_id=%s error_type=%s",
                current.occurrence_id,
                type(exc).__name__,
            )
            outcome.undelivered.extend(
                outcome.added[outcome.added.index(user_id):]
            )
            break
        if seated is None:
            LOGGER.debug(
                "Skipped a roster addition notice for a seat that has gone; "
                "occurrence_id=%s",
                current.occurrence_id,
            )
            outcome.undelivered.append(user_id)
            continue
        if not await send_direct_message(bot, user_id, content):
            outcome.undelivered.append(user_id)
    if outcome.stop is None:
        # Those notices are external deliveries, up to one per picked member,
        # so the event can be deleted or retired while they go out. The check
        # above guarded what the notices say; this one guards what is offered
        # afterwards, so edit controls are never rebuilt for a roster that has
        # since gone.
        try:
            final = _addition_target(bot, event, current)
        except SQLAlchemyError as exc:
            LOGGER.error(
                "Could not read the run back after a roster addition's "
                "notices; occurrence_id=%s error_type=%s",
                current.occurrence_id,
                type(exc).__name__,
            )
            final = _AdditionTarget(stop=_AdditionStop.UNREADABLE)
        # Adopted like the earlier verifications, and for the same reason: a
        # channel move landing while the notices went out replaces the thread
        # the announcement below is sent to and the message the preview is
        # drawn from, so both must be addressed to the run as it now stands.
        if final.event is not None and final.occurrence is not None:
            event, current = final.event, final.occurrence
        if final.stop is not None:
            outcome.stop = final.stop
            LOGGER.debug(
                "Roster addition target went away while its notices were "
                "sent; occurrence_id=%s user_id=%s reason=%s",
                current.occurrence_id,
                interaction.user.id,
                final.stop.value,
            )
    # Re-read the seats rather than trusting what each write returned. An edit
    # landing while seat_signup awaited Discord re-seats the whole roster under
    # the new capacity, so the row a write returned can describe a capacity
    # that no longer applies - reporting a member as waitlisted when the
    # rebalance has since seated them. One store read describes them all as
    # they now stand. A member whose row has gone (the event was deleted) is
    # left out of the waitlist entirely; the stop note below covers that.
    #
    # Guarded like every other read on this path: the seats are committed and
    # the announcement and the summary still have to go out, so a store that
    # will not say who ended up on the waitlist costs that one line of the
    # summary rather than the whole answer.
    try:
        outcome.waitlisted = [
            user_id
            for user_id in outcome.added
            if _is_waitlisted(bot, current.occurrence_id, user_id)
        ]
    except SQLAlchemyError as exc:
        # The assignment never happened, so what each seat_signup reported
        # stands. It can be a capacity an edit has since replaced, but it is
        # what this batch was told, and clearing it would tell a commander
        # that members the event had no room for were seated.
        LOGGER.error(
            "Could not read back who a roster addition waitlisted; "
            "occurrence_id=%s error_type=%s",
            current.occurrence_id,
            type(exc).__name__,
        )
    # Each addition can flex seated members into another of their roles, and a
    # later addition can move someone an earlier one already moved. Merging
    # collapses each member's changes into one line describing the net result.
    # A concurrent category change makes those moves obsolete - they were
    # computed against the old capacity, and the edit announces its own
    # rebalance in the same thread - so there is nothing left worth saying.
    merged = (
        RosterUpdate()
        if outcome.stop is _AdditionStop.CHANGED
        else merge_roster_updates(updates)
    )
    await notify_roster_update(bot, current, merged)
    LOGGER.debug(
        "Applied roster addition; event_id=%s occurrence_id=%s user_id=%s "
        "role=%s picked=%s added=%s waitlisted=%s already_signed_up=%s "
        "departed=%s failed=%s left_off=%s undelivered=%s reassigned=%s "
        "stop=%s",
        event.event_id,
        current.occurrence_id,
        interaction.user.id,
        role.value if role is not None else None,
        len(picked),
        len(outcome.added),
        len(outcome.waitlisted),
        len(outcome.skipped),
        len(outcome.departed),
        len(outcome.failed),
        len(outcome.left_off),
        len(outcome.undelivered),
        len(merged.reassigned),
        outcome.stop.value if outcome.stop is not None else None,
    )
    summary = _addition_summary(outcome)
    if outcome.stop is not None:
        # The event went out from under the batch, so this edit session is no
        # longer valid. Report what was applied and stop, rather than
        # re-showing an edit preview that can no longer be trusted.
        await interaction.edit_original_response(
            content=summary,
            embeds=[],
            view=None,
        )
        return
    embeds, view = build_event_preview(bot, draft, primary=current)
    await interaction.edit_original_response(
        content=summary,
        embeds=embeds,
        view=view,
    )


def _addition_finished_note(stop: _AdditionStop) -> str:
    """Say what became of the event once the whole batch had been applied.

    Everyone picked was dealt with, so there is nobody to name; the commander
    still has to be told, because the preview does not come back.
    """
    if stop is _AdditionStop.CHANGED:
        return (
            "Another leader changed this event's category while the members "
            "were being added, so its roster may have been re-seated since."
        )
    if stop is _AdditionStop.RETIRED:
        return (
            "This event's post is no longer available; its message may have "
            "been deleted."
        )
    if stop is _AdditionStop.UNREADABLE:
        return (
            "The roster could not be read while the members were being "
            "added, so it may have moved on since."
        )
    return "The event ended while the members were being added."


def _addition_stop_note(
    stop: _AdditionStop | None,
    left_off: list[int],
) -> str:
    """Say why the members after the stop were not added."""
    who = _mention_list(left_off)
    were = "was" if len(left_off) == 1 else "were"
    if stop is _AdditionStop.CHANGED:
        return (
            "Another leader changed this event while the members were being "
            f"added, so {who} {were} left off. Run `/event edit` again to add "
            "them."
        )
    if stop is _AdditionStop.RETIRED:
        # The occurrence was retired rather than finished, which happens when
        # its message or channel has been deleted - worth saying, because the
        # commander can go and look.
        return (
            f"This event's post is no longer available, so {who} {were} left "
            "off. Its message may have been deleted."
        )
    if stop is _AdditionStop.UNREADABLE:
        return (
            f"The roster could not be read just now, so {who} {were} left "
            "off. Try `/event edit` again in a moment."
        )
    return (
        f"The event ended before the rest could be added, so {who} {were} "
        "left off."
    )


def _addition_summary(outcome: _AdditionOutcome) -> str:
    waitlisted = outcome.waitlisted
    seated = [
        user_id for user_id in outcome.added if user_id not in waitlisted
    ]
    lines: list[str] = []
    if seated:
        lines.append(f"Added {_mention_list(seated)} to the roster.")
    elif not outcome.added:
        lines.append("Nobody was added to the roster.")
    if waitlisted:
        lines.append(
            "The event is full, so "
            + _mention_list(waitlisted)
            + (
                " was added to the waitlist."
                if len(waitlisted) == 1
                else " were added to the waitlist."
            )
        )
    if outcome.skipped:
        skipped = _mention_list(outcome.skipped)
        lines.append(
            f"{skipped} was already signed up for this event."
            if len(outcome.skipped) == 1
            else f"{skipped} were already signed up for this event."
        )
    if outcome.departed:
        lines.append(
            _mention_list(outcome.departed)
            + (" has" if len(outcome.departed) == 1 else " have")
            + " left the server, so they were not added."
        )
    if outcome.failed:
        lines.append(
            f"Could not add {_mention_list(outcome.failed)} to the roster."
        )
    if outcome.left_off:
        lines.append(_addition_stop_note(outcome.stop, outcome.left_off))
    elif outcome.stop is not None:
        lines.append(_addition_finished_note(outcome.stop))
    if outcome.undelivered:
        # The addition itself went through; only the notice did not, which the
        # commander needs to know so they can pass the word along.
        lines.append(
            "Could not send a direct message to "
            + _mention_list(outcome.undelivered)
            + ", so they were not notified."
        )
    return "\n".join(lines)
