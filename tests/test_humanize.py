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

"""Tests for the human-readable renderings of file-system quantities."""

from __future__ import annotations

import time

import pytest

from repomatic.humanize import SECONDS_PER_DAY, format_age, format_file_size


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
