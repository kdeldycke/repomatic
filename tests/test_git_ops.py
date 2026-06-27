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

"""Tests for Git operations module."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest
from packaging.version import Version

from repomatic.git_ops import (
    create_and_push_tag,
    create_tag,
    get_latest_tag_version,
    get_release_version_from_commits,
    push_tag,
    tag_exists,
)
from repomatic.metadata import Metadata, is_version_bump_allowed


def test_tag_exists_true():
    """Return True when tag exists."""
    with patch("repomatic.git_ops.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        assert tag_exists("v1.0.0") is True
        mock_run.assert_called_once_with(
            ["git", "show-ref", "--tags", "v1.0.0", "--quiet"],
            capture_output=True,
            check=False,
        )


def test_tag_exists_false():
    """Return False when tag does not exist."""
    with patch("repomatic.git_ops.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=1)
        assert tag_exists("v1.0.0") is False


def test_create_tag_head():
    """Create tag at HEAD."""
    with patch("repomatic.git_ops.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        create_tag("v1.0.0")
        mock_run.assert_called_once_with(
            ["git", "tag", "v1.0.0"],
            check=True,
            capture_output=True,
            text=True,
        )


def test_create_tag_at_commit():
    """Create tag at specific commit."""
    with patch("repomatic.git_ops.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        create_tag("v1.0.0", "abc123")
        mock_run.assert_called_once_with(
            ["git", "tag", "v1.0.0", "abc123"],
            check=True,
            capture_output=True,
            text=True,
        )


def test_create_tag_failure():
    """Raise exception on git failure."""
    with patch("repomatic.git_ops.subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.CalledProcessError(1, "git")
        with pytest.raises(subprocess.CalledProcessError):
            create_tag("v1.0.0")


def test_push_tag_default_remote():
    """Push tag to default origin remote."""
    with patch("repomatic.git_ops.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        push_tag("v1.0.0")
        mock_run.assert_called_once_with(
            ["git", "push", "origin", "v1.0.0"],
            check=True,
            capture_output=True,
            text=True,
        )


def test_push_tag_custom_remote():
    """Push tag to custom remote."""
    with patch("repomatic.git_ops.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        push_tag("v1.0.0", remote="upstream")
        mock_run.assert_called_once_with(
            ["git", "push", "upstream", "v1.0.0"],
            check=True,
            capture_output=True,
            text=True,
        )


def test_push_tag_failure():
    """Raise exception on push failure."""
    with patch("repomatic.git_ops.subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.CalledProcessError(1, "git")
        with pytest.raises(subprocess.CalledProcessError):
            push_tag("v1.0.0")


def test_create_new_tag():
    """Create and push new tag."""
    with (
        patch("repomatic.git_ops.tag_exists", return_value=False),
        patch("repomatic.git_ops.create_tag") as mock_create,
        patch("repomatic.git_ops.push_tag") as mock_push,
    ):
        result = create_and_push_tag("v1.0.0")
        assert result is True
        mock_create.assert_called_once_with("v1.0.0", None)
        mock_push.assert_called_once_with("v1.0.0")


def test_create_and_push_tag_at_commit():
    """Create and push tag at specific commit."""
    with (
        patch("repomatic.git_ops.tag_exists", return_value=False),
        patch("repomatic.git_ops.create_tag") as mock_create,
        patch("repomatic.git_ops.push_tag"),
    ):
        result = create_and_push_tag("v1.0.0", commit="abc123")
        assert result is True
        mock_create.assert_called_once_with("v1.0.0", "abc123")


def test_skip_existing_tag():
    """Skip when tag exists and skip_existing is True."""
    with (
        patch("repomatic.git_ops.tag_exists", return_value=True),
        patch("repomatic.git_ops.create_tag") as mock_create,
        patch("repomatic.git_ops.push_tag") as mock_push,
    ):
        result = create_and_push_tag("v1.0.0", skip_existing=True)
        assert result is False
        mock_create.assert_not_called()
        mock_push.assert_not_called()


def test_error_existing_tag():
    """Raise ValueError when tag exists and skip_existing is False."""
    with (
        patch("repomatic.git_ops.tag_exists", return_value=True),
        pytest.raises(ValueError, match="already exists"),
    ):
        create_and_push_tag("v1.0.0", skip_existing=False)


def test_create_without_push():
    """Create tag without pushing."""
    with (
        patch("repomatic.git_ops.tag_exists", return_value=False),
        patch("repomatic.git_ops.create_tag") as mock_create,
        patch("repomatic.git_ops.push_tag") as mock_push,
    ):
        result = create_and_push_tag("v1.0.0", push=False)
        assert result is True
        mock_create.assert_called_once()
        mock_push.assert_not_called()


def test_get_latest_tag_version():
    """Test that we can retrieve the latest Git tag version."""
    latest = get_latest_tag_version()
    # In CI environments with shallow clones, tags may not be available.
    if latest is None:
        pytest.skip("No release tags available (shallow clone in CI).")
    assert isinstance(latest, Version)
    # Sanity check: version should be a reasonable semver.
    assert latest.major >= 0
    assert latest.minor >= 0
    assert latest.micro >= 0


def test_get_release_version_from_commits():
    """Test that get_release_version_from_commits returns expected type.

    This function searches recent commits for release messages matching
    ``[changelog] Release vX.Y.Z`` pattern and extracts the version.
    """
    result = get_release_version_from_commits()
    # Result can be None (no release commits) or a Version object.
    assert result is None or isinstance(result, Version)
    if result is not None:
        # Sanity check: version should be a reasonable semver.
        assert result.major >= 0
        assert result.minor >= 0
        assert result.micro >= 0


def test_get_release_version_from_commits_max_count():
    """Test that max_count parameter limits commit search."""
    # With max_count=1, we only check the HEAD commit.
    result = get_release_version_from_commits(max_count=1)
    assert result is None or isinstance(result, Version)

    # With max_count=0, no commits should be checked.
    result = get_release_version_from_commits(max_count=0)
    assert result is None


def test_is_version_bump_allowed_returns_bool():
    """Test that is_version_bump_allowed returns a boolean."""
    # Test minor check.
    result = is_version_bump_allowed("minor")
    assert isinstance(result, bool)

    # Test major check.
    result = is_version_bump_allowed("major")
    assert isinstance(result, bool)


def test_is_version_bump_allowed_invalid_part():
    """Test that is_version_bump_allowed raises for invalid parts."""
    with pytest.raises(ValueError, match="Invalid version part"):
        is_version_bump_allowed("patch")  # type: ignore[arg-type]


def test_is_version_bump_allowed_current_repo():
    """Test the version bump check logic against the current repository state.

    This test verifies the correct behavior based on comparing current version
    in pyproject.toml against the latest Git tag.
    """
    current_version_str = Metadata.get_current_version()
    assert current_version_str is not None
    current = Version(current_version_str)

    latest_tag = get_latest_tag_version()
    # In CI environments with shallow clones, tags may not be available.
    if latest_tag is None:
        pytest.skip("No release tags available (shallow clone in CI).")

    # Verify the logic matches what the function should return.
    minor_allowed = is_version_bump_allowed("minor")
    major_allowed = is_version_bump_allowed("major")

    # Expected: minor bump blocked if minor already ahead (within same major).
    expected_minor_blocked = current.major > latest_tag.major or (
        current.major == latest_tag.major and current.minor > latest_tag.minor
    )
    assert minor_allowed == (not expected_minor_blocked)

    # Expected: major bump blocked if major already ahead.
    expected_major_blocked = current.major > latest_tag.major
    assert major_allowed == (not expected_major_blocked)


def test_minor_bump_allowed_property() -> None:
    """Test that minor_bump_allowed property returns a boolean."""
    metadata = Metadata()
    assert isinstance(metadata.minor_bump_allowed, bool)


def test_major_bump_allowed_property() -> None:
    """Test that major_bump_allowed property returns a boolean."""
    metadata = Metadata()
    assert isinstance(metadata.major_bump_allowed, bool)


def test_is_version_bump_allowed_uses_commit_fallback():
    """Test that is_version_bump_allowed still works when tags might not be available.

    This test verifies the function returns a boolean regardless of whether
    tags are found, as it now has a fallback to parse commit messages.
    """
    # The function should always return a boolean, even if tags aren't available.
    result = is_version_bump_allowed("minor")
    assert isinstance(result, bool)

    result = is_version_bump_allowed("major")
    assert isinstance(result, bool)
