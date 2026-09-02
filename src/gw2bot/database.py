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
    Index,
    Integer,
    String,
    create_engine,
    func,
)
from sqlalchemy.engine import Engine, URL
from sqlalchemy.inspection import inspect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

LOGGER = logging.getLogger(__name__)

# Set when the raffle totals table gains gold_raffle_tickets, and cleared by
# RaffleStore once it has split the legacy totals. It has to outlive the call
# that added the column, because the settings store may open the database
# first and consume the in-memory signal.
PENDING_LEGACY_TOTALS_KEY = "pending_legacy_totals_split"


class Base(DeclarativeBase):
    pass


class SettingRecord(Base):
    __tablename__ = "metadata"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(String, nullable=False)


class BotSettingRecord(Base):
    """One value an officer set with /settings.

    Kept apart from the `metadata` table above, which is the bot's own
    bookkeeping - log cursors, index watermarks, the guild binding. Mixing
    operator settings into it would make both harder to read, and only this
    table holds anything that may be encrypted.
    """

    __tablename__ = "gw2_bot_settings"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(String, nullable=False)
    is_encrypted: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
    )


class ProfitApiKeyRecord(Base):
    """One encrypted Trading Post API key per Discord member."""

    __tablename__ = "gw2_profit_api_keys"

    discord_user_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    encrypted_api_key: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)


class ProfitCacheSyncRecord(Base):
    """Freshness marker for one member's cached Trading Post collection."""

    __tablename__ = "gw2_profit_cache_syncs"

    discord_user_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cache_kind: Mapped[str] = mapped_column(String, primary_key=True)
    synced_at: Mapped[str] = mapped_column(String, nullable=False)


class ProfitTransactionRecord(Base):
    """The fields from a Trading Post transaction needed by the reports."""

    __tablename__ = "gw2_profit_transactions"
    __table_args__ = (
        Index(
            "idx_gw2_profit_transactions_lookup",
            "discord_user_id",
            "transaction_kind",
            "occurred_at",
        ),
    )

    discord_user_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    transaction_kind: Mapped[str] = mapped_column(String, primary_key=True)
    transaction_id: Mapped[str] = mapped_column(String, primary_key=True)
    item_id: Mapped[int] = mapped_column(Integer, nullable=False)
    price: Mapped[int] = mapped_column(Integer, nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    occurred_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)


class ProfitItemRecord(Base):
    """Shared item names fetched from the public Guild Wars 2 endpoint."""

    __tablename__ = "gw2_profit_items"

    item_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)


class ProfitPreferenceRecord(Base):
    """One member's remembered choices on the profit dashboard.

    The report window used to live only in the page's URL, so a member who
    opened /profit without one was put back on the default window. It is kept
    here instead, beside the excluded items below, so the dashboard opens the
    way the member left it on any browser they sign in from.
    """

    __tablename__ = "gw2_profit_preferences"

    discord_user_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    report_days: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)


class ProfitOrderExclusionRecord(Base):
    """One item a member left out of their Open Orders table."""

    __tablename__ = "gw2_profit_order_exclusions"

    discord_user_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    item_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[str] = mapped_column(String, nullable=False)


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


class FeastStockLogRecord(Base):
    __tablename__ = "feast_stock_log"

    log_id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )
    guild_storage_id: Mapped[int] = mapped_column(Integer, nullable=False)
    count: Mapped[int] = mapped_column(Integer, nullable=False)
    recorded_at: Mapped[float] = mapped_column(Float, nullable=False)


class GuildMemberCountLogRecord(Base):
    """One observed guild member count, written only when it changes.

    The roster page draws its line from the join and leave events already
    stored, but those only say how the count moved; this says where it
    actually stood. The newest row is the anchor every derived count is
    measured from, so a stretch of missed events shifts the history rather
    than the present.
    """

    __tablename__ = "guild_member_count_log"

    log_id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )
    member_count: Mapped[int] = mapped_column(Integer, nullable=False)
    pending_invite_count: Mapped[int] = mapped_column(Integer, nullable=False)
    recorded_at: Mapped[float] = mapped_column(Float, nullable=False)


