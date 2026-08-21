import logging
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
from sqlalchemy.exc import SQLAlchemyError

from factories import (
    gold_deposit,
    gold_withdrawal,
    guild_join,
    settings_interaction,
    settings_reply,
)
from gw2bot.bot import Gw2Bot
from gw2bot.config import DEFAULT_RAFFLE_OFFICER_ROLE_ID, Config
from gw2bot.gold import (
    DEPOSIT,
    GOLD_RANGES,
    WITHDRAW,
    GoldBalanceSample,
    GoldLedgerEntry,
    GoldMovement,
    GoldSeries,
    build_gold_series,
)
from gw2bot.gold.commands import GoldCommands
from gw2bot.gold.import_log import format_import_result, import_gold_history
from gw2bot.gold.models import GoldImportResult
from gw2bot.guild_stash import stash_coin_balance
from gw2bot.raffle import COPPER_PER_GOLD, RaffleStore
from gw2bot.raffle.events import parse_gold_withdrawal, parse_stash_coin_movement
from gw2bot.raffle.models import GoldWithdrawal

NOW = 1_800_000_000.0
# The guild log's own timestamp spelling, which is what the ledger stores.
EVENT_TIME = "2026-06-07T06:26:17.000Z"
EVENT_MOMENT = 1_780_813_577.0


@pytest.fixture
def store(tmp_path: Path):
    store = RaffleStore(str(tmp_path / "gw2bot.db"), "guild-id")
    store.initialize_cursor(0)
    yield store
    store.close()


def movement(
    offset: float,
    operation: str = DEPOSIT,
    username: str = "Member.1234",
    gold: float = 1,
) -> GoldMovement:
    return GoldMovement(
        occurred_at=NOW + offset,
        operation=operation,
        username=username,
        coins=int(gold * COPPER_PER_GOLD),
    )


def anchor(offset: float, gold: float) -> GoldBalanceSample:
    return GoldBalanceSample(
        recorded_at=NOW + offset,
        coins=int(gold * COPPER_PER_GOLD),
    )


def balances(series: GoldSeries) -> list[float]:
    return [point.coins / COPPER_PER_GOLD for point in series.points]


def plotted(series: GoldSeries) -> list[float | None]:
    """The balance each plotted movement left the bank at, in gold."""
    return [
        None if movement.coins_after is None
        else movement.coins_after / COPPER_PER_GOLD
        for movement in series.movements
    ]


def balance_log_rows(tmp_path: Path) -> int:
    """How many rows the stash balance log holds, read outside the store.

    The store deliberately offers no way to count them - nothing in the bot
    needs to - so the claim that an unchanged poll adds no row is checked
    against the database itself.
    """
    with sqlite3.connect(tmp_path / "gw2bot.db") as connection:
        return connection.execute(
            "SELECT COUNT(*) FROM guild_stash_balance_log"
        ).fetchone()[0]


class TestParseGoldWithdrawal:
    def test_reads_a_withdrawal_of_coins(self) -> None:
        parsed = parse_gold_withdrawal(
            cast(dict[str, Any], gold_withdrawal(101, "Officer.5678", 50_000))
        )

        assert parsed == GoldWithdrawal(
            event_id=101,
            username="Officer.5678",
            coins_withdrawn=50_000,
            event_time=EVENT_TIME,
        )

    def test_ignores_a_deposit(self) -> None:
        assert parse_gold_withdrawal(
            cast(dict[str, Any], gold_deposit(101))
        ) is None

    def test_ignores_an_item_withdrawal(self) -> None:
        # Taking an item out is a stash event too, and reports no coins.
        event = dict(gold_withdrawal(101), coins=0, item_id=1234, count=1)
        assert parse_gold_withdrawal(cast(dict[str, Any], event)) is None

    def test_ignores_everything_that_is_not_a_stash_event(self) -> None:
        assert parse_gold_withdrawal(
            cast(dict[str, Any], guild_join(101))
        ) is None

    def test_names_the_account_and_the_amount(self) -> None:
        withdrawal = GoldWithdrawal(
            event_id=101,
            username="Officer.5678",
            coins_withdrawn=125_000,
            event_time=EVENT_TIME,
        )

        assert withdrawal.message == (
            "Officer.5678 withdrew 12.5 gold from the guild bank."
        )


