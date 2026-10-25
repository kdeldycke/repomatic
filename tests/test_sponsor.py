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

"""Tests for :mod:`repomatic.github.sponsor`."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from repomatic.cli.main import repomatic
from repomatic.github.gh import GhCommandError
from repomatic.github.sponsor import SPONSORS_QUERY, is_sponsor, iter_sponsors


def _page(*logins: str, cursor: str = "") -> str:
    """One page of the sponsors connection, as `gh api graphql` prints it."""
    return json.dumps({
        "data": {
            "repositoryOwner": {
                "sponsorshipsAsMaintainer": {
                    "pageInfo": {"hasNextPage": bool(cursor), "endCursor": cursor},
                    "nodes": [{"sponsorEntity": {"login": login}} for login in logins],
                }
            }
        }
    })


def test_iter_sponsors_reads_every_page_with_one_query():
    """User and organization owners share one query, walked page by page."""
    pages = [_page("papaya", cursor="c1"), _page("quince")]
    with patch("repomatic.github.gh.run_gh_command", side_effect=pages) as mock_gh:
        assert list(iter_sponsors("orchard")) == ["papaya", "quince"]
    assert mock_gh.call_count == 2
    first = mock_gh.call_args_list[0].args[0]
    assert f"query={SPONSORS_QUERY}" in first
    assert "owner=orchard" in first


def test_is_sponsor_stops_at_the_first_match():
    """A sponsor found on the first page spares the read of the next ones."""
    pages = [_page("papaya", cursor="c1"), _page("quince")]
    with patch("repomatic.github.gh.run_gh_command", side_effect=pages) as mock_gh:
        assert is_sponsor("orchard", "papaya") is True
    assert mock_gh.call_count == 1


def test_is_sponsor_reads_a_failed_query_as_no():
    """An API outage never fails the job: the author reads as no sponsor."""
    with patch(
        "repomatic.github.gh.run_gh_command",
        side_effect=GhCommandError("GraphQL: Could not resolve to a User"),
    ):
        assert is_sponsor("orchard", "papaya") is False


def test_is_sponsor_asks_again_on_each_call():
    """Nothing is cached, so a later call never replays an outage."""
    with patch(
        "repomatic.github.gh.run_gh_command",
        side_effect=[GhCommandError("HTTP 502"), _page("papaya")],
    ):
        assert is_sponsor("orchard", "papaya") is False
        assert is_sponsor("orchard", "papaya") is True


def test_sponsor_label_labels_a_sponsor_issue(tmp_path, monkeypatch):
    """The command labels the event's thread when its author sponsors the owner."""
    event = tmp_path / "event.json"
    event.write_text(
        json.dumps({"issue": {"number": 12, "user": {"login": "papaya"}}}),
        encoding="UTF-8",
    )
    calls: list[list[str]] = []

    def fake_gh(args):
        calls.append(list(args))
        if args[:2] == ["api", "graphql"]:
            return _page("papaya")
        return ""

    with (
        patch("repomatic.github.gh.run_gh_command", side_effect=fake_gh),
        patch("repomatic.github.threads.run_gh_command", side_effect=fake_gh),
    ):
        result = CliRunner().invoke(
            repomatic,
            ["sponsor-label", "--repo", "kevin/orchard", "--owner", "kevin"],
            env={"GH_TOKEN": "x", "GITHUB_EVENT_PATH": str(event)},
        )

    assert result.exit_code == 0, result.output
    assert "Added '💖 sponsor' label to issue #12" in result.output
    edit = calls[-1]
    assert edit[:3] == ["issue", "edit", "12"]
    assert edit[edit.index("--repo") + 1] == "kevin/orchard"


@pytest.mark.parametrize(
    ("issue", "sponsor", "edit_error", "exit_code", "message"),
    (
        pytest.param(
            {"number": 12},
            "papaya",
            None,
            2,
            "Missing required parameters: --author.",
            id="no-author",
        ),
        pytest.param(
            {"user": {"login": "papaya"}},
            "papaya",
            None,
            2,
            "Missing --number, and it could not be auto-detected",
            id="no-number",
        ),
        pytest.param(
            {"number": 12, "user": {"login": "papaya"}},
            "quince",
            None,
            0,
            "Author 'papaya' is not a sponsor of 'kevin'",
            id="not-a-sponsor",
        ),
        pytest.param(
            {"number": 12, "user": {"login": "papaya"}},
            "papaya",
            GhCommandError("HTTP 403: Resource not accessible"),
            1,
            "Failed to add sponsor label",
            id="refused-edit",
        ),
    ),
)
def test_sponsor_label_stops_short_of_labelling(
    tmp_path, issue, sponsor, edit_error, exit_code, message
):
    """A missing author or number, a non-sponsor, or a refused edit adds no label."""
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"issue": issue}), encoding="UTF-8")

    def fake_gh(args):
        if args[:2] == ["api", "graphql"]:
            return _page(sponsor)
        if edit_error:
            raise edit_error
        return ""

    with (
        patch("repomatic.github.gh.run_gh_command", side_effect=fake_gh),
        patch("repomatic.github.threads.run_gh_command", side_effect=fake_gh),
    ):
        result = CliRunner().invoke(
            repomatic,
            ["sponsor-label", "--repo", "kevin/orchard", "--owner", "kevin"],
            env={"GH_TOKEN": "x", "GITHUB_EVENT_PATH": str(event)},
        )

    assert result.exit_code == exit_code, result.output
    assert message in result.output
    assert "Added '💖 sponsor' label" not in result.output
