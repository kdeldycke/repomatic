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

"""Tests for :mod:`repomatic.github.actions` run cancellation and report labels."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from repomatic.github.actions import ReportAction, cancel_superseded_runs
from repomatic.github.sponsor import (
    get_default_author,
    get_default_number,
    is_pull_request,
)

# -- ReportAction -------------------------------------------------------------


@pytest.mark.parametrize(
    ("action", "expected"),
    (
        (ReportAction.DRY_RUN, "\U0001f441\ufe0f Dry-run"),
        (ReportAction.FAILED, "\u26a0\ufe0f Failed"),
        (ReportAction.SKIPPED, "\u2705 In sync"),
        (ReportAction.UNSUBSCRIBED, "\U0001f515 Unsubscribed"),
        (ReportAction.UPDATED, "\U0001f504 Updated"),
    ),
)
def test_report_action_label(action, expected):
    """Each action carries its own emoji-and-label string as its value."""
    assert action.value == expected


def test_report_action_labels_are_distinct():
    """No two actions render the same cell, which would make a report unreadable."""
    values = [action.value for action in ReportAction]
    assert len(set(values)) == len(values)


# -- Event payload readers ----------------------------------------------------
#
# These live in `repomatic.github.sponsor` but read `get_github_event()`, this
# module's view of the event payload, and `sponsor` has no test module of its
# own.


_PR_EVENT = {"pull_request": {"number": 7, "user": {"login": "papaya"}}}
_ISSUE_EVENT = {"issue": {"number": 12, "user": {"login": "quince"}}}


@pytest.mark.parametrize(
    ("event", "is_pr", "author", "number"),
    (
        (_PR_EVENT, True, "papaya", 7),
        (_ISSUE_EVENT, False, "quince", 12),
        # Both nodes present: the pull request wins, and every reader agrees
        # on which node it read.
        ({**_PR_EVENT, **_ISSUE_EVENT}, True, "papaya", 7),
        ({}, False, None, None),
        # An empty `pull_request` object is not a pull request. Key presence
        # alone used to say otherwise, splitting `is_pull_request` from the
        # lookups that fell through to the issue below it.
        ({"pull_request": {}, **_ISSUE_EVENT}, False, "quince", 12),
        # A subject with neither a user nor a number degrades to None rather
        # than raising.
        ({"issue": {"title": "no user, no number"}}, False, None, None),
    ),
)
def test_event_subject_readers_agree(event, is_pr, author, number):
    """All three payload readers resolve the same subject node."""
    # Patched in `sponsor`, not `actions`: the readers bound the name at
    # import time, and it also sidesteps the `lru_cache` on the real one.
    with patch("repomatic.github.sponsor.get_github_event", return_value=event):
        assert is_pull_request() is is_pr
        assert get_default_author() == author
        assert get_default_number() == number


# -- cancel_superseded_runs ---------------------------------------------------


def _dispatch(listings: dict[str, str], cancel_error: str = ""):
    """Build a `run_gh_command` double serving listings and cancellations.

    :param listings: Status name to newline-separated run IDs returned by
        the listing call.
    :param cancel_error: When set, every cancellation call raises a
        `RuntimeError` with this message.
    """

    def run(args):
        # The endpoint is the last argument of the cancel call and precedes
        # `--jq` in the listing call.
        if "/cancel" in args[-1]:
            if cancel_error:
                raise RuntimeError(cancel_error)
            return ""
        endpoint = args[2]
        for status, ids in listings.items():
            if f"status={status}" in endpoint:
                return ids
        return ""

    return run


def test_cancel_superseded_runs_spares_current_run():
    """Every listed run is cancelled except the caller's own."""
    run = _dispatch({"in_progress": "11\n22", "queued": "33"})
    with patch("repomatic.github.actions.run_gh_command", side_effect=run) as mock_gh:
        cancelled = cancel_superseded_runs("melon-branch", "22")
    assert cancelled == 2
    cancel_calls = [
        call.args[0] for call in mock_gh.call_args_list if "/cancel" in call.args[0][-1]
    ]
    assert [args[-1] for args in cancel_calls] == [
        "repos/{owner}/{repo}/actions/runs/11/cancel",
        "repos/{owner}/{repo}/actions/runs/33/cancel",
    ]


def test_cancel_superseded_runs_queries_both_statuses():
    """Both the in-progress and queued listings are swept, paginated."""
    run = _dispatch({})
    with patch("repomatic.github.actions.run_gh_command", side_effect=run) as mock_gh:
        cancelled = cancel_superseded_runs("melon-branch", "1")
    assert cancelled == 0
    listing_calls = [call.args[0] for call in mock_gh.call_args_list]
    assert len(listing_calls) == 2
    for args, status in zip(listing_calls, ("in_progress", "queued")):
        assert args[1] == "--paginate"
        assert f"branch=melon-branch&status={status}" in args[2]


def test_cancel_superseded_runs_tolerates_cancel_failure():
    """A run that fails to cancel is skipped without aborting the sweep."""
    run = _dispatch({"queued": "44\n55"}, cancel_error="HTTP 409: already done")
    with patch("repomatic.github.actions.run_gh_command", side_effect=run):
        cancelled = cancel_superseded_runs("melon-branch", "1")
    assert cancelled == 0


def test_cancel_superseded_runs_empty_listing():
    """No live runs means no cancellation calls and a zero count."""
    run = _dispatch({"in_progress": "", "queued": ""})
    with patch("repomatic.github.actions.run_gh_command", side_effect=run) as mock_gh:
        cancelled = cancel_superseded_runs("melon-branch", "1")
    assert cancelled == 0
    assert all("/cancel" not in call.args[0][-1] for call in mock_gh.call_args_list)
