"""The accounts invited in-game that have not accepted yet.

Each invitee is matched to a Discord account through the Trial application
forum index, the same matching the Trial reports use.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

from gw2bot.gw2.guild_members import (
    TrialMemberReportEntry,
    format_pending_invite_report,
    get_pending_invite_members,
    get_pending_invite_times,
)

if TYPE_CHECKING:
    from gw2bot.bot import Gw2Bot

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PendingInvites:
    """The accounts still holding an invite, and how well they were matched.

    ``forum_read`` is False when the Trial application forum could not be
    read. Every entry is unmatched then, which says nothing about whether the
    account applied, so a caller must not report those as confirmed
    non-matches or keep them as an answer.

    ``invited_at`` says when each invitation was sent, keyed by the same
    account name its entry carries. An account the GW2 API gave no readable
    timestamp for is absent from it rather than dated with a guess, so a
    caller has to be ready for a name it holds nothing about.
    """

    entries: list[TrialMemberReportEntry]
    forum_read: bool
    invited_at: dict[str, datetime] = field(default_factory=dict)


async def build_pending_invite_entries(bot: Gw2Bot) -> PendingInvites:
    """Every account invited in-game that has not accepted yet.

    The names come from the same guild member list the member count topic is
    built from, and each is matched to a Discord account through the Trial
    application forum index - the same matching the Trial reports use, so an
    invitee who applied is named by their mention rather than by their account
    name alone.
    """
    guild_id = bot._config.gw2_guild_id
    if bot._api is None or guild_id is None:
        raise RuntimeError("GW2 API client was not initialized")
    members = await bot._api.get_guild_members(guild_id)
    usernames = get_pending_invite_members(members)
    if not usernames:
        LOGGER.debug(
            "Built pending invite list; members=%s pending=0 matched=0 "
            "forum_read=true",
            len(members),
        )
        return PendingInvites([], True)
    invited_at = get_pending_invite_times(members)
    # Only the match matters here: the report drops the in-game status label
    # an invited account has no rank for, and the roster page names the
    # matched Discord account itself. Asking for the status would cost a
    # member fetch per matched invite for a value nothing reads.
    matches = await bot._resolve_trial_forum_matches(
        usernames, resolve_status=False
    )
    LOGGER.debug(
        "Built pending invite list; members=%s pending=%s matched=%s "
        "forum_read=%s dated=%s",
        len(members),
        len(matches.entries),
        sum(
            1
            for entry in matches.entries
            if entry.discord_user_id is not None
        ),
        matches.forum_read,
        len(invited_at),
    )
    return PendingInvites(matches.entries, matches.forum_read, invited_at)


async def build_pending_invite_messages(bot: Gw2Bot) -> list[str]:
    pending = await build_pending_invite_entries(bot)
    messages = format_pending_invite_report(
        pending.entries, forum_read=pending.forum_read
    )
    LOGGER.debug("Formatted pending invite report into %s messages", len(messages))
    return messages
