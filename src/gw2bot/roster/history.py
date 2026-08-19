from __future__ import annotations

import logging
import re
from collections.abc import Sequence

from gw2bot.roster.models import (
    JOIN,
    KICK,
    LEAVE,
    MemberCountSample,
    MembershipEvent,
    RosterEvent,
    RosterPoint,
    RosterSeries,
)

LOGGER = logging.getLogger(__name__)

# The three notification texts the bot posts for a membership change, written
# out here so a log channel can be read back into events. They mirror
# GuildJoin.message and GuildLeave.message exactly; a test round-trips those
# properties through this parser so the two cannot drift apart.
_JOIN_PATTERN = re.compile(r"(?P<username>.+?) has joined the guild\.")
_LEAVE_PATTERN = re.compile(r"(?P<username>.+?) has left the guild\.")
_KICK_PATTERN = re.compile(
    r"(?P<actor>.+?) kicked (?P<username>.+?) from the guild\."
)

# `diag` posts a preview of every automated message, each under a bold header
# ending in this marker. Those previews name a fictional account and describe
# nothing that happened, so they must never reach the history. Every pattern
# above is matched against the whole message, which already rules a two-line
# preview out; this is the belt to that pair of braces, and it is what keeps a
# future one-line preview out too.
DIAGNOSTIC_PREVIEW_MARKER = "(test)"


def parse_membership_message(content: str) -> tuple[str, str, str | None] | None:
    """Read one notification-channel message as a membership change.

    Returns ``(kind, username, actor)``, or ``None`` when the message is not
    one of the three membership notifications - which is most of that channel:
    invitations, rank changes, raffle audits, stock alerts and `diag`
    previews all land there too.
    """
    text = content.strip()
    if not text or DIAGNOSTIC_PREVIEW_MARKER in text:
        return None
    kick = _KICK_PATTERN.fullmatch(text)
    if kick is not None:
        return KICK, kick.group("username"), kick.group("actor")
    join = _JOIN_PATTERN.fullmatch(text)
    if join is not None:
        return JOIN, join.group("username"), None
    leave = _LEAVE_PATTERN.fullmatch(text)
    if leave is not None:
        return LEAVE, leave.group("username"), None
    return None


def build_roster_series(
    events: Sequence[MembershipEvent],
    anchor: MemberCountSample | None,
    since: float,
    now: float,
) -> RosterSeries:
    """Derive the member count line and the plotted events for one window.

    ``events`` must hold every known membership change from the earlier of
    ``since`` and the anchor's own moment onwards; anything later than ``now``
    is ignored. The count at any moment is measured from ``anchor``, the last
    count the bot actually observed, by walking the events between the two.
    Measuring from the newest observation rather than the oldest is what keeps
    the right-hand edge of the graph - the part a reader checks against the
    channel description - true even when an older stretch of events was missed.

    Without an anchor nothing can be placed on a count axis, so the events come
    back with no count and the line is empty rather than invented.
    """
    ordered = sorted(
        (event for event in events if event.occurred_at <= now),
        key=lambda event: event.occurred_at,
    )
    in_window = [
        index
        for index, event in enumerate(ordered)
        if since <= event.occurred_at <= now
    ]
    if anchor is None:
        LOGGER.debug(
            "Built roster series without an observed member count; "
            "events=%s in_window=%s",
            len(ordered),
            len(in_window),
        )
        return RosterSeries(
            points=(),
            events=tuple(
                _plot(ordered[index], None) for index in in_window
            ),
        )

    counts_after, baseline, count_now = _derive_counts(ordered, anchor)
    points = [
        RosterPoint(
            at=since,
            member_count=_count_before(ordered, counts_after, baseline, since),
        )
    ]
    points.extend(
        RosterPoint(
            at=ordered[index].occurred_at,
            member_count=counts_after[index],
        )
        for index in in_window
    )
    points.append(RosterPoint(at=now, member_count=count_now))
    series = RosterSeries(
        points=tuple(points),
        events=tuple(
            _plot(ordered[index], counts_after[index]) for index in in_window
        ),
    )
    LOGGER.debug(
        "Built roster series; events=%s in_window=%s points=%s "
        "anchor_age_seconds=%s current_count=%s",
        len(ordered),
        len(in_window),
        len(series.points),
        int(now - anchor.recorded_at),
        count_now,
    )
    return series


def _plot(event: MembershipEvent, member_count: int | None) -> RosterEvent:
    return RosterEvent(
        occurred_at=event.occurred_at,
        kind=event.kind,
        username=event.username,
        actor=event.actor,
        member_count=member_count,
        imported=event.imported,
    )


def _derive_counts(
    ordered: Sequence[MembershipEvent],
    anchor: MemberCountSample,
) -> tuple[list[int], int, int]:
    """Return the count after each event, before the oldest, and right now.

    ``ordered`` is oldest first. Events at or before the anchor are walked
    backwards from it, subtracting each change to recover the count that stood
    before it; events after the anchor are walked forwards, adding each change.
    """
    counts_after = [0] * len(ordered)
    running = anchor.member_count
    for index in range(len(ordered) - 1, -1, -1):
        if ordered[index].occurred_at > anchor.recorded_at:
            continue
        counts_after[index] = running
        running -= ordered[index].delta
    baseline = running
    running = anchor.member_count
    for index, event in enumerate(ordered):
        if event.occurred_at <= anchor.recorded_at:
            continue
        running += event.delta
        counts_after[index] = running
    return counts_after, baseline, running


def _count_before(
    ordered: Sequence[MembershipEvent],
    counts_after: Sequence[int],
    baseline: int,
    moment: float,
) -> int:
    """The member count as it stood just before ``moment``."""
    result = baseline
    for index, event in enumerate(ordered):
        if event.occurred_at >= moment:
            break
        result = counts_after[index]
    return result