class GuildStashCoinLogRecord(Base):
    """One movement of coins in or out of the guild stash.

    The bank ledger the /gold page draws, and deliberately not the raffle's
    deposit table: that one is filtered by the raffle's own rules - an
    oversized Officer deposit earns no tickets and is never written there - so
    a balance derived from it would drift away from the guild's real one. This
    table records every coin movement the guild log reports, whatever the
    raffle made of it.

    ``event_id`` is the guild log's own id, which is what makes both the
    poller and the one-time import idempotent against each other: whichever
    sees an event first writes the row, and the other finds it already there.
    """

    __tablename__ = "guild_stash_coin_log"

    event_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String, nullable=False)
    # "deposit" or "withdraw"; see gw2bot.raffle.events.
    operation: Mapped[str] = mapped_column(String, nullable=False)
    coins: Mapped[int] = mapped_column(Integer, nullable=False)
    event_time: Mapped[str] = mapped_column(String, nullable=False)
    # Only a withdrawal is announced, so a deposit's row is written already
    # marked sent - the raffle's own deposit embed is that announcement.
    notification_sent: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
    )


class GuildStashBalanceLogRecord(Base):
    """One observed guild stash coin balance, written only when it changes.

    The /gold page derives its line from the coin movements above, but those
    only say how the balance moved; this says where it actually stood. The
    newest row is the anchor every derived balance is measured from, which is
    what keeps a stretch of events the guild log dropped from shifting the
    present rather than the past.
    """

    __tablename__ = "guild_stash_balance_log"

    log_id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )
    coins: Mapped[int] = mapped_column(Integer, nullable=False)
    recorded_at: Mapped[float] = mapped_column(Float, nullable=False)


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
    # Comma-separated Discord role ids pinged when an occurrence is posted.
    ping_role_ids: Mapped[str] = mapped_column(
        String,
        nullable=False,
        default="",
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
    # The announcement that pinged this occurrence's roles from outside the
    # forum post it was sent into, so the same lifecycle that removes the
    # event's message removes the announcement pointing at it. Both are NULL
    # for an occurrence posted to a channel, and for one whose announcement
    # was never delivered. The channel is stored alongside the message
    # because the ping channel setting can move between two occurrences of
    # one series.
    ping_channel_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ping_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # The roles that announcement actually mentioned. Editing a message does
    # not notify a mention added to it, so a later correction has to keep the
    # ones that were delivered rather than re-render the event's current pick
    # - which would both claim roles that were never alerted and lose the
    # record of who was.
    ping_role_ids: Mapped[str] = mapped_column(
        String,
        nullable=False,
        default="",
    )
    # Announcements from earlier posts of this occurrence that still have to be
    # removed, kept because a channel move overwrites the pair above with the
    # replacement's ids. Without them a deletion Discord refused once would be
    # forgotten, and the announcement would outlive the message it links to.
    # A list rather than one pair: an occurrence moved again before an earlier
    # removal succeeds owes both, and a single slot would drop the older one.
    # Stored as "channel:message" entries for the same reason ping_role_ids is
    # a string - the row holds a handful of ids, not a table of its own.
    stale_ping_messages: Mapped[str] = mapped_column(
        String,
        nullable=False,
        default="",
    )
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
    # "Edit my signup" token bucket. edit_tokens_updated_at is NULL until the
    # first edit, which reads as a full bucket.
    edit_tokens: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        default=3.0,
    )
    edit_tokens_updated_at: Mapped[str | None] = mapped_column(
        String,
        nullable=True,
    )


class EventReminderRecord(Base):
    __tablename__ = "gw2_event_reminders"

    # One row per reminder that has been handled for an occurrence, so a restart
    # or a second maintenance pass never pings the same roster twice. A row is
    # also written for a reminder that was deliberately not sent (nobody to
    # ping, no thread to ping in, or its moment lapsed during downtime): the
    # question the row answers is "is this reminder still outstanding?", and in
    # each of those cases it is not.
    occurrence_id: Mapped[int] = mapped_column(
        ForeignKey("gw2_event_occurrences.occurrence_id"),
        primary_key=True,
    )
    offset_minutes: Mapped[int] = mapped_column(Integer, primary_key=True)
    handled_at: Mapped[str] = mapped_column(String, nullable=False)


