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

"""Tests for :mod:`repomatic.github.threads`."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from functools import partial
from unittest.mock import patch

import pytest

from repomatic.github.threads import (
    BOT_ISSUE_LABEL,
    LOCK_ISSUE_COMMENT,
    LOCK_PR_COMMENT,
    LOCK_REASON,
    ThreadKind,
    add_labels,
    lock_stale_threads,
    run_unlocking,
)
from tests.conftest import patch_gh as shared_patch_gh

TITLE = "Papaya harvest report"
"""Arbitrary thread title the locking tests match against."""

LOCKED_ERROR_MESSAGE = (
    "GraphQL: Unable to create comment because issue is locked (addComment)"
)
"""Verbatim `gh` failure when GitHub refuses a comment on a locked conversation."""

patch_gh = partial(shared_patch_gh, "repomatic.github.threads")
"""The conftest `patch_gh`, bound to this module's dispatch points."""


# -- ThreadKind ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("is_pr", "kind", "label"),
    (
        (False, ThreadKind.ISSUE, "issue"),
        (True, ThreadKind.PR, "PR"),
    ),
)
def test_thread_kind_reads_the_flag(is_pr, kind, label):
    """The flag picks the kind, which names its `gh` command and its label."""
    assert ThreadKind.of(is_pr) is kind
    assert kind.label == label


# -- add_labels -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "repository", "expected"),
    (
        (ThreadKind.ISSUE, None, ["issue", "edit", "42"]),
        (ThreadKind.PR, "kevin/fruits", ["pr", "edit", "42", "--repo", "kevin/fruits"]),
    ),
)
def test_add_labels_targets_the_thread(kind, repository, expected):
    """The write names the kind's command, and the repository when given."""
    with patch("repomatic.github.threads.run_gh_command") as mock_gh:
        assert add_labels(42, ["🍈 fruit"], kind=kind, repository=repository)
    args = mock_gh.call_args.args[0]
    assert args[: len(expected)] == expected
    assert args[args.index("--add-label") + 1] == "🍈 fruit"


def test_add_labels_without_anything_to_add_makes_no_call():
    """No label and no assignee is a no-op, not an empty edit."""
    with patch("repomatic.github.threads.run_gh_command") as mock_gh:
        assert add_labels(42, []) is True
    mock_gh.assert_not_called()


def test_add_labels_degrades_a_refusal_to_a_warning():
    """A refused assignee loses the labels, never the job."""
    with patch(
        "repomatic.github.threads.run_gh_command",
        side_effect=RuntimeError("could not assign github-actions[bot]"),
    ):
        assert add_labels(42, ["🍈 fruit"], assignees=["github-actions[bot]"]) is False


# -- run_unlocking --------------------------------------------------------------


def test_locked_conversation_is_unlocked_then_retried():
    """A lock blocking a comment is cleared, then the original write replayed."""
    with patch("repomatic.github.threads.run_gh_command") as mock_gh:
        mock_gh.side_effect = [RuntimeError(LOCKED_ERROR_MESSAGE), "", ""]
        run_unlocking(["issue", "close", "42", "--comment", "Done."], 42)

    commands = [call.args[0] for call in mock_gh.call_args_list]
    assert commands[1] == ["issue", "unlock", "42"]
    assert commands[2] == commands[0]


def test_unlocking_targets_the_repository_of_the_write():
    """The unlock reaches the repository the refused write named."""
    with patch("repomatic.github.threads.run_gh_command") as mock_gh:
        mock_gh.side_effect = [RuntimeError(LOCKED_ERROR_MESSAGE), "", ""]
        run_unlocking(
            ["pr", "close", "7", "--comment", "Stale."],
            7,
            kind=ThreadKind.PR,
            repository="kevin/fruits",
        )

    commands = [call.args[0] for call in mock_gh.call_args_list]
    assert commands[0][-2:] == ["--repo", "kevin/fruits"]
    assert commands[1] == ["pr", "unlock", "7", "--repo", "kevin/fruits"]


def test_unrelated_failure_propagates_without_unlocking():
    """Only a lock triggers the unlock: every other failure surfaces as-is."""
    with patch("repomatic.github.threads.run_gh_command") as mock_gh:
        mock_gh.side_effect = RuntimeError("GraphQL: Could not resolve to an Issue")
        with pytest.raises(RuntimeError, match="Could not resolve"):
            run_unlocking(["issue", "reopen", "42"], 42)
    assert mock_gh.call_count == 1


def test_lock_surviving_the_unlock_still_raises():
    """The retry is bounded to one: a lock that outlives the unlock is fatal."""
    with patch("repomatic.github.threads.run_gh_command") as mock_gh:
        mock_gh.side_effect = [
            RuntimeError(LOCKED_ERROR_MESSAGE),
            "",
            RuntimeError(LOCKED_ERROR_MESSAGE),
        ]
        with pytest.raises(RuntimeError, match="is locked"):
            run_unlocking(["issue", "reopen", "42"], 42)
    commands = [call.args[0] for call in mock_gh.call_args_list]
    assert [command[1] for command in commands] == ["reopen", "unlock", "reopen"]


