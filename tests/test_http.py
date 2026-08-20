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

"""Tests for the shared HTTP fetch and cache mechanics."""

from __future__ import annotations

import json
from http.client import IncompleteRead
from unittest.mock import patch

import pytest

from repomatic.http import FetchError, get_cached_json, get_json, get_text
from tests.conftest import FakeResponse

PAYLOAD = {"time": {"1.0.0": "2026-01-02T00:00:00.000Z"}}
RAW = json.dumps(PAYLOAD).encode()


def _fetch(ttl: int = 3600):
    """Call `get_cached_json` with the fixed npm/papaya coordinates."""
    return get_cached_json(
        "npm", "papaya", "https://example.test/papaya", ttl=ttl, log_label="papaya"
    )


def test_get_json_retries_incomplete_read():
    """A truncated body gets one retry before giving up."""
    with patch(
        "repomatic.http.urlopen",
        side_effect=[IncompleteRead(b""), FakeResponse(RAW)],
    ):
        assert get_json("https://example.test/mango") == (PAYLOAD, RAW)


def test_get_json_persistent_incomplete_read():
    """A body truncated on the retry too raises FetchError."""
    with (
        patch(
            "repomatic.http.urlopen",
            side_effect=[IncompleteRead(b""), IncompleteRead(b"")],
        ),
        pytest.raises(FetchError),
    ):
        get_json("https://example.test/mango")


def test_get_text_decodes_body():
    """A text fetch returns the decoded body rather than a parsed value."""
    with patch("repomatic.http.urlopen", return_value=FakeResponse(b"pomelo\n")):
        assert get_text("https://example.test/fruit") == "pomelo\n"


def test_get_text_rejects_undecodable_body():
    """A body that is not UTF-8 is a failed fetch, not mojibake."""
    with (
        patch("repomatic.http.urlopen", return_value=FakeResponse(b"\xff\xfe")),
        pytest.raises(FetchError),
    ):
        get_text("https://example.test/fruit")


def test_get_text_retries_incomplete_read():
    """A truncated text body earns the same single retry as a JSON one."""
    with patch(
        "repomatic.http.urlopen",
        side_effect=[IncompleteRead(b""), FakeResponse(b"pomelo")],
    ):
        assert get_text("https://example.test/fruit") == "pomelo"


def test_get_cached_json_fetches_and_stores():
    """A cache miss fetches over HTTP and stores the raw response body."""
    with (
        patch("repomatic.http.get_cached_response", return_value=None),
        patch("repomatic.http.get_json", return_value=(PAYLOAD, RAW)) as mock_get,
        patch("repomatic.http.store_response") as mock_store,
    ):
        result = _fetch()

    assert result == PAYLOAD
    mock_get.assert_called_once()
    mock_store.assert_called_once_with("npm", "papaya", RAW)


def test_get_cached_json_uses_cache_when_fresh():
    """A cache hit is parsed and returned without any HTTP request."""
    with (
        patch("repomatic.http.get_cached_response", return_value=RAW.decode()),
        patch("repomatic.http.get_json") as mock_get,
    ):
        result = _fetch()

    assert result == PAYLOAD
    mock_get.assert_not_called()


def test_get_cached_json_ignores_corrupt_cache():
    """A cache entry that no longer parses falls through to a fresh fetch."""
    with (
        patch("repomatic.http.get_cached_response", return_value="<<not json>>"),
        patch("repomatic.http.get_json", return_value=(PAYLOAD, RAW)) as mock_get,
        patch("repomatic.http.store_response"),
    ):
        result = _fetch()

    assert result == PAYLOAD
    mock_get.assert_called_once()


def test_get_cached_json_returns_none_on_fetch_error():
    """An HTTP or network failure is swallowed into a `None` result."""
    with (
        patch("repomatic.http.get_cached_response", return_value=None),
        patch("repomatic.http.get_json", side_effect=FetchError("HTTP 404")),
        patch("repomatic.http.store_response") as mock_store,
    ):
        assert _fetch() is None

    # A failed fetch must never persist anything to the cache.
    mock_store.assert_not_called()


def test_get_cached_json_skips_store_when_ttl_disabled():
    """With caching disabled (`ttl == 0`), a fetched body is not stored."""
    with (
        patch("repomatic.http.get_cached_response", return_value=None),
        patch("repomatic.http.get_json", return_value=(PAYLOAD, RAW)),
        patch("repomatic.http.store_response") as mock_store,
    ):
        assert _fetch(ttl=0) == PAYLOAD

    mock_store.assert_not_called()
