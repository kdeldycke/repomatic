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

"""Tests for `repomatic.upstream`: the identity of the upstream toolkit."""

from __future__ import annotations

from pathlib import Path

import pytest

from repomatic.upstream import (
    DEFAULT_REPO,
    UPSTREAM_PACKAGE,
    UPSTREAM_REPO_SLUGS,
    UPSTREAM_SOURCE_GLOB,
    UPSTREAM_SOURCE_PREFIX,
    is_awesome_repo,
    is_source_repo,
)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("awesome-papaya", True),
        ("papaya-awesome", False),
        ("awesome", False),
        ("Awesome-papaya", False),
    ],
)
def test_is_awesome_repo(name: str, expected: bool) -> None:
    """Only the `awesome-` prefix marks a curated list."""
    assert is_awesome_repo(name) is expected


def test_is_source_repo_recognizes_this_checkout() -> None:
    """The checkout holding this test suite is the upstream source."""
    assert is_source_repo(Path(__file__).parent.parent)


def test_is_source_repo_needs_the_data_folder(tmp_path: Path) -> None:
    """A package folder without `data/` is another project of the same name."""
    package = tmp_path / UPSTREAM_PACKAGE
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="UTF-8")
    assert not is_source_repo(tmp_path)
    (package / "data").mkdir()
    assert is_source_repo(tmp_path)


def test_upstream_names_derive_from_the_repository() -> None:
    """The package and its source globs follow the repository name."""
    assert DEFAULT_REPO.endswith(f"/{UPSTREAM_PACKAGE}")
    assert UPSTREAM_REPO_SLUGS[0] == DEFAULT_REPO
    assert UPSTREAM_SOURCE_GLOB == f"{UPSTREAM_PACKAGE}/**"
    assert UPSTREAM_SOURCE_PREFIX == f"{UPSTREAM_PACKAGE}/"
