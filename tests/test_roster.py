import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import discord
import pytest
from sqlalchemy.exc import SQLAlchemyError

from factories import (
    guild_join,
    guild_kick,
    guild_leave,
    settings_interaction,
    settings_reply,
)
from gw2bot.config import DEFAULT_RAFFLE_OFFICER_ROLE_ID, Config
from gw2bot.raffle import RaffleStore
from gw2bot.raffle.models import GuildInvite, GuildJoin, GuildLeave, GuildRankChange
from gw2bot.roster import (
    JOIN,
    KICK,
    LEAVE,
    ROSTER_RANGES,
    ImportedMembershipEvent,
    MemberCountSample,
    MembershipEvent,
    build_roster_series,
    parse_membership_message,
)
from gw2bot.roster.commands import RosterCommands
from gw2bot.roster.import_log import (
    format_import_result,
    import_membership_history,
)

BOT_USER_ID = 4242
NOW = 1_800_000_000.0


@pytest.fixture
def store(tmp_path: Path):
    store = RaffleStore(str(tmp_path / "gw2bot.db"), "guild-id")
    yield store
    store.close()


def count_log_rows(tmp_path: Path) -> int:
    """How many rows the member count log holds, read outside the store.

    The store deliberately offers no way to count them - nothing in the bot
    needs to - so the claim that an unchanged poll adds no row is checked
    against the database itself.
    """
    with sqlite3.connect(tmp_path / "gw2bot.db") as connection:
        return connection.execute(
            "SELECT COUNT(*) FROM guild_member_count_log"
        ).fetchone()[0]


def event(
    offset: float,
    kind: str = JOIN,
    username: str = "Member.1234",
    actor: str | None = None,
    imported: bool = False,
) -> MembershipEvent:
    return MembershipEvent(
        occurred_at=NOW + offset,
        kind=kind,
        username=username,
        actor=actor,
        imported=imported,
    )


def anchor(offset: float, member_count: int) -> MemberCountSample:
    return MemberCountSample(
        recorded_at=NOW + offset,
        member_count=member_count,
        pending_invite_count=0,
    )


class TestParseMembershipMessage:
    def test_reads_the_messages_the_bot_posts(self) -> None:
        # Parsed straight off the models that write them, so the two cannot
        # drift apart without this failing.
        join = GuildJoin(event_id=1, username="New.1234", event_time="")
        left = GuildLeave(event_id=2, username="Gone.5678", event_time="")
        kicked = GuildLeave(
            event_id=3,
            username="Removed.9012",
            event_time="",
            kicked_by="Officer.3456",
        )

        assert parse_membership_message(join.message) == (
            JOIN,
            "New.1234",
            None,
        )
        assert parse_membership_message(left.message) == (
            LEAVE,
            "Gone.5678",
            None,
        )
        assert parse_membership_message(kicked.message) == (
            KICK,
            "Removed.9012",
            "Officer.3456",
        )

    def test_ignores_the_other_membership_messages(self) -> None:
        # An invitation does not change the member count - the GW2 roster
        # reports an invited account separately - and neither does a rank
        # change, so neither belongs on the graph.
        invite = GuildInvite(
            event_id=1,
            username="Asked.1234",
            event_time="",
            invited_by="Officer.5678",
        )
        rank = GuildRankChange(
            event_id=2,
            username="Member.1234",
            old_rank="Trial",
            new_rank="Sunborne",
            event_time="",
        )

        assert parse_membership_message(invite.message) is None
        assert parse_membership_message(rank.message) is None

    def test_ignores_diagnostic_previews(self) -> None:
        preview = (
            "**Guild join notification (test)**\n"
            + GuildJoin(
                event_id=0,
                username="DiagnosticUser.1234",
                event_time="",
            ).message
        )

        assert parse_membership_message(preview) is None

    def test_ignores_a_single_line_preview_marked_as_a_test(self) -> None:
        # The header and the body arrive as one message today, which the
        # whole-message match already rules out. The marker is what keeps a
        # preview out if that ever changes.
        assert (
            parse_membership_message(
                "DiagnosticUser.1234 has joined the guild. (test)"
            )
            is None
        )

    @pytest.mark.parametrize(
        "content",
        [
            "",
            "   ",
            "Guild Storage is low on **Bowl of Fruit Salad**: 4 left",
            "Someone typed this in the channel",
            "Member.1234 has joined the guild",
        ],
    )
    def test_ignores_everything_else_in_the_channel(self, content: str) -> None:
        assert parse_membership_message(content) is None


