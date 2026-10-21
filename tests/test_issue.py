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
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from repomatic.github.issue import (
    ISSUE_LIST_LIMIT,
    Triage,
    close_issue,
    create_issue,
    list_issues,
    manage_issue_lifecycle,
    reopen_issue,
    triage_issues,
)
from repomatic.github.pr_body import GITHUB_BODY_MAX_CHARS

TITLE = "Papaya harvest report"
"""Arbitrary issue title the triage tests match against."""

LOCKED_ERROR_MESSAGE = (
    "GraphQL: Unable to create comment because issue is locked (addComment)"
)
"""Verbatim `gh` failure when GitHub refuses a comment on a locked conversation."""


@contextmanager
def patch_every_gh(**mock_kwargs):
    """Patch the three `gh` dispatch points an issue lifecycle crosses.

    Listings parse through the `gh` module, comments go through the lock
    recovery of the threads module, and the rest is this module's own. One
    mock keeps a test's call sequence in one list.
    """
    mock_gh = Mock(**mock_kwargs)
    with (
        patch("repomatic.github.issue.run_gh_command", mock_gh),
        patch("repomatic.github.gh.run_gh_command", mock_gh),
        patch("repomatic.github.threads.run_gh_command", mock_gh),
        patch("repomatic.github.issue.Metadata"),
        patch("repomatic.github.issue.generate_pr_metadata_block", return_value=""),
    ):
        yield mock_gh


# ---------------------------------------------------------------------------
# Issue triage tests
# ---------------------------------------------------------------------------


def _issue(number: int, created: str, state: str = "OPEN", title: str = TITLE) -> dict:
    """Build one entry of the `gh issue list` payload `triage_issues` reads."""
    return {
        "number": number,
        "title": title,
        "createdAt": f"{created}T00:00:00Z",
        "state": state,
    }


@pytest.mark.parametrize(
    ("issues", "needed", "expected"),
    (
        pytest.param([], True, (None, None, set()), id="empty-needed"),
        pytest.param([], False, (None, None, set()), id="empty-not-needed"),
        pytest.param(
            [_issue(42, "2025-01-01")],
            True,
            (42, "OPEN", set()),
            id="one-open-kept",
        ),
        pytest.param(
            [_issue(42, "2025-01-01")],
            False,
            (None, None, {42}),
            id="one-open-closed",
        ),
        # A closed match is handed back for reopening when still needed, and
        # left alone when not: closing it twice would be a no-op API call.
        pytest.param(
            [_issue(42, "2025-01-01", state="CLOSED")],
            True,
            (42, "CLOSED", set()),
            id="one-closed-reopened",
        ),
        pytest.param(
            [_issue(42, "2025-01-01", state="CLOSED")],
            False,
            (None, None, set()),
            id="one-closed-skipped",
        ),
        # The most recent match survives; its older siblings are swept.
        pytest.param(
            [
                _issue(10, "2024-06-01"),
                _issue(42, "2025-01-01"),
                _issue(5, "2024-01-01"),
            ],
            True,
            (42, "OPEN", {10, 5}),
            id="newest-kept-rest-closed",
        ),
        pytest.param(
            [_issue(10, "2024-06-01"), _issue(42, "2025-01-01")],
            False,
            (None, None, {10, 42}),
            id="all-open-closed",
        ),
        pytest.param(
            [_issue(10, "2024-06-01", state="CLOSED"), _issue(42, "2025-01-01")],
            False,
            (None, None, {42}),
            id="already-closed-not-reclosed",
        ),
    ),
)
def test_triage_issues(issues, needed, expected):
    """Triage picks the surviving issue and the ones to close."""
    assert triage_issues(issues, needed) == Triage(*expected)


def test_list_issues_keeps_exact_title_matches():
    """The full-text search over-returns; only the exact title survives."""
    listing = [
        _issue(1, "2025-06-01", title="Other issue"),
        _issue(42, "2025-01-01"),
        _issue(2, "2025-03-01", title=f"{TITLE} (draft)"),
    ]
    with patch_every_gh(return_value=json.dumps(listing)) as mock_gh:
        issues = list_issues(TITLE, repository="kevin/fruits")
    assert [issue["number"] for issue in issues] == [42]
    args = mock_gh.call_args.args[0]
    assert args[args.index("--limit") + 1] == str(ISSUE_LIST_LIMIT)
    assert args[args.index("--repo") + 1] == "kevin/fruits"
    assert args[args.index("--search") + 1] == f"{TITLE} in:title"


def test_lifecycle_trims_oversized_body_before_writing():
    """Oversized issue bodies are trimmed before they ever reach `gh`.

    The GitHub API rejects bodies over the size limit outright, so the
    lifecycle helper fits the rendered body before writing it to disk,
    marking the cut with a truncation notice.
    """
    bodies: list[str] = []

    def fake_gh(args):
        if args[:2] == ["issue", "list"]:
            return "[]"
        if args[:2] == ["issue", "create"]:
            body_path = Path(args[args.index("--body-file") + 1])
            bodies.append(body_path.read_text(encoding="UTF-8"))
            return "https://github.com/user/repo/issues/1\n"
        return ""

    oversized = "\n".join(["- broken link report row 🍈"] * 5000)
    with patch_every_gh(side_effect=fake_gh):
        manage_issue_lifecycle(has_issues=True, body=oversized, labels=[], title=TITLE)

    (fitted,) = bodies
    assert len(fitted.encode("utf-16-le")) // 2 <= GITHUB_BODY_MAX_CHARS
    assert "> Report truncated to fit GitHub's body size limit." in fitted


