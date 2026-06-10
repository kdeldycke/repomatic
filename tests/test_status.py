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

"""Tests for :mod:`repomatic.github.status`."""

from __future__ import annotations

import io
import json
from unittest.mock import patch
from urllib.error import URLError

import pytest

from repomatic.github import status as status_module
from repomatic.github.status import (
    GitHubStatus,
    get_github_status,
    status_annotation,
)


@pytest.fixture(autouse=True)
def _clear_status_cache():
    """Drop the process-wide memoization between tests."""
    get_github_status.cache_clear()
    yield
    get_github_status.cache_clear()


def _payload(indicator: str, description: str) -> io.BytesIO:
    body = json.dumps({"status": {"indicator": indicator, "description": description}})
    return io.BytesIO(body.encode())


def test_healthy_status_parsed():
    """A healthy indicator returns a non-incident GitHubStatus."""
    with patch.object(status_module, "urlopen") as mock_urlopen:
        mock_urlopen.return_value.__enter__.return_value = _payload(
            "none", "All Systems Operational"
        )
        result = get_github_status()
        assert result == GitHubStatus("none", "All Systems Operational")
        assert result.is_incident is False
        assert result.annotation() == ""


@pytest.mark.parametrize(
    "indicator",
    ["minor", "major", "critical", "maintenance"],
)
def test_incident_status_parsed(indicator):
    """Any non-`none` indicator surfaces as an incident."""
    with patch.object(status_module, "urlopen") as mock_urlopen:
        mock_urlopen.return_value.__enter__.return_value = _payload(
            indicator, "Partial Outage"
        )
        result = get_github_status()
        assert result is not None
        assert result.is_incident is True
        assert indicator in result.annotation()
        assert "Partial Outage" in result.annotation()
        assert "githubstatus.com" in result.annotation()


def test_network_error_returns_none():
    """A network error collapses to None and does not raise."""
    with patch.object(status_module, "urlopen", side_effect=URLError("dns fail")):
        assert get_github_status() is None


def test_timeout_error_returns_none():
    """A timeout collapses to None and does not raise."""
    with patch.object(status_module, "urlopen", side_effect=TimeoutError()):
        assert get_github_status() is None


def test_malformed_json_returns_none():
    """Garbage in the response body returns None instead of raising."""
    with patch.object(status_module, "urlopen") as mock_urlopen:
        mock_urlopen.return_value.__enter__.return_value = io.BytesIO(b"not json")
        assert get_github_status() is None


def test_missing_status_field_returns_none():
    """A payload without the expected status object returns None."""
    with patch.object(status_module, "urlopen") as mock_urlopen:
        mock_urlopen.return_value.__enter__.return_value = io.BytesIO(b"{}")
        assert get_github_status() is None


def test_partial_status_field_returns_none():
    """A status object missing indicator or description returns None."""
    with patch.object(status_module, "urlopen") as mock_urlopen:
        mock_urlopen.return_value.__enter__.return_value = io.BytesIO(
            b'{"status": {"indicator": "minor"}}'
        )
        assert get_github_status() is None


def test_get_github_status_is_memoized():
    """Repeat calls within one process probe the network once."""
    with patch.object(status_module, "urlopen") as mock_urlopen:
        mock_urlopen.return_value.__enter__.return_value = _payload(
            "none", "All Systems Operational"
        )
        get_github_status()
        get_github_status()
        get_github_status()
        assert mock_urlopen.call_count == 1


def test_status_annotation_empty_when_unreachable():
    """The annotation helper returns an empty string when the probe fails."""
    with patch.object(status_module, "urlopen", side_effect=URLError("offline")):
        assert status_annotation() == ""


def test_status_annotation_empty_when_healthy():
    """The annotation helper returns empty when GitHub reports healthy."""
    with patch.object(status_module, "urlopen") as mock_urlopen:
        mock_urlopen.return_value.__enter__.return_value = _payload(
            "none", "All Systems Operational"
        )
        assert status_annotation() == ""


def test_status_annotation_populated_when_incident():
    """The annotation helper renders the incident summary when not healthy."""
    with patch.object(status_module, "urlopen") as mock_urlopen:
        mock_urlopen.return_value.__enter__.return_value = _payload(
            "major", "Partial System Outage"
        )
        annotation = status_annotation()
        assert "major" in annotation
        assert "Partial System Outage" in annotation
        assert "githubstatus.com" in annotation
