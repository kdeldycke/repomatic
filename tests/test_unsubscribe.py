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

"""Tests for :mod:`repomatic.github.unsubscribe`."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from functools import partial
from unittest.mock import patch

import pytest

from repomatic.github.actions import ReportAction
from repomatic.github.unsubscribe import (
    GRAPHQL_PAGE_SIZE,
    NOTIFICATION_PAGE_SIZE,
    THREADLESS_SEARCH_QUERY,
    DetailRow,
    UnsubscribeResult,
    _compute_cutoff,
    _fetch_notification_threads,
    _format_link,
    _get_authenticated_username,
    _get_thread_details,
    _graphql_unsubscribe,
    _iter_closed_items,
    _render_detail_table,
    _unsubscribe_rest_thread,
    _validate_notifications_token,
    render_report,
    unsubscribe_threads,
)
from tests.conftest import patch_gh as shared_patch_gh

_MODULE = "repomatic.github.unsubscribe"

# A subject URL well in the past: parses below any realistic cutoff.
STALE = "2020-01-01T00:00:00Z"
# A subject URL far in the future: parses above any realistic cutoff.
RECENT = "2099-01-01T00:00:00Z"

# A stable cutoff for report-rendering tests; its isoformat is asserted below.
CUTOFF = datetime(2026, 4, 16, tzinfo=timezone.utc)
CUTOFF_ISO = "2026-04-16T00:00:00+00:00"


# -- Test helpers -------------------------------------------------------------


def _make_row(
    *,
    action=ReportAction.UNSUBSCRIBED,
    html_url="https://example.com/apple/1",
    number=1,
    repo="fruits/apple",
    title="Sunny afternoon",
    updated_at=None,
) -> DetailRow:
    """Build a `DetailRow` with domain-neutral defaults."""
    return DetailRow(
        action=action,
        html_url=html_url,
        number=number,
        repo=repo,
        title=title,
        updated_at=updated_at,
    )


def _thread_line(
    thread_id: str,
    subject_url: str,
    *,
    repo="fruits/apple",
    title="Sunny",
) -> str:
    """Render one JSON line as `_fetch_notification_threads` receives it."""
    return json.dumps({
        "id": thread_id,
        "repo": repo,
        "subject_type": "Issue",
        "subject_url": subject_url,
        "title": title,
    })


def _closed_detail(
    *,
    state="closed",
    updated_at=STALE,
    number=1,
    html_url="https://github.com/fruits/apple/issues/1",
) -> dict[str, object]:
    """Build a thread-detail dict as `_get_thread_details` returns it."""
    return {
        "state": state,
        "updated_at": updated_at,
        "html_url": html_url,
        "number": number,
    }


def _gql_item(
    node_id: str,
    *,
    subscription="SUBSCRIBED",
    number=1,
    repo="fruits/apple",
    title="Sunny",
    updated_at=STALE,
    url="https://github.com/fruits/apple/issues/1",
) -> dict[str, object]:
    """Build a GraphQL search node as `_iter_closed_items` yields it."""
    return {
        "id": node_id,
        "number": number,
        "title": title,
        "repository": {"nameWithOwner": repo},
        "updatedAt": updated_at,
        "url": url,
        "viewerSubscription": subscription,
    }


def _dispatch_gh(
    *,
    notifications="",
    details=None,
    username="octocat",
    fail_notifications=False,
    fail_username=False,
    fail_delete=frozenset(),
    fail_patch=frozenset(),
    fail_mutation=frozenset(),
):
    """Build a `run_gh_command` side effect that answers by argument shape.

    Routes each gh call to a canned response so `unsubscribe_threads` runs
    end-to-end without a network. `details` maps a subject URL to its detail
    dict; an unlisted URL raises, modeling an inaccessible subject.
    """
    detail_map = details or {}

    def _run(args):
        if args[:4] == ["api", "--method", "GET", "/notifications"]:
            if fail_notifications:
                raise RuntimeError("notifications unavailable")
            return notifications
        if args[:2] == ["api", "/user"]:
            if fail_username:
                raise RuntimeError("no user")
            return f"{username}\n"
        if args[:3] == ["api", "--method", "DELETE"]:
            thread_id = args[3].split("/")[3]
            if thread_id in fail_delete:
                raise RuntimeError("delete failed")
            return ""
        if args[:3] == ["api", "--method", "PATCH"]:
            thread_id = args[3].split("/")[-1]
            if thread_id in fail_patch:
                raise RuntimeError("patch failed")
            return ""
        if args[:2] == ["api", "graphql"]:
            node_id = next(a[len("id=") :] for a in args if a.startswith("id="))
            if node_id in fail_mutation:
                raise RuntimeError("mutation failed")
            return "{}"
        # Remaining shape: a thread-detail lookup keyed by subject URL.
        subject_url = args[1]
        if subject_url not in detail_map:
            raise RuntimeError("subject inaccessible")
        return json.dumps(detail_map[subject_url])

    return _run


patch_gh = partial(shared_patch_gh, _MODULE)
"""The conftest `patch_gh`, bound to this module's dispatch points."""


