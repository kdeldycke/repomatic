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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from repomatic.github.issue import (
    BOT_ISSUE_LABEL,
    LOCK_ISSUE_COMMENT,
    LOCK_PR_COMMENT,
    LOCK_REASON,
    close_issue,
    create_issue,
    lock_stale_threads,
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
    with (
        patch("repomatic.github.issue.run_gh_command", side_effect=fake_gh),
        patch("repomatic.github.issue.Metadata"),
        patch("repomatic.github.issue.generate_pr_metadata_block", return_value=""),
    ):
        manage_issue_lifecycle(has_issues=True, body=oversized, labels=[], title=TITLE)

    (fitted,) = bodies
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
        pytest.raises(RuntimeError, match="Could not read a thread number"),
    ):
        create_issue(body_file, [], "Papaya")


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

    with patch("repomatic.github.issue.run_gh_command", side_effect=fake_gh):
        rows = lock_stale_threads("kevin/fruits", **kwargs)
    return rows, calls


def test_search_window_counts_back_from_today():
    """The `updated:<` cutoff is `inactive_days` before now, not a fixed date."""
    _, calls = _locking_calls([], inactive_days=30)

    query = calls[0]
    cutoff = query[query.index("--updated") + 1]
    expected = datetime.now(timezone.utc).date() - timedelta(days=30)
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
