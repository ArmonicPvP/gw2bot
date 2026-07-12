from __future__ import annotations

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

    @property
    def has_roles(self) -> bool:
        return self.healers is not None


CATEGORY_CAPACITIES: dict[EventCategory, CategoryCapacity] = {
    EventCategory.RAID: CategoryCapacity(10, 2, 8, 2, 2),
    EventCategory.STRIKE: CategoryCapacity(10, 2, 8, 2, 2),
    EventCategory.FRACTAL: CategoryCapacity(5, 1, 4, 1, 1),
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


@dataclass(frozen=True, slots=True)
class EventSignup:
    occurrence_id: int
    discord_user_id: int
    role: EventRole | None
    assigned_role: EventRole | None
    flex_roles: tuple[EventRole, ...]
    signed_up_at: datetime
    waitlisted: bool


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


def fitting_roles(
    capacity: CategoryCapacity,
    signups: list[EventSignup],
) -> list[EventRole]:
    counts = count_roster(signups)
    return [role for role in EventRole if role_fits(capacity, counts, role)]


def choose_assigned_role(
    capacity: CategoryCapacity,
    signups: list[EventSignup],
    role: EventRole,
    flex_roles: tuple[EventRole, ...],
) -> EventRole | None:
    counts = count_roster(signups)
    for candidate in (role, *flex_roles):
        if role_fits(capacity, counts, candidate):
            return candidate
    return None


def is_roster_full(
    capacity: CategoryCapacity,
    signups: list[EventSignup],
) -> bool:
    counts = count_roster(signups)
    if not capacity.has_roles:
        return counts.active >= capacity.total
    return counts.healers >= (capacity.healers or 0) and counts.dps >= (
        capacity.dps or 0
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
    served. Each is offered its own role and flex roles first; one that no longer
    fits any of them falls back to plain DPS, and is waitlisted when even that
    has no seat left. A signup carried over from a role-less category has no
    stored role, so it starts from that same DPS fallback, and the role is
    materialised because waitlist promotion skips a role-less signup.

    Moving *to* a role-less category clears the assignments instead: seats there
    are plain headcount.
    """
    reseated: list[EventSignup] = []
    for signup in signups:
        if not capacity.has_roles:
            reseated.append(
                replace(
                    signup,
                    assigned_role=None,
                    waitlisted=(
                        count_roster(reseated).active >= capacity.total
                    ),
                )
            )
            continue
        role = signup.role if signup.role is not None else EventRole.DPS
        assigned = choose_assigned_role(
            capacity,
            reseated,
            role,
            signup.flex_roles,
        )
        if assigned is None and EventRole.DPS not in (role, *signup.flex_roles):
            assigned = choose_assigned_role(
                capacity,
                reseated,
                EventRole.DPS,
                (),
            )
        reseated.append(
            replace(
                signup,
                role=role,
                assigned_role=assigned,
                waitlisted=assigned is None,
            )
        )
    return reseated
