"""What a poll is, and the rules its name and options are held to."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

MIN_POLL_OPTIONS = 2
# Nine, so each option has a keycap from 1 to 9 in the post and the menu.
MAX_POLL_OPTIONS = 9
# Discord's limit on a select option's label, which is where each one is shown.
POLL_OPTION_MAX_LENGTH = 100
POLL_NAME_MAX_LENGTH = 100
POLL_DESCRIPTION_MAX_LENGTH = 1000
# Room for nine full-length options, the commas between them, and some stray
# spaces: whitespace is trimmed after the form is sent, but the form counts it.
POLL_OPTIONS_INPUT_MAX_LENGTH = 1000

OPTION_EMOJIS = ("1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣")


class PollOptionsError(ValueError):
    """The options an officer typed cannot make a poll; the message says why."""


def parse_poll_options(text: str) -> tuple[str, ...]:
    """Split the comma-separated options typed into the create form.

    Blank entries are dropped, so a trailing comma is harmless, and runs of
    whitespace - a line break included - collapse to one space, since an
    option is shown on a single line in the menu.
    """
    options = tuple(
        cleaned
        for piece in text.split(",")
        if (cleaned := " ".join(piece.split()))
    )
    if len(options) < MIN_POLL_OPTIONS:
        raise PollOptionsError(
            f"A poll needs at least {MIN_POLL_OPTIONS} options, separated by "
            "commas."
        )
    if len(options) > MAX_POLL_OPTIONS:
        raise PollOptionsError(
            f"A poll can have at most {MAX_POLL_OPTIONS} options; "
            f"{len(options)} were given."
        )
    too_long = [
        position + 1
        for position, option in enumerate(options)
        if len(option) > POLL_OPTION_MAX_LENGTH
    ]
    if too_long:
        numbers = ", ".join(str(number) for number in too_long)
        raise PollOptionsError(
            f"Each option can be at most {POLL_OPTION_MAX_LENGTH} characters; "
            f"option {numbers} is longer."
        )
    seen: set[str] = set()
    for option in options:
        key = option.casefold()
        if key in seen:
            raise PollOptionsError(
                f"“{option}” is listed more than once. Each option must be "
                "different."
            )
        seen.add(key)
    return options


def option_display(options: tuple[str, ...], position: int) -> str:
    """An option as it appears in the post: its keycap, then its label."""
    return f"{OPTION_EMOJIS[position]} {options[position]}"


@dataclass(frozen=True, slots=True)
class Poll:
    poll_id: int
    name: str
    description: str
    options: tuple[str, ...]
    channel_id: int
    message_id: int | None
    created_by: int
    created_at: datetime
    closed_at: datetime | None = None
    closed_by: int | None = None

    @property
    def is_open(self) -> bool:
        return self.closed_at is None


@dataclass(frozen=True, slots=True)
class PollResults:
    """The final tally of a closed poll, one count per option in order."""

    poll: Poll
    counts: tuple[int, ...]

    @property
    def total(self) -> int:
        return sum(self.counts)

    @property
    def leaders(self) -> tuple[int, ...]:
        """The positions sharing the highest count; none when nobody voted."""
        if self.total == 0:
            return ()
        highest = max(self.counts)
        return tuple(
            position
            for position, count in enumerate(self.counts)
            if count == highest
        )


class VoteOutcome(Enum):
    """What recording a confirmed vote did."""

    RECORDED = "recorded"
    CHANGED = "changed"
    UNCHANGED = "unchanged"
    # The poll ended, or was never there, while the confirmation sat open.
    CLOSED = "closed"