class EventSignupPreferenceRecord(Base):
    __tablename__ = "gw2_event_signup_preferences"

    # Remembered roles are per event, like automatic sign-up: the role someone
    # plays in a raid says nothing about the role they want in a fractal, so a
    # member signing up for an event they have never joined is asked afresh.
    event_id: Mapped[int] = mapped_column(
        ForeignKey("gw2_events.event_id"),
        primary_key=True,
    )
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
        if "gold_raffle_tickets" in added_columns:
            # Splitting the legacy raffle_tickets total into gold and manual
            # halves is RaffleStore's job, but only this call knows the column
            # was just added - and any store that opens the database first
            # consumes that signal. Record it so whichever store does the
            # migration still finds out, and so a database migrated by an
            # earlier release is never re-split.
            connection.exec_driver_sql(
                "INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)",
                (PENDING_LEGACY_TOTALS_KEY, "1"),
            )

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

        for column_name in (
            "ping_channel_id",
            "ping_message_id",
        ):
            if column_name in occurrence_columns:
                continue
            # Nothing to backfill: no release before this one sent an
            # announcement, so every legacy row correctly reads as having
            # none to clean up.
            operations.add_column(
                EventOccurrenceRecord.__tablename__,
                Column(column_name, Integer, nullable=True),
            )
            added_columns.add(column_name)

        if "ping_role_ids" not in occurrence_columns:
            operations.add_column(
                EventOccurrenceRecord.__tablename__,
                Column(
                    "ping_role_ids",
                    String,
                    nullable=False,
                    server_default="",
                ),
            )
            added_columns.add("occurrence_ping_role_ids")

        if "stale_ping_messages" not in occurrence_columns:
            # Nothing to backfill, as above: no release before this one sent an
            # announcement, so no legacy row owes a removal.
            operations.add_column(
                EventOccurrenceRecord.__tablename__,
                Column(
                    "stale_ping_messages",
                    String,
                    nullable=False,
                    server_default="",
                ),
            )
            added_columns.add("stale_ping_messages")

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

        if "ping_role_ids" not in event_columns:
            # Events created before role pinging existed ping nobody, which is
            # exactly what the empty default records.
            operations.add_column(
                EventRecord.__tablename__,
                Column(
                    "ping_role_ids",
                    String,
                    nullable=False,
                    server_default="",
                ),
            )
            added_columns.add("ping_role_ids")

        signup_columns = {
            column["name"]
            for column in inspect(connection).get_columns(
                EventSignupRecord.__tablename__
            )
        }
        if "edit_tokens" not in signup_columns:
            operations.add_column(
                EventSignupRecord.__tablename__,
                Column(
                    "edit_tokens",
                    Float,
                    nullable=False,
                    server_default="3.0",
                ),
            )
            added_columns.add("edit_tokens")
        if "edit_tokens_updated_at" not in signup_columns:
            operations.add_column(
                EventSignupRecord.__tablename__,
                Column("edit_tokens_updated_at", String, nullable=True),
            )
            added_columns.add("edit_tokens_updated_at")

        preference_columns = {
            column["name"]
            for column in inspect(connection).get_columns(
                EventSignupPreferenceRecord.__tablename__
            )
        }
        if "event_id" not in preference_columns:
            # Legacy remembered roles were one global row per member. They are
            # now keyed by event as well, which SQLite cannot express as an
            # added column: the primary key changes, so the table is rebuilt.
            # Legacy rows are fanned out over the events the member has
            # already signed up for - exactly the events whose roles they had
            # been asked about - so their memory survives where it was earned
            # and every other event asks them once.
            legacy_table = f"{EventSignupPreferenceRecord.__tablename__}_legacy"
            operations.rename_table(
                EventSignupPreferenceRecord.__tablename__,
                legacy_table,
            )
            # The metadata lookup, rather than the mapped class's __table__,
            # is the typed route to the Table this recreates.
            Base.metadata.tables[
                EventSignupPreferenceRecord.__tablename__
            ].create(connection)
            connection.exec_driver_sql(
                "INSERT INTO gw2_event_signup_preferences "
                "(event_id, discord_user_id, role, flex_roles, mode) "
                "SELECT DISTINCT occurrences.event_id, "
                "legacy.discord_user_id, legacy.role, legacy.flex_roles, "
                "legacy.mode "
                f"FROM {legacy_table} AS legacy "
                "JOIN gw2_event_signups AS signups "
                "ON signups.discord_user_id = legacy.discord_user_id "
                "JOIN gw2_event_occurrences AS occurrences "
                "ON occurrences.occurrence_id = signups.occurrence_id"
            )
            operations.drop_table(legacy_table)
            added_columns.add("signup_preference_event_id")

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
