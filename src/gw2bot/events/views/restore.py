"""Undoing the last change to an event: putting back an event `/event delete`
took down, or the details its last edit replaced."""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import discord
from sqlalchemy.exc import SQLAlchemyError

from gw2bot.core.discord_utils import user_has_role
from gw2bot.events.audit import event_restored_audit, send_event_audit
from gw2bot.events.formatting import calendar_link, event_embed
from gw2bot.events.models import (
    Event,
    EventOccurrence,
    EventSnapshot,
    EventStatus,
    RepeatFrequency,
    SNAPSHOT_RETENTION,
    SnapshotKind,
    rebalance_signups,
)
from gw2bot.events.views.lifecycle import apply_event_edit
from gw2bot.events.views.shared import (
    FLOW_TIMEOUT_SECONDS,
    _CHANGE_FIELDS,
    _send_flow_decline,
    _send_flow_result,
    draft_from_event,
)

if TYPE_CHECKING:
    from gw2bot.bot import Gw2Bot

LOGGER = logging.getLogger(__name__)


STALE_RESTORE = (
    "That event has changed since this was opened, so this is no longer what "
    "restoring it would do. Run `/event restore` again to see what it would "
    "put back now."
)


@dataclass(frozen=True)
class _EditUndo:
    """What undoing an event's last edit would do, worked out against now."""

    # The event as it is stored now, before the undo.
    current: Event
    # The run the undo reschedules: the soonest one still to come, which is
    # the one apply_event_edit treats as the edit's.
    primary: EventOccurrence
    # Where that run starts once the edit is undone.
    start_time: datetime
    # The labels of the details the undo changes, in the edit flow's order.
    changed: tuple[str, ...]


def _join(labels: tuple[str, ...]) -> str:
    if len(labels) == 1:
        return labels[0]
    return f"{', '.join(labels[:-1])} and {labels[-1]}"


def _plural(count: int, singular: str, plural: str) -> str:
    return singular if count == 1 else plural


def _plan_edit_undo(
    bot: Gw2Bot,
    snapshot: EventSnapshot,
    now: datetime,
) -> _EditUndo | str:
    """Work out undoing an edit, or say why it cannot be undone now.

    Undoing is saving the old details as an edit, so it is held to what an
    edit is: an event that has started is frozen until the run is over, and
    one with no runs left has nothing to save over.

    The old details are the event row, whose start is the series origin
    rather than the run the commander sees. The edit moved that run and the
    origin by the same amount, so the undo moves the soonest run back by the
    origin's difference - which also lands right when the series has moved on
    to a later run since.
    """
    event_id = snapshot.event.event_id
    event = bot.event_store.get_event(event_id)
    live = (
        [
            occurrence
            for occurrence in bot.event_store.get_event_occurrences(event_id)
            if occurrence.status is not EventStatus.OVER
        ]
        if event is not None and not event.cancelled
        else []
    )
    if event is None or not live:
        return (
            "That event has no runs left, so its last edit can no longer be "
            "undone."
        )
    primary = live[0]
    if primary.start_time <= now:
        return (
            "That event has already started, so its details stay as they are "
            "until the run is over. Run `/event restore` again after that."
        )
    start_time = primary.start_time + (
        snapshot.event.start_time - event.start_time
    )
    if start_time <= now:
        return (
            "The time that event had before its last edit has already "
            "passed, so the edit can no longer be undone."
        )
    restored = snapshot.event
    differs = {
        "category": event.category is not restored.category,
        "title": event.title != restored.title,
        "description": event.description != restored.description,
        "channel": event.channel_id != restored.channel_id,
        "start": primary.start_time != start_time,
        "duration": event.duration_minutes != restored.duration_minutes,
        "requirements": event.requirements != restored.requirements,
        "max_participants": (
            event.max_participants != restored.max_participants
        ),
        "mentee": event.mentee_enabled != restored.mentee_enabled,
        "repeat": (
            event.repeat_frequency,
            event.repeat_days,
            event.delete_previous_on_repeat,
        )
        != (
            restored.repeat_frequency,
            restored.repeat_days,
            restored.delete_previous_on_repeat,
        ),
        "leader": event.leader_discord_id != restored.leader_discord_id,
        "ping_roles": event.ping_role_ids != restored.ping_role_ids,
    }
    changed = tuple(
        label.lower() for key, label in _CHANGE_FIELDS if differs[key]
    )
    if not changed:
        return (
            "That event already matches how it was before its last edit, so "
            "there is nothing to undo."
        )
    return _EditUndo(
        current=event,
        primary=primary,
        start_time=start_time,
        changed=changed,
    )


