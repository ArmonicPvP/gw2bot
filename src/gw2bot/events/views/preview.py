"""The event preview, and the confirmation a commander answers it with.

Every flow renders through here, and the preview's own buttons open those
flows back up. That loop is real rather than accidental, so the concrete
views are imported inside the functions that build them; importing them at
module scope would be a cycle.
"""

from __future__ import annotations

import logging
import re
from dataclasses import field
from typing import TYPE_CHECKING

import discord
from discord.utils import MISSING

from gw2bot.events.formatting import (
    calendar_footer_link,
    confirm_embed,
    describe_repeat,
    details_confirm_embed,
    details_preview_embed,
    edit_confirm_embed,
    event_embed,
    roster_edit_embed,
)
from gw2bot.events.models import EventOccurrence, EventStatus, RepeatFrequency
from gw2bot.events.views.shared import (
    EventDraft,
    PREVIEW_EVENT_ID_TEXT,
    _CHANGE_FIELDS,
    _EditFlowView,
    _append_ping_note,
    _editing_occurrence,
    _is_ephemeral_component_interaction,
    _preview_status,
)

if TYPE_CHECKING:
    from gw2bot.bot import Gw2Bot

LOGGER = logging.getLogger(__name__)


def build_details_preview(
    bot: Gw2Bot,
    draft: EventDraft,
) -> tuple[list[discord.Embed], discord.ui.View]:
    # Imported here rather than at module scope: the confirm view's own
    # "Change something" button renders back through this module, so the two
    # import each other and only one of them can do it at import time.
    from gw2bot.events.views.create import EventDetailsConfirmView

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
        calendar_url=calendar_footer_link(bot._config),
    )
    confirmation = details_confirm_embed()
    _append_ping_note(confirmation, draft)
    return [preview, confirmation], EventDetailsConfirmView(bot, draft)


def build_event_preview(
    bot: Gw2Bot,
    draft: EventDraft,
    *,
    primary: EventOccurrence | None = None,
) -> tuple[list[discord.Embed], discord.ui.View]:
    # Imported here rather than at module scope, for the reason above: each of
    # these views renders back through this module.
    from gw2bot.events.views.create import (
        EventConfirmView,
        EventEditConfirmView,
    )
    from gw2bot.events.views.roster import EventRosterEditView

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
            calendar_url=calendar_footer_link(bot._config),
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
            calendar_url=calendar_footer_link(bot._config),
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
    if not draft.roster_only:
        # A running event's details are frozen, so its ping roles are no longer
        # something the roster editor can act on.
        _append_ping_note(confirmation, draft)
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


class _PreviewConfirmView(_EditFlowView):
    def change_fields(self) -> tuple[tuple[str, str], ...]:
        # A method rather than a class attribute so a subclass can narrow the
        # list; the step-one preview offers only the fields entered by then.
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
        # Imported here rather than at module scope: the change flow re-renders
        # this preview when it is done, so the two import each other.
        from gw2bot.events.views.field_edit import ChangeFieldView

        await interaction.response.send_message(
            "What would you like to change?",
            view=ChangeFieldView(
                self._bot,
                self._draft,
                fields=self.change_fields(),
            ),
            ephemeral=True,
        )
