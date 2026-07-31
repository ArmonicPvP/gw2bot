from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import discord
from discord import app_commands

from gw2bot.discord_utils import user_has_role
from gw2bot.events.models import Event, EventOccurrence, EventStatus
from gw2bot.events.reminders import reminder_participants, send_reminder
from gw2bot.events.roles import EVENT_CREATE_ROLE_ID
from gw2bot.events.views import (
    ONGOING_EDIT_REJECTION,
    EventDeleteConfirmView,
    EventDetailsModal,
    EventDraft,
    draft_from_event,
    send_event_preview,
)

if TYPE_CHECKING:
    from gw2bot.bot import Gw2Bot

LOGGER = logging.getLogger(__name__)

EVENT_AUTOCOMPLETE_LIMIT = 25
EVENT_CHOICE_NAME_LIMIT = 100


def _event_choice_name(event: Event) -> str:
    # Keep the id visible even when a long title has to be truncated, since it
    # is the choice value the commander is really picking.
    prefix = f"[{event.category.value}] "
    suffix = f" — id {event.event_id}"
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
        )
        self._bot = bot

    @app_commands.command(name="new", description="Create a new guild event")
    async def new(self, interaction: discord.Interaction) -> None:
        LOGGER.debug(
            "Event creation command invoked by Discord user %s",
            interaction.user.id,
        )
        if not user_has_role(interaction.user, EVENT_CREATE_ROLE_ID):
            LOGGER.warning(
                "Rejected event creation command from Discord user %s; "
                "required role %s",
                interaction.user.id,
                EVENT_CREATE_ROLE_ID,
            )
            await interaction.response.send_message(
                "You do not have the required role to create events.",
                ephemeral=True,
            )
            return
        draft = EventDraft(leader_discord_id=interaction.user.id)
        await interaction.response.send_modal(
            EventDetailsModal(self._bot, draft)
        )
        LOGGER.debug(
            "Event creation modal opened; user_id=%s",
            interaction.user.id,
        )

    async def active_event_id_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[int]]:
        # Shared by /event edit and /event delete. Autocomplete must never error
        # out; unauthorized users simply see no suggestions (each command still
        # enforces the role).
        if not user_has_role(interaction.user, EVENT_CREATE_ROLE_ID):
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
        if not user_has_role(interaction.user, EVENT_CREATE_ROLE_ID):
            LOGGER.warning(
                "Rejected event edit command from Discord user %s; "
                "required role %s",
                interaction.user.id,
                EVENT_CREATE_ROLE_ID,
            )
            await interaction.response.send_message(
                "You do not have the required role to edit events.",
                ephemeral=True,
            )
            return
        event = self._bot.event_store.get_event(event_id)
        # Fetch occurrences once and reuse them for the editability check, the
        # draft's start time, and the preview roster.
        live = (
            [
                occurrence
                for occurrence in self._bot.event_store.get_event_occurrences(
                    event_id
                )
                if occurrence.status is not EventStatus.OVER
            ]
            if event is not None and not event.cancelled
            else []
        )
        if event is None or event.cancelled or not live:
            LOGGER.debug(
                "Event edit rejected for missing or completed event; "
                "user_id=%s event_id=%s exists=%s",
                interaction.user.id,
                event_id,
                event is not None,
            )
            await interaction.response.send_message(
                "That event does not exist or is over and can no longer be "
                "edited.",
                ephemeral=True,
            )
            return
        # An event that has started is ongoing: it can only be deleted. Editing
        # it would re-render a live roster, and shortening its duration would
        # persist OVER without seeding a recurring series' next occurrence.
        if any(
            occurrence.start_time <= datetime.now(UTC) for occurrence in live
        ):
            LOGGER.debug(
                "Event edit rejected for an ongoing event; "
                "user_id=%s event_id=%s",
                interaction.user.id,
                event_id,
            )
            await interaction.response.send_message(
                ONGOING_EDIT_REJECTION,
                ephemeral=True,
            )
            return
        primary = live[0]
        draft = draft_from_event(
            event,
            self._bot.event_timezone,
            start_time_override=primary.start_time,
        )
        await send_event_preview(
            self._bot,
            interaction,
            draft,
            primary=primary,
        )
        LOGGER.debug(
            "Event edit preview opened; user_id=%s event_id=%s",
            interaction.user.id,
            event_id,
        )

    def _reminder_occurrence(self, event_id: int) -> EventOccurrence | None:
        # The occurrence a manual reminder is about: the earliest one that has
        # not finished, which is the one whose roster is being reminded. A
        # recurring series can hold a later occurrence too, and pinging next
        # week's roster about an event that has not come around yet would be
        # noise.
        for occurrence in self._bot.event_store.get_event_occurrences(event_id):
            if occurrence.status is not EventStatus.OVER:
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
        if not user_has_role(interaction.user, EVENT_CREATE_ROLE_ID):
            LOGGER.warning(
                "Rejected event remind command from Discord user %s; "
                "required role %s",
                interaction.user.id,
                EVENT_CREATE_ROLE_ID,
            )
            await interaction.response.send_message(
                "You do not have the required role to remind event members.",
                ephemeral=True,
            )
            return
        event = self._bot.event_store.get_event(event_id)
        occurrence = (
            self._reminder_occurrence(event_id)
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
        if not user_has_role(interaction.user, EVENT_CREATE_ROLE_ID):
            LOGGER.warning(
                "Rejected event delete command from Discord user %s; "
                "required role %s",
                interaction.user.id,
                EVENT_CREATE_ROLE_ID,
            )
            await interaction.response.send_message(
                "You do not have the required role to delete events.",
                ephemeral=True,
            )
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
        await interaction.response.send_message(
            f"Delete **{event.title}** (event **{event.event_id}**)? This "
            "removes its message(s), any signup thread(s) the bot opened for "
            "them and everyone's sign-ups, and cannot be undone. A forum post "
            "the event was posted into is kept.",
            view=EventDeleteConfirmView(self._bot, event),
            ephemeral=True,
        )
        LOGGER.debug(
            "Event delete confirmation opened; user_id=%s event_id=%s",
            interaction.user.id,
            event_id,
        )
