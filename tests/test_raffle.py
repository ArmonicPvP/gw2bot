import logging
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import (
    Boolean,
    Column,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
    func,
    select,
)

from gw2bot.raffle import (
    RAFFLE_DRAW_TIERS,
    RAFFLE_REWARD_TIERS,
    RaffleDrawTier,
    RaffleRewardTier,
    RaffleStore,
    format_gold,
    parse_gold_deposit,
    parse_guild_join,
    parse_guild_leave,
)
import pytest


def gold_deposit(
    event_id: int,
    username: str = "Username.1234",
    coins: int = 10_000,
    event_time: str = "2026-06-07T06:26:17.000Z",
) -> dict[str, object]:
    return {
        "id": event_id,
        "time": event_time,
        "type": "stash",
        "user": username,
        "operation": "deposit",
        "coins": coins,
        "item_id": 0,
        "count": 0,
    }


def guild_leave(
    event_id: int,
    username: str = "Username.1234",
) -> dict[str, object]:
    return {
        "id": event_id,
        "time": "2026-06-07T06:26:17.000Z",
        "type": "kick",
        "user": username,
        "kicked_by": username,
    }


def guild_kick(
    event_id: int,
    username: str = "Kicked.1234",
    kicked_by: str = "Officer.5678",
) -> dict[str, object]:
    return {
        "id": event_id,
        "time": "2026-06-07T06:26:17.000Z",
        "type": "kick",
        "user": username,
        "kicked_by": kicked_by,
    }


def guild_join(
    event_id: int,
    username: str = "Username.1234",
) -> dict[str, object]:
    return {
        "id": event_id,
        "time": "2026-06-07T06:26:17.000Z",
        "type": "joined",
        "user": username,
    }