class TestBuildRosterSeries:
    def test_counts_are_measured_backwards_from_the_anchor(self) -> None:
        series = build_roster_series(
            [event(-300, JOIN), event(-200, LEAVE)],
            anchor(-100, 10),
            NOW - 400,
            NOW,
        )

        assert [(change.kind, change.member_count) for change in series.events] == [
            (JOIN, 11),
            (LEAVE, 10),
        ]

    def test_counts_are_measured_forwards_past_the_anchor(self) -> None:
        series = build_roster_series(
            [event(-100, JOIN), event(-50, KICK)],
            anchor(-200, 10),
            NOW - 400,
            NOW,
        )

        assert [change.member_count for change in series.events] == [11, 10]

    def test_the_line_spans_the_whole_window(self) -> None:
        series = build_roster_series(
            [event(-200, JOIN)],
            anchor(-100, 10),
            NOW - 400,
            NOW,
        )

        assert [(point.at, point.member_count) for point in series.points] == [
            (NOW - 400, 9),
            (NOW - 200, 10),
            (NOW, 10),
        ]

    def test_a_quiet_window_is_a_flat_line_at_the_current_count(self) -> None:
        series = build_roster_series([], anchor(-100, 400), NOW - 400, NOW)

        assert [point.member_count for point in series.points] == [400, 400]
        assert series.events == ()

    def test_changes_before_the_window_still_place_its_starting_count(
        self,
    ) -> None:
        # The window opens after two departures the graph does not show, so
        # its left edge has to sit two members above where the anchor stands.
        series = build_roster_series(
            [event(-900, LEAVE), event(-800, LEAVE)],
            anchor(-100, 10),
            NOW - 400,
            NOW,
        )

        assert series.events == ()
        assert [point.member_count for point in series.points] == [10, 10]

    def test_a_change_at_the_window_edge_is_shown_once(self) -> None:
        series = build_roster_series(
            [event(-400, JOIN)],
            anchor(-100, 10),
            NOW - 400,
            NOW,
        )

        assert len(series.events) == 1
        # The leading vertex is the count entering the window, so it sits
        # below the point the join takes it to rather than duplicating it.
        assert [point.member_count for point in series.points] == [9, 10, 10]

    def test_without_an_observed_count_nothing_is_placed_on_the_axis(
        self,
    ) -> None:
        series = build_roster_series(
            [event(-200, JOIN), event(-100, KICK, actor="Officer.5678")],
            None,
            NOW - 400,
            NOW,
        )

        assert series.points == ()
        assert [change.member_count for change in series.events] == [None, None]
        assert series.events[1].actor == "Officer.5678"

    def test_changes_after_now_are_ignored(self) -> None:
        series = build_roster_series(
            [event(-100, JOIN), event(100, JOIN)],
            anchor(-200, 10),
            NOW - 400,
            NOW,
        )

        assert len(series.events) == 1
        assert series.points[-1].member_count == 11

    def test_counts_each_kind_of_change(self) -> None:
        series = build_roster_series(
            [
                event(-300, JOIN),
                event(-250, JOIN),
                event(-200, LEAVE),
                event(-100, KICK, actor="Officer.5678"),
            ],
            anchor(-50, 10),
            NOW - 400,
            NOW,
        )

        assert (series.joins, series.leaves, series.kicks) == (2, 1, 1)


