# Copyright Kevin Deldycke <kevin@deldycke.com> and contributors.
#
# This program is Free Software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place - Suite 330, Boston, MA  02111-1307, USA.

"""Tests for :mod:`repomatic.github.pr`."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from repomatic.cli import repomatic
from repomatic.github.pr import (
    PrOperation,
    PrSyncResult,
    _needs_push,
    carry_pr_branch_paths,
    close_open_prs_on_branch,
    close_pr,
    list_open_prs_by_branch,
    upsert_pr,
)

BRANCH = "sync-fruit-basket"
"""Arbitrary head branch the sync tests target."""

BASE_SHA = "a" * 40
"""Stand-in SHA for the base branch tip."""

CANDIDATE_SHA = "b" * 40
"""Stand-in SHA for the commit built from the working tree."""

REMOTE_SHA = "c" * 40
"""Stand-in SHA for the tip of an already-published head branch."""


def test_list_open_prs_by_branch_filters_arguments():
    with patch("repomatic.github.pr.run_gh_command") as mock_gh:
        mock_gh.return_value = json.dumps([{"number": 42, "isDraft": False}])
        prs = list_open_prs_by_branch("minor-version-increment")
    assert prs == [{"number": 42, "isDraft": False}]
    args = mock_gh.call_args.args[0]
    assert args[:2] == ["pr", "list"]
    assert "--state" in args and args[args.index("--state") + 1] == "open"
    assert (
        "--head" in args and args[args.index("--head") + 1] == "minor-version-increment"
    )


def test_list_open_prs_by_branch_empty():
    with patch("repomatic.github.pr.run_gh_command") as mock_gh:
        mock_gh.return_value = "[]"
        assert list_open_prs_by_branch("major-version-increment") == []


def test_close_pr_default_deletes_branch():
    # The close write goes through the issue module's lock-recovery wrapper.
    with patch("repomatic.github.issue.run_gh_command") as mock_gh:
        close_pr(7, "stale")
    args = mock_gh.call_args.args[0]
    assert args[:3] == ["pr", "close", "7"]
    assert "--comment" in args and args[args.index("--comment") + 1] == "stale"
    assert "--delete-branch" in args


def test_close_pr_can_keep_branch():
    with patch("repomatic.github.issue.run_gh_command") as mock_gh:
        close_pr(7, "stale", delete_branch=False)
    assert "--delete-branch" not in mock_gh.call_args.args[0]


def test_close_pr_unlocks_a_locked_conversation():
    """A hand-locked PR is unlocked so the retire path's close comment lands."""
    calls: list[list[str]] = []

    def fake_gh(args):
        calls.append(list(args))
        if args[:2] == ["pr", "close"] and ["pr", "unlock", "7"] not in calls:
            raise RuntimeError(
                "GraphQL: Unable to create comment because pull request is"
                " locked (addComment)"
            )
        return ""

    with patch("repomatic.github.issue.run_gh_command", side_effect=fake_gh):
        close_pr(7, "stale")

    assert [call[:2] for call in calls] == [
        ["pr", "close"],
        ["pr", "unlock"],
        ["pr", "close"],
    ]


def test_close_open_prs_on_branch_no_match_is_noop():
    with patch("repomatic.github.pr.run_gh_command") as mock_gh:
        mock_gh.return_value = "[]"
        closed = close_open_prs_on_branch("minor-version-increment", "stale")
    assert closed == []
    assert mock_gh.call_count == 1


def test_close_open_prs_on_branch_closes_every_match():
    payload = json.dumps([
        {"number": 11, "isDraft": False},
        {"number": 22, "isDraft": False},
    ])
    with (
        patch("repomatic.github.pr.run_gh_command") as mock_list,
        patch("repomatic.github.issue.run_gh_command") as mock_close,
    ):
        mock_list.return_value = payload
        closed = close_open_prs_on_branch("minor-version-increment", "stale")
    assert closed == [11, 22]
    close_args = [call.args[0] for call in mock_close.call_args_list]
    assert close_args[0][:3] == ["pr", "close", "11"]
    assert close_args[1][:3] == ["pr", "close", "22"]


