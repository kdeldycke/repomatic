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
from unittest.mock import patch

import pytest

from repomatic.github.pr import (
    PrOperation,
    _needs_push,
    _parse_pr_number,
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
        mock_gh.return_value = json.dumps([{"number": 42, "title": "Bump"}])
        prs = list_open_prs_by_branch("minor-version-increment")
    assert prs == [{"number": 42, "title": "Bump"}]
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
    with patch("repomatic.github.pr.run_gh_command") as mock_gh:
        close_pr(7, "stale")
    args = mock_gh.call_args.args[0]
    assert args[:3] == ["pr", "close", "7"]
    assert "--comment" in args and args[args.index("--comment") + 1] == "stale"
    assert "--delete-branch" in args


def test_close_pr_can_keep_branch():
    with patch("repomatic.github.pr.run_gh_command") as mock_gh:
        close_pr(7, "stale", delete_branch=False)
    assert "--delete-branch" not in mock_gh.call_args.args[0]


def test_close_open_prs_on_branch_no_match_is_noop():
    with patch("repomatic.github.pr.run_gh_command") as mock_gh:
        mock_gh.return_value = "[]"
        closed = close_open_prs_on_branch("minor-version-increment", "stale")
    assert closed == []
    assert mock_gh.call_count == 1


def test_close_open_prs_on_branch_closes_every_match():
    payload = json.dumps([
        {"number": 11, "title": "A"},
        {"number": 22, "title": "B"},
    ])
    with patch("repomatic.github.pr.run_gh_command") as mock_gh:
        mock_gh.side_effect = [payload, "", ""]
        closed = close_open_prs_on_branch("minor-version-increment", "stale")
    assert closed == [11, 22]
    close_args = [call.args[0] for call in mock_gh.call_args_list[1:]]
    assert close_args[0][:3] == ["pr", "close", "11"]
    assert close_args[1][:3] == ["pr", "close", "22"]


@pytest.mark.parametrize(
    ("output", "expected"),
    (
        ("https://github.com/kate/fruits/pull/123", 123),
        ("https://github.com/kate/fruits/pull/123/", 123),
        # `gh` prepends advisory lines of its own; only the last line counts.
        ("Warning: 3 uncommitted changes\nhttps://x/pull/9", 9),
    ),
)
def test_parse_pr_number(output, expected):
    assert _parse_pr_number(output) == expected


@pytest.mark.parametrize(
    "output",
    ("", "no url here", "https://github.com/kate/fruits/pull/not-a-number"),
)
def test_parse_pr_number_rejects_unparsable_output(output):
    with pytest.raises(RuntimeError, match="Could not read the PR number"):
        _parse_pr_number(output)


@pytest.mark.parametrize(
    ("candidate_tree", "remote_tree", "commits_ahead", "expected"),
    (
        # Same tree, exactly one commit on base: the branch is already right.
        ("tree-1", "tree-1", 1, False),
        # The job's output moved.
        ("tree-2", "tree-1", 1, True),
        # Same tree, but the branch is not a lone commit on top of base.
        ("tree-1", "tree-1", 2, True),
        ("tree-1", "tree-1", 0, True),
    ),
)
def test_needs_push(candidate_tree, remote_tree, commits_ahead, expected):
    trees = {CANDIDATE_SHA: candidate_tree, REMOTE_SHA: remote_tree}
    with (
        patch("repomatic.github.pr.git_ops.tree_sha", side_effect=trees.get),
        patch(
            "repomatic.github.pr.git_ops.count_commits_between",
            return_value=commits_ahead,
        ),
    ):
        assert _needs_push(CANDIDATE_SHA, REMOTE_SHA, BASE_SHA) is expected


def test_upsert_pr_requires_a_base_on_detached_head():
    with (
        patch("repomatic.github.pr.git_ops.current_branch", return_value=None),
        pytest.raises(RuntimeError, match="detached"),
    ):
        upsert_pr(BRANCH, "t", "b", "m")


def test_upsert_pr_without_changes_or_branch_does_nothing():
    """The quietest path: nothing to publish and nothing to retire."""
    with (
        patch("repomatic.github.pr.git_ops.current_branch", return_value="main"),
        patch("repomatic.github.pr.git_ops.head_sha", return_value=BASE_SHA),
        patch("repomatic.github.pr.git_ops.fetch_remote_branch", return_value=None),
        patch("repomatic.github.pr.git_ops.stage_all", return_value=False),
        patch("repomatic.github.pr.run_gh_command") as mock_gh,
    ):
        result = upsert_pr(BRANCH, "t", "b", "m")
    assert result.operation == PrOperation.NONE
    assert result.number is None
    mock_gh.assert_not_called()


def test_upsert_pr_retires_branch_and_pr_when_changes_evaporate():
    """The path a skipped action step could never reach."""
    with (
        patch("repomatic.github.pr.git_ops.current_branch", return_value="main"),
        patch("repomatic.github.pr.git_ops.head_sha", return_value=BASE_SHA),
        patch(
            "repomatic.github.pr.git_ops.fetch_remote_branch", return_value=REMOTE_SHA
        ),
        patch("repomatic.github.pr.git_ops.stage_all", return_value=False),
        patch("repomatic.github.pr.git_ops.delete_remote_branch") as mock_delete,
        patch("repomatic.github.pr.run_gh_command") as mock_gh,
    ):
        mock_gh.side_effect = [json.dumps([{"number": 5, "title": "Sync"}]), ""]
        result = upsert_pr(BRANCH, "t", "b", "m")
    assert result == (PrOperation.CLOSED, BRANCH, 5, "")
    # `gh pr close --delete-branch` covers the branch, so no separate delete.
    mock_delete.assert_not_called()
    assert mock_gh.call_args_list[-1].args[0][:3] == ["pr", "close", "5"]


def test_upsert_pr_deletes_an_orphan_branch_with_no_open_pr():
    with (
        patch("repomatic.github.pr.git_ops.current_branch", return_value="main"),
        patch("repomatic.github.pr.git_ops.head_sha", return_value=BASE_SHA),
        patch(
            "repomatic.github.pr.git_ops.fetch_remote_branch", return_value=REMOTE_SHA
        ),
        patch("repomatic.github.pr.git_ops.stage_all", return_value=False),
        patch("repomatic.github.pr.git_ops.delete_remote_branch") as mock_delete,
        patch("repomatic.github.pr.run_gh_command", return_value="[]"),
    ):
        result = upsert_pr(BRANCH, "t", "b", "m")
    assert result.operation == PrOperation.CLOSED
    assert result.number is None
    mock_delete.assert_called_once_with(BRANCH, remote="origin")


def test_upsert_pr_creates_branch_and_pr():
    with (
        patch("repomatic.github.pr.git_ops.current_branch", return_value="main"),
        patch("repomatic.github.pr.git_ops.head_sha", return_value=BASE_SHA),
        patch("repomatic.github.pr.git_ops.fetch_remote_branch", return_value=None),
        patch("repomatic.github.pr.git_ops.stage_all", return_value=True),
        patch("repomatic.github.pr.git_ops.create_branch"),
        patch("repomatic.github.pr.git_ops.commit_staged", return_value=CANDIDATE_SHA),
        patch("repomatic.github.pr.git_ops.force_push_branch") as mock_push,
        patch("repomatic.github.pr.git_ops.checkout") as mock_checkout,
        patch("repomatic.github.pr.git_ops.delete_branch") as mock_delete_local,
        patch("repomatic.github.pr.run_gh_command") as mock_gh,
    ):
        mock_gh.side_effect = ["[]", "https://github.com/kate/fruits/pull/77", ""]
        result = upsert_pr(BRANCH, "Title", "Body", "Message", labels=["🤖 ci"])

    assert result.operation == PrOperation.CREATED
    assert result.number == 77
    # A branch that does not exist yet takes no lease.
    assert mock_push.call_args.kwargs["expected_sha"] is None
    # The checkout is handed back exactly as it was found.
    mock_checkout.assert_called_once_with("main")
    mock_delete_local.assert_called_once()
    create_args = mock_gh.call_args_list[1].args[0]
    assert create_args[:2] == ["pr", "create"]
    assert create_args[create_args.index("--base") + 1] == "main"
    assert create_args[create_args.index("--head") + 1] == BRANCH


def test_upsert_pr_updates_an_existing_pr_and_leases_the_push():
    with (
        patch("repomatic.github.pr.git_ops.current_branch", return_value="main"),
        patch("repomatic.github.pr.git_ops.head_sha", return_value=BASE_SHA),
        patch(
            "repomatic.github.pr.git_ops.fetch_remote_branch", return_value=REMOTE_SHA
        ),
        patch("repomatic.github.pr.git_ops.stage_all", return_value=True),
        patch("repomatic.github.pr.git_ops.create_branch"),
        patch("repomatic.github.pr.git_ops.commit_staged", return_value=CANDIDATE_SHA),
        patch("repomatic.github.pr.git_ops.tree_sha", side_effect=["new", "old"]),
        patch("repomatic.github.pr.git_ops.count_commits_between", return_value=1),
        patch("repomatic.github.pr.git_ops.force_push_branch") as mock_push,
        patch("repomatic.github.pr.git_ops.checkout"),
        patch("repomatic.github.pr.git_ops.delete_branch"),
        patch("repomatic.github.pr.run_gh_command") as mock_gh,
    ):
        mock_gh.side_effect = [json.dumps([{"number": 88, "title": "Old"}]), "", ""]
        result = upsert_pr(BRANCH, "Title", "Body", "Message")

    assert result.operation == PrOperation.UPDATED
    assert result.number == 88
    assert mock_push.call_args.kwargs["expected_sha"] == REMOTE_SHA
    assert mock_gh.call_args_list[1].args[0][:3] == ["pr", "edit", "88"]


def test_upsert_pr_skips_the_push_when_the_branch_already_matches():
    """The check that keeps an unchanged job from re-triggering every workflow."""
    with (
        patch("repomatic.github.pr.git_ops.current_branch", return_value="main"),
        patch("repomatic.github.pr.git_ops.head_sha", return_value=BASE_SHA),
        patch(
            "repomatic.github.pr.git_ops.fetch_remote_branch", return_value=REMOTE_SHA
        ),
        patch("repomatic.github.pr.git_ops.stage_all", return_value=True),
        patch("repomatic.github.pr.git_ops.create_branch"),
        patch("repomatic.github.pr.git_ops.commit_staged", return_value=CANDIDATE_SHA),
        patch("repomatic.github.pr.git_ops.tree_sha", return_value="same"),
        patch("repomatic.github.pr.git_ops.count_commits_between", return_value=1),
        patch("repomatic.github.pr.git_ops.force_push_branch") as mock_push,
        patch("repomatic.github.pr.git_ops.checkout") as mock_checkout,
        patch("repomatic.github.pr.git_ops.delete_branch"),
        patch("repomatic.github.pr.run_gh_command") as mock_gh,
    ):
        result = upsert_pr(BRANCH, "Title", "Body", "Message")

    assert result.operation == PrOperation.NONE
    mock_push.assert_not_called()
    mock_gh.assert_not_called()
    # Even on the no-op path the checkout is restored.
    mock_checkout.assert_called_once_with("main")


def test_upsert_pr_restores_the_checkout_when_the_push_fails():
    with (
        patch("repomatic.github.pr.git_ops.current_branch", return_value="main"),
        patch("repomatic.github.pr.git_ops.head_sha", return_value=BASE_SHA),
        patch("repomatic.github.pr.git_ops.fetch_remote_branch", return_value=None),
        patch("repomatic.github.pr.git_ops.stage_all", return_value=True),
        patch("repomatic.github.pr.git_ops.create_branch"),
        patch("repomatic.github.pr.git_ops.commit_staged", return_value=CANDIDATE_SHA),
        patch(
            "repomatic.github.pr.git_ops.force_push_branch",
            side_effect=RuntimeError("rejected"),
        ),
        patch("repomatic.github.pr.git_ops.checkout") as mock_checkout,
        patch("repomatic.github.pr.git_ops.delete_branch") as mock_delete_local,
        pytest.raises(RuntimeError, match="rejected"),
    ):
        upsert_pr(BRANCH, "Title", "Body", "Message")
    mock_checkout.assert_called_once_with("main")
    mock_delete_local.assert_called_once()


def test_upsert_pr_survives_a_refused_label_or_assignee():
    """`github-actions[bot]` cannot be assigned, and that must not lose the PR."""
    with (
        patch("repomatic.github.pr.git_ops.current_branch", return_value="main"),
        patch("repomatic.github.pr.git_ops.head_sha", return_value=BASE_SHA),
        patch("repomatic.github.pr.git_ops.fetch_remote_branch", return_value=None),
        patch("repomatic.github.pr.git_ops.stage_all", return_value=True),
        patch("repomatic.github.pr.git_ops.create_branch"),
        patch("repomatic.github.pr.git_ops.commit_staged", return_value=CANDIDATE_SHA),
        patch("repomatic.github.pr.git_ops.force_push_branch"),
        patch("repomatic.github.pr.git_ops.checkout"),
        patch("repomatic.github.pr.git_ops.delete_branch"),
        patch("repomatic.github.pr.run_gh_command") as mock_gh,
    ):
        mock_gh.side_effect = [
            "[]",
            "https://github.com/kate/fruits/pull/77",
            RuntimeError("could not assign github-actions[bot]"),
        ]
        result = upsert_pr(
            BRANCH, "Title", "Body", "Message", assignees=["github-actions[bot]"]
        )
    assert result.operation == PrOperation.CREATED
    assert result.number == 77
