"""The embeds a poll is posted and closed with."""

from __future__ import annotations

from datetime import datetime

import discord

from gw2bot.polls.models import Poll, PollResults, option_display

OPEN_COLOR = discord.Color.blurple()
CLOSED_COLOR = discord.Color.dark_grey()
RESULT_BAR_WIDTH = 12
OPEN_FOOTER = "Pick from the menu below. Results are shown when the poll ends."


def _votes(count: int) -> str:
    return f"{count} vote" + ("" if count == 1 else "s")


def _with_description(poll: Poll, body: str) -> str:
    if not poll.description:
        return body
    return f"{poll.description}\n\n{body}"


def discord_timestamp(value: datetime) -> str:
    """A time every member sees in their own timezone, with how far away it is."""
    seconds = int(value.timestamp())
    return f"<t:{seconds}:F> (<t:{seconds}:R>)"


def open_poll_embed(poll: Poll, voter_count: int) -> discord.Embed:
    """The post while voting is open: the options, and only how many voted.

    The count per option stays hidden until the poll ends, so an early lead
    cannot sway the members who have not picked yet.
    """
    options = "\n".join(
        option_display(poll.options, position)
        for position in range(len(poll.options))
    )
    embed = discord.Embed(
        title=poll.name,
        description=poll.description or None,
        color=OPEN_COLOR,
    )
    embed.add_field(name="Options", value=options, inline=False)
    if poll.ends_at is not None:
        embed.add_field(name="Ends", value=discord_timestamp(poll.ends_at))
    embed.add_field(name="Votes cast", value=str(voter_count))
    embed.set_footer(text=OPEN_FOOTER)
    return embed


# The partial blocks from one eighth to seven eighths of a cell; index 0 is
# the empty remainder of a bar that ends on a whole cell.
PARTIAL_BLOCKS = ("", "▏", "▎", "▍", "▌", "▋", "▊", "▉")
FULL_BLOCK = "█"
# A no-break space rather than a plain one: Discord can collapse a run of
# ordinary spaces into one, which would shorten every bar that is not full
# and leave them different lengths.
BAR_PADDING = "\u00a0"


def result_bar(count: int, total: int) -> str:
    """An option's share as a bar ``RESULT_BAR_WIDTH`` cells wide.

    Measured in eighths of a cell, so a share that falls between whole cells
    ends on the matching partial block. The rest of the width is padding, so
    every bar is exactly ``RESULT_BAR_WIDTH`` characters and the text after
    each one starts in the same column.
    """
    eighths = round(RESULT_BAR_WIDTH * 8 * count / total) if total else 0
    full, remainder = divmod(eighths, 8)
    bar = FULL_BLOCK * full + PARTIAL_BLOCKS[remainder]
    return bar.ljust(RESULT_BAR_WIDTH, BAR_PADDING)


def results_lines(results: PollResults) -> str:
    """One line per option with its share, then the winner or the tie."""
    total = results.total
    lines: list[str] = []
    for position, count in enumerate(results.counts):
        share = round(100 * count / total) if total else 0
        lines.append(
            f"{option_display(results.poll.options, position)}\n"
            f"`{result_bar(count, total)}` **{_votes(count)}** ({share}%)"
        )
    leaders = results.leaders
    if not leaders:
        lines.append("\nNo votes were cast.")
    elif len(leaders) == 1:
        lines.append(f"\n🏆 **Winner:** {results.poll.options[leaders[0]]}")
    else:
        tied = ", ".join(results.poll.options[position] for position in leaders)
        lines.append(f"\n🤝 **Tie:** {tied}")
    return "\n".join(lines)


def closed_poll_embed(results: PollResults) -> discord.Embed:
    """The post once the poll has ended, with every option's final tally."""
    poll = results.poll
    embed = discord.Embed(
        title=f"{poll.name} (closed)",
        description=_with_description(poll, results_lines(results)),
        color=CLOSED_COLOR,
        timestamp=poll.closed_at,
    )
    embed.add_field(name="Votes cast", value=str(results.total))
    embed.set_footer(text=f"Anonymous poll #{poll.poll_id} · Voting has ended")
    return embed


def poll_message_link(guild_id: int | None, poll: Poll) -> str | None:
    if guild_id is None or poll.message_id is None:
        return None
    return (
        f"https://discord.com/channels/{guild_id}/{poll.channel_id}/"
        f"{poll.message_id}"
    )