def _edit_undo_prompt(
    bot: Gw2Bot,
    snapshot: EventSnapshot,
    plan: _EditUndo,
) -> tuple[str, discord.Embed]:
    current = plan.current
    content = (
        f"Undo the last edit to **{current.title}** (event "
        f"**{current.event_id}**), made "
        f"<t:{int(snapshot.taken_at.timestamp())}:R>? This puts back its "
        f"{_join(plan.changed)}, as shown below."
    )
    if (
        current.channel_id != snapshot.event.channel_id
        and plan.primary.message_id is not None
    ):
        content += (
            f" Its post moves back to <#{snapshot.event.channel_id}>: the "
            "current message and any signup thread the bot opened for it are "
            "deleted, and the roster is posted there instead."
        )
    restored = replace(snapshot.event, start_time=plan.start_time)
    signups = bot.event_store.get_signups(plan.primary.occurrence_id)
    if (
        not restored.capacity.has_roles
        and restored.capacity != current.capacity
    ):
        # The undo re-seats a headcount roster against the size it puts
        # back, so the picture shows the seats it will leave, as the edit
        # preview does.
        signups = rebalance_signups(restored.capacity, signups)
    embed = event_embed(
        restored,
        signups,
        EventStatus.OPEN,
        event_id_text=str(current.event_id),
        calendar_url=calendar_link(bot._config),
    )
    return content, embed


def _deletion_prompt(snapshot: EventSnapshot, now: datetime) -> str:
    event = snapshot.event
    runs = snapshot.restorable_runs(now)
    signups = sum(run.signups for run in runs)
    content = (
        f"Restore **{event.title}** (event **{event.event_id}**), deleted "
        f"<t:{int(snapshot.taken_at.timestamp())}:R>?"
    )
    if len(runs) == 1:
        content += (
            f" Its run on <t:{int(runs[0].start_time.timestamp())}:F> is "
            f"posted again in <#{event.channel_id}>"
        )
    elif runs:
        content += (
            f" Its {len(runs)} runs are posted again in <#{event.channel_id}>"
        )
    else:
        content += (
            " The runs it lost have finished since, so its next run is "
            f"posted in <#{event.channel_id}>"
        )
    if signups:
        content += (
            f", with the {signups} "
            f"{_plural(signups, 'sign-up', 'sign-ups')} "
            f"{_plural(len(runs), 'it', 'they')} had."
        )
    else:
        content += "."
    dropped = len(snapshot.removed_runs) - len(runs)
    if dropped and runs:
        content += (
            f" {dropped} {_plural(dropped, 'run', 'runs')} that finished "
            "while it was deleted "
            f"{_plural(dropped, 'is', 'are')} not brought back."
        )
    return content


