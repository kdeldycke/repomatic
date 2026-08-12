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
    create_issue,
    manage_issue_lifecycle,
    reopen_issue,
    triage_issues,
)
from repomatic.github.pr_body import GITHUB_BODY_MAX_CHARS

TITLE = "Papaya harvest report"
"""Arbitrary issue title the triage tests match against."""


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
        pytest.param([], True, (True, None, None, set()), id="empty-needed"),
        pytest.param([], False, (False, None, None, set()), id="empty-not-needed"),
        pytest.param(
            [_issue(1, "2025-01-01", title="Other issue")],
            True,
            (True, None, None, set()),
            id="no-title-match-needed",
        ),
        pytest.param(
            [_issue(1, "2025-01-01", title="Other issue")],
            False,
            (False, None, None, set()),
            id="no-title-match-not-needed",
        ),
        pytest.param(
            [_issue(42, "2025-01-01")],
            True,
            (True, 42, "OPEN", set()),
            id="one-open-kept",
        ),
        pytest.param(
            [_issue(42, "2025-01-01")],
            False,
            (False, None, None, {42}),
            id="one-open-closed",
        ),
        # A closed match is handed back for reopening when still needed, and
        # left alone when not: closing it twice would be a no-op API call.
        pytest.param(
            [_issue(42, "2025-01-01", state="CLOSED")],
            True,
            (True, 42, "CLOSED", set()),
            id="one-closed-reopened",
        ),
        pytest.param(
            [_issue(42, "2025-01-01", state="CLOSED")],
            False,
            (False, None, None, set()),
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
            (True, 42, "OPEN", {10, 5}),
            id="newest-kept-rest-closed",
        ),
        pytest.param(
            [_issue(10, "2024-06-01"), _issue(42, "2025-01-01")],
            False,
            (False, None, None, {10, 42}),
            id="all-open-closed",
        ),
        pytest.param(
            [_issue(10, "2024-06-01", state="CLOSED"), _issue(42, "2025-01-01")],
            False,
            (False, None, None, {42}),
            id="already-closed-not-reclosed",
        ),
        pytest.param(
            [
                _issue(1, "2025-06-01", title="Other issue"),
                _issue(42, "2025-01-01"),
                _issue(10, "2024-06-01"),
                _issue(2, "2025-03-01", title="Another issue"),
            ],
            True,
            (True, 42, "OPEN", {10}),
            id="foreign-titles-ignored",
        ),
        # A payload predating the `state` field reads as open.
        pytest.param(
            [{"number": 42, "title": TITLE, "createdAt": "2025-01-01T00:00:00Z"}],
            True,
            (True, 42, "OPEN", set()),
            id="state-defaults-to-open",
        ),
    ),
)
def test_triage_issues(issues, needed, expected):
    """Triage picks the surviving issue and the ones to close."""
    assert triage_issues(issues, TITLE, needed) == expected


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


# -- create_issue number parsing ----------------------------------------------


@pytest.mark.parametrize(
    ("output", "expected"),
    (
        ("https://github.com/user/repo/issues/123\n", 123),
        # A trailing slash is tolerated, as it always was.
        ("https://github.com/user/repo/issues/7/\n", 7),
        # gh prepends advisory lines of its own. Parsing the whole output
        # turned those into a ValueError that read as a failed creation, even
        # though the issue had been created.
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
        pytest.raises(RuntimeError, match="Could not read the issue number"),
    ):
        create_issue(body_file, [], "Papaya")
