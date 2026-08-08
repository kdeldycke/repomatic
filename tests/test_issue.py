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

"""Tests for :mod:`repomatic.github.issue`."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from repomatic.github.issue import (
    _fit_body_file,
    close_issue,
    close_open_prs_on_branch,
    close_pr,
    list_open_prs_by_branch,
    manage_issue_lifecycle,
    reopen_issue,
    triage_issues,
)
from repomatic.github.pr_body import GITHUB_BODY_MAX_CHARS

TITLE = "Papaya harvest report"
"""Arbitrary issue title the triage tests match against."""


def test_list_open_prs_by_branch_filters_arguments():
    with patch("repomatic.github.issue.run_gh_command") as mock_gh:
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
    with patch("repomatic.github.issue.run_gh_command") as mock_gh:
        mock_gh.return_value = "[]"
        assert list_open_prs_by_branch("major-version-increment") == []


def test_close_pr_default_deletes_branch():
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


def test_close_open_prs_on_branch_no_match_is_noop():
    with patch("repomatic.github.issue.run_gh_command") as mock_gh:
        mock_gh.return_value = "[]"
        closed = close_open_prs_on_branch("minor-version-increment", "stale")
    assert closed == []
    assert mock_gh.call_count == 1


def test_close_open_prs_on_branch_closes_every_match():
    payload = json.dumps([
        {"number": 11, "title": "A"},
        {"number": 22, "title": "B"},
    ])
    with patch("repomatic.github.issue.run_gh_command") as mock_gh:
        mock_gh.side_effect = [payload, "", ""]
        closed = close_open_prs_on_branch("minor-version-increment", "stale")
    assert closed == [11, 22]
    close_args = [call.args[0] for call in mock_gh.call_args_list[1:]]
    assert close_args[0][:3] == ["pr", "close", "11"]
    assert close_args[1][:3] == ["pr", "close", "22"]


# ---------------------------------------------------------------------------
# Issue triage tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("needed", "expected"),
    [
        (True, (True, None, None, set())),
        (False, (False, None, None, set())),
    ],
)
def test_no_matching_issues(needed, expected):
    """No issues match the title."""
    issues = [
        {
            "number": 1,
            "title": "Other issue",
            "createdAt": "2025-01-01T00:00:00Z",
            "state": "OPEN",
        },
    ]
    assert triage_issues(issues, TITLE, needed) == expected


@pytest.mark.parametrize(
    ("needed", "expected"),
    [
        (True, (True, None, None, set())),
        (False, (False, None, None, set())),
    ],
)
def test_empty_issues(needed, expected):
    """Empty issue list returns no matches."""
    assert triage_issues([], TITLE, needed) == expected


def test_one_match_needed():
    """Single matching open issue is kept when needed."""
    issues = [
        {
            "number": 42,
            "title": TITLE,
            "createdAt": "2025-01-01T00:00:00Z",
            "state": "OPEN",
        },
    ]
    assert triage_issues(issues, TITLE, needed=True) == (True, 42, "OPEN", set())


def test_one_match_not_needed():
    """Single matching open issue is closed when not needed."""
    issues = [
        {
            "number": 42,
            "title": TITLE,
            "createdAt": "2025-01-01T00:00:00Z",
            "state": "OPEN",
        },
    ]
    assert triage_issues(issues, TITLE, needed=False) == (False, None, None, {42})


def test_one_closed_match_needed():
    """Single matching closed issue is returned for reopening when needed."""
    issues = [
        {
            "number": 42,
            "title": TITLE,
            "createdAt": "2025-01-01T00:00:00Z",
            "state": "CLOSED",
        },
    ]
    assert triage_issues(issues, TITLE, needed=True) == (True, 42, "CLOSED", set())


def test_one_closed_match_not_needed():
    """Single matching closed issue is skipped when not needed."""
    issues = [
        {
            "number": 42,
            "title": TITLE,
            "createdAt": "2025-01-01T00:00:00Z",
            "state": "CLOSED",
        },
    ]
    assert triage_issues(issues, TITLE, needed=False) == (False, None, None, set())


def test_multiple_matches_needed():
    """Most recent issue is kept, older open ones are closed."""
    issues = [
        {
            "number": 10,
            "title": TITLE,
            "createdAt": "2024-06-01T00:00:00Z",
            "state": "OPEN",
        },
        {
            "number": 42,
            "title": TITLE,
            "createdAt": "2025-01-01T00:00:00Z",
            "state": "OPEN",
        },
        {
            "number": 5,
            "title": TITLE,
            "createdAt": "2024-01-01T00:00:00Z",
            "state": "OPEN",
        },
    ]
    assert triage_issues(issues, TITLE, needed=True) == (True, 42, "OPEN", {10, 5})


def test_multiple_matches_not_needed():
    """All open matching issues are closed when not needed."""
    issues = [
        {
            "number": 10,
            "title": TITLE,
            "createdAt": "2024-06-01T00:00:00Z",
            "state": "OPEN",
        },
        {
            "number": 42,
            "title": TITLE,
            "createdAt": "2025-01-01T00:00:00Z",
            "state": "OPEN",
        },
    ]
    assert triage_issues(issues, TITLE, needed=False) == (False, None, None, {10, 42})


def test_multiple_matches_closed_not_needed():
    """Already-closed issues are skipped when not needed."""
    issues = [
        {
            "number": 10,
            "title": TITLE,
            "createdAt": "2024-06-01T00:00:00Z",
            "state": "CLOSED",
        },
        {
            "number": 42,
            "title": TITLE,
            "createdAt": "2025-01-01T00:00:00Z",
            "state": "OPEN",
        },
    ]
    assert triage_issues(issues, TITLE, needed=False) == (False, None, None, {42})


def test_mixed_titles():
    """Non-matching issues are ignored."""
    issues = [
        {
            "number": 1,
            "title": "Other issue",
            "createdAt": "2025-06-01T00:00:00Z",
            "state": "OPEN",
        },
        {
            "number": 42,
            "title": TITLE,
            "createdAt": "2025-01-01T00:00:00Z",
            "state": "OPEN",
        },
        {
            "number": 10,
            "title": TITLE,
            "createdAt": "2024-06-01T00:00:00Z",
            "state": "OPEN",
        },
        {
            "number": 2,
            "title": "Another issue",
            "createdAt": "2025-03-01T00:00:00Z",
            "state": "OPEN",
        },
    ]
    assert triage_issues(issues, TITLE, needed=True) == (True, 42, "OPEN", {10})


def test_state_defaults_to_open():
    """Issues without a state field default to OPEN for backward compatibility."""
    issues = [
        {"number": 42, "title": TITLE, "createdAt": "2025-01-01T00:00:00Z"},
    ]
    assert triage_issues(issues, TITLE, needed=True) == (True, 42, "OPEN", set())


def test_fit_body_file_rewrites_oversized_body(tmp_path):
    """Oversized issue bodies are trimmed in place before reaching `gh`.

    The GitHub API rejects bodies over the size limit outright, so the
    lifecycle helpers rewrite the body file to fit, keeping the attribution
    footer at the end.
    """
    body_file = tmp_path / "body.md"
    small = "A short issue body."
    body_file.write_text(small, encoding="UTF-8")
    _fit_body_file(body_file)
    assert body_file.read_text(encoding="UTF-8") == small

    oversized = "\n".join(["- broken link report row 🍈"] * 5000)
    body_file.write_text(oversized, encoding="UTF-8")
    _fit_body_file(body_file)
    fitted = body_file.read_text(encoding="UTF-8")
    assert len(fitted.encode("utf-16-le")) // 2 <= GITHUB_BODY_MAX_CHARS
    assert "> Report truncated to fit GitHub's body size limit." in fitted


# ---------------------------------------------------------------------------
# Locked conversations
# ---------------------------------------------------------------------------

LOCKED_ERROR_MESSAGE = (
    "GraphQL: Unable to create comment because issue is locked (addComment)"
)
"""Verbatim `gh` failure when GitHub refuses a comment on a locked conversation."""

COMMENTING_OPERATIONS = (
    pytest.param(lambda: close_issue(42, "Superseded."), "close", id="close_issue"),
    pytest.param(
        lambda: reopen_issue(42, "Condition recurred."), "reopen", id="reopen_issue"
    ),
)
"""Every wrapper that posts a comment, and the `gh issue` verb it invokes."""


@pytest.mark.parametrize(("operation", "verb"), COMMENTING_OPERATIONS)
def test_locked_conversation_is_unlocked_then_retried(operation, verb):
    """A lock blocking a comment is cleared, then the original write replayed."""
    with patch("repomatic.github.issue.run_gh_command") as mock_gh:
        mock_gh.side_effect = [RuntimeError(LOCKED_ERROR_MESSAGE), "", ""]
        operation()

    commands = [call.args[0] for call in mock_gh.call_args_list]
    assert commands[0][:3] == ["issue", verb, "42"]
    assert commands[1] == ["issue", "unlock", "42"]
    assert commands[2] == commands[0]


@pytest.mark.parametrize(("operation", "verb"), COMMENTING_OPERATIONS)
def test_unlocked_conversation_costs_no_extra_call(operation, verb):
    """The common path stays a single `gh` call: no speculative unlock."""
    with patch("repomatic.github.issue.run_gh_command") as mock_gh:
        mock_gh.return_value = ""
        operation()

    commands = [call.args[0] for call in mock_gh.call_args_list]
    assert len(commands) == 1
    assert commands[0][:3] == ["issue", verb, "42"]


@pytest.mark.parametrize(("operation", "verb"), COMMENTING_OPERATIONS)
def test_unrelated_failure_propagates_without_unlocking(operation, verb):
    """Only a lock triggers the unlock: every other failure surfaces as-is."""
    with patch("repomatic.github.issue.run_gh_command") as mock_gh:
        mock_gh.side_effect = RuntimeError("GraphQL: Could not resolve to an Issue")
        with pytest.raises(RuntimeError, match="Could not resolve"):
            operation()

    commands = [call.args[0] for call in mock_gh.call_args_list]
    assert len(commands) == 1
    assert commands[0][1] == verb


@pytest.mark.parametrize(("operation", "verb"), COMMENTING_OPERATIONS)
def test_lock_surviving_the_unlock_still_raises(operation, verb):
    """The retry is bounded to one: a lock that outlives the unlock is fatal."""
    with patch("repomatic.github.issue.run_gh_command") as mock_gh:
        mock_gh.side_effect = [
            RuntimeError(LOCKED_ERROR_MESSAGE),
            "",
            RuntimeError(LOCKED_ERROR_MESSAGE),
        ]
        with pytest.raises(RuntimeError, match="is locked"):
            operation()

    commands = [call.args[0] for call in mock_gh.call_args_list]
    assert [command[1] for command in commands] == [verb, "unlock", verb]


def test_locked_closed_issue_is_reopened_and_updated():
    """A closed issue locked by the autolock workflow still gets reopened.

    Reproduces the deadlock between two components this project ships: the
    autolock job locks a recurring issue 90 days after it closes, then the
    checker that owns the issue needs to reopen it when the condition recurs
    and has its reopen comment refused.
    """
    listing = json.dumps([
        {
            "number": 42,
            "title": TITLE,
            "createdAt": "2025-01-01T00:00:00Z",
            "state": "CLOSED",
        },
    ])
    calls: list[list[str]] = []

    def fake_gh(args):
        calls.append(list(args))
        if args[:2] == ["issue", "list"]:
            return listing
        # The lock only lifts once the unlock has actually been issued.
        if args[:2] == ["issue", "reopen"] and ["issue", "unlock", "42"] not in calls:
            raise RuntimeError(LOCKED_ERROR_MESSAGE)
        return ""

    with (
        patch("repomatic.github.issue.run_gh_command", side_effect=fake_gh),
        patch("repomatic.github.issue.Metadata"),
        patch("repomatic.github.issue.generate_pr_metadata_block", return_value=""),
    ):
        manage_issue_lifecycle(
            has_issues=True,
            body="## Papaya\n\nThe crate is empty.",
            labels=["🍈 fruit"],
            title=TITLE,
        )

    assert [command[1] for command in calls] == [
        "list",
        "reopen",
        "unlock",
        "reopen",
        "edit",
    ]