class TestParseStashCoinMovement:
    def test_reads_both_directions(self) -> None:
        deposit = parse_stash_coin_movement(
            cast(dict[str, Any], gold_deposit(101, "Member.1234", 20_000))
        )
        withdrawal = parse_stash_coin_movement(
            cast(dict[str, Any], gold_withdrawal(102, "Officer.5678", 30_000))
        )

        assert deposit == GoldLedgerEntry(
            event_id=101,
            username="Member.1234",
            operation=DEPOSIT,
            coins=20_000,
            event_time=EVENT_TIME,
        )
        assert withdrawal == GoldLedgerEntry(
            event_id=102,
            username="Officer.5678",
            operation=WITHDRAW,
            coins=30_000,
            event_time=EVENT_TIME,
        )

    def test_ignores_anything_that_moves_no_coins(self) -> None:
        assert parse_stash_coin_movement(
            cast(dict[str, Any], guild_join(101))
        ) is None


class TestStashCoinBalance:
    def test_sums_every_vault_section(self) -> None:
        # The guild log never says which section a deposit reached, so the
        # page tracks the bank rather than any one tab.
        assert stash_coin_balance(
            [{"coins": 10_000}, {"coins": 5_000}, {"coins": 0}]
        ) == 15_000

    def test_an_empty_stash_holds_nothing(self) -> None:
        assert stash_coin_balance([]) == 0

    def test_an_unreadable_section_is_skipped_not_fatal(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="gw2bot"):
            total = stash_coin_balance(
                [{"coins": 10_000}, {"coins": "nonsense"}, {}]
            )

        assert total == 10_000
        assert "1 guild stash section(s)" in caplog.text


class TestBuildGoldSeries:
    def test_balances_are_measured_backwards_from_the_anchor(self) -> None:
        # The anchor is the only balance anybody observed; everything before
        # it is recovered by unwinding the movements between the two.
        series = build_gold_series(
            [movement(-300, DEPOSIT, gold=50), movement(-200, WITHDRAW, gold=20)],
            anchor(-100, 130),
            NOW - 400,
            NOW,
        )

        assert plotted(series) == [150, 130]
        assert balances(series)[0] == 100

    def test_balances_are_measured_forwards_past_the_anchor(self) -> None:
        series = build_gold_series(
            [movement(-50, WITHDRAW, gold=25)],
            anchor(-100, 130),
            NOW - 400,
            NOW,
        )

        assert plotted(series) == [105]
        assert balances(series)[-1] == 105

    def test_a_quiet_window_is_a_flat_line_at_the_current_balance(self) -> None:
        series = build_gold_series([], anchor(-100, 130), NOW - 400, NOW)

        assert balances(series) == [130, 130]
        assert series.movements == ()

    def test_without_an_observed_balance_nothing_is_placed_on_the_axis(
        self,
    ) -> None:
        # A movement says how much gold moved, never how much there was. With
        # no reading to measure against, the line is left undrawn rather than
        # invented.
        series = build_gold_series(
            [movement(-100, DEPOSIT, gold=5)],
            None,
            NOW - 400,
            NOW,
        )

        assert series.points == ()
        assert plotted(series) == [None]

    def test_a_window_that_ends_early_unwinds_the_movements_after_it(
        self,
    ) -> None:
        # A picked window closing in the past sits behind the anchor, so the
        # movements between the two are exactly what has to be taken back off.
        series = build_gold_series(
            [movement(-100, WITHDRAW, gold=40)],
            anchor(-50, 60),
            NOW - 400,
            NOW - 200,
        )

        assert series.movements == ()
        assert balances(series) == [100, 100]

    def test_movements_outside_the_window_are_not_plotted(self) -> None:
        series = build_gold_series(
            [movement(-500, DEPOSIT, gold=10), movement(-100, DEPOSIT, gold=5)],
            anchor(-10, 115),
            NOW - 400,
            NOW,
        )

        assert len(series.movements) == 1
        # The one before the window still places the line's opening balance.
        assert balances(series)[0] == 110

    def test_totals_each_direction_and_the_net(self) -> None:
        series = build_gold_series(
            [
                movement(-300, DEPOSIT, gold=50),
                movement(-200, WITHDRAW, gold=20),
                movement(-100, DEPOSIT, gold=5),
            ],
            anchor(-50, 135),
            NOW - 400,
            NOW,
        )

        assert series.deposited == 55 * COPPER_PER_GOLD
        assert series.withdrawn == 20 * COPPER_PER_GOLD
        assert series.net == 35 * COPPER_PER_GOLD

    def test_a_net_can_run_the_other_way(self) -> None:
        series = build_gold_series(
            [movement(-100, WITHDRAW, gold=30)],
            anchor(-50, 70),
            NOW - 400,
            NOW,
        )

        assert series.net == -30 * COPPER_PER_GOLD