def _gh_calls_matching(mock_gh, prefix):
    """Return the argument lists of gh calls whose args start with *prefix*."""
    return [
        entry.args[0]
        for entry in mock_gh.call_args_list
        if entry.args[0][: len(prefix)] == prefix
    ]


# -- _compute_cutoff ----------------------------------------------------------


@pytest.mark.parametrize(
    ("fixed_now", "months", "expected"),
    [
        pytest.param(
            datetime(2026, 3, 31, 12, tzinfo=timezone.utc),
            1,
            datetime(2026, 2, 28, 12, tzinfo=timezone.utc),
            id="clamp-to-feb-28",
        ),
        pytest.param(
            datetime(2024, 3, 31, 12, tzinfo=timezone.utc),
            1,
            datetime(2024, 2, 29, 12, tzinfo=timezone.utc),
            id="clamp-to-leap-feb-29",
        ),
        pytest.param(
            datetime(2026, 7, 16, 9, tzinfo=timezone.utc),
            3,
            datetime(2026, 4, 16, 9, tzinfo=timezone.utc),
            id="simple-three-months",
        ),
        pytest.param(
            datetime(2026, 2, 15, tzinfo=timezone.utc),
            3,
            datetime(2025, 11, 15, tzinfo=timezone.utc),
            id="year-rollover",
        ),
        pytest.param(
            datetime(2026, 7, 16, 9, tzinfo=timezone.utc),
            12,
            datetime(2025, 7, 16, 9, tzinfo=timezone.utc),
            id="full-year",
        ),
        pytest.param(
            datetime(2026, 1, 31, 6, tzinfo=timezone.utc),
            1,
            datetime(2025, 12, 31, 6, tzinfo=timezone.utc),
            id="january-to-december",
        ),
    ],
)
def test_compute_cutoff(fixed_now, months, expected):
    """Subtracting months clamps the day and rolls the year over correctly."""
    with patch(f"{_MODULE}.datetime") as mock_dt:
        mock_dt.now.return_value = fixed_now
        assert _compute_cutoff(months) == expected


# -- _format_link -------------------------------------------------------------


@pytest.mark.parametrize(
    ("number", "html_url", "expected"),
    [
        pytest.param(
            7,
            "https://example.com/a/7",
            "[`fruits/apple#7`](https://example.com/a/7)",
            id="number-and-url",
        ),
        pytest.param(
            0,
            "https://example.com/a/0",
            "[`fruits/apple#0`](https://example.com/a/0)",
            id="zero-is-a-valid-number",
        ),
        pytest.param(None, "https://example.com/a/1", "fruits/apple", id="no-number"),
        pytest.param(7, "", "fruits/apple", id="no-url"),
    ],
)
def test_format_link(number, html_url, expected):
    """A link renders only with both a number and a URL, else the repo name."""
    row = _make_row(number=number, html_url=html_url, repo="fruits/apple")
    assert _format_link(row) == expected


# -- _render_detail_table -----------------------------------------------------


def test_render_detail_table_empty():
    """No rows renders to an empty string, not a headerless table."""
    assert _render_detail_table([]) == ""