@pytest.mark.parametrize(
    ("candidate_tree", "remote_tree", "remote_commits", "expected_commits", "expected"),
    (
        # Same tree, same shape: the branch is already right.
        ("tree-1", "tree-1", 1, 1, False),
        # The job's output moved.
        ("tree-2", "tree-1", 1, 1, True),
        # Same tree, but the branch does not hold the same number of commits.
        ("tree-1", "tree-1", 2, 1, True),
        ("tree-1", "tree-1", 0, 1, True),
        # A release freeze legitimately carries two commits.
        ("tree-1", "tree-1", 2, 2, False),
        ("tree-1", "tree-1", 1, 2, True),
    ),
)
def test_needs_push(
    candidate_tree, remote_tree, remote_commits, expected_commits, expected
):
    trees = {CANDIDATE_SHA: candidate_tree, REMOTE_SHA: remote_tree}
    with (
        patch("repomatic.github.pr.git_ops.tree_sha", side_effect=trees.get),
        patch(
            "repomatic.github.pr.git_ops.count_commits_between",
            return_value=remote_commits,
        ),
    ):
        assert (
            _needs_push(CANDIDATE_SHA, REMOTE_SHA, BASE_SHA, expected_commits)
            is expected
        )


@pytest.fixture
def git(monkeypatch):
    """A stand-in for the git layer, wired to the ordinary happy path.

    Each test overrides only the calls its own scenario turns on, which keeps
    the interesting difference visible instead of buried in a stack of patches.
    """
    stub = MagicMock()
    stub.current_branch.return_value = "main"
    stub.head_sha.return_value = CANDIDATE_SHA
    stub.fetch_remote_branch.side_effect = lambda name, remote="origin": (
        BASE_SHA if name == "main" else None
    )
    stub.stage_all.return_value = True
    stub.commit_staged.return_value = CANDIDATE_SHA
    stub.count_commits_between.return_value = 1
    stub.is_ancestor.return_value = True
    stub.merge_base.return_value = BASE_SHA
    stub.rebase_onto.return_value = True
    stub.tree_sha.side_effect = lambda sha: f"tree-of-{sha}"
    monkeypatch.setattr("repomatic.github.pr.git_ops", stub)
    return stub


@pytest.fixture
def gh(monkeypatch):
    """Capture `gh` invocations, returning whatever the test queues up.

    One stub covers both dispatch points: the module's own calls and the
    close path, which routes through the issue module's lock-recovery
    wrapper. A single mock keeps each test's call sequence in one list.
    """
    stub = MagicMock(return_value="[]")
    monkeypatch.setattr("repomatic.github.pr.run_gh_command", stub)
    monkeypatch.setattr("repomatic.github.issue.run_gh_command", stub)
    return stub


def verbs(gh_stub) -> list[str]:
    """The `gh <noun> <verb>` pairs a test's run produced, in order."""
    return [" ".join(call.args[0][:2]) for call in gh_stub.call_args_list]


def test_upsert_pr_requires_a_base_on_detached_head(git, gh):
    git.current_branch.return_value = None
    with pytest.raises(RuntimeError, match="detached"):
        upsert_pr(BRANCH, "t", "b", "m")


def test_upsert_pr_rejects_a_base_missing_from_the_remote(git, gh):
    git.fetch_remote_branch.side_effect = lambda name, remote="origin": None
    with pytest.raises(RuntimeError, match="does not exist on origin"):
        upsert_pr(BRANCH, "t", "b", "m", base="main")


def test_upsert_pr_without_changes_or_branch_does_nothing(git, gh):
    """The quietest path: nothing to publish and nothing to retire."""
    git.stage_all.return_value = False
    git.count_commits_between.return_value = 0
    result = upsert_pr(BRANCH, "t", "b", "m")
    assert result.operation == PrOperation.NONE
    assert result.number is None
    gh.assert_not_called()
    git.force_push_branch.assert_not_called()


def test_upsert_pr_retires_branch_and_pr_when_changes_evaporate(git, gh):
    """The path a skipped action step could never reach."""
    git.stage_all.return_value = False
    git.count_commits_between.return_value = 0
    git.fetch_remote_branch.side_effect = lambda name, remote="origin": (
        BASE_SHA if name == "main" else REMOTE_SHA
    )
    gh.side_effect = [json.dumps([{"number": 5, "isDraft": False}]), ""]
    result = upsert_pr(BRANCH, "t", "b", "m")
    assert result == (PrOperation.CLOSED, BRANCH, 5, "")
    # `gh pr close --delete-branch` covers the branch, so no separate delete.
    git.delete_remote_branch.assert_not_called()
    assert verbs(gh) == ["pr list", "pr close"]