class TestStashBalanceLog:
    def test_records_only_a_changed_balance(
        self,
        store: RaffleStore,
        tmp_path: Path,
    ) -> None:
        assert store.record_stash_balance(10_000, NOW)
        assert not store.record_stash_balance(10_000, NOW + 60)
        assert store.record_stash_balance(20_000, NOW + 120)

        assert balance_log_rows(tmp_path) == 2

    def test_an_unchanged_balance_moves_the_anchor_forward(
        self,
        store: RaffleStore,
    ) -> None:
        # The refresh is the point: it puts a stretch of movements the guild
        # log dropped behind the anchor, where they can no longer be replayed
        # on top of a balance that already holds them.
        store.record_stash_balance(10_000, NOW)
        store.record_stash_balance(10_000, NOW + 600)

        sample = store.get_last_stash_balance()
        assert sample == GoldBalanceSample(recorded_at=NOW + 600, coins=10_000)

    def test_the_anchor_never_moves_backwards(
        self,
        store: RaffleStore,
    ) -> None:
        # A host clock stepped back between two polls would otherwise backdate
        # the anchor, and every movement since the last reading would be
        # replayed on top of a balance that already includes it.
        store.record_stash_balance(10_000, NOW)
        store.record_stash_balance(20_000, NOW - 600)

        sample = store.get_last_stash_balance()
        assert sample is not None
        assert sample.recorded_at == NOW
        assert sample.coins == 20_000

    def test_reports_no_sample_before_the_first_poll(
        self,
        store: RaffleStore,
    ) -> None:
        assert store.get_last_stash_balance() is None


class TestGoldMovementStore:
    def test_the_poller_records_both_directions(
        self,
        store: RaffleStore,
    ) -> None:
        store.process_events(
            [
                cast(dict[str, Any], gold_deposit(101, "Member.1234", 20_000)),
                cast(
                    dict[str, Any],
                    gold_withdrawal(102, "Officer.5678", 30_000),
                ),
            ]
        )

        assert store.get_gold_movements(0.0) == [
            GoldMovement(EVENT_MOMENT, DEPOSIT, "Member.1234", 20_000),
            GoldMovement(EVENT_MOMENT, WITHDRAW, "Officer.5678", 30_000),
        ]

    def test_a_deposit_the_raffle_turned_away_still_reaches_the_ledger(
        self,
        store: RaffleStore,
    ) -> None:
        # An Officer's oversized deposit buys no tickets and is never recorded
        # as a raffle deposit, but the gold went into the bank all the same.
        store.process_events(
            [cast(dict[str, Any], gold_deposit(101, "Officer.5678", 500_000))],
            {"Officer.5678"},
        )

        assert store.get_totals() == []
        assert store.get_gold_movements(0.0) == [
            GoldMovement(EVENT_MOMENT, DEPOSIT, "Officer.5678", 500_000)
        ]

    def test_only_a_withdrawal_waits_to_be_announced(
        self,
        store: RaffleStore,
    ) -> None:
        # A deposit already has the raffle's own embed; announcing it here too
        # would say the same thing twice.
        store.process_events(
            [
                cast(dict[str, Any], gold_deposit(101)),
                cast(
                    dict[str, Any],
                    gold_withdrawal(102, "Officer.5678", 30_000),
                ),
            ]
        )

        pending = store.get_pending_gold_withdrawal_notifications()
        assert [item.event_id for item in pending] == [102]

    def test_an_announced_withdrawal_stops_being_pending(
        self,
        store: RaffleStore,
    ) -> None:
        store.process_events(
            [cast(dict[str, Any], gold_withdrawal(102, "Officer.5678"))]
        )

        store.mark_gold_withdrawal_notification_sent(102)

        assert store.get_pending_gold_withdrawal_notifications() == []

    def test_drops_movements_before_the_window(
        self,
        store: RaffleStore,
    ) -> None:
        store.process_events([cast(dict[str, Any], gold_deposit(101))])

        assert store.get_gold_movements(EVENT_MOMENT + 1) == []

    def test_a_row_with_an_unreadable_time_is_dropped(
        self,
        store: RaffleStore,
    ) -> None:
        # Placing it would mean guessing when it happened, and every balance
        # after it would inherit the guess.
        event = dict(gold_deposit(101), time="not a timestamp")
        store.process_events([cast(dict[str, Any], event)])

        assert store.get_gold_movements(0.0) == []