async def open_event_restore(
    bot: Gw2Bot,
    interaction: discord.Interaction,
    event_id: int,
) -> None:
    """Answer `/event restore` with what it would do, to be confirmed.

    The caller has checked the role that restoring events needs.
    """
    now = datetime.now(UTC)
    snapshot = bot.event_store.get_snapshot(event_id, now)
    if snapshot is None:
        LOGGER.debug(
            "Event restore found nothing to undo; user_id=%s event_id=%s",
            interaction.user.id,
            event_id,
        )
        await interaction.response.send_message(
            f"There is nothing to restore for event **{event_id}**. Only an "
            "event's last deletion or edit can be undone, within "
            f"{SNAPSHOT_RETENTION.days} days of it.",
            ephemeral=True,
        )
        return
    if snapshot.kind is SnapshotKind.DELETE:
        if not snapshot.can_restore(now):
            LOGGER.debug(
                "Event restore refused for a deleted event whose run is "
                "over; user_id=%s event_id=%s",
                interaction.user.id,
                event_id,
            )
            await interaction.response.send_message(
                f"**{snapshot.event.title}** (event **{event_id}**) has "
                "nothing left to restore: its run finished after it was "
                "deleted.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            _deletion_prompt(snapshot, now),
            view=EventRestoreConfirmView(bot, snapshot),
            ephemeral=True,
        )
        LOGGER.debug(
            "Event restore confirmation opened for a deleted event; "
            "user_id=%s event_id=%s runs=%s",
            interaction.user.id,
            event_id,
            len(snapshot.restorable_runs(now)),
        )
        return
    plan = _plan_edit_undo(bot, snapshot, now)
    if isinstance(plan, str):
        LOGGER.debug(
            "Event restore refused an edit it cannot undo; user_id=%s "
            "event_id=%s",
            interaction.user.id,
            event_id,
        )
        await interaction.response.send_message(plan, ephemeral=True)
        return
    content, embed = _edit_undo_prompt(bot, snapshot, plan)
    await interaction.response.send_message(
        content,
        embed=embed,
        view=EventRestoreConfirmView(bot, snapshot),
        ephemeral=True,
    )
    LOGGER.debug(
        "Event restore confirmation opened for an edit; user_id=%s "
        "event_id=%s fields_changed=%s",
        interaction.user.id,
        event_id,
        len(plan.changed),
    )


