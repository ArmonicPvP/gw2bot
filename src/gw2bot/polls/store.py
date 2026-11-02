"""Polls, their options, and the votes cast while they are open."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session, sessionmaker

from gw2bot.core.database import (
    PollOptionRecord,
    PollRecord,
    PollVoteRecord,
    create_database_engine,
    initialize_database,
)
from gw2bot.polls.models import Poll, PollResults, VoteOutcome

LOGGER = logging.getLogger(__name__)


def _serialize_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


class PollStore:
    def __init__(self, database_path: str):
        LOGGER.debug("Opening poll store")
        self._engine = create_database_engine(database_path)
        initialize_database(self._engine)
        self._sessions = sessionmaker(self._engine, expire_on_commit=False)
        LOGGER.debug("Poll store initialized")

    def close(self) -> None:
        LOGGER.debug("Closing poll store")
        self._engine.dispose()

    def create_poll(
        self,
        *,
        name: str,
        description: str,
        options: Sequence[str],
        channel_id: int,
        created_by: int,
        ends_at: datetime | None = None,
        now: datetime | None = None,
    ) -> Poll:
        created_at = now if now is not None else datetime.now(UTC)
        with self._sessions.begin() as session:
            record = PollRecord(
                name=name,
                description=description,
                channel_id=channel_id,
                message_id=None,
                created_by=created_by,
                created_at=_serialize_time(created_at),
                ends_at=None if ends_at is None else _serialize_time(ends_at),
            )
            session.add(record)
            session.flush()
            session.add_all(
                PollOptionRecord(
                    poll_id=record.poll_id,
                    position=position,
                    label=label,
                )
                for position, label in enumerate(options)
            )
            poll_id = record.poll_id
        LOGGER.debug(
            "Created poll; poll_id=%s options=%s has_end_time=%s",
            poll_id,
            len(options),
            ends_at is not None,
        )
        poll = self.get_poll(poll_id)
        assert poll is not None
        return poll

    def set_message(self, poll_id: int, message_id: int) -> None:
        with self._sessions.begin() as session:
            session.execute(
                update(PollRecord)
                .where(PollRecord.poll_id == poll_id)
                .values(message_id=message_id)
            )
        LOGGER.debug("Recorded poll message; poll_id=%s", poll_id)

    def delete_poll(self, poll_id: int) -> None:
        """Remove a poll whose post never went up, with everything under it."""
        with self._sessions.begin() as session:
            session.execute(
                delete(PollVoteRecord).where(PollVoteRecord.poll_id == poll_id)
            )
            session.execute(
                delete(PollOptionRecord).where(PollOptionRecord.poll_id == poll_id)
            )
            session.execute(delete(PollRecord).where(PollRecord.poll_id == poll_id))
        LOGGER.debug("Deleted poll; poll_id=%s", poll_id)

    def get_poll(self, poll_id: int) -> Poll | None:
        with self._sessions() as session:
            record = session.get(PollRecord, poll_id)
            if record is None:
                return None
            return _poll_from_record(session, record)

    def open_polls(self) -> list[Poll]:
        """Every poll still taking votes, newest first."""
        with self._sessions() as session:
            records = session.scalars(
                select(PollRecord)
                .where(PollRecord.closed_at.is_(None))
                .order_by(PollRecord.poll_id.desc())
            ).all()
            return [_poll_from_record(session, record) for record in records]

    def closed_polls_since(self, since: datetime) -> list[Poll]:
        """Polls ended at or after ``since``, most recently ended first."""
        with self._sessions() as session:
            records = session.scalars(
                select(PollRecord).where(PollRecord.closed_at.is_not(None))
            ).all()
            polls = [_poll_from_record(session, record) for record in records]
        recent = [
            poll
            for poll in polls
            if poll.closed_at is not None and poll.closed_at >= since
        ]
        return sorted(
            recent,
            key=lambda poll: poll.closed_at or since,
            reverse=True,
        )

    def due_polls(self, now: datetime) -> list[Poll]:
        """Open polls whose end time has come, oldest first."""
        with self._sessions() as session:
            records = session.scalars(
                select(PollRecord)
                .where(
                    PollRecord.closed_at.is_(None),
                    PollRecord.ends_at.is_not(None),
                )
                .order_by(PollRecord.poll_id)
            ).all()
            polls = [_poll_from_record(session, record) for record in records]
        # Compared as datetimes rather than in SQL: the stored text is only
        # ordered like the times it holds while every value has the same shape.
        return [poll for poll in polls if poll.is_due(now)]

    def voter_count(self, poll_id: int) -> int:
        with self._sessions() as session:
            return session.scalar(
                select(func.count())
                .select_from(PollVoteRecord)
                .where(PollVoteRecord.poll_id == poll_id)
            ) or 0

    def current_vote(self, poll_id: int, discord_user_id: int) -> int | None:
        """The position this member last confirmed, or None if they have not."""
        with self._sessions() as session:
            record = session.get(PollVoteRecord, (poll_id, discord_user_id))
            return None if record is None else record.position

    def record_vote(
        self,
        poll_id: int,
        discord_user_id: int,
        position: int,
        now: datetime | None = None,
    ) -> VoteOutcome:
        """Store a confirmed vote, replacing the member's earlier one.

        The poll is re-read inside the same transaction, so a confirmation
        left open while the poll was ended, or past its end time, is refused
        rather than counted.
        """
        current_time = now if now is not None else datetime.now(UTC)
        with self._sessions.begin() as session:
            poll = session.get(PollRecord, poll_id)
            if (
                poll is None
                or poll.closed_at is not None
                or (
                    poll.ends_at is not None
                    and _parse_time(poll.ends_at) <= current_time
                )
            ):
                outcome = VoteOutcome.CLOSED
            elif session.get(PollOptionRecord, (poll_id, position)) is None:
                raise ValueError(f"poll {poll_id} has no option {position}")
            else:
                existing = session.get(PollVoteRecord, (poll_id, discord_user_id))
                if existing is None:
                    session.add(
                        PollVoteRecord(
                            poll_id=poll_id,
                            discord_user_id=discord_user_id,
                            position=position,
                        )
                    )
                    outcome = VoteOutcome.RECORDED
                elif existing.position == position:
                    outcome = VoteOutcome.UNCHANGED
                else:
                    existing.position = position
                    outcome = VoteOutcome.CHANGED
        # The choice itself is never logged: a line tying a member to an
        # option would undo the anonymity the poll promises.
        LOGGER.debug(
            "Recorded poll vote; poll_id=%s user_id=%s outcome=%s",
            poll_id,
            discord_user_id,
            outcome.value,
        )
        return outcome

    def close_poll(
        self,
        poll_id: int,
        closed_by: int | None,
        now: datetime | None = None,
    ) -> PollResults | None:
        """End a poll, freeze its tallies, and forget who voted for what.

        ``closed_by`` is None when the poll reached its end time. Returns None
        when the poll is gone or was already ended, which only happens when
        two closes - two officers, or an officer and the end time - collide.
        """
        closed_at = now if now is not None else datetime.now(UTC)
        with self._sessions.begin() as session:
            record = session.get(PollRecord, poll_id)
            if record is None or record.closed_at is not None:
                LOGGER.debug(
                    "Skipped closing a poll that is not open; poll_id=%s "
                    "found=%s",
                    poll_id,
                    record is not None,
                )
                return None
            tallies: dict[int, int] = {
                position: count
                for position, count in session.execute(
                    select(PollVoteRecord.position, func.count())
                    .where(PollVoteRecord.poll_id == poll_id)
                    .group_by(PollVoteRecord.position)
                )
            }
            options = session.scalars(
                select(PollOptionRecord)
                .where(PollOptionRecord.poll_id == poll_id)
                .order_by(PollOptionRecord.position)
            ).all()
            for option in options:
                option.final_votes = tallies.get(option.position, 0)
            record.closed_at = _serialize_time(closed_at)
            record.closed_by = closed_by
            session.execute(
                delete(PollVoteRecord).where(PollVoteRecord.poll_id == poll_id)
            )
            session.flush()
            poll = _poll_from_record(session, record)
            counts = tuple(option.final_votes or 0 for option in options)
        LOGGER.debug(
            "Closed poll; poll_id=%s options=%s votes=%s",
            poll_id,
            len(counts),
            sum(counts),
        )
        return PollResults(poll=poll, counts=counts)


    def results(self, poll_id: int) -> PollResults | None:
        """The tallies frozen when a poll closed; None if it is missing or open."""
        with self._sessions() as session:
            record = session.get(PollRecord, poll_id)
            if record is None or record.closed_at is None:
                return None
            counts = session.scalars(
                select(PollOptionRecord.final_votes)
                .where(PollOptionRecord.poll_id == poll_id)
                .order_by(PollOptionRecord.position)
            ).all()
            return PollResults(
                poll=_poll_from_record(session, record),
                counts=tuple(count or 0 for count in counts),
            )


def _poll_from_record(session: Session, record: PollRecord) -> Poll:
    labels = session.scalars(
        select(PollOptionRecord.label)
        .where(PollOptionRecord.poll_id == record.poll_id)
        .order_by(PollOptionRecord.position)
    ).all()
    return Poll(
        poll_id=record.poll_id,
        name=record.name,
        description=record.description,
        options=tuple(labels),
        channel_id=record.channel_id,
        message_id=record.message_id,
        created_by=record.created_by,
        created_at=_parse_time(record.created_at),
        closed_at=(
            None if record.closed_at is None else _parse_time(record.closed_at)
        ),
        closed_by=record.closed_by,
        ends_at=None if record.ends_at is None else _parse_time(record.ends_at),
    )
