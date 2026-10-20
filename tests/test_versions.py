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

"""Tests for the shared PEP 440 version primitives."""

from __future__ import annotations

import pytest
from packaging.version import Version

from repomatic.versions import (
    is_newer,
    parse_tag_version,
    safe_version,
    strip_dev_suffix,
)


@pytest.mark.parametrize(
    ("new", "old", "expected"),
    (
        ("1.2.0", "1.1.0", True),
        ("1.1.0", "1.1.0", False),
        ("1.0.0", "1.1.0", False),
        ("garbage", "1.0.0", False),
    ),
)
def test_is_newer(new, old, expected):
    assert is_newer(new, old) is expected


@pytest.mark.parametrize(
    ("value", "expected"),
    (
        ("1.2.3", "1.2.3"),
        ("1.0", "1.0"),
        ("2.1.0.dev0", "2.1.0.dev0"),
        ("", None),
        ("not-a-version", None),
    ),
)
def test_safe_version(value, expected):
    parsed = safe_version(value)
    assert (str(parsed) if parsed else None) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    (
        ("5.10.0.dev0", "5.10.0"),
        ("5.10.0.dev12", "5.10.0"),
        ("5.10.0", "5.10.0"),
        ("1.0", "1.0"),
    ),
)
def test_strip_dev_suffix(value, expected):
    assert strip_dev_suffix(value) == expected


@pytest.mark.parametrize(
    ("tag", "expected"),
    (
        pytest.param("v1.2.3", Version("1.2.3"), id="tag"),
        pytest.param("1.2.3", Version("1.2.3"), id="bare"),
        pytest.param("v2.0.0.dev0", Version("2.0.0.dev0"), id="dev"),
        pytest.param("v1.2.3_hotfix", None, id="foreign-scheme"),
        pytest.param("nightly", None, id="not-a-version"),
        pytest.param("", None, id="empty"),
    ),
)
def test_parse_tag_version(tag, expected):
    """A tag drops its `v` namespace, and a foreign scheme reads as `None`."""
    assert parse_tag_version(tag) == expected