# -- Thread locking -----------------------------------------------------------


def _thread(
    number: int,
    *,
    is_pr: bool = False,
    labels: tuple[str, ...] = (),
    title: str = TITLE,
) -> dict:
    """Build one entry of the `gh search issues` payload the locker reads."""
    return {
        "number": number,
        "title": title,
        "url": f"https://github.com/kevin/fruits/issues/{number}",
        "isPullRequest": is_pr,
        "labels": [{"name": name} for name in labels],
        "updatedAt": "2025-01-01T00:00:00Z",
    }


def _locking_calls(threads, **kwargs):
    """Run `lock_stale_threads` over *threads*, returning its rows and gh calls.

    Runs live unless the test asks for a dry run: the library default is the
    safe dry run, and most tests here are about what a live run writes.
    """
    kwargs.setdefault("dry_run", False)
    calls: list[list[str]] = []

    def fake_gh(args):
        calls.append(list(args))
        if args[:2] == ["search", "issues"]:
            return json.dumps(threads)
        return ""

    with patch_gh(side_effect=fake_gh):
        rows = lock_stale_threads("kevin/fruits", **kwargs)
    return rows, calls


def test_search_window_counts_back_from_today():
    """The `updated:<` cutoff is `inactive_days` before now, not a fixed date."""
    _, calls = _locking_calls([], inactive_days=30)

    query = calls[0]
    cutoff = query[query.index("--updated") + 1]
    expected = datetime.now(UTC).date() - timedelta(days=30)
    assert cutoff == f"<{expected.isoformat()}"
    # `is:unlocked` is what makes a re-run a no-op: without it the command
    # would re-comment on every thread it locked the week before.
    assert "--locked=false" in query
    assert "--include-prs" in query


@pytest.mark.parametrize(
    ("is_pr", "kind", "comment"),
    (
        pytest.param(False, "issue", LOCK_ISSUE_COMMENT, id="issue"),
        pytest.param(True, "pr", LOCK_PR_COMMENT, id="pr"),
    ),
)
def test_each_kind_is_commented_then_locked_with_its_own_wording(is_pr, kind, comment):
    """Issues and PRs take the `gh` verb and the comment that match their kind."""
    rows, calls = _locking_calls([_thread(42, is_pr=is_pr)])

    assert rows == [("PR" if is_pr else "issue", "#42", TITLE, "locked")]
    writes = [call for call in calls if call[:2] != ["search", "issues"]]
    # The comment lands before the lock: afterwards it would be refused.
    assert [call[:3] for call in writes] == [
        [kind, "comment", "42"],
        [kind, "lock", "42"],
    ]
    assert writes[0][writes[0].index("--body") + 1] == comment
    assert writes[1][writes[1].index("--reason") + 1] == LOCK_REASON


@pytest.mark.parametrize(
    ("labels", "expected"),
    (
        pytest.param((BOT_ISSUE_LABEL,), "skipped (labelled)", id="exact"),
        pytest.param((BOT_ISSUE_LABEL.upper(),), "skipped (labelled)", id="case-fold"),
        pytest.param(("🍈 fruit", BOT_ISSUE_LABEL), "skipped (labelled)", id="among"),
        pytest.param(("🍈 fruit",), "locked", id="unrelated"),
        pytest.param((), "locked", id="unlabelled"),
    ),
)
def test_excluded_labels_spare_a_thread(labels, expected):
    """A thread carrying an excluded label is reported, never written to.

    The default exclusion is the bot label, because those issues are designed
    to reopen when their condition recurs and a lock refuses the reopen
    comment.
    """
    rows, calls = _locking_calls([_thread(42, labels=labels)])

    assert rows == [("issue", "#42", TITLE, expected)]
    wrote = any(call[:2] != ["search", "issues"] for call in calls)
    assert wrote == (expected == "locked")


def test_dry_run_reports_without_writing():
    """The default mode searches and reports, and never comments or locks."""
    rows, calls = _locking_calls(
        [_thread(1), _thread(2, is_pr=True)],
        dry_run=True,
    )

    assert rows == [
        ("issue", "#1", TITLE, "would lock"),
        ("PR", "#2", TITLE, "would lock"),
    ]
    assert [call[:2] for call in calls] == [["search", "issues"]]


def test_empty_comment_skips_the_comment_call():
    """Locking silently is one call, not a comment with an empty body."""
    _, calls = _locking_calls([_thread(42)], issue_comment="")

    writes = [call for call in calls if call[:2] != ["search", "issues"]]
    assert [call[:2] for call in writes] == [["issue", "lock"]]


def test_every_write_names_the_repository():
    """`gh` resolves a repo from the checkout: the locker never relies on that.

    The autolock job runs on a schedule with no pull request in sight, and a
    downstream caller may point the command at another repository entirely, so
    the slug travels on each call rather than through the working directory.
    """
    _, calls = _locking_calls([_thread(1), _thread(2, is_pr=True)])

    for call in calls:
        assert call[call.index("--repo") + 1] == "kevin/fruits"
