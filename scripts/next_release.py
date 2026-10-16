"""Decide the next release from what was merged since the last one.

`.github/workflows/release.yml` runs `plan` once CI passes for a push to
`main`, and CI runs `check-branch` on every pull request. Both read the same
table, so the branch names CI accepts are exactly the ones a release knows how
to number:

    feat/<name>   new behaviour a member or operator can use   MINOR
    fix/<name>    a backwards-compatible bug fix               PATCH
    chore/<name>  nothing a member or operator would notice    no release

A merged pull request labelled `breaking` is a MAJOR release whatever its
branch. `docs/versioning.md` says what counts as breaking.

The version lives only in the `vX.Y.Z` tags this cuts. Nothing is committed to
`main`, so the release needs no way around the branch ruleset, and a run that
was cancelled or never started is caught up by the next one: each run releases
everything merged since the newest tag, not only the commit that started it.

Only the standard library is used, so neither job installs anything first.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import Any, Final


class Bump(IntEnum):
    """How far a change moves the version, ordered so the largest one wins."""

    NONE = 0
    PATCH = 1
    MINOR = 2
    MAJOR = 3


BRANCH_BUMPS: Final[Mapping[str, Bump]] = {
    "feat": Bump.MINOR,
    "fix": Bump.PATCH,
    "chore": Bump.NONE,
}
# Lowercase words joined by single hyphens, the way `feat/profit-page` is
# written. One level only, so the prefix is always the whole of the first
# segment.
BRANCH_NAME: Final = re.compile(r"(?P<prefix>[a-z]+)/[a-z0-9]+(?:-[a-z0-9]+)*")
BREAKING_LABEL: Final = "breaking"
# A merged pull request whose branch has none of the prefixes still changed
# what ships, so it goes out as the smallest release rather than waiting,
# unnumbered, for the next one that does.
UNPREFIXED_BUMP: Final = Bump.PATCH
# Only plain releases count. A pre-release or any other tag is left alone, so
# a hand-made `v2.0.0-rc.1` never becomes the base the next bump starts from.
RELEASE_TAG: Final = re.compile(r"v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")


@dataclass(frozen=True, order=True)
class Version:
    major: int
    minor: int
    patch: int

    @classmethod
    def from_tag(cls, tag: str) -> Version | None:
        match = RELEASE_TAG.fullmatch(tag)
        if match is None:
            return None
        major, minor, patch = (int(part) for part in match.groups())
        return cls(major, minor, patch)

    def bumped(self, bump: Bump) -> Version:
        # Semantic Versioning resets every lower part when a higher one moves.
        if bump is Bump.MAJOR:
            return Version(self.major + 1, 0, 0)
        if bump is Bump.MINOR:
            return Version(self.major, self.minor + 1, 0)
        if bump is Bump.PATCH:
            return Version(self.major, self.minor, self.patch + 1)
        return self

    @property
    def tag(self) -> str:
        return f"v{self}"

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"


# The bot was in daily use before it had a version, which is what 1.0.0 means
# in the specification: the public API is stable and people depend on it.
FIRST_RELEASE: Final = Version(1, 0, 0)


@dataclass(frozen=True)
class MergedPull:
    number: int
    branch: str
    labels: frozenset[str]

    @property
    def bump(self) -> Bump:
        if BREAKING_LABEL in self.labels:
            return Bump.MAJOR
        bump = branch_bump(self.branch)
        return UNPREFIXED_BUMP if bump is None else bump


@dataclass(frozen=True)
class Plan:
    """What `plan` decided, and the pull requests it decided from."""

    version: Version | None
    previous: Version | None
    pulls: tuple[MergedPull, ...] = ()
    already_released: bool = False
    # `version` is already tagged on this commit: an earlier run claimed the
    # number and this one finishes publishing it.
    tagged: bool = False


def branch_bump(branch: str) -> Bump | None:
    """The bump `branch`'s prefix asks for, or None when it has no known one.

    Only the prefix is read here. Whether the rest of the name is well formed
    is `branch_name_problem`'s question, and a release should not be smaller
    because a branch was spelled `feat/Profit-Page`.
    """
    prefix, slash, _ = branch.partition("/")
    return BRANCH_BUMPS.get(prefix) if slash else None


def branch_name_problem(branch: str) -> str | None:
    """Why `branch` is not a name a pull request may come from, or None."""
    allowed = ", ".join(f"{prefix}/" for prefix in BRANCH_BUMPS)
    match = BRANCH_NAME.fullmatch(branch)
    if match is None or match["prefix"] not in BRANCH_BUMPS:
        return (
            f"Branch {branch!r} must be named <type>/<name> with a type of "
            f"{allowed} and a lowercase, hyphen-separated name, such as "
            "feat/profit-page or fix/raffle-rounding."
        )
    return None


def latest_release(tags: Sequence[str]) -> tuple[str, Version] | None:
    """The newest plain release among `tags`, with the tag it came from."""
    releases = [
        (version, tag)
        for tag in tags
        if (version := Version.from_tag(tag)) is not None
    ]
    if not releases:
        return None
    version, tag = max(releases)
    return tag, version


def decide(
    previous: Version | None,
    pulls: Sequence[MergedPull],
    requested: Bump = Bump.NONE,
) -> Version | None:
    """The next version, or None when nothing merged asks for one."""
    if previous is None:
        return FIRST_RELEASE
    bump = max((requested, *(pull.bump for pull in pulls)))
    if bump is Bump.NONE:
        return None
    return previous.bumped(bump)


def parse_pulls(payload: Any, base: str) -> list[MergedPull]:
    """The pull requests in a `commits/{sha}/pulls` response merged into `base`.

    The endpoint also answers with open pull requests that happen to contain
    the commit, which say nothing about how it reached `base`.
    """
    pulls: list[MergedPull] = []
    for item in payload:
        if not item.get("merged_at") or item["base"]["ref"] != base:
            continue
        pulls.append(
            MergedPull(
                number=int(item["number"]),
                branch=str(item["head"]["ref"]),
                labels=frozenset(str(label["name"]) for label in item["labels"]),
            )
        )
    return pulls


def github_pulls(
    api_url: str,
    repository: str,
    token: str | None,
    base: str,
) -> Callable[[str], list[MergedPull]]:
    """Look up the pull requests that brought a commit into `base`."""
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    def fetch(sha: str) -> list[MergedPull]:
        request = urllib.request.Request(
            f"{api_url}/repos/{repository}/commits/{sha}/pulls",
            headers=headers,
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            return parse_pulls(json.load(response), base)

    return fetch


# Git's own error output is left on stderr, where the workflow log shows it;
# CalledProcessError alone says only which command failed.
def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ("git", *args),
        cwd=repo,
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    ).stdout


def _is_ancestor(repo: Path, commit: str, of: str) -> bool:
    result = subprocess.run(
        ("git", "merge-base", "--is-ancestor", commit, of),
        cwd=repo,
    )
    # 1 is Git's answer "no"; anything above it is Git failing to answer.
    if result.returncode not in (0, 1):
        raise subprocess.CalledProcessError(result.returncode, result.args)
    return result.returncode == 0


def _commit(repo: Path, revision: str) -> str:
    return _git(repo, "rev-parse", "--verify", f"{revision}^{{commit}}").strip()


def plan(
    repo: Path,
    head: str,
    fetch_pulls: Callable[[str], list[MergedPull]],
    requested: Bump = Bump.NONE,
) -> Plan:
    """Decide what releasing `head` in the clone at `repo` would number it.

    The clone needs its full history and tags, which is `fetch-depth: 0`.
    """
    found = latest_release(_git(repo, "tag", "--list", "v*").split())
    if found is None:
        return Plan(version=decide(None, (), requested), previous=None)
    tag, previous = found
    # The workflow creates the tag before it points the version's image tags
    # at the commit's image, so a run that failed in between leaves the newest
    # tag on its own commit. Re-running it finishes that version rather than
    # numbering the same code twice.
    if _commit(repo, head) == _commit(repo, tag):
        return Plan(version=previous, previous=previous, tagged=True)
    # A run can start after a later one has already released past it: the CI
    # for an earlier push finishing last, or a re-run of an old workflow. Its
    # commit is then inside a release already, and numbering it again would
    # put a newer version on older code.
    if _is_ancestor(repo, head, tag):
        return Plan(version=None, previous=previous, already_released=True)
    # First-parent history is one commit per merge, so this asks once per
    # pull request rather than once for every commit inside each one.
    commits = _git(repo, "rev-list", "--first-parent", f"{tag}..{head}").split()
    seen: dict[int, MergedPull] = {}
    for commit in commits:
        for pull in fetch_pulls(commit):
            seen.setdefault(pull.number, pull)
    pulls = tuple(sorted(seen.values(), key=lambda pull: pull.number))
    return Plan(
        version=decide(previous, pulls, requested),
        previous=previous,
        pulls=pulls,
    )


def summary(result: Plan) -> str:
    """The Markdown the workflow shows for this run."""
    previous = result.previous.tag if result.previous else "none"
    if result.already_released:
        heading = f"Nothing to release: this commit is already in {previous}."
    elif result.tagged:
        heading = (
            f"This commit is already tagged {previous}: pointing the image "
            "tags at its image again."
        )
    elif result.version is None and result.pulls:
        heading = f"Nothing to release since {previous}: every merge was a chore."
    elif result.version is None:
        heading = f"Nothing to release: no pull request was merged since {previous}."
    elif result.previous is None:
        heading = f"Releasing {result.version.tag}, the first release."
    else:
        heading = f"Releasing {result.version.tag} (previous {previous})."
    lines = [f"### {heading}"]
    if result.pulls:
        lines += ["", "| Pull request | Branch | Bump |", "| --- | --- | --- |"]
        lines += [
            f"| #{pull.number} | `{pull.branch}` | {pull.bump.name.lower()} |"
            for pull in result.pulls
        ]
    return "\n".join(lines) + "\n"


def outputs(result: Plan) -> dict[str, str]:
    """The step outputs the release workflow reads."""
    version = result.version
    if version is None:
        return {"release": "false"}
    return {
        "release": "true",
        "version": str(version),
        "tag": version.tag,
        # The image's moving tags: the newest of this major, and of this minor.
        "major": f"{version.major}",
        "minor": f"{version.major}.{version.minor}",
        "tagged": "true" if result.tagged else "false",
    }


def _append(path: str | None, text: str) -> None:
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(text)
    else:
        sys.stdout.write(text)


def _check_branch(args: argparse.Namespace) -> int:
    problem = branch_name_problem(args.branch)
    if problem is None:
        print(f"Branch {args.branch!r} is a {args.branch.partition('/')[0]} branch.")
        return 0
    print(f"::error title=Branch name::{problem}")
    return 1


def _plan(args: argparse.Namespace) -> int:
    requested = Bump[args.requested.upper()]
    fetch_pulls = github_pulls(
        os.environ.get("GITHUB_API_URL", "https://api.github.com"),
        os.environ["GITHUB_REPOSITORY"],
        os.environ.get("GITHUB_TOKEN"),
        args.base,
    )
    result = plan(Path.cwd(), args.head, fetch_pulls, requested)
    for pull in result.pulls:
        if branch_bump(pull.branch) is None and BREAKING_LABEL not in pull.labels:
            print(
                f"::warning title=Unprefixed branch::Pull request #{pull.number} "
                f"came from {pull.branch!r}, which has no feat/, fix/ or chore/ "
                "prefix; it is released as a patch."
            )
    _append(os.environ.get("GITHUB_STEP_SUMMARY"), summary(result))
    _append(
        os.environ.get("GITHUB_OUTPUT"),
        "".join(f"{key}={value}\n" for key, value in outputs(result).items()),
    )
    if result.already_released and requested is not Bump.NONE:
        # Someone asked for this release by hand, so say why there isn't one
        # rather than finish green having done nothing.
        print(
            f"::error title=Already released::{args.head} is already part of "
            f"{result.previous.tag if result.previous else 'a release'}."
        )
        return 1
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Name pull request branches and number releases."
    )
    commands = parser.add_subparsers(required=True)

    check = commands.add_parser(
        "check-branch",
        help="fail unless BRANCH is named feat/, fix/ or chore/<name>",
    )
    check.add_argument("branch")
    check.set_defaults(handler=_check_branch)

    decide_next = commands.add_parser(
        "plan",
        help="decide the release for HEAD and write the step outputs",
    )
    decide_next.add_argument("--head", required=True, help="commit to release")
    decide_next.add_argument(
        "--requested",
        choices=[bump.name.lower() for bump in Bump],
        default="none",
        help="the smallest release to cut even if every merge is a chore",
    )
    decide_next.add_argument("--base", default="main", help="branch released from")
    decide_next.set_defaults(handler=_plan)

    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