def test_upsert_pr_deletes_an_orphan_branch_with_no_open_pr(git, gh):
    git.stage_all.return_value = False
    git.count_commits_between.return_value = 0
    git.fetch_remote_branch.side_effect = lambda name, remote="origin": (
        BASE_SHA if name == "main" else REMOTE_SHA
    )
    result = upsert_pr(BRANCH, "t", "b", "m")
    assert result.operation == PrOperation.CLOSED
    assert result.number is None
    git.delete_remote_branch.assert_called_once_with(BRANCH, remote="origin")


def test_upsert_pr_creates_branch_and_pr(git, gh):
    gh.side_effect = ["[]", "https://github.com/kate/fruits/pull/77", ""]
    result = upsert_pr(BRANCH, "Title", "Body", "Message", labels=["🤖 ci"])

    assert result.operation == PrOperation.CREATED
    assert result.number == 77
    # A branch that does not exist yet takes no lease.
    assert git.force_push_branch.call_args.kwargs["expected_sha"] is None
    # The checkout is handed back exactly as it was found.
    git.checkout.assert_called_once_with("main")
    git.delete_branch.assert_called_once()
    create_args = gh.call_args_list[1].args[0]
    assert create_args[:2] == ["pr", "create"]
    assert create_args[create_args.index("--base") + 1] == "main"
    assert create_args[create_args.index("--head") + 1] == BRANCH
    assert "--draft" not in create_args


def test_upsert_pr_updates_an_existing_pr_and_leases_the_push(git, gh):
    git.fetch_remote_branch.side_effect = lambda name, remote="origin": (
        BASE_SHA if name == "main" else REMOTE_SHA
    )
    git.tree_sha.side_effect = ["new", "old"]
    gh.side_effect = [json.dumps([{"number": 88, "isDraft": False}]), "", ""]
    result = upsert_pr(BRANCH, "Title", "Body", "Message")

    assert result.operation == PrOperation.UPDATED
    assert result.number == 88
    assert git.force_push_branch.call_args.kwargs["expected_sha"] == REMOTE_SHA
    assert gh.call_args_list[1].args[0][:3] == ["pr", "edit", "88"]


def test_upsert_pr_skips_the_push_when_the_branch_already_matches(git, gh):
    """The check that keeps an unchanged job from re-triggering every workflow."""
    git.fetch_remote_branch.side_effect = lambda name, remote="origin": (
        BASE_SHA if name == "main" else REMOTE_SHA
    )
    git.tree_sha.side_effect = lambda sha: "same"
    result = upsert_pr(BRANCH, "Title", "Body", "Message")

    assert result.operation == PrOperation.NONE
    git.force_push_branch.assert_not_called()
    gh.assert_not_called()
    # Even on the no-op path the checkout is restored.
    git.checkout.assert_called_once_with("main")


def test_upsert_pr_restores_the_checkout_when_the_push_fails(git, gh):
    git.force_push_branch.side_effect = RuntimeError("rejected")
    with pytest.raises(RuntimeError, match="rejected"):
        upsert_pr(BRANCH, "Title", "Body", "Message")
    git.checkout.assert_called_once_with("main")
    git.delete_branch.assert_called_once()


def test_upsert_pr_survives_a_refused_label_or_assignee(git, gh):
    """`github-actions[bot]` cannot be assigned, and that must not lose the PR."""
    gh.side_effect = [
        "[]",
        "https://github.com/kate/fruits/pull/77",
        RuntimeError("could not assign github-actions[bot]"),
    ]
    result = upsert_pr(
        BRANCH, "Title", "Body", "Message", assignees=["github-actions[bot]"]
    )
    assert result.operation == PrOperation.CREATED
    assert result.number == 77


# --- Wave 2: detached HEAD, carried commits, draft ---


def test_upsert_pr_opens_against_an_explicit_base_from_a_detached_head(git, gh):
    """`fix-changelog` and `bump-version` check out a raw SHA, not a branch."""
    git.current_branch.return_value = None
    git.head_sha.return_value = CANDIDATE_SHA
    gh.side_effect = ["[]", "https://github.com/kate/fruits/pull/91", ""]
    result = upsert_pr(BRANCH, "Title", "Body", "Message", base="main")

    assert result.operation == PrOperation.CREATED
    # The base commit comes from the remote branch, never from the checkout.
    git.fetch_remote_branch.assert_any_call("main", remote="origin")
    create_args = gh.call_args_list[1].args[0]
    assert create_args[create_args.index("--base") + 1] == "main"
    # A detached checkout is restored by SHA, since there is no branch to name.
    git.checkout.assert_called_once_with(CANDIDATE_SHA)