@pytest.mark.parametrize(
    ("listing", "expected_number", "expected_verbs"),
    (
        pytest.param([], 7, ["list", "create"], id="created"),
        pytest.param([_issue(42, "2025-01-01")], 42, ["list", "edit"], id="updated"),
    ),
)
def test_lifecycle_returns_the_issue_it_keeps(listing, expected_number, expected_verbs):
    """The number of the issue the condition lives in comes back to the caller.

    A new issue gets the assignees: an existing one keeps the assignees it has,
    so a re-run updates it instead of opening a second one.
    """
    calls: list[list[str]] = []

    def fake_gh(args):
        calls.append(list(args))
        if args[:2] == ["issue", "list"]:
            return json.dumps(listing)
        if args[:2] == ["issue", "create"]:
            return "https://github.com/user/repo/issues/7\n"
        return ""

    with patch_every_gh(side_effect=fake_gh):
        number = manage_issue_lifecycle(
            has_issues=True,
            body="The crate is empty.",
            labels=[],
            title=TITLE,
            assignees=("kevin",),
        )

    assert number == expected_number
    assert [call[1] for call in calls] == expected_verbs
    created = [call for call in calls if call[1] == "create"]
    for call in created:
        assert call[call.index("--assignee") + 1] == "kevin"


def test_lifecycle_returns_nothing_when_no_issue_is_needed():
    with patch_every_gh(return_value="[]"):
        assert (
            manage_issue_lifecycle(has_issues=False, body="", labels=[], title=TITLE)
            is None
        )


def test_lifecycle_targets_the_repository_given():
    """Every call of the lifecycle names the repository, when one is given."""
    listing = json.dumps([_issue(42, "2025-01-01")])
    with patch_every_gh(return_value=listing) as mock_gh:
        manage_issue_lifecycle(
            has_issues=False,
            body="",
            labels=[],
            title=TITLE,
            repository="kevin/fruits",
        )
    for call in mock_gh.call_args_list:
        args = call.args[0]
        assert args[args.index("--repo") + 1] == "kevin/fruits"


# ---------------------------------------------------------------------------
# Locked conversations
# ---------------------------------------------------------------------------

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
    answers = [RuntimeError(LOCKED_ERROR_MESSAGE), "", ""]
    with patch_every_gh(side_effect=answers) as mock_gh:
        operation()

    commands = [call.args[0] for call in mock_gh.call_args_list]
    assert commands[0][:3] == ["issue", verb, "42"]
    assert commands[1] == ["issue", "unlock", "42"]
    assert commands[2] == commands[0]


@pytest.mark.parametrize(("operation", "verb"), COMMENTING_OPERATIONS)
def test_unlocked_conversation_costs_no_extra_call(operation, verb):
    """The common path stays a single `gh` call: no speculative unlock."""
    with patch_every_gh(return_value="") as mock_gh:
        operation()

    commands = [call.args[0] for call in mock_gh.call_args_list]
    assert len(commands) == 1
    assert commands[0][:3] == ["issue", verb, "42"]


def test_locked_closed_issue_is_reopened_and_updated():
    """A closed issue locked by the autolock workflow still gets reopened.

    Reproduces the deadlock between two components this project ships: the
    autolock job locks a recurring issue 90 days after it closes, then the
    checker that owns the issue needs to reopen it when the condition recurs
    and has its reopen comment refused.
    """
    listing = json.dumps([_issue(42, "2025-01-01", state="CLOSED")])
    calls: list[list[str]] = []

    def fake_gh(args):
        calls.append(list(args))
        if args[:2] == ["issue", "list"]:
            return listing
        # The lock only lifts once the unlock has actually been issued.
        if args[:2] == ["issue", "reopen"] and ["issue", "unlock", "42"] not in calls:
            raise RuntimeError(LOCKED_ERROR_MESSAGE)
        return ""

    with patch_every_gh(side_effect=fake_gh):
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


# -- create_issue number parsing ----------------------------------------------


@pytest.mark.parametrize(
    ("output", "expected"),
    (
        ("https://github.com/user/repo/issues/123\n", 123),
        # A trailing slash is tolerated.
        ("https://github.com/user/repo/issues/7/\n", 7),
        # gh prepends advisory lines of its own: the number is read off the URL
        # line, whatever gh printed above it.
        (
            "Warning: 3 uncommitted changes\nhttps://github.com/user/repo/issues/42\n",
            42,
        ),
        (
            (
                "\nA new release of gh is available: 2.40.0 -> 2.81.0\n"
                "https://github.com/user/repo/issues/9\n"
            ),
            9,
        ),
    ),
)
def test_create_issue_reads_the_number_off_the_url_line(tmp_path, output, expected):
    """The issue number comes from the URL line, whatever gh printed above it."""
    body_file = tmp_path / "body.md"
    body_file.write_text("Papaya crate is empty.", encoding="UTF-8")
    with patch("repomatic.github.issue.run_gh_command", return_value=output):
        assert create_issue(body_file, ["🍈 fruit"], "Papaya") == expected


@pytest.mark.parametrize(
    "output",
    (
        "",
        "   \n",
        "Something went sideways\n",
        "https://github.com/user/repo/issues/\n",
    ),
)
def test_create_issue_rejects_unparsable_output(tmp_path, output):
    """No readable number raises, rather than returning a bogus issue number."""
    body_file = tmp_path / "body.md"
    body_file.write_text("Papaya crate is empty.", encoding="UTF-8")
    with (
        patch("repomatic.github.issue.run_gh_command", return_value=output),
        pytest.raises(RuntimeError, match="Could not read a thread number"),
    ):
        create_issue(body_file, [], "Papaya")
