from __future__ import annotations

import logging
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import (
    Boolean,
    Column,
    Float,
    ForeignKey,
    Integer,
    String,
    create_engine,
    func,
)
from sqlalchemy.engine import Engine, URL
from sqlalchemy.inspection import inspect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

LOGGER = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


class SettingRecord(Base):
    __tablename__ = "metadata"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(String, nullable=False)


class RaffleTotalRecord(Base):
    __tablename__ = "raffle_totals"

    username: Mapped[str] = mapped_column(String, primary_key=True)
    coins_deposited: Mapped[int] = mapped_column(Integer, nullable=False)
    raffle_tickets: Mapped[int] = mapped_column(Integer, nullable=False)
    gold_raffle_tickets: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )
    manual_raffle_tickets: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )


class RaffleAccountLinkRecord(Base):
    __tablename__ = "raffle_account_links"

    discord_user_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String, nullable=False)


class RaffleManualTicketRecord(Base):
    __tablename__ = "raffle_manual_tickets"

    ticket_id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )
    username: Mapped[str] = mapped_column(String, nullable=False)
    event_time: Mapped[str] = mapped_column(String, nullable=False)


class RaffleMilestoneRecord(Base):
    __tablename__ = "raffle_milestones"

    threshold: Mapped[int] = mapped_column(Integer, primary_key=True)
    tier_name: Mapped[str] = mapped_column(String, nullable=False)
    notification_sent: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
    )


class RaffleDepositRecord(Base):
    __tablename__ = "raffle_deposits"

    event_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String, nullable=False)
    coins_deposited: Mapped[int] = mapped_column(Integer, nullable=False)
    raffle_tickets: Mapped[int] = mapped_column(Integer, nullable=False)
    event_time: Mapped[str] = mapped_column(String, nullable=False)
    notification_sent: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
    )
    audit_notification_sent: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
    )


class GuildLeaveRecord(Base):
    __tablename__ = "guild_leave_events"

    event_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String, nullable=False)
    kicked_by: Mapped[str | None] = mapped_column(String, nullable=True)
    event_time: Mapped[str] = mapped_column(String, nullable=False)
    notification_sent: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
    )


class GuildJoinRecord(Base):
    __tablename__ = "guild_join_events"

    event_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String, nullable=False)
    event_time: Mapped[str] = mapped_column(String, nullable=False)
    notification_sent: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
    )


class GuildInviteRecord(Base):
    __tablename__ = "guild_invite_events"

    event_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String, nullable=False)
    invited_by: Mapped[str | None] = mapped_column(String, nullable=True)
    event_time: Mapped[str] = mapped_column(String, nullable=False)
    notification_sent: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
    )


class GuildRankChangeRecord(Base):
    __tablename__ = "guild_rank_change_events"

    event_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String, nullable=False)
    old_rank: Mapped[str] = mapped_column(String, nullable=False)
    new_rank: Mapped[str] = mapped_column(String, nullable=False)
    changed_by: Mapped[str | None] = mapped_column(String, nullable=True)
    event_time: Mapped[str] = mapped_column(String, nullable=False)
    notification_sent: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
    )


class TrackedTrialMemberRecord(Base):
    __tablename__ = "tracked_trial_members"

    username: Mapped[str] = mapped_column(String, primary_key=True)
    tracked_by_discord_user_id: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )
    tracked_at: Mapped[str] = mapped_column(String, nullable=False)


class TrialForumPostRecord(Base):
    __tablename__ = "trial_forum_posts"

    thread_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    normalized_content: Mapped[str] = mapped_column(String, nullable=False)
    last_activity: Mapped[str] = mapped_column(String, nullable=False)
    indexed_at: Mapped[str] = mapped_column(String, nullable=False)


class FeastAlertRecord(Base):
    __tablename__ = "feast_alert_state"

    guild_storage_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    last_notification_time: Mapped[float] = mapped_column(Float, nullable=False)


class RaffleRunRecord(Base):
    __tablename__ = "raffle_runs"

    run_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_time: Mapped[str] = mapped_column(
        String,
        nullable=False,
        server_default=func.current_timestamp(),
    )
    winner: Mapped[str] = mapped_column(String, nullable=False)
    winning_ticket: Mapped[int] = mapped_column(Integer, nullable=False)
    total_tickets: Mapped[int] = mapped_column(Integer, nullable=False)
    purchased_tickets: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )
    free_tickets: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )
    announcement_sent: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
    )


