from __future__ import annotations

from dataclasses import dataclass

COPPER_PER_GOLD = 10_000
MAX_GOLD_RAFFLE_TICKETS = 10
MAX_MANUAL_RAFFLE_TICKETS = 1
OFFICER_RANK = "Officer"
OFFICER_MAX_TICKET_DEPOSIT_COINS = 10 * COPPER_PER_GOLD


def format_gold(coins: int) -> str:
    whole, remainder = divmod(coins, COPPER_PER_GOLD)
    if remainder == 0:
        return str(whole)
    return f"{whole}.{remainder:04d}".rstrip("0")


@dataclass(frozen=True, slots=True)
class RaffleDeposit:
    event_id: int
    username: str
    coins_deposited: int
    raffle_tickets: int
    event_time: str

    @property
    def message(self) -> str:
        gold = format_gold(self.coins_deposited)
        return (
            f"{self.username} deposited {gold} gold and purchased "
            f"{self.raffle_tickets} raffle tickets"
        )


@dataclass(frozen=True, slots=True)
class RaffleTotal:
    username: str
    coins_deposited: int
    raffle_tickets: int
    gold_raffle_tickets: int
    manual_raffle_tickets: int


@dataclass(frozen=True, slots=True)
class RaffleWinner:
    username: str
    winning_ticket: int
    tickets_before_draw: int
    # Tickets the winner held at the moment of this draw; None for legacy
    # runs recorded before ticket counts were kept.
    tickets_held: int | None = None

    @property
    def win_chance(self) -> float | None:
        if self.tickets_held is None or self.tickets_before_draw <= 0:
            return None
        return self.tickets_held / self.tickets_before_draw


@dataclass(frozen=True, slots=True)
class RaffleResult:
    run_id: int
    winners: tuple[RaffleWinner, ...]
    total_tickets: int
    purchased_tickets: int
    free_tickets: int


@dataclass(frozen=True, slots=True)
class RaffleRunSummary:
    run_id: int
    run_time: str


@dataclass(frozen=True, slots=True)
class RaffleAuditRange:
    username: str
    tickets: int
    first_ticket: int
    last_ticket: int


@dataclass(frozen=True, slots=True)
class RaffleAuditDraw:
    draw_position: int
    username: str
    winning_ticket: int
    tickets_before_draw: int
    # Tickets the winner held at the moment of this draw; None for legacy
    # runs recorded before entrant snapshots were kept.
    tickets_held: int | None
    # Ticket ranges in effect for this draw, alphabetical by username;
    # empty for legacy runs without an entrant snapshot.
    ranges: tuple[RaffleAuditRange, ...]

    @property
    def win_chance(self) -> float | None:
        if self.tickets_held is None or self.tickets_before_draw <= 0:
            return None
        return self.tickets_held / self.tickets_before_draw


@dataclass(frozen=True, slots=True)
class RaffleAudit:
    run_id: int
    run_time: str
    total_tickets: int
    purchased_tickets: int
    free_tickets: int
    # Ticket ranges for the first draw, alphabetical by username; empty for
    # legacy runs recorded before entrant snapshots were kept.
    entrants: tuple[RaffleAuditRange, ...]
    draws: tuple[RaffleAuditDraw, ...]

    @property
    def has_entrant_snapshot(self) -> bool:
        return bool(self.entrants)


@dataclass(frozen=True, slots=True)
class RaffleContribution:
    username: str
    purchased_tickets: int
    event_tickets: int


@dataclass(frozen=True, slots=True)
class RaffleRewardTier:
    threshold: int
    name: str


@dataclass(frozen=True, slots=True)
class RaffleDrawTier:
    minimum_purchased_tickets: int
    winner_count: int


@dataclass(frozen=True, slots=True)
class RaffleMilestone:
    threshold: int
    tier_name: str

    @property
    def message(self) -> str:
        return (
            f"{self.threshold} total tickets have been purchased for this raffle. "
            f"{self.tier_name} rewards have been reached!"
        )


RAFFLE_REWARD_TIERS = (
    RaffleRewardTier(50, "Tier 1"),
    RaffleRewardTier(100, "Tier 2"),
    RaffleRewardTier(150, "Tier 3"),
    RaffleRewardTier(200, "Tier 4"),
)

RAFFLE_DRAW_TIERS = (
    RaffleDrawTier(0, 2),
    RaffleDrawTier(50, 2),
    RaffleDrawTier(100, 3),
    RaffleDrawTier(150, 4),
    RaffleDrawTier(200, 5),
)


@dataclass(frozen=True, slots=True)
class GuildLeave:
    event_id: int
    username: str
    event_time: str
    kicked_by: str | None = None

    @property
    def message(self) -> str:
        if self.kicked_by is not None:
            return f"{self.kicked_by} kicked {self.username} from the guild."
        return f"{self.username} has left the guild."


@dataclass(frozen=True, slots=True)
class GuildJoin:
    event_id: int
    username: str
    event_time: str

    @property
    def message(self) -> str:
        return f"{self.username} has joined the guild."


@dataclass(frozen=True, slots=True)
class TrialForumPost:
    thread_id: int
    owner_id: int | None
    normalized_content: str
    last_activity: str


@dataclass(frozen=True, slots=True)
class GuildInvite:
    event_id: int
    username: str
    event_time: str
    invited_by: str | None = None

    @property
    def message(self) -> str:
        if self.invited_by is not None:
            return f"{self.invited_by} invited {self.username} to the guild."
        return f"{self.username} was invited to the guild."


@dataclass(frozen=True, slots=True)
class GuildRankChange:
    event_id: int
    username: str
    old_rank: str
    new_rank: str
    event_time: str
    changed_by: str | None = None

    @property
    def message(self) -> str:
        if self.changed_by is not None and self.changed_by != self.username:
            return (
                f"{self.changed_by} changed {self.username}'s guild rank "
                f"from {self.old_rank} to {self.new_rank}."
            )
        return (
            f"{self.username}'s guild rank changed "
            f"from {self.old_rank} to {self.new_rank}."
        )
