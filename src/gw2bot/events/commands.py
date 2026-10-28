from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import discord
from discord import app_commands

from gw2bot.core.command_access import RoleSetting, access_extras
from gw2bot.core.discord_utils import autocomplete_allowed, require_role
from gw2bot.events.formatting import format_event_datetime
from gw2bot.events.models import (
    Event,
    EventOccurrence,
    EventStatus,
    RepeatFrequency,
    SNAPSHOT_RETENTION,
    SnapshotKind,
)
from gw2bot.events.posting import split_event_history
from gw2bot.events.reminders import (
    occurrence_finished,
    reminder_participants,
    send_reminder,
)
from gw2bot.events.views import (
    EventCancelConfirmView,
    EventDeleteConfirmView,
    EventDetailsModal,
    EventDraft,
    open_event_edit,
    open_event_restore,
)

if TYPE_CHECKING:
    from gw2bot.bot import Gw2Bot

LOGGER = logging.getLogger(__name__)

EVENT_AUTOCOMPLETE_LIMIT = 25
EVENT_CHOICE_NAME_LIMIT = 100

# What a delete confirmation says about taking it back. The new post is a new
# message and thread: the old thread's messages are not something the bot can
# bring back.
RESTORE_NOTE = (
    "`/event restore` can put it back, with its sign-ups, for "
    f"{SNAPSHOT_RETENTION.days} days."
)


def _kept_history_note(kept: int) -> str:
    """What a delete confirmation promises about the runs it will not touch.

    Deleting an event leaves the runs it has already put on standing, so the
    confirmation has to say so before the commander decides - and say nothing
    at all for an event that has none, where there is no history to reassure
    anyone about. What it promises is what the deletion decides: the runs
    stay, with their place on the calendar, and their posts are not touched.
    Whether a post is still in the channel is not the deletion's to promise -
    a run whose message turned out to be gone keeps its row and the id of a
    message Discord no longer has.
    """
    if not kept:
        return ""
    if kept == 1:
        return (
            " Its one finished run is kept, with its place on the calendar, "
            "and its post is left alone."
        )
    return (
        f" Its {kept} finished runs are kept, with their places on the "
        "calendar, and their posts are left alone."
    )


def _event_choice_name(event: Event, note: str = "") -> str:
    # Keep the id visible even when a long title has to be truncated, since it
    # is the choice value the commander is really picking.
    prefix = f"[{event.category.value}] "
    suffix = f" — id {event.event_id}" + (f" ({note})" if note else "")
    budget = EVENT_CHOICE_NAME_LIMIT - len(prefix) - len(suffix)
    title = event.title
    if budget < 1:
        return f"{prefix}{suffix}"[:EVENT_CHOICE_NAME_LIMIT]
    if len(title) > budget:
        title = title[: budget - 1].rstrip() + "…"
    return f"{prefix}{title}{suffix}"


