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

"""Tests for the shared file digesting."""

from __future__ import annotations

import hashlib

from repomatic.hashing import compute_file_sha256


def test_compute_file_sha256(tmp_path):
    """Compute SHA-256 of a file with known content."""
    p = tmp_path / "test.bin"
    p.write_bytes(b"hello world")
    expected = hashlib.sha256(b"hello world").hexdigest()
    assert compute_file_sha256(p) == expected
