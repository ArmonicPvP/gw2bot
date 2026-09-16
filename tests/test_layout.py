"""The import boundaries the package layout depends on.

`core/` is the layer every feature is allowed to import. What keeps it from
decaying into a junk drawer is that the dependency only ever points one way,
so this asserts it rather than leaving it to review.

"The `core/` Boundary" in CLAUDE.md and AGENTS.md says what belongs there and
what to do instead when something in `core/` looks like it needs a feature.
"""

from __future__ import annotations

import ast
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parent.parent / "src" / "gw2bot"
CORE_ROOT = SOURCE_ROOT / "core"


def _imported_gw2bot_modules(
    path: Path,
    root: Path = SOURCE_ROOT.parent,
) -> set[str]:
    """Every module the file at `path` imports, relative spellings resolved.

    A relative import is resolved against the file's own package rather than
    trusted: `from ..config import Config` leaves `core/` exactly as surely as
    `from gw2bot.config import Config` does, and only the spelling differs.

    `root` is the directory the package tree starts in, which the tests below
    override to resolve a file that is not in this repository.
    """

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    # A module's package is the directory holding it, and a package's
    # `__init__` is that package, so both resolve against the same parts.
    package = path.relative_to(root).parts[:-1]
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(
                alias.name for alias in node.names if alias.name.startswith("gw2bot")
            )
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                if node.module and node.module.startswith("gw2bot"):
                    imported.add(node.module)
                continue
            # Each dot climbs one package: the first stays in the file's own,
            # and every dot after that goes up one more.
            climbed = package[: len(package) - (node.level - 1)]
            if not climbed:
                # More dots than there are packages to climb. Python refuses
                # that at import time, so record it as written rather than
                # invent a target for it.
                imported.add("." * node.level + (node.module or ""))
                continue
            resolved = ".".join(
                (*climbed, node.module) if node.module else climbed
            )
            if resolved.startswith("gw2bot"):
                imported.add(resolved)
    return imported


class TestCoreIsSelfContained:
    def test_core_never_imports_a_feature_package(self) -> None:
        offenders: dict[str, set[str]] = {}
        for path in sorted(CORE_ROOT.rglob("*.py")):
            outside = {
                module
                for module in _imported_gw2bot_modules(path)
                if module != "gw2bot.core" and not module.startswith("gw2bot.core.")
            }
            if outside:
                offenders[path.relative_to(SOURCE_ROOT).as_posix()] = outside

        assert offenders == {}, (
            "core/ holds the shared layer every feature imports, so it may not "
            f"import anything else under gw2bot back: {offenders}. See "
            '"The `core/` Boundary" in CLAUDE.md for what to do instead.'
        )

    def test_core_is_not_empty(self) -> None:
        # Guards the test above against silently passing if core/ is moved or
        # renamed and the glob stops matching anything.
        modules = [
            path for path in CORE_ROOT.rglob("*.py") if path.name != "__init__.py"
        ]
        assert len(modules) >= 5


class TestRelativeImportsAreResolved:
    """A relative import must be judged by where it lands, not how it reads.

    `from ..config import Config` inside `core/` escapes the boundary, so the
    resolver has to follow the dots rather than wave relative spellings past.
    """

    def _imports(self, tmp_path: Path, source: str) -> set[str]:
        module = tmp_path / "gw2bot" / "core" / "probe.py"
        module.parent.mkdir(parents=True, exist_ok=True)
        module.write_text(source, encoding="utf-8")
        return _imported_gw2bot_modules(module, root=tmp_path)

    def test_one_dot_stays_inside_core(self, tmp_path: Path) -> None:
        assert self._imports(tmp_path, "from .database import engine\n") == {
            "gw2bot.core.database"
        }

    def test_one_dot_without_a_module_is_the_package_itself(
        self, tmp_path: Path
    ) -> None:
        assert self._imports(tmp_path, "from . import database\n") == {
            "gw2bot.core"
        }

    def test_two_dots_escape_core_and_are_reported(self, tmp_path: Path) -> None:
        # The case that slipped through while only `level == 0` was checked.
        assert self._imports(tmp_path, "from ..config import Config\n") == {
            "gw2bot.config"
        }

    def test_two_dots_reach_a_feature_package(self, tmp_path: Path) -> None:
        assert self._imports(tmp_path, "from ..raffle.models import Total\n") == {
            "gw2bot.raffle.models"
        }

    def test_more_dots_than_packages_is_recorded_as_written(
        self, tmp_path: Path
    ) -> None:
        # Python refuses this outright, so there is no target to resolve; it
        # is reported by its spelling so the failure still names the line.
        assert self._imports(tmp_path, "from ...elsewhere import thing\n") == {
            "...elsewhere"
        }

    def test_an_escaping_relative_import_fails_the_boundary_check(
        self, tmp_path: Path
    ) -> None:
        # The resolved names above only matter because the boundary check
        # rejects them, so assert the two halves meet.
        escaping = self._imports(tmp_path, "from ..config import Config\n")
        outside = {
            module
            for module in escaping
            if module != "gw2bot.core" and not module.startswith("gw2bot.core.")
        }
        assert outside == {"gw2bot.config"}