class TestImportGoldMovements:
    def test_imported_movements_are_never_announced(
        self,
        store: RaffleStore,
    ) -> None:
        # A withdrawal from months ago is history, not news.
        store.import_gold_movements(
            [
                GoldLedgerEntry(
                    102, "Officer.5678", WITHDRAW, 30_000, EVENT_TIME
                )
            ]
        )

        assert store.get_pending_gold_withdrawal_notifications() == []
        assert len(store.get_gold_movements(0.0)) == 1

    def test_importing_the_same_log_twice_adds_nothing(
        self,
        store: RaffleStore,
    ) -> None:
        entries = [
            GoldLedgerEntry(101, "Member.1234", DEPOSIT, 20_000, EVENT_TIME)
        ]

        assert store.import_gold_movements(entries) == 1
        assert store.import_gold_movements(entries) == 0
        assert len(store.get_gold_movements(0.0)) == 1

    def test_the_poller_leaves_an_imported_row_alone(
        self,
        store: RaffleStore,
    ) -> None:
        # Both write by the guild log's own event id, so whichever gets there
        # first owns the row - and the withdrawal is not announced late.
        store.import_gold_movements(
            [
                GoldLedgerEntry(
                    102, "Officer.5678", WITHDRAW, 30_000, EVENT_TIME
                )
            ]
        )

        store.process_events(
            [cast(dict[str, Any], gold_withdrawal(102, "Officer.5678", 30_000))]
        )

        assert len(store.get_gold_movements(0.0)) == 1
        assert store.get_pending_gold_withdrawal_notifications() == []


class TestGoldWithdrawalNotifications:
    async def test_announces_each_pending_withdrawal_once(self) -> None:
        withdrawal = GoldWithdrawal(102, "Officer.5678", 30_000, EVENT_TIME)
        store = MagicMock()
        store.get_pending_gold_withdrawal_notifications.return_value = [
            withdrawal
        ]
        bot = SimpleNamespace(
            _raffle_store=store,
            _try_send_notification=AsyncMock(return_value=True),
        )

        await Gw2Bot._send_pending_gold_withdrawal_notifications(
            cast(Gw2Bot, bot)
        )

        bot._try_send_notification.assert_awaited_once_with(
            "Officer.5678 withdrew 3 gold from the guild bank."
        )
        store.mark_gold_withdrawal_notification_sent.assert_called_once_with(
            102
        )

    async def test_a_failed_delivery_stays_pending(self) -> None:
        withdrawal = GoldWithdrawal(102, "Officer.5678", 30_000, EVENT_TIME)
        store = MagicMock()
        store.get_pending_gold_withdrawal_notifications.return_value = [
            withdrawal
        ]
        bot = SimpleNamespace(
            _raffle_store=store,
            _try_send_notification=AsyncMock(return_value=False),
        )

        await Gw2Bot._send_pending_gold_withdrawal_notifications(
            cast(Gw2Bot, bot)
        )

        store.mark_gold_withdrawal_notification_sent.assert_not_called()