def test_render_detail_table_exact():
    """Rows render as a headed markdown table with one line each."""
    rows = [
        _make_row(
            action=ReportAction.UNSUBSCRIBED,
            html_url="https://example.com/apple/7",
            number=7,
            repo="fruits/apple",
            title="Sunny afternoon",
            updated_at=None,
        ),
        _make_row(
            action=ReportAction.FAILED,
            html_url="",
            number=None,
            repo="cities/paris",
            title="Rainy morning",
            updated_at=None,
        ),
    ]
    expected_lines = [
        "### \U0001f4dd Details",
        "",
        (
            "| \U0001f4ac Title | \U0001f517 Link"
            " | \U0001f550 Last activity | \u26a1 Action |"
        ),
        "| --- | --- | --- | --- |",
        (
            "| Sunny afternoon | [`fruits/apple#7`](https://example.com/apple/7)"
            " | - | \U0001f515 Unsubscribed |"
        ),
        "| Rainy morning | cities/paris | - | \u26a0\ufe0f Failed |",
    ]
    expected = "\n".join(expected_lines)
    assert _render_detail_table(rows) == expected


def test_render_detail_table_humanizes_timestamp():
    """A real timestamp renders a relative string, not the '-' placeholder."""
    row = _make_row(updated_at=datetime(2020, 1, 1, tzinfo=timezone.utc))
    table = _render_detail_table([row])
    assert " | - | " not in table
    assert "ago" in table


# -- render_report ------------------------------------------------------------


def test_render_report_phase1_dry_run_summary():
    """Dry-run phase 1 counts candidate rows and marks the mode."""
    result = UnsubscribeResult(dry_run=True, months=3)
    result.phase1.cutoff = CUTOFF
    result.phase1.rows = [_make_row(), _make_row()]
    report = render_report(result)
    assert "Unsubscribe report (dry-run)" in report
    assert (
        "\U0001f50d **Candidates found:** 2"
        f" \u2014 cutoff: `{CUTOFF_ISO}`"
        " (inactive for more than 3 months, dry-run)"
    ) in report


def test_render_report_phase1_live_summary():
    """Live phase 1 reports unsubscribed and failed counts."""
    result = UnsubscribeResult(dry_run=False, months=6)
    result.phase1.cutoff = CUTOFF
    result.phase1.threads_unsubscribed = 5
    result.phase1.threads_failed = 1
    report = render_report(result)
    assert "Unsubscribe report (live)" in report
    assert (
        "\U0001f515 **Unsubscribed:** 5"
        " | \u26a0\ufe0f **Failed:** 1"
        f" \u2014 cutoff: `{CUTOFF_ISO}`"
        " (inactive for more than 6 months)"
    ) in report


@pytest.mark.parametrize(
    ("oldest_updated", "threads_total", "expect_warning"),
    [
        pytest.param(
            datetime(2026, 5, 1, tzinfo=timezone.utc), 100, True, id="stale-backlog"
        ),
        pytest.param(
            datetime(2026, 3, 1, tzinfo=timezone.utc),
            100,
            False,
            id="oldest-below-cutoff",
        ),
        pytest.param(
            datetime(2026, 5, 1, tzinfo=timezone.utc), 10, False, id="fully-inspected"
        ),
    ],
)
def test_render_report_backlog_warning(oldest_updated, threads_total, expect_warning):
    """The backlog warning fires only for an unreached, still-recent batch."""
    result = UnsubscribeResult(dry_run=False, months=3)
    p1 = result.phase1
    p1.cutoff = CUTOFF
    p1.batch_size = 200
    p1.threads_total = threads_total
    p1.threads_inspected = 10
    p1.oldest_updated = oldest_updated
    report = render_report(result)
    assert ("Oldest activity seen in this batch" in report) is expect_warning


def test_render_report_phase2_skipped():
    """A skipped phase 2 renders its reason inside a warning admonition."""
    result = UnsubscribeResult(dry_run=True, months=3)
    result.phase2.skipped = True
    result.phase2.skip_reason = "Fine-grained PAT unsupported."
    report = render_report(result)
    assert "Threadless subscriptions (dry-run)" in report
    assert "> [!WARNING]" in report
    assert "Fine-grained PAT unsupported." in report


