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

"""Tests for the npm registry client."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

from repomatic.http import FetchError
from repomatic.npm import _fetch_json, get_release_dates


def _config(npm_ttl: int) -> SimpleNamespace:
    """Stand in for the resolved config, exposing just `cache.npm_ttl`."""
    return SimpleNamespace(cache=SimpleNamespace(npm_ttl=npm_ttl))


def test_get_release_dates_maps_versions_to_dates():
    """Each version maps to its publication date, truncated to `YYYY-MM-DD`."""
    payload = {
        "time": {
            "created": "2026-01-01T00:00:00.000Z",
            "modified": "2026-06-01T00:00:00.000Z",
            "1.0.0": "2026-01-02T03:04:05.000Z",
            "2.1.0": "2026-03-04T05:06:07.000Z",
        },
    }
    with patch("repomatic.npm._fetch_json", return_value=payload):
        dates = get_release_dates("papaya")

    # Housekeeping keys are dropped; timestamps are trimmed to the date.
    assert dates == {"1.0.0": "2026-01-02", "2.1.0": "2026-03-04"}


def test_get_release_dates_empty_when_fetch_fails():
    """A failed metadata fetch degrades to an empty mapping."""
    with patch("repomatic.npm._fetch_json", return_value=None):
        assert get_release_dates("papaya") == {}


def test_fetch_json_fetches_and_stores():
    """A cache miss fetches over HTTP and stores the raw response body."""
    payload = {"time": {"1.0.0": "2026-01-02T00:00:00.000Z"}}
    raw = json.dumps(payload).encode()
    with (
        patch("repomatic.npm.load_repomatic_config", return_value=_config(3600)),
        patch("repomatic.npm.get_cached_response", return_value=None),
        patch("repomatic.npm.get_json", return_value=(payload, raw)) as mock_get,
        patch("repomatic.npm.store_response") as mock_store,
    ):
        result = _fetch_json("papaya")

    assert result == payload
    mock_get.assert_called_once()
    mock_store.assert_called_once_with("npm", "papaya", raw)


def test_fetch_json_uses_cache_when_fresh():
    """A cache hit is parsed and returned without any HTTP request."""
    cached = json.dumps({"time": {"1.0.0": "2026-01-02T00:00:00.000Z"}})
    with (
        patch("repomatic.npm.load_repomatic_config", return_value=_config(3600)),
        patch("repomatic.npm.get_cached_response", return_value=cached),
        patch("repomatic.npm.get_json") as mock_get,
    ):
        result = _fetch_json("papaya")

    assert result == {"time": {"1.0.0": "2026-01-02T00:00:00.000Z"}}
    mock_get.assert_not_called()


def test_fetch_json_ignores_corrupt_cache():
    """A cache entry that no longer parses falls through to a fresh fetch."""
    payload: dict[str, dict[str, str]] = {"time": {}}
    raw = json.dumps(payload).encode()
    with (
        patch("repomatic.npm.load_repomatic_config", return_value=_config(3600)),
        patch("repomatic.npm.get_cached_response", return_value="<<not json>>"),
        patch("repomatic.npm.get_json", return_value=(payload, raw)) as mock_get,
        patch("repomatic.npm.store_response"),
    ):
        result = _fetch_json("papaya")

    assert result == payload
    mock_get.assert_called_once()


def test_fetch_json_returns_none_on_fetch_error():
    """An HTTP or network failure is swallowed into a `None` result."""
    with (
        patch("repomatic.npm.load_repomatic_config", return_value=_config(3600)),
        patch("repomatic.npm.get_cached_response", return_value=None),
        patch("repomatic.npm.get_json", side_effect=FetchError("HTTP 404")),
        patch("repomatic.npm.store_response") as mock_store,
    ):
        assert _fetch_json("papaya") is None

    # A failed fetch must never persist anything to the cache.
    mock_store.assert_not_called()


def test_fetch_json_skips_store_when_ttl_disabled():
    """With caching disabled (`ttl == 0`), a fetched body is not stored."""
    payload: dict[str, dict[str, str]] = {"time": {}}
    raw = json.dumps(payload).encode()
    with (
        patch("repomatic.npm.load_repomatic_config", return_value=_config(0)),
        patch("repomatic.npm.get_cached_response", return_value=None),
        patch("repomatic.npm.get_json", return_value=(payload, raw)),
        patch("repomatic.npm.store_response") as mock_store,
    ):
        assert _fetch_json("papaya") == payload

    mock_store.assert_not_called()
