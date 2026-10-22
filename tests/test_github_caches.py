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

"""Tests of the GitHub Actions cache listing and pruning."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from click_extra.testing import CliRunner

from repomatic.cli.main import repomatic
from repomatic.github import caches as caches_module
from repomatic.github.caches import (
    ActionsCache,
    PruneReport,
    list_caches,
    prune_caches,
)
from repomatic.github.gh import GhCommandError

REPO = "orchard/papaya"


def _item(cache_id: int, key: str, created_at: str, size: int = 1024) -> dict:
    """One cache entry as the REST API lists it."""
    return {
        "id": cache_id,
        "key": key,
        "ref": "refs/heads/main",
        "size_in_bytes": size,
        "created_at": created_at,
        "last_accessed_at": created_at,
    }


LISTING = [
    _item(1, "nuitka-linux-x64-aaa", "2026-10-01T10:00:00Z"),
    _item(3, "nuitka-linux-x64-ccc", "2026-10-03T10:00:00Z", size=2048),
    _item(2, "nuitka-linux-x64-bbb", "2026-10-02T10:00:00Z"),
    # A key the prefix does not cover, as a lax server-side match could return.
    _item(9, "nuitka-linux-arm64-zzz", "2026-10-04T10:00:00Z"),
]


@patch("repomatic.github.caches.gh_api_pages", return_value=LISTING)
def test_list_caches_filters_on_the_prefix_and_sorts_newest_first(mock_pages):
    caches = list_caches(REPO, "nuitka-linux-x64-")

    assert [cache.id for cache in caches] == [3, 2, 1]
    endpoint = mock_pages.call_args[0][0]
    assert endpoint.startswith(f"repos/{REPO}/actions/caches?key=nuitka-linux-x64-")
    assert mock_pages.call_args[1] == {"key": "actions_caches"}


@patch("repomatic.github.caches.run_gh_command")
@patch("repomatic.github.caches.gh_api_pages", return_value=LISTING)
def test_prune_keeps_the_newest_and_deletes_the_rest(mock_pages, mock_run):
    report = prune_caches(REPO, "nuitka-linux-x64-", keep=1)

    assert [cache.id for cache in report.kept] == [3]
    assert [cache.id for cache in report.deleted] == [2, 1]
    assert report.refused == ()
    assert report.freed == 2048
    deleted_endpoints = [call[0][0][-1] for call in mock_run.call_args_list]
    assert deleted_endpoints == [
        f"repos/{REPO}/actions/caches/2",
        f"repos/{REPO}/actions/caches/1",
    ]
    for call in mock_run.call_args_list:
        assert call[0][0][:3] == ["api", "--method", "DELETE"]


@patch("repomatic.github.caches.run_gh_command")
@patch("repomatic.github.caches.gh_api_pages", return_value=LISTING)
def test_prune_dry_run_deletes_nothing(mock_pages, mock_run):
    report = prune_caches(REPO, "nuitka-linux-x64-", keep=1, dry_run=True)

    assert [cache.id for cache in report.deleted] == [2, 1]
    mock_run.assert_not_called()
    assert [row[-1] for row in report.rows(dry_run=True)] == [
        "kept",
        "would delete",
        "would delete",
    ]


@patch("repomatic.github.caches.gh_api_pages", return_value=LISTING)
def test_prune_reports_a_refused_deletion_without_raising(mock_pages, caplog):
    """A token without `actions: write` leaves the entry and the job alive."""

    def refuse(args):
        if args[-1].endswith("/2"):
            raise GhCommandError("HTTP 403: Resource not accessible by integration")
        return ""

    with patch.object(caches_module, "run_gh_command", side_effect=refuse):
        report = prune_caches(REPO, "nuitka-linux-x64-", keep=1)

    assert [cache.id for cache in report.refused] == [2]
    assert [cache.id for cache in report.deleted] == [1]
    assert "Could not delete cache 'nuitka-linux-x64-bbb'" in caplog.text


@pytest.mark.parametrize("keep", [0, 2, 5])
@patch("repomatic.github.caches.run_gh_command")
@patch("repomatic.github.caches.gh_api_pages", return_value=LISTING)
def test_prune_keep_bounds(mock_pages, mock_run, keep):
    report = prune_caches(REPO, "nuitka-linux-x64-", keep=keep)

    assert len(report.kept) == min(keep, 3)
    assert len(report.deleted) == 3 - len(report.kept)


def test_report_rows_name_every_entry_once():
    cache = ActionsCache(id=1, key="k", ref="refs/heads/main", size=10, created_at="t")
    report = PruneReport(prefix="k", kept=(cache,), deleted=(), refused=(cache,))

    rows = report.rows()
    assert [row[0] for row in rows] == ["k", "k"]
    assert [row[-1] for row in rows] == ["kept", "refused"]


@patch("repomatic.github.caches.run_gh_command")
@patch("repomatic.github.caches.gh_api_pages", return_value=LISTING)
def test_cli_prunes_each_prefix(mock_pages, mock_run):
    result = CliRunner().invoke(
        repomatic,
        [
            "prune-caches",
            "--repo",
            REPO,
            "--prefix",
            "nuitka-linux-x64-",
            "--keep",
            "2",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "nuitka-linux-x64-aaa" in result.output
    assert "deleted" in result.output
    assert mock_run.call_count == 1


@patch("repomatic.github.caches.run_gh_command")
@patch(
    "repomatic.github.caches.gh_api_pages",
    side_effect=GhCommandError("HTTP 403: Resource not accessible by integration"),
)
def test_cli_warns_when_the_listing_is_refused(mock_pages, mock_run):
    """An unreadable listing is a warning, not a failed job."""
    result = CliRunner().invoke(
        repomatic,
        ["prune-caches", "--repo", REPO, "--prefix", "nuitka-linux-x64-"],
    )

    assert result.exit_code == 0, result.output
    assert "Could not list the caches" in result.output
    mock_run.assert_not_called()