def test_render_report_phase2_content():
    """An active phase 2 renders its summary, search table, and query."""
    result = UnsubscribeResult(dry_run=True, months=3)
    p2 = result.phase2
    p2.cutoff = CUTOFF
    p2.batch_size = 200
    p2.graphql_total = 3
    p2.graphql_not_subscribed = 1
    p2.search_query = "involves:fruitbot is:closed updated:<2026-04-16"
    p2.rows = [_make_row(action=ReportAction.DRY_RUN)]
    report = render_report(result)
    assert "Search details" in report
    assert "involves:fruitbot is:closed updated:<2026-04-16" in report
    assert (
        "\U0001f50d **Candidates found:** 1"
        f" \u2014 cutoff: `{CUTOFF_ISO}`"
        " (inactive for more than 3 months, dry-run)"
    ) in report


def test_render_report_empty_result():
    """A default result renders both phases plus the attribution footer."""
    report = render_report(UnsubscribeResult())
    assert "Unsubscribe report (live)" in report
    assert "Threadless subscriptions (live)" in report
    assert "Generated with" in report


# -- _fetch_notification_threads ----------------------------------------------


def test_fetch_notification_threads_reverses_and_truncates():
    """Threads are reversed to oldest-first, then cut to the batch size."""
    lines = [
        json.dumps({"id": "t0", "subject_url": "u0"}),
        json.dumps({"id": "t1", "subject_url": "u1"}),
        json.dumps({"id": "t2", "subject_url": "u2"}),
    ]
    with patch_gh(return_value="\n".join(lines)):
        total, batch = _fetch_notification_threads(2)
    assert total == 3
    assert [t["id"] for t in batch] == ["t2", "t1"]


def test_fetch_notification_threads_skips_malformed_lines():
    """Blank and unparsable lines are dropped without failing the fetch."""
    lines = [
        json.dumps({"id": "t0", "subject_url": "u0"}),
        "not-json{",
        "",
        json.dumps({"id": "t1", "subject_url": "u1"}),
    ]
    with patch_gh(return_value="\n".join(lines)):
        total, batch = _fetch_notification_threads(10)
    assert total == 2
    assert {t["id"] for t in batch} == {"t0", "t1"}


def test_fetch_notification_threads_gh_failure_returns_empty():
    """A gh failure degrades to a zero total and an empty batch."""
    with patch_gh(side_effect=RuntimeError("boom")):
        assert _fetch_notification_threads(10) == (0, [])


def test_fetch_notification_threads_query_args():
    """The fetch requests both subject types with pagination and all=true."""
    with patch_gh(return_value="") as mock_gh:
        _fetch_notification_threads(10)
    args = mock_gh.call_args.args[0]
    assert args[:4] == ["api", "--method", "GET", "/notifications"]
    assert "--paginate" in args
    assert "all=true" in args
    assert f"per_page={NOTIFICATION_PAGE_SIZE}" in args
    jq_filter = args[args.index("--jq") + 1]
    assert '"Issue"' in jq_filter
    assert '"PullRequest"' in jq_filter


# -- _get_thread_details ------------------------------------------------------


def test_get_thread_details_success():
    """A well-formed response is parsed and returned verbatim."""
    detail = _closed_detail()
    with patch_gh(return_value=json.dumps(detail)):
        assert _get_thread_details("https://api.github.com/x") == detail


def test_get_thread_details_inaccessible_returns_none():
    """A gh failure (inaccessible subject) yields None."""
    with patch_gh(side_effect=RuntimeError("404")):
        assert _get_thread_details("https://api.github.com/x") is None


def test_get_thread_details_malformed_returns_none():
    """A non-JSON response yields None instead of raising."""
    with patch_gh(return_value="not-json{"):
        assert _get_thread_details("https://api.github.com/x") is None


# -- _unsubscribe_rest_thread -------------------------------------------------