class EventCommands(app_commands.Group):
    def __init__(self, bot: Gw2Bot):
        super().__init__(
            name="event",
            description="Manage guild events",
            guild_only=True,
            # Every subcommand checks this role before doing anything.
            extras=access_extras(RoleSetting("event_create_role_id")),
        )
        self._bot = bot

    @app_commands.command(name="new", description="Create a new guild event")
    async def new(self, interaction: discord.Interaction) -> None:
        LOGGER.debug(
            "Event creation command invoked by Discord user %s",
            interaction.user.id,
        )
        if not await require_role(
            interaction,
            self._bot._config.event_create_role_id,
            action="event creation command",
            denial="You do not have the required role to create events.",
        ):
            return
        draft = EventDraft(leader_discord_id=interaction.user.id)
        # The guild is what the ping-role picker is built from; the command is
        # guild-only, so it is only ever missing if Discord sends the
        # interaction without one.
        modal = EventDetailsModal(self._bot, draft, interaction.guild)
        await interaction.response.send_modal(modal)
        LOGGER.debug(
            "Event creation modal opened; user_id=%s ping_roles_offered=%s",
            interaction.user.id,
            0 if modal.ping_roles is None else len(modal.ping_roles.options),
        )

    async def active_event_id_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[int]]:
        # Shared by /event edit and /event delete. Autocomplete must never error
        # out; unauthorized users simply see no suggestions (each command still
        # enforces the role).
        if not autocomplete_allowed(
            interaction,
            self._bot._config.event_create_role_id,
            action="active event",
        ):
            return []
        query = current.strip().casefold()
        choices: list[app_commands.Choice[int]] = []
        for event in self._bot.event_store.get_active_events():
            if query and (
                query not in event.title.casefold()
                and query not in str(event.event_id)
            ):
                continue
            choices.append(
                app_commands.Choice(
                    name=_event_choice_name(event),
                    value=event.event_id,
                )
            )
            if len(choices) >= EVENT_AUTOCOMPLETE_LIMIT:
                break
        LOGGER.debug(
            "Returning active event autocomplete choices; choices=%s",
            len(choices),
        )
        return choices

    async def restorable_event_id_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[int]]:
        # The events with a deletion or an edit /event restore can still
        # undo. Like the active autocomplete above, it never errors out and
        # offers nothing to a member without the role.
        if not autocomplete_allowed(
            interaction,
            self._bot._config.event_create_role_id,
            action="restorable event",
        ):
            return []
        query = current.strip().casefold()
        now = datetime.now(UTC)
        choices: list[app_commands.Choice[int]] = []
        for snapshot in self._bot.event_store.get_snapshots(now):
            event = snapshot.event
            note = "deleted"
            if snapshot.kind is SnapshotKind.DELETE and not (
                snapshot.can_restore(now)
            ):
                # A one-off event whose run ended after it was deleted has
                # nothing to bring back, and never will again.
                continue
            if snapshot.kind is SnapshotKind.EDIT:
                # Listed under the title it has now, which is the one the
                # commander is looking at in the channel.
                event = (
                    self._bot.event_store.get_event(event.event_id) or event
                )
                note = "edited"
            if query and (
                query not in event.title.casefold()
                and query not in snapshot.event.title.casefold()
                and query not in str(event.event_id)
            ):
                continue
            choices.append(
                app_commands.Choice(
                    name=_event_choice_name(event, note),
                    value=event.event_id,
                )
            )
            if len(choices) >= EVENT_AUTOCOMPLETE_LIMIT:
                break
        LOGGER.debug(
            "Returning restorable event autocomplete choices; choices=%s",
            len(choices),
        )
        return choices

    @app_commands.command(
        name="edit", description="Edit an existing guild event"
    )
    @app_commands.describe(
        event_id="The event to edit (shown as eventID in its footer)"
    )
    @app_commands.autocomplete(event_id=active_event_id_autocomplete)
    async def edit(
        self,
        interaction: discord.Interaction,
        event_id: int,
    ) -> None:
        LOGGER.debug(
            "Event edit command invoked by Discord user %s; event_id=%s",
            interaction.user.id,
            event_id,
        )
        if not await require_role(
            interaction,
            self._bot._config.event_create_role_id,
            action="event edit command",
            denial="You do not have the required role to edit events.",
        ):
            return
        await open_event_edit(self._bot, interaction, event_id)

    def _next_occurrence(
        self,
        event: Event,
        now: datetime | None = None,
    ) -> EventOccurrence | None:
        # The occurrence an event is currently about: the earliest one that has
        # not finished, which is the roster a manual reminder pings and the run
        # `/event cancel` calls off. A recurring series can hold a later
        # occurrence too, and pinging (or cancelling) next week's run while
        # this week's has not come around yet is never what was meant.
        #
        # Finishing is judged on the clock as well as on the stored status: the
        # status only moves when a maintenance pass persists it, so an
        # occurrence that ended moments ago (or during downtime) can still read
        # as ONGOING, and reminding its roster would announce an event that is
        # already behind them. The stored status still counts on its own,
        # because an occurrence whose message was deleted is retired as OVER
        # before its end time.
        current_time = now if now is not None else datetime.now(UTC)
        for occurrence in self._bot.event_store.get_event_occurrences(
            event.event_id
        ):
            if occurrence.status is EventStatus.OVER:
                continue
            if occurrence_finished(event, occurrence, current_time):
                continue
            return occurrence
        return None

    @app_commands.command(
        name="remind",
        description="Ping an event's members that it is coming up",
    )
    @app_commands.describe(
        event_id="The event to remind about (shown as eventID in its footer)"
    )
    @app_commands.autocomplete(event_id=active_event_id_autocomplete)
    async def remind(
        self,
        interaction: discord.Interaction,
        event_id: int,
    ) -> None:
        LOGGER.debug(
            "Event remind command invoked by Discord user %s; event_id=%s",
            interaction.user.id,
            event_id,
        )
        if not await require_role(
            interaction,
            self._bot._config.event_create_role_id,
            action="event remind command",
            denial="You do not have the required role to remind event members.",
        ):
            return
        event = self._bot.event_store.get_event(event_id)
        occurrence = (
            self._next_occurrence(event)
            if event is not None and not event.cancelled
            else None
        )
        if event is None or occurrence is None:
            LOGGER.debug(
                "Event remind rejected for a missing or completed event; "
                "user_id=%s event_id=%s exists=%s",
                interaction.user.id,
                event_id,
                event is not None,
            )
            await interaction.response.send_message(
                "That event does not exist or is over, so there is nobody "
                "left to remind.",
                ephemeral=True,
            )
            return
        if occurrence.thread_id is None:
            LOGGER.debug(
                "Event remind rejected without a thread; user_id=%s "
                "event_id=%s occurrence_id=%s posted=%s",
                interaction.user.id,
                event_id,
                occurrence.occurrence_id,
                occurrence.message_id is not None,
            )
            await interaction.response.send_message(
                "That event has no thread or forum post to remind its members "
                "in yet.",
                ephemeral=True,
            )
            return
        signups = self._bot.event_store.get_signups(occurrence.occurrence_id)
        participants = reminder_participants(signups)
        if not participants:
            LOGGER.debug(
                "Event remind found nobody to ping; user_id=%s event_id=%s "
                "occurrence_id=%s",
                interaction.user.id,
                event_id,
                occurrence.occurrence_id,
            )
            await interaction.response.send_message(
                "Nobody is signed up for that event yet.",
                ephemeral=True,
            )
            return
        # The reminder is one or more Discord sends, which can outrun the three
        # seconds an interaction has to be answered in.
        await interaction.response.defer(ephemeral=True)
        sent = await send_reminder(self._bot, event, occurrence, signups)
        if not sent:
            await interaction.followup.send(
                "The reminder could not be sent. Check that the bot can post "
                "in the event's thread or forum post.",
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            f"Reminded {len(participants)} member(s) about **{event.title}**.",
            ephemeral=True,
        )
        LOGGER.debug(
            "Sent a manual event reminder; user_id=%s event_id=%s "
            "occurrence_id=%s participants=%s",
            interaction.user.id,
            event_id,
            occurrence.occurrence_id,
            len(participants),
        )

    @app_commands.command(
        name="cancel",
        description="Cancel an event's next occurrence",
    )
    @app_commands.describe(
        event_id="The event to cancel (shown as eventID in its footer)"
    )
    @app_commands.autocomplete(event_id=active_event_id_autocomplete)
    async def cancel(
        self,
        interaction: discord.Interaction,
        event_id: int,
    ) -> None:
        LOGGER.debug(
            "Event cancel command invoked by Discord user %s; event_id=%s",
            interaction.user.id,
            event_id,
        )
        if not await require_role(
            interaction,
            self._bot._config.event_create_role_id,
            action="event cancel command",
            denial="You do not have the required role to cancel events.",
        ):
            return
        event = self._bot.event_store.get_event(event_id)
        if event is None or event.cancelled:
            LOGGER.debug(
                "Event cancel rejected for missing event; "
                "user_id=%s event_id=%s exists=%s",
                interaction.user.id,
                event_id,
                event is not None,
            )
            await interaction.response.send_message(
                "That event does not exist.",
                ephemeral=True,
            )
            return
        # Resolve the run first, whether or not the event repeats. Cancelling
        # is about a run still to come, and an event whose last run is behind
        # it has none - offering to delete a one-off event in that state would
        # retire the event, and drop the roles and automatic sign-ups it
        # remembers, through a command that only ever promised to call off the
        # next run.
        occurrence = self._next_occurrence(event)
        if occurrence is None:
            LOGGER.debug(
                "Event cancel rejected without an upcoming occurrence; "
                "user_id=%s event_id=%s repeats=%s",
                interaction.user.id,
                event_id,
                event.repeat_frequency is not RepeatFrequency.NONE,
            )
            await interaction.response.send_message(
                "That event has no upcoming occurrence left to cancel.",
                ephemeral=True,
            )
            return
        if event.repeat_frequency is RepeatFrequency.NONE:
            # A one-off event has nothing after the run being called off, so
            # cancelling it is deleting it. Hand over to the delete
            # confirmation rather than leaving an event row behind with no
            # occurrence for the scheduler to ever post.
            LOGGER.debug(
                "Event cancel fell back to deletion for a one-off event; "
                "user_id=%s event_id=%s",
                interaction.user.id,
                event_id,
            )
            kept, _ = split_event_history(
                event,
                self._bot.event_store.get_event_occurrences(event_id),
                datetime.now(UTC),
            )
            await interaction.response.send_message(
                f"**{event.title}** (event **{event.event_id}**) does not "
                "repeat, so cancelling it deletes the event. This removes "
                "the run it is on: its message, any signup thread the bot "
                "opened for it and everyone's sign-ups for it."
                f"{_kept_history_note(len(kept))} A forum post the event was "
                f"posted into is kept. {RESTORE_NOTE}",
                view=EventDeleteConfirmView(
                    self._bot,
                    event,
                    only_while_one_off=True,
                ),
                ephemeral=True,
            )
            return
        starts_at = format_event_datetime(
            occurrence.start_time,
            self._bot.event_timezone,
        )
        await interaction.response.send_message(
            f"Cancel **{event.title}** (event **{event.event_id}**) on "
            f"{starts_at}? This removes that occurrence's message, any signup "
            "thread the bot opened for it and everyone's sign-ups for it, and "
            "cannot be undone. A forum post the event was posted into is "
            "kept. The event keeps repeating, so its next occurrence is "
            "posted right away.",
            view=EventCancelConfirmView(self._bot, event, occurrence),
            ephemeral=True,
        )
        LOGGER.debug(
            "Event cancel confirmation opened; user_id=%s event_id=%s "
            "occurrence_id=%s",
            interaction.user.id,
            event_id,
            occurrence.occurrence_id,
        )

    @app_commands.command(
        name="delete", description="Delete an existing guild event"
    )
    @app_commands.describe(
        event_id="The event to delete (shown as eventID in its footer)"
    )
    @app_commands.autocomplete(event_id=active_event_id_autocomplete)
    async def delete(
        self,
        interaction: discord.Interaction,
        event_id: int,
    ) -> None:
        LOGGER.debug(
            "Event delete command invoked by Discord user %s; event_id=%s",
            interaction.user.id,
            event_id,
        )
        if not await require_role(
            interaction,
            self._bot._config.event_create_role_id,
            action="event delete command",
            denial="You do not have the required role to delete events.",
        ):
            return
        event = self._bot.event_store.get_event(event_id)
        if event is None:
            LOGGER.debug(
                "Event delete rejected for missing event; "
                "user_id=%s event_id=%s",
                interaction.user.id,
                event_id,
            )
            await interaction.response.send_message(
                "That event does not exist.",
                ephemeral=True,
            )
            return
        kept, removed = split_event_history(
            event,
            self._bot.event_store.get_event_occurrences(event_id),
            datetime.now(UTC),
        )
        # An event whose runs have all finished loses none, so there is
        # nothing for a restore to post again.
        restore_note = f" {RESTORE_NOTE}" if removed else ""
        await interaction.response.send_message(
            f"Delete **{event.title}** (event **{event.event_id}**)? This "
            "removes every run that has not finished, the one in progress "
            "included: their message(s), any signup thread(s) the bot "
            "opened for them and everyone's sign-ups for them."
            f"{_kept_history_note(len(kept))} A forum post the event was "
            f"posted into is kept.{restore_note}",
            view=EventDeleteConfirmView(self._bot, event),
            ephemeral=True,
        )
        LOGGER.debug(
            "Event delete confirmation opened; user_id=%s event_id=%s",
            interaction.user.id,
            event_id,
        )

    @app_commands.command(
        name="restore",
        description="Restore a deleted event, or undo an event's last edit",
    )
    @app_commands.describe(
        event_id="The deleted or edited event to restore"
    )
    @app_commands.autocomplete(event_id=restorable_event_id_autocomplete)
    async def restore(
        self,
        interaction: discord.Interaction,
        event_id: int,
    ) -> None:
        LOGGER.debug(
            "Event restore command invoked by Discord user %s; event_id=%s",
            interaction.user.id,
            event_id,
        )
        if not await require_role(
            interaction,
            self._bot._config.event_create_role_id,
            action="event restore command",
            denial="You do not have the required role to restore events.",
        ):
            return
        await open_event_restore(self._bot, interaction, event_id)
