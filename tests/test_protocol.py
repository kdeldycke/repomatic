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
    CHANGELOG_COMMIT_PREFIX,
    POST_RELEASE_COMMIT_PREFIX,
    RELEASE_COMMIT_PATTERN,
    RELEASE_COMMIT_PREFIX,
    RELEASE_PUSH_PREFIXES,
    SKIP_BINARY_BUILD_BRANCHES,
    SQUASHED_RELEASE_PREFIX,
    VERSION_BUMP_BRANCHES,
    VERSION_BUMP_COMMIT_PREFIXES,
    bump_branch,
    post_release_commit_message,
    release_commit_message,
    squashed_release_pr,
)
from repomatic.templating import PrTemplate
from repomatic.versions import BUMP_PARTS


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


@pytest.mark.parametrize(
    ("head_commit", "release_push"),
    (
        ("[changelog] Post-release bump v1.2.3 → v1.2.4", True),
        ("[changelog] Release v1.2.3", True),
        ("[changelog] Bump minor version to `v1.3.0`", False),
        ("Release notes for the melon parser", False),
    ),
)
def test_release_push_prefixes_read_the_head_commit(head_commit, release_push):
    """A rebase-merged release push ends on its post-release bump.

    The concurrency groups and the cancel sweep read the head commit of a
    push, so the post-release bump has to count as a release push too.
    """
    assert head_commit.startswith(RELEASE_PUSH_PREFIXES) is release_push


def test_release_push_prefixes_carry_the_machinery_prefix():
    """Both prefixes are version-machinery commits the workflow gates know."""
    assert all(p.startswith(CHANGELOG_COMMIT_PREFIX) for p in RELEASE_PUSH_PREFIXES)
    assert POST_RELEASE_COMMIT_PREFIX in VERSION_BUMP_COMMIT_PREFIXES


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


@pytest.mark.parametrize("version", ("1.2.3", "10.0.12"))
def test_release_commit_message_is_the_one_the_lane_tags(version):
    """The freeze commit message is what the release workflow finds and tags."""
    match = RELEASE_COMMIT_PATTERN.fullmatch(release_commit_message(version))
    assert match is not None
    assert match["version"] == version


def test_post_release_commit_message_names_both_versions():
    """The unfreeze commit names the released and the bumped version."""
    message = post_release_commit_message("1.2.3", "1.2.4.dev0")
    assert message == "[changelog] Post-release bump v1.2.3 → v1.2.4.dev0"
    assert message.startswith(POST_RELEASE_COMMIT_PREFIX)
    assert POST_RELEASE_COMMIT_PREFIX in VERSION_BUMP_COMMIT_PREFIXES
    assert not RELEASE_COMMIT_PATTERN.fullmatch(message)


@pytest.mark.parametrize(
    ("message", "reference"),
    (
        ("Release `v1.2.3` (#42)", "#42"),
        ("Release `v1.2.3`", ""),
        # The squash body lists the squashed commits, the last reference wins.
        ("Release `v1.2.3` (#42)\n\n* Mention #7\n* Fix #8", "#8"),
        ("[changelog] Release v1.2.3", None),
        ("Release notes for the mango crate (#12)", None),
        ("", None),
    ),
)
def test_squashed_release_pr(message, reference):
    assert squashed_release_pr(message) == reference


def test_squashed_release_prefix_matches_the_release_pr_title():
    """A squash merge names its commit after the release pull request title.

    The detection keys on the start of that title, so a template rewording it
    would make every squash merge go unreported.
    """
    title = PrTemplate.load("prepare-release").title(version="1.2.3")
    assert title.startswith(SQUASHED_RELEASE_PREFIX)
    assert squashed_release_pr(f"{title} (#42)") == "#42"


def test_bump_branches_are_version_bump_branches():
    """Every bump part opens a branch the heavy workflows skip."""
    for part in BUMP_PARTS:
        assert bump_branch(part) in VERSION_BUMP_BRANCHES
    assert bump_branch("minor") == "minor-version-increment"