class TestRaffle:
    def test_default_reward_tiers_are_data_driven(self) -> None:
        assert [
            (tier.threshold, tier.name) for tier in RAFFLE_REWARD_TIERS
        ] == [
            (50, "Tier 1"),
            (100, "Tier 2"),
            (150, "Tier 3"),
            (200, "Tier 4"),
        ]
        assert [
            (tier.minimum_purchased_tickets, tier.winner_count)
            for tier in RAFFLE_DRAW_TIERS
        ] == [
            (0, 2),
            (50, 2),
            (100, 3),
            (150, 4),
            (200, 5),
        ]

    def test_parses_gold_deposit_and_formats_message(self) -> None:
        deposit = parse_gold_deposit(gold_deposit(101, coins=35_000))

        assert deposit is not None
        assert deposit.raffle_tickets == 3
        assert (
            deposit.message
            == "Username.1234 deposited 3.5 gold and purchased 3 raffle tickets"
        )

    def test_tracks_partial_gold_but_ignores_non_deposit_events(self) -> None:
        partial = parse_gold_deposit(gold_deposit(101, coins=9_999))
        assert partial is not None
        assert partial.raffle_tickets == 0
        assert (
            parse_gold_deposit({**gold_deposit(102), "operation": "withdraw"}) is None
        )
        assert parse_gold_deposit({**gold_deposit(103), "type": "treasury"}) is None

    def test_parses_guild_leave_with_exact_message(self) -> None:
        leave = parse_guild_leave(guild_leave(104))

        assert leave is not None
        assert leave.message == "Username.1234 has left the guild."
        assert parse_guild_leave({**guild_leave(105), "type": "joined"}) is None

    def test_parses_guild_kick_with_exact_message(self) -> None:
        leave = parse_guild_leave(guild_kick(106))

        assert leave is not None
        assert leave.message == "Officer.5678 kicked Kicked.1234 from the guild."

    def test_parses_guild_join_with_exact_message(self) -> None:
        join = parse_guild_join(guild_join(104))

        assert join is not None
        assert join.message == "Username.1234 has joined the guild."
        assert parse_guild_join({**guild_join(105), "type": "invited"}) is None
        assert parse_guild_join({**guild_join(106), "user": ""}) is None

    def test_persists_leave_notification_and_prevents_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = str(Path(directory) / "raffle.db")
            store = RaffleStore(database_path, "guild-id")
            store.initialize_cursor(100)
            store.process_events([guild_leave(101)])

            assert [
                leave.message for leave in store.get_pending_leave_notifications()
            ] == ["Username.1234 has left the guild."]
            store.close()

            reopened = RaffleStore(database_path, "guild-id")
            reopened.process_events([guild_leave(101)])
            pending = reopened.get_pending_leave_notifications()
            assert len(pending) == 1
            reopened.mark_leave_notification_sent(101)
            assert reopened.get_pending_leave_notifications() == []
            reopened.close()

    def test_persists_kick_notification_and_prevents_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = str(Path(directory) / "raffle.db")
            store = RaffleStore(database_path, "guild-id")
            store.initialize_cursor(100)
            store.process_events([guild_kick(101)])

            assert [
                leave.message for leave in store.get_pending_leave_notifications()
            ] == ["Officer.5678 kicked Kicked.1234 from the guild."]
            store.close()

            reopened = RaffleStore(database_path, "guild-id")
            reopened.process_events([guild_kick(101)])
            pending = reopened.get_pending_leave_notifications()
            assert len(pending) == 1
            reopened.mark_leave_notification_sent(101)
            assert reopened.get_pending_leave_notifications() == []
            reopened.close()

    def test_repeated_join_leave_cycles_create_distinct_notifications(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RaffleStore(str(Path(directory) / "raffle.db"), "guild-id")
            store.initialize_cursor(100)

            store.process_events(
                [
                    guild_leave(101),
                    guild_join(102),
                    guild_leave(103),
                    guild_join(104),
                ]
            )

            assert [
                join.event_id for join in store.get_pending_join_notifications()
            ] == [102, 104]
            assert [
                join.message for join in store.get_pending_join_notifications()
            ] == [
                "Username.1234 has joined the guild.",
                "Username.1234 has joined the guild.",
            ]
            assert [
                leave.event_id for leave in store.get_pending_leave_notifications()
            ] == [101, 103]
            assert [
                leave.message for leave in store.get_pending_leave_notifications()
            ] == [
                "Username.1234 has left the guild.",
                "Username.1234 has left the guild.",
            ]
            store.close()

    def test_persists_join_notification_and_prevents_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = str(Path(directory) / "raffle.db")
            store = RaffleStore(database_path, "guild-id")
            store.initialize_cursor(100)
            store.process_events([guild_join(101)])

            assert [
                join.message for join in store.get_pending_join_notifications()
            ] == ["Username.1234 has joined the guild."]
            store.close()

            reopened = RaffleStore(database_path, "guild-id")
            reopened.process_events([guild_join(101)])
            pending = reopened.get_pending_join_notifications()
            assert len(pending) == 1
            reopened.mark_join_notification_sent(101)
            assert reopened.get_pending_join_notifications() == []
            reopened.close()

    def test_processes_deposit_join_and_leave_before_advancing_cursor(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RaffleStore(str(Path(directory) / "raffle.db"), "guild-id")
            store.initialize_cursor(100)

            store.process_events(
                [guild_leave(103), guild_join(102), gold_deposit(101)]
            )

            assert store.get_cursor() == 103
            assert len(store.get_pending_notifications()) == 1
            assert len(store.get_pending_join_notifications()) == 1
            assert len(store.get_pending_leave_notifications()) == 1
            store.close()

    def test_guild_event_logging_does_not_include_raw_event_secrets(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RaffleStore(str(Path(directory) / "raffle.db"), "guild-id")
            store.initialize_cursor(100)

            with caplog.at_level(logging.DEBUG, logger="gw2bot.raffle"):
                store.process_events(
                    [{**guild_join(101), "api_key": "join-event-secret"}]
                )

            assert "join-event-secret" not in caplog.text
            store.close()

    def test_persists_totals_cursor_and_notification_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = str(Path(directory) / "raffle.db")
            store = RaffleStore(database_path, "guild-id")
            store.initialize_cursor(100)
            store.process_events(
                [
                    gold_deposit(102, coins=20_000),
                    gold_deposit(101, coins=10_000),
                ]
            )

            assert store.get_cursor() == 102
            assert store.get_totals()[0].coins_deposited == 30_000
            assert store.get_totals()[0].raffle_tickets == 3
            assert [
                deposit.event_id for deposit in store.get_pending_notifications()
            ] == [101, 102]
            store.mark_notification_sent(101)
            store.close()

            reopened = RaffleStore(database_path, "guild-id")
            reopened.process_events(
                [gold_deposit(101), gold_deposit(102, coins=20_000)]
            )

            assert reopened.get_cursor() == 102
            assert reopened.get_totals()[0].raffle_tickets == 3
            assert [
                deposit.event_id for deposit in reopened.get_pending_notifications()
            ] == [102]
            reopened.close()

    def test_persists_and_updates_discord_account_links(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = str(Path(directory) / "raffle.db")
            store = RaffleStore(database_path, "guild-id")

            assert store.get_linked_username(1234) is None
            store.link_account(1234, "First.1234")
            assert store.get_linked_username(1234) == "First.1234"
            store.link_account(1234, "Second.5678")
            store.close()

            reopened = RaffleStore(database_path, "guild-id")
            assert reopened.get_linked_username(1234) == "Second.5678"
            reopened.close()

    def test_returns_zero_ticket_total_for_player_without_a_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RaffleStore(str(Path(directory) / "raffle.db"), "guild-id")

            total = store.get_total("No Tickets.1234")

            assert total.username == "No Tickets.1234"
            assert total.raffle_tickets == 0
            assert total.gold_raffle_tickets == 0
            assert total.manual_raffle_tickets == 0
            store.close()

    def test_orders_current_totals_by_ticket_count_then_username(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RaffleStore(str(Path(directory) / "raffle.db"), "guild-id")
            store.initialize_cursor(100)
            store.process_events(
                [
                    gold_deposit(101, username="Zulu.1234", coins=20_000),
                    gold_deposit(102, username="alpha.1234", coins=20_000),
                    gold_deposit(103, username="Highest.1234", coins=30_000),
                    gold_deposit(104, username="Beta.1234", coins=20_000),
                ]
            )
            store.add_manual_ticket("Free Only.1234")

            assert [
                (total.username, total.raffle_tickets)
                for total in store.get_totals()
            ] == [
                ("Highest.1234", 3),
                ("alpha.1234", 2),
                ("Beta.1234", 2),
                ("Zulu.1234", 2),
                ("Free Only.1234", 1),
            ]
            store.close()

    def test_aggregates_purchased_and_event_tickets_in_six_hour_window(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = str(Path(directory) / "raffle.db")
            store = RaffleStore(database_path, "guild-id")
            store.initialize_cursor(100)
            store.process_events(
                [
                    gold_deposit(
                        101,
                        username="Alpha.1234",
                        coins=20_000,
                        event_time="2026-06-07T00:00:00Z",
                    ),
                    gold_deposit(
                        102,
                        username="Alpha.1234",
                        event_time="2026-06-07T05:59:59Z",
                    ),
                    gold_deposit(
                        103,
                        username="Boundary.1234",
                        event_time="2026-06-07T06:00:00Z",
                    ),
                    gold_deposit(
                        104,
                        username="Partial.1234",
                        coins=5_000,
                        event_time="2026-06-07T04:00:00Z",
                    ),
                    gold_deposit(
                        105,
                        username="Zulu.1234",
                        coins=20_000,
                        event_time="2026-06-07T04:00:00Z",
                    ),
                    gold_deposit(
                        106,
                        username="able.1234",
                        coins=20_000,
                        event_time="2026-06-07T04:00:00Z",
                    ),
                    gold_deposit(
                        107,
                        username="Bravo.1234",
                        coins=20_000,
                        event_time="2026-06-07T04:00:00Z",
                    ),
                ]
            )
            store.add_manual_ticket(
                "Alpha.1234",
                datetime(2026, 6, 7, 1, tzinfo=UTC),
            )
            store.add_manual_ticket(
                "Beta.1234",
                datetime(2026, 6, 7, 3, tzinfo=UTC),
            )
            store.add_manual_ticket(
                "Boundary.1234",
                datetime(2026, 6, 7, 6, tzinfo=UTC),
            )
            store.close()

            reopened = RaffleStore(database_path, "guild-id")
            contributions = reopened.get_contributions(
                datetime(2026, 6, 7, 0, tzinfo=UTC),
                datetime(2026, 6, 7, 6, tzinfo=UTC),
            )

            assert [
                (
                    contribution.username,
                    contribution.purchased_tickets,
                    contribution.event_tickets,
                )
                for contribution in contributions
            ] == [
                ("Alpha.1234", 3, 1),
                ("able.1234", 2, 0),
                ("Bravo.1234", 2, 0),
                ("Zulu.1234", 2, 0),
                ("Beta.1234", 0, 1),
            ]
            reopened.close()

    def test_caps_gold_purchased_tickets_at_ten_per_raffle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RaffleStore(str(Path(directory) / "raffle.db"), "guild-id")
            store.initialize_cursor(100)

            store.process_events(
                [
                    gold_deposit(101, coins=80_000),
                    gold_deposit(102, coins=50_000),
                    gold_deposit(103, coins=5_000),
                ]
            )

            total = store.get_totals()[0]
            assert total.coins_deposited == 135_000
            assert total.raffle_tickets == 10
            assert total.gold_raffle_tickets == 10
            assert [
                deposit.raffle_tickets for deposit in store.get_pending_notifications()
            ] == [8, 2, 0]

            store.run_raffle(randbelow=lambda total: 0)
            store.process_events([gold_deposit(104, coins=20_000)])

            reset_total = store.get_totals()[0]
            assert reset_total.coins_deposited == 155_000
            assert reset_total.raffle_tickets == 2
            assert reset_total.gold_raffle_tickets == 2
            store.close()

    def test_officer_deposits_over_ten_gold_do_not_fire_purchase_event(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RaffleStore(str(Path(directory) / "raffle.db"), "guild-id")
            store.initialize_cursor(100)

            with caplog.at_level(logging.DEBUG, logger="gw2bot.raffle"):
                store.process_events(
                    [
                        gold_deposit(
                            101,
                            username="Officer Allowed.1234",
                            coins=100_000,
                        ),
                        {
                            **gold_deposit(
                                102,
                                username="Officer Blocked.1234",
                                coins=100_001,
                            ),
                            "api_key": "blocked-deposit-secret",
                        },
                        gold_deposit(
                            103,
                            username="Member.1234",
                            coins=110_000,
                        ),
                    ],
                    {"officer allowed.1234", "OFFICER BLOCKED.1234"},
                )

            totals = {
                total.username: total
                for total in store.get_totals()
            }
            assert totals["Officer Allowed.1234"].gold_raffle_tickets == 10
            assert "Officer Blocked.1234" not in totals
            assert totals["Member.1234"].gold_raffle_tickets == 10
            assert [
                deposit.event_id
                for deposit in store.get_pending_notifications()
            ] == [101, 103]
            assert [
                deposit.event_id
                for deposit in store.get_pending_deposit_audit_notifications()
            ] == [101, 103]
            assert store.get_cursor() == 103
            assert "Skipped oversized Officer raffle deposit event 102" in caplog.text
            assert "blocked-deposit-secret" not in caplog.text
            store.close()

    def test_deposit_main_and_audit_notifications_track_delivery_independently(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RaffleStore(str(Path(directory) / "raffle.db"), "guild-id")
            store.initialize_cursor(100)
            store.process_events([gold_deposit(101)])

            assert [deposit.event_id for deposit in store.get_pending_notifications()] == [
                101
            ]
            assert [
                deposit.event_id
                for deposit in store.get_pending_deposit_audit_notifications()
            ] == [101]

            store.mark_notification_sent(101)
            assert store.get_pending_notifications() == []
            assert [
                deposit.event_id
                for deposit in store.get_pending_deposit_audit_notifications()
            ] == [101]

            store.mark_deposit_audit_notification_sent(101)
            assert store.get_pending_deposit_audit_notifications() == []
            store.close()

    def test_removes_only_purchased_tickets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RaffleStore(str(Path(directory) / "raffle.db"), "guild-id")
            store.initialize_cursor(100)
            store.process_events([gold_deposit(101, coins=30_000)])
            store.add_manual_ticket("Username.1234")

            total = store.remove_gold_tickets("Username.1234", 2)

            assert total.coins_deposited == 30_000
            assert total.gold_raffle_tickets == 1
            assert total.manual_raffle_tickets == 1
            assert total.raffle_tickets == 2

            default_removal = store.remove_gold_tickets("Username.1234")
            assert default_removal.gold_raffle_tickets == 0
            assert default_removal.manual_raffle_tickets == 1
            assert default_removal.raffle_tickets == 1

            with pytest.raises(ValueError, match="greater than zero"):
                store.remove_gold_tickets("Username.1234", 0)
            with pytest.raises(ValueError, match="only 0 purchased"):
                store.remove_gold_tickets("Username.1234")
            store.close()

    def test_creates_configurable_milestones_once_and_resets_after_draw(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RaffleStore(
                str(Path(directory) / "raffle.db"),
                "guild-id",
                reward_tiers=(
                    RaffleRewardTier(2, "Tier A"),
                    RaffleRewardTier(4, "Tier B"),
                ),
            )
            store.initialize_cursor(100)

            store.process_events([gold_deposit(101, coins=40_000)])

            assert [
                milestone.message for milestone in store.get_pending_milestones()
            ] == [
                "2 total tickets have been purchased for this raffle. "
                "Tier A rewards have been reached!",
                "4 total tickets have been purchased for this raffle. "
                "Tier B rewards have been reached!",
            ]
            store.mark_milestone_notification_sent(2)
            store.process_events([])
            store.close()

            reopened = RaffleStore(
                str(Path(directory) / "raffle.db"),
                "guild-id",
                reward_tiers=(
                    RaffleRewardTier(2, "Tier A"),
                    RaffleRewardTier(4, "Tier B"),
                ),
            )
            assert [
                milestone.threshold
                for milestone in reopened.get_pending_milestones()
            ] == [4]

            reopened.run_raffle(randbelow=lambda total: 0)
            assert reopened.get_pending_milestones() == []
            reopened.process_events([gold_deposit(102, coins=20_000)])
            assert [
                milestone.threshold
                for milestone in reopened.get_pending_milestones()
            ] == [2]
            reopened.close()

    def test_free_tickets_do_not_count_toward_purchase_milestones(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RaffleStore(
                str(Path(directory) / "raffle.db"),
                "guild-id",
                reward_tiers=(RaffleRewardTier(1, "Purchased Tier"),),
            )
            store.initialize_cursor(100)
            store.add_manual_ticket("Free Only.1234")
            store.process_events([])

            assert store.get_pending_milestones() == []

            store.process_events([gold_deposit(101)])
            assert [
                milestone.threshold for milestone in store.get_pending_milestones()
            ] == [1]
            store.close()

    def test_rejects_invalid_reward_tier_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = str(Path(directory) / "raffle.db")
            with pytest.raises(ValueError, match="must be unique"):
                RaffleStore(
                    database_path,
                    "guild-id",
                    reward_tiers=(
                        RaffleRewardTier(50, "First"),
                        RaffleRewardTier(50, "Duplicate"),
                    ),
                )

    def test_draws_same_user_multiple_times_and_removes_one_ticket_each_draw(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = str(Path(directory) / "raffle.db")
            store = RaffleStore(
                database_path,
                "guild-id",
                draw_tiers=(RaffleDrawTier(0, 3),),
            )
            store.initialize_cursor(100)
            store.process_events([gold_deposit(101, coins=30_000)])

            result = store.run_raffle(randbelow=lambda total: 0)

            assert result is not None
            assert [
                (
                    winner.username,
                    winner.winning_ticket,
                    winner.tickets_before_draw,
                )
                for winner in result.winners
            ] == [
                ("Username.1234", 1, 3),
                ("Username.1234", 1, 2),
                ("Username.1234", 1, 1),
            ]
            store.close()

            engine = create_engine(f"sqlite:///{database_path}")
            metadata = MetaData()
            winners = Table("raffle_run_winners", metadata, autoload_with=engine)
            with engine.connect() as connection:
                assert connection.execute(
                    select(
                        winners.c.draw_position,
                        winners.c.username,
                        winners.c.tickets_before_draw,
                    ).order_by(winners.c.draw_position)
                ).all() == [
                    (1, "Username.1234", 3),
                    (2, "Username.1234", 2),
                    (3, "Username.1234", 1),
                ]
            engine.dispose()

    def test_draw_count_uses_current_purchased_ticket_tier(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RaffleStore(
                str(Path(directory) / "raffle.db"),
                "guild-id",
                draw_tiers=(
                    RaffleDrawTier(0, 1),
                    RaffleDrawTier(2, 2),
                    RaffleDrawTier(4, 3),
                ),
            )
            store.initialize_cursor(100)
            store.process_events([gold_deposit(101, coins=40_000)])

            result = store.run_raffle(randbelow=lambda total: 0)

            assert result is not None
            assert len(result.winners) == 3
            store.close()

    def test_draw_count_ignores_free_tickets_and_cannot_exceed_pool(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RaffleStore(
                str(Path(directory) / "raffle.db"),
                "guild-id",
                draw_tiers=(
                    RaffleDrawTier(0, 1),
                    RaffleDrawTier(2, 5),
                ),
            )
            store.initialize_cursor(100)
            store.process_events([gold_deposit(101)])
            store.add_manual_ticket("Username.1234")

            result = store.run_raffle(randbelow=lambda total: 0)

            assert result is not None
            assert len(result.winners) == 1
            store.close()

        with tempfile.TemporaryDirectory() as directory:
            store = RaffleStore(
                str(Path(directory) / "raffle.db"),
                "guild-id",
                draw_tiers=(RaffleDrawTier(0, 5),),
            )
            store.initialize_cursor(100)
            store.process_events([gold_deposit(101)])

            result = store.run_raffle(randbelow=lambda total: 0)

            assert result is not None
            assert len(result.winners) == 1
            store.close()

    def test_manual_tickets_and_weighted_run_reset_current_tickets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = str(Path(directory) / "raffle.db")
            store = RaffleStore(database_path, "guild-id")
            store.initialize_cursor(100)
            store.process_events(
                [
                    gold_deposit(101, username="User A.1111", coins=100_000),
                    gold_deposit(102, username="User B.2222", coins=50_000),
                ]
            )
            store.add_manual_ticket("User B.2222")

            result = store.run_raffle(randbelow=lambda total: 10)

            assert result is not None
            assert [winner.username for winner in result.winners] == [
                "User B.2222",
                "User B.2222",
            ]
            assert [
                winner.tickets_before_draw for winner in result.winners
            ] == [16, 15]
            assert result.total_tickets == 16
            for total in store.get_totals():
                assert total.raffle_tickets == 0
                assert total.gold_raffle_tickets == 0
                assert total.manual_raffle_tickets == 0
            assert {
                total.username: total.coins_deposited for total in store.get_totals()
            } == {"User A.1111": 100_000, "User B.2222": 50_000}
            store.close()

            engine = create_engine(f"sqlite:///{database_path}")
            metadata = MetaData()
            runs = Table("raffle_runs", metadata, autoload_with=engine)
            entries = Table("raffle_run_entries", metadata, autoload_with=engine)
            winners = Table("raffle_run_winners", metadata, autoload_with=engine)
            with engine.connect() as connection:
                assert connection.execute(
                    select(runs.c.winner, runs.c.total_tickets)
                ).one() == ("User B.2222", 16)
                assert (
                    connection.execute(
                        select(func.count()).select_from(entries)
                    ).scalar_one()
                    == 2
                )
                assert (
                    connection.execute(
                        select(func.count()).select_from(winners)
                    ).scalar_one()
                    == 2
                )
            engine.dispose()

    def test_persists_pending_draw_and_blocks_another_until_announced(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = str(Path(directory) / "raffle.db")
            store = RaffleStore(database_path, "guild-id")
            store.initialize_cursor(100)
            store.process_events([gold_deposit(101, coins=20_000)])

            first = store.run_raffle(randbelow=lambda total: 0)

            assert first is not None
            store.close()

            reopened = RaffleStore(database_path, "guild-id")
            assert reopened.get_pending_raffle_result() == first
            reopened.process_events([gold_deposit(102, coins=10_000)])

            retry = reopened.run_raffle(randbelow=lambda total: 0)

            assert retry == first
            assert reopened.get_totals()[0].raffle_tickets == 1
            reopened.mark_raffle_announcement_sent(first.run_id)
            assert reopened.get_pending_raffle_result() is None

            second = reopened.run_raffle(randbelow=lambda total: 0)

            assert second is not None
            assert second.run_id != first.run_id
            reopened.close()

    def test_manual_ticket_addition_caps_at_one_per_raffle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RaffleStore(str(Path(directory) / "raffle.db"), "guild-id")

            total = store.add_manual_ticket("Member.1234")

            assert total.raffle_tickets == 1
            assert total.manual_raffle_tickets == 1
            with pytest.raises(ValueError, match="maximum of 1 manually added ticket"):
                store.add_manual_ticket("Member.1234")

            store.run_raffle(randbelow=lambda total: 0)
            reset_total = store.add_manual_ticket("Member.1234")

            assert reset_total.raffle_tickets == 1
            assert reset_total.manual_raffle_tickets == 1
            store.close()

    def test_migrates_existing_totals_and_caps_current_gold_tickets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = str(Path(directory) / "raffle.db")
            engine = create_engine(f"sqlite:///{database_path}")
            metadata = MetaData()
            legacy_totals = Table(
                "raffle_totals",
                metadata,
                Column("username", String, primary_key=True),
                Column("coins_deposited", Integer, nullable=False),
                Column("raffle_tickets", Integer, nullable=False),
            )
            metadata.create_all(engine)
            with engine.begin() as connection:
                connection.execute(
                    legacy_totals.insert().values(
                        username="Member.1234",
                        coins_deposited=150_000,
                        raffle_tickets=15,
                    )
                )
            engine.dispose()

            store = RaffleStore(database_path, "guild-id")

            total = store.get_totals()[0]
            assert total.coins_deposited == 150_000
            assert total.raffle_tickets == 10
            assert total.gold_raffle_tickets == 10
            assert total.manual_raffle_tickets == 0
            store.close()

    def test_migrates_existing_deposits_without_replaying_audit_notifications(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = str(Path(directory) / "raffle.db")
            engine = create_engine(f"sqlite:///{database_path}")
            metadata = MetaData()
            legacy_deposits = Table(
                "raffle_deposits",
                metadata,
                Column("event_id", Integer, primary_key=True),
                Column("username", String, nullable=False),
                Column("coins_deposited", Integer, nullable=False),
                Column("raffle_tickets", Integer, nullable=False),
                Column("event_time", String, nullable=False),
                Column("notification_sent", Boolean, nullable=False),
            )
            metadata.create_all(engine)
            with engine.begin() as connection:
                connection.execute(
                    legacy_deposits.insert().values(
                        event_id=101,
                        username="Existing.1234",
                        coins_deposited=10_000,
                        raffle_tickets=1,
                        event_time="2026-06-07T06:26:17.000Z",
                        notification_sent=True,
                    )
                )
            engine.dispose()

            store = RaffleStore(database_path, "guild-id")

            assert store.get_pending_notifications() == []
            assert store.get_pending_deposit_audit_notifications() == []
            store.close()

    def test_migrates_existing_leave_events_without_kicker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = str(Path(directory) / "raffle.db")
            engine = create_engine(f"sqlite:///{database_path}")
            metadata = MetaData()
            legacy_leaves = Table(
                "guild_leave_events",
                metadata,
                Column("event_id", Integer, primary_key=True),
                Column("username", String, nullable=False),
                Column("event_time", String, nullable=False),
                Column("notification_sent", Boolean, nullable=False),
            )
            metadata.create_all(engine)
            with engine.begin() as connection:
                connection.execute(
                    legacy_leaves.insert().values(
                        event_id=101,
                        username="Existing.1234",
                        event_time="2026-06-07T06:26:17.000Z",
                        notification_sent=False,
                    )
                )
            engine.dispose()

            store = RaffleStore(database_path, "guild-id")
            store.initialize_cursor(101)
            store.process_events([guild_kick(102)])

            assert [
                leave.message for leave in store.get_pending_leave_notifications()
            ] == [
                "Existing.1234 has left the guild.",
                "Officer.5678 kicked Kicked.1234 from the guild.",
            ]
            store.close()

    def test_one_time_migration_caps_existing_free_tickets_at_one(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = str(Path(directory) / "raffle.db")
            secret = "migration-secret"
            engine = create_engine(f"sqlite:///{database_path}")
            metadata = MetaData()
            totals = Table(
                "raffle_totals",
                metadata,
                Column("username", String, primary_key=True),
                Column("coins_deposited", Integer, nullable=False),
                Column("raffle_tickets", Integer, nullable=False),
                Column("gold_raffle_tickets", Integer, nullable=False),
                Column("manual_raffle_tickets", Integer, nullable=False),
            )
            metadata.create_all(engine)
            with engine.begin() as connection:
                connection.execute(
                    totals.insert(),
                    [
                        {
                            "username": "Purchased And Free.1234",
                            "coins_deposited": 20_000,
                            "raffle_tickets": 5,
                            "gold_raffle_tickets": 2,
                            "manual_raffle_tickets": 3,
                        },
                        {
                            "username": f"Free Only {secret}.1234",
                            "coins_deposited": 0,
                            "raffle_tickets": 4,
                            "gold_raffle_tickets": 0,
                            "manual_raffle_tickets": 4,
                        },
                        {
                            "username": "Already Valid.1234",
                            "coins_deposited": 10_000,
                            "raffle_tickets": 2,
                            "gold_raffle_tickets": 1,
                            "manual_raffle_tickets": 1,
                        },
                    ],
                )
            engine.dispose()

            with caplog.at_level(logging.INFO, logger="gw2bot.raffle"):
                store = RaffleStore(database_path, "guild-id")

            assert {
                total.username: (
                    total.raffle_tickets,
                    total.gold_raffle_tickets,
                    total.manual_raffle_tickets,
                )
                for total in store.get_totals()
            } == {
                "Purchased And Free.1234": (3, 2, 1),
                "Already Valid.1234": (2, 1, 1),
                f"Free Only {secret}.1234": (1, 0, 1),
            }
            assert secret not in caplog.text
            store.close()

    def test_free_ticket_cap_migration_runs_only_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = str(Path(directory) / "raffle.db")
            store = RaffleStore(database_path, "guild-id")
            store.close()

            engine = create_engine(f"sqlite:///{database_path}")
            metadata = MetaData()
            totals = Table("raffle_totals", metadata, autoload_with=engine)
            with engine.begin() as connection:
                connection.execute(
                    totals.insert().values(
                        username="After Migration.1234",
                        coins_deposited=0,
                        raffle_tickets=2,
                        gold_raffle_tickets=0,
                        manual_raffle_tickets=2,
                    )
                )
            engine.dispose()

            reopened = RaffleStore(database_path, "guild-id")
            total = reopened.get_totals()[0]

            assert total.raffle_tickets == 2
            assert total.manual_raffle_tickets == 2
            reopened.close()

    def test_migrates_existing_raffle_runs_as_already_announced(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = str(Path(directory) / "raffle.db")
            engine = create_engine(f"sqlite:///{database_path}")
            metadata = MetaData()
            legacy_runs = Table(
                "raffle_runs",
                metadata,
                Column("run_id", Integer, primary_key=True, autoincrement=True),
                Column("run_time", String, nullable=False),
                Column("winner", String, nullable=False),
                Column("winning_ticket", Integer, nullable=False),
                Column("total_tickets", Integer, nullable=False),
            )
            metadata.create_all(engine)
            with engine.begin() as connection:
                connection.execute(
                    legacy_runs.insert().values(
                        run_time="2026-06-07 12:00:00",
                        winner="Winner.1234",
                        winning_ticket=1,
                        total_tickets=10,
                    )
                )
            engine.dispose()

            store = RaffleStore(database_path, "guild-id")

            assert store.get_pending_raffle_result() is None
            store.close()

    def test_loads_legacy_pending_single_winner_as_one_winner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = str(Path(directory) / "raffle.db")
            engine = create_engine(f"sqlite:///{database_path}")
            metadata = MetaData()
            legacy_runs = Table(
                "raffle_runs",
                metadata,
                Column("run_id", Integer, primary_key=True, autoincrement=True),
                Column("run_time", String, nullable=False),
                Column("winner", String, nullable=False),
                Column("winning_ticket", Integer, nullable=False),
                Column("total_tickets", Integer, nullable=False),
                Column("announcement_sent", Boolean, nullable=False),
            )
            metadata.create_all(engine)
            with engine.begin() as connection:
                connection.execute(
                    legacy_runs.insert().values(
                        run_time="2026-06-07 12:00:00",
                        winner="Winner.1234",
                        winning_ticket=4,
                        total_tickets=10,
                        announcement_sent=False,
                    )
                )
            engine.dispose()

            store = RaffleStore(database_path, "guild-id")
            result = store.get_pending_raffle_result()

            assert result is not None
            assert result.total_tickets == 10
            assert [
                (
                    winner.username,
                    winner.winning_ticket,
                    winner.tickets_before_draw,
                )
                for winner in result.winners
            ] == [("Winner.1234", 4, 10)]
            store.close()

    def test_new_store_cursor_skips_historical_events(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RaffleStore(str(Path(directory) / "raffle.db"), "guild-id")
            store.initialize_cursor(500)

            store.process_events([gold_deposit(499), gold_deposit(500)])

            assert store.get_totals() == []
            assert store.get_pending_notifications() == []
            store.close()

    def test_database_cannot_be_reused_for_another_guild(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = str(Path(directory) / "raffle.db")
            store = RaffleStore(database_path, "first-guild")
            store.close()

            with pytest.raises(ValueError, match="different guild"):
                RaffleStore(database_path, "second-guild")

    def test_persists_and_clears_feast_alert_times(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = str(Path(directory) / "raffle.db")
            store = RaffleStore(database_path, "guild-id")
            store.mark_feast_alert_sent(1078, 123.5)
            store.close()

            reopened = RaffleStore(database_path, "guild-id")
            assert reopened.get_feast_alert_times() == {1078: 123.5}
            reopened.clear_feast_alert(1078)
            assert reopened.get_feast_alert_times() == {}
            reopened.close()

    def test_formats_whole_and_fractional_gold(self) -> None:
        assert format_gold(10_000) == "1"
        assert format_gold(12_345) == "1.2345"
