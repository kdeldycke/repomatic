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

import pytest
from click_extra.testing import CliRunner
from packaging.version import Version

from repomatic.cli.main import repomatic
from repomatic.git_ops import (
    COMMIT_IDENTITY_EMAIL,
    COMMIT_IDENTITY_NAME,
    GitError,
    changed_paths,
    checkout,
    commit_exists,
    commit_staged,
    commit_tracked,
    count_commits_between,
    create_and_push_tag,
    create_branch,
    current_branch,
    deepen_until_related,
    delete_branch,
    delete_remote_branch,
    discard_changes,
    fetch_commit,
    fetch_remote_branch,
    force_push_branch,
    get_all_version_tags,
    get_commit,
    get_latest_tag_version,
    get_release_version_from_commits,
    head_sha,
    is_ancestor,
    is_shallow,
    list_commits,
    merge_base,
    rebase_onto,
    restore_paths,
    show_file,
    stage_all,
    tree_sha,
)
from repomatic.metadata.project import is_version_bump_allowed
from repomatic.pyproject import get_current_version
from tests.conftest import make_shallow_clone, run_git, seed_commit

# --- Repository fixtures ---


@pytest.fixture
def git_workdir(tmp_path, monkeypatch, hermetic_git):
    """A working clone (cwd) of a bare `origin` remote, one commit on main."""
    remote = tmp_path / "remote.git"
    run_git("init", "--bare", "--initial-branch=main", str(remote), cwd=tmp_path)
    work = tmp_path / "work"
    run_git("clone", str(remote), str(work), cwd=tmp_path)
    run_git("checkout", "-B", "main", cwd=work)
    seed_commit(work, "seed.txt", "seed\n", "Seed commit")
    run_git("push", "--set-upstream", "origin", "main", cwd=work)
    monkeypatch.chdir(work)
    return work


@pytest.fixture
def shallow_clone(tmp_path, monkeypatch, hermetic_git):
    """The SHAs of a depth-1 clone's remote history, the clone being the cwd."""
    clone, shas = make_shallow_clone(tmp_path)
    monkeypatch.chdir(clone)
    return shas


# --- Errors and reads ---


def test_git_error_carries_stderr(git_workdir):
    """A failure names the command and carries git's own error output."""
    with pytest.raises(GitError) as excinfo:
        get_commit("no-such-papaya")
    error = excinfo.value
    assert error.returncode == 128
    assert str(error).startswith("git log ")
    assert "no-such-papaya" in error.stderr
    assert error.stderr.strip() in str(error)
    # Callers handling any failed external command catch it too.
    assert isinstance(error, subprocess.CalledProcessError)
    assert isinstance(error, RuntimeError)


def test_show_file_reads_a_past_commit_in_place(git_workdir):
    """A past version of a file is read without moving `HEAD`."""
    first = head_sha()
    seed_commit(git_workdir, "seed.txt", "changed\n", "Change the seed")
    tip = head_sha()

    assert show_file(first, "seed.txt") == "seed\n"
    assert show_file(tip, "seed.txt") == "changed\n"
    assert show_file(first, "missing.txt") is None
    assert head_sha() == tip


# --- Tags ---


def test_create_and_push_tag_publishes_the_tag(git_workdir, tmp_path):
    """The tag lands on the remote, at `HEAD` by default."""
    assert create_and_push_tag("v1.0.0") is True
    remote_tags = run_git("tag", "--list", cwd=tmp_path / "remote.git")
    assert remote_tags.split() == ["v1.0.0"]
    tagged = run_git("rev-list", "-n", "1", "v1.0.0", cwd=git_workdir)
    assert tagged.strip() == head_sha()


def test_create_and_push_tag_at_commit_without_push(git_workdir, tmp_path):
    """A tag can point at an older commit, and stay local."""
    base = head_sha()
    seed_commit(git_workdir, "fruits.txt", "papaya\n", "Add fruits")

    assert create_and_push_tag("v1.0.0", commit=base, push=False) is True
    tagged = run_git("rev-list", "-n", "1", "v1.0.0", cwd=git_workdir)
    assert tagged.strip() == base
    assert not run_git("tag", "--list", cwd=tmp_path / "remote.git").strip()


