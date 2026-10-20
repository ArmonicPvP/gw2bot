"""Semantic Versioning: the number the bot reports and how releases pick it.

`scripts/next_release.py` is what `.github/workflows/release.yml` and the
`Branch name` CI check run. A mistake there does not fail anything at the time;
it puts the wrong number on a release, which a tag then makes permanent. So the
decisions are pinned here, and `plan` is run against a real Git repository
rather than canned `git` output, because which commits it walks is the point.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

import next_release
from gw2bot.version import UNRELEASED_VERSION, VERSION_FILE, read_version
from next_release import (
    FIRST_RELEASE,
    Bump,
    MergedPull,
    Plan,
    Version,
    branch_bump,
    branch_name_problem,
    decide,
    latest_release,
    outputs,
    parse_pulls,
    plan,
    summary,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
RELEASE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release.yml"


def _pull(number: int, branch: str, *labels: str) -> MergedPull:
    return MergedPull(number=number, branch=branch, labels=frozenset(labels))


class GitRepo:
    """A throwaway repository whose history is built the way merges land."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.git("init", "--quiet", "--initial-branch=main")

    def git(self, *args: str) -> str:
        return subprocess.run(
            ("git", *args),
            cwd=self.path,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def commit(self, message: str) -> str:
        self.git("commit", "--quiet", "--allow-empty", "--message", message)
        return self.git("rev-parse", "HEAD")

    def merge(self, branch: str, commits: int = 2) -> str:
        """Land `branch` with a merge commit, as GitHub merges a pull request."""
        self.git("switch", "--quiet", "--create", branch)
        for index in range(commits):
            self.commit(f"{branch} {index}")
        self.git("switch", "--quiet", "main")
        self.git("merge", "--quiet", "--no-ff", "--message", f"Merge {branch}", branch)
        return self.git("rev-parse", "HEAD")

    def tag(self, name: str, commit: str = "HEAD") -> None:
        self.git("tag", name, commit)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> GitRepo:
    # Nothing from the machine's Git configuration - signing, hooks, a default
    # branch name - may change what these histories look like.
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "Release Test")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "release@example.invalid")
    git_repo = GitRepo(tmp_path)
    git_repo.commit("Initial commit")
    return git_repo


def _fetcher(
    pulls: dict[str, list[MergedPull]],
    asked: list[str] | None = None,
) -> Callable[[str], list[MergedPull]]:
    def fetch(sha: str) -> list[MergedPull]:
        if asked is not None:
            asked.append(sha)
        return pulls.get(sha, [])

    return fetch


def _no_github(sha: str) -> list[MergedPull]:
    raise AssertionError(f"asked GitHub about {sha} when nothing needed it")


class TestTheRunningVersion:
    def test_reads_the_stamp_the_image_build_writes(self, tmp_path: Path) -> None:
        stamp = tmp_path / "VERSION"
        stamp.write_text("1.4.2\n", encoding="utf-8")

        assert read_version(stamp) == "1.4.2"

    def test_an_unstamped_copy_reports_itself_unreleased(self, tmp_path: Path) -> None:
        assert read_version(tmp_path / "VERSION") == UNRELEASED_VERSION

    def test_an_empty_stamp_is_unreleased_not_blank(self, tmp_path: Path) -> None:
        stamp = tmp_path / "VERSION"
        stamp.write_text(" \n", encoding="utf-8")

        assert read_version(stamp) == UNRELEASED_VERSION

    def test_unreleased_sorts_below_every_release(self) -> None:
        # A pre-release of 0.0.0, so nothing that compares versions can take
        # an unstamped build for a real one.
        assert UNRELEASED_VERSION.startswith("0.0.0-")

    def test_the_image_build_stamps_the_file_the_bot_reads(self) -> None:
        # Moving version.py would leave the Dockerfile writing a file nothing
        # reads, and every published image would call itself unreleased.
        stamp = VERSION_FILE.relative_to(REPO_ROOT).as_posix()
        dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
        workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")

        assert f"> {stamp};" in dockerfile
        assert "GW2BOT_VERSION=${{ steps.plan.outputs.version }}" in workflow
        for ignore in (".gitignore", ".dockerignore"):
            assert stamp in (REPO_ROOT / ignore).read_text(encoding="utf-8")