class EventRestoreConfirmView(discord.ui.View):
    def __init__(self, bot: Gw2Bot, snapshot: EventSnapshot):
        super().__init__(timeout=FLOW_TIMEOUT_SECONDS)
        self._bot = bot
        # Not `_snapshot`, which discord.ui.View already uses for itself.
        self._change = snapshot
        self._restoring = False

    @property
    def _event_id(self) -> int:
        return self._change.event.event_id

    @discord.ui.button(label="Restore", style=discord.ButtonStyle.success)
    async def restore(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[EventRestoreConfirmView],
    ) -> None:
        # The confirmation can sit open for minutes; recheck the role before
        # changing anything, as the edit and delete confirmations do.
        if not user_has_role(
            interaction.user,
            self._bot._config.event_create_role_id,
        ):
            LOGGER.warning(
                "Rejected event restore from Discord user %s; required role "
                "%s",
                interaction.user.id,
                self._bot._config.event_create_role_id,
            )
            await interaction.response.send_message(
                "You do not have the required role to restore events.",
                ephemeral=True,
            )
            return
        # Guard a double click racing two callbacks before the first removes
        # the buttons; the check and set are synchronous, so the second
        # observes it.
        if self._restoring:
            await interaction.response.send_message(
                "This event is already being restored.",
                ephemeral=True,
            )
            return
        self._restoring = True
        # Another change can replace the snapshot this was opened for while
        # it sits open, and the week can run out. Restoring whatever is there
        # now would not be what the commander confirmed.
        try:
            current = self._bot.event_store.get_snapshot(self._event_id)
        except SQLAlchemyError as exc:
            self._restoring = False
            LOGGER.error(
                "Could not re-read an event snapshot before restoring; "
                "event_id=%s error_type=%s",
                self._event_id,
                type(exc).__name__,
            )
            await interaction.response.send_message(
                "The event could not be restored. Try again later.",
                ephemeral=True,
            )
            return
        if (
            current is None
            or current.kind is not self._change.kind
            or current.taken_at != self._change.taken_at
        ):
            LOGGER.debug(
                "Event restore refused for a snapshot replaced or expired "
                "while it sat open; user_id=%s event_id=%s",
                interaction.user.id,
                self._event_id,
            )
            await interaction.response.edit_message(
                content=STALE_RESTORE,
                embeds=[],
                view=None,
            )
            return
        if current.kind is SnapshotKind.EDIT:
            await self._undo_edit(interaction, current)
        else:
            await self._restore_deletion(interaction, current)

    async def _undo_edit(
        self,
        interaction: discord.Interaction,
        snapshot: EventSnapshot,
    ) -> None:
        plan = _plan_edit_undo(self._bot, snapshot, datetime.now(UTC))
        if isinstance(plan, str):
            LOGGER.debug(
                "Event edit undo refused on confirmation; user_id=%s "
                "event_id=%s",
                interaction.user.id,
                self._event_id,
            )
            await interaction.response.edit_message(
                content=plan,
                embeds=[],
                view=None,
            )
            return
        draft = draft_from_event(
            snapshot.event,
            self._bot.event_timezone,
            start_time_override=plan.start_time,
        )
        LOGGER.debug(
            "Undoing an event edit; user_id=%s event_id=%s fields_changed=%s",
            interaction.user.id,
            self._event_id,
            len(plan.changed),
        )
        # Saved through the edit path, which re-seats the roster against a
        # size put back, moves the post back to a channel put back, and
        # refreshes every live run, exactly as the edit being undone did.
        await apply_event_edit(
            self._bot,
            interaction,
            draft,
            plan.current.channel_id,
            repost=True,
            undoing=True,
        )

    async def _restore_deletion(
        self,
        interaction: discord.Interaction,
        snapshot: EventSnapshot,
    ) -> None:
        try:
            await interaction.response.edit_message(
                content="Restoring the event…",
                embeds=[],
                view=None,
            )
        except discord.HTTPException as exc:
            # Nothing is restored yet, so the guard must not outlive a failed
            # acknowledgement and lock the commander out of retrying.
            self._restoring = False
            LOGGER.error(
                "Could not acknowledge an event restore; user_id=%s "
                "event_id=%s error_type=%s",
                interaction.user.id,
                self._event_id,
                type(exc).__name__,
            )
            return
        now = datetime.now(UTC)
        try:
            restored = self._bot.event_store.restore_deleted_event(
                self._event_id,
                now,
            )
        except (SQLAlchemyError, ValueError) as exc:
            self._restoring = False
            LOGGER.error(
                "Could not restore a deleted event; event_id=%s "
                "error_type=%s",
                self._event_id,
                type(exc).__name__,
            )
            await _send_flow_result(
                interaction,
                "The event could not be restored. Try again later.",
                workflow="event restore failure",
                event_id=self._event_id,
            )
            return
        if restored is None:
            # The run was live when this was checked moments ago, and has
            # finished since.
            await _send_flow_result(
                interaction,
                f"**{snapshot.event.title}** (event **{self._event_id}**) "
                "has nothing left to restore: its run finished after it was "
                "deleted.",
                workflow="event restore refusal",
                event_id=self._event_id,
            )
            return
        occurrences = list(restored.occurrences)
        if not occurrences:
            seeded = _seed_next_run(self._bot, restored.event, snapshot, now)
            if seeded is not None:
                occurrences.append(seeded)
        failed = await _post_restored_runs(
            self._bot,
            restored.event,
            occurrences,
            now,
        )
        # The event is back whatever became of its posts, so it is recorded,
        # before the reply, as a deletion is.
        await send_event_audit(
            self._bot,
            event_restored_audit(
                interaction.user.id,
                restored.event,
                (
                    occurrences[0].start_time
                    if occurrences
                    else restored.event.start_time
                ),
            ),
        )
        LOGGER.debug(
            "Restored a deleted event; user_id=%s event_id=%s runs=%s "
            "runs_failed=%s runs_dropped=%s",
            interaction.user.id,
            self._event_id,
            len(occurrences),
            failed,
            restored.dropped,
        )
        await _send_flow_result(
            interaction,
            _restored_message(restored.event, len(occurrences), failed),
            workflow="event restore",
            event_id=self._event_id,
        )

    @discord.ui.button(label="Leave it", style=discord.ButtonStyle.secondary)
    async def keep(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[EventRestoreConfirmView],
    ) -> None:
        LOGGER.debug(
            "Event restore declined; user_id=%s event_id=%s",
            interaction.user.id,
            self._event_id,
        )
        await _send_flow_decline(
            interaction,
            "Nothing was restored.",
            workflow="event restore",
            event_id=self._event_id,
        )


