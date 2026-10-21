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

"""Tests for :mod:`repomatic.github.runs` listings and run cancellation."""

from __future__ import annotations

import json
from datetime import date, timedelta
from unittest.mock import patch
from urllib.parse import parse_qs

import pytest

from repomatic.github.gh import GhCommandError
from repomatic.github.runs import (
    RUN_LISTING_WINDOW,
    cancel_superseded_runs,
    commit_runs,
    job_log,
    read_run,
    workflow_runs,
)

# -- cancel_superseded_runs ---------------------------------------------------


def _listing(*runs: tuple[int, str]) -> str:
    """One `--slurp` listing: a single page holding *runs* as `(id, title)`."""
    return json.dumps([
        {"workflow_runs": [{"id": i, "display_title": t} for i, t in runs]}
    ])


def _dispatch(listings: dict[str, str], cancel_error: str = ""):
    """Build a `run_gh_command` double serving listings and cancellations.

    :param listings: Status name to the listing call's raw output.
    :param cancel_error: When set, every cancellation call raises a
        `GhCommandError` with this message.
    """

    def run(args):
        if args[-1].endswith("/cancel"):
            if cancel_error:
                raise GhCommandError(cancel_error)
            return ""
        query = parse_qs(args[-1].partition("?")[2])
        return listings.get(query["status"][0], _listing())

    return run


def _cancelled(mock_gh) -> list[str]:
    """The endpoints of the cancellation calls a sweep made, in order."""
    return [
        call.args[0][-1]
        for call in mock_gh.call_args_list
        if call.args[0][-1].endswith("/cancel")
    ]


def test_cancel_superseded_runs_spares_current_run():
    """Every listed run is cancelled except the caller's own."""
    run = _dispatch({
        "in_progress": _listing((11, "Mango"), (22, "Kiwi")),
        "queued": _listing((33, "Papaya")),
    })
    with (
        patch("repomatic.github.gh.run_gh_command", side_effect=run),
        patch("repomatic.github.runs.run_gh_command", side_effect=run) as cancel,
    ):
        cancelled = cancel_superseded_runs("melon-branch", "22")
    assert cancelled == 2
    assert _cancelled(cancel) == [
        "repos/{owner}/{repo}/actions/runs/11/cancel",
        "repos/{owner}/{repo}/actions/runs/33/cancel",
    ]


def test_cancel_superseded_runs_queries_both_statuses():
    """Both the in-progress and queued listings are swept, every page of them."""
    with patch(
        "repomatic.github.gh.run_gh_command", side_effect=_dispatch({})
    ) as mock_gh:
        assert cancel_superseded_runs("melon-branch", "1") == 0
    listing_calls = [call.args[0] for call in mock_gh.call_args_list]
    assert len(listing_calls) == 2
    for args, status in zip(listing_calls, ("in_progress", "queued")):
        assert args[:3] == ["api", "--paginate", "--slurp"]
        query = parse_qs(args[3].partition("?")[2])
        assert query["branch"] == ["melon-branch"]
        assert query["status"] == [status]
        assert query["created"][0].startswith(">=")


def test_cancel_superseded_runs_encodes_the_branch():
    """A branch carrying query syntax stays one parameter of the listing."""
    with patch(
        "repomatic.github.gh.run_gh_command", side_effect=_dispatch({})
    ) as mock_gh:
        cancel_superseded_runs("fix/kiwi&status=completed", "1")
    for call in mock_gh.call_args_list:
        query = parse_qs(call.args[0][3].partition("?")[2])
        assert query["branch"] == ["fix/kiwi&status=completed"]
        assert len(query["status"]) == 1


def test_cancel_superseded_runs_tolerates_cancel_failure():
    """A run that fails to cancel is skipped without aborting the sweep."""
    run = _dispatch(
        {"queued": _listing((44, "Lime"), (55, "Fig"))},
        cancel_error="HTTP 409: already done",
    )
    with (
        patch("repomatic.github.gh.run_gh_command", side_effect=run),
        patch("repomatic.github.runs.run_gh_command", side_effect=run) as cancel,
    ):
        assert cancel_superseded_runs("melon-branch", "1") == 0
    assert len(_cancelled(cancel)) == 2


def test_cancel_superseded_runs_cancels_a_run_listed_twice_once():
    """A run that changed status between the two listings is cancelled once."""
    run = _dispatch({
        "in_progress": _listing((11, "Mango")),
        "queued": _listing((11, "Mango")),
    })
    with (
        patch("repomatic.github.gh.run_gh_command", side_effect=run),
        patch("repomatic.github.runs.run_gh_command", side_effect=run) as cancel,
    ):
        assert cancel_superseded_runs("melon-branch", "1") == 1
    assert len(_cancelled(cancel)) == 1


def test_cancel_superseded_runs_spares_release_runs():
    """A run publishing a release survives the sweep.

    Nothing else protects it: this sweep enters no concurrency group, so the
    release groups of the workflows never shield it, and a cancelled release
    matrix costs that version its binaries for good. A rebase-merged release
    push ends on its post-release bump, so its runs carry that title, not the
    title of the release commit.
    """
    run = _dispatch({
        "in_progress": _listing(
            (11, "Fix the melon parser"), (12, "[changelog] Release v1.2.3")
        ),
        "queued": _listing(
            (13, "[changelog] Post-release bump v1.2.3 → v1.2.4"),
            (14, "Release notes for the melon parser"),
        ),
    })
    with (
        patch("repomatic.github.gh.run_gh_command", side_effect=run),
        patch("repomatic.github.runs.run_gh_command", side_effect=run) as cancel,
    ):
        assert cancel_superseded_runs("main", "1") == 2
    assert _cancelled(cancel) == [
        "repos/{owner}/{repo}/actions/runs/11/cancel",
        "repos/{owner}/{repo}/actions/runs/14/cancel",
    ]


