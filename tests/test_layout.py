"""The import boundaries the package layout depends on.

`core/` is the layer every feature is allowed to import. What keeps it from
decaying into a junk drawer is that the dependency only ever points one way,
so this asserts it rather than leaving it to review.
"""

from __future__ import annotations

import ast
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parent.parent / "src" / "gw2bot"
CORE_ROOT = SOURCE_ROOT / "core"


def _imported_gw2bot_modules(path: Path) -> set[str]:
    """Every `gw2bot.*` module the file at `path` imports."""

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(
                alias.name for alias in node.names if alias.name.startswith("gw2bot")
            )
        elif isinstance(node, ast.ImportFrom):
            # A relative import inside core/ can only reach core/, so the
            # absolute ones are the whole question here.
            if node.level == 0 and node.module and node.module.startswith("gw2bot"):
                imported.add(node.module)
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
            f"import a feature package back: {offenders}"
        )

    def test_core_is_not_empty(self) -> None:
        # Guards the test above against silently passing if core/ is moved or
        # renamed and the glob stops matching anything.
        modules = [
            path for path in CORE_ROOT.rglob("*.py") if path.name != "__init__.py"
        ]
        assert len(modules) >= 5
