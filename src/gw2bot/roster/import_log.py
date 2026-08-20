from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import discord

from gw2bot.discord_utils import log_discord_failure, safe_int
from gw2bot.roster.history import parse_membership_message
from gw2bot.roster.models import (
    ImportedMembershipEvent,
    MembershipImportResult,
)

if TYPE_CHECKING:
    from gw2bot.bot import Gw2Bot

LOGGER = logging.getLogger(__name__)

# How often the scan reports where it has got to. A guild's log channel holds
# years of messages, and the import walks all of them, so a long silence would
# be indistinguishable from a stalled command.
PROGRESS_INTERVAL = 2_000


async def import_membership_history(
    bot: Gw2Bot,
    channel: Any,
) -> MembershipImportResult:
    """Read the notification channel's history into the membership log.

    The bot has only recorded joins and leaves since the guild log poller
    started keeping them, but it has been *announcing* them in this channel
    for far longer. Reading its own messages back recovers that history: each
    one names the account and the kind of change, and Discord's own timestamp
    says when the bot posted it - which is within one poll of when it happened.

    Only messages the bot itself wrote are read, and only the three membership
    notifications among them. Everything else in that channel - invitations,
    rank changes, raffle audits, stock alerts, anything a person typed - is
    passed over, as are the `diag` previews, which describe a fictional
    account and nothing that happened.

    Changes at or after the first one the guild log poller recorded are left
    alone: those are already in the database from the guild log itself, and
    importing the notification posted about one would count the same departure
    twice. The scan is idempotent besides, so a run interrupted by a Discord
    failure can simply be run again.
    """
    boundary = bot.raffle_store.get_earliest_polled_membership_time()
    scanned = 0
    matched = 0
    skipped_recent = 0
    completed = True
    collected: list[ImportedMembershipEvent] = []
    bot_user_id = safe_int(getattr(bot.user, "id", None))
    if bot_user_id is None:
        LOGGER.error(
            "Cannot import the roster history; the bot's own user is unknown"
        )
        return MembershipImportResult(
            scanned=0,
            matched=0,
            imported=0,
            duplicates=0,
            skipped_recent=0,
            completed=False,
        )
    LOGGER.debug(
        "Starting the roster history import; bounded=%s",
        boundary is not None,
    )
    try:
        async for message in channel.history(limit=None, oldest_first=False):
            scanned += 1
            if scanned % PROGRESS_INTERVAL == 0:
                LOGGER.debug(
                    "Roster history import in progress; scanned=%s matched=%s",
                    scanned,
                    matched,
                )
            author = getattr(message, "author", None)
            if safe_int(getattr(author, "id", None)) != bot_user_id:
                continue
            content = str(getattr(message, "content", ""))
            parsed = parse_membership_message(content)
            if parsed is None:
                continue
            occurred_at = _message_time(message)
            message_id = safe_int(getattr(message, "id", None))
            if occurred_at is None or message_id is None:
                continue
            matched += 1
            if boundary is not None and occurred_at >= boundary:
                skipped_recent += 1
                continue
            kind, username, actor = parsed
            collected.append(
                ImportedMembershipEvent(
                    message_id=message_id,
                    occurred_at=occurred_at,
                    kind=kind,
                    username=username,
                    actor=actor,
                )
            )
    except discord.DiscordException as error:
        # Whatever was read before the failure is still worth storing: the
        # import is keyed by message id, so a later run picks up from a
        # complete history rather than repeating what this one managed.
        completed = False
        log_discord_failure(
            "Could not finish reading the log channel; scanned=%s matched=%s",
            error,
            scanned,
            matched,
        )

    imported = bot.raffle_store.import_membership_events(collected)
    result = MembershipImportResult(
        scanned=scanned,
        matched=matched,
        imported=imported,
        duplicates=len(collected) - imported,
        skipped_recent=skipped_recent,
        completed=completed,
    )
    LOGGER.info(
        "Roster history import finished; scanned=%s matched=%s imported=%s "
        "duplicates=%s skipped_recent=%s completed=%s",
        result.scanned,
        result.matched,
        result.imported,
        result.duplicates,
        result.skipped_recent,
        result.completed,
    )
    return result


def _message_time(message: Any) -> float | None:
    """When Discord says the message was posted, as a UTC timestamp."""
    created_at = getattr(message, "created_at", None)
    if not isinstance(created_at, datetime):
        return None
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    return created_at.timestamp()


def format_import_result(result: MembershipImportResult) -> str:
    """What the officer who ran the import is told about it."""
    lines = [
        "**Guild roster history import**",
        f"Read {result.scanned} message(s) from the log channel and found "
        f"{result.matched} membership notification(s).",
        f"Imported {result.imported} change(s) into the roster history.",
    ]
    if result.duplicates:
        lines.append(
            f"{result.duplicates} were already imported and were left alone."
        )
    if result.skipped_recent:
        lines.append(
            f"{result.skipped_recent} were skipped because the guild log "
            "poller has already recorded that period."
        )
    if not result.completed:
        lines.append(
            "Discord could not be read to the end, so the history may be "
            "incomplete. Running the import again resumes it safely."
        )
    return "\n".join(lines)
