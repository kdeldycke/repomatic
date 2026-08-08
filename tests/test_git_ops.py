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
    COMMIT_IDENTITY_NAME,
    commit_and_push_files,
    create_and_push_tag,
    create_tag,
    get_latest_tag_version,
    get_release_version_from_commits,
    push_tag,
    tag_exists,
)
from repomatic.metadata import Metadata, is_version_bump_allowed


@pytest.mark.parametrize(
    ("call", "expected_argv"),
    (
        pytest.param(
            lambda: tag_exists("v1.0.0"),
            ["git", "show-ref", "--tags", "v1.0.0", "--quiet"],
            id="tag-exists",
        ),
        pytest.param(
            lambda: create_tag("v1.0.0"), ["git", "tag", "v1.0.0"], id="create-at-head"
        ),
        pytest.param(
            lambda: create_tag("v1.0.0", "abc123"),
            ["git", "tag", "v1.0.0", "abc123"],
            id="create-at-commit",
        ),
        pytest.param(
            lambda: push_tag("v1.0.0"),
            ["git", "push", "origin", "v1.0.0"],
            id="push-default-remote",
        ),
        pytest.param(
            lambda: push_tag("v1.0.0", remote="upstream"),
            ["git", "push", "upstream", "v1.0.0"],
            id="push-custom-remote",
        ),
    ),
)
def test_tag_command_argv(call, expected_argv):
    """Each tag helper shells out to exactly the git command it names."""
    with patch("repomatic.git_ops.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        call()
        mock_run.assert_called_once()
        assert mock_run.call_args.args[0] == expected_argv


def test_tag_exists_false():
    """A non-zero exit from `git show-ref` reads as a missing tag."""
    with patch("repomatic.git_ops.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=1)
        assert tag_exists("v1.0.0") is False


@pytest.mark.parametrize(
    "call",
    (
        pytest.param(lambda: create_tag("v1.0.0"), id="create"),
        pytest.param(lambda: push_tag("v1.0.0"), id="push"),
    ),
)
def test_tag_command_propagates_git_failure(call):
    """A failed git invocation surfaces rather than being swallowed."""
    with patch("repomatic.git_ops.subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.CalledProcessError(1, "git")
        with pytest.raises(subprocess.CalledProcessError):
            call()


# --- commit_and_push_files ---


def _run_git(*args: str, cwd) -> str:
    """Run a git command in *cwd* and return its stdout."""
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        encoding="UTF-8",
    )
    return result.stdout


def _seed_commit(workdir, filename: str, content: str, message: str) -> None:
    """Create a file and commit it with a throwaway identity."""
    (workdir / filename).write_text(content, encoding="UTF-8")
    _run_git("add", "--", filename, cwd=workdir)
    _run_git(
        "-c",
        "user.name=Seeder",
        "-c",
        "user.email=seeder@example.com",
        "commit",
        "--message",
        message,
        cwd=workdir,
    )


@pytest.fixture()
def git_workdir(tmp_path, monkeypatch):
    """A working clone (cwd) of a bare `origin` remote, one commit on main."""
    # Shield the temporary repos (and the code under test) from the
    # developer's global git config: commit signing, hooks, and identity
    # would otherwise leak in and break hermeticity.
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")
    remote = tmp_path / "remote.git"
    _run_git("init", "--bare", "--initial-branch=main", str(remote), cwd=tmp_path)
    work = tmp_path / "work"
    _run_git("clone", str(remote), str(work), cwd=tmp_path)
    _run_git("checkout", "-B", "main", cwd=work)
    _seed_commit(work, "seed.txt", "seed\n", "Seed commit")
    _run_git("push", "--set-upstream", "origin", "main", cwd=work)
    monkeypatch.chdir(work)
    return work


def test_commit_and_push_files_no_changes(git_workdir):
    """Unchanged files produce no commit and report False."""
    assert commit_and_push_files(["seed.txt"], "No-op") is False
    assert "No-op" not in _run_git("log", "--format=%s", cwd=git_workdir)


def test_commit_and_push_files_pushes(git_workdir, tmp_path):
    """A changed file is committed with the bot identity and pushed."""
    (git_workdir / "seed.txt").write_text("updated\n", encoding="UTF-8")
    assert commit_and_push_files(["seed.txt"], "Record update") is True
    remote_log = _run_git("log", "--format=%s|%an", "main", cwd=tmp_path / "remote.git")
    assert f"Record update|{COMMIT_IDENTITY_NAME}" in remote_log


def test_commit_and_push_files_rebases_on_rejection(git_workdir, tmp_path):
    """A concurrent push to another file is absorbed by fetch and rebase."""
    other = tmp_path / "other"
    _run_git("clone", str(tmp_path / "remote.git"), str(other), cwd=tmp_path)
    _seed_commit(other, "other.txt", "other\n", "Concurrent commit")
    _run_git("push", "origin", "main", cwd=other)

    (git_workdir / "seed.txt").write_text("updated\n", encoding="UTF-8")
    assert commit_and_push_files(["seed.txt"], "Record update") is True
    remote_log = _run_git("log", "--format=%s", "main", cwd=tmp_path / "remote.git")
    assert "Record update" in remote_log
    assert "Concurrent commit" in remote_log


def test_commit_and_push_files_conflict_raises(git_workdir, tmp_path):
    """A concurrent push touching the same lines aborts with a clear error."""
    other = tmp_path / "other"
    _run_git("clone", str(tmp_path / "remote.git"), str(other), cwd=tmp_path)
    _seed_commit(other, "seed.txt", "theirs\n", "Concurrent conflicting commit")
    _run_git("push", "origin", "main", cwd=other)

    (git_workdir / "seed.txt").write_text("ours\n", encoding="UTF-8")
    with pytest.raises(RuntimeError, match="conflicted"):
        commit_and_push_files(["seed.txt"], "Record update")


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


@pytest.mark.parametrize(
    ("current", "released", "part", "allowed"),
    (
        # Only the patch moved since the release: neither part has been bumped
        # in this cycle, so both are still on the table.
        ("5.0.2", "5.0.1", "minor", True),
        ("5.0.2", "5.0.1", "major", True),
        # The minor was already bumped: bumping it again would double-increment,
        # but the major is untouched and still allowed.
        ("5.1.0", "5.0.1", "minor", False),
        ("5.1.0", "5.0.1", "major", True),
        # The major was already bumped, which blocks both parts.
        ("6.0.0", "5.0.1", "minor", False),
        ("6.0.0", "5.0.1", "major", False),
    ),
)
def test_is_version_bump_allowed_uses_commit_fallback(
    monkeypatch, current, released, part, allowed
):
    """With no tag reachable, the verdict comes from the release commit.

    The release workflow pushes its tag after the bump job starts, so a shallow
    clone or a plain race leaves `get_latest_tag_version` empty. Parsing the
    release commit is what keeps the guard working through that window; without
    the fallback the check fails open and allows a double increment.
    """
    monkeypatch.setattr(
        "repomatic.metadata.Metadata.get_current_version", lambda: current
    )
    monkeypatch.setattr("repomatic.metadata.get_latest_tag_version", lambda: None)
    monkeypatch.setattr(
        "repomatic.metadata.get_release_version_from_commits",
        lambda: Version(released),
    )
    assert is_version_bump_allowed(part) is allowed


def test_is_version_bump_allowed_without_tags_or_commits(monkeypatch):
    """Nothing to compare against fails open, so a release is never blocked."""
    monkeypatch.setattr(
        "repomatic.metadata.Metadata.get_current_version", lambda: "5.0.2"
    )
    monkeypatch.setattr("repomatic.metadata.get_latest_tag_version", lambda: None)
    monkeypatch.setattr(
        "repomatic.metadata.get_release_version_from_commits", lambda: None
    )
    assert is_version_bump_allowed("minor") is True
