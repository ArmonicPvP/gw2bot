from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum

EMOJI_QUICKNESS = "<:quickness:1525428594371985459>"
EMOJI_ALACRITY = "<:alacrity:1525428627045351515>"
EMOJI_DPS = "⚔️"

EMOJI_RAID = "<:raid:1525431773498970172>"
EMOJI_STRIKE = "<:strike:1525431254340866171>"
EMOJI_WVW = "<:wvw:1525431137982353428>"
EMOJI_FRACTAL = "<:fractal:1525431043950116864>"


class EventCategory(StrEnum):
    RAID = "Raid"
    STRIKE = "Strike"
    FRACTAL = "Fractal"
    WVW = "World vs. World"


CATEGORY_EMOJI: dict[EventCategory, str] = {
    EventCategory.RAID: EMOJI_RAID,
    EventCategory.STRIKE: EMOJI_STRIKE,
    EventCategory.FRACTAL: EMOJI_FRACTAL,
    EventCategory.WVW: EMOJI_WVW,
}


class EventRole(StrEnum):
    DPS = "Just DPS"
    QUICKNESS_DPS = "Quickness DPS"
    ALACRITY_DPS = "Alacrity DPS"
    QUICKNESS_HEAL = "Quickness Heal"
    ALACRITY_HEAL = "Alacrity Heal"


HEAL_ROLES = frozenset({EventRole.QUICKNESS_HEAL, EventRole.ALACRITY_HEAL})
DPS_ROLES = frozenset(
    {EventRole.DPS, EventRole.QUICKNESS_DPS, EventRole.ALACRITY_DPS}
)
QUICKNESS_ROLES = frozenset(
    {EventRole.QUICKNESS_DPS, EventRole.QUICKNESS_HEAL}
)
ALACRITY_ROLES = frozenset({EventRole.ALACRITY_DPS, EventRole.ALACRITY_HEAL})
# Every heal role carries a boon, so HEAL_ROLES doubles as the boon-heal set;
# the boon-DPS set is the DPS roles that bring one.
BOON_DPS_ROLES = frozenset({EventRole.QUICKNESS_DPS, EventRole.ALACRITY_DPS})

ROLE_EMOJI: dict[EventRole, str] = {
    EventRole.DPS: EMOJI_DPS,
    EventRole.QUICKNESS_DPS: EMOJI_QUICKNESS,
    EventRole.ALACRITY_DPS: EMOJI_ALACRITY,
    EventRole.QUICKNESS_HEAL: EMOJI_QUICKNESS,
    EventRole.ALACRITY_HEAL: EMOJI_ALACRITY,
}


class EventStatus(StrEnum):
    OPEN = "open"
    FULL = "full"
    ONGOING = "ongoing"
    OVER = "over"


STATUS_EMOJI: dict[EventStatus, str] = {
    EventStatus.OPEN: "🟢",
    EventStatus.ONGOING: "🟡",
    EventStatus.FULL: "🔴",
    EventStatus.OVER: "⚫️",
}

STATUS_COLORS: dict[EventStatus, int] = {
    EventStatus.OPEN: 0x2ECC71,
    EventStatus.ONGOING: 0xF1C40F,
    EventStatus.FULL: 0xE74C3C,
    EventStatus.OVER: 0x31373D,
}


class RepeatFrequency(StrEnum):
    NONE = "none"
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"


class PreferenceMode(StrEnum):
    ASK = "ask"
    REMEMBER = "remember"
    NEVER_ASK = "never_ask"


class AutoSignupChoice(StrEnum):
    YES = "yes"
    NO = "no"
    NEVER_ASK = "never_ask"


@dataclass(frozen=True, slots=True)
class CategoryCapacity:
    total: int
    healers: int | None
    dps: int | None
    quickness: int | None
    alacrity: int | None
    # Minimum composition a roster must hold before it counts as FULL: seats
    # can all be taken while the boons are uncovered (say, a healer plus four
    # plain DPS), and such a roster should keep advertising itself as open.
    required_boon_healers: int | None = None
    required_boon_dps: int | None = None

    @property
    def has_roles(self) -> bool:
        return self.healers is not None