class TestVersion:
    @pytest.mark.parametrize(
        ("tag", "expected"),
        [
            ("v1.0.0", Version(1, 0, 0)),
            ("v0.12.3", Version(0, 12, 3)),
            ("v10.20.30", Version(10, 20, 30)),
        ],
    )
    def test_reads_a_release_tag(self, tag: str, expected: Version) -> None:
        assert Version.from_tag(tag) == expected

    @pytest.mark.parametrize(
        "tag",
        ["1.0.0", "v1.0", "v1.0.0-rc.1", "v1.0.0+build", "v01.0.0", "latest"],
    )
    def test_ignores_anything_but_a_plain_release(self, tag: str) -> None:
        assert Version.from_tag(tag) is None

    @pytest.mark.parametrize(
        ("bump", "expected"),
        [
            (Bump.MAJOR, Version(2, 0, 0)),
            (Bump.MINOR, Version(1, 4, 0)),
            (Bump.PATCH, Version(1, 3, 6)),
            (Bump.NONE, Version(1, 3, 5)),
        ],
    )
    def test_a_bump_resets_every_lower_part(
        self, bump: Bump, expected: Version
    ) -> None:
        assert Version(1, 3, 5).bumped(bump) == expected

    def test_the_newest_release_is_compared_as_numbers(self) -> None:
        # Sorted as text, v1.9.0 would beat v1.10.0, and the pre-release would
        # beat both.
        tags = ["v1.9.0", "v1.10.0", "v2.0.0-rc.1", "nightly"]

        assert latest_release(tags) == ("v1.10.0", Version(1, 10, 0))

    def test_no_release_tags_is_no_previous_release(self) -> None:
        assert latest_release(["nightly", "v2.0.0-rc.1"]) is None


class TestBranchNames:
    @pytest.mark.parametrize(
        ("branch", "bump"),
        [
            ("feat/profit-page", Bump.MINOR),
            ("fix/bug-patch", Bump.PATCH),
            ("chore/update-ci", Bump.NONE),
        ],
    )
    def test_each_prefix_asks_for_its_bump(self, branch: str, bump: Bump) -> None:
        assert branch_bump(branch) is bump
        assert branch_name_problem(branch) is None

    @pytest.mark.parametrize(
        "branch",
        [
            "claude/wizardly-wright-ddtj5a",
            "ccr-686784d1-ghg2mv",
            "feature/profit-page",
            "feat",
            "feat/",
            "feat/Profit-Page",
            "feat/profit--page",
            "feat/profit-page-",
            "feat/events/mentee-slot",
            "fix/raffle rounding",
            "feat/profit_page",
            "fix/profit.page",
        ],
    )
    def test_refuses_anything_else(self, branch: str) -> None:
        problem = branch_name_problem(branch)

        assert problem is not None
        assert repr(branch) in problem
        assert "feat/, fix/, chore/" in problem

    def test_a_misspelled_name_still_releases_by_its_prefix(self) -> None:
        # CI refuses the name, but if it was merged anyway the release should
        # not be smaller for it.
        assert branch_bump("feat/Profit-Page") is Bump.MINOR

    def test_a_breaking_label_is_major_whatever_the_branch(self) -> None:
        assert _pull(1, "fix/drop-old-setting", "breaking").bump is Bump.MAJOR
        assert _pull(2, "chore/rename-volume", "breaking").bump is Bump.MAJOR

    def test_an_unprefixed_merge_still_ships_as_a_patch(self) -> None:
        assert _pull(3, "claude/wizardly-wright-ddtj5a").bump is Bump.PATCH