def test_unsubscribe_rest_thread_success():
    """A successful unsubscribe deletes the subscription then marks it read."""
    with patch_gh() as mock_gh:
        assert _unsubscribe_rest_thread("42") is True
    assert mock_gh.call_args_list[0].args[0] == [
        "api",
        "--method",
        "DELETE",
        "/notifications/threads/42/subscription",
    ]
    assert mock_gh.call_args_list[1].args[0] == [
        "api",
        "--method",
        "PATCH",
        "/notifications/threads/42",
    ]


def test_unsubscribe_rest_thread_delete_failure():
    """A failed DELETE returns False and never attempts the PATCH."""
    with patch_gh(side_effect=RuntimeError("no")) as mock_gh:
        assert _unsubscribe_rest_thread("42") is False
    assert mock_gh.call_count == 1


def test_unsubscribe_rest_thread_patch_failure():
    """A failed PATCH (after a successful DELETE) returns False."""
    with patch_gh(
        side_effect=[None, RuntimeError("no")],
    ) as mock_gh:
        assert _unsubscribe_rest_thread("42") is False
    assert mock_gh.call_count == 2


# -- _graphql_unsubscribe -----------------------------------------------------


def test_graphql_unsubscribe_success():
    """A successful mutation passes the node id and query, returning True."""
    with patch_gh() as mock_gh:
        assert _graphql_unsubscribe("node-1") is True
    args = mock_gh.call_args.args[0]
    assert args[:2] == ["api", "graphql"]
    assert "id=node-1" in args
    assert any(a.startswith("query=") for a in args)


def test_graphql_unsubscribe_failure():
    """A failed mutation returns False."""
    with patch_gh(side_effect=RuntimeError("no")):
        assert _graphql_unsubscribe("node-1") is False


# -- _get_authenticated_username ----------------------------------------------


def test_get_authenticated_username_strips():
    """The login is trimmed of trailing whitespace."""
    with patch_gh(return_value="octocat\n") as mock_gh:
        assert _get_authenticated_username() == "octocat"
    assert mock_gh.call_args.args[0] == ["api", "/user", "--jq", ".login"]


def test_get_authenticated_username_propagates_error():
    """A gh failure propagates so the caller can skip phase 2."""
    with (
        patch_gh(side_effect=RuntimeError("boom")),
        pytest.raises(RuntimeError, match="boom"),
    ):
        _get_authenticated_username()


# -- _iter_closed_items -------------------------------------------------------


def test_iter_closed_items_delegates_to_paginator():
    """The search wraps the shared paginator with the batch as node budget."""
    with patch(f"{_MODULE}.iter_graphql_nodes") as mock_iter:
        mock_iter.return_value = iter([{"id": "n1"}])
        _iter_closed_items("involves:fruitbot is:closed", 42)
    mock_iter.assert_called_once()
    pos_args = mock_iter.call_args.args
    kw_args = mock_iter.call_args.kwargs
    assert pos_args[0] == THREADLESS_SEARCH_QUERY
    assert pos_args[1] == ("search",)
    assert pos_args[2] == {"searchQuery": "involves:fruitbot is:closed"}
    assert kw_args["page_size_var"] == "pageSize"
    assert kw_args["page_size"] == GRAPHQL_PAGE_SIZE
    assert kw_args["max_nodes"] == 42


# -- _validate_notifications_token --------------------------------------------


def test_validate_notifications_token_exact_scope(caplog):
    """A token scoped to exactly 'notifications' passes without a warning."""
    with (
        patch(
            f"{_MODULE}.validate_classic_pat_scope",
            return_value=["notifications"],
        ),
        caplog.at_level(logging.WARNING),
    ):
        _validate_notifications_token()
    assert "more scopes than needed" not in caplog.text


def test_validate_notifications_token_extra_scopes_warns(caplog):
    """Extra scopes on the token log a least-privilege warning."""
    with (
        patch(
            f"{_MODULE}.validate_classic_pat_scope",
            return_value=["notifications", "repo"],
        ),
        caplog.at_level(logging.WARNING),
    ):
        _validate_notifications_token()
    assert "more scopes than needed" in caplog.text