CATEGORY_CAPACITIES: dict[EventCategory, CategoryCapacity] = {
    EventCategory.RAID: CategoryCapacity(
        total=10,
        healers=2,
        dps=8,
        quickness=2,
        alacrity=2,
        required_boon_healers=2,
        required_boon_dps=2,
    ),
    EventCategory.STRIKE: CategoryCapacity(
        total=10,
        healers=2,
        dps=8,
        quickness=2,
        alacrity=2,
        required_boon_healers=2,
        required_boon_dps=2,
    ),
    EventCategory.FRACTAL: CategoryCapacity(
        total=5,
        healers=1,
        dps=4,
        quickness=1,
        alacrity=1,
        required_boon_healers=1,
        required_boon_dps=1,
    ),
    EventCategory.WVW: CategoryCapacity(50, None, None, None, None),
}


@dataclass(frozen=True, slots=True)
class Event:
    event_id: int
    category: EventCategory
    title: str
    description: str
    channel_id: int
    leader_discord_id: int
    start_time: datetime
    duration_minutes: int
    repeat_frequency: RepeatFrequency
    repeat_days: tuple[int, ...]
    cancelled: bool = False
    # For a repeating event, delete the previous occurrence's post (and its
    # thread) once the next occurrence is posted, so the channel keeps only the
    # current event. Ignored when the event does not repeat.
    delete_previous_on_repeat: bool = False

    @property
    def capacity(self) -> CategoryCapacity:
        return CATEGORY_CAPACITIES[self.category]


@dataclass(frozen=True, slots=True)
class EventOccurrence:
    occurrence_id: int
    event_id: int
    start_time: datetime
    message_id: int | None
    thread_id: int | None
    status: EventStatus
    # The channel the message was posted to. An event's channel can change after
    # the fact, and occurrences that were not re-posted (finished ones, or a
    # re-post that failed) keep living where they were sent, so the message must
    # be resolved through this rather than the event's current channel. None
    # until the occurrence is posted.
    channel_id: int | None = None
    # Set when the public message failed to refresh so the scheduler retries
    # even if the computed status still matches the stored one.
    needs_refresh: bool = False


# "Edit my signup" rate limit: a token bucket per signup row. A member can
# spend up to three edits back to back; spent tokens return continuously at
# one per three hours. The bucket lives on the signup, so it resets when the
# member signs out and rejoins - which also costs them their queue position,
# a steeper price than waiting for the refill.
SIGNUP_EDIT_TOKEN_CAPACITY = 3.0
SIGNUP_EDIT_REFILL_SECONDS = 3 * 60 * 60


@dataclass(frozen=True, slots=True)
class EventSignup:
    occurrence_id: int
    discord_user_id: int
    role: EventRole | None
    assigned_role: EventRole | None
    flex_roles: tuple[EventRole, ...]
    signed_up_at: datetime
    waitlisted: bool
    edit_tokens: float = SIGNUP_EDIT_TOKEN_CAPACITY
    edit_tokens_updated_at: datetime | None = None


def available_edit_tokens(signup: EventSignup, now: datetime) -> float:
    if signup.edit_tokens_updated_at is None:
        return SIGNUP_EDIT_TOKEN_CAPACITY
    elapsed = max(
        0.0,
        (now - signup.edit_tokens_updated_at).total_seconds(),
    )
    return min(
        SIGNUP_EDIT_TOKEN_CAPACITY,
        signup.edit_tokens + elapsed / SIGNUP_EDIT_REFILL_SECONDS,
    )


@dataclass(frozen=True, slots=True)
class SignupPreference:
    discord_user_id: int
    role: EventRole | None
    flex_roles: tuple[EventRole, ...]
    mode: PreferenceMode


@dataclass(frozen=True, slots=True)
class AutoSignup:
    event_id: int
    discord_user_id: int
    choice: AutoSignupChoice
    role: EventRole | None
    flex_roles: tuple[EventRole, ...]


@dataclass(frozen=True, slots=True)
class RosterCounts:
    active: int
    healers: int
    dps: int
    quickness: int
    alacrity: int
    boon_healers: int
    boon_dps: int


