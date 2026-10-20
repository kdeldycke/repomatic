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

"""Tests for :mod:`repomatic.github.event` payload readers."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from repomatic.github.event import (
    extract_workflow_filename,
    get_default_author,
    get_default_branch,
    get_default_number,
    get_github_event,
    is_pull_request,
)

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
        # An empty `pull_request` object is not a pull request: every reader
        # falls through to the issue below it alike.
        ({"pull_request": {}, **_ISSUE_EVENT}, False, "quince", 12),
        # A subject with neither a user nor a number degrades to None rather
        # than raising.
        ({"issue": {"title": "no user, no number"}}, False, None, None),
    ),
)
def test_event_subject_readers_agree(event, is_pr, author, number):
    """All three payload readers resolve the same subject node."""
    # Patching the module global also sidesteps the memo on the real one.
    with patch("repomatic.github.event.get_github_event", return_value=event):
        assert is_pull_request() is is_pr
        assert get_default_author() == author
        assert get_default_number() == number


@pytest.mark.parametrize(
    ("event", "expected"),
    (
        ({"repository": {"default_branch": "trunk"}}, "trunk"),
        ({"repository": {}}, None),
        ({}, None),
    ),
)
def test_get_default_branch(event, expected):
    """The repository node names the branch a pull request opens against."""
    with patch("repomatic.github.event.get_github_event", return_value=event):
        assert get_default_branch() == expected


def test_get_github_event_reads_the_payload(tmp_path, monkeypatch):
    """The payload file the runner names is parsed once and returned."""
    payload = tmp_path / "event.json"
    payload.write_text(json.dumps(_PR_EVENT), encoding="UTF-8")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(payload))
    assert get_github_event() == _PR_EVENT


@pytest.mark.parametrize(
    "content",
    (
        pytest.param(None, id="missing-file"),
        pytest.param("{ not json", id="malformed"),
        pytest.param("[1, 2]", id="not-an-object"),
    ),
)
def test_get_github_event_degrades_on_an_unreadable_payload(
    tmp_path, monkeypatch, content
):
    """A payload that cannot be read means no defaults, never a traceback."""
    payload = tmp_path / "event.json"
    if content is not None:
        payload.write_text(content, encoding="UTF-8")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(payload))
    assert get_github_event() == {}


def test_get_github_event_without_a_path(monkeypatch):
    """Outside a workflow run there is no payload at all."""
    monkeypatch.delenv("GITHUB_EVENT_PATH", raising=False)
    assert get_github_event() == {}


@pytest.mark.parametrize(
    ("workflow_ref", "expected"),
    [
        (
            "owner/repo/.github/workflows/autofix.yaml@refs/heads/main",
            "autofix.yaml",
        ),
        (
            "owner/repo/.github/workflows/release.yaml@refs/tags/v1.0.0",
            "release.yaml",
        ),
        ("", ""),
        ("just-a-filename.yaml", "just-a-filename.yaml"),
    ],
)
def test_extract_workflow_filename(workflow_ref, expected):
    """Extract workflow filename from various reference formats."""
    assert extract_workflow_filename(workflow_ref) == expected