def test_create_and_push_tag_skips_an_existing_tag(git_workdir):
    """Re-running the tag step is a no-op, unless asked to fail."""
    assert create_and_push_tag("v1.0.0", push=False) is True
    assert create_and_push_tag("v1.0.0", push=False) is False
    with pytest.raises(ValueError, match="already exists"):
        create_and_push_tag("v1.0.0", push=False, skip_existing=False)


def test_create_and_push_tag_matches_the_exact_tag(git_workdir):
    """A tag whose name only ends like the one asked for is not a match."""
    run_git("tag", "release/v1.0.0", cwd=git_workdir)
    assert create_and_push_tag("v1.0.0", push=False) is True


def test_git_tag_command_reports_each_outcome(git_workdir, tmp_path):
    """The command reports a new tag, skips an existing one, or fails on it."""
    output = tmp_path / "github-output.txt"
    runner = CliRunner()

    result = runner.invoke(
        repomatic, ["git-tag", "--tag", "v1.0.0", "--output", str(output)]
    )
    assert result.exit_code == 0, result.output
    assert "Created and pushed tag 'v1.0.0'" in result.output
    assert output.read_text(encoding="UTF-8") == "created=true\n"

    result = runner.invoke(
        repomatic, ["git-tag", "--tag", "v1.0.0", "--output", str(output)]
    )
    assert result.exit_code == 0, result.output
    assert "Tag 'v1.0.0' already exists, skipped." in result.output
    assert output.read_text(encoding="UTF-8") == "created=false\n"

    result = runner.invoke(repomatic, ["git-tag", "--tag", "v1.0.1", "--no-push"])
    assert result.exit_code == 0, result.output
    assert "Created tag 'v1.0.1'" in result.output

    result = runner.invoke(
        repomatic, ["git-tag", "--tag", "v1.0.0", "--error-existing"]
    )
    assert result.exit_code == 1, result.output
    assert "Tag 'v1.0.0' already exists." in result.output


def test_version_tags_skip_names_that_do_not_parse(git_workdir):
    """A tag the glob selects but PEP 440 refuses is left out of every reader."""
    for tag in ("v1.2.3", "v1.2.3_hotfix", "v1.10.0", "nightly"):
        run_git("tag", tag, cwd=git_workdir)

    assert get_latest_tag_version() == Version("1.10.0")
    assert set(get_all_version_tags()) == {"1.2.3", "1.10.0"}


# --- Shallow clones ---


def test_fetch_commit_brings_a_commit_without_its_history(shallow_clone):
    """A commit fetched on its own is a boundary of its own."""
    oldest, tip = shallow_clone[0], shallow_clone[-1]
    assert is_shallow()
    assert not commit_exists(oldest)

    assert fetch_commit(oldest) is True
    assert commit_exists(oldest)
    assert merge_base(oldest, tip) is None
    # Already present: nothing to fetch.
    assert fetch_commit(oldest) is True


def test_fetch_commit_reports_a_commit_the_remote_does_not_serve(shallow_clone):
    assert fetch_commit("f" * 40) is False


def test_deepening_relates_a_commit_fetched_on_its_own(shallow_clone):
    """Presence of both ends is not enough to walk the range between them.

    The order `metadata` resolves its keys in can fetch the start of the range
    before the range itself is resolved. A range walked from there would stop
    at the boundary of the clone, and miss every commit below the tip.
    """
    start, end = shallow_clone[1], shallow_clone[-1]
    assert fetch_commit(start)

    assert deepen_until_related(start, end, depth=1) is True
    assert [commit.hash for commit in list_commits(start, end)] == shallow_clone[2:]


def test_deepening_gives_up_after_its_attempts(shallow_clone):
    start, end = shallow_clone[0], shallow_clone[-1]
    assert fetch_commit(start)

    assert deepen_until_related(start, end, attempts=1, depth=1) is False


def test_deepening_stops_on_a_complete_clone(git_workdir):
    """A complete clone has no boundary to deepen: unrelated commits stay so."""
    assert not is_shallow()
    assert deepen_until_related("f" * 40, head_sha()) is False


@pytest.mark.repo_maintenance
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


@pytest.mark.repo_maintenance
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


@pytest.mark.repo_maintenance
def test_get_release_version_from_commits_max_count():
    """Test that max_count parameter limits commit search."""
    # With max_count=1, we only check the HEAD commit.
    result = get_release_version_from_commits(max_count=1)
    assert result is None or isinstance(result, Version)

    # With max_count=0, no commits should be checked.
    result = get_release_version_from_commits(max_count=0)
    assert result is None