def _seed_next_run(
    bot: Gw2Bot,
    event: Event,
    snapshot: EventSnapshot,
    now: datetime,
) -> EventOccurrence | None:
    """Give a restored series the run it is due, when it lost none still live.

    Seeded the way the maintenance pass seeds a series' next run, after the
    latest run the event has had, and flagged so the pass posts it if this
    flow does not get to. Nothing else would: the pass only posts a run of a
    series with no post of its own when it is flagged.
    """
    from gw2bot.events.posting import ensure_next_recurring_occurrence

    if event.repeat_frequency is RepeatFrequency.NONE:
        return None
    try:
        latest = max(
            [run.start_time for run in snapshot.removed_runs]
            + [
                occurrence.start_time
                for occurrence in bot.event_store.get_event_occurrences(
                    event.event_id
                )
            ],
            default=event.start_time,
        )
        seeded = ensure_next_recurring_occurrence(
            bot,
            event,
            EventOccurrence(
                occurrence_id=0,
                event_id=event.event_id,
                start_time=latest,
                message_id=None,
                thread_id=None,
                status=EventStatus.OVER,
            ),
            now,
        )
        if seeded is not None:
            bot.event_store.set_occurrence_needs_refresh(
                seeded.occurrence_id,
                True,
            )
    except (SQLAlchemyError, ValueError) as exc:
        LOGGER.error(
            "Could not seed the next run of a restored event; event_id=%s "
            "error_type=%s",
            event.event_id,
            type(exc).__name__,
        )
        return None
    return seeded


async def _post_restored_runs(
    bot: Gw2Bot,
    event: Event,
    occurrences: list[EventOccurrence],
    now: datetime,
) -> int:
    """Post a restored event's runs, the way the maintenance pass would.

    Each run is flagged for posting already, so one Discord refuses is left
    for the pass to retry rather than lost. Returns how many failed.
    """
    from gw2bot.events.posting import (
        ensure_next_recurring_occurrence,
        post_pending_occurrence,
        prune_superseded_occurrences,
    )

    failed = 0
    for occurrence in occurrences:
        try:
            posted = await post_pending_occurrence(
                bot,
                event,
                occurrence,
                now,
            )
            # Posting a run that ended moments ago persists it as OVER, so
            # the pass never takes it through the transition that seeds the
            # series' next run.
            if posted is not None and posted.status is EventStatus.OVER:
                ensure_next_recurring_occurrence(bot, event, posted, now)
        except (
            discord.HTTPException,
            SQLAlchemyError,
            ValueError,
            RuntimeError,
        ) as exc:
            failed += 1
            LOGGER.error(
                "Could not post a restored event run; occurrence_id=%s "
                "error_type=%s",
                occurrence.occurrence_id,
                type(exc).__name__,
            )
    # A series that removes its previous post removes the ones the restored
    # run supersedes, as it would on any post. Best-effort and never raises.
    await prune_superseded_occurrences(bot, event)
    return failed


def _restored_message(event: Event, runs: int, failed: int) -> str:
    content = f"Event **{event.event_id}** was restored."
    if not runs:
        return (
            f"{content} Its next run could not be scheduled, so nothing was "
            "posted. Remove it with `/event delete` and create it again."
        )
    if failed:
        return (
            f"{content} {failed} of its {runs} "
            f"{_plural(runs, 'run', 'runs')} could not be posted yet, and "
            f"the bot will keep trying. Check that it can post in "
            f"<#{event.channel_id}>."
        )
    return (
        f"{content} {_plural(runs, 'Its run is', f'Its {runs} runs are')} "
        f"posted in <#{event.channel_id}>."
    )
