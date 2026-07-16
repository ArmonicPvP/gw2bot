from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.orm import sessionmaker

from gw2bot.database import (
    PollRecord,
    PollVoteRecord,
    create_database_engine,
    initialize_database,
)
from gw2bot.polls.models import Poll, PollVote

LOGGER = logging.getLogger(__name__)


def _serialize_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def _serialize_options(options: tuple[str, ...]) -> str:
    # Each option is a single line, so newline-joining is lossless and mirrors
    # how EventRecord serializes its small list columns.
    return "\n".join(options)


def _parse_options(value: str) -> tuple[str, ...]:
    return tuple(value.split("\n")) if value else ()


def _poll_from_record(record: PollRecord) -> Poll:
    return Poll(
        poll_id=record.poll_id,
        guild_id=record.guild_id,
        channel_id=record.channel_id,
        creator_discord_id=record.creator_discord_id,
        title=record.title,
        options=_parse_options(record.options),
        allow_multiple=record.allow_multiple,
        created_at=_parse_time(record.created_at),
        end_time=_parse_time(record.end_time),
        message_id=record.message_id,
    )


def _vote_from_record(record: PollVoteRecord) -> PollVote:
    return PollVote(
        poll_id=record.poll_id,
        option_index=record.option_index,
        discord_user_id=record.discord_user_id,
        voted_at=_parse_time(record.voted_at),
    )


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
        guild_id: int,
        channel_id: int,
        creator_discord_id: int,
        title: str,
        options: tuple[str, ...],
        allow_multiple: bool,
        duration_minutes: int,
        now: datetime | None = None,
    ) -> Poll:
        created_at = now if now is not None else datetime.now(UTC)
        end_time = created_at + timedelta(minutes=duration_minutes)
        with self._sessions() as session:
            record = PollRecord(
                guild_id=guild_id,
                channel_id=channel_id,
                message_id=None,
                creator_discord_id=creator_discord_id,
                title=title,
                options=_serialize_options(options),
                allow_multiple=allow_multiple,
                created_at=_serialize_time(created_at),
                end_time=_serialize_time(end_time),
            )
            session.add(record)
            session.commit()
            LOGGER.debug(
                "Created poll; poll_id=%s options=%s allow_multiple=%s "
                "duration_minutes=%s title_characters=%s",
                record.poll_id,
                len(options),
                allow_multiple,
                duration_minutes,
                len(title),
            )
            return _poll_from_record(record)

    def update_poll(
        self,
        *,
        poll_id: int,
        title: str,
        options: tuple[str, ...],
        allow_multiple: bool,
        end_time: datetime,
    ) -> Poll:
        with self._sessions() as session:
            record = session.get(PollRecord, poll_id)
            if record is None:
                raise ValueError(f"Unknown poll {poll_id}")
            record.title = title
            record.options = _serialize_options(options)
            record.allow_multiple = allow_multiple
            record.end_time = _serialize_time(end_time)
            session.commit()
            LOGGER.debug(
                "Updated poll; poll_id=%s options=%s allow_multiple=%s "
                "title_characters=%s",
                poll_id,
                len(options),
                allow_multiple,
                len(title),
            )
            return _poll_from_record(record)

    def set_poll_message(
        self,
        poll_id: int,
        channel_id: int,
        message_id: int,
    ) -> None:
        # Channel is stored with the message because a channel change re-posts
        # the poll, and the (channel, message) pair is what any later edit or
        # delete has to address.
        with self._sessions() as session:
            record = session.get(PollRecord, poll_id)
            if record is None:
                raise ValueError(f"Unknown poll {poll_id}")
            record.channel_id = channel_id
            record.message_id = message_id
            session.commit()
        LOGGER.debug("Stored poll message; poll_id=%s", poll_id)

    def get_poll(self, poll_id: int) -> Poll | None:
        with self._sessions() as session:
            record = session.get(PollRecord, poll_id)
            return _poll_from_record(record) if record is not None else None

    def get_poll_by_message(self, message_id: int) -> Poll | None:
        with self._sessions() as session:
            record = session.scalars(
                select(PollRecord)
                .where(PollRecord.message_id == message_id)
                .limit(1)
            ).first()
            return _poll_from_record(record) if record is not None else None

    def get_active_polls(self) -> list[Poll]:
        # Every stored poll is active: finalizing deletes the row. Ordered
        # newest first so autocomplete surfaces the most recent polls.
        with self._sessions() as session:
            records = session.scalars(
                select(PollRecord).order_by(PollRecord.poll_id.desc())
            ).all()
            return [_poll_from_record(record) for record in records]

    def get_expired_polls(self, now: datetime | None = None) -> list[Poll]:
        current_time = now if now is not None else datetime.now(UTC)
        # Stored times share one UTC ISO format, so string comparison matches
        # chronological order.
        with self._sessions() as session:
            records = session.scalars(
                select(PollRecord)
                .where(PollRecord.message_id.is_not(None))
                .where(PollRecord.end_time <= _serialize_time(current_time))
                .order_by(PollRecord.poll_id)
            ).all()
            return [_poll_from_record(record) for record in records]

    def delete_poll(self, poll_id: int) -> None:
        with self._sessions() as session:
            session.execute(
                delete(PollVoteRecord).where(
                    PollVoteRecord.poll_id == poll_id
                )
            )
            session.execute(
                delete(PollRecord).where(PollRecord.poll_id == poll_id)
            )
            session.commit()
        LOGGER.debug("Deleted poll with its votes; poll_id=%s", poll_id)

    def add_vote(
        self,
        poll_id: int,
        option_index: int,
        discord_user_id: int,
        now: datetime | None = None,
    ) -> None:
        voted_at = now if now is not None else datetime.now(UTC)
        with self._sessions() as session:
            record = session.get(
                PollVoteRecord,
                (poll_id, option_index, discord_user_id),
            )
            if record is None:
                record = PollVoteRecord(
                    poll_id=poll_id,
                    option_index=option_index,
                    discord_user_id=discord_user_id,
                    voted_at=_serialize_time(voted_at),
                )
                session.add(record)
            else:
                # A remove/re-add of the same option refreshes the timestamp so
                # single-choice enforcement still treats it as the newest vote.
                record.voted_at = _serialize_time(voted_at)
            session.commit()
        LOGGER.debug(
            "Recorded poll vote; poll_id=%s option_index=%s",
            poll_id,
            option_index,
        )

    def remove_vote(
        self,
        poll_id: int,
        option_index: int,
        discord_user_id: int,
    ) -> None:
        with self._sessions() as session:
            record = session.get(
                PollVoteRecord,
                (poll_id, option_index, discord_user_id),
            )
            if record is None:
                return
            session.delete(record)
            session.commit()
        LOGGER.debug(
            "Removed poll vote; poll_id=%s option_index=%s",
            poll_id,
            option_index,
        )

    def clear_votes(self, poll_id: int) -> None:
        with self._sessions() as session:
            session.execute(
                delete(PollVoteRecord).where(
                    PollVoteRecord.poll_id == poll_id
                )
            )
            session.commit()
        LOGGER.debug("Cleared poll votes; poll_id=%s", poll_id)

    def replace_votes(
        self,
        poll_id: int,
        votes: Mapping[int, set[int]],
        now: datetime | None = None,
    ) -> None:
        """Rewrite a poll's votes to exactly match ``votes``.

        This is the reconciliation write: the live reactions on the message are
        the source of truth, so every stored vote is replaced with the passed
        option -> voter-id mapping. Existing timestamps are preserved where the
        vote survives, so single-choice ordering is not reset by reconciliation.
        """
        stamp = _serialize_time(now if now is not None else datetime.now(UTC))
        with self._sessions() as session:
            existing = {
                (record.option_index, record.discord_user_id): record
                for record in session.scalars(
                    select(PollVoteRecord).where(
                        PollVoteRecord.poll_id == poll_id
                    )
                ).all()
            }
            desired: set[tuple[int, int]] = {
                (option_index, user_id)
                for option_index, user_ids in votes.items()
                for user_id in user_ids
            }
            for key, record in existing.items():
                if key not in desired:
                    session.delete(record)
            for option_index, user_id in desired:
                if (option_index, user_id) in existing:
                    continue
                session.add(
                    PollVoteRecord(
                        poll_id=poll_id,
                        option_index=option_index,
                        discord_user_id=user_id,
                        voted_at=stamp,
                    )
                )
            session.commit()
        LOGGER.debug(
            "Reconciled poll votes; poll_id=%s options_with_votes=%s",
            poll_id,
            sum(1 for user_ids in votes.values() if user_ids),
        )

    def get_vote_counts(self, poll_id: int) -> dict[int, int]:
        with self._sessions() as session:
            rows = session.execute(
                select(
                    PollVoteRecord.option_index,
                    func.count(),
                )
                .where(PollVoteRecord.poll_id == poll_id)
                .group_by(PollVoteRecord.option_index)
            ).all()
            return {option_index: count for option_index, count in rows}

    def get_user_options(
        self,
        poll_id: int,
        discord_user_id: int,
    ) -> list[int]:
        with self._sessions() as session:
            rows = session.scalars(
                select(PollVoteRecord.option_index)
                .where(PollVoteRecord.poll_id == poll_id)
                .where(PollVoteRecord.discord_user_id == discord_user_id)
                .order_by(PollVoteRecord.voted_at)
            ).all()
            return list(rows)

    def get_user_option_times(
        self,
        poll_id: int,
    ) -> dict[int, list[tuple[int, datetime]]]:
        """Every voter's options paired with when they voted, oldest first.

        Used to enforce single-choice after a poll is switched from multiple:
        each user's newest option is kept and the rest are dropped.
        """
        with self._sessions() as session:
            records = session.scalars(
                select(PollVoteRecord)
                .where(PollVoteRecord.poll_id == poll_id)
                .order_by(PollVoteRecord.voted_at)
            ).all()
            by_user: dict[int, list[tuple[int, datetime]]] = {}
            for record in records:
                by_user.setdefault(record.discord_user_id, []).append(
                    (record.option_index, _parse_time(record.voted_at))
                )
            return by_user

    def get_votes(self, poll_id: int) -> list[PollVote]:
        with self._sessions() as session:
            records = session.scalars(
                select(PollVoteRecord)
                .where(PollVoteRecord.poll_id == poll_id)
                .order_by(
                    PollVoteRecord.option_index,
                    PollVoteRecord.discord_user_id,
                )
            ).all()
            return [_vote_from_record(record) for record in records]