class TestDecide:
    def test_the_first_release_is_1_0_0(self) -> None:
        assert decide(None, [_pull(1, "chore/ci")]) == FIRST_RELEASE == Version(1, 0, 0)

    def test_the_largest_bump_wins(self) -> None:
        pulls = [_pull(1, "fix/a"), _pull(2, "feat/b"), _pull(3, "chore/c")]

        assert decide(Version(1, 2, 3), pulls) == Version(1, 3, 0)

    def test_only_chores_release_nothing(self) -> None:
        assert decide(Version(1, 2, 3), [_pull(1, "chore/a")]) is None
        assert decide(Version(1, 2, 3), []) is None

    def test_a_requested_bump_is_a_floor_not_a_ceiling(self) -> None:
        previous = Version(1, 2, 3)

        assert decide(previous, [_pull(1, "chore/a")], Bump.PATCH) == Version(1, 2, 4)
        assert decide(previous, [_pull(1, "feat/a")], Bump.PATCH) == Version(1, 3, 0)


class TestParsePulls:
    def test_keeps_only_pull_requests_merged_into_the_base(self) -> None:
        payload = [
            {
                "number": 7,
                "merged_at": "2026-10-16T15:27:53Z",
                "head": {"ref": "feat/profit-page"},
                "base": {"ref": "main"},
                "labels": [{"name": "breaking"}, {"name": "web"}],
            },
            {
                # Open, and only containing the commit.
                "number": 8,
                "merged_at": None,
                "head": {"ref": "fix/other"},
                "base": {"ref": "main"},
                "labels": [],
            },
            {
                "number": 9,
                "merged_at": "2026-10-16T15:27:53Z",
                "head": {"ref": "fix/stacked"},
                "base": {"ref": "feat/profit-page"},
                "labels": [],
            },
        ]

        assert parse_pulls(payload, "main") == [
            _pull(7, "feat/profit-page", "breaking", "web")
        ]