# -- workflow_runs ------------------------------------------------------------


def _runs_page(*run_ids):
    """One page of a workflow's run listing, as the REST API returns it."""
    return json.dumps({"workflow_runs": [{"id": run_id} for run_id in run_ids]})


def _query(call):
    """The decoded query parameters of one `gh api` call."""
    return parse_qs(call.args[0][1].partition("?")[2])


def test_workflow_runs_sends_a_created_filter():
    """The listing names the workflow, the branch, the status and the window."""
    with patch(
        "repomatic.github.gh.run_gh_command", return_value=_runs_page(3, 2)
    ) as mock_gh:
        runs = workflow_runs("tests.yaml", "melon-branch", status="success", limit=2)
    assert [run["id"] for run in runs] == [3, 2]
    endpoint = mock_gh.call_args.args[0][1]
    assert endpoint.startswith(
        "repos/{owner}/{repo}/actions/workflows/tests.yaml/runs?"
    )
    query = _query(mock_gh.call_args)
    assert query["branch"] == ["melon-branch"]
    assert query["status"] == ["success"]
    assert query["per_page"] == ["2"]
    created = query["created"][0]
    assert created.startswith(">=")
    since = date.fromisoformat(created.removeprefix(">="))
    # Computed in UTC, compared against the local date: a day of slack.
    assert abs(date.today() - RUN_LISTING_WINDOW - since) <= timedelta(days=1)


def test_workflow_runs_pages_up_to_the_limit():
    """A limit past one page reads the next page, then stops at the limit."""
    pages = [_runs_page(*range(100)), _runs_page(*range(100, 200))]
    with patch("repomatic.github.gh.run_gh_command", side_effect=pages) as mock_gh:
        runs = workflow_runs("tests.yaml", "main", limit=150)
    assert [run["id"] for run in runs] == list(range(150))
    assert [_query(call)["page"] for call in mock_gh.call_args_list] == [["1"], ["2"]]


def test_workflow_runs_stops_on_a_short_page():
    """A page shorter than asked is the last one: no further call is made."""
    with patch(
        "repomatic.github.gh.run_gh_command", return_value=_runs_page(5)
    ) as mock_gh:
        runs = workflow_runs("tests.yaml", "main", limit=3)
    assert [run["id"] for run in runs] == [5]
    assert mock_gh.call_count == 1


def test_workflow_runs_raises_only_when_strict():
    """A `gh` that cannot run fails a strict read and empties a lenient one."""
    with patch(
        "repomatic.github.gh.run_gh_command", side_effect=GhCommandError("offline")
    ):
        with pytest.raises(GhCommandError, match="offline"):
            workflow_runs("tests.yaml", "main", strict=True)
        assert workflow_runs("tests.yaml", "main") == []


# -- Single reads -------------------------------------------------------------


def test_commit_runs_reads_the_commit_listing():
    """One page of the commit's runs, filtered on its full SHA."""
    sha = "a" * 40
    with patch(
        "repomatic.github.gh.run_gh_command", return_value=_runs_page(8, 9)
    ) as mock_gh:
        runs = commit_runs(sha)
    assert [run["id"] for run in runs] == [8, 9]
    query = _query(mock_gh.call_args)
    assert query["head_sha"] == [sha]
    assert query["per_page"] == ["100"]


@pytest.mark.parametrize(
    ("answer", "expected"),
    (
        ('{"jobs": []}', {"jobs": []}),
        ("[]", None),
    ),
)
def test_read_run_requests_the_fields_named(answer, expected):
    """The run is read by ID, and only a JSON object counts as a run."""
    with patch("repomatic.github.gh.run_gh_command", return_value=answer) as mock_gh:
        assert read_run(42, ("jobs", "status")) == expected
    assert mock_gh.call_args.args[0] == ["run", "view", "42", "--json", "jobs,status"]


def test_read_run_soft_swallows_a_failed_call():
    """A soft read of a run `gh` cannot reach is no run, not a traceback."""
    with patch(
        "repomatic.github.gh.run_gh_command", side_effect=GhCommandError("offline")
    ):
        assert read_run(42, ("jobs",), soft=True) is None
        with pytest.raises(GhCommandError):
            read_run(42, ("jobs",))


def test_job_log_reads_the_job_endpoint_and_strips_colours():
    """A job's log is read by ID, with the escape sequences gone on arrival."""
    raw = "2026-10-21T10:00:00Z \x1b[36;1mRun pytest\x1b[0m\n"
    raw += "\x1b[31mFAILED\x1b[0m x\n"
    with patch("repomatic.github.runs.run_gh_command", return_value=raw) as mock_gh:
        assert job_log(42) == "2026-10-21T10:00:00Z Run pytest\nFAILED x\n"
    args = mock_gh.call_args.args[0]
    assert args[:2] == ["api", "repos/{owner}/{repo}/actions/jobs/42/logs"]
    assert "--allow-escape-sequences" in args