class TestMemberCountLog:
    def test_records_only_a_changed_count(self, store: RaffleStore) -> None:
        assert store.record_member_count(400, 3, NOW)
        assert not store.record_member_count(400, 4, NOW + 60)
        assert store.record_member_count(401, 4, NOW + 120)

        latest = store.get_last_member_count()
        assert latest is not None
        assert (latest.member_count, latest.recorded_at) == (401, NOW + 120)

    def test_an_unchanged_count_moves_the_anchor_without_adding_a_row(
        self,
        store: RaffleStore,
        tmp_path: Path,
    ) -> None:
        store.record_member_count(400, 3, NOW)

        assert not store.record_member_count(400, 5, NOW + 60)

        latest = store.get_last_member_count()
        assert latest is not None
        assert (
            latest.member_count,
            latest.pending_invite_count,
            latest.recorded_at,
        ) == (400, 5, NOW + 60)
        assert count_log_rows(tmp_path) == 1

    def test_the_anchor_never_moves_backwards(
        self,
        store: RaffleStore,
    ) -> None:
        # A clock stepped back would otherwise drag the anchor into the past
        # and re-open the gap the refresh exists to close.
        store.record_member_count(400, 3, NOW)

        assert not store.record_member_count(400, 4, NOW - 600)

        latest = store.get_last_member_count()
        assert latest is not None
        assert (latest.recorded_at, latest.pending_invite_count) == (NOW, 3)

    def test_reports_no_sample_before_the_first_poll(
        self,
        store: RaffleStore,
    ) -> None:
        assert store.get_last_member_count() is None


class TestMembershipEventStore:
    def test_reads_polled_joins_leaves_and_kicks(
        self,
        store: RaffleStore,
    ) -> None:
        store.initialize_cursor(0)
        store.process_events(
            [
                guild_join(1, "Joined.1234"),
                guild_leave(2, "Left.5678"),
                guild_kick(3, "Removed.9012", "Officer.3456"),
            ]
        )

        events = store.get_membership_events(0.0)

        assert {(item.kind, item.username, item.actor) for item in events} == {
            (JOIN, "Joined.1234", None),
            (LEAVE, "Left.5678", None),
            (KICK, "Removed.9012", "Officer.3456"),
        }
        assert not any(item.imported for item in events)

    def test_drops_changes_before_the_window(self, store: RaffleStore) -> None:
        store.import_membership_events(
            [
                ImportedMembershipEvent(11, NOW - 500, JOIN, "Old.1234"),
                ImportedMembershipEvent(12, NOW - 100, JOIN, "Recent.5678"),
            ]
        )

        events = store.get_membership_events(NOW - 300)

        assert [item.username for item in events] == ["Recent.5678"]

    def test_imported_changes_are_marked_as_such(
        self,
        store: RaffleStore,
    ) -> None:
        store.import_membership_events(
            [
                ImportedMembershipEvent(
                    21,
                    NOW - 100,
                    KICK,
                    "Removed.9012",
                    "Officer.3456",
                )
            ]
        )

        events = store.get_membership_events(0.0)

        assert len(events) == 1
        assert events[0].imported
        assert events[0].kind == KICK
        assert events[0].actor == "Officer.3456"

    def test_importing_the_same_messages_twice_adds_nothing(
        self,
        store: RaffleStore,
    ) -> None:
        offered = [
            ImportedMembershipEvent(31, NOW - 200, JOIN, "One.1234"),
            ImportedMembershipEvent(32, NOW - 100, LEAVE, "Two.5678"),
        ]

        assert store.import_membership_events(offered) == 2
        assert store.import_membership_events(offered) == 0
        assert len(store.get_membership_events(0.0)) == 2

    def test_imported_changes_never_queue_a_notification(
        self,
        store: RaffleStore,
    ) -> None:
        # The message being read is the notification, so posting it again
        # would replay years of departures into the channel.
        store.import_membership_events(
            [
                ImportedMembershipEvent(41, NOW - 200, JOIN, "One.1234"),
                ImportedMembershipEvent(42, NOW - 100, LEAVE, "Two.5678"),
            ]
        )

        assert store.get_pending_join_notifications() == []
        assert store.get_pending_leave_notifications() == []

    def test_the_import_boundary_ignores_imported_rows(
        self,
        store: RaffleStore,
    ) -> None:
        store.import_membership_events(
            [ImportedMembershipEvent(51, NOW - 900, JOIN, "Old.1234")]
        )
        assert store.get_earliest_polled_membership_time() is None

        store.initialize_cursor(0)
        store.process_events([guild_join(1, "Polled.1234")])

        polled = store.get_earliest_polled_membership_time()
        assert polled == datetime(
            2026, 6, 7, 6, 26, 17, tzinfo=UTC
        ).timestamp()