def count_roster(signups: list[EventSignup]) -> RosterCounts:
    active = [signup for signup in signups if not signup.waitlisted]
    assigned = [
        signup.assigned_role
        for signup in active
        if signup.assigned_role is not None
    ]
    return RosterCounts(
        active=len(active),
        healers=sum(1 for role in assigned if role in HEAL_ROLES),
        dps=sum(1 for role in assigned if role in DPS_ROLES),
        quickness=sum(1 for role in assigned if role in QUICKNESS_ROLES),
        alacrity=sum(1 for role in assigned if role in ALACRITY_ROLES),
        boon_healers=sum(1 for role in assigned if role in HEAL_ROLES),
        boon_dps=sum(1 for role in assigned if role in BOON_DPS_ROLES),
    )


def role_fits(
    capacity: CategoryCapacity,
    counts: RosterCounts,
    role: EventRole,
) -> bool:
    if not capacity.has_roles:
        return False
    healers = capacity.healers or 0
    dps = capacity.dps or 0
    quickness = capacity.quickness or 0
    alacrity = capacity.alacrity or 0
    if role in HEAL_ROLES and counts.healers >= healers:
        return False
    if role in DPS_ROLES and counts.dps >= dps:
        return False
    if role in QUICKNESS_ROLES and counts.quickness >= quickness:
        return False
    if role in ALACRITY_ROLES and counts.alacrity >= alacrity:
        return False
    return True


# Flex placement prefers scarcer seats: a flexer parked on plain DPS while a
# boon or heal seat sits open makes the roster look emptier than it is, and
# placement never affects who can be admitted later (feasibility depends only
# on each member's acceptable role set), so specialised seats are filled
# first at no cost. Ties inside a tier fall back to enum declaration order so
# the choice is deterministic.
_FLEX_TIER: dict[EventRole, int] = {
    EventRole.QUICKNESS_HEAL: 0,
    EventRole.ALACRITY_HEAL: 0,
    EventRole.QUICKNESS_DPS: 1,
    EventRole.ALACRITY_DPS: 1,
    EventRole.DPS: 2,
}

_CANONICAL_INDEX: dict[EventRole, int] = {
    role: index for index, role in enumerate(EventRole)
}


def preferred_role_order(
    role: EventRole,
    flex_roles: tuple[EventRole, ...],
) -> tuple[EventRole, ...]:
    # Flex roles are an unordered set: Discord's multi-select does not
    # reliably preserve the order the user clicked, so only the primary role
    # outranks them and the rest sort by scarcity tier.
    flexes = sorted(
        set(flex_roles) - {role},
        key=lambda flex: (_FLEX_TIER[flex], _CANONICAL_INDEX[flex]),
    )
    return (role, *flexes)


@dataclass(frozen=True, slots=True)
class RosterCandidate:
    discord_user_id: int
    preferences: tuple[EventRole, ...]


@dataclass(frozen=True, slots=True)
class RosterAssignment:
    discord_user_id: int
    role: EventRole | None
    assigned_role: EventRole | None
    waitlisted: bool


@dataclass(frozen=True, slots=True)
class RoleChange:
    discord_user_id: int
    old_role: EventRole
    new_role: EventRole


@dataclass(frozen=True, slots=True)
class RosterUpdate:
    reassigned: tuple[RoleChange, ...] = ()
    promoted: tuple[EventSignup, ...] = ()

    @property
    def has_changes(self) -> bool:
        return bool(self.reassigned or self.promoted)