def test_upsert_pr_carries_commits_the_job_made_itself(git, gh):
    """`prepare-release` commits freeze and unfreeze; both must reach the PR."""
    git.stage_all.return_value = False
    git.count_commits_between.return_value = 2
    gh.side_effect = ["[]", "https://github.com/kate/fruits/pull/92", ""]
    result = upsert_pr(BRANCH, "Title", "Body", "Message", base="main")

    assert result.operation == PrOperation.CREATED
    # Nothing uncommitted, so no extra commit is manufactured on top.
    git.commit_staged.assert_not_called()
    git.force_push_branch.assert_called_once()


def test_upsert_pr_replays_carried_commits_onto_a_moved_base(git, gh):
    git.is_ancestor.return_value = False
    git.merge_base.return_value = "f" * 40
    gh.side_effect = ["[]", "https://github.com/kate/fruits/pull/93", ""]
    upsert_pr(BRANCH, "Title", "Body", "Message", base="main")

    git.rebase_onto.assert_called_once()
    onto, fork_point, _branch = git.rebase_onto.call_args.args
    assert onto == BASE_SHA
    assert fork_point == "f" * 40


def test_upsert_pr_publishes_anyway_when_the_replay_conflicts(git, gh):
    """A branch on a stale base still opens a usable PR; the next run converges."""
    git.is_ancestor.return_value = False
    git.rebase_onto.return_value = False
    gh.side_effect = ["[]", "https://github.com/kate/fruits/pull/94", ""]
    result = upsert_pr(BRANCH, "Title", "Body", "Message", base="main")

    assert result.operation == PrOperation.CREATED
    git.force_push_branch.assert_called_once()


def test_upsert_pr_skips_the_replay_when_history_is_unrelatable(git, gh):
    """A shallow clone answers neither yes nor no, and must not be rebased blind."""
    git.is_ancestor.return_value = None
    gh.side_effect = ["[]", "https://github.com/kate/fruits/pull/95", ""]
    upsert_pr(BRANCH, "Title", "Body", "Message", base="main")

    git.rebase_onto.assert_not_called()


def test_upsert_pr_creates_a_draft_when_asked(git, gh):
    gh.side_effect = ["[]", "https://github.com/kate/fruits/pull/96", ""]
    upsert_pr(BRANCH, "Title", "Body", "Message", base="main", draft=True)
    assert "--draft" in gh.call_args_list[1].args[0]


def test_upsert_pr_puts_a_readied_pr_back_into_draft(git, gh):
    """`draft` is re-applied on update, matching the action's always-true mode."""
    git.fetch_remote_branch.side_effect = lambda name, remote="origin": (
        BASE_SHA if name == "main" else REMOTE_SHA
    )
    git.tree_sha.side_effect = ["new", "old"]
    gh.side_effect = [
        json.dumps([{"number": 97, "isDraft": False}]),
        "",
        "",
        "",
    ]
    result = upsert_pr(BRANCH, "Title", "Body", "Message", base="main", draft=True)

    assert result.operation == PrOperation.UPDATED
    assert verbs(gh)[-1] == "pr ready"
    assert "--undo" in gh.call_args_list[-1].args[0]


def test_upsert_pr_leaves_an_already_draft_pr_alone(git, gh):
    git.fetch_remote_branch.side_effect = lambda name, remote="origin": (
        BASE_SHA if name == "main" else REMOTE_SHA
    )
    git.tree_sha.side_effect = ["new", "old"]
    gh.side_effect = [
        json.dumps([{"number": 98, "isDraft": True}]),
        "",
        "",
    ]
    upsert_pr(BRANCH, "Title", "Body", "Message", base="main", draft=True)
    assert "pr ready" not in verbs(gh)


# --- pr-sync CLI: template-driven defaults ---


@pytest.fixture
def cli_upsert(monkeypatch):
    """Capture what the `pr-sync` command hands to `upsert_pr`."""
    captured = {}

    def spy(**kwargs):
        captured.update(kwargs)
        return PrSyncResult(PrOperation.NONE, kwargs["branch"])

    monkeypatch.setattr("repomatic.cli.upsert_pr", spy)
    monkeypatch.setattr(
        "repomatic.cli._render_pr_content",
        lambda **kwargs: ("Rendered title", "Rendered body", "Rendered message"),
    )
    return captured


