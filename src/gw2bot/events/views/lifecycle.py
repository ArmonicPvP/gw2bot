"""Applying an edit, moving an event's channel, and deleting or cancelling it."""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import discord
from sqlalchemy.exc import SQLAlchemyError

from gw2bot.core.discord_utils import user_has_role
from gw2bot.events.formatting import format_event_datetime
from gw2bot.events.models import (
    Event,
    EventOccurrence,
    EventStatus,
    RepeatFrequency,
    RosterUpdate,
)
from gw2bot.events.views.preview import send_event_preview
from gw2bot.events.views.shared import (
    EventDraft,
    FINISHED_EDIT_REJECTION,
    FLOW_TIMEOUT_SECONDS,
    ONGOING_EDIT_REJECTION,
    _mark_occurrence_stale,
    _restore_event_channel,
    _send_flow_decline,
    _send_flow_result,
    occurrence_has_ended,
)

if TYPE_CHECKING:
    from gw2bot.bot import Gw2Bot
    from gw2bot.events.posting import OccurrenceCancellation

LOGGER = logging.getLogger(__name__)


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
        if not user_has_role(
            interaction.user,
            self._bot._config.event_create_role_id,
        ):
            LOGGER.warning(
                "Rejected event channel move from Discord user %s; required "
                "role %s",
                interaction.user.id,
                self._bot._config.event_create_role_id,
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
        check_roster_membership,
        merge_roster_updates,
        notify_roster_update,
        occurrence_finished,
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
    # Nothing live left to edit: the series ended, or `/event delete` retired
    # it while this preview sat open, keeping its finished runs and the event
    # row they are read through. The command refuses an event in that state,
    # and saving now would rewrite the title, category and duration the
    # calendar shows for runs that already happened - while their posts, which
    # nothing refreshes again, kept the details they were run under.
    if not occurrences:
        LOGGER.warning(
            "Rejected edit of an event with no runs left; event_id=%s "
            "user_id=%s",
            editing_event_id,
            interaction.user.id,
        )
        await interaction.edit_original_response(
            content=FINISHED_EDIT_REJECTION,
            view=None,
        )
        return
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
            ping_role_ids=edited.ping_role_ids,
            requirements=edited.requirements,
            mentee_enabled=edited.mentee_enabled,
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
            # Re-seat the members who are actually still here. The preview's
            # check can be minutes old by the time the pickers and the modal
            # are done with, and a departed member re-seated under the new
            # capacity would hold one of its seats - or be handed a better one
            # - for a squad they cannot see, with their automatic sign-up
            # still on. A move checks again after its post as well, which is a
            # different question: who is still here to be subscribed to the
            # thread it has just opened.
            checked = RosterUpdate()
            try:
                _, checked = await check_roster_membership(
                    bot,
                    updated,
                    current,
                    force=True,
                    notify=False,
                )
            except (discord.DiscordException, SQLAlchemyError) as exc:
                # A roster the bot could not check is still a roster to
                # re-seat; the departures wait for the next check.
                LOGGER.error(
                    "Could not check the roster before a category rebalance; "
                    "occurrence_id=%s error_type=%s",
                    current.occurrence_id,
                    type(exc).__name__,
                )
            # The check removes through remove_signup, which re-solves the
            # roster it leaves behind, so the rebalance below must see those
            # rows rather than the ones read before it. That removal also
            # refreshes a message somebody may have deleted, and the NotFound
            # behind it retires the run: re-seating a roster that is history,
            # and re-rendering a message that is gone, helps nobody. Every
            # occurrence here is still in the future, so only that can end one.
            try:
                reread = bot.event_store.get_occurrence(
                    current.occurrence_id
                )
                # The event comes back with it. This edit's own save is
                # committed, but the check awaits Discord for every member,
                # and another leader saving in that window leaves the row
                # holding their category, title, channel and duration. The
                # re-seat and the render below have to describe the event
                # that is stored, or the roster and the public post are left
                # disagreeing with it.
                resaved = bot.event_store.get_event(updated.event_id)
            except SQLAlchemyError as exc:
                # The event row is already saved, so this must not escape the
                # callback and leave the commander on "Saving your changes".
                # A store that cannot answer cannot re-seat either.
                #
                # Not the same thing as a run that has gone, though, which is
                # why this does not fall into the branch below: that message
                # is still in its channel carrying the old category, and
                # calling the edit applied would leave it there. Counted as a
                # posted occurrence that failed to refresh, so the scheduler
                # retries it, the commander is told, and a move puts the
                # channel back rather than pointing the event at one its post
                # never reached.
                LOGGER.error(
                    "Could not re-read the occurrence after its roster "
                    "check; occurrence_id=%s error_type=%s",
                    current.occurrence_id,
                    type(exc).__name__,
                )
                await notify_roster_update(bot, current, checked)
                if current.message_id is not None:
                    attempted += 1
                    _mark_occurrence_stale(bot, current)
                continue
            if (
                reread is None
                or resaved is None
                or occurrence_finished(resaved, reread)
            ):
                LOGGER.debug(
                    "Skipped a category rebalance for a run the check "
                    "retired; occurrence_id=%s exists=%s event_exists=%s",
                    current.occurrence_id,
                    reread is not None,
                    resaved is not None,
                )
                await notify_roster_update(bot, current, checked)
                continue
            current = reread
            updated = resaved
            # The category picks the capacity the roster was seated against, so
            # changing it invalidates every stored assignment. Re-seat the roster
            # before the message is re-rendered, so the embed and the capacity
            # checks both describe the new category, and announce the moves in
            # the occurrence's thread so members learn their new seat.
            try:
                _, rebalanced = rebalance_occurrence_roster(
                    bot, updated, current
                )
                # The check moved the roster before the re-seat did, and it
                # can have moved the same member: folded, they read as one
                # move, and anybody it took off is dropped rather than given a
                # seat in the new squad.
                roster_update = merge_roster_updates([checked, rebalanced])
            except (SQLAlchemyError, ValueError) as exc:
                # A stale roster must not block the rest of the edit.
                LOGGER.error(
                    "Could not rebalance roster after a category change; "
                    "occurrence_id=%s error_type=%s",
                    current.occurrence_id,
                    type(exc).__name__,
                )
                # The check's own movements are committed whatever the
                # re-seat did, so they are still what gets announced.
                roster_update = checked
            if not moving:
                # For an in-place refresh the thread is stable, so announce
                # what moved the roster now - the re-seat and the check
                # folded, or just the check when the re-seat failed. A channel
                # move deletes this thread and opens a new one, so its ping is
                # deferred to after the repost below and re-targeted at the
                # new thread.
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
                # The re-post checks the roster against the server, which
                # can take departed members off it and re-seat the rest, so
                # the update computed above is handed over rather than sent
                # after it: the two are folded into one line per member,
                # inside the thread the move has just opened.
                reposted = await repost_occurrence(
                    bot,
                    updated,
                    current,
                    roster_update,
                )
                if reposted is None:
                    # The move's own roster check retired this run - its
                    # replacement message was gone by the time the check
                    # refreshed it - so there is no live post in the new
                    # channel to report as moved.
                    LOGGER.error(
                        "Moved occurrence retired during its roster check; "
                        "occurrence_id=%s",
                        current.occurrence_id,
                    )
                    continue
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


def _deleted_history_note(kept: int) -> str:
    """What a deletion reply says about the runs it left standing.

    It promises what the deletion itself decides - the runs stay, with their
    rosters and their place on the calendar, and their posts were not touched
    - rather than that the posts are still there. A run retires early when its
    message turns out to be gone, keeping the row and the id of a message
    Discord no longer has, so "its post is still up" is not the deletion's to
    promise.
    """
    if not kept:
        return ""
    if kept == 1:
        return (
            " Its one finished run was kept, with its place on the calendar, "
            "and its post was left alone."
        )
    return (
        f" Its {kept} finished runs were kept, with their places on the "
        "calendar, and their posts were left alone."
    )


class EventDeleteConfirmView(discord.ui.View):
    def __init__(
        self,
        bot: Gw2Bot,
        event: Event,
        *,
        only_while_one_off: bool = False,
    ):
        super().__init__(timeout=FLOW_TIMEOUT_SECONDS)
        self._bot = bot
        self._event = event
        self._deleting = False
        # Set when `/event cancel` opened this confirmation because the event
        # does not repeat, which is the only reason cancelling one run means
        # deleting the whole event. If an edit gives the event a repeat while
        # this sits open, that reason is gone and the deletion has to be
        # refused: `/event cancel` never removes more than the next run of a
        # repeating event.
        self._only_while_one_off = only_while_one_off

    @discord.ui.button(label="Delete event", style=discord.ButtonStyle.danger)
    async def delete(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[EventDeleteConfirmView],
    ) -> None:
        from gw2bot.events.posting import (
            delete_event_posts,
            refresh_retired_posts,
            split_event_history,
        )

        # The confirmation can sit open for minutes; recheck the role before the
        # irreversible delete, mirroring the edit/post paths.
        if not user_has_role(
            interaction.user,
            self._bot._config.event_create_role_id,
        ):
            LOGGER.warning(
                "Rejected event delete from Discord user %s; required role %s",
                interaction.user.id,
                self._bot._config.event_create_role_id,
            )
            await interaction.response.send_message(
                "You do not have the required role to delete events.",
                ephemeral=True,
            )
            return
        # Guard a double click racing two callbacks before the first removes the
        # buttons; the check and set are synchronous, so the second observes it.
        if self._deleting:
            LOGGER.debug(
                "Skipped a duplicate event deletion click; user_id=%s "
                "event_id=%s",
                interaction.user.id,
                self._event.event_id,
            )
            await interaction.response.send_message(
                "This event is already being deleted.",
                ephemeral=True,
            )
            return
        self._deleting = True
        try:
            await interaction.response.edit_message(
                content="Deleting the event…",
                embeds=[],
                view=None,
            )
        except discord.HTTPException as exc:
            # As above: nothing is deleted yet, so the guard must not outlive
            # a failed acknowledgement and lock the commander out of retrying.
            self._deleting = False
            LOGGER.error(
                "Could not acknowledge an event deletion; user_id=%s "
                "event_id=%s error_type=%s",
                interaction.user.id,
                self._event.event_id,
                type(exc).__name__,
            )
            return
        # Read after the acknowledgement, not before: that await is a Discord
        # round-trip, and an edit landing inside it would make a read taken
        # first stale by the time the deletion runs. The event this view was
        # opened with is a snapshot, and an edit is allowed through while the
        # confirmation sits open - the duration decides which runs count as
        # finished and the channel decides where their posts are addressed, so
        # both have to be the event's as it stands now. An event that has gone
        # in the meantime keeps the snapshot: its rows are gone with it, so
        # everything below finds nothing to do and says so.
        try:
            stored = self._bot.event_store.get_event(self._event.event_id)
        except SQLAlchemyError as exc:
            self._deleting = False
            LOGGER.error(
                "Could not re-read an event before deleting; event_id=%s "
                "error_type=%s",
                self._event.event_id,
                type(exc).__name__,
            )
            await _send_flow_result(
                interaction,
                "The event could not be deleted. Try again later.",
                workflow="event deletion failure",
                event_id=self._event.event_id,
            )
            return
        event = stored if stored is not None else self._event
        if self._only_while_one_off:
            from gw2bot.events.posting import leading_occurrence

            still_upcoming = stored is not None and (
                leading_occurrence(self._bot, stored, datetime.now(UTC))
                is not None
            )
            refusal: str | None = None
            reason = ""
            if stored is not None and not still_upcoming:
                # The run ended while this confirmation sat open. `/event
                # cancel` calls off runs still to come, so deleting now would
                # retire an event whose runs are all behind it - through a
                # command that never offered to.
                reason = "a finished event"
                refusal = (
                    "That event has already run, so there is nothing left to "
                    "cancel. Use `/event delete` if you want to remove it."
                )
            elif (
                stored is not None
                and stored.repeat_frequency is not RepeatFrequency.NONE
            ):
                reason = "an event that now repeats"
                refusal = (
                    "That event repeats now, so cancelling it would only call "
                    "off its next run rather than delete it. Run "
                    "`/event cancel` again to do that, or `/event delete` to "
                    "remove the whole event."
                )
            if refusal is not None:
                self._deleting = False
                LOGGER.debug(
                    "Event cancel deletion refused for %s; user_id=%s "
                    "event_id=%s",
                    reason,
                    interaction.user.id,
                    self._event.event_id,
                )
                await _send_flow_result(
                    interaction,
                    refusal,
                    workflow="event cancel deletion refusal",
                    event_id=self._event.event_id,
                )
                return
        # Read the occurrences before the store rows are removed so their
        # messages can still be cleaned up afterwards.
        occurrences = self._bot.event_store.get_event_occurrences(
            self._event.event_id
        )
        # The runs the event has already put on are history: they keep their
        # posts and their rows, so the calendar still shows what the guild
        # ran. Only what is left of the event is removed - and an event that
        # has nothing to keep goes entirely, rather than leaving a row behind
        # that nothing would ever show.
        kept, removed = split_event_history(
            event,
            occurrences,
            datetime.now(UTC),
        )
        try:
            if kept:
                self._bot.event_store.retire_event(
                    self._event.event_id,
                    [occurrence.occurrence_id for occurrence in removed],
                )
            else:
                self._bot.event_store.delete_event(self._event.event_id)
        except SQLAlchemyError as exc:
            self._deleting = False
            LOGGER.error(
                "Could not delete event; event_id=%s error_type=%s",
                self._event.event_id,
                type(exc).__name__,
            )
            await _send_flow_result(
                interaction,
                "The event could not be deleted. Try again later.",
                workflow="event deletion failure",
                event_id=self._event.event_id,
            )
            return
        await delete_event_posts(self._bot, event, removed)
        # The kept runs leave the maintenance pass for good with the rows
        # just retired, so this is the last chance to show one whose end no
        # pass had caught up with as the finished run it is.
        await refresh_retired_posts(
            self._bot,
            event,
            kept,
            datetime.now(UTC),
        )
        LOGGER.debug(
            "Deleted event; event_id=%s occurrences_removed=%s "
            "occurrences_kept=%s user_id=%s",
            self._event.event_id,
            len(removed),
            len(kept),
            interaction.user.id,
        )
        await _send_flow_result(
            interaction,
            f"Event **{self._event.event_id}** was deleted."
            + _deleted_history_note(len(kept)),
            workflow="event deletion",
            event_id=self._event.event_id,
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
        LOGGER.debug(
            "Event deletion declined; user_id=%s event_id=%s",
            interaction.user.id,
            self._event.event_id,
        )
        await _send_flow_decline(
            interaction,
            "The event was not deleted.",
            workflow="event deletion",
            event_id=self._event.event_id,
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
        if not user_has_role(
            interaction.user,
            self._bot._config.event_create_role_id,
        ):
            LOGGER.warning(
                "Rejected event cancel from Discord user %s; required role %s",
                interaction.user.id,
                self._bot._config.event_create_role_id,
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
            LOGGER.debug(
                "Skipped a duplicate event cancellation click; user_id=%s "
                "event_id=%s occurrence_id=%s",
                interaction.user.id,
                self._event.event_id,
                self._occurrence.occurrence_id,
            )
            await interaction.response.send_message(
                "This occurrence is already being cancelled.",
                ephemeral=True,
            )
            return
        self._cancelling = True
        # Answer the click before the checks below rather than after them. The
        # acknowledgement is a Discord round-trip, and an occurrence can end
        # (or be edited) inside it, so a check made first is already stale by
        # the time the cancellation runs; making it last is what keeps it the
        # word the mutation acts on.
        try:
            await interaction.response.edit_message(
                content="Cancelling the occurrence…",
                embeds=[],
                view=None,
            )
        except discord.HTTPException as exc:
            # Nothing has been touched yet and the confirmation still carries
            # its buttons, so the guard has to come back off: left set, it
            # would refuse every later click as an in-progress cancellation
            # until the view times out.
            self._cancelling = False
            LOGGER.error(
                "Could not acknowledge an event cancellation; user_id=%s "
                "event_id=%s occurrence_id=%s error_type=%s",
                interaction.user.id,
                self._event.event_id,
                self._occurrence.occurrence_id,
                type(exc).__name__,
            )
            return
        # The confirmation holds the event and the occurrence as they were when
        # it was opened, and either can be gone by the time it is answered: the
        # event deleted, or the occurrence pruned or retired. Cancelling from
        # those stale copies would seed a successor for an event that no longer
        # exists, so re-read both and stop if the run has already gone.
        try:
            event = self._bot.event_store.get_event(self._event.event_id)
            occurrence = self._bot.event_store.get_occurrence(
                self._occurrence.occurrence_id
            )
        except SQLAlchemyError as exc:
            # Nothing has been touched, so this is a plain retry - but the
            # buttons are gone with the acknowledgement, so it has to be said
            # rather than left on "Cancelling the occurrence…" forever.
            self._cancelling = False
            LOGGER.error(
                "Could not re-read an event before cancelling; event_id=%s "
                "occurrence_id=%s error_type=%s",
                self._event.event_id,
                self._occurrence.occurrence_id,
                type(exc).__name__,
            )
            await _send_flow_result(
                interaction,
                "The occurrence could not be cancelled. Try again later.",
                workflow="event cancellation failure",
                event_id=self._event.event_id,
            )
            return
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
            await _send_flow_result(
                interaction,
                "That occurrence is no longer there, so there is nothing left "
                "to cancel.",
                workflow="event cancel refusal",
                event_id=self._event.event_id,
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
            await _send_flow_result(
                interaction,
                "That occurrence has already run, so it can no longer be "
                "cancelled. Run `/event cancel` again for the next one.",
                workflow="event cancel refusal",
                event_id=self._event.event_id,
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
            await _send_flow_result(
                interaction,
                "That event no longer repeats, so this occurrence is all "
                "there is of it. Run `/event cancel` again to delete the "
                "event instead.",
                workflow="event cancel refusal",
                event_id=self._event.event_id,
            )
            return
        self._event = event
        self._occurrence = occurrence
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
            await _send_flow_result(
                interaction,
                "The occurrence could not be cancelled. Try again later.",
                workflow="event cancellation failure",
                event_id=self._event.event_id,
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
        await _send_flow_result(
            interaction,
            self._result_message(cancellation),
            workflow="event cancellation",
            event_id=self._event.event_id,
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
            if not cancellation.retry_pending:
                # Saying "run /event cancel again" here would be telling the
                # commander to delete the run this message just named: that is
                # what cancelling the unposted successor does. State what is
                # actually there and what the alternative costs instead.
                return (
                    f"**{title}** on {cancelled_on} was cancelled. Its next "
                    f"occurrence on {next_on} could not be posted in "
                    f"<#{self._event.channel_id}>, and the retry could not be "
                    "recorded either, so nothing will post it on its own. The "
                    "run and its sign-ups are still there. Fix the bot's "
                    "permissions in that channel, then use `/event cancel` "
                    "again only if you would rather skip that run and post "
                    "the one after it."
                )
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
        LOGGER.debug(
            "Event cancel declined; user_id=%s event_id=%s occurrence_id=%s",
            interaction.user.id,
            self._event.event_id,
            self._occurrence.occurrence_id,
        )
        await _send_flow_decline(
            interaction,
            "The occurrence was not cancelled.",
            workflow="event cancellation",
            event_id=self._event.event_id,
        )
