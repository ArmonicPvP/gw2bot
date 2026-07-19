import logging
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
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
    RaffleDrawTier,
    RaffleRewardTier,
    RaffleStore,
    TrialForumPost,
)

from factories import (
    gold_deposit,
    guild_invite,
    guild_join,
    guild_kick,
    guild_leave,
    guild_rank_change,
)


class TestRaffleStore:
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

    def test_persists_invite_notification_and_prevents_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = str(Path(directory) / "raffle.db")
            store = RaffleStore(database_path, "guild-id")
            store.initialize_cursor(100)
            store.process_events([guild_invite(101)])

            assert [
                invite.message
                for invite in store.get_pending_invite_notifications()
            ] == ["Officer.5678 invited Invited.1234 to the guild."]
            store.close()

            reopened = RaffleStore(database_path, "guild-id")
            reopened.process_events([guild_invite(101)])
            pending = reopened.get_pending_invite_notifications()
            assert len(pending) == 1
            reopened.mark_invite_notification_sent(101)
            assert reopened.get_pending_invite_notifications() == []
            reopened.close()

    def test_persists_rank_change_notification_and_prevents_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = str(Path(directory) / "raffle.db")
            store = RaffleStore(database_path, "guild-id")
            store.initialize_cursor(100)
            store.process_events([guild_rank_change(101)])

            assert [
                change.message
                for change in store.get_pending_rank_change_notifications()
            ] == [
                "Officer.5678 changed Member.1234's guild rank "
                "from Trial to Sunborne."
            ]
            store.close()

            reopened = RaffleStore(database_path, "guild-id")
            reopened.process_events([guild_rank_change(101)])
            pending = reopened.get_pending_rank_change_notifications()
            assert len(pending) == 1
            reopened.mark_rank_change_notification_sent(101)
            assert reopened.get_pending_rank_change_notifications() == []
            reopened.close()

    def test_toggles_and_persists_tracked_trial_members(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = str(Path(directory) / "raffle.db")
            store = RaffleStore(database_path, "guild-id")

            assert store.get_tracked_trial_members() == set()
            assert store.is_trial_member_tracked("Trialist.1234") is False
            assert (
                store.toggle_trial_member_tracking("Trialist.1234", 42) is True
            )
            assert store.is_trial_member_tracked("Trialist.1234") is True
            assert store.get_tracked_trial_members() == {"Trialist.1234"}
            store.close()

            reopened = RaffleStore(database_path, "guild-id")
            assert reopened.get_tracked_trial_members() == {"Trialist.1234"}
            assert (
                reopened.toggle_trial_member_tracking("Trialist.1234", 42) is False
            )
            assert reopened.get_tracked_trial_members() == set()
            reopened.close()

    def test_tracked_trial_member_times_record_when_tracked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RaffleStore(str(Path(directory) / "raffle.db"), "guild-id")
            tracked_at = datetime(2026, 6, 10, 17, tzinfo=UTC)

            store.toggle_trial_member_tracking(
                "Trialist.1234",
                42,
                event_time=tracked_at,
            )

            times = store.get_tracked_trial_member_times()
            assert set(times) == {"Trialist.1234"}
            assert times["Trialist.1234"] == tracked_at
            assert times["Trialist.1234"].tzinfo is not None
            store.close()

    def test_untrack_trial_member_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RaffleStore(str(Path(directory) / "raffle.db"), "guild-id")
            store.toggle_trial_member_tracking("Trialist.1234", 42)

            store.untrack_trial_member("Trialist.1234")
            store.untrack_trial_member("Trialist.1234")

            assert store.get_tracked_trial_members() == set()
            store.close()

    def test_trial_forum_index_round_trips_across_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = str(Path(directory) / "raffle.db")
            store = RaffleStore(database_path, "guild-id")

            assert store.get_trial_forum_index() == {}
            assert store.get_trial_forum_watermark() is None

            store.upsert_trial_forum_posts(
                [
                    TrialForumPost(1, 101, "first.1234", "2026-06-01T00:00:00+00:00"),
                    TrialForumPost(2, 202, "second.5678", "2026-06-02T00:00:00+00:00"),
                ]
            )
            watermark = datetime(2026, 6, 10, 17, tzinfo=UTC)
            store.set_trial_forum_watermark(watermark)
            store.close()

            reopened = RaffleStore(database_path, "guild-id")
            index = reopened.get_trial_forum_index()
            assert set(index) == {1, 2}
            assert index[1].owner_id == 101
            assert index[1].normalized_content == "first.1234"
            assert reopened.get_trial_forum_watermark() == watermark

            # Upsert overwrites, delete removes, clear wipes everything.
            reopened.upsert_trial_forum_posts(
                [TrialForumPost(1, 999, "updated.1234", "2026-06-03T00:00:00+00:00")]
            )
            assert reopened.get_trial_forum_index()[1].owner_id == 999
            reopened.delete_trial_forum_posts({2})
            assert set(reopened.get_trial_forum_index()) == {1}
            reopened.clear_trial_forum_index()
            assert reopened.get_trial_forum_index() == {}
            assert reopened.get_trial_forum_watermark() is None
            reopened.close()

    def test_set_trial_forum_watermark_normalizes_to_utc(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RaffleStore(str(Path(directory) / "raffle.db"), "guild-id")
            naive = datetime(2026, 6, 10, 17)

            store.set_trial_forum_watermark(naive)

            stored = store.get_trial_forum_watermark()
            assert stored is not None
            assert stored.tzinfo is not None
            assert stored == naive.replace(tzinfo=UTC)
            store.close()

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

    def test_officer_purchase_records_a_complete_deposit_event(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RaffleStore(
                str(Path(directory) / "raffle.db"),
                "guild-id",
                reward_tiers=(RaffleRewardTier(2, "Officer Tier"),),
            )
            event_time = datetime(2026, 6, 21, 12, tzinfo=UTC)

            total = store.add_officer_purchase(
                "Member.1234",
                3,
                event_time=event_time,
            )

            assert total.coins_deposited == 30_000
            assert total.gold_raffle_tickets == 3
            assert total.manual_raffle_tickets == 0
            assert total.raffle_tickets == 3
            assert [
                deposit.message for deposit in store.get_pending_notifications()
            ] == [
                "Member.1234 deposited 3 gold and purchased 3 raffle tickets"
            ]
            assert [
                deposit.message
                for deposit in store.get_pending_deposit_audit_notifications()
            ] == [
                "Member.1234 deposited 3 gold and purchased 3 raffle tickets"
            ]
            assert [
                (
                    contribution.username,
                    contribution.purchased_tickets,
                    contribution.event_tickets,
                )
                for contribution in store.get_contributions(
                    event_time,
                    event_time + timedelta(hours=1),
                )
            ] == [("Member.1234", 3, 0)]
            assert [
                milestone.message for milestone in store.get_pending_milestones()
            ] == [
                "2 total tickets have been purchased for this raffle. "
                "Officer Tier rewards have been reached!"
            ]
            store.close()

    def test_officer_purchase_over_cap_fails_without_partial_update(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RaffleStore(str(Path(directory) / "raffle.db"), "guild-id")
            store.add_officer_purchase("Member.1234", 8)

            with pytest.raises(
                ValueError,
                match=(
                    "Adding 3 purchased raffle tickets would put Member.1234 "
                    "over the maximum of 10"
                ),
            ):
                store.add_officer_purchase("Member.1234", 3)

            total = store.get_total("Member.1234")
            assert total.coins_deposited == 80_000
            assert total.gold_raffle_tickets == 8
            assert total.raffle_tickets == 8
            assert len(store.get_pending_notifications()) == 1
            with pytest.raises(ValueError, match="greater than zero"):
                store.add_officer_purchase("Member.1234", 0)
            store.close()

    def test_lifetime_contributions_persist_across_draw(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RaffleStore(str(Path(directory) / "raffle.db"), "guild-id")
            store.initialize_cursor(100)
            store.process_events(
                [
                    gold_deposit(101, username="Alpha.1234", coins=30_000),
                    gold_deposit(102, username="Beta.1234", coins=10_000),
                ]
            )
            store.add_manual_ticket("Alpha.1234")

            # A draw wipes current ticket counters but not the event history.
            assert store.run_raffle(randbelow=lambda total: 0) is not None
            assert store.get_total("Alpha.1234").raffle_tickets == 0

            assert [
                (
                    contribution.username,
                    contribution.purchased_tickets,
                    contribution.event_tickets,
                )
                for contribution in store.get_lifetime_contributions()
            ] == [
                ("Alpha.1234", 3, 1),
                ("Beta.1234", 1, 0),
            ]
            store.close()

    def test_lifetime_contributions_empty_without_activity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RaffleStore(str(Path(directory) / "raffle.db"), "guild-id")
            assert store.get_lifetime_contributions() == []
            store.close()

    def test_officer_purchase_logging_omits_username_content(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        secret = "secret-officer-account.1234"
        with tempfile.TemporaryDirectory() as directory:
            store = RaffleStore(str(Path(directory) / "raffle.db"), "guild-id")

            with caplog.at_level(logging.DEBUG, logger="gw2bot.raffle"):
                store.add_officer_purchase(secret, 10)
                with pytest.raises(ValueError):
                    store.add_officer_purchase(secret, 1)

            assert secret not in caplog.text
            assert "Recorded officer raffle purchase; amount=10" in caplog.text
            assert "Officer raffle purchase rejected; amount=1" in caplog.text
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
                    winner.tickets_held,
                )
                for winner in result.winners
            ] == [
                ("Username.1234", 1, 3, 3),
                ("Username.1234", 1, 2, 2),
                ("Username.1234", 1, 1, 1),
            ]
            assert [winner.win_chance for winner in result.winners] == [
                1.0,
                1.0,
                1.0,
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

    def test_audit_reconstructs_multi_winner_ticket_ranges_per_draw(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RaffleStore(str(Path(directory) / "raffle.db"), "guild-id")
            store.initialize_cursor(100)
            store.process_events(
                [
                    gold_deposit(101, username="Alpha One.1111", coins=20_000),
                    gold_deposit(102, username="Beta Two.2222", coins=30_000),
                    gold_deposit(103, username="Gamma Three.3333", coins=40_000),
                ]
            )
            tickets = iter([0, 1])
            result = store.run_raffle(randbelow=lambda total: next(tickets))
            assert result is not None

            audit = store.get_raffle_audit(result.run_id)

            assert audit is not None
            assert audit.run_id == result.run_id
            assert audit.run_time
            assert audit.total_tickets == 9
            assert audit.purchased_tickets == 9
            assert audit.free_tickets == 0
            assert audit.has_entrant_snapshot
            assert [
                (
                    entrant.username,
                    entrant.tickets,
                    entrant.first_ticket,
                    entrant.last_ticket,
                )
                for entrant in audit.entrants
            ] == [
                ("Alpha One.1111", 2, 1, 2),
                ("Beta Two.2222", 3, 3, 5),
                ("Gamma Three.3333", 4, 6, 9),
            ]
            assert [
                (
                    draw.draw_position,
                    draw.username,
                    draw.winning_ticket,
                    draw.tickets_before_draw,
                    draw.tickets_held,
                )
                for draw in audit.draws
            ] == [
                (1, "Alpha One.1111", 1, 9, 2),
                (2, "Beta Two.2222", 2, 8, 3),
            ]
            assert audit.draws[0].ranges == audit.entrants
            # Alpha's win removed one ticket, so every later range shifts.
            assert [
                (
                    entrant.username,
                    entrant.tickets,
                    entrant.first_ticket,
                    entrant.last_ticket,
                )
                for entrant in audit.draws[1].ranges
            ] == [
                ("Alpha One.1111", 1, 1, 1),
                ("Beta Two.2222", 3, 2, 4),
                ("Gamma Three.3333", 4, 5, 8),
            ]
            assert audit.draws[0].win_chance == pytest.approx(2 / 9)
            assert audit.draws[1].win_chance == pytest.approx(3 / 8)
            store.close()

    def test_audit_reconstructs_single_winner_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RaffleStore(
                str(Path(directory) / "raffle.db"),
                "guild-id",
                draw_tiers=(RaffleDrawTier(0, 1),),
            )
            store.initialize_cursor(100)
            store.process_events([gold_deposit(101, coins=20_000)])
            result = store.run_raffle(randbelow=lambda total: 1)
            assert result is not None

            audit = store.get_raffle_audit(result.run_id)

            assert audit is not None
            assert [
                (
                    entrant.username,
                    entrant.tickets,
                    entrant.first_ticket,
                    entrant.last_ticket,
                )
                for entrant in audit.entrants
            ] == [("Username.1234", 2, 1, 2)]
            assert len(audit.draws) == 1
            draw = audit.draws[0]
            assert (
                draw.draw_position,
                draw.username,
                draw.winning_ticket,
                draw.tickets_before_draw,
                draw.tickets_held,
            ) == (1, "Username.1234", 2, 2, 2)
            assert draw.ranges == audit.entrants
            assert draw.win_chance == 1.0
            store.close()

    def test_audit_degrades_gracefully_for_legacy_run_without_snapshot(
        self,
    ) -> None:
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
                        winning_ticket=4,
                        total_tickets=10,
                    )
                )
            engine.dispose()

            store = RaffleStore(database_path, "guild-id")
            audit = store.get_raffle_audit(1)

            assert audit is not None
            assert audit.run_time == "2026-06-07 12:00:00"
            assert audit.total_tickets == 10
            assert audit.purchased_tickets == 10
            assert audit.free_tickets == 0
            assert not audit.has_entrant_snapshot
            assert audit.entrants == ()
            assert [
                (
                    draw.draw_position,
                    draw.username,
                    draw.winning_ticket,
                    draw.tickets_before_draw,
                    draw.tickets_held,
                    draw.ranges,
                )
                for draw in audit.draws
            ] == [(1, "Winner.1234", 4, 10, None, ())]
            assert audit.draws[0].win_chance is None
            store.close()

    def test_audit_returns_none_for_unknown_run_and_lists_summaries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RaffleStore(str(Path(directory) / "raffle.db"), "guild-id")
            store.initialize_cursor(100)
            assert store.get_raffle_run_summaries() == []
            assert store.get_raffle_audit(1) is None

            store.process_events([gold_deposit(101, coins=20_000)])
            first = store.run_raffle(randbelow=lambda total: 0)
            assert first is not None
            store.mark_raffle_announcement_sent(first.run_id)
            store.process_events([gold_deposit(102, coins=10_000)])
            second = store.run_raffle(randbelow=lambda total: 0)
            assert second is not None

            summaries = store.get_raffle_run_summaries()
            assert [summary.run_id for summary in summaries] == [
                second.run_id,
                first.run_id,
            ]
            assert all(summary.run_time for summary in summaries)
            assert store.get_raffle_audit(999) is None
            store.close()

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
                (winner.tickets_before_draw, winner.tickets_held)
                for winner in result.winners
            ] == [(16, 6), (15, 5)]
            assert result.total_tickets == 16
            assert result.purchased_tickets == 15
            assert result.free_tickets == 1
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
                    select(
                        runs.c.winner,
                        runs.c.total_tickets,
                        runs.c.purchased_tickets,
                        runs.c.free_tickets,
                    )
                ).one() == ("User B.2222", 16, 15, 1)
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

    def test_migrates_existing_deposits_preserving_pending_audit_notifications(
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
                    legacy_deposits.insert(),
                    [
                        {
                            "event_id": 101,
                            "username": "Delivered.1234",
                            "coins_deposited": 10_000,
                            "raffle_tickets": 1,
                            "event_time": "2026-06-07T06:26:17.000Z",
                            "notification_sent": True,
                        },
                        {
                            "event_id": 102,
                            "username": "Pending.1234",
                            "coins_deposited": 20_000,
                            "raffle_tickets": 2,
                            "event_time": "2026-06-07T06:27:17.000Z",
                            "notification_sent": False,
                        },
                    ],
                )
            engine.dispose()

            store = RaffleStore(database_path, "guild-id")

            assert store.get_pending_notifications() == []
            pending_audits = store.get_pending_deposit_audit_notifications()
            assert [deposit.event_id for deposit in pending_audits] == [102]
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
            assert result.purchased_tickets == 10
            assert result.free_tickets == 0
            assert [
                (
                    winner.username,
                    winner.winning_ticket,
                    winner.tickets_before_draw,
                    winner.tickets_held,
                )
                for winner in result.winners
            ] == [("Winner.1234", 4, 10, None)]
            assert result.winners[0].win_chance is None
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

    def test_records_and_reads_latest_feast_counts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = str(Path(directory) / "raffle.db")
            store = RaffleStore(database_path, "guild-id")

            assert store.get_last_feast_counts() == {}

            store.record_feast_counts({1078: 2, 1102: 3}, 100.0)
            assert store.get_last_feast_counts() == {1078: 2, 1102: 3}

            # A later write only moves the latest value for the changed feast.
            store.record_feast_counts({1078: 0}, 200.0)
            store.close()

            reopened = RaffleStore(database_path, "guild-id")
            assert reopened.get_last_feast_counts() == {1078: 0, 1102: 3}
            reopened.close()

    def test_record_feast_counts_ignores_empty_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RaffleStore(str(Path(directory) / "raffle.db"), "guild-id")

            store.record_feast_counts({}, 100.0)

            assert store.get_last_feast_counts() == {}
            store.close()

    def test_feast_stock_series_windows_samples_and_prior_count(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RaffleStore(str(Path(directory) / "raffle.db"), "guild-id")
            # Two records before the window and two inside it for feast 1078.
            store.record_feast_counts({1078: 50}, 100.0)
            store.record_feast_counts({1078: 44}, 150.0)
            store.record_feast_counts({1078: 40}, 300.0)
            store.record_feast_counts({1078: 38}, 400.0)

            series = store.get_feast_stock_series(since=200.0)

            # Every tracked feast is present even with no samples in the window.
            assert set(series) == {1078, 1089, 1102, 1112}
            feast = series[1078]
            assert feast.prior_count == 44
            assert [
                (sample.recorded_at, sample.count) for sample in feast.samples
            ] == [(300.0, 40), (400.0, 38)]
            assert series[1089].prior_count is None
            assert series[1089].samples == ()
            store.close()

    def test_feast_stock_series_orders_samples_by_time(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RaffleStore(str(Path(directory) / "raffle.db"), "guild-id")
            store.record_feast_counts({1102: 30}, 500.0)
            store.record_feast_counts({1102: 25}, 300.0)
            store.record_feast_counts({1102: 20}, 400.0)

            series = store.get_feast_stock_series(since=0.0)

            assert [
                sample.recorded_at for sample in series[1102].samples
            ] == [300.0, 400.0, 500.0]
            assert series[1102].prior_count is None
            store.close()
