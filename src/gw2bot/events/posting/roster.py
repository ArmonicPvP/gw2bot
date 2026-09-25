"""Who is seated on an occurrence, and every way that changes.

Seating and removing a sign-up, rebalancing roles around it, re-checking that
seated members are still in the guild, applying a commander's edits, and a
member's claim on the mentee slot.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, TYPE_CHECKING
from weakref import WeakKeyDictionary

import discord
from sqlalchemy.exc import SQLAlchemyError

from gw2bot.core.discord_utils import (
    GuildMembership,
    resolve_guild_memberships,
)
from gw2bot.events.formatting import (
    roster_update_messages,
    signup_edit_limit_message,
)
from gw2bot.events.models import (
    AutoSignupChoice,
    Event,
    EventOccurrence,
    EventRole,
    EventSignup,
    EventStatus,
    MenteeStatus,
    RoleChange,
    RosterAssignment,
    RosterCandidate,
    RosterUpdate,
    available_edit_tokens,
    can_admit,
    is_roster_full,
    normalize_stored_roles,
    preferred_role_order,
    rebalance_signups,
    roster_feasible,
    seated_candidates,
    solve_roster,
)
from gw2bot.events.posting.channels import (
    _event_guild,
    reopen_occurrence_thread,
    resolve_channel,
    update_thread_membership,
)
from gw2bot.events.posting.state import occurrence_finished, occurrence_status

if TYPE_CHECKING:
    from gw2bot.bot import Gw2Bot

LOGGER = logging.getLogger(__name__)


class RosterUnreadable(Exception):
    """The store would not say what a roster is, so nothing was changed.

    remove_signup answers this rather than the None that means "not on the
    roster": its callers turn that None into "you were not signed up", which
    would be a lie told to a member whose signup is still sitting there. The
    store failing for one call is not evidence about anybody's seat, and this
    says so. Whatever the roster check before it moved is announced first, so
    raising costs nobody their notification.
    """


async def complete_signup(
    bot: Gw2Bot,
    event: Event,
    occurrence: EventOccurrence,
    discord_user_id: int,
    role: EventRole | None,
    flex_roles: tuple[EventRole, ...],
    now: datetime | None = None,
) -> EventSignup:
    """Seat one member who signed themselves up, announcing what it moved."""
    signup, _ = await seat_signup(
        bot,
        event,
        occurrence,
        discord_user_id,
        role,
        flex_roles,
        now,
    )
    return signup


async def seat_signup(
    bot: Gw2Bot,
    event: Event,
    occurrence: EventOccurrence,
    discord_user_id: int,
    role: EventRole | None,
    flex_roles: tuple[EventRole, ...],
    now: datetime | None = None,
    *,
    notify: bool = True,
) -> tuple[EventSignup, RosterUpdate]:
    """Put one member on the roster and report what seating them moved.

    A leader adding several members at once wants one announcement rather than
    one per member, so it passes notify=False and merges the updates itself;
    everything else takes the announcement here. Mirrors remove_signup.
    """
    # Imported here rather than at module scope: refreshing the post can
    # seed the next occurrence, whose auto-sign-ups come back through this
    # module, so the two import each other.
    from gw2bot.events.posting.messages import (
        refresh_occurrence_message,
    )

    signups = bot.event_store.get_signups(occurrence.occurrence_id)
    # The role/flex/remember views can linger until their timeout, so the
    # occurrence may have ended between opening the flow and this click.
    # Refuse to mutate a historical roster (which would also update thread
    # membership and refresh the past message).
    if occurrence_status(event, occurrence, signups, now) is EventStatus.OVER:
        raise ValueError(
            "This event has already ended, so you can no longer sign up."
        )
    # Seat this member against a roster the server still recognises. A member
    # who has left holds a seat nobody can fill, and admitting around one
    # would send this member to the waitlist over a place that is not really
    # taken, so the departed go first and the seating below is solved without
    # them.
    event, occurrence, signups, checked = await _checked_roster(
        bot,
        event,
        occurrence,
        now,
        "This event has already ended, so you can no longer sign up.",
    )
    # The selection was made against the event as it stood before those
    # lookups, and a commander can save a category change while they run.
    # Normalised against the event the check read back, the same way a
    # picker's stale role is: a role the new category does not support would
    # otherwise be solved against a capacity that has no seat for it, and
    # waitlist a member the roster has room for. A headcount event has no
    # role to hold at all, so the selection is dropped rather than stored
    # against a row that cannot use it.
    if role is not None:
        if event.capacity.has_roles:
            settled_role, settled_flex = normalize_stored_roles(
                event.capacity,
                role,
                flex_roles,
            )
        else:
            settled_role, settled_flex = None, ()
        if settled_role is not role or settled_flex != flex_roles:
            LOGGER.debug(
                "Normalized a signup's roles after an event changed "
                "category; occurrence_id=%s user_id=%s category=%s "
                "normalized_role=%s normalized_flex_count=%s",
                occurrence.occurrence_id,
                discord_user_id,
                event.category.value,
                settled_role.value if settled_role is not None else None,
                len(settled_flex),
            )
        role, flex_roles = settled_role, settled_flex
    assigned_role: EventRole | None = None
    waitlisted: bool
    update = RosterUpdate()
    if event.capacity.has_roles:
        if role is None:
            # The check moved the roster before this call refused to seat
            # anybody, and a call that raises hands its caller no update to
            # fold, whatever notify says. Announce it here or nowhere.
            await notify_roster_update(bot, occurrence, checked)
            raise ValueError("This event requires picking a role.")
        # The newcomer is appended after the seated members rather than
        # re-sorted: their signup time is "now", so they carry the lowest
        # seating priority. Seated members keep their full acceptable sets,
        # so admission may flex them to another of their roles but can never
        # unseat them; when even that cannot fit the newcomer, they are
        # waitlisted.
        candidates = seated_candidates(signups)
        candidates.append(
            RosterCandidate(
                discord_user_id=discord_user_id,
                preferences=preferred_role_order(role, flex_roles),
            )
        )
        solution = solve_roster(event.capacity, candidates)
        waitlisted = solution is None
        if solution is not None:
            assigned_role = solution[discord_user_id]
            assignments, changes = _seated_reassignments(signups, solution)
            # Persist the reshuffle before the new row, and keep both writes
            # synchronous and adjacent: no concurrent interaction can observe
            # the half-applied roster, and a crash between the two commits
            # leaves a roster that still respects every cap (the movers
            # vacated the contested seats before the newcomer exists) and is
            # re-canonicalised by the next mutation.
            #
            # A store that refuses that write leaves the roster as the check
            # left it: its departures are committed, and the member is still
            # waiting on a deferred interaction that only answers a
            # ValueError. Say what the check moved - nothing else will, and
            # this call hands its caller no update to fold - and refuse the
            # seating in the one way the flows above know how to report.
            try:
                bot.event_store.apply_roster_assignments(
                    occurrence.occurrence_id,
                    assignments,
                )
            except SQLAlchemyError as exc:
                LOGGER.error(
                    "Could not re-seat the roster for a signup; "
                    "occurrence_id=%s user_id=%s error_type=%s",
                    occurrence.occurrence_id,
                    discord_user_id,
                    type(exc).__name__,
                )
                await notify_roster_update(bot, occurrence, checked)
                raise ValueError(
                    "The roster could not be updated just now. Try again "
                    "in a moment."
                ) from exc
            update = RosterUpdate(reassigned=tuple(changes))
    else:
        waitlisted = is_roster_full(event.capacity, signups)
    LOGGER.debug(
        "Resolved signup seating; occurrence_id=%s user_id=%s "
        "waitlisted=%s assigned_role=%s reassigned=%s",
        occurrence.occurrence_id,
        discord_user_id,
        waitlisted,
        assigned_role.value if assigned_role is not None else None,
        len(update.reassigned),
    )
    # Guarded like the re-seat above it. The reshuffle that made room for
    # this member is committed by now, so what goes out is that as well as
    # the check's own departures: those members really did move, even though
    # the newcomer they moved for never arrived.
    try:
        signup = bot.event_store.add_signup(
            occurrence_id=occurrence.occurrence_id,
            discord_user_id=discord_user_id,
            role=role,
            assigned_role=assigned_role,
            flex_roles=flex_roles,
            waitlisted=waitlisted,
        )
    except SQLAlchemyError as exc:
        LOGGER.error(
            "Could not seat a signup; occurrence_id=%s user_id=%s "
            "error_type=%s",
            occurrence.occurrence_id,
            discord_user_id,
            type(exc).__name__,
        )
        await notify_roster_update(
            bot,
            occurrence,
            merge_roster_updates([checked, update]),
        )
        raise ValueError(
            "The roster could not be updated just now. Try again in a "
            "moment."
        ) from exc
    # The check moved the roster before this seating did, and a member it
    # promoted can be one the seating then flexes. Folded, they read as one
    # move apiece.
    update = merge_roster_updates([checked, update])
    await update_thread_membership(
        bot,
        occurrence,
        discord_user_id,
        add=True,
    )
    if notify:
        await notify_roster_update(bot, occurrence, update)
    await refresh_occurrence_message(bot, event, occurrence)
    return signup, update


async def remove_signup(
    bot: Gw2Bot,
    event: Event,
    occurrence: EventOccurrence,
    discord_user_id: int,
    *,
    notify: bool = True,
) -> tuple[EventSignup | None, RosterUpdate]:
    # Before the seat is freed, not after: the resettle below hands it to the
    # waitlist, and a waitlisted member who has left the server would be
    # promoted into a run they cannot see. Checking first takes them out of
    # the queue, so the seat goes to somebody who can actually use it. The
    # check itself removes through this function, and re-enters it while its
    # own removals are in flight; that re-entry is refused there rather than
    # here, so this stays the one door onto the roster.
    #
    # Its movements are announced with this removal rather than ahead of it:
    # the member being removed can be one the check has just promoted into a
    # seat a departure freed, and telling the thread they moved up moments
    # before taking them off it says two contradictory things.
    #
    # The row is read first, because that check can take this very member
    # off itself: a commander's batch outlasting the freshness window makes
    # one here, and a pick who left the server in the meantime is removed by
    # the check rather than by the deletion below. A caller reads a missing
    # row as "they were not signed up", which would deny the removal this
    # call's own check has just made, so the row it took off is what gets
    # reported. A refused read leaves that unanswerable and is left to the
    # guards below, which say so properly.
    # As in seat_signup above.
    from gw2bot.events.posting.messages import (
        refresh_occurrence_message,
    )

    try:
        before = bot.event_store.get_signup(
            occurrence.occurrence_id,
            discord_user_id,
        )
    except SQLAlchemyError as exc:
        LOGGER.error(
            "Could not read a signup before removing it; occurrence_id=%s "
            "user_id=%s error_type=%s",
            occurrence.occurrence_id,
            discord_user_id,
            type(exc).__name__,
        )
        before = None
    departed, checked = await check_roster_membership(
        bot,
        event,
        occurrence,
        notify=False,
    )
    # That awaited Discord, and the run can cross its end - or be retired by
    # the check's own refresh - while the lookups are in flight. Read the row
    # back and leave a roster that is now history alone: the resettle below
    # hands the freed seat to the waitlist, so going on would promote somebody
    # into a run that has already finished. prune_departed_signups stops on
    # the same test rather than removing from a finished roster.
    # The event comes back with it. A commander can save the event while the
    # lookups are in flight, and this removal judges the run's end by its
    # duration, re-seats the roster the freed seat belongs to against its
    # capacity, and re-renders the message from both.
    #
    # Both are store calls, and a store that cannot say what the run is now
    # cannot be asked to change it either. Answered as RosterUnreadable
    # rather than as the None that means "not on the roster": a sign-out and
    # a commander's batch both read that None as absence and would tell
    # somebody they were never signed up while their signup is still there.
    try:
        current = bot.event_store.get_occurrence(occurrence.occurrence_id)
        edited = bot.event_store.get_event(event.event_id)
    except SQLAlchemyError as exc:
        LOGGER.error(
            "Could not read the run back before removing a signup; "
            "occurrence_id=%s user_id=%s error_type=%s",
            occurrence.occurrence_id,
            discord_user_id,
            type(exc).__name__,
        )
        # Announced whatever notify says, the same as a refused seating: a
        # call that raises hands its caller no update to fold, and a batch
        # whose members outlast the freshness window runs a check of its own
        # here - so a departure it committed is announced here or nowhere.
        await notify_roster_update(bot, occurrence, checked)
        raise RosterUnreadable from exc
    if (
        current is None
        or edited is None
        or occurrence_finished(edited, current)
    ):
        LOGGER.debug(
            "Skipped a removal from a roster that is history; "
            "occurrence_id=%s user_id=%s exists=%s event_exists=%s",
            occurrence.occurrence_id,
            discord_user_id,
            current is not None,
            edited is not None,
        )
        if notify:
            await notify_roster_update(bot, occurrence, checked)
        return None, checked
    occurrence = current
    event = edited
    # Read in the same synchronous stretch as the removal and the resettle,
    # so what it is compared against afterwards is only their doing: either
    # can hand the mentee slot to the next member in line.
    mentee_before = _mentee_snapshot(bot, event, occurrence.occurrence_id)
    # The write itself is guarded like the reads above it, and answered the
    # same way: nothing of this call is committed, so RosterUnreadable is
    # exactly what happened, and the callers that already handle it - a
    # sign-out, a commander's removal batch, the check's own prune - answer
    # the person waiting instead of leaving them on "Removing...".
    try:
        removed = bot.event_store.remove_signup(
            occurrence.occurrence_id,
            discord_user_id,
        )
    except SQLAlchemyError as exc:
        LOGGER.error(
            "Could not remove a signup; occurrence_id=%s user_id=%s "
            "error_type=%s",
            occurrence.occurrence_id,
            discord_user_id,
            type(exc).__name__,
        )
        # Announced whatever notify says, for the reason above: this raises,
        # so there is no update for the caller to fold into its own.
        await notify_roster_update(bot, occurrence, checked)
        raise RosterUnreadable from exc
    if removed is None:
        if before is None and discord_user_id in departed:
            # The check took them off, and the read that would have said
            # what it took off is the one that refused. Absence is the one
            # answer this cannot be: it would deny that removal. Answered as
            # the store declining to say, which every caller of this already
            # handles, with what the check moved announced first.
            LOGGER.error(
                "Could not say what a check's own removal took off; "
                "occurrence_id=%s user_id=%s",
                occurrence.occurrence_id,
                discord_user_id,
            )
            await notify_roster_update(bot, occurrence, checked)
            raise RosterUnreadable
        if before is not None and discord_user_id in departed:
            # The check above took them off, which is the removal this call
            # was asked for - it just happened a step earlier, because they
            # had left the server. Reported as the removal it is: the row is
            # the one the check deleted, and everything a removal does for it
            # (the re-seat, the thread, the refresh) the check has done.
            LOGGER.debug(
                "Removal found its member already taken off by the check; "
                "occurrence_id=%s user_id=%s",
                occurrence.occurrence_id,
                discord_user_id,
            )
            if notify:
                await notify_roster_update(bot, occurrence, checked)
            return before, checked
        # This member was not on the roster, but the check may still have
        # moved it, and that movement is real whatever this call does next.
        if notify:
            await notify_roster_update(bot, occurrence, checked)
        return None, checked
    # Resettle the roster into the freed capacity before yielding to any
    # awaited Discord I/O. The removal and the resettle are synchronous store
    # writes, so keeping them adjacent makes the mutation atomic: a concurrent
    # complete_signup cannot observe the freed slot and claim it ahead of the
    # existing waitlist while we await the thread update below. A waitlisted
    # departure frees nothing, so the roster is left untouched.
    update = RosterUpdate()
    if not removed.waitlisted:
        # Unlike the deletion above, this refusal has the member already off
        # the roster. Reporting it as a failed removal would leave them in
        # the thread and in whatever the caller announces, and send them to a
        # retry that finds no signup to remove - so the removal is finished
        # on its own terms instead, with nothing promoted. The freed seat is
        # re-solved by the next roster change, and a caller with a recovery
        # of its own - the prune - still has one for the failures after this
        # point, which is where its own re-seat comes in.
        try:
            update = _resettle_roster(bot, event, occurrence)
        except SQLAlchemyError as exc:
            LOGGER.error(
                "Could not resettle a roster after removing a signup; "
                "the removal stands; occurrence_id=%s user_id=%s "
                "error_type=%s",
                occurrence.occurrence_id,
                discord_user_id,
                type(exc).__name__,
            )
    mentee = _mentee_movement(
        bot,
        event,
        occurrence.occurrence_id,
        mentee_before,
    )
    # The check moved the roster first and this removal moved it after, so
    # they fold into one line per member with this member dropped: whatever
    # seat the check gave them, they are off the roster now.
    update = merge_roster_updates(
        [checked, update, mentee],
        [discord_user_id],
    )
    await update_thread_membership(
        bot,
        occurrence,
        discord_user_id,
        add=False,
    )
    if notify:
        await notify_roster_update(bot, occurrence, update)
    await refresh_occurrence_message(bot, event, occurrence)
    return removed, update


MENTEE_ENDED_MESSAGE = (
    "This event has already ended, so its roster can no longer be changed."
)


async def set_mentee_request(
    bot: Gw2Bot,
    event: Event,
    occurrence: EventOccurrence,
    discord_user_id: int,
    *,
    requested: bool,
) -> EventSignup:
    """Put a member's claim on the mentee slot, or take it back.

    Only ever called for the member themselves answering: nothing seeds a
    claim, so an automatic sign-up can never make somebody a mentee. Whether
    the claim holds the slot or joins its waitlist is the store's to settle,
    and the row it returns says which.

    The question and the sign-out choice can both sit open until they time
    out, so the run is read back before anything is written: a run that has
    ended keeps the roster it had, and an event that has stopped offering the
    slot takes no new claims - though a member can always give one up.
    Raises ValueError with the text to show the member when nothing changed.
    """
    # As in seat_signup above.
    from gw2bot.events.posting.messages import (
        refresh_occurrence_message,
    )

    try:
        current = bot.event_store.get_occurrence(occurrence.occurrence_id)
        edited = bot.event_store.get_event(event.event_id)
    except SQLAlchemyError as exc:
        LOGGER.error(
            "Could not read the run back for a mentee claim; "
            "occurrence_id=%s user_id=%s error_type=%s",
            occurrence.occurrence_id,
            discord_user_id,
            type(exc).__name__,
        )
        raise ValueError(
            "The roster could not be read just now. Try again in a moment."
        ) from exc
    if current is None or edited is None or edited.cancelled:
        raise ValueError("This event is no longer available.")
    if occurrence_finished(edited, current):
        LOGGER.debug(
            "Refused a mentee claim on a finished run; occurrence_id=%s "
            "user_id=%s requested=%s",
            current.occurrence_id,
            discord_user_id,
            requested,
        )
        raise ValueError(MENTEE_ENDED_MESSAGE)
    if requested and not edited.mentee_enabled:
        LOGGER.debug(
            "Refused a mentee claim on an event without the slot; "
            "event_id=%s user_id=%s",
            edited.event_id,
            discord_user_id,
        )
        raise ValueError(
            "This event is no longer looking for a mentee, so nothing was "
            "changed."
        )
    # Giving up the slot hands it to the next member in line, who is told in
    # the thread the way a promotion off the event's waitlist is.
    mentee_before = _mentee_snapshot(bot, edited, current.occurrence_id)
    try:
        signup = bot.event_store.set_signup_mentee(
            current.occurrence_id,
            discord_user_id,
            requested,
        )
    except SQLAlchemyError as exc:
        LOGGER.error(
            "Could not store a mentee claim; occurrence_id=%s user_id=%s "
            "error_type=%s",
            current.occurrence_id,
            discord_user_id,
            type(exc).__name__,
        )
        raise ValueError(
            "The roster could not be updated just now. Try again in a "
            "moment."
        ) from exc
    LOGGER.debug(
        "Applied a mentee claim; occurrence_id=%s user_id=%s requested=%s "
        "status=%s waitlisted=%s",
        current.occurrence_id,
        discord_user_id,
        requested,
        signup.mentee.value,
        signup.waitlisted,
    )
    await notify_roster_update(
        bot,
        current,
        _mentee_movement(
            bot,
            edited,
            current.occurrence_id,
            mentee_before,
        ),
    )
    # The claim is committed, so a store refusing the refresh costs the post
    # its update rather than the member their answer.
    try:
        await refresh_occurrence_message(bot, edited, current)
    except SQLAlchemyError as exc:
        LOGGER.error(
            "Could not refresh the event after a mentee claim; "
            "occurrence_id=%s error_type=%s",
            current.occurrence_id,
            type(exc).__name__,
        )
    return signup


def departed_roster_members(
    signups: Sequence[EventSignup],
    memberships: Mapping[int, GuildMembership],
) -> list[int]:
    """Pick out the roster members Discord has confirmed have left.

    Only a definite "not a member" counts. A lookup that failed - a permission
    error, an outage, a member the bot was never told about - reports None, and
    treating that as a departure would drop half a roster the first time
    Discord is unreachable.
    """
    return [
        signup.discord_user_id
        for signup in signups
        if memberships.get(signup.discord_user_id, GuildMembership()).in_guild
        is False
    ]


def _mentee_promotions(
    before: Sequence[EventSignup],
    after: Sequence[EventSignup],
) -> tuple[EventSignup, ...]:
    """Who moved up from the mentee waitlist into the slot between readings.

    A member who took an open slot by answering the question themselves was
    never waiting, so they are not reported: they have their answer already.
    """
    was = {signup.discord_user_id: signup.mentee for signup in before}
    return tuple(
        signup
        for signup in after
        if signup.mentee is MenteeStatus.MENTEE
        and was.get(signup.discord_user_id) is MenteeStatus.WAITLISTED
    )


def _mentee_snapshot(
    bot: Gw2Bot,
    event: Event,
    occurrence_id: int,
) -> list[EventSignup] | None:
    """The roster to compare the mentee slot against, if it is worth reading.

    None when the event does not show the slot - claims left from before it
    was turned off still move, but nobody can see them, so announcing one
    would name a slot the post does not have - or when the store will not
    say. The change being read around goes ahead either way: this only costs
    the announcement.
    """
    if not event.mentee_enabled:
        return None
    try:
        return bot.event_store.get_signups(occurrence_id)
    except SQLAlchemyError as exc:
        LOGGER.error(
            "Could not read the roster for the mentee slot; "
            "occurrence_id=%s error_type=%s",
            occurrence_id,
            type(exc).__name__,
        )
        return None


def _mentee_movement(
    bot: Gw2Bot,
    event: Event,
    occurrence_id: int,
    before: Sequence[EventSignup] | None,
) -> RosterUpdate:
    """The mentee promotion a change made, read against its earlier roster."""
    if before is None:
        return RosterUpdate()
    after = _mentee_snapshot(bot, event, occurrence_id)
    if after is None:
        return RosterUpdate()
    promoted = _mentee_promotions(before, after)
    if promoted:
        LOGGER.debug(
            "The mentee slot moved to the next in line; occurrence_id=%s "
            "promoted=%s",
            occurrence_id,
            len(promoted),
        )
    return RosterUpdate(mentee_promoted=promoted)


def _roster_movement(
    before: Sequence[EventSignup],
    after: Sequence[EventSignup],
    removed_user_id: int,
    *,
    mentee_enabled: bool = False,
) -> RosterUpdate:
    """What changed between two readings of one roster, minus who left.

    Used to recover the movement a removal made when the store failed after
    committing it: the resettle may have landed and the refresh behind it
    raised, so re-running the resettle finds nothing left to do and reports
    nothing, while the promotion is sitting in the rows. Comparing the two
    readings says what really happened whichever half failed.
    """
    was = {signup.discord_user_id: signup for signup in before}
    promoted: list[EventSignup] = []
    reassigned: list[RoleChange] = []
    for signup in after:
        previous = was.get(signup.discord_user_id)
        if previous is None or signup.discord_user_id == removed_user_id:
            continue
        if previous.waitlisted and not signup.waitlisted:
            promoted.append(signup)
        elif (
            not previous.waitlisted
            and previous.assigned_role is not None
            and signup.assigned_role is not None
            and previous.assigned_role is not signup.assigned_role
        ):
            reassigned.append(
                RoleChange(
                    discord_user_id=signup.discord_user_id,
                    old_role=previous.assigned_role,
                    new_role=signup.assigned_role,
                )
            )
    return RosterUpdate(
        reassigned=tuple(reassigned),
        promoted=tuple(promoted),
        mentee_promoted=(
            _mentee_promotions(before, after) if mentee_enabled else ()
        ),
    )


async def prune_departed_signups(
    bot: Gw2Bot,
    event: Event,
    occurrence: EventOccurrence,
    memberships: Mapping[int, GuildMembership],
    now: datetime | None = None,
) -> tuple[list[int], RosterUpdate]:
    """Drop roster members who have left the Discord server.

    A member who leaves the server keeps their seat: the bot runs without the
    members intent, so it never hears about the departure, and the seat would
    hold a place nobody can fill for an event they cannot even see. Every
    roster edit re-checks the members it lists and clears those out, promoting
    the waitlist into the freed seats exactly as a leader's own removal would.

    Automatic sign-up is switched off for each departure as well, so a
    recurring series does not seat them again on its next occurrence.

    Returns the ids actually removed and the merged roster movement to report,
    which the caller announces or folds into its own announcement.
    """
    signups = bot.event_store.get_signups(occurrence.occurrence_id)
    if not signups:
        return [], RosterUpdate()
    if occurrence_status(event, occurrence, signups, now) is EventStatus.OVER:
        # A finished occurrence's roster is history. Removing from it would
        # promote someone into a run that is already over, and re-rendering the
        # message can persist OVER without seeding the series' next occurrence.
        LOGGER.debug(
            "Skipped pruning departed members from a finished occurrence; "
            "occurrence_id=%s",
            occurrence.occurrence_id,
        )
        return [], RosterUpdate()
    departed = departed_roster_members(signups, memberships)
    if not departed:
        LOGGER.debug(
            "No departed members on the roster; occurrence_id=%s roster=%s",
            occurrence.occurrence_id,
            len(signups),
        )
        return [], RosterUpdate()
    removed: list[int] = []
    updates: list[RosterUpdate] = []
    for index, user_id in enumerate(departed):
        # remove_signup awaits Discord I/O between members, so the event can
        # cross its end partway through a long roster even though the check
        # above passed. Re-read both the clock and the occurrence every
        # iteration - the row can be rescheduled or retired outright while the
        # loop runs - and stop the moment the run is over, so no removal (and
        # no waitlist promotion behind it) ever lands on a finished roster.
        # An explicit now pins the clock for callers that asked for one;
        # otherwise it really is the elapsing time that counts.
        current_time = now if now is not None else datetime.now(UTC)
        # Every store call that opens an iteration fails the same way and
        # stops the loop the same way: what the members ahead of this one
        # committed is reported, rather than thrown away by an exception
        # escaping to the check above.
        try:
            current = bot.event_store.get_occurrence(occurrence.occurrence_id)
        except SQLAlchemyError as exc:
            LOGGER.error(
                "Could not read the occurrence mid-prune; stopping; "
                "occurrence_id=%s kept=%s error_type=%s",
                occurrence.occurrence_id,
                len(departed) - index,
                type(exc).__name__,
            )
            break
        if current is None or occurrence_finished(
            event, current, current_time
        ):
            LOGGER.debug(
                "Event over mid-prune; stopping; occurrence_id=%s kept=%s "
                "exists=%s",
                occurrence.occurrence_id,
                len(departed) - index,
                current is not None,
            )
            break
        # One member failing to leave the roster must not strand the rest, and
        # remove_signup already absorbs its own Discord failures. A store
        # failure is different: it says the database is refusing writes right
        # now, so the removals still to come would fail too. Stop there and
        # report what did land - the rows are committed one removal at a
        # time, and a caller told nothing happened would announce a roster it
        # no longer has and seat members against seats that are already free.
        # Read before the removal so the recovery below can say what it did:
        # the store can fail with the seat already handed on, and what landed
        # is only visible by comparing the two readings. Guarded like the read
        # above, and for the same reason.
        try:
            before = bot.event_store.get_signups(current.occurrence_id)
        except SQLAlchemyError as exc:
            LOGGER.error(
                "Could not read the roster before removing a departed "
                "member; stopping the prune; occurrence_id=%s kept=%s "
                "error_type=%s",
                occurrence.occurrence_id,
                len(departed) - index,
                type(exc).__name__,
            )
            break
        try:
            signup, update = await remove_signup(
                bot,
                event,
                current,
                user_id,
                notify=False,
            )
        except (SQLAlchemyError, RosterUnreadable) as exc:
            LOGGER.error(
                "Could not remove a departed member; stopping the prune; "
                "occurrence_id=%s kept=%s error_type=%s",
                occurrence.occurrence_id,
                len(departed) - index,
                type(exc).__name__,
            )
            # The deletion commits before the resettle and the message
            # refresh behind it, so this can fail with the member already off
            # the roster. Read the row back and report the departure if it
            # landed: a caller told it did not happen keeps them in whatever
            # it announces, naming a seat that is no longer theirs.
            try:
                still_on = (
                    bot.event_store.get_signup(
                        current.occurrence_id,
                        user_id,
                    )
                    is not None
                )
            except SQLAlchemyError:
                # The store cannot say either way, and claiming a departure
                # that did not happen is the worse mistake of the two.
                still_on = True
            if not still_on:
                removed.append(user_id)
                # The deletion landed; what follows it did not. Try that
                # again rather than leave the seat it freed unclaimed with a
                # waitlist behind it, and the member's automatic sign-up on
                # to seed them onto the next run. The store may still refuse,
                # which is logged and left: the resettle re-solves from what
                # is there, so the next roster change picks it up, and a
                # member seeded again is one the next post's check removes.
                #
                # One boundary each: they are independent repairs, and the
                # automatic sign-up is the one that matters next week. Sharing
                # a try meant a refused resettle took the disable with it, and
                # seeded the member who had left onto the next run - the very
                # thing this recovery was added to stop.
                try:
                    _resettle_roster(bot, event, current)
                except SQLAlchemyError as cleanup_error:
                    LOGGER.error(
                        "Could not re-seat the roster a departure freed; "
                        "occurrence_id=%s user_id=%s error_type=%s",
                        occurrence.occurrence_id,
                        user_id,
                        type(cleanup_error).__name__,
                    )
                try:
                    disable_auto_signup(bot, event, current, user_id)
                except SQLAlchemyError as cleanup_error:
                    LOGGER.error(
                        "Could not switch off a departed member's automatic "
                        "sign-up; occurrence_id=%s user_id=%s error_type=%s",
                        occurrence.occurrence_id,
                        user_id,
                        type(cleanup_error).__name__,
                    )
                # Whatever the roster did, whichever half failed: the resettle
                # above reports only what it moved itself, and it moves
                # nothing when the first one had already landed.
                try:
                    after = bot.event_store.get_signups(
                        current.occurrence_id
                    )
                    updates.append(
                        _roster_movement(
                            before,
                            after,
                            user_id,
                            mentee_enabled=event.mentee_enabled,
                        )
                    )
                except SQLAlchemyError:
                    LOGGER.error(
                        "Could not read back a recovered removal's roster; "
                        "occurrence_id=%s user_id=%s",
                        occurrence.occurrence_id,
                        user_id,
                    )
            break
        if signup is None:
            continue
        removed.append(user_id)
        updates.append(update)
        try:
            # The fresh row again: disable_auto_signup withdraws the member
            # from occurrences later than this one, which is decided by
            # comparing start times.
            disable_auto_signup(bot, event, current, user_id)
        except SQLAlchemyError as exc:
            LOGGER.error(
                "Could not disable auto signup for a departed member; "
                "occurrence_id=%s user_id=%s error_type=%s",
                occurrence.occurrence_id,
                user_id,
                type(exc).__name__,
            )
    merged = merge_roster_updates(updates, removed)
    LOGGER.debug(
        "Pruned departed members from the roster; event_id=%s "
        "occurrence_id=%s roster=%s departed=%s removed=%s promoted=%s",
        event.event_id,
        occurrence.occurrence_id,
        len(signups),
        len(departed),
        len(removed),
        len(merged.promoted),
    )
    return removed, merged


# How long one membership check of an occurrence's roster stands. The bot runs
# without the members intent, so its member cache is empty and every member on
# the roster costs a Discord lookup; a burst of sign-ups on a fifty-seat roster
# would otherwise spend fifty of them per click. A commander who opened the
# roster asks for a fresh answer with force=True, and a roster the check has
# never seen - a newly seeded occurrence about to be posted - is never covered
# by an earlier one, so what this bounds is only how often the same live roster
# is re-asked about.
ROSTER_MEMBERSHIP_CHECK_SECONDS = 60.0


@dataclass(slots=True)
class _RosterMembershipChecks:
    """One bot's bookkeeping for the roster membership checks.

    ``checked_at`` is when each occurrence's roster was last asked about, and
    ``in_flight`` the occurrences a check is running over right now. Held per
    bot rather than per module because it describes that bot's conversation
    with Discord: a second bot (a test's, say) starts with its own empty state
    and cannot be answered from the first one's.
    """

    checked_at: dict[int, float] = field(default_factory=dict)
    in_flight: set[int] = field(default_factory=set)


_MEMBERSHIP_CHECKS: WeakKeyDictionary[Any, _RosterMembershipChecks] = (
    WeakKeyDictionary()
)


def _membership_checks(bot: Gw2Bot) -> _RosterMembershipChecks:
    checks = _MEMBERSHIP_CHECKS.get(bot)
    if checks is None:
        checks = _RosterMembershipChecks()
        _MEMBERSHIP_CHECKS[bot] = checks
    return checks


def _membership_check_due(
    checks: _RosterMembershipChecks,
    occurrence_id: int,
    moment: float,
) -> bool:
    # Expired entries are dropped as they are passed over, so the table holds
    # the rosters checked within the window rather than every occurrence this
    # process has ever posted.
    for checked_id, checked_at in list(checks.checked_at.items()):
        if moment - checked_at >= ROSTER_MEMBERSHIP_CHECK_SECONDS:
            del checks.checked_at[checked_id]
    return occurrence_id not in checks.checked_at


def _answer_stands(
    bot: Gw2Bot,
    occurrence_id: int,
    memberships: Mapping[int, GuildMembership],
) -> bool:
    """Whether this check's answer is worth standing for the whole window.

    Asked of the rows rather than of the prune, because the prune stops on a
    store failure the same way it stops on a finished run - by reporting what
    it committed - and both leave departures behind that the next roster
    change must ask about again.

    Every seated member the sweep asked about has to have been answered for.
    A definite departure still sitting there means the prune stopped before
    reaching them; an unknown means a lookup failed and nothing was
    established about the seat they are holding. Members who signed up while
    the lookups ran were not part of the question, and a check of their own
    will ask it.
    """
    try:
        remaining = bot.event_store.get_signups(occurrence_id)
    except SQLAlchemyError as exc:
        LOGGER.error(
            "Could not read the roster back after checking it; "
            "occurrence_id=%s error_type=%s",
            occurrence_id,
            type(exc).__name__,
        )
        return False
    asked = [
        memberships[signup.discord_user_id]
        for signup in remaining
        if signup.discord_user_id in memberships
    ]
    if not any(membership.in_guild is not None for membership in asked):
        # Nothing definite came back about anybody still seated, which is how
        # a bot that may not look members up answers every lookup it makes.
        # That is the unreachable server the window is a backoff for, not a
        # sweep worth making again on the very next click.
        LOGGER.debug(
            "Kept a roster check that established nothing; occurrence_id=%s "
            "seated=%s",
            occurrence_id,
            len(asked),
        )
        return True
    return all(membership.in_guild is True for membership in asked)


async def check_roster_membership(
    bot: Gw2Bot,
    event: Event,
    occurrence: EventOccurrence,
    *,
    guild: discord.Guild | None = None,
    memberships: Mapping[int, GuildMembership] | None = None,
    now: datetime | None = None,
    force: bool = False,
    notify: bool = True,
) -> tuple[list[int], RosterUpdate]:
    """Re-check a roster against Discord and take off everyone who has left.

    The bot is never told that a member left the server: it runs without the
    members intent, so nothing arrives to act on and a departed member keeps
    their seat. That seat holds a place nobody can fill for an event they
    cannot even see, blocks the waitlist behind it, and on a repeating event
    is handed straight back to them when their automatic sign-up seeds the
    next occurrence. So the roster is asked about again whenever it changes
    and before an occurrence is posted, rather than only when a commander
    opens it.

    Callers that have already looked the roster up - the pickers, which need
    the same lookups for their names - pass ``memberships`` and spend no
    further calls here.

    Returns the ids actually removed and the roster movement their removal
    caused, which has already been announced - unless the caller passed
    notify=False because it has an announcement of its own to fold this into.
    Nothing here is allowed to fail its caller: a sign-up, a removal or a post
    must land whether or not the check behind it could be made.
    """
    occurrence_id = occurrence.occurrence_id
    checks = _membership_checks(bot)
    if occurrence_id in checks.in_flight:
        # prune_departed_signups removes through remove_signup, which asks for
        # this check itself. Without this the removal would re-enter the check
        # that is making it, over a roster read before any of it landed, and
        # go round removing the same members again.
        LOGGER.debug(
            "Skipped a roster membership check already in flight; "
            "occurrence_id=%s",
            occurrence_id,
        )
        return [], RosterUpdate()
    moment = time.monotonic()
    if not force and not _membership_check_due(checks, occurrence_id, moment):
        LOGGER.debug(
            "Skipped a roster membership check made moments ago; "
            "occurrence_id=%s",
            occurrence_id,
        )
        return [], RosterUpdate()
    server = guild if guild is not None else _event_guild(bot)
    if memberships is None and server is None:
        # Only the server can say who is still in it. Without one there is no
        # answer to act on, and guessing would cost members their seats, so
        # the roster stands as it is until a check can be made.
        LOGGER.debug(
            "Skipped a roster membership check without a server to ask; "
            "occurrence_id=%s",
            occurrence_id,
        )
        return [], RosterUpdate()
    try:
        signups = bot.event_store.get_signups(occurrence_id)
    except SQLAlchemyError as exc:
        LOGGER.error(
            "Could not read the roster to check it against the server; "
            "occurrence_id=%s error_type=%s",
            occurrence_id,
            type(exc).__name__,
        )
        return [], RosterUpdate()
    if not signups:
        checks.checked_at[occurrence_id] = moment
        LOGGER.debug(
            "Checked an empty roster against the server; occurrence_id=%s",
            occurrence_id,
        )
        return [], RosterUpdate()
    checks.in_flight.add(occurrence_id)
    try:
        resolved = (
            memberships
            if memberships is not None
            else await resolve_guild_memberships(
                bot,
                server,
                [signup.discord_user_id for signup in signups],
            )
        )
        # Those lookups awaited Discord, and a commander can save the event
        # while they are in flight. The prune judges the run's end by the
        # event's duration and re-seats the roster it leaves behind against
        # the event's capacity, so it needs the event as it stands now:
        # _checked_roster reads it back for its own callers, but only after
        # this prune has already moved the roster.
        edited = bot.event_store.get_event(event.event_id)
        if edited is None or edited.cancelled:
            LOGGER.debug(
                "Skipped a roster prune for an event that is gone; "
                "occurrence_id=%s exists=%s",
                occurrence_id,
                edited is not None,
            )
            return [], RosterUpdate()
        # The occurrence comes back for the same reason the event does. A
        # channel move landing while the lookups were in flight has deleted
        # the thread this would otherwise announce in, and the row now names
        # the one the move opened.
        moved = bot.event_store.get_occurrence(occurrence_id)
        if moved is not None:
            occurrence = moved
        departed, update = await prune_departed_signups(
            bot,
            edited,
            occurrence,
            resolved,
            now,
        )
        if departed and notify:
            await notify_roster_update(bot, occurrence, update)
    except discord.DiscordException as exc:
        # A roster the bot could not check is still a roster: report nothing
        # removed and let the sign-up, removal or post behind this carry on.
        LOGGER.error(
            "Could not check the roster against the server; occurrence_id=%s "
            "error_type=%s",
            occurrence_id,
            type(exc).__name__,
        )
        # Recorded for this one alone: a check that failed against an
        # unreachable Discord must not be retried by every click behind it,
        # which would spend the same failing lookups over and over.
        checks.checked_at[occurrence_id] = time.monotonic()
        return [], RosterUpdate()
    except SQLAlchemyError as exc:
        # Not recorded: the store refusing says nothing about who is still in
        # the server, and whatever this sweep did not take off is still
        # sitting on the roster. Standing the answer down for a minute would
        # let every click behind it seat members around those seats.
        LOGGER.error(
            "Could not check the roster against the server; occurrence_id=%s "
            "error_type=%s",
            occurrence_id,
            type(exc).__name__,
        )
        return [], RosterUpdate()
    finally:
        checks.in_flight.discard(occurrence_id)
    # An answer stands for a minute, so it is only worth standing when the
    # roster really is clean. The prune stops on a store failure and reports
    # what it committed rather than raising, so a departure it never made
    # would otherwise sit unasked-about for the window's length with every
    # click behind it skipping the check.
    if _answer_stands(bot, occurrence_id, resolved):
        checks.checked_at[occurrence_id] = time.monotonic()
    LOGGER.debug(
        "Checked the roster against the server; event_id=%s occurrence_id=%s "
        "roster=%s departed=%s promoted=%s",
        event.event_id,
        occurrence_id,
        len(signups),
        len(departed),
        len(update.promoted),
    )
    return departed, update


async def _checked_roster(
    bot: Gw2Bot,
    event: Event,
    occurrence: EventOccurrence,
    now: datetime | None,
    ended_message: str,
    *,
    force: bool = False,
) -> tuple[Event, EventOccurrence, list[EventSignup], RosterUpdate]:
    """Check the roster against the server, then read back what stands.

    The check awaits Discord, so nothing read before it holds afterwards.
    Members it removed are off the roster - and a prune that failed partway
    through still committed the removals it had made, which it cannot report -
    so the roster is read again whatever the check said rather than only when
    it named someone. The occurrence can also cross its end (or be deleted
    outright) while the lookups are in flight, which is what admitted the
    caller in the first place, so that decision is taken again too: seating
    somebody into a run that finished meanwhile would mutate a historical
    roster and refresh a post nobody is coming back to. The check can retire
    the occurrence itself - the removal it makes refreshes a message that may
    be gone - so the stored status is what is read back, not a status derived
    from the schedule.

    The event comes back too. A commander can save an edit while the lookups
    are in flight, and the roster is seated against whatever category the
    event carries now - admitting somebody under the capacity it had before
    would seat them into a squad shape that no longer exists, and a shortened
    duration can have ended the run outright.

    Whatever the check moved comes back rather than being announced here: the
    caller is about to move the same roster, and on a role-limited one it can
    move the very member the check just promoted. One announcement, folded,
    says one thing about each of them.
    """
    _, checked = await check_roster_membership(
        bot,
        event,
        occurrence,
        now=now,
        force=force,
        notify=False,
    )
    try:
        edited = bot.event_store.get_event(event.event_id)
        current = bot.event_store.get_occurrence(occurrence.occurrence_id)
        signups = (
            bot.event_store.get_signups(current.occurrence_id)
            if current is not None
            else []
        )
    except SQLAlchemyError as exc:
        # The callers here are a member's sign-up or signup edit, whose views
        # answer a ValueError and nothing else: letting this escape leaves
        # them on "Signing you up..." for good. The check's own removals are
        # committed, and this is the last chance to say what they moved.
        LOGGER.error(
            "Could not read the roster back after checking it; "
            "occurrence_id=%s error_type=%s",
            occurrence.occurrence_id,
            type(exc).__name__,
        )
        await notify_roster_update(bot, occurrence, checked)
        raise ValueError(
            "The roster could not be read just now. Try again in a moment."
        ) from exc
    if edited is None or edited.cancelled or current is None:
        await notify_roster_update(bot, occurrence, checked)
        raise ValueError(ended_message)
    if occurrence_finished(edited, current, now):
        await notify_roster_update(bot, occurrence, checked)
        raise ValueError(ended_message)
    return edited, current, signups, checked


def rebalance_occurrence_roster(
    bot: Gw2Bot,
    event: Event,
    occurrence: EventOccurrence,
) -> tuple[int, RosterUpdate]:
    # Call this after an edit changes an event's category: the stored
    # assignments were seated against the old category's capacity and no longer
    # describe a valid roster. Returns how many signups actually moved and the
    # role changes to announce.
    signups = bot.event_store.get_signups(occurrence.occurrence_id)
    if not signups:
        return 0, RosterUpdate()
    reseated = rebalance_signups(event.capacity, signups)
    assignments: list[RosterAssignment] = []
    reassigned: list[RoleChange] = []
    promoted: list[EventSignup] = []
    for before, after in zip(signups, reseated, strict=True):
        if (
            before.role is after.role
            and before.flex_roles == after.flex_roles
            and before.assigned_role is after.assigned_role
            and before.waitlisted == after.waitlisted
        ):
            continue
        assignments.append(
            RosterAssignment(
                discord_user_id=after.discord_user_id,
                role=after.role,
                assigned_role=after.assigned_role,
                waitlisted=after.waitlisted,
                flex_roles=after.flex_roles,
            )
        )
        if before.waitlisted and not after.waitlisted:
            promoted.append(after)
        elif (
            not before.waitlisted
            and not after.waitlisted
            and before.assigned_role is not None
            and after.assigned_role is not None
        ):
            reassigned.append(
                RoleChange(
                    discord_user_id=after.discord_user_id,
                    old_role=before.assigned_role,
                    new_role=after.assigned_role,
                )
            )
    bot.event_store.apply_roster_assignments(
        occurrence.occurrence_id,
        assignments,
    )
    # A new category re-seats everyone, which can unseat the mentee or seat
    # somebody waiting for the slot.
    mentee = _mentee_movement(
        bot,
        event,
        occurrence.occurrence_id,
        signups if event.mentee_enabled else None,
    )
    LOGGER.debug(
        "Rebalanced event roster for a new category; occurrence_id=%s "
        "category=%s signups=%s changed=%s",
        occurrence.occurrence_id,
        event.category.value,
        len(signups),
        len(assignments),
    )
    return len(assignments), RosterUpdate(
        reassigned=tuple(reassigned),
        promoted=tuple(promoted),
        mentee_promoted=mentee.mentee_promoted,
    )


def _seated_reassignments(
    signups: Sequence[EventSignup],
    solution: dict[int, EventRole],
) -> tuple[list[RosterAssignment], list[RoleChange]]:
    # Diff the solver's canonical assignment against the stored seated rows.
    # Rows the solution does not cover (the newcomer being admitted) and rows
    # it leaves in place produce no write.
    assignments: list[RosterAssignment] = []
    changes: list[RoleChange] = []
    for signup in signups:
        if signup.waitlisted:
            continue
        target = solution.get(signup.discord_user_id)
        if target is None or signup.assigned_role is target:
            continue
        assignments.append(
            RosterAssignment(
                discord_user_id=signup.discord_user_id,
                role=signup.role,
                assigned_role=target,
                waitlisted=False,
            )
        )
        if signup.assigned_role is not None:
            changes.append(
                RoleChange(
                    discord_user_id=signup.discord_user_id,
                    old_role=signup.assigned_role,
                    new_role=target,
                )
            )
    return assignments, changes


def _resettle_roster(
    bot: Gw2Bot,
    event: Event,
    occurrence: EventOccurrence,
) -> RosterUpdate:
    """Promote fitting waitlisted members and re-canonicalise assignments.

    Runs after a seated member departs. Fully synchronous - callers rely on
    the store read and every write landing without an intervening await. The
    waitlist is swept once in FCFS order and each candidate whose addition is
    feasible (counting seated flexers moving aside) is admitted; one pass is
    complete because admitting a member never makes another candidate newly
    feasible. The final solve then snaps every seated flexer back to the best
    role their seniority allows, so a member flexed away from their primary
    pick recovers it as soon as the roster permits.
    """
    signups = bot.event_store.get_signups(occurrence.occurrence_id)
    if not event.capacity.has_roles:
        assignments: list[RosterAssignment] = []
        promoted: list[EventSignup] = []
        active = sum(1 for signup in signups if not signup.waitlisted)
        # An uncapped category (General) has no seat to run out of, so every
        # waitlisted member is promoted.
        capacity_total = event.capacity.total
        for signup in signups:
            if not signup.waitlisted:
                continue
            if capacity_total is not None and active >= capacity_total:
                break
            assignments.append(
                RosterAssignment(
                    discord_user_id=signup.discord_user_id,
                    role=signup.role,
                    assigned_role=None,
                    waitlisted=False,
                )
            )
            promoted.append(replace(signup, waitlisted=False))
            active += 1
        bot.event_store.apply_roster_assignments(
            occurrence.occurrence_id,
            assignments,
        )
        LOGGER.debug(
            "Resettled role-less event roster; occurrence_id=%s promoted=%s",
            occurrence.occurrence_id,
            len(promoted),
        )
        return RosterUpdate(promoted=tuple(promoted))
    seated = [signup for signup in signups if not signup.waitlisted]
    waitlisted = sorted(
        (signup for signup in signups if signup.waitlisted),
        key=lambda signup: (signup.signed_up_at, signup.discord_user_id),
    )
    admitted_ids: set[int] = set()
    skipped = 0
    for candidate in waitlisted:
        if candidate.role is None:
            # A role-less signup cannot hold a seat in a role-based roster;
            # left waitlisted, exactly as the pre-solver promotion did.
            continue
        if can_admit(
            event.capacity,
            seated,
            candidate.role,
            candidate.flex_roles,
        ):
            seated.append(replace(candidate, waitlisted=False))
            admitted_ids.add(candidate.discord_user_id)
        else:
            skipped += 1
    solution = solve_roster(event.capacity, seated_candidates(seated))
    if solution is None:
        # Unreachable with well-formed data: the seated set was feasible when
        # each member was admitted. Never unseat anyone over corrupt state;
        # leave the stored roster untouched.
        LOGGER.error(
            "Roster resettle found seated members infeasible; leaving the "
            "stored roster untouched; occurrence_id=%s seated=%s",
            occurrence.occurrence_id,
            len(seated),
        )
        return RosterUpdate()
    assignments, changes = _seated_reassignments(
        [
            signup
            for signup in seated
            if signup.discord_user_id not in admitted_ids
        ],
        solution,
    )
    promoted_signups: list[EventSignup] = []
    for signup in seated:
        if signup.discord_user_id not in admitted_ids:
            continue
        promoted_signup = replace(
            signup,
            assigned_role=solution[signup.discord_user_id],
            waitlisted=False,
        )
        assignments.append(
            RosterAssignment(
                discord_user_id=signup.discord_user_id,
                role=signup.role,
                assigned_role=promoted_signup.assigned_role,
                waitlisted=False,
            )
        )
        promoted_signups.append(promoted_signup)
    bot.event_store.apply_roster_assignments(
        occurrence.occurrence_id,
        assignments,
    )
    LOGGER.debug(
        "Resettled event roster; occurrence_id=%s seated=%s "
        "waitlist_skipped=%s promoted=%s reassigned=%s",
        occurrence.occurrence_id,
        len(seated),
        skipped,
        len(promoted_signups),
        len(changes),
    )
    return RosterUpdate(
        reassigned=tuple(changes),
        promoted=tuple(promoted_signups),
    )


async def notify_roster_update(
    bot: Gw2Bot,
    occurrence: EventOccurrence,
    update: RosterUpdate,
) -> None:
    # A failure here must never fail the signup or removal that produced the
    # update: the roster is already persisted and the embed refresh that
    # follows does not depend on this message landing.
    contents = roster_update_messages(update)
    if not contents:
        return
    if occurrence.thread_id is None:
        LOGGER.debug(
            "Skipped roster update notification without a thread; "
            "occurrence_id=%s reassigned=%s promoted=%s mentee_promoted=%s",
            occurrence.occurrence_id,
            len(update.reassigned),
            len(update.promoted),
            len(update.mentee_promoted),
        )
        return
    # An event posted into a dormant forum post has to reopen it before the
    # announcement can land; a signup flow has usually done so already, and this
    # no-ops when the thread is open.
    await reopen_occurrence_thread(bot, occurrence)
    try:
        thread = await resolve_channel(bot, occurrence.thread_id)
    except discord.HTTPException as exc:
        LOGGER.error(
            "Could not resolve thread for roster update notification; "
            "occurrence_id=%s reassigned=%s promoted=%s error_type=%s",
            occurrence.occurrence_id,
            len(update.reassigned),
            len(update.promoted),
            type(exc).__name__,
        )
        return
    # A large update is split over several messages; one that fails is logged
    # and the rest are still attempted, so a single rejection does not cost
    # every other member their notification.
    sent = 0
    for part, content in enumerate(contents, start=1):
        try:
            await thread.send(content)
        except discord.HTTPException as exc:
            LOGGER.error(
                "Could not send roster update notification; occurrence_id=%s "
                "part=%s parts=%s reassigned=%s promoted=%s error_type=%s",
                occurrence.occurrence_id,
                part,
                len(contents),
                len(update.reassigned),
                len(update.promoted),
                type(exc).__name__,
            )
        else:
            sent += 1
    LOGGER.debug(
        "Sent roster update notification; occurrence_id=%s sent=%s parts=%s "
        "reassigned=%s promoted=%s mentee_promoted=%s",
        occurrence.occurrence_id,
        sent,
        len(contents),
        len(update.reassigned),
        len(update.promoted),
        len(update.mentee_promoted),
    )


def merge_roster_updates(
    updates: Sequence[RosterUpdate],
    removed_user_ids: Sequence[int] = (),
) -> RosterUpdate:
    """Fold sequential roster updates into one announcement.

    A leader removing several members produces one update per removal; the
    merged result reads as a single change. Per-user reassignments chain into
    first-old to last-new (dropped when they end where they started), a
    promotion followed by later reassignments folds into one promotion line
    at the final seat, and users removed later in the same batch are dropped
    entirely - they are off the roster, so reporting a move or promotion for
    them would be wrong. A move up into the mentee slot is reported once
    however many updates carried it.
    """
    removed = set(removed_user_ids)
    chains: dict[int, RoleChange] = {}
    promoted: dict[int, EventSignup] = {}
    mentees: dict[int, EventSignup] = {}
    for update in updates:
        for signup in update.mentee_promoted:
            mentees[signup.discord_user_id] = signup
        for signup in update.promoted:
            promoted[signup.discord_user_id] = signup
        for change in update.reassigned:
            promoted_signup = promoted.get(change.discord_user_id)
            if promoted_signup is not None:
                promoted[change.discord_user_id] = replace(
                    promoted_signup,
                    assigned_role=change.new_role,
                )
                continue
            existing = chains.get(change.discord_user_id)
            chains[change.discord_user_id] = RoleChange(
                discord_user_id=change.discord_user_id,
                old_role=(
                    existing.old_role
                    if existing is not None
                    else change.old_role
                ),
                new_role=change.new_role,
            )
    return RosterUpdate(
        reassigned=tuple(
            change
            for change in chains.values()
            if change.discord_user_id not in removed
            and change.old_role is not change.new_role
        ),
        promoted=tuple(
            signup
            for user_id, signup in promoted.items()
            if user_id not in removed
        ),
        mentee_promoted=tuple(
            signup
            for user_id, signup in mentees.items()
            if user_id not in removed
        ),
    )


@dataclass(frozen=True, slots=True)
class SignupEditResult:
    # The edited row after the roster settled, or None when nothing was
    # applied because the member must first confirm losing their seat.
    signup: EventSignup | None
    update: RosterUpdate
    needs_waitlist_confirmation: bool = False
    # The edit applied, but the automatic sign-up that would carry it into
    # next week's roster still holds the old selection. The member is told,
    # because nothing else will correct it before the series is seeded.
    auto_signup_stale: bool = False


def _without_member(update: RosterUpdate, discord_user_id: int) -> RosterUpdate:
    return RosterUpdate(
        reassigned=tuple(
            change
            for change in update.reassigned
            if change.discord_user_id != discord_user_id
        ),
        promoted=tuple(
            signup
            for signup in update.promoted
            if signup.discord_user_id != discord_user_id
        ),
        # Kept whoever it names, the editor included: an edit that seats a
        # member waiting for the mentee slot hands it to them, and the
        # summary the editor sees is about their roles, not the slot.
        mentee_promoted=update.mentee_promoted,
    )


async def apply_signup_edit(
    bot: Gw2Bot,
    event: Event,
    occurrence: EventOccurrence,
    discord_user_id: int,
    role: EventRole,
    flex_roles: tuple[EventRole, ...],
    *,
    allow_waitlist: bool = False,
    now: datetime | None = None,
) -> SignupEditResult:
    """Replace a member's declared roles without costing them their place.

    Unlike sign-out-and-rejoin, the signup row (and its signed_up_at, which
    decides seating priority) survives, so the member keeps their seat when
    the new roles still fit and keeps their queue position when they do not.
    A seated member whose new selection cannot fit alongside the other seated
    members is only moved to the waitlist after opting in via
    ``allow_waitlist`` - callers get ``needs_waitlist_confirmation`` back and
    nothing is mutated until the member confirms.
    """
    # As in seat_signup above.
    from gw2bot.events.posting.messages import (
        refresh_occurrence_message,
    )

    signups = bot.event_store.get_signups(occurrence.occurrence_id)
    # The edit views can linger like the signup views; refuse to mutate a
    # historical roster.
    if occurrence_status(event, occurrence, signups, now) is EventStatus.OVER:
        raise ValueError(
            "This event has already ended, so your signup can no longer be "
            "changed."
        )
    if not event.capacity.has_roles:
        raise ValueError("This event has no roles to edit.")
    # The new selection is judged against the members who are actually still
    # here, so an edit is not sent to the waitlist by a seat its holder left
    # the server on.
    event, occurrence, signups, checked = await _checked_roster(
        bot,
        event,
        occurrence,
        now,
        "This event has already ended, so your signup can no longer be "
        "changed.",
        # A confirmed edit is the member's second look at this roster: the
        # call that offered them the waitlist checked it moments ago, and
        # answering from that would drop them behind a seat whose holder left
        # while the confirmation sat open. Only EditWaitlistConfirmView gets
        # here, so this forces a confirmation rather than every edit.
        force=allow_waitlist,
    )
    # Asked again of the event the check read back. A commander saving a
    # category change while the lookups ran can have made this a headcount
    # event, which has no role to persist: going on would spend one of the
    # member's three edit tokens writing a choice the event cannot honour,
    # and leave it waiting on the roster for a category change back. Refused
    # exactly as it would have been a moment earlier, except that the check
    # has committed its departures since, so they are announced here.
    if not event.capacity.has_roles:
        await notify_roster_update(bot, occurrence, checked)
        raise ValueError("This event has no roles to edit.")
    # Having roles is not the same as having these roles: a dungeon is still
    # role-based and seats no healer at all. Normalised against the capacity
    # the check read back, exactly as a signup is, so the selection is not
    # judged infeasible - offering the waitlist over seats standing open -
    # and then stored as a role this category cannot assign.
    settled_role, settled_flex = normalize_stored_roles(
        event.capacity,
        role,
        flex_roles,
    )
    if settled_role is not role or settled_flex != flex_roles:
        LOGGER.debug(
            "Normalized a signup edit's roles after an event changed "
            "category; occurrence_id=%s user_id=%s category=%s "
            "normalized_role=%s normalized_flex_count=%s",
            occurrence.occurrence_id,
            discord_user_id,
            event.category.value,
            settled_role.value,
            len(settled_flex),
        )
    role, flex_roles = settled_role, settled_flex
    current = next(
        (
            signup
            for signup in signups
            if signup.discord_user_id == discord_user_id
        ),
        None,
    )
    if current is None:
        # Nothing below this will announce what the check moved, and it did
        # move the roster: the departures are committed and so is whatever
        # they promoted. Say so before this call ends empty-handed.
        await notify_roster_update(bot, occurrence, checked)
        raise ValueError("You are not signed up for this event.")
    # Rate limit: a token bucket per signup (three edits, refilling one per
    # three hours) keeps a member from churning the roster and pinging the
    # thread over and over. Checked before anything mutates; consumed only
    # when an edit actually applies.
    current_time = now if now is not None else datetime.now(UTC)
    tokens = available_edit_tokens(current, current_time)
    if tokens < 1.0:
        LOGGER.debug(
            "Rejected signup edit over the rate limit; occurrence_id=%s "
            "user_id=%s tokens=%.2f",
            occurrence.occurrence_id,
            discord_user_id,
            tokens,
        )
        await notify_roster_update(bot, occurrence, checked)
        raise ValueError(signup_edit_limit_message(tokens))
    keeps_seat = current.waitlisted is False
    if keeps_seat:
        others = [
            candidate.preferences
            for candidate in seated_candidates(signups)
            if candidate.discord_user_id != discord_user_id
        ]
        keeps_seat = roster_feasible(
            event.capacity,
            [*others, preferred_role_order(role, flex_roles)],
        )
        if not keeps_seat and not allow_waitlist:
            LOGGER.debug(
                "Signup edit would waitlist the member; awaiting "
                "confirmation; occurrence_id=%s user_id=%s role=%s "
                "flex_count=%s",
                occurrence.occurrence_id,
                discord_user_id,
                role.value,
                len(flex_roles),
            )
            # The check ahead of this edit has already taken its departures
            # off the roster and moved whoever that promoted, and this exit
            # changes nothing further. The member may well cancel the
            # confirmation they are about to see, and a confirmed one re-runs
            # the check over a roster that is settled by then, so there is no
            # later announcement to fold this into: it is announced here.
            await notify_roster_update(bot, occurrence, checked)
            return SignupEditResult(
                signup=None,
                update=checked,
                needs_waitlist_confirmation=True,
            )
    # Everything from here is a store call on a deferred interaction that
    # only answers a ValueError, and the check before it has already taken
    # its departures off the roster. A refusal therefore says what the check
    # moved - nothing later will - rather than escaping and leaving the
    # member on "Updating your signup...".
    #
    # Each of these commits on its own, so they are guarded one at a time
    # and answered by how far the edit actually got.
    try:
        bot.event_store.set_signup_edit_tokens(
            occurrence.occurrence_id,
            discord_user_id,
            tokens - 1.0,
            current_time,
        )
    except SQLAlchemyError as exc:
        LOGGER.error(
            "Could not spend an edit token; occurrence_id=%s user_id=%s "
            "error_type=%s",
            occurrence.occurrence_id,
            discord_user_id,
            type(exc).__name__,
        )
        await notify_roster_update(bot, occurrence, checked)
        raise ValueError(
            "Your signup could not be updated just now. Try again in a "
            "moment."
        ) from exc
    try:
        bot.event_store.update_signup_roles(
            occurrence.occurrence_id,
            discord_user_id,
            role=role,
            flex_roles=flex_roles,
            assigned_role=current.assigned_role if keeps_seat else None,
            waitlisted=not keeps_seat,
        )
    except SQLAlchemyError as exc:
        LOGGER.error(
            "Could not write a signup edit; occurrence_id=%s user_id=%s "
            "error_type=%s",
            occurrence.occurrence_id,
            discord_user_id,
            type(exc).__name__,
        )
        # The token above is committed and bought nothing, so it is handed
        # back before the member is sent to a retry that would spend a
        # second one. Guarded in turn: a store refusing the write may refuse
        # this too, and the member is owed an answer either way.
        try:
            bot.event_store.set_signup_edit_tokens(
                occurrence.occurrence_id,
                discord_user_id,
                tokens,
                current_time,
            )
        except SQLAlchemyError as refund_error:
            LOGGER.error(
                "Could not hand back an edit token the edit did not use; "
                "occurrence_id=%s user_id=%s error_type=%s",
                occurrence.occurrence_id,
                discord_user_id,
                type(refund_error).__name__,
            )
        await notify_roster_update(bot, occurrence, checked)
        raise ValueError(
            "Your signup could not be updated just now. Try again in a "
            "moment."
        ) from exc
    # The declaration is committed from here, so nothing below reports this
    # edit as having failed.
    #
    # The resettle re-solves the seated set (fixing an assigned role the new
    # declaration no longer covers), seats a waitlisted editor whose new
    # roles now fit, and offers capacity the editor vacated to the waitlist.
    # It is one transaction, so a refusal commits nothing of its own: the
    # member keeps the seat they had under their new declaration, which the
    # next roster change re-solves, and that is what the summary describes.
    update = RosterUpdate()
    try:
        update = _resettle_roster(bot, event, occurrence)
    except SQLAlchemyError as exc:
        LOGGER.error(
            "Could not re-seat the roster after a signup edit; the edit "
            "stands; occurrence_id=%s user_id=%s error_type=%s",
            occurrence.occurrence_id,
            discord_user_id,
            type(exc).__name__,
        )
    # The roster the edit was judged against is the one it changed: nothing
    # awaited between that read and the writes above. An edit can cost a
    # mentee their seat, or seat somebody waiting for the slot, and either
    # hands it on.
    update = merge_roster_updates(
        [
            update,
            _mentee_movement(
                bot,
                event,
                occurrence.occurrence_id,
                signups if event.mentee_enabled else None,
            ),
        ]
    )
    # The edit is committed by here, so this read is outside the guard above:
    # answering a refusal with "try again" would spend a second edit token on
    # a change that is already on the roster, and lose the announcement and
    # the message refresh below with it. The row is described from what was
    # just written instead, corrected by what the resettle reported about
    # this member - it re-solves the seated set, so it can have promoted them
    # or moved their assigned role since.
    try:
        updated = bot.event_store.get_signup(
            occurrence.occurrence_id,
            discord_user_id,
        )
    except SQLAlchemyError as exc:
        LOGGER.error(
            "Could not read a signup back after editing it; "
            "occurrence_id=%s user_id=%s error_type=%s",
            occurrence.occurrence_id,
            discord_user_id,
            type(exc).__name__,
        )
        updated = replace(
            current,
            role=role,
            flex_roles=flex_roles,
            assigned_role=current.assigned_role if keeps_seat else None,
            waitlisted=not keeps_seat,
        )
        seated = next(
            (
                signup
                for signup in update.promoted
                if signup.discord_user_id == discord_user_id
            ),
            None,
        )
        moved = next(
            (
                change
                for change in update.reassigned
                if change.discord_user_id == discord_user_id
            ),
            None,
        )
        if seated is not None:
            updated = seated
        elif moved is not None:
            updated = replace(updated, assigned_role=moved.new_role)
    if updated is None:
        # The resettle above committed too, so both halves are announced
        # rather than lost with the row.
        await notify_roster_update(
            bot,
            occurrence,
            merge_roster_updates([checked, update]),
        )
        raise ValueError("You are not signed up for this event.")
    # A stored auto sign-up snapshots the roles it will use for future
    # occurrences, so an enabled one must follow the edit or next week's
    # roster would resurrect the old selection.
    #
    # Guarded on its own rather than with the phase above, because the edit
    # this follows is committed: refusing it now would tell the member their
    # change failed when it is sitting on the roster. A failure here is not
    # silent either - the snapshot decides next week's roster, and nothing
    # retries it - so the member is told their automatic sign-up still holds
    # the old selection and can put it right themselves.
    auto_signup_stale = False
    try:
        auto = bot.event_store.get_auto_signup(
            event.event_id,
            discord_user_id,
        )
        if auto is not None and auto.choice is AutoSignupChoice.YES:
            bot.event_store.set_auto_signup(
                event.event_id,
                discord_user_id,
                AutoSignupChoice.YES,
                role,
                flex_roles,
            )
    except SQLAlchemyError as exc:
        auto_signup_stale = True
        LOGGER.error(
            "Could not carry a signup edit into its automatic sign-up; "
            "occurrence_id=%s user_id=%s error_type=%s",
            occurrence.occurrence_id,
            discord_user_id,
            type(exc).__name__,
        )
    LOGGER.debug(
        "Applied signup edit; occurrence_id=%s user_id=%s role=%s "
        "flex_count=%s waitlisted=%s assigned_role=%s reassigned=%s "
        "promoted=%s",
        occurrence.occurrence_id,
        discord_user_id,
        role.value,
        len(flex_roles),
        updated.waitlisted,
        (
            updated.assigned_role.value
            if updated.assigned_role is not None
            else None
        ),
        len(update.reassigned),
        len(update.promoted),
    )
    # The check moved the roster before this edit did, and a member it
    # promoted can be one the edit then flexes, so the two are folded first.
    update = merge_roster_updates([checked, update])
    # The editor sees their own outcome in the ephemeral summary; the thread
    # only hears about the members their edit moved.
    await notify_roster_update(
        bot,
        occurrence,
        _without_member(update, discord_user_id),
    )
    await refresh_occurrence_message(bot, event, occurrence)
    return SignupEditResult(
        signup=updated,
        update=update,
        auto_signup_stale=auto_signup_stale,
    )


def apply_auto_signups(
    bot: Gw2Bot,
    event: Event,
    occurrence: EventOccurrence,
) -> int:
    applied = 0
    for entry in bot.event_store.get_auto_signup_entries(event.event_id):
        signups = bot.event_store.get_signups(occurrence.occurrence_id)
        if any(
            signup.discord_user_id == entry.discord_user_id
            for signup in signups
        ):
            continue
        assigned_role: EventRole | None = None
        signup_role = entry.role
        signup_flex_roles = entry.flex_roles
        if event.capacity.has_roles:
            if entry.role is None:
                LOGGER.debug(
                    "Skipped auto signup without a stored role; "
                    "event_id=%s user_id=%s",
                    event.event_id,
                    entry.discord_user_id,
                )
                continue
            signup_role, signup_flex_roles = normalize_stored_roles(
                event.capacity,
                entry.role,
                entry.flex_roles,
            )
            if (
                signup_role is not entry.role
                or signup_flex_roles != entry.flex_roles
            ):
                bot.event_store.set_auto_signup(
                    event.event_id,
                    entry.discord_user_id,
                    entry.choice,
                    signup_role,
                    signup_flex_roles,
                )
                LOGGER.debug(
                    "Normalized automatic signup roles for the current "
                    "category; event_id=%s user_id=%s stored_role=%s "
                    "normalized_role=%s normalized_flex_count=%s",
                    event.event_id,
                    entry.discord_user_id,
                    entry.role.value,
                    signup_role.value,
                    len(signup_flex_roles),
                )
            # Same admission as a live signup: earlier entries may be flexed
            # aside to fit this one, but are never unseated. The roster is
            # freshly seeded and unseen, so the reshuffle happens silently.
            candidates = seated_candidates(signups)
            candidates.append(
                RosterCandidate(
                    discord_user_id=entry.discord_user_id,
                    preferences=preferred_role_order(
                        signup_role,
                        signup_flex_roles,
                    ),
                )
            )
            solution = solve_roster(event.capacity, candidates)
            waitlisted = solution is None
            if solution is not None:
                assigned_role = solution[entry.discord_user_id]
                assignments, _ = _seated_reassignments(signups, solution)
                bot.event_store.apply_roster_assignments(
                    occurrence.occurrence_id,
                    assignments,
                )
        else:
            waitlisted = is_roster_full(event.capacity, signups)
        bot.event_store.add_signup(
            occurrence_id=occurrence.occurrence_id,
            discord_user_id=entry.discord_user_id,
            role=signup_role,
            assigned_role=assigned_role,
            flex_roles=signup_flex_roles,
            waitlisted=waitlisted,
        )
        applied += 1
    LOGGER.debug(
        "Applied auto signups; event_id=%s occurrence_id=%s applied=%s",
        event.event_id,
        occurrence.occurrence_id,
        applied,
    )
    return applied


@dataclass(frozen=True, slots=True)
class AutoSignupDisableResult:
    """What turning automatic sign-up off did beyond storing the choice."""

    # Occurrences the member's automatic seat was pulled out of.
    withdrawn: tuple[EventOccurrence, ...] = ()
    # Later occurrences that still seat the member because they are already
    # posted, so the seat may have been taken deliberately. The caller has to
    # say so rather than promise the member is off every future roster.
    still_seated: tuple[EventOccurrence, ...] = ()


def disable_auto_signup(
    bot: Gw2Bot,
    event: Event,
    occurrence: EventOccurrence,
    discord_user_id: int,
) -> AutoSignupDisableResult:
    """Store the "no automatic sign-up" choice and undo the seats it took.

    Storing the choice only stops future seeding, and by the time a caller
    gets here the next occurrence can already hold this member: any roster
    change that crosses the occurrence's end (or finds its message gone) seeds
    the next one from inside refresh_occurrence_message, and the scheduler
    seeds it while a sign-out prompt sits open. Telling the member they will
    not be signed up again while that seat stands would be false, so drop it.

    Only occurrences that have not been posted are withdrawn from. A member
    can only sign themselves up from a posted message and apply_auto_signups
    runs only on a freshly created occurrence, so a signup on an unposted
    occurrence can only be automatic. A seat on a posted occurrence may well
    have been taken on purpose, and removing that - unseating the member and
    promoting someone in their place - would be the worse mistake, so those
    are reported back for the caller to mention instead.
    """
    bot.event_store.set_auto_signup(
        event.event_id,
        discord_user_id,
        AutoSignupChoice.NO,
        None,
        (),
    )
    withdrawn: list[EventOccurrence] = []
    still_seated: list[EventOccurrence] = []
    for later in bot.event_store.get_event_occurrences(event.event_id):
        if later.start_time <= occurrence.start_time:
            continue
        if later.message_id is not None:
            if (
                bot.event_store.get_signup(
                    later.occurrence_id,
                    discord_user_id,
                )
                is not None
            ):
                still_seated.append(later)
            continue
        removed = bot.event_store.remove_signup(
            later.occurrence_id,
            discord_user_id,
        )
        if removed is None:
            continue
        withdrawn.append(later)
        if not removed.waitlisted:
            # The seeded roster was solved with this member on it, so hand the
            # freed seat to the waitlist. The occurrence has not been posted,
            # so there is no message to refresh and nobody to notify.
            _resettle_roster(bot, event, later)
    LOGGER.debug(
        "Disabled auto signup; event_id=%s occurrence_id=%s user_id=%s "
        "withdrawn=%s still_seated=%s",
        event.event_id,
        occurrence.occurrence_id,
        discord_user_id,
        len(withdrawn),
        len(still_seated),
    )
    return AutoSignupDisableResult(
        withdrawn=tuple(withdrawn),
        still_seated=tuple(still_seated),
    )
