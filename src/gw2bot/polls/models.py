from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

MAX_POLL_OPTIONS = 10

# Keycap-digit reactions, one per option. The request asks for 1-0 with the
# tenth option shown as 0, so the sequence runs 1..9 then 0. Each entry is a
# Unicode keycap sequence (digit + variation selector + combining enclosing
# keycap), which is exactly what ``str(payload.emoji)`` yields for a reaction.
POLL_OPTION_EMOJI: tuple[str, ...] = tuple(
    f"{digit}️⃣" for digit in "1234567890"
)

_EMOJI_TO_INDEX: dict[str, int] = {
    emoji: index for index, emoji in enumerate(POLL_OPTION_EMOJI)
}


@dataclass(frozen=True, slots=True)
class Poll:
    poll_id: int
    guild_id: int
    channel_id: int
    creator_discord_id: int
    title: str
    options: tuple[str, ...]
    allow_multiple: bool
    created_at: datetime
    end_time: datetime
    # None until the poll's message has been posted.
    message_id: int | None = None


@dataclass(frozen=True, slots=True)
class PollVote:
    poll_id: int
    option_index: int
    discord_user_id: int
    voted_at: datetime


def emoji_for_index(index: int) -> str:
    """The reaction emoji that stands for the option at ``index`` (0-based)."""
    return POLL_OPTION_EMOJI[index]


def index_for_emoji(emoji: str) -> int | None:
    """The 0-based option index a reaction emoji maps to, or None if it is not
    one of the poll keycap emojis."""
    return _EMOJI_TO_INDEX.get(emoji)


def is_valid_option_index(index: int | None, option_count: int) -> bool:
    return index is not None and 0 <= index < option_count


def total_votes(counts: Mapping[int, int]) -> int:
    return sum(counts.values())


def option_percentage(counts: Mapping[int, int], option_index: int) -> float:
    """Share of all cast votes that went to ``option_index``.

    The denominator is the sum of every option's votes, so for a single-choice
    poll it equals the number of voters, while for a multiple-choice poll it is
    the total number of selections. Returns 0.0 when nothing has been voted.
    """
    total = total_votes(counts)
    if total <= 0:
        return 0.0
    return counts.get(option_index, 0) / total * 100.0