def roster_feasible(
    capacity: CategoryCapacity,
    acceptable: Sequence[tuple[EventRole, ...]],
) -> bool:
    """Whether every member can be seated in one of their acceptable roles.

    The quickness and alacrity caps cross-cut the healer/DPS split (a
    quickness seat can be QDPS or QHEAL), so this is a search rather than
    simple counting. Members are assigned depth-first in order; count states
    proven dead are memoised, which bounds the search by the tiny number of
    distinct (index, healers, quickness, alacrity) states rather than the
    number of role combinations.
    """
    if not capacity.has_roles:
        return len(acceptable) <= capacity.total
    healer_cap = capacity.healers or 0
    dps_cap = capacity.dps or 0
    quickness_cap = capacity.quickness or 0
    alacrity_cap = capacity.alacrity or 0
    dead: set[tuple[int, int, int, int]] = set()

    def search(
        index: int,
        healers: int,
        dps: int,
        quickness: int,
        alacrity: int,
    ) -> bool:
        if index == len(acceptable):
            return True
        # dps is implied by (index, healers): every role occupies exactly one
        # of the healer/DPS groups, so it stays out of the memo key.
        key = (index, healers, quickness, alacrity)
        if key in dead:
            return False
        for role in acceptable[index]:
            next_healers = healers + (role in HEAL_ROLES)
            next_dps = dps + (role in DPS_ROLES)
            next_quickness = quickness + (role in QUICKNESS_ROLES)
            next_alacrity = alacrity + (role in ALACRITY_ROLES)
            if (
                next_healers <= healer_cap
                and next_dps <= dps_cap
                and next_quickness <= quickness_cap
                and next_alacrity <= alacrity_cap
                and search(
                    index + 1,
                    next_healers,
                    next_dps,
                    next_quickness,
                    next_alacrity,
                )
            ):
                return True
        dead.add(key)
        return False

    return search(0, 0, 0, 0, 0)


def solve_roster(
    capacity: CategoryCapacity,
    candidates: Sequence[RosterCandidate],
) -> dict[int, EventRole] | None:
    """Assign every candidate a role, favouring the earliest signups.

    Candidates must already be in sign-up order. Each in turn is fixed to the
    first of their preferences that still lets everyone after them be seated
    somehow, so an earlier signup only gets flexed off a preferred role when
    keeping it would force a later member out entirely. Returns None when no
    complete assignment exists.
    """
    if not capacity.has_roles:
        raise ValueError("solve_roster requires a role-based capacity")
    acceptable = [candidate.preferences for candidate in candidates]
    if not roster_feasible(capacity, acceptable):
        return None
    assignment: dict[int, EventRole] = {}
    fixed: list[tuple[EventRole, ...]] = []
    for index, candidate in enumerate(candidates):
        for preference in candidate.preferences:
            trial = [*fixed, (preference,), *acceptable[index + 1 :]]
            if roster_feasible(capacity, trial):
                assignment[candidate.discord_user_id] = preference
                fixed.append((preference,))
                break
    return assignment


def seated_candidates(
    signups: Sequence[EventSignup],
) -> list[RosterCandidate]:
    # The user id tiebreak keeps FCFS priority deterministic when two members
    # share a signed_up_at timestamp.
    seated = sorted(
        (signup for signup in signups if not signup.waitlisted),
        key=lambda signup: (signup.signed_up_at, signup.discord_user_id),
    )
    candidates: list[RosterCandidate] = []
    for signup in seated:
        if signup.role is None:
            # A seated signup without a role only occurs in corrupt or legacy
            # data (e.g. a category change that was never rebalanced). Treat
            # it as rigid DPS so it still occupies capacity.
            preferences: tuple[EventRole, ...] = (EventRole.DPS,)
        else:
            preferences = preferred_role_order(signup.role, signup.flex_roles)
        candidates.append(
            RosterCandidate(
                discord_user_id=signup.discord_user_id,
                preferences=preferences,
            )
        )
    return candidates


def can_admit(
    capacity: CategoryCapacity,
    signups: Sequence[EventSignup],
    role: EventRole,
    flex_roles: tuple[EventRole, ...],
) -> bool:
    """Whether a new signup fits, allowing seated flexers to be reassigned.

    Seated members are never unseated: they keep their full acceptable sets,
    so a feasible solution seats all of them plus the newcomer. Feasibility
    only depends on membership, so the newcomer's position in the list does
    not matter.
    """
    if not capacity.has_roles:
        return not is_roster_full(capacity, list(signups))
    acceptable = [
        candidate.preferences for candidate in seated_candidates(signups)
    ]
    acceptable.append(preferred_role_order(role, flex_roles))
    return roster_feasible(capacity, acceptable)