class RaffleRunEntryRecord(Base):
    __tablename__ = "raffle_run_entries"

    run_id: Mapped[int] = mapped_column(
        ForeignKey("raffle_runs.run_id"),
        primary_key=True,
    )
    username: Mapped[str] = mapped_column(String, primary_key=True)
    raffle_tickets: Mapped[int] = mapped_column(Integer, nullable=False)


class EventRecord(Base):
    __tablename__ = "gw2_events"

    event_id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )
    category: Mapped[str] = mapped_column(String, nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(String, nullable=False)
    channel_id: Mapped[int] = mapped_column(Integer, nullable=False)
    leader_discord_id: Mapped[int] = mapped_column(Integer, nullable=False)
    start_time: Mapped[str] = mapped_column(String, nullable=False)
    duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    repeat_frequency: Mapped[str] = mapped_column(
        String,
        nullable=False,
        default="none",
    )
    repeat_days: Mapped[str] = mapped_column(String, nullable=False, default="")
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    cancelled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
    )
    delete_previous_on_repeat: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
    )


class EventOccurrenceRecord(Base):
    __tablename__ = "gw2_event_occurrences"

    occurrence_id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )
    event_id: Mapped[int] = mapped_column(
        ForeignKey("gw2_events.event_id"),
        nullable=False,
    )
    start_time: Mapped[str] = mapped_column(String, nullable=False)
    # The channel the message was actually posted to. An event's channel can be
    # changed later, and occurrences that were not re-posted keep living in the
    # channel they were sent to, so a message must be resolved through this and
    # not through the event's current channel. NULL until the occurrence posts.
    channel_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    thread_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="open")
    needs_refresh: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
    )


class EventSignupRecord(Base):
    __tablename__ = "gw2_event_signups"

    occurrence_id: Mapped[int] = mapped_column(
        ForeignKey("gw2_event_occurrences.occurrence_id"),
        primary_key=True,
    )
    discord_user_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    role: Mapped[str | None] = mapped_column(String, nullable=True)
    assigned_role: Mapped[str | None] = mapped_column(String, nullable=True)
    flex_roles: Mapped[str] = mapped_column(String, nullable=False, default="")
    signed_up_at: Mapped[str] = mapped_column(String, nullable=False)
    waitlisted: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
    )


class EventSignupPreferenceRecord(Base):
    __tablename__ = "gw2_event_signup_preferences"

    discord_user_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    role: Mapped[str | None] = mapped_column(String, nullable=True)
    flex_roles: Mapped[str] = mapped_column(String, nullable=False, default="")
    mode: Mapped[str] = mapped_column(String, nullable=False, default="ask")


class EventAutoSignupRecord(Base):
    __tablename__ = "gw2_event_auto_signups"

    event_id: Mapped[int] = mapped_column(
        ForeignKey("gw2_events.event_id"),
        primary_key=True,
    )
    discord_user_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    choice: Mapped[str] = mapped_column(String, nullable=False)
    role: Mapped[str | None] = mapped_column(String, nullable=True)
    flex_roles: Mapped[str] = mapped_column(String, nullable=False, default="")


class RaffleRunWinnerRecord(Base):
    __tablename__ = "raffle_run_winners"

    run_id: Mapped[int] = mapped_column(
        ForeignKey("raffle_runs.run_id"),
        primary_key=True,
    )
    draw_position: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String, nullable=False)
    winning_ticket: Mapped[int] = mapped_column(Integer, nullable=False)
    tickets_before_draw: Mapped[int] = mapped_column(Integer, nullable=False)


