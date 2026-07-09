from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

EMOJI_QUICKNESS = "<:e:730558718843813930>"
EMOJI_ALACRITY = "<:e:730558718978162769>"
EMOJI_DPS = "<:e:688551054606073927>"


class EventCategory(StrEnum):
    RAID = "Raid"
    STRIKE = "Strike"
    FRACTAL = "Fractal"
    WVW = "World vs. World"


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
