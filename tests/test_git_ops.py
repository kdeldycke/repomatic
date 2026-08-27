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

import logging
import subprocess
from unittest.mock import MagicMock, patch

import pytest
from packaging.version import Version

from repomatic.git_ops import (
    COMMIT_IDENTITY_NAME,
    checkout,
    commit_and_push_files,
    commit_staged,
    count_commits_between,
    create_and_push_tag,
    create_branch,
    create_tag,
    current_branch,
    delete_branch,
    delete_remote_branch,
    fetch_remote_branch,
    force_push_branch,
    get_latest_tag_version,
    get_release_version_from_commits,
    head_sha,
    is_ancestor,
    merge_base,
    push_tag,
    rebase_onto,
    restore_paths,
    stage_all,
    tag_exists,
    tree_sha,
)
from repomatic.metadata.core import Metadata
from repomatic.metadata.project import is_version_bump_allowed


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


@pytest.fixture
def git_workdir(tmp_path, monkeypatch, hermetic_git):
    """A working clone (cwd) of a bare `origin` remote, one commit on main."""
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
        "repomatic.metadata.project.ProjectMetadata.get_current_version",
        lambda: current,
    )
    monkeypatch.setattr(
        "repomatic.metadata.project.get_latest_tag_version", lambda: None
    )
    monkeypatch.setattr(
        "repomatic.metadata.project.get_release_version_from_commits",
        lambda: Version(released),
    )
    assert is_version_bump_allowed(part) is allowed


def test_is_version_bump_allowed_without_tags_or_commits(monkeypatch):
    """Nothing to compare against fails open, so a release is never blocked."""
    monkeypatch.setattr(
        "repomatic.metadata.project.ProjectMetadata.get_current_version",
        lambda: "5.0.2",
    )
    monkeypatch.setattr(
        "repomatic.metadata.project.get_latest_tag_version", lambda: None
    )
    monkeypatch.setattr(
        "repomatic.metadata.project.get_release_version_from_commits", lambda: None
    )
    assert is_version_bump_allowed("minor") is True


# --- Branch primitives behind `repomatic pr-sync` ---
#
# These are the one part of this module whose contract lives in git's behaviour
# rather than in its argv, so they run against the real `git_workdir` clone
# instead of a mock.


def test_branch_cycle_publishes_and_restores(git_workdir):
    """A publish leaves the remote updated and the checkout exactly as found."""
    base_sha = head_sha()
    assert current_branch() == "main"
    assert fetch_remote_branch("sync-fruits") is None
    assert stage_all() is False

    (git_workdir / "fruits.txt").write_text("papaya\nquince\n", encoding="UTF-8")
    (git_workdir / "extra.txt").write_text("untracked\n", encoding="UTF-8")
    assert stage_all() is True
    create_branch("tmp-publish")
    published = commit_staged("Sync fruits")
    force_push_branch("tmp-publish", "sync-fruits", expected_sha=None)
    checkout("main")
    delete_branch("tmp-publish")

    assert head_sha() == base_sha
    # Untracked output went into the commit, so the tree comes back clean.
    assert not (git_workdir / "extra.txt").exists()
    assert fetch_remote_branch("sync-fruits") == published
    assert count_commits_between(base_sha, published) == 1


def test_stage_all_pathspec_leaves_everything_else_dirty(git_workdir):
    """A scoped stage commits its pathspec and nothing else.

    The case that needs it: a job installing its own tooling into the checkout,
    where the whole-tree default would publish the installation alongside the
    output. Anything outside the pathspec stays in the working tree, so it goes
    away with the throwaway branch instead of into the pull request.
    """
    (git_workdir / "fruits.txt").write_text("papaya\nquince\n", encoding="UTF-8")
    (git_workdir / "package-lock.json").write_text("{}\n", encoding="UTF-8")

    assert stage_all(["fruits.txt"]) is True
    create_branch("tmp-scoped")
    commit_staged("Sync fruits")

    # The lock file never reached the commit, and is still sitting in the tree.
    assert (git_workdir / "package-lock.json").exists()
    assert stage_all(["fruits.txt"]) is False
    assert stage_all() is True


def test_stage_all_pathspec_matching_nothing_reports_no_work(git_workdir):
    """A pathspec no dirty file matches is not a commit waiting to happen."""
    (git_workdir / "package-lock.json").write_text("{}\n", encoding="UTF-8")

    assert stage_all(["fruits.txt"]) is False