def test_validate_notifications_token_propagates():
    """A validation failure propagates to the CLI entry point."""
    with (
        patch(
            f"{_MODULE}.validate_classic_pat_scope",
            side_effect=RuntimeError("bad scope"),
        ),
        pytest.raises(RuntimeError, match="bad scope"),
    ):
        _validate_notifications_token()


# -- unsubscribe_threads: phase 1 (REST) --------------------------------------


def test_phase1_live_unsubscribes_closed_stale():
    """A closed, stale thread is unsubscribed via DELETE + PATCH in live mode."""
    url = "https://api.github.com/repos/fruits/apple/issues/1"
    run = _dispatch_gh(
        notifications=_thread_line("t1", url),
        details={url: _closed_detail()},
    )
    with (
        patch_gh(side_effect=run) as mock_gh,
        patch(f"{_MODULE}.iter_graphql_nodes", return_value=[]),
    ):
        result = unsubscribe_threads(months=3, batch_size=200, dry_run=False)
    p1 = result.phase1
    assert p1.threads_total == 1
    assert p1.threads_inspected == 1
    assert p1.threads_unsubscribed == 1
    assert p1.threads_failed == 0
    assert p1.rows[0].action == ReportAction.UNSUBSCRIBED
    assert len(_gh_calls_matching(mock_gh, ["api", "--method", "DELETE"])) == 1
    assert len(_gh_calls_matching(mock_gh, ["api", "--method", "PATCH"])) == 1


def test_phase1_dry_run_records_candidate_without_mutations():
    """Dry-run records a candidate row but issues no DELETE or PATCH."""
    url = "https://api.github.com/repos/fruits/apple/issues/1"
    run = _dispatch_gh(
        notifications=_thread_line("t1", url),
        details={url: _closed_detail()},
    )
    with (
        patch_gh(side_effect=run) as mock_gh,
        patch(f"{_MODULE}.iter_graphql_nodes", return_value=[]),
    ):
        result = unsubscribe_threads(months=3, batch_size=200, dry_run=True)
    assert result.phase1.threads_unsubscribed == 1
    assert result.phase1.rows[0].action == ReportAction.DRY_RUN
    assert _gh_calls_matching(mock_gh, ["api", "--method", "DELETE"]) == []
    assert _gh_calls_matching(mock_gh, ["api", "--method", "PATCH"]) == []


@pytest.mark.parametrize(
    ("detail", "counter"),
    [
        pytest.param(
            _closed_detail(state="open", updated_at=STALE),
            "threads_skipped_open",
            id="open",
        ),
        pytest.param(
            _closed_detail(state="closed", updated_at=RECENT),
            "threads_skipped_recent",
            id="recent",
        ),
        pytest.param(
            _closed_detail(state="unknown", updated_at=STALE),
            "threads_skipped_unknown",
            id="unknown-state",
        ),
        pytest.param(
            _closed_detail(state="closed", updated_at="not-a-date"),
            "threads_skipped_unknown",
            id="bad-timestamp",
        ),
    ],
)
def test_phase1_skip_scenarios(detail, counter):
    """Open, recent, unknown-state, and unparsable threads are each skipped."""
    url = "https://api.github.com/repos/fruits/apple/issues/1"
    run = _dispatch_gh(notifications=_thread_line("t1", url), details={url: detail})
    with (
        patch_gh(side_effect=run) as mock_gh,
        patch(f"{_MODULE}.iter_graphql_nodes", return_value=[]),
    ):
        result = unsubscribe_threads(months=3, batch_size=200, dry_run=False)
    assert getattr(result.phase1, counter) == 1
    assert result.phase1.threads_unsubscribed == 0
    assert _gh_calls_matching(mock_gh, ["api", "--method", "DELETE"]) == []


def test_phase1_inaccessible_subject_skipped():
    """An inaccessible subject (no details) counts as unknown, not a failure."""
    url = "https://api.github.com/repos/fruits/apple/issues/1"
    run = _dispatch_gh(notifications=_thread_line("t1", url), details={})
    with (
        patch_gh(side_effect=run),
        patch(f"{_MODULE}.iter_graphql_nodes", return_value=[]),
    ):
        result = unsubscribe_threads(months=3, batch_size=200, dry_run=False)
    assert result.phase1.threads_skipped_unknown == 1
    assert result.phase1.threads_unsubscribed == 0