def create_database_engine(database_path: str) -> Engine:
    path = Path(database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    LOGGER.debug("Creating SQLite database engine at %s", path)
    return create_engine(URL.create("sqlite", database=str(path)))


def initialize_database(engine: Engine) -> set[str]:
    LOGGER.debug("Initializing database schema")
    # New databases use ORM metadata; Alembic upgrades pre-ORM database files.
    Base.metadata.create_all(engine)
    added_columns: set[str] = set()

    with engine.begin() as connection:
        total_columns = {
            column["name"]
            for column in inspect(connection).get_columns(
                RaffleTotalRecord.__tablename__
            )
        }
        operations = Operations(MigrationContext.configure(connection))
        for column_name in ("gold_raffle_tickets", "manual_raffle_tickets"):
            if column_name in total_columns:
                continue
            operations.add_column(
                RaffleTotalRecord.__tablename__,
                Column(
                    column_name,
                    Integer,
                    nullable=False,
                    server_default="0",
                ),
            )
            added_columns.add(column_name)

        deposit_columns = {
            column["name"]
            for column in inspect(connection).get_columns(
                RaffleDepositRecord.__tablename__
            )
        }
        if "audit_notification_sent" not in deposit_columns:
            operations.add_column(
                RaffleDepositRecord.__tablename__,
                Column(
                    "audit_notification_sent",
                    Boolean,
                    nullable=False,
                    server_default="0",
                ),
            )
            # The legacy flag tracked audit delivery. Preserve pending audits while
            # treating the newly introduced contribution-channel delivery as done.
            connection.exec_driver_sql(
                "UPDATE raffle_deposits "
                "SET audit_notification_sent = notification_sent, "
                "notification_sent = 1"
            )
            added_columns.add("audit_notification_sent")

        occurrence_columns = {
            column["name"]
            for column in inspect(connection).get_columns(
                EventOccurrenceRecord.__tablename__
            )
        }
        if "needs_refresh" not in occurrence_columns:
            operations.add_column(
                EventOccurrenceRecord.__tablename__,
                Column(
                    "needs_refresh",
                    Boolean,
                    nullable=False,
                    server_default="0",
                ),
            )
            added_columns.add("needs_refresh")

        if "channel_id" not in occurrence_columns:
            operations.add_column(
                EventOccurrenceRecord.__tablename__,
                Column("channel_id", Integer, nullable=True),
            )
            # Legacy rows predate per-occurrence channels. Every message they
            # reference was posted through the event's channel, which is the
            # only information those rows ever carried, so backfill from it.
            # Posts made before a channel change are unrecoverable this way, but
            # the backfill is no worse than the behaviour it replaces.
            connection.exec_driver_sql(
                "UPDATE gw2_event_occurrences SET channel_id = ("
                "SELECT channel_id FROM gw2_events "
                "WHERE gw2_events.event_id = gw2_event_occurrences.event_id"
                ") WHERE message_id IS NOT NULL"
            )
            added_columns.add("channel_id")

        event_columns = {
            column["name"]
            for column in inspect(connection).get_columns(
                EventRecord.__tablename__
            )
        }
        if "delete_previous_on_repeat" not in event_columns:
            operations.add_column(
                EventRecord.__tablename__,
                Column(
                    "delete_previous_on_repeat",
                    Boolean,
                    nullable=False,
                    server_default="0",
                ),
            )
            added_columns.add("delete_previous_on_repeat")

        guild_leave_columns = {
            column["name"]
            for column in inspect(connection).get_columns(
                GuildLeaveRecord.__tablename__
            )
        }
        if "kicked_by" not in guild_leave_columns:
            operations.add_column(
                GuildLeaveRecord.__tablename__,
                Column("kicked_by", String, nullable=True),
            )
            added_columns.add("kicked_by")

        run_columns = {
            column["name"]
            for column in inspect(connection).get_columns(
                RaffleRunRecord.__tablename__
            )
        }
        if "purchased_tickets" not in run_columns:
            operations.add_column(
                RaffleRunRecord.__tablename__,
                Column(
                    "purchased_tickets",
                    Integer,
                    nullable=False,
                    server_default="0",
                ),
            )
            connection.exec_driver_sql(
                "UPDATE raffle_runs SET purchased_tickets = total_tickets"
            )
            added_columns.add("purchased_tickets")
        if "free_tickets" not in run_columns:
            operations.add_column(
                RaffleRunRecord.__tablename__,
                Column(
                    "free_tickets",
                    Integer,
                    nullable=False,
                    server_default="0",
                ),
            )
            connection.exec_driver_sql(
                "UPDATE raffle_runs SET free_tickets = "
                "CASE WHEN total_tickets >= purchased_tickets "
                "THEN total_tickets - purchased_tickets ELSE 0 END"
            )
            added_columns.add("free_tickets")
        if "announcement_sent" not in run_columns:
            # Legacy runs predate delivery tracking and cannot be recovered.
            operations.add_column(
                RaffleRunRecord.__tablename__,
                Column(
                    "announcement_sent",
                    Boolean,
                    nullable=False,
                    server_default="0",
                ),
            )
            connection.exec_driver_sql(
                "UPDATE raffle_runs SET announcement_sent = 1"
            )
            added_columns.add("announcement_sent")

    LOGGER.debug(
        "Database schema initialization completed; added_columns=%s",
        sorted(added_columns),
    )
    return added_columns
