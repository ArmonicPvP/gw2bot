from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.orm import sessionmaker

from gw2bot.database import (
    EventAutoSignupRecord,
    EventOccurrenceRecord,
    EventRecord,
    EventSignupPreferenceRecord,
    EventSignupRecord,
    create_database_engine,
    initialize_database,
)
from gw2bot.events.models import (
    AutoSignup,
    AutoSignupChoice,
    Event,
    EventCategory,
    EventOccurrence,
    EventRole,
    EventSignup,
    EventStatus,
    PreferenceMode,
    RepeatFrequency,
    SignupPreference,
)

LOGGER = logging.getLogger(__name__)


def _serialize_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def _serialize_roles(roles: tuple[EventRole, ...]) -> str:
    return ",".join(role.value for role in roles)


def _parse_roles(value: str) -> tuple[EventRole, ...]:
    return tuple(
        EventRole(entry) for entry in value.split(",") if entry
    )


def _serialize_days(days: tuple[int, ...]) -> str:
    return ",".join(str(day) for day in days)


def _parse_days(value: str) -> tuple[int, ...]:
    return tuple(int(entry) for entry in value.split(",") if entry)


def _event_from_record(record: EventRecord) -> Event:
    return Event(
        event_id=record.event_id,
        category=EventCategory(record.category),
        title=record.title,
        description=record.description,
        channel_id=record.channel_id,
        leader_discord_id=record.leader_discord_id,
        start_time=_parse_time(record.start_time),
        duration_minutes=record.duration_minutes,
        repeat_frequency=RepeatFrequency(record.repeat_frequency),
        repeat_days=_parse_days(record.repeat_days),
        cancelled=record.cancelled,
    )


def _occurrence_from_record(
    record: EventOccurrenceRecord,
) -> EventOccurrence:
    return EventOccurrence(
        occurrence_id=record.occurrence_id,
        event_id=record.event_id,
        start_time=_parse_time(record.start_time),
        message_id=record.message_id,
        thread_id=record.thread_id,
        status=EventStatus(record.status),
        needs_refresh=record.needs_refresh,
    )


def _signup_from_record(record: EventSignupRecord) -> EventSignup:
    return EventSignup(
        occurrence_id=record.occurrence_id,
        discord_user_id=record.discord_user_id,
        role=EventRole(record.role) if record.role else None,
        assigned_role=(
            EventRole(record.assigned_role) if record.assigned_role else None
        ),
        flex_roles=_parse_roles(record.flex_roles),
        signed_up_at=_parse_time(record.signed_up_at),
        waitlisted=record.waitlisted,
    )