def fitting_roles(
    capacity: CategoryCapacity,
    signups: list[EventSignup],
) -> list[EventRole]:
    # A role fits when a rigid signup for it could be admitted, counting the
    # seated flexers' ability to move aside — so a boon seat held by someone
    # who can flex elsewhere does not read as full.
    if not capacity.has_roles:
        return []
    acceptable = [
        candidate.preferences for candidate in seated_candidates(signups)
    ]
    return [
        role
        for role in EventRole
        if roster_feasible(capacity, [*acceptable, (role,)])
    ]


def is_roster_full(
    capacity: CategoryCapacity,
    signups: list[EventSignup],
) -> bool:
    # FULL means "seats taken AND the composition is covered": a roster can
    # occupy every seat without its required boon coverage (a healer plus
    # four plain DPS leaves a boon uncovered), and such an event should keep
    # reading as open rather than done.
    counts = count_roster(signups)
    if not capacity.has_roles:
        return counts.active >= capacity.total
    return (
        counts.healers >= (capacity.healers or 0)
        and counts.dps >= (capacity.dps or 0)
        and counts.boon_healers >= (capacity.required_boon_healers or 0)
        and counts.boon_dps >= (capacity.required_boon_dps or 0)
    )


def rebalance_signups(
    capacity: CategoryCapacity,
    signups: list[EventSignup],
) -> list[EventSignup]:
    """Re-seat a roster against a category's capacity.

    A signup's assigned_role and waitlisted flag only mean anything relative to
    the capacity it was seated against, so changing an event's category
    invalidates every stored assignment. The worst case is a role-less category
    (WvW), whose signups carry no assigned_role at all: a role-based capacity
    reads that roster as zero healers and zero DPS and keeps admitting on top of
    it, so the roster overfills and the embed shows seats nobody holds.

    Signups are re-seated in sign-up order, so seats stay first come, first
    served. Each is offered its own role and flex roles, widened with a plain
    DPS fallback so one that no longer fits any declared role still keeps a
    seat when a DPS slot is left; it is waitlisted only when even that fails.
    A signup carried over from a role-less category has no stored role, so it
    starts from that same DPS fallback, and the role is materialised because
    waitlist promotion skips a role-less signup. Admitted members may be
    reassigned among their acceptable roles by the final solve, exactly as
    during normal signups.

    Moving *to* a role-less category clears the assignments instead: seats there
    are plain headcount.
    """
    if not capacity.has_roles:
        reseated: list[EventSignup] = []
        for signup in signups:
            reseated.append(
                replace(
                    signup,
                    assigned_role=None,
                    waitlisted=(
                        count_roster(reseated).active >= capacity.total
                    ),
                )
            )
        return reseated
    ordered = sorted(
        signups,
        key=lambda signup: (signup.signed_up_at, signup.discord_user_id),
    )
    admitted: list[EventSignup] = []
    admitted_prefs: list[tuple[EventRole, ...]] = []
    result_by_user: dict[int, EventSignup] = {}
    for signup in ordered:
        role = signup.role if signup.role is not None else EventRole.DPS
        preferences = preferred_role_order(role, signup.flex_roles)
        if EventRole.DPS not in preferences:
            preferences = (*preferences, EventRole.DPS)
        if roster_feasible(capacity, [*admitted_prefs, preferences]):
            admitted.append(replace(signup, role=role))
            admitted_prefs.append(preferences)
        else:
            result_by_user[signup.discord_user_id] = replace(
                signup,
                role=role,
                assigned_role=None,
                waitlisted=True,
            )
    solution = solve_roster(
        capacity,
        [
            RosterCandidate(signup.discord_user_id, preferences)
            for signup, preferences in zip(
                admitted, admitted_prefs, strict=True
            )
        ],
    )
    if solution is None:
        # Unreachable: every admission above kept the admitted set feasible.
        raise RuntimeError("rebalance admitted an infeasible roster")
    for signup in admitted:
        result_by_user[signup.discord_user_id] = replace(
            signup,
            assigned_role=solution[signup.discord_user_id],
            waitlisted=False,
        )
    return [result_by_user[signup.discord_user_id] for signup in signups]
