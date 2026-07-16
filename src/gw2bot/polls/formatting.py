from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

import discord

from gw2bot.events.formatting import format_duration, parse_event_duration
from gw2bot.polls.models import (
    MAX_POLL_OPTIONS,
    Poll,
    emoji_for_index,
    option_percentage,
    total_votes,
)

POLL_TITLE_MAX_LENGTH = 240
POLL_OPTION_MAX_LENGTH = 100
POLL_DURATION_PLACEHOLDER = "HH:mm"
EMBED_TITLE_LIMIT = 256
EMBED_DESCRIPTION_LIMIT = 4096
_TRUNCATION_MARKER = "…"
_BAR_SEGMENTS = 12

_OPEN_COLOR = 0x5865F2
_ENDED_COLOR = 0x95A5A6


def parse_poll_title(text: str) -> str:
    title = text.strip()
    if not title:
        raise ValueError("The poll needs a title to pose as the question.")
    if len(title) > POLL_TITLE_MAX_LENGTH:
        raise ValueError(
            f"The title must be {POLL_TITLE_MAX_LENGTH} characters or fewer."
        )
    return title


def parse_poll_options(text: str) -> tuple[str, ...]:
    options = tuple(
        line.strip() for line in text.splitlines() if line.strip()
    )
    if len(options) < 2:
        raise ValueError(
            "Enter at least two options, one per line."
        )
    if len(options) > MAX_POLL_OPTIONS:
        raise ValueError(
            f"A poll can have at most {MAX_POLL_OPTIONS} options, one per line."
        )
    for option in options:
        if len(option) > POLL_OPTION_MAX_LENGTH:
            raise ValueError(
                f"Each option must be {POLL_OPTION_MAX_LENGTH} characters or "
                "fewer."
            )
    return options


def parse_poll_duration(text: str) -> int:
    # Reuses the event HH:mm parser; a large hour value simply makes the poll
    # run for multiple days.
    return parse_event_duration(text)


def format_poll_options_input(options: tuple[str, ...]) -> str:
    # One option per line, re-parseable for pre-filling the edit modal.
    return "\n".join(options)


def format_poll_duration_input(created_at: datetime, end_time: datetime) -> str:
    minutes = max(0, round((end_time - created_at).total_seconds() / 60))
    hours, mins = divmod(minutes, 60)
    return f"{hours}:{mins:02d}"


def _progress_bar(percentage: float) -> str:
    filled = round(percentage / 100 * _BAR_SEGMENTS)
    filled = max(0, min(_BAR_SEGMENTS, filled))
    return "█" * filled + "░" * (_BAR_SEGMENTS - filled)


def _embed_title(title: str, *, ended: bool) -> str:
    prefix = "🔒 " if ended else "📊 "
    budget = EMBED_TITLE_LIMIT - len(prefix)
    if len(title) > budget:
        title = title[: budget - len(_TRUNCATION_MARKER)].rstrip()
        title += _TRUNCATION_MARKER
    return f"{prefix}{title}"


def _winner_line(poll: Poll, counts: Mapping[int, int]) -> str:
    total = total_votes(counts)
    if total <= 0:
        return "No votes were cast."
    best = max(counts.get(index, 0) for index in range(len(poll.options)))
    winners = [
        poll.options[index]
        for index in range(len(poll.options))
        if counts.get(index, 0) == best
    ]
    if len(winners) == 1:
        return f"**Winner:** {winners[0]} ({best} votes)"
    return f"**Tie:** {', '.join(winners)} ({best} votes each)"


def build_poll_embed(
    poll: Poll,
    counts: Mapping[int, int],
    *,
    ended: bool = False,
    now: datetime | None = None,
) -> discord.Embed:
    total = total_votes(counts)
    lines: list[str] = []
    if ended:
        lines.append("**This poll has ended.**")
        lines.append(_winner_line(poll, counts))
    else:
        mode = (
            "Select one or more options."
            if poll.allow_multiple
            else "Select a single option."
        )
        lines.append(f"React to vote. {mode}")
    lines.append("")
    for index, option in enumerate(poll.options):
        count = counts.get(index, 0)
        percentage = option_percentage(counts, index)
        plural = "s" if count != 1 else ""
        lines.append(f"{emoji_for_index(index)} **{option}**")
        lines.append(
            f"`{_progress_bar(percentage)}` {count} vote{plural} "
            f"({percentage:.0f}%)"
        )
    lines.append("")
    total_plural = "s" if total != 1 else ""
    lines.append(f"🗳️ {total} total vote{total_plural}")
    if ended:
        lines.append("Voting is closed.")
    else:
        end_epoch = int(poll.end_time.timestamp())
        lines.append(f"⏳ Ends <t:{end_epoch}:R>")

    description = "\n".join(lines)
    if len(description) > EMBED_DESCRIPTION_LIMIT:
        keep = EMBED_DESCRIPTION_LIMIT - len(_TRUNCATION_MARKER)
        description = description[:keep].rstrip() + _TRUNCATION_MARKER

    embed = discord.Embed(
        title=_embed_title(poll.title, ended=ended),
        description=description,
        color=_ENDED_COLOR if ended else _OPEN_COLOR,
    )
    embed.set_footer(text=f"pollID: {poll.poll_id}")
    return embed


def describe_poll(poll: Poll) -> str:
    # Short human summary used in confirmation / status messages. Contains only
    # counts and the channel mention, never the option text.
    mode = "multiple choice" if poll.allow_multiple else "single choice"
    return (
        f"**{poll.title}** — {len(poll.options)} options, {mode}, "
        f"running for {format_duration(_duration_minutes(poll))} in "
        f"<#{poll.channel_id}>"
    )


def _duration_minutes(poll: Poll) -> int:
    return max(0, round((poll.end_time - poll.created_at).total_seconds() / 60))