class EventStore:
    def __init__(self, database_path: str):
        LOGGER.debug("Opening event store")
        self._engine = create_database_engine(database_path)
        initialize_database(self._engine)
        self._sessions = sessionmaker(self._engine, expire_on_commit=False)
        LOGGER.debug("Event store initialized")

    def close(self) -> None:
        LOGGER.debug("Closing event store")
        self._engine.dispose()

    def create_event(
        self,
        *,
        category: EventCategory,
        title: str,
        description: str,
        channel_id: int,
        leader_discord_id: int,
        start_time: datetime,
        duration_minutes: int,
        repeat_frequency: RepeatFrequency,
        repeat_days: tuple[int, ...],
        now: datetime | None = None,
    ) -> Event:
        created_at = now if now is not None else datetime.now(UTC)
        with self._sessions() as session:
            record = EventRecord(
                category=category.value,
                title=title,
                description=description,
                channel_id=channel_id,
                leader_discord_id=leader_discord_id,
                start_time=_serialize_time(start_time),
                duration_minutes=duration_minutes,
                repeat_frequency=repeat_frequency.value,
                repeat_days=_serialize_days(repeat_days),
                created_at=_serialize_time(created_at),
                cancelled=False,
            )
            session.add(record)
            session.commit()
            LOGGER.debug(
                "Created event; event_id=%s category=%s repeat=%s "
                "title_characters=%s",
                record.event_id,
                category.value,
                repeat_frequency.value,
                len(title),
            )
            return _event_from_record(record)

    def update_event(
        self,
        *,
        event_id: int,
        category: EventCategory,
        title: str,
        description: str,
        channel_id: int,
        leader_discord_id: int,
        start_time: datetime,
        duration_minutes: int,
        repeat_frequency: RepeatFrequency,
        repeat_days: tuple[int, ...],
    ) -> Event:
        with self._sessions() as session:
            record = session.get(EventRecord, event_id)
            if record is None:
                raise ValueError(f"Unknown event {event_id}")
            record.category = category.value
            record.title = title
            record.description = description
            record.channel_id = channel_id
            record.leader_discord_id = leader_discord_id
            record.start_time = _serialize_time(start_time)
            record.duration_minutes = duration_minutes
            record.repeat_frequency = repeat_frequency.value
            record.repeat_days = _serialize_days(repeat_days)
            session.commit()
            LOGGER.debug(
                "Updated event; event_id=%s category=%s repeat=%s "
                "title_characters=%s",
                event_id,
                category.value,
                repeat_frequency.value,
                len(title),
            )
            return _event_from_record(record)

    def create_occurrence(
        self,
        event_id: int,
        start_time: datetime,
    ) -> EventOccurrence:
        with self._sessions() as session:
            record = EventOccurrenceRecord(
                event_id=event_id,
                start_time=_serialize_time(start_time),
                message_id=None,
                thread_id=None,
                status=EventStatus.OPEN.value,
            )
            session.add(record)
            session.commit()
            LOGGER.debug(
                "Created event occurrence; event_id=%s occurrence_id=%s",
                event_id,
                record.occurrence_id,
            )
            return _occurrence_from_record(record)

    def set_occurrence_message(
        self,
        occurrence_id: int,
        message_id: int,
        thread_id: int | None,
    ) -> None:
        with self._sessions() as session:
            record = session.get(EventOccurrenceRecord, occurrence_id)
            if record is None:
                raise ValueError(f"Unknown event occurrence {occurrence_id}")
            record.message_id = message_id
            record.thread_id = thread_id
            session.commit()
        LOGGER.debug(
            "Stored occurrence message; occurrence_id=%s has_thread=%s",
            occurrence_id,
            thread_id is not None,
        )

    def set_occurrence_start_time(
        self,
        occurrence_id: int,
        start_time: datetime,
    ) -> None:
        with self._sessions() as session:
            record = session.get(EventOccurrenceRecord, occurrence_id)
            if record is None:
                raise ValueError(f"Unknown event occurrence {occurrence_id}")
            record.start_time = _serialize_time(start_time)
            session.commit()
        LOGGER.debug(
            "Rescheduled occurrence; occurrence_id=%s",
            occurrence_id,
        )

    def set_occurrence_status(
        self,
        occurrence_id: int,
        status: EventStatus,
    ) -> None:
        with self._sessions() as session:
            record = session.get(EventOccurrenceRecord, occurrence_id)
            if record is None:
                raise ValueError(f"Unknown event occurrence {occurrence_id}")
            record.status = status.value
            session.commit()
        LOGGER.debug(
            "Updated occurrence status; occurrence_id=%s status=%s",
            occurrence_id,
            status.value,
        )

    def set_occurrence_needs_refresh(
        self,
        occurrence_id: int,
        needs_refresh: bool,
    ) -> None:
        with self._sessions() as session:
            record = session.get(EventOccurrenceRecord, occurrence_id)
            if record is None:
                raise ValueError(f"Unknown event occurrence {occurrence_id}")
            record.needs_refresh = needs_refresh
            session.commit()
        LOGGER.debug(
            "Updated occurrence refresh flag; occurrence_id=%s "
            "needs_refresh=%s",
            occurrence_id,
            needs_refresh,
        )

    def get_event(self, event_id: int) -> Event | None:
        with self._sessions() as session:
            record = session.get(EventRecord, event_id)
            return _event_from_record(record) if record is not None else None

    def get_occurrence(self, occurrence_id: int) -> EventOccurrence | None:
        with self._sessions() as session:
            record = session.get(EventOccurrenceRecord, occurrence_id)
            return (
                _occurrence_from_record(record)
                if record is not None
                else None
            )

    def get_posted_unfinished_occurrences(self) -> list[EventOccurrence]:
        with self._sessions() as session:
            records = session.scalars(
                select(EventOccurrenceRecord)
                .where(EventOccurrenceRecord.status != EventStatus.OVER.value)
                .where(EventOccurrenceRecord.message_id.is_not(None))
                .order_by(EventOccurrenceRecord.occurrence_id)
            ).all()
            return [_occurrence_from_record(record) for record in records]

    def get_unposted_occurrences(self) -> list[EventOccurrence]:
        with self._sessions() as session:
            records = session.scalars(
                select(EventOccurrenceRecord)
                .where(EventOccurrenceRecord.message_id.is_(None))
                .order_by(EventOccurrenceRecord.occurrence_id)
            ).all()
            return [_occurrence_from_record(record) for record in records]

    def get_event_occurrences(self, event_id: int) -> list[EventOccurrence]:
        with self._sessions() as session:
            records = session.scalars(
                select(EventOccurrenceRecord)
                .where(EventOccurrenceRecord.event_id == event_id)
                .order_by(EventOccurrenceRecord.start_time)
            ).all()
            return [_occurrence_from_record(record) for record in records]

    def get_active_events(self) -> list[Event]:
        with self._sessions() as session:
            # A correlated EXISTS keeps this to a single query with no
            # per-id bound parameters, so it does not hit SQLite's variable
            # limit no matter how many events have accumulated.
            has_active_occurrence = (
                select(EventOccurrenceRecord.occurrence_id)
                .where(
                    EventOccurrenceRecord.event_id == EventRecord.event_id
                )
                .where(
                    EventOccurrenceRecord.status != EventStatus.OVER.value
                )
                .exists()
            )
            records = session.scalars(
                select(EventRecord)
                .where(has_active_occurrence)
                .where(EventRecord.cancelled.is_(False))
                .order_by(EventRecord.event_id.desc())
            ).all()
            return [_event_from_record(record) for record in records]

    def delete_event(self, event_id: int) -> None:
        with self._sessions() as session:
            occurrence_ids = session.scalars(
                select(EventOccurrenceRecord.occurrence_id).where(
                    EventOccurrenceRecord.event_id == event_id
                )
            ).all()
            if occurrence_ids:
                session.execute(
                    delete(EventSignupRecord).where(
                        EventSignupRecord.occurrence_id.in_(occurrence_ids)
                    )
                )
            session.execute(
                delete(EventAutoSignupRecord).where(
                    EventAutoSignupRecord.event_id == event_id
                )
            )
            session.execute(
                delete(EventOccurrenceRecord).where(
                    EventOccurrenceRecord.event_id == event_id
                )
            )
            session.execute(
                delete(EventRecord).where(EventRecord.event_id == event_id)
            )
            session.commit()
        LOGGER.debug(
            "Deleted event with its occurrences and signups; event_id=%s "
            "occurrences=%s",
            event_id,
            len(occurrence_ids),
        )

    def has_posted_occurrence(self, event_id: int) -> bool:
        with self._sessions() as session:
            record = session.scalars(
                select(EventOccurrenceRecord.occurrence_id)
                .where(EventOccurrenceRecord.event_id == event_id)
                .where(EventOccurrenceRecord.message_id.is_not(None))
                .limit(1)
            ).first()
            return record is not None

    def has_later_occurrence(self, event_id: int, after: datetime) -> bool:
        with self._sessions() as session:
            records = session.scalars(
                select(EventOccurrenceRecord).where(
                    EventOccurrenceRecord.event_id == event_id
                )
            ).all()
            return any(
                _parse_time(record.start_time) > after for record in records
            )

    def get_signups(self, occurrence_id: int) -> list[EventSignup]:
        with self._sessions() as session:
            records = session.scalars(
                select(EventSignupRecord)
                .where(EventSignupRecord.occurrence_id == occurrence_id)
                .order_by(EventSignupRecord.signed_up_at)
            ).all()
            return [_signup_from_record(record) for record in records]

    def get_signup(
        self,
        occurrence_id: int,
        discord_user_id: int,
    ) -> EventSignup | None:
        with self._sessions() as session:
            record = session.get(
                EventSignupRecord,
                (occurrence_id, discord_user_id),
            )
            return _signup_from_record(record) if record is not None else None

    def add_signup(
        self,
        *,
        occurrence_id: int,
        discord_user_id: int,
        role: EventRole | None,
        assigned_role: EventRole | None,
        flex_roles: tuple[EventRole, ...],
        waitlisted: bool,
        now: datetime | None = None,
    ) -> EventSignup:
        signed_up_at = now if now is not None else datetime.now(UTC)
        with self._sessions() as session:
            existing = session.get(
                EventSignupRecord,
                (occurrence_id, discord_user_id),
            )
            if existing is not None:
                raise ValueError("You are already signed up for this event.")
            record = EventSignupRecord(
                occurrence_id=occurrence_id,
                discord_user_id=discord_user_id,
                role=role.value if role is not None else None,
                assigned_role=(
                    assigned_role.value if assigned_role is not None else None
                ),
                flex_roles=_serialize_roles(flex_roles),
                signed_up_at=_serialize_time(signed_up_at),
                waitlisted=waitlisted,
            )
            session.add(record)
            session.commit()
            LOGGER.debug(
                "Added event signup; occurrence_id=%s user_id=%s role=%s "
                "assigned_role=%s flex_count=%s waitlisted=%s",
                occurrence_id,
                discord_user_id,
                role.value if role is not None else None,
                assigned_role.value if assigned_role is not None else None,
                len(flex_roles),
                waitlisted,
            )
            return _signup_from_record(record)

    def remove_signup(
        self,
        occurrence_id: int,
        discord_user_id: int,
    ) -> EventSignup | None:
        with self._sessions() as session:
            record = session.get(
                EventSignupRecord,
                (occurrence_id, discord_user_id),
            )
            if record is None:
                return None
            removed = _signup_from_record(record)
            session.delete(record)
            session.commit()
        LOGGER.debug(
            "Removed event signup; occurrence_id=%s user_id=%s waitlisted=%s",
            occurrence_id,
            discord_user_id,
            removed.waitlisted,
        )
        return removed

    def promote_signup(
        self,
        occurrence_id: int,
        discord_user_id: int,
        assigned_role: EventRole | None,
    ) -> None:
        with self._sessions() as session:
            record = session.get(
                EventSignupRecord,
                (occurrence_id, discord_user_id),
            )
            if record is None:
                raise ValueError("The signup to promote no longer exists.")
            record.waitlisted = False
            record.assigned_role = (
                assigned_role.value if assigned_role is not None else None
            )
            session.commit()
        LOGGER.debug(
            "Promoted event signup from waitlist; occurrence_id=%s "
            "user_id=%s assigned_role=%s",
            occurrence_id,
            discord_user_id,
            assigned_role.value if assigned_role is not None else None,
        )

    def get_signup_preference(
        self,
        discord_user_id: int,
    ) -> SignupPreference | None:
        with self._sessions() as session:
            record = session.get(
                EventSignupPreferenceRecord,
                discord_user_id,
            )
            if record is None:
                return None
            return SignupPreference(
                discord_user_id=record.discord_user_id,
                role=EventRole(record.role) if record.role else None,
                flex_roles=_parse_roles(record.flex_roles),
                mode=PreferenceMode(record.mode),
            )

    def set_signup_preference(
        self,
        discord_user_id: int,
        role: EventRole | None,
        flex_roles: tuple[EventRole, ...],
        mode: PreferenceMode,
    ) -> None:
        with self._sessions() as session:
            record = session.get(
                EventSignupPreferenceRecord,
                discord_user_id,
            )
            if record is None:
                record = EventSignupPreferenceRecord(
                    discord_user_id=discord_user_id
                )
                session.add(record)
            record.role = role.value if role is not None else None
            record.flex_roles = _serialize_roles(flex_roles)
            record.mode = mode.value
            session.commit()
        LOGGER.debug(
            "Stored event signup preference; user_id=%s mode=%s "
            "has_role=%s flex_count=%s",
            discord_user_id,
            mode.value,
            role is not None,
            len(flex_roles),
        )

    def get_auto_signup(
        self,
        event_id: int,
        discord_user_id: int,
    ) -> AutoSignup | None:
        with self._sessions() as session:
            record = session.get(
                EventAutoSignupRecord,
                (event_id, discord_user_id),
            )
            if record is None:
                return None
            return AutoSignup(
                event_id=record.event_id,
                discord_user_id=record.discord_user_id,
                choice=AutoSignupChoice(record.choice),
                role=EventRole(record.role) if record.role else None,
                flex_roles=_parse_roles(record.flex_roles),
            )

    def set_auto_signup(
        self,
        event_id: int,
        discord_user_id: int,
        choice: AutoSignupChoice,
        role: EventRole | None,
        flex_roles: tuple[EventRole, ...],
    ) -> None:
        with self._sessions() as session:
            record = session.get(
                EventAutoSignupRecord,
                (event_id, discord_user_id),
            )
            if record is None:
                record = EventAutoSignupRecord(
                    event_id=event_id,
                    discord_user_id=discord_user_id,
                    choice=choice.value,
                )
                session.add(record)
            record.choice = choice.value
            record.role = role.value if role is not None else None
            record.flex_roles = _serialize_roles(flex_roles)
            session.commit()
        LOGGER.debug(
            "Stored event auto signup choice; event_id=%s user_id=%s "
            "choice=%s",
            event_id,
            discord_user_id,
            choice.value,
        )

    def get_auto_signup_entries(self, event_id: int) -> list[AutoSignup]:
        with self._sessions() as session:
            records = session.scalars(
                select(EventAutoSignupRecord)
                .where(EventAutoSignupRecord.event_id == event_id)
                .where(
                    EventAutoSignupRecord.choice == AutoSignupChoice.YES.value
                )
                .order_by(EventAutoSignupRecord.discord_user_id)
            ).all()
            return [
                AutoSignup(
                    event_id=record.event_id,
                    discord_user_id=record.discord_user_id,
                    choice=AutoSignupChoice(record.choice),
                    role=EventRole(record.role) if record.role else None,
                    flex_roles=_parse_roles(record.flex_roles),
                )
                for record in records
            ]
