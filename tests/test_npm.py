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

"""Tests for the npm registry client.

The shared fetch-and-cache mechanics behind `_fetch_json` are covered once in
`tests/test_http.py`.
"""

from __future__ import annotations

from unittest.mock import patch

from repomatic.npm import get_release_dates


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