def test_pr_sync_cli_resolves_everything_from_the_template(cli_upsert, monkeypatch):
    """One flag yields branch, labels, draft, title, body and commit message."""
    monkeypatch.setattr("repomatic.cli.current_branch", lambda: "main")
    result = CliRunner().invoke(
        repomatic, ["pr-sync", "--template", "format-python"], catch_exceptions=False
    )
    assert result.exit_code == 0
    assert cli_upsert["branch"] == "format-python"
    assert cli_upsert["labels"] == ("🤖 ci",)
    assert cli_upsert["draft"] is False
    assert cli_upsert["title"] == "Rendered title"
    assert cli_upsert["commit_message"] == "Rendered message"


def test_pr_sync_cli_reads_draft_from_frontmatter(cli_upsert, monkeypatch):

    monkeypatch.setattr("repomatic.cli.current_branch", lambda: "main")
    result = CliRunner().invoke(
        repomatic,
        [
            "pr-sync",
            "--template",
            "bump-version",
            "--branch",
            "minor-version-increment",
        ],
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    assert cli_upsert["branch"] == "minor-version-increment"
    assert cli_upsert["labels"] == ("🆙 changelog",)
    assert cli_upsert["draft"] is True


def test_pr_sync_cli_falls_back_to_the_event_default_branch(
    cli_upsert, monkeypatch, tmp_path
):
    """A detached checkout takes its base from the CI event payload."""
    monkeypatch.setattr("repomatic.cli.current_branch", lambda: None)
    event = tmp_path / "event.json"
    event.write_text(
        json.dumps({"repository": {"default_branch": "trunk"}}), encoding="UTF-8"
    )
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    result = CliRunner().invoke(
        repomatic, ["pr-sync", "--template", "fix-changelog"], catch_exceptions=False
    )
    assert result.exit_code == 0
    assert cli_upsert["base"] == "trunk"


def test_pr_sync_cli_explicit_labels_override_frontmatter(cli_upsert, monkeypatch):

    monkeypatch.setattr("repomatic.cli.current_branch", lambda: "main")
    result = CliRunner().invoke(
        repomatic,
        ["pr-sync", "--template", "format-python", "--label", "🍉 special"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    assert cli_upsert["labels"] == ("🍉 special",)


@pytest.mark.parametrize(
    ("args", "error"),
    (
        # A template renders the trio; passing both is ambiguous.
        (
            ["--template", "format-python", "--title", "T"],
            "rendered from the template",
        ),
        # Without a template the trio is mandatory.
        (["--title", "T", "--body", "B"], "--commit-message are required"),
        # And so is the branch.
        (
            ["--title", "T", "--body", "B", "--commit-message", "M"],
            "--branch is required",
        ),
    ),
)
def test_pr_sync_cli_validates_its_inputs(cli_upsert, args, error):

    result = CliRunner().invoke(repomatic, ["pr-sync", *args])
    assert result.exit_code != 0
    assert error in result.output


@pytest.mark.parametrize(
    ("tip", "restored", "expected"),
    (
        pytest.param(None, (), (), id="no-branch"),
        pytest.param("abc123", (), (), id="branch-without-the-file"),
        pytest.param("abc123", ("store.csv",), ("store.csv",), id="work-pending"),
    ),
)
def test_carry_pr_branch_paths_outcomes(monkeypatch, tip, restored, expected):
    """Work travels only when the branch exists and carries the named file.

    Every other outcome is the first run of a fresh accrual, which must be a
    silent no-op rather than a failure: the branch is legitimately gone after
    each merge.
    """
    monkeypatch.setattr(
        "repomatic.github.pr.git_ops.fetch_remote_branch", lambda *a, **kw: tip
    )
    monkeypatch.setattr(
        "repomatic.github.pr.git_ops.restore_paths", lambda *a, **kw: restored
    )
    assert carry_pr_branch_paths(BRANCH, ("store.csv",)) == expected


def test_carry_pr_branch_paths_restores_the_fetched_tip(monkeypatch):
    """The restore reads the tip just fetched, never the branch name.

    A branch name resolves against whatever remote-tracking ref the checkout
    held before the fetch, which on a CI clone is frequently nothing at all.
    """
    seen: dict[str, object] = {}
    monkeypatch.setattr(
        "repomatic.github.pr.git_ops.fetch_remote_branch", lambda *a, **kw: "dee"
    )
    monkeypatch.setattr(
        "repomatic.github.pr.git_ops.restore_paths",
        lambda ref, paths: seen.update(ref=ref, paths=tuple(paths)) or ("store.csv",),
    )
    assert carry_pr_branch_paths(BRANCH, ("store.csv",)) == ("store.csv",)
    assert seen == {"ref": "dee", "paths": ("store.csv",)}
