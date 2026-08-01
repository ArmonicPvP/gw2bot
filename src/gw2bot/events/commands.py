from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from sqlalchemy.exc import SQLAlchemyError

from gw2bot.discord_utils import user_has_role
from gw2bot.events.models import Event, EventStatus
from gw2bot.events.roles import EVENT_CREATE_ROLE_ID
from gw2bot.events.views import (
    EventDeleteConfirmView,
    EventDetailsModal,
    EventDraft,
    draft_from_event,
    occurrence_has_ended,
    prune_departed_members,
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
        now = datetime.now(UTC)
        # The soonest live occurrence is the one being edited; an event with a
        # started occurrence always has it first, because they are ordered by
        # start time.
        primary = live[0] if live else None
        if event is None or event.cancelled or primary is None:
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
        # An event that has started is in progress: its stored details are
        # frozen, because re-rendering it from an edit can persist OVER without
        # seeding a recurring series' next occurrence, and rescheduling or
        # moving a run people are already in helps nobody. Its roster is still
        # live, though, so that alone stays editable.
        roster_only = primary.start_time <= now
        if roster_only and occurrence_has_ended(event, primary, now):
            # Past its end but not yet retired by the scheduler: the roster is
            # history now, so there is nothing left to edit either.
            LOGGER.debug(
                "Event edit rejected for a finished occurrence awaiting "
                "retirement; user_id=%s event_id=%s",
                interaction.user.id,
                event_id,
            )
            await interaction.response.send_message(
                "That event does not exist or is over and can no longer be "
                "edited.",
                ephemeral=True,
            )
            return
        draft = draft_from_event(
            event,
            self._bot.event_timezone,
            start_time_override=primary.start_time,
            roster_only=roster_only,
            # A running event is minutes from ending, and the scheduler seeds
            # the series' next occurrence the moment it does. Pin the run being
            # edited so the session cannot drift onto that successor's roster.
            editing_occurrence_id=(
                primary.occurrence_id if roster_only else None
            ),
        )
        # Checking the roster against the server is one Discord fetch per
        # member, which cannot finish inside the three-second interaction
        # window, so acknowledge first and send the preview as a follow-up.
        await interaction.response.defer(ephemeral=True)
        departed_note = None
        try:
            departed_note, _ = await prune_departed_members(
                self._bot,
                interaction.guild,
                event,
                primary,
            )
        except (discord.DiscordException, SQLAlchemyError) as exc:
            # A roster the bot could not clean up is still an editable roster,
            # so report nothing and show the preview as it stands.
            LOGGER.error(
                "Could not check the roster for departed members; "
                "event_id=%s error_type=%s",
                event_id,
                type(exc).__name__,
            )
        await send_event_preview(
            self._bot,
            interaction,
            draft,
            primary=primary,
            deferred=True,
            content=departed_note,
        )
        LOGGER.debug(
            "Event edit preview opened; user_id=%s event_id=%s "
            "roster_only=%s departed_removed=%s",
            interaction.user.id,
            event_id,
            roster_only,
            departed_note is not None,
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