class TestPlan:
    def test_the_first_release_needs_nothing_from_github(self, repo: GitRepo) -> None:
        head = repo.merge("chore/ci")

        result = plan(repo.path, head, _no_github)

        assert result == Plan(version=FIRST_RELEASE, previous=None)

    def test_releases_every_merge_since_the_last_tag(self, repo: GitRepo) -> None:
        repo.tag("v1.2.3")
        fix = repo.merge("fix/a")
        feat = repo.merge("feat/b")
        chore = repo.merge("chore/c")
        asked: list[str] = []
        fetch = _fetcher(
            {
                fix: [_pull(1, "fix/a")],
                feat: [_pull(2, "feat/b")],
                chore: [_pull(3, "chore/c")],
            },
            asked,
        )

        result = plan(repo.path, chore, fetch)

        assert result.version == Version(1, 3, 0)
        assert result.previous == Version(1, 2, 3)
        assert [pull.number for pull in result.pulls] == [1, 2, 3]
        # One question per merge: the commits inside each branch are not on
        # main's first-parent line, and asking about them would cost a request
        # each for answers the merge commit already gave.
        assert sorted(asked) == sorted([fix, feat, chore])

    def test_a_merge_released_already_is_not_counted_again(
        self, repo: GitRepo
    ) -> None:
        feat = repo.merge("feat/old")
        repo.tag("v1.1.0")
        fix = repo.merge("fix/new")
        fetch = _fetcher({feat: [_pull(1, "feat/old")], fix: [_pull(2, "fix/new")]})

        result = plan(repo.path, fix, fetch)

        assert result.version == Version(1, 1, 1)
        assert [pull.number for pull in result.pulls] == [2]

    def test_only_chores_since_the_last_tag_release_nothing(
        self, repo: GitRepo
    ) -> None:
        repo.tag("v1.1.0")
        chore = repo.merge("chore/docs")

        result = plan(repo.path, chore, _fetcher({chore: [_pull(4, "chore/docs")]}))

        assert result.version is None
        assert not result.already_released

    def test_a_requested_bump_releases_chores(self, repo: GitRepo) -> None:
        repo.tag("v1.1.0")
        chore = repo.merge("chore/deps")
        fetch = _fetcher({chore: [_pull(4, "chore/deps")]})

        result = plan(repo.path, chore, fetch, Bump.PATCH)

        assert result.version == Version(1, 1, 1)

    def test_a_commit_inside_a_newer_release_is_not_numbered_again(
        self, repo: GitRepo
    ) -> None:
        # The CI for an earlier push can finish after the one for a later
        # push has already been released; that release already contains it.
        earlier = repo.merge("fix/first")
        repo.merge("feat/second")
        repo.tag("v1.1.0")

        result = plan(repo.path, earlier, _no_github)

        assert result == Plan(
            version=None, previous=Version(1, 1, 0), already_released=True
        )

    def test_the_newest_tag_on_its_own_commit_is_published_again(
        self, repo: GitRepo
    ) -> None:
        # A run that failed between creating the tag and pointing the image
        # tags at it, being re-run. It keeps the number the tag claimed.
        repo.tag("v1.0.0")
        head = repo.merge("feat/x")
        repo.tag("v1.1.0")

        result = plan(repo.path, head, _no_github)

        assert result == Plan(
            version=Version(1, 1, 0), previous=Version(1, 1, 0), tagged=True
        )

    def test_an_older_tagged_commit_is_not_published_again(
        self, repo: GitRepo
    ) -> None:
        # Its image would move `latest` and the major and minor tags back
        # from the newer release.
        head = repo.merge("feat/x")
        repo.tag("v1.0.0")
        repo.merge("fix/y")
        repo.tag("v1.0.1")

        assert plan(repo.path, head, _no_github).already_released

    def test_a_pull_request_on_several_commits_counts_once(
        self, repo: GitRepo
    ) -> None:
        # A rebase merge puts every commit of the pull request on main.
        repo.tag("v1.0.0")
        first = repo.commit("fix part one")
        second = repo.commit("fix part two")
        pull = _pull(5, "fix/rebased")

        result = plan(repo.path, second, _fetcher({first: [pull], second: [pull]}))

        assert result.pulls == (pull,)
        assert result.version == Version(1, 0, 1)


class TestWorkflowOutputs:
    def test_a_release_names_the_version_tag_and_moving_image_tags(self) -> None:
        result = Plan(version=Version(1, 4, 2), previous=Version(1, 4, 1))

        assert outputs(result) == {
            "release": "true",
            "version": "1.4.2",
            "tag": "v1.4.2",
            "major": "1",
            "minor": "1.4",
            "tagged": "false",
        }

    def test_a_tagged_release_says_the_tag_exists(self) -> None:
        result = Plan(version=Version(1, 4, 2), previous=Version(1, 4, 2), tagged=True)

        assert outputs(result)["tagged"] == "true"
        assert "already tagged v1.4.2" in summary(result)

    def test_no_release_says_only_that(self) -> None:
        assert outputs(Plan(version=None, previous=Version(1, 0, 0))) == {
            "release": "false"
        }

    def test_the_summary_lists_each_pull_request_and_its_bump(self) -> None:
        result = Plan(
            version=Version(2, 0, 0),
            previous=Version(1, 4, 2),
            pulls=(_pull(9, "fix/drop", "breaking"), _pull(10, "chore/ci")),
        )

        text = summary(result)

        assert "Releasing v2.0.0 (previous v1.4.2)" in text
        assert "| #9 | `fix/drop` | major |" in text
        assert "| #10 | `chore/ci` | none |" in text


