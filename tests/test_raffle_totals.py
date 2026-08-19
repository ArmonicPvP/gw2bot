import tempfile
from pathlib import Path
from unittest.mock import patch

from gw2bot.raffle import RaffleStore
from gw2bot.raffle_totals import main as run_totals


class TestRaffleTotalsCommand:
    """The documented `python -m gw2bot.raffle_totals` utility.

    It used to require GW2_GUILD_ID, which the migration tells operators to
    delete, and a fresh install never has at all. Reading a ledger needs no
    guild id: the database already records which guild it belongs to.
    """

    def _database(self, directory: str, guild_id: str | None) -> str:
        path = str(Path(directory) / "gw2bot.db")
        store = RaffleStore(path, guild_id)
        store.add_manual_ticket("Member.1234")
        store.close()
        return path

    def test_reads_totals_without_the_legacy_variable(
        self,
        capsys: object,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._database(directory, "guild-id")

            with patch.dict("os.environ", {"RAFFLE_DB_PATH": path}, clear=True):
                run_totals()

        output = capsys.readouterr().out  # type: ignore[attr-defined]
        assert "Member.1234: 1 current tickets" in output

    def test_reads_a_ledger_no_guild_has_claimed(self, capsys: object) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._database(directory, None)

            with patch.dict("os.environ", {"RAFFLE_DB_PATH": path}, clear=True):
                run_totals()

        assert "Member.1234" in capsys.readouterr().out  # type: ignore[attr-defined]

    def test_reports_an_empty_ledger(self, capsys: object) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "gw2bot.db")
            RaffleStore(path, "guild-id").close()

            with patch.dict("os.environ", {"RAFFLE_DB_PATH": path}, clear=True):
                run_totals()

        assert "No raffle totals recorded." in capsys.readouterr().out  # type: ignore[attr-defined]