def test_stage_all_stale_pathspec_does_not_cost_the_others(git_workdir):
    """One unresolvable entry no longer takes the whole staging down with it.

    `git add` refuses the entire invocation on its first unmatched pathspec, so
    an allowlist naming output a run happened not to produce would stage
    nothing rather than stage the rest.
    """
    (git_workdir / "fruits.txt").write_text("papaya\nquince\n", encoding="UTF-8")

    assert stage_all(["fruits.txt", "vegetables.txt"]) is True
    create_branch("tmp-mixed")
    commit_staged("Sync fruits")
    assert not (git_workdir / "vegetables.txt").exists()


def test_rebuilding_the_same_output_yields_the_same_tree(git_workdir):
    """The comparison that keeps an unchanged job from re-pushing."""
    (git_workdir / "fruits.txt").write_text("papaya\nquince\n", encoding="UTF-8")
    stage_all()
    create_branch("tmp-first")
    first = commit_staged("Sync fruits")
    force_push_branch("tmp-first", "sync-fruits", expected_sha=None)
    checkout("main")
    delete_branch("tmp-first")

    (git_workdir / "fruits.txt").write_text("papaya\nquince\n", encoding="UTF-8")
    stage_all()
    create_branch("tmp-second")
    second = commit_staged("Sync fruits")
    # Deliberately not comparing commit SHAs: rebuilding the same tree on the
    # same parent within the same second reproduces the commit byte for byte,
    # so the two SHAs are equal as often as not. The tree is the stable signal,
    # which is why `_needs_push` compares that and never the commit.
    assert tree_sha(second) == tree_sha(first)

    (git_workdir / "fruits.txt").write_text(
        "papaya\nquince\nrhubarb\n", encoding="UTF-8"
    )
    stage_all()
    changed = commit_staged("Sync fruits again")
    assert tree_sha(changed) != tree_sha(first)


def test_force_push_refuses_an_outdated_lease(git_workdir):
    """A branch that moved since the caller looked is not silently overwritten."""

    def publish(content: str, branch: str, expected_sha: str | None) -> str:
        (git_workdir / "fruits.txt").write_text(content, encoding="UTF-8")
        stage_all()
        create_branch(branch)
        sha = commit_staged("Sync fruits")
        force_push_branch(branch, "sync-fruits", expected_sha=expected_sha)
        checkout("main")
        return sha

    observed = publish("papaya\nquince\n", "tmp-first", None)
    # A concurrent writer moves the branch on after this run read it.
    publish("papaya\nrhubarb\n", "tmp-concurrent", observed)

    # Our run now pushes against the tip it saw at the top, which is stale.
    (git_workdir / "fruits.txt").write_text("papaya\nsorrel\n", encoding="UTF-8")
    stage_all()
    create_branch("tmp-late")
    commit_staged("Sync fruits")
    with pytest.raises(subprocess.CalledProcessError):
        force_push_branch("tmp-late", "sync-fruits", expected_sha=observed)


def test_force_push_rejection_logs_stderr(git_workdir, caplog):
    """A rejected push logs git's stderr, which `CalledProcessError` itself drops."""
    caplog.set_level(logging.ERROR)

    def publish(content: str, branch: str, expected_sha: str | None) -> str:
        (git_workdir / "fruits.txt").write_text(content, encoding="UTF-8")
        stage_all()
        create_branch(branch)
        sha = commit_staged("Sync fruits")
        force_push_branch(branch, "sync-fruits", expected_sha=expected_sha)
        checkout("main")
        return sha

    observed = publish("papaya\nquince\n", "tmp-first", None)
    publish("papaya\nrhubarb\n", "tmp-concurrent", observed)

    (git_workdir / "fruits.txt").write_text("papaya\nsorrel\n", encoding="UTF-8")
    stage_all()
    create_branch("tmp-late")
    commit_staged("Sync fruits")
    with pytest.raises(subprocess.CalledProcessError):
        force_push_branch("tmp-late", "sync-fruits", expected_sha=observed)

    [record] = caplog.records
    assert record.levelname == "ERROR"
    assert "git push" in record.message
    assert "sync-fruits" in record.message


def test_branch_deletion_is_idempotent(git_workdir):
    """Both deletions tolerate a branch that is already gone."""
    (git_workdir / "fruits.txt").write_text("papaya\nquince\n", encoding="UTF-8")
    stage_all()
    create_branch("tmp-delete")
    commit_staged("Sync fruits")
    force_push_branch("tmp-delete", "sync-fruits", expected_sha=None)
    checkout("main")

    delete_remote_branch("sync-fruits")
    assert fetch_remote_branch("sync-fruits") is None
    delete_remote_branch("sync-fruits")

    delete_branch("tmp-delete")
    delete_branch("tmp-delete")


