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

from repomatic.github.issue import (
    close_open_prs_on_branch,
    close_pr,
    list_open_prs_by_branch,
)


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