@pytest.mark.repo_maintenance
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


@pytest.mark.repo_maintenance
def test_is_version_bump_allowed_current_repo():
    """Test the version bump check logic against the current repository state.

    This test verifies the correct behavior based on comparing current version
    in pyproject.toml against the latest Git tag.
    """
    current_version_str = get_current_version()
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
        "repomatic.metadata.project.get_current_version",
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
        "repomatic.metadata.project.get_current_version",
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


def test_discard_changes_drops_what_a_failed_commit_staged(git_workdir):
    """The index goes back to `HEAD` with the tree, untracked files aside.

    An updater whose commit fails after its `git add` leaves its diff staged.
    Restored from the index alone, the tree would keep that diff, and the
    whole-tree stage of the next updater would publish it in its own pull
    request.
    """
    (git_workdir / "seed.txt").write_text("papaya\n", encoding="UTF-8")
    (git_workdir / "fruits.txt").write_text("quince\n", encoding="UTF-8")
    assert stage_all() is True
    (git_workdir / "seed.txt").write_text("melon\n", encoding="UTF-8")
    (git_workdir / "leftover.txt").write_text("stray\n", encoding="UTF-8")

    discard_changes()

    assert (git_workdir / "seed.txt").read_text(encoding="UTF-8") == "seed\n"
    assert not (git_workdir / "fruits.txt").exists()
    assert (git_workdir / "leftover.txt").exists()
    assert run_git("diff", "--cached", "--name-only", cwd=git_workdir) == ""


def test_changed_paths_lists_what_a_reset_would_discard(git_workdir):
    """Staged and unstaged changes count, a rename as its two paths.

    An untracked file is no change: no reset to `HEAD` touches it.
    """
    assert changed_paths() == ()
    seed_commit(git_workdir, "pears.txt", "pear\n", "Seed pears")
    (git_workdir / "seed.txt").write_text("papaya\n", encoding="UTF-8")
    run_git("mv", "pears.txt", "plums.txt", cwd=git_workdir)
    (git_workdir / "kiwis.txt").write_text("kiwi\n", encoding="UTF-8")
    run_git("add", "kiwis.txt", cwd=git_workdir)
    (git_workdir / "notes.txt").write_text("untracked\n", encoding="UTF-8")

    assert changed_paths() == ("kiwis.txt", "pears.txt", "plums.txt", "seed.txt")


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


def test_commit_tracked_leaves_untracked_files_out(git_workdir):
    """Only tracked files enter the commit, authored by the CI bot.

    The release commits carry what the freeze edited: a file a tool left in
    the checkout must not ride along into a published tag.
    """
    (git_workdir / "seed.txt").write_text("papaya\n", encoding="UTF-8")
    (git_workdir / "leftover.txt").write_text("stray\n", encoding="UTF-8")

    sha = commit_tracked("[changelog] Release v1.2.3")

    assert sha == head_sha()
    log = run_git("log", "-1", "--format=%an <%ae>%n%s", cwd=git_workdir)
    assert log.splitlines() == [
        f"{COMMIT_IDENTITY_NAME} <{COMMIT_IDENTITY_EMAIL}>",
        "[changelog] Release v1.2.3",
    ]
    assert run_git(
        "show", "--name-only", "--format=", "HEAD", cwd=git_workdir
    ).split() == ["seed.txt"]
    assert (git_workdir / "leftover.txt").exists()


def test_commit_tracked_refuses_an_empty_commit(git_workdir):
    """A freeze that changed nothing is an error, never an empty release commit."""
    with pytest.raises(GitError):
        commit_tracked("[changelog] Release v1.2.3")


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


def test_force_push_rejection_carries_stderr(git_workdir, caplog):
    """A rejected push raises with git's stderr, and logs nothing on its own."""
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
    with pytest.raises(GitError) as excinfo:
        force_push_branch("tmp-late", "sync-fruits", expected_sha=observed)

    assert str(excinfo.value).startswith("git push ")
    assert "sync-fruits" in str(excinfo.value)
    assert excinfo.value.stderr.strip() in str(excinfo.value)
    assert not caplog.records


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
    seed_commit(git_workdir, "seed.txt", "branch edit\n", "Diverge the branch")
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