def test_phase1_delete_failure_records_failed():
    """A failed DELETE records a FAILED row and skips the PATCH."""
    url = "https://api.github.com/repos/fruits/apple/issues/1"
    run = _dispatch_gh(
        notifications=_thread_line("t1", url),
        details={url: _closed_detail()},
        fail_delete={"t1"},
    )
    with (
        patch_gh(side_effect=run) as mock_gh,
        patch(f"{_MODULE}.iter_graphql_nodes", return_value=[]),
    ):
        result = unsubscribe_threads(months=3, batch_size=200, dry_run=False)
    p1 = result.phase1
    assert p1.threads_failed == 1
    assert p1.threads_unsubscribed == 0
    assert p1.rows[0].action == ReportAction.FAILED
    assert len(_gh_calls_matching(mock_gh, ["api", "--method", "DELETE"])) == 1
    assert _gh_calls_matching(mock_gh, ["api", "--method", "PATCH"]) == []


def test_phase1_batch_size_truncates_but_reports_total():
    """Only batch-size threads are inspected, yet the full total is reported."""
    base = "https://api.github.com/repos/fruits/apple/issues"
    lines = "\n".join(_thread_line(f"t{i}", f"{base}/{i}") for i in range(5))
    details = {f"{base}/{i}": _closed_detail(number=i) for i in range(5)}
    run = _dispatch_gh(notifications=lines, details=details)
    with (
        patch_gh(side_effect=run),
        patch(f"{_MODULE}.iter_graphql_nodes", return_value=[]),
    ):
        result = unsubscribe_threads(months=3, batch_size=2, dry_run=True)
    assert result.phase1.threads_total == 5
    assert result.phase1.threads_inspected == 2
    assert result.phase1.threads_unsubscribed == 2


def test_phase1_fetch_failure_skips_phase():
    """A fetch failure leaves phase 1 empty while phase 2 still runs."""
    run = _dispatch_gh(fail_notifications=True)
    with (
        patch_gh(side_effect=run),
        patch(f"{_MODULE}.iter_graphql_nodes", return_value=[]),
    ):
        result = unsubscribe_threads(months=3, batch_size=200, dry_run=True)
    assert result.phase1.threads_total == 0
    assert result.phase1.threads_inspected == 0
    assert result.phase2.skipped is False


# -- unsubscribe_threads: phase 2 (GraphQL) -----------------------------------


def test_phase2_filters_non_subscribed_items():
    """Only items with viewerSubscription SUBSCRIBED are acted on."""
    items = [
        _gql_item("n1", subscription="SUBSCRIBED"),
        _gql_item("n2", subscription="IGNORED"),
        _gql_item("n3", subscription="UNSUBSCRIBED"),
    ]
    run = _dispatch_gh(notifications="")
    with (
        patch_gh(side_effect=run),
        patch(f"{_MODULE}.iter_graphql_nodes", return_value=items),
    ):
        result = unsubscribe_threads(months=3, batch_size=200, dry_run=True)
    p2 = result.phase2
    assert p2.graphql_total == 3
    assert p2.graphql_not_subscribed == 2
    assert p2.graphql_unsubscribed == 1
    assert p2.skipped is False


def test_phase2_skips_items_active_since_cutoff():
    """Fresh or timestamp-less items are skipped despite the search filter.

    The search query's day-granular `updated:<` filter is served by GitHub's
    search index, which can lag; the client-side re-check mirrors phase 1.
    """
    items = [
        _gql_item("n1", updated_at=STALE),
        _gql_item("n2", updated_at=RECENT),
        _gql_item("n3", updated_at="not-a-timestamp"),
    ]
    run = _dispatch_gh(notifications="")
    with (
        patch_gh(side_effect=run),
        patch(f"{_MODULE}.iter_graphql_nodes", return_value=items),
    ):
        result = unsubscribe_threads(months=3, batch_size=200, dry_run=True)
    p2 = result.phase2
    assert p2.graphql_total == 3
    assert p2.graphql_skipped_recent == 2
    assert p2.graphql_unsubscribed == 1


