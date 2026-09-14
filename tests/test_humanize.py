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

"""Tests for the human-readable renderings of machine quantities."""

from __future__ import annotations

import time
from datetime import date, datetime, timezone

import pytest

from repomatic.humanize import (
    SECONDS_PER_DAY,
    format_age,
    format_countdown,
    format_elapsed,
    format_file_size,
    utc_midnight,
)


@pytest.mark.parametrize(
    ("size_bytes", "expected"),
    [
        (0, "0 B"),
        (512, "512 B"),
        (1023, "1,023 B"),
        (1024, "1.0 KB"),
        (1536, "1.5 KB"),
        (10240, "10.0 KB"),
        (1048576, "1.0 MB"),
        (1572864, "1.5 MB"),
        (1073741824, "1.0 GB"),
    ],
)
def test_format_file_size(size_bytes: int, expected: str) -> None:
    """Human-readable file size formatting."""
    assert format_file_size(size_bytes) == expected


@pytest.mark.parametrize(
    ("days_ago", "expected"),
    [
        (0, "today"),
        (1, "1 day"),
        (2, "2 days"),
        (30, "30 days"),
    ],
)
def test_format_age(days_ago: int, expected: str) -> None:
    """An mtime renders as whole days, singular for one."""
    # Offset by a few seconds so a boundary case never rounds up mid-test.
    mtime = time.time() - days_ago * SECONDS_PER_DAY - 5
    assert format_age(mtime) == expected


def _utc(year, month, day, hour=0, minute=0):
    """Shorthand for an aware UTC instant."""
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


def test_utc_midnight_starts_the_day() -> None:
    """A date carries no time of day, so it enters a countdown at midnight."""
    assert utc_midnight(date(2026, 6, 21)) == _utc(2026, 6, 21)


@pytest.mark.parametrize(
    ("deadline", "reference", "expected"),
    [
        (_utc(2026, 6, 25), _utc(2026, 6, 21), "2026-06-25 (in 4 days)"),
        # Hours left, not a whole day: the countdown says so instead of
        # collapsing to `just now`, which reads as a deadline already passed.
        (_utc(2026, 6, 21, 14), _utc(2026, 6, 21, 9), "2026-06-21 (in 5 hours)"),
        (_utc(2026, 6, 21, 9, 30), _utc(2026, 6, 21, 9), "2026-06-21 (in 30 minutes)"),
        # Already passed: the countdown would read as noise, so it is dropped.
        (_utc(2026, 6, 20), _utc(2026, 6, 21), "2026-06-20"),
        (_utc(2026, 6, 21, 9), _utc(2026, 6, 21, 9), "2026-06-21"),
    ],
)
def test_format_countdown(deadline, reference, expected) -> None:
    """A countdown follows the precision of the instants it is given."""
    assert format_countdown(deadline, reference) == expected


@pytest.mark.parametrize(
    ("instant", "reference", "expected"),
    [
        (_utc(2026, 6, 21), _utc(2026, 6, 25), "2026-06-21 (4 days ago)"),
        # Hours, not a whole day: the mirror of the countdown case above.
        (_utc(2026, 6, 21, 9), _utc(2026, 6, 21, 14), "2026-06-21 (5 hours ago)"),
        (_utc(2026, 6, 21, 9), _utc(2026, 6, 21, 9), "2026-06-21 (just now)"),
        # Still ahead: `in 4 days` would contradict the column this sits in.
        (_utc(2026, 6, 25), _utc(2026, 6, 21), "2026-06-25"),
    ],
)
def test_format_elapsed(instant, reference, expected) -> None:
    """The past-facing mirror drops its phrase in the other direction."""
    assert format_elapsed(instant, reference) == expected