class FakeChannel:
    def __init__(self, messages: list[Any], failure: Exception | None = None):
        self._messages = messages
        self._failure = failure

    def history(self, limit: int | None = None, oldest_first: bool = False):
        messages = self._messages if oldest_first else list(
            reversed(self._messages)
        )
        failure = self._failure

        async def iterator():
            for message in messages:
                yield message
            if failure is not None:
                raise failure

        return iterator()


def message(
    message_id: int,
    content: str,
    *,
    offset_seconds: float = 0.0,
    author_id: int = BOT_USER_ID,
) -> Any:
    return SimpleNamespace(
        id=message_id,
        content=content,
        author=SimpleNamespace(id=author_id),
        created_at=datetime.fromtimestamp(NOW, UTC)
        + timedelta(seconds=offset_seconds),
    )


def import_bot(store: RaffleStore, **attributes: Any) -> Any:
    return SimpleNamespace(
        user=SimpleNamespace(id=BOT_USER_ID),
        raffle_store=store,
        **attributes,
    )


class TestImportMembershipHistory:
    async def test_imports_the_membership_messages_it_finds(
        self,
        store: RaffleStore,
    ) -> None:
        channel = FakeChannel(
            [
                message(1, "One.1234 has joined the guild.", offset_seconds=-500),
                message(2, "Two.5678 has left the guild.", offset_seconds=-400),
                message(
                    3,
                    "Officer.3456 kicked Three.9012 from the guild.",
                    offset_seconds=-300,
                ),
            ]
        )
        bot = import_bot(store)

        result = await import_membership_history(bot, channel)

        assert (result.scanned, result.matched, result.imported) == (3, 3, 3)
        assert result.completed
        events = store.get_membership_events(0.0)
        assert [(item.kind, item.username) for item in events] == [
            (JOIN, "One.1234"),
            (LEAVE, "Two.5678"),
            (KICK, "Three.9012"),
        ]

    async def test_ignores_messages_other_authors_wrote(
        self,
        store: RaffleStore,
    ) -> None:
        channel = FakeChannel(
            [
                message(
                    1,
                    "Impostor.1234 has joined the guild.",
                    author_id=BOT_USER_ID + 1,
                ),
                message(2, "Real.5678 has joined the guild."),
            ]
        )

        result = await import_membership_history(import_bot(store), channel)

        assert (result.matched, result.imported) == (1, 1)
        assert [
            item.username for item in store.get_membership_events(0.0)
        ] == ["Real.5678"]

    async def test_ignores_diagnostic_previews_and_other_traffic(
        self,
        store: RaffleStore,
    ) -> None:
        channel = FakeChannel(
            [
                message(
                    1,
                    "**Guild join notification (test)**\n"
                    "DiagnosticUser.1234 has joined the guild.",
                ),
                message(
                    2,
                    "**Guild leave notification (test)**\n"
                    "DiagnosticUser.1234 has left the guild.",
                ),
                message(3, "Officer.5678 invited Asked.1234 to the guild."),
                message(4, "Guild Storage is low on **Steak**: 4 left"),
            ]
        )

        result = await import_membership_history(import_bot(store), channel)

        assert (result.scanned, result.matched, result.imported) == (4, 0, 0)
        assert store.get_membership_events(0.0) == []

    async def test_stops_where_the_guild_log_poller_took_over(
        self,
        store: RaffleStore,
    ) -> None:
        store.initialize_cursor(0)
        store.process_events([guild_join(1, "Polled.1234")])
        boundary = store.get_earliest_polled_membership_time()
        assert boundary is not None
        channel = FakeChannel(
            [
                message(
                    1,
                    "Older.1234 has joined the guild.",
                    offset_seconds=boundary - NOW - 60,
                ),
                message(
                    2,
                    "Polled.1234 has joined the guild.",
                    offset_seconds=boundary - NOW + 30,
                ),
            ]
        )

        result = await import_membership_history(import_bot(store), channel)

        assert (result.matched, result.imported, result.skipped_recent) == (
            2,
            1,
            1,
        )
        # The polled join is recorded once, from the guild log, not twice.
        assert sorted(
            item.username for item in store.get_membership_events(0.0)
        ) == ["Older.1234", "Polled.1234"]

    async def test_keeps_what_it_read_when_discord_stops_answering(
        self,
        store: RaffleStore,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        channel = FakeChannel(
            [message(1, "One.1234 has joined the guild.")],
            failure=discord.DiscordException(),
        )

        with caplog.at_level(logging.ERROR, logger="gw2bot"):
            result = await import_membership_history(import_bot(store), channel)

        assert not result.completed
        assert result.imported == 1
        assert "Could not finish reading the log channel" in caplog.text

    async def test_reports_what_it_did(self, store: RaffleStore) -> None:
        channel = FakeChannel(
            [message(1, "One.1234 has joined the guild.")]
        )
        bot = import_bot(store)

        await import_membership_history(bot, channel)
        again = await import_membership_history(bot, channel)

        reply = format_import_result(again)
        assert "Imported 0 change(s)" in reply
        assert "1 were already imported" in reply

    async def test_logging_names_no_account(
        self,
        store: RaffleStore,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        channel = FakeChannel(
            [
                message(1, "Secret.1234 has joined the guild."),
                message(2, "Officer.5678 kicked Private.9012 from the guild."),
            ]
        )

        with caplog.at_level(logging.DEBUG, logger="gw2bot"):
            await import_membership_history(import_bot(store), channel)

        for name in ("Secret.1234", "Officer.5678", "Private.9012"):
            assert name not in caplog.text


class RosterCommandBot:
    def __init__(self, store: RaffleStore, channel: Any, **overrides: Any):
        self._config = Config(
            discord_token="discord-token",
            discord_command_guild_id=5678,
            **overrides,
        )
        self.user = SimpleNamespace(id=BOT_USER_ID)
        self.raffle_store = store
        self._channel = channel

    async def _get_notification_channel(self) -> Any:
        if isinstance(self._channel, Exception):
            raise self._channel
        return self._channel


def roster_commands(bot: Any) -> RosterCommands:
    from gw2bot.bot import Gw2Bot

    return RosterCommands(cast(Gw2Bot, bot))


class TestRosterImportCommand:
    async def test_rejects_a_caller_without_the_officer_role(
        self,
        store: RaffleStore,
    ) -> None:
        bot = RosterCommandBot(
            store,
            FakeChannel([]),
            discord_notification_channel_id=9012,
        )
        interaction = settings_interaction()

        await roster_commands(bot)._handle_import(interaction)

        assert "do not have the required role" in settings_reply(interaction)
        assert store.get_membership_events(0.0) == []

    async def test_reports_an_unset_notification_channel(
        self,
        store: RaffleStore,
    ) -> None:
        bot = RosterCommandBot(store, FakeChannel([]))
        interaction = settings_interaction(
            role_ids=(DEFAULT_RAFFLE_OFFICER_ROLE_ID,)
        )

        await roster_commands(bot)._handle_import(interaction)

        assert (
            "/settings discord_notification_channel_id"
            in settings_reply(interaction)
        )

    async def test_reports_a_channel_discord_will_not_open(
        self,
        store: RaffleStore,
    ) -> None:
        bot = RosterCommandBot(
            store,
            discord.DiscordException(),
            discord_notification_channel_id=9012,
        )
        interaction = settings_interaction(
            role_ids=(DEFAULT_RAFFLE_OFFICER_ROLE_ID,)
        )

        await roster_commands(bot)._handle_import(interaction)

        assert "log channel could not be opened" in settings_reply(interaction)

    async def test_imports_and_reports_the_result(
        self,
        store: RaffleStore,
    ) -> None:
        channel = FakeChannel(
            [
                message(1, "One.1234 has joined the guild."),
                message(2, "Two.5678 has left the guild."),
            ]
        )
        bot = RosterCommandBot(
            store,
            channel,
            discord_notification_channel_id=9012,
        )
        interaction = settings_interaction(
            role_ids=(DEFAULT_RAFFLE_OFFICER_ROLE_ID,)
        )

        await roster_commands(bot)._handle_import(interaction)

        assert "Imported 2 change(s)" in settings_reply(interaction)
        assert len(store.get_membership_events(0.0)) == 2

    async def test_reports_a_database_failure_without_raising(
        self,
        store: RaffleStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def explode(events: Any) -> int:
            raise SQLAlchemyError("disk gone")

        monkeypatch.setattr(store, "import_membership_events", explode)
        bot = RosterCommandBot(
            store,
            FakeChannel([message(1, "One.1234 has joined the guild.")]),
            discord_notification_channel_id=9012,
        )
        interaction = settings_interaction(
            role_ids=(DEFAULT_RAFFLE_OFFICER_ROLE_ID,)
        )

        await roster_commands(bot)._handle_import(interaction)

        assert "could not be written" in settings_reply(interaction)


class TestRosterSeriesAgainstStoredHistory:
    def test_an_imported_history_reads_back_as_a_line(
        self,
        store: RaffleStore,
    ) -> None:
        # What the operator actually does: import the channel, then let the
        # member count poller log where the roster stands. The graph walks
        # back from that count through the imported changes.
        store.import_membership_events(
            [
                ImportedMembershipEvent(1, NOW - 3000, JOIN, "One.1234"),
                ImportedMembershipEvent(2, NOW - 2000, LEAVE, "Two.5678"),
                ImportedMembershipEvent(
                    3,
                    NOW - 1000,
                    KICK,
                    "Three.9012",
                    "Officer.3456",
                ),
            ]
        )
        store.record_member_count(400, 2, NOW - 500)

        series = build_roster_series(
            store.get_membership_events(NOW - 4000),
            store.get_last_member_count(),
            NOW - 4000,
            NOW,
        )

        assert [change.member_count for change in series.events] == [
            402,
            401,
            400,
        ]
        assert series.points[0].member_count == 401
        assert series.points[-1].member_count == 400

    def test_a_gap_in_the_guild_log_falls_behind_a_fresh_observation(
        self,
        store: RaffleStore,
    ) -> None:
        # The guild log keeps only about a hundred events per type, so an
        # outage can drop a departure for good. Here a leave was lost and the
        # join that followed it was captured, leaving the roster back where it
        # started. The next poll observes that unchanged count and moves the
        # anchor past the gap, so the captured join is measured from before it
        # rather than replayed on top of it.
        store.record_member_count(400, 0, NOW - 3000)
        store.import_membership_events(
            [ImportedMembershipEvent(1, NOW - 2000, JOIN, "Back.1234")]
        )

        store.record_member_count(400, 0, NOW - 100)

        series = build_roster_series(
            store.get_membership_events(NOW - 4000),
            store.get_last_member_count(),
            NOW - 4000,
            NOW,
        )

        assert series.points[-1].member_count == 400
        assert [change.member_count for change in series.events] == [400]


class TestRosterRanges:
    def test_offers_a_day_a_week_and_a_month(self) -> None:
        assert ROSTER_RANGES == {
            "24h": 24 * 60 * 60,
            "7d": 7 * 24 * 60 * 60,
            "30d": 30 * 24 * 60 * 60,
        }
