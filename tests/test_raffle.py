import tempfile
import unittest
from pathlib import Path

from sqlalchemy import (
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
    RaffleStore,
    format_gold,
    parse_gold_deposit,
    parse_guild_leave,
)


def gold_deposit(
    event_id: int,
    username: str = "Username.1234",
    coins: int = 10_000,
) -> dict[str, object]:
    return {
        "id": event_id,
        "time": "2026-06-07T06:26:17.000Z",
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


class RaffleTests(unittest.TestCase):
    def test_parses_gold_deposit_and_formats_message(self) -> None:
        deposit = parse_gold_deposit(gold_deposit(101, coins=35_000))

        self.assertIsNotNone(deposit)
        assert deposit is not None
        self.assertEqual(deposit.raffle_tickets, 3)
        self.assertEqual(
            deposit.message,
            "Username.1234 deposited 3.5 gold and purchased 3 raffle tickets",
        )

    def test_tracks_partial_gold_but_ignores_non_deposit_events(self) -> None:
        partial = parse_gold_deposit(gold_deposit(101, coins=9_999))
        self.assertIsNotNone(partial)
        assert partial is not None
        self.assertEqual(partial.raffle_tickets, 0)
        self.assertIsNone(
            parse_gold_deposit({**gold_deposit(102), "operation": "withdraw"})
        )
        self.assertIsNone(parse_gold_deposit({**gold_deposit(103), "type": "treasury"}))

    def test_parses_guild_leave_with_exact_message(self) -> None:
        leave = parse_guild_leave(guild_leave(104))

        self.assertIsNotNone(leave)
        assert leave is not None
        self.assertEqual(leave.message, "Username.1234 has left the guild.")
        self.assertIsNone(parse_guild_leave({**guild_leave(105), "type": "joined"}))
        self.assertIsNone(
            parse_guild_leave(
                {
                    **guild_leave(106),
                    "kicked_by": "Officer.5678",
                }
            )
        )

    def test_persists_leave_notification_and_prevents_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = str(Path(directory) / "raffle.db")
            store = RaffleStore(database_path, "guild-id")
            store.initialize_cursor(100)
            store.process_events([guild_leave(101)])

            self.assertEqual(
                [leave.message for leave in store.get_pending_leave_notifications()],
                ["Username.1234 has left the guild."],
            )
            store.close()

            reopened = RaffleStore(database_path, "guild-id")
            reopened.process_events([guild_leave(101)])
            pending = reopened.get_pending_leave_notifications()
            self.assertEqual(len(pending), 1)
            reopened.mark_leave_notification_sent(101)
            self.assertEqual(reopened.get_pending_leave_notifications(), [])
            reopened.close()

    def test_repeated_join_leave_cycles_create_distinct_notifications(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RaffleStore(str(Path(directory) / "raffle.db"), "guild-id")
            store.initialize_cursor(100)

            store.process_events(
                [
                    guild_leave(101),
                    {**guild_leave(102), "type": "joined"},
                    guild_leave(103),
                ]
            )

            self.assertEqual(
                [leave.event_id for leave in store.get_pending_leave_notifications()],
                [101, 103],
            )
            self.assertEqual(
                [leave.message for leave in store.get_pending_leave_notifications()],
                [
                    "Username.1234 has left the guild.",
                    "Username.1234 has left the guild.",
                ],
            )
            store.close()

    def test_processes_raffle_deposit_and_guild_leave_before_advancing_cursor(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RaffleStore(str(Path(directory) / "raffle.db"), "guild-id")
            store.initialize_cursor(100)

            store.process_events([guild_leave(102), gold_deposit(101)])

            self.assertEqual(store.get_cursor(), 102)
            self.assertEqual(len(store.get_pending_notifications()), 1)
            self.assertEqual(len(store.get_pending_leave_notifications()), 1)
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

            self.assertEqual(store.get_cursor(), 102)
            self.assertEqual(store.get_totals()[0].coins_deposited, 30_000)
            self.assertEqual(store.get_totals()[0].raffle_tickets, 3)
            self.assertEqual(
                [deposit.event_id for deposit in store.get_pending_notifications()],
                [101, 102],
            )
            store.mark_notification_sent(101)
            store.close()

            reopened = RaffleStore(database_path, "guild-id")
            reopened.process_events(
                [gold_deposit(101), gold_deposit(102, coins=20_000)]
            )

            self.assertEqual(reopened.get_cursor(), 102)
            self.assertEqual(reopened.get_totals()[0].raffle_tickets, 3)
            self.assertEqual(
                [deposit.event_id for deposit in reopened.get_pending_notifications()],
                [102],
            )
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
            self.assertEqual(total.coins_deposited, 135_000)
            self.assertEqual(total.raffle_tickets, 10)
            self.assertEqual(total.gold_raffle_tickets, 10)
            self.assertEqual(
                [
                    deposit.raffle_tickets
                    for deposit in store.get_pending_notifications()
                ],
                [8, 2, 0],
            )

            store.run_raffle(randbelow=lambda total: 0)
            store.process_events([gold_deposit(104, coins=20_000)])

            reset_total = store.get_totals()[0]
            self.assertEqual(reset_total.coins_deposited, 155_000)
            self.assertEqual(reset_total.raffle_tickets, 2)
            self.assertEqual(reset_total.gold_raffle_tickets, 2)
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
            store.add_manual_ticket("User B.2222")
            store.add_manual_ticket("User B.2222")

            result = store.run_raffle(randbelow=lambda total: 10)

            self.assertIsNotNone(result)
            assert result is not None
            self.assertEqual(result.winner, "User B.2222")
            self.assertEqual(result.total_tickets, 18)
            for total in store.get_totals():
                self.assertEqual(total.raffle_tickets, 0)
                self.assertEqual(total.gold_raffle_tickets, 0)
                self.assertEqual(total.manual_raffle_tickets, 0)
            self.assertEqual(
                {total.username: total.coins_deposited for total in store.get_totals()},
                {"User A.1111": 100_000, "User B.2222": 50_000},
            )
            store.close()

            engine = create_engine(f"sqlite:///{database_path}")
            metadata = MetaData()
            runs = Table("raffle_runs", metadata, autoload_with=engine)
            entries = Table("raffle_run_entries", metadata, autoload_with=engine)
            with engine.connect() as connection:
                self.assertEqual(
                    connection.execute(
                        select(runs.c.winner, runs.c.total_tickets)
                    ).one(),
                    ("User B.2222", 18),
                )
                self.assertEqual(
                    connection.execute(select(func.count()).select_from(entries))
                    .scalar_one(),
                    2,
                )
            engine.dispose()

    def test_persists_pending_draw_and_blocks_another_until_announced(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = str(Path(directory) / "raffle.db")
            store = RaffleStore(database_path, "guild-id")
            store.initialize_cursor(100)
            store.process_events([gold_deposit(101, coins=20_000)])

            first = store.run_raffle(randbelow=lambda total: 0)

            self.assertIsNotNone(first)
            assert first is not None
            store.close()

            reopened = RaffleStore(database_path, "guild-id")
            self.assertEqual(reopened.get_pending_raffle_result(), first)
            reopened.process_events([gold_deposit(102, coins=10_000)])

            retry = reopened.run_raffle(randbelow=lambda total: 0)

            self.assertEqual(retry, first)
            self.assertEqual(reopened.get_totals()[0].raffle_tickets, 1)
            reopened.mark_raffle_announcement_sent(first.run_id)
            self.assertIsNone(reopened.get_pending_raffle_result())

            second = reopened.run_raffle(randbelow=lambda total: 0)

            self.assertIsNotNone(second)
            assert second is not None
            self.assertNotEqual(second.run_id, first.run_id)
            reopened.close()

    def test_manual_ticket_addition_caps_at_three_per_raffle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RaffleStore(str(Path(directory) / "raffle.db"), "guild-id")

            store.add_manual_ticket("Member.1234")
            store.add_manual_ticket("Member.1234")
            total = store.add_manual_ticket("Member.1234")

            self.assertEqual(total.raffle_tickets, 3)
            self.assertEqual(total.manual_raffle_tickets, 3)
            with self.assertRaisesRegex(ValueError, "maximum of 3"):
                store.add_manual_ticket("Member.1234")
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
            self.assertEqual(total.coins_deposited, 150_000)
            self.assertEqual(total.raffle_tickets, 10)
            self.assertEqual(total.gold_raffle_tickets, 10)
            self.assertEqual(total.manual_raffle_tickets, 0)
            store.close()

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

            self.assertIsNone(store.get_pending_raffle_result())
            store.close()

    def test_new_store_cursor_skips_historical_events(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RaffleStore(str(Path(directory) / "raffle.db"), "guild-id")
            store.initialize_cursor(500)

            store.process_events([gold_deposit(499), gold_deposit(500)])

            self.assertEqual(store.get_totals(), [])
            self.assertEqual(store.get_pending_notifications(), [])
            store.close()

    def test_database_cannot_be_reused_for_another_guild(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = str(Path(directory) / "raffle.db")
            store = RaffleStore(database_path, "first-guild")
            store.close()

            with self.assertRaisesRegex(ValueError, "different guild"):
                RaffleStore(database_path, "second-guild")

    def test_persists_and_clears_feast_alert_times(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = str(Path(directory) / "raffle.db")
            store = RaffleStore(database_path, "guild-id")
            store.mark_feast_alert_sent(1078, 123.5)
            store.close()

            reopened = RaffleStore(database_path, "guild-id")
            self.assertEqual(reopened.get_feast_alert_times(), {1078: 123.5})
            reopened.clear_feast_alert(1078)
            self.assertEqual(reopened.get_feast_alert_times(), {})
            reopened.close()

    def test_formats_whole_and_fractional_gold(self) -> None:
        self.assertEqual(format_gold(10_000), "1")
        self.assertEqual(format_gold(12_345), "1.2345")


if __name__ == "__main__":
    unittest.main()