class TestReleaseWorkflow:
    """What the workflow must keep that only shows up on a real release.

    None breaks a test or CI run before it, which is why they are pinned.
    """

    def test_the_token_can_read_the_merged_pull_requests(self) -> None:
        # A job's permissions block sets everything it leaves out to none, and
        # `plan` asks the pull requests endpoint about every merge after the
        # first release.
        workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")

        assert "pull-requests: read" in workflow

    def test_the_image_is_pushed_with_the_runs_own_token(self) -> None:
        # GitHub Packages takes the workflow's token, which the permissions
        # block has to grant, so a release needs no repository secret.
        workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")

        assert "packages: write" in workflow
        assert "secrets." not in workflow

    def test_a_version_is_claimed_only_between_push_and_publish(self) -> None:
        # Pushed under the commit SHA first, a failed login or build claims no
        # number. Tagged next, no later run can give the number to other code.
        # Pointed at the pushed image last, a re-run repeats it harmlessly.
        workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")
        build = workflow.index("docker/build-push-action")
        build_tags = workflow[build : workflow.index("labels:", build)]

        assert build < workflow.index("gh release create")
        assert workflow.index("gh release create") < workflow.index(
            "imagetools create"
        )
        # The build itself never names a version, only the commit.
        assert "outputs.version" not in build_tags.split("tags:")[1]
        assert "latest" not in build_tags


class TestCommandLine:
    def test_check_branch_passes_a_prefixed_branch(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert next_release.main(["check-branch", "feat/profit-page"]) == 0
        assert "::error" not in capsys.readouterr().out

    def test_check_branch_fails_with_an_annotation(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert next_release.main(["check-branch", "claude/wizardly-wright"]) == 1
        assert capsys.readouterr().out.startswith("::error title=Branch name::")

    def _run_plan(
        self,
        repo: GitRepo,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        fetch: Callable[[str], list[MergedPull]],
        *args: str,
    ) -> tuple[int, str, str]:
        output = tmp_path / "output"
        step_summary = tmp_path / "summary"
        monkeypatch.chdir(repo.path)
        monkeypatch.setenv("GITHUB_REPOSITORY", "ArmonicPvP/gw2bot")
        monkeypatch.setenv("GITHUB_OUTPUT", str(output))
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(step_summary))
        monkeypatch.setattr(next_release, "github_pulls", lambda *_: fetch)
        code = next_release.main(["plan", *args])
        return (
            code,
            output.read_text(encoding="utf-8") if output.exists() else "",
            step_summary.read_text(encoding="utf-8"),
        )

    def test_plan_writes_the_step_outputs_and_summary(
        self,
        repo: GitRepo,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        repo.tag("v1.0.0")
        head = repo.merge("claude/unprefixed")
        fetch = _fetcher({head: [_pull(11, "claude/unprefixed")]})

        code, output, step_summary = self._run_plan(
            repo, tmp_path, monkeypatch, fetch, "--head", head
        )

        assert code == 0
        assert output.splitlines() == [
            "release=true",
            "version=1.0.1",
            "tag=v1.0.1",
            "major=1",
            "minor=1.0",
            "tagged=false",
        ]
        assert "Releasing v1.0.1" in step_summary
        # The run says why an unprefixed branch became a patch, where the
        # person reading the run will see it.
        assert "::warning title=Unprefixed branch::Pull request #11" in (
            capsys.readouterr().out
        )

    def test_a_requested_release_of_released_code_fails(
        self,
        repo: GitRepo,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        head = repo.merge("feat/x")
        repo.merge("fix/y")
        repo.tag("v1.0.0")

        code, output, _ = self._run_plan(
            repo,
            tmp_path,
            monkeypatch,
            _no_github,
            *("--head", head, "--requested", "patch"),
        )

        assert code == 1
        assert output == "release=false\n"
        assert "::error title=Already released::" in capsys.readouterr().out

    def test_an_automatic_run_on_released_code_is_quiet(
        self,
        repo: GitRepo,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        head = repo.merge("feat/x")
        repo.merge("fix/y")
        repo.tag("v1.0.0")

        code, output, step_summary = self._run_plan(
            repo, tmp_path, monkeypatch, _no_github, "--head", head
        )

        assert code == 0
        assert output == "release=false\n"
        assert "already in v1.0.0" in step_summary