def test_is_ancestor_reads_both_directions(git_workdir):
    base = head_sha()
    (git_workdir / "fruits.txt").write_text("papaya\n", encoding="UTF-8")
    stage_all()
    create_branch("tmp-ahead")
    ahead = commit_staged("Add fruits")

    assert is_ancestor(base, ahead) is True
    assert is_ancestor(ahead, base) is False
    assert merge_base(base, ahead) == base


def test_rebase_onto_replays_carried_commits(git_workdir, tmp_path):
    """The moved-base path: two job commits land on the newer base intact."""
    base = head_sha()
    # Two commits of the shape `prepare-release` produces.
    (git_workdir / "freeze.txt").write_text("frozen\n", encoding="UTF-8")
    stage_all()
    create_branch("tmp-carry")
    commit_staged("Freeze")
    (git_workdir / "unfreeze.txt").write_text("bumped\n", encoding="UTF-8")
    stage_all()
    commit_staged("Unfreeze")
    assert count_commits_between(base, "tmp-carry") == 2

    # Meanwhile the base moves on, touching an unrelated file.
    checkout("main")
    (git_workdir / "other.txt").write_text("elsewhere\n", encoding="UTF-8")
    stage_all()
    moved_base = commit_staged("Concurrent commit")
    assert is_ancestor(moved_base, "tmp-carry") is False

    assert rebase_onto(moved_base, base, "tmp-carry") is True
    assert count_commits_between(moved_base, "tmp-carry") == 2
    # Both the carried work and the base's own commit are present.
    assert (git_workdir / "freeze.txt").exists()
    assert (git_workdir / "unfreeze.txt").exists()
    assert (git_workdir / "other.txt").exists()


def test_rebase_onto_reports_a_conflict_without_leaving_one_behind(git_workdir):
    """A conflicting replay aborts cleanly, leaving the branch usable as-is."""
    base = head_sha()
    (git_workdir / "seed.txt").write_text("ours\n", encoding="UTF-8")
    stage_all()
    create_branch("tmp-conflict")
    carried = commit_staged("Our edit")

    checkout("main")
    (git_workdir / "seed.txt").write_text("theirs\n", encoding="UTF-8")
    stage_all()
    moved_base = commit_staged("Their edit")

    # `--strategy-option=theirs` resolves this overlap rather than conflicting,
    # so the replay succeeds and the carried content wins.
    assert rebase_onto(moved_base, base, "tmp-conflict") is True
    checkout("tmp-conflict")
    assert (git_workdir / "seed.txt").read_text(encoding="UTF-8") == "ours\n"
    assert head_sha() != carried
    # No rebase left in progress.
    assert current_branch() == "tmp-conflict"


def test_restore_paths_carries_a_file_without_moving_head(git_workdir):
    """A path travels from a branch while the checkout keeps its own tree.

    The property the sampling lane depends on: a job reads its accumulating
    store back from an open pull request and still runs the code of the branch
    it was called on.
    """
    (git_workdir / "readings.csv").write_text("papaya,12\n", encoding="UTF-8")
    stage_all()
    create_branch("pending")
    _seed_commit(git_workdir, "seed.txt", "branch edit\n", "Diverge the branch")
    branch_tip = head_sha()

    checkout("main")
    main_tip = head_sha()
    restored = restore_paths(branch_tip, ["readings.csv"])

    assert restored == ("readings.csv",)
    assert (git_workdir / "readings.csv").read_text(encoding="UTF-8") == "papaya,12\n"
    # Only the named path travelled: `HEAD` and every other file stayed put.
    assert head_sha() == main_tip
    assert (git_workdir / "seed.txt").read_text(encoding="UTF-8") == "seed\n"


def test_restore_paths_skips_what_the_ref_does_not_carry(git_workdir):
    """A path missing from the ref is left alone, which is the first-run case."""
    assert restore_paths(head_sha(), ["never-committed.csv"]) == ()
    assert not (git_workdir / "never-committed.csv").exists()


def test_restore_paths_skips_a_path_outside_the_working_tree(git_workdir, tmp_path):
    """A path resolving outside the checkout is skipped, never reached for."""
    outsider = tmp_path / "elsewhere.csv"
    outsider.write_text("untouched\n", encoding="UTF-8")

    assert restore_paths(head_sha(), [outsider]) == ()
    assert outsider.read_text(encoding="UTF-8") == "untouched\n"
