from __future__ import annotations

from dataclasses import dataclass

# The three kinds of membership change the roster history tracks. An invite is
# deliberately absent: the GW2 roster reports an invited account with the
# ``invited`` rank, and the member count excludes those, so an invitation moves
# nothing on this graph until it is accepted and arrives as a join.
JOIN = "join"
LEAVE = "leave"
KICK = "kick"

# How each kind moves the guild member count.
MEMBERSHIP_DELTAS: dict[str, int] = {JOIN: 1, LEAVE: -1, KICK: -1}

# Time windows the roster page offers, mapped to their length in seconds. The
# keys are the values the ``/api/roster?range=`` query accepts; they mirror the
# feast usage dashboard's ranges so both pages read the same way.
ROSTER_RANGES: dict[str, int] = {
    "24h": 24 * 60 * 60,
    "7d": 7 * 24 * 60 * 60,
    "30d": 30 * 24 * 60 * 60,
}


@dataclass(frozen=True, slots=True)
class MembershipEvent:
    """One join, leave or kick, whenever and however it was recorded.

    ``imported`` marks a row the one-time Discord log import wrote rather than
    the guild log poller, because its timestamp is when the bot posted about
    the change and not when the GW2 API says it happened.
    """

    occurred_at: float
    kind: str
    username: str
    actor: str | None = None
    imported: bool = False

    @property
    def delta(self) -> int:
        return MEMBERSHIP_DELTAS.get(self.kind, 0)


@dataclass(frozen=True, slots=True)
class MemberCountSample:
    """One observed guild member count, and when it was observed."""

    recorded_at: float
    member_count: int
    pending_invite_count: int


@dataclass(frozen=True, slots=True)
class RosterPoint:
    """One vertex of the member count line."""

    at: float
    member_count: int


@dataclass(frozen=True, slots=True)
class RosterEvent:
    """One membership change plotted on the graph.

    ``member_count`` is the count immediately after the change, or ``None``
    when no observed count has ever been recorded and there is therefore
    nothing to measure the history against.
    """

    occurred_at: float
    kind: str
    username: str
    actor: str | None
    member_count: int | None
    imported: bool


@dataclass(frozen=True, slots=True)
class RosterSeries:
    """Everything the roster page draws for one time window.

    ``points`` are the member count line's vertices, oldest first, and empty
    when no count has ever been observed. ``events`` are the in-window changes,
    oldest first, one dot each.
    """

    points: tuple[RosterPoint, ...]
    events: tuple[RosterEvent, ...]

    @property
    def joins(self) -> int:
        return sum(1 for event in self.events if event.kind == JOIN)

    @property
    def leaves(self) -> int:
        return sum(1 for event in self.events if event.kind == LEAVE)

    @property
    def kicks(self) -> int:
        return sum(1 for event in self.events if event.kind == KICK)


@dataclass(frozen=True, slots=True)
class ImportedMembershipEvent:
    """One membership change read back out of the Discord log channel.

    ``message_id`` is the Discord message the change was read from. It is what
    makes the import idempotent: the row it becomes is keyed by that id, so
    running the import twice writes the same rows twice over rather than
    counting every departure a second time.
    """

    message_id: int
    occurred_at: float
    kind: str
    username: str
    actor: str | None = None


@dataclass(frozen=True, slots=True)
class MembershipImportResult:
    """What one run of the Discord log import did."""

    scanned: int
    matched: int
    imported: int
    duplicates: int
    skipped_recent: int
    completed: bool = True
