"""The agent instruction files and the reference files they point at.

`CLAUDE.md` and `AGENTS.md` are loaded automatically every session, so they
hold only what must always be in context: the instruction priority, the
constraints, and an index of everything else. The rest lives in `docs/`, read
on demand.

Two things have to hold for that to work, and neither is visible in review: the
two files must stay identical, and every file the index points at must exist.
A pointer to a missing file loses its guidance silently, which is worse than
having written none.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CLAUDE_MD = REPO_ROOT / ".claude" / "CLAUDE.md"
AGENTS_MD = REPO_ROOT / "AGENTS.md"
DOCS_ROOT = REPO_ROOT / "docs"

# Sections that may never be moved out into a reference file: an agent has to
# have them whatever it is working on.
ALWAYS_INLINE = ("## Instruction priority", "## Constraints (hard rules)")


def _referenced_docs(text: str) -> set[str]:
    """Every `docs/<name>.md` path mentioned in `text`."""

    return set(re.findall(r"docs/[\w./-]+\.md", text))


class TestTheTwoFilesStayMirrors:
    def test_claude_md_and_agents_md_are_identical(self) -> None:
        # The repository states this as a hard rule. Nothing enforced it, so
        # the two drifted only as far as someone remembered to diff them.
        assert CLAUDE_MD.read_text(encoding="utf-8") == AGENTS_MD.read_text(
            encoding="utf-8"
        ), (
            "CLAUDE.md and AGENTS.md must be mirrors of each other; a change to "
            "one is a change to the other."
        )


class TestTheIndexResolves:
    def test_every_referenced_doc_exists(self) -> None:
        referenced = _referenced_docs(CLAUDE_MD.read_text(encoding="utf-8"))
        missing = sorted(
            name for name in referenced if not (REPO_ROOT / name).is_file()
        )
        assert missing == [], (
            f"the agent files point at reference files that do not exist: "
            f"{missing}. A dead pointer loses its guidance without saying so."
        )

    def test_every_doc_is_referenced(self) -> None:
        referenced = _referenced_docs(CLAUDE_MD.read_text(encoding="utf-8"))
        orphans = sorted(
            path.relative_to(REPO_ROOT).as_posix()
            for path in DOCS_ROOT.glob("*.md")
            if path.relative_to(REPO_ROOT).as_posix() not in referenced
        )
        assert orphans == [], (
            f"nothing points at {orphans}, so no agent will read them. Add a "
            "row to the reference table saying when each one is worth reading."
        )

    def test_the_index_is_not_empty(self) -> None:
        # Guards the two tests above from passing because the table was
        # deleted or its paths were rewritten into some other shape.
        assert len(_referenced_docs(CLAUDE_MD.read_text(encoding="utf-8"))) >= 5


class TestWhatMustStayInline:
    def test_priority_and_constraints_are_never_moved_out(self) -> None:
        for path in (CLAUDE_MD, AGENTS_MD):
            text = path.read_text(encoding="utf-8")
            for heading in ALWAYS_INLINE:
                assert heading in text, (
                    f"{path.name} must carry {heading!r} inline. These are read "
                    "every session and may not be moved into docs/."
                )

    def test_the_constraints_are_spelled_out_not_referenced(self) -> None:
        # A constraints section that had been reduced to a pointer would pass
        # the heading check above while holding nothing.
        text = CLAUDE_MD.read_text(encoding="utf-8")
        start = text.index("## Constraints (hard rules)")
        end = text.index("\n## ", start + 1)
        body = [
            line for line in text[start:end].splitlines() if line.startswith("- ")
        ]
        assert len(body) >= 3, (
            "the constraints must be written out in full in CLAUDE.md, not "
            f"replaced by a reference; found {len(body)} of them."
        )