def test_phase2_dry_run_no_mutation_calls():
    """Dry-run records a candidate row but issues no mutation."""
    items = [_gql_item("n1", subscription="SUBSCRIBED")]
    run = _dispatch_gh(notifications="")
    with (
        patch_gh(side_effect=run) as mock_gh,
        patch(f"{_MODULE}.iter_graphql_nodes", return_value=items),
    ):
        result = unsubscribe_threads(months=3, batch_size=200, dry_run=True)
    assert result.phase2.graphql_unsubscribed == 1
    assert result.phase2.rows[0].action == ReportAction.DRY_RUN
    assert _gh_calls_matching(mock_gh, ["api", "graphql"]) == []


def test_phase2_live_unsubscribes_subscribed():
    """Live mode issues the mutation for a subscribed item."""
    items = [_gql_item("n1", subscription="SUBSCRIBED")]
    run = _dispatch_gh(notifications="")
    with (
        patch_gh(side_effect=run) as mock_gh,
        patch(f"{_MODULE}.iter_graphql_nodes", return_value=items),
    ):
        result = unsubscribe_threads(months=3, batch_size=200, dry_run=False)
    assert result.phase2.graphql_unsubscribed == 1
    assert result.phase2.rows[0].action == ReportAction.UNSUBSCRIBED
    gql_calls = _gh_calls_matching(mock_gh, ["api", "graphql"])
    assert len(gql_calls) == 1
    assert "id=n1" in gql_calls[0]


def test_phase2_mutation_failure_records_failed():
    """A failed mutation records a FAILED row."""
    items = [_gql_item("n1", subscription="SUBSCRIBED")]
    run = _dispatch_gh(notifications="", fail_mutation={"n1"})
    with (
        patch_gh(side_effect=run),
        patch(f"{_MODULE}.iter_graphql_nodes", return_value=items),
    ):
        result = unsubscribe_threads(months=3, batch_size=200, dry_run=False)
    assert result.phase2.graphql_failed == 1
    assert result.phase2.graphql_unsubscribed == 0
    assert result.phase2.rows[0].action == ReportAction.FAILED


def test_phase2_username_failure_skips_search():
    """A username lookup failure skips phase 2 before any search."""
    run = _dispatch_gh(notifications="", fail_username=True)
    with (
        patch_gh(side_effect=run),
        patch(f"{_MODULE}.iter_graphql_nodes") as mock_iter,
    ):
        result = unsubscribe_threads(months=3, batch_size=200, dry_run=True)
    assert result.phase2.skipped is True
    assert "authenticated username" in result.phase2.skip_reason
    mock_iter.assert_not_called()


def test_phase2_search_failure_skips():
    """A GraphQL search failure marks phase 2 skipped with a reason."""
    run = _dispatch_gh(notifications="")
    with (
        patch_gh(side_effect=run),
        patch(
            f"{_MODULE}.iter_graphql_nodes",
            side_effect=RuntimeError("no graphql"),
        ),
    ):
        result = unsubscribe_threads(months=3, batch_size=200, dry_run=True)
    assert result.phase2.skipped is True
    assert "GraphQL search" in result.phase2.skip_reason


def test_phase2_search_query_built():
    """The search query embeds the username and the computed cutoff date."""
    with patch(f"{_MODULE}.datetime") as mock_dt:
        mock_dt.now.return_value = datetime(2026, 7, 16, tzinfo=timezone.utc)
        run = _dispatch_gh(notifications="", username="fruitbot")
        with (
            patch_gh(side_effect=run),
            patch(f"{_MODULE}.iter_graphql_nodes", return_value=[]),
        ):
            result = unsubscribe_threads(months=3, batch_size=200, dry_run=True)
    assert result.phase2.search_query == (
        "involves:fruitbot is:closed updated:<2026-04-16"
    )
