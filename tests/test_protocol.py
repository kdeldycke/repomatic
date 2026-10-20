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

"""Tests for `repomatic.release.protocol`: the release-commit protocol.

`tests/test_workflows.py` holds the workflow gates to these values. The tests
here cover the values on their own.
"""

from __future__ import annotations

import pytest

from repomatic.release.protocol import (
    RELEASE_COMMIT_PATTERN,
    RELEASE_COMMIT_PREFIX,
    SKIP_BINARY_BUILD_BRANCHES,
    VERSION_BUMP_BRANCHES,
)


@pytest.mark.parametrize(
    ("message", "version"),
    (
        ("[changelog] Release v1.2.3", "1.2.3"),
        ("[changelog] Release v10.0.12", "10.0.12"),
        # A squash merge retitles the commit after the pull request.
        ("Release `v1.2.3` (#42)", None),
        ("[changelog] Release v1.2", None),
        ("[changelog] Post-release bump v1.2.3 → v1.2.4", None),
    ),
)
def test_release_commit_pattern(message, version):
    match = RELEASE_COMMIT_PATTERN.fullmatch(message)
    assert (match["version"] if match else None) == version
    if match:
        assert message.startswith(RELEASE_COMMIT_PREFIX)


def test_skip_binary_build_branches_constant():
    """The skip list names housekeeping branches, never a code branch."""
    assert isinstance(SKIP_BINARY_BUILD_BRANCHES, frozenset)
    # Verify the list contains expected branches for non-code changes.
    assert "sync-mailmap" in SKIP_BINARY_BUILD_BRANCHES
    assert "format-markdown" in SKIP_BINARY_BUILD_BRANCHES
    assert "format-images" in SKIP_BINARY_BUILD_BRANCHES
    assert "sync-gitignore" in SKIP_BINARY_BUILD_BRANCHES
    # Verify branches that affect code are NOT in the list.
    assert "format-python" not in SKIP_BINARY_BUILD_BRANCHES
    assert "main" not in SKIP_BINARY_BUILD_BRANCHES


def test_skip_binary_build_disjoint_from_version_bumps():
    """Version-bump branches rewrite the version string baked into the
    Nuitka binary, so they must not appear in SKIP_BINARY_BUILD_BRANCHES.
    """
    assert SKIP_BINARY_BUILD_BRANCHES.isdisjoint(VERSION_BUMP_BRANCHES)