class TestRecordGuildStashBalance:
    async def test_reads_the_stash_and_logs_what_it_holds(
        self,
        store: RaffleStore,
    ) -> None:
        api = SimpleNamespace(
            get_guild_stash=AsyncMock(
                return_value=[{"coins": 10_000}, {"coins": 5_000}]
            )
        )
        bot = SimpleNamespace(
            _api=api,
            _raffle_store=store,
            _config=SimpleNamespace(gw2_guild_id="guild-id"),
        )

        coins = await Gw2Bot._record_guild_stash_balance(cast(Gw2Bot, bot))

        assert coins == 15_000
        sample = store.get_last_stash_balance()
        assert sample is not None
        assert sample.coins == 15_000

    async def test_logging_names_no_balance(
        self,
        store: RaffleStore,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # The guild's holdings are the page's whole subject; the console has
        # no business carrying them.
        api = SimpleNamespace(
            get_guild_stash=AsyncMock(return_value=[{"coins": 4_242_424}])
        )
        bot = SimpleNamespace(
            _api=api,
            _raffle_store=store,
            _config=SimpleNamespace(gw2_guild_id="guild-id"),
        )

        with caplog.at_level(logging.DEBUG, logger="gw2bot"):
            await Gw2Bot._record_guild_stash_balance(cast(Gw2Bot, bot))

        assert "4242424" not in caplog.text
        assert "4,242,424" not in caplog.text
        assert "Read the guild stash balance; sections=1" in caplog.text


class TestImportGoldHistory:
    def _bot(self, store: RaffleStore, events: list[Any], stash: list[Any]) -> Any:
        api = SimpleNamespace(
            get_guild_log=AsyncMock(return_value=events),
            get_guild_stash=AsyncMock(return_value=stash),
        )
        return SimpleNamespace(
            _api=api,
            _raffle_store=store,
            _config=SimpleNamespace(gw2_guild_id="guild-id"),
        )

    async def test_reads_the_whole_log_not_the_slice_after_the_cursor(
        self,
        store: RaffleStore,
    ) -> None:
        # The poller's cursor names where it got to; the import is meant to
        # reach behind it, so it asks for the log without a `since`.
        bot = self._bot(store, [], [{"coins": 10_000}])

        await import_gold_history(cast(Gw2Bot, bot))

        bot._api.get_guild_log.assert_awaited_once_with("guild-id")

    async def test_stores_every_coin_movement_and_the_balance(
        self,
        store: RaffleStore,
    ) -> None:
        bot = self._bot(
            store,
            [
                gold_deposit(101, "Member.1234", 20_000),
                gold_withdrawal(102, "Officer.5678", 30_000),
                guild_join(103),
            ],
            [{"coins": 900_000}],
        )

        result = await import_gold_history(cast(Gw2Bot, bot))

        assert result.fetched == 3
        assert result.matched == 2
        assert result.imported == 2
        assert result.balance_coins == 900_000
        assert result.balance_recorded
        assert len(store.get_gold_movements(0.0)) == 2

    async def test_a_second_run_adds_only_what_is_new(
        self,
        store: RaffleStore,
    ) -> None:
        events = [gold_deposit(101, "Member.1234", 20_000)]
        bot = self._bot(store, events, [{"coins": 900_000}])
        await import_gold_history(cast(Gw2Bot, bot))

        again = await import_gold_history(cast(Gw2Bot, bot))

        assert again.imported == 0
        assert again.duplicates == 1
        assert len(store.get_gold_movements(0.0)) == 1

    async def test_an_imported_history_reads_back_as_a_line(
        self,
        store: RaffleStore,
    ) -> None:
        # What the operator actually does, end to end: import the log, then
        # let the page walk back from the balance the API reported.
        bot = self._bot(
            store,
            [
                gold_deposit(101, "Member.1234", 500_000),
                gold_withdrawal(102, "Officer.5678", 200_000),
            ],
            [{"coins": 900_000}],
        )
        await import_gold_history(cast(Gw2Bot, bot))

        series = build_gold_series(
            store.get_gold_movements(0.0),
            store.get_last_stash_balance(),
            EVENT_MOMENT - 100,
            EVENT_MOMENT + 100,
        )

        # Both movements share a moment, so the pair is unwound together: the
        # bank held 600,000 before them and 900,000 after.
        assert series.points[0].coins == 600_000
        assert series.points[-1].coins == 900_000
        assert series.deposited == 500_000
        assert series.withdrawn == 200_000

    async def test_logging_names_no_account_or_balance(
        self,
        store: RaffleStore,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        bot = self._bot(
            store,
            [gold_deposit(101, "Secret.1234", 4_242_424)],
            [{"coins": 4_242_424}],
        )

        with caplog.at_level(logging.DEBUG, logger="gw2bot"):
            await import_gold_history(cast(Gw2Bot, bot))

        assert "Secret.1234" not in caplog.text
        assert "4242424" not in caplog.text
        assert "Guild bank history import finished" in caplog.text


class TestFormatImportResult:
    def test_reports_what_it_did(self) -> None:
        message = format_import_result(
            GoldImportResult(
                fetched=40,
                matched=6,
                imported=4,
                duplicates=2,
                balance_coins=1_234_500,
                balance_recorded=True,
            )
        )

        assert "Read 40 guild log event(s)" in message
        assert "found 6 coin movement(s)" in message
        assert "Imported 4 movement(s)" in message
        assert "2 were already recorded" in message
        assert "The guild bank holds 123.45 gold" in message

    def test_says_nothing_about_duplicates_when_there_were_none(self) -> None:
        message = format_import_result(
            GoldImportResult(
                fetched=1,
                matched=1,
                imported=1,
                duplicates=0,
                balance_coins=0,
                balance_recorded=False,
            )
        )

        assert "already recorded" not in message
        # The ceiling is the point of the closing sentence: the guild log
        # keeps only its most recent events, so the rest is unrecoverable.
        assert "only keeps its most recent events" in message


class GoldCommandBot:
    def __init__(
        self,
        store: RaffleStore,
        api: Any = None,
        gw2_api_enabled: bool = True,
        **overrides: Any,
    ):
        self._config = Config(
            discord_token="discord-token",
            discord_command_guild_id=5678,
            **overrides,
        )
        self._api = api
        self._raffle_store = store
        self.raffle_store = store
        self.gw2_api_enabled = gw2_api_enabled
        self.reject_without_gw2_api = AsyncMock(
            return_value=not gw2_api_enabled
        )


def gold_commands(bot: Any) -> GoldCommands:
    return GoldCommands(cast(Gw2Bot, bot))


class TestGoldImportCommand:
    def _api(self, **overrides: Any) -> Any:
        values: dict[str, Any] = {
            "get_guild_log": AsyncMock(return_value=[gold_deposit(101)]),
            "get_guild_stash": AsyncMock(return_value=[{"coins": 900_000}]),
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    async def test_rejects_a_caller_without_the_officer_role(
        self,
        store: RaffleStore,
    ) -> None:
        bot = GoldCommandBot(store, self._api(), gw2_guild_id="guild-id")
        interaction = settings_interaction()

        await gold_commands(bot)._handle_import(interaction)

        assert "do not have the required role" in settings_reply(interaction)
        assert store.get_gold_movements(0.0) == []

    async def test_reports_an_unconfigured_api(
        self,
        store: RaffleStore,
    ) -> None:
        bot = GoldCommandBot(store, None, gw2_api_enabled=False)
        interaction = settings_interaction(
            role_ids=(DEFAULT_RAFFLE_OFFICER_ROLE_ID,)
        )

        await gold_commands(bot)._handle_import(interaction)

        bot.reject_without_gw2_api.assert_awaited_once()
        assert store.get_gold_movements(0.0) == []

    async def test_imports_and_reports_the_result(
        self,
        store: RaffleStore,
    ) -> None:
        bot = GoldCommandBot(store, self._api(), gw2_guild_id="guild-id")
        interaction = settings_interaction(
            role_ids=(DEFAULT_RAFFLE_OFFICER_ROLE_ID,)
        )

        await gold_commands(bot)._handle_import(interaction)

        assert "Imported 1 movement(s)" in settings_reply(interaction)
        assert len(store.get_gold_movements(0.0)) == 1

    async def test_reports_an_api_failure_without_raising(
        self,
        store: RaffleStore,
    ) -> None:
        bot = GoldCommandBot(
            store,
            self._api(
                get_guild_log=AsyncMock(side_effect=aiohttp.ClientError())
            ),
            gw2_guild_id="guild-id",
        )
        interaction = settings_interaction(
            role_ids=(DEFAULT_RAFFLE_OFFICER_ROLE_ID,)
        )

        await gold_commands(bot)._handle_import(interaction)

        assert "Guild Wars 2 API could not be read" in settings_reply(
            interaction
        )

    async def test_reports_a_database_failure_without_raising(
        self,
        store: RaffleStore,
    ) -> None:
        failing = MagicMock(wraps=store)
        failing.import_gold_movements.side_effect = SQLAlchemyError("no")
        bot = GoldCommandBot(
            cast(RaffleStore, failing),
            self._api(),
            gw2_guild_id="guild-id",
        )
        interaction = settings_interaction(
            role_ids=(DEFAULT_RAFFLE_OFFICER_ROLE_ID,)
        )

        await gold_commands(bot)._handle_import(interaction)

        assert "could not be written to the database" in settings_reply(
            interaction
        )


class TestGoldRanges:
    def test_offers_a_day_a_week_and_a_month(self) -> None:
        assert GOLD_RANGES == {
            "24h": 24 * 60 * 60,
            "7d": 7 * 24 * 60 * 60,
            "30d": 30 * 24 * 60 * 60,
        }
