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

"""Git operations for GitHub Actions workflows.

This module provides utilities for common Git operations in CI/CD contexts,
with idempotent behavior to allow safe re-runs of failed workflows.

All operations follow a "belt-and-suspenders" approach: combine workflow
timing guarantees (like `workflow_run` ensuring tags exist) with idempotent
guards (like `skip_existing` on tag creation). This ensures correctness
in the face of race conditions, API eventual consistency, and partial failures
that are common in GitHub Actions.

Every git failure raises {class}`GitError`, which carries git's own error
output. A function returns `None` or `False` instead only when it is a probe,
whose question a failure answers.

```{warning} Tag push requires `REPOMATIC_PAT`

Tags pushed with the default `GITHUB_TOKEN` do not trigger downstream
`on.push.tags` workflows. The custom PAT is required so that tagging
a release commit actually fires the publish and release creation jobs.
```
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import NamedTuple

from packaging.version import Version

from .github.urls import parse_github_repo
from .release import protocol
from .versions import parse_tag_version

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Sequence

COMMIT_IDENTITY_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"
"""Commit author email for automated commits: GitHub's own Actions bot user.

The `41898282+` prefix is the bot's stable user ID, which makes GitHub link
the commit to the verified `github-actions[bot]` account.
"""

COMMIT_IDENTITY_NAME = "github-actions[bot]"
"""Commit author name for automated commits."""

_BOT_IDENTITY_ARGS = (
    "-c",
    f"user.name={COMMIT_IDENTITY_NAME}",
    "-c",
    f"user.email={COMMIT_IDENTITY_EMAIL}",
)
"""Per-command `git -c` overrides authoring a commit as the CI bot.

CI checkouts carry no git identity of their own, so every command that writes
a commit object (`commit`, `rebase`) has to supply one. Passed per-command
rather than written to the checkout's config, so nothing persists past the
call."""

SHORT_SHA_LENGTH = 7
"""Default SHA length hard-coded to `7`.

```{caution}

The [default is subject to change](https://stackoverflow.com/a/21015031) and
depends on the size of the repository.
```
"""

NULL_SHA = "0" * 40
"""The null SHA used by Git to represent a non-existent commit.

GitHub sends this value as the `before` SHA of a push that creates a branch or
a tag, since there is no previous commit to compare against.
"""

VERSION_TAG_GLOB = "v[0-9]*.[0-9]*.[0-9]*"
"""Shell glob that selects the release tags in `git tag --list`.

A glob is no PEP 440 filter: `v1.2.3_hotfix` matches it. So every tag it
selects is parsed, and a tag naming no version is skipped.
"""

GIT_LOG_FORMAT = "%H%x00%B"
"""`git log` pretty-format placeholders for a single commit: full SHA, then a
`NUL`, then the raw body.

Paired with `git log -z` (which terminates each commit's output with a `NUL`),
this frames the stream as alternating `(hash, message)` tokens. Commit messages
may contain newlines but never `NUL` bytes, so splitting on `NUL` recovers the
fields unambiguously even for multi-line messages.
"""


class GitError(subprocess.CalledProcessError, RuntimeError):
    """A `git` command that exited with a non-zero status.

    The message carries what git wrote to its standard error. The message of a
    plain `CalledProcessError` names only the command and the exit code, which
    leaves a CI failure with no way to tell a lease rejection from a
    permission error.

    Also a `RuntimeError`, like {class}`~repomatic.github.gh.GhCommandError`,
    so a caller handling any failed external command catches it too.
    """

    def __str__(self) -> str:
        command = (
            self.cmd if isinstance(self.cmd, str) else " ".join(map(str, self.cmd))
        )
        message = f"{command} exited with status {self.returncode}"
        detail = (self.stderr or "").strip()
        return f"{message}:\n{detail}" if detail else f"{message}."


class Commit(NamedTuple):
    """A minimal git commit.

    Only the hash and message are ever consumed downstream, so a full git
    library object (with diffs, modified-file analysis, and complexity metrics)
    is unnecessary: the `git` CLI feeds these two fields directly.
    """

    hash: str
    """The commit's full 40-character SHA-1 hash."""

    msg: str
    """The commit message, stripped of surrounding whitespace."""


def _git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run a `git` command and capture its output.

    Decodes output as UTF-8 so non-ASCII commit metadata (accented author
    names, emoji in messages) survives on platforms whose default encoding is
    not UTF-8 (Windows `cp1252`).

    Nothing is logged here. A failure travels as a {class}`GitError` carrying
    git's error output, and the caller decides whether it is fatal.

    :param check: Raise on a non-zero exit. A probe, which reads the exit
        status itself, passes `False`.
    :raises GitError: When *check* is set and git exits non-zero.
    """
    process = subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        encoding="UTF-8",
        check=False,
    )
    if check and process.returncode:
        raise GitError(
            process.returncode,
            process.args,
            output=process.stdout,
            stderr=process.stderr,
        )
    return process


def _parse_commit_log(output: str) -> tuple[Commit, ...]:
    """Parse `NUL`-framed `git log --format=GIT_LOG_FORMAT` output into commits.

    See {data}`GIT_LOG_FORMAT` for the framing. `git log -z` leaves a trailing
    empty token after the final commit; a plain `git log` does not. Either way,
    tokens pair up as `(hash, message)`.
    """
    tokens = output.split("\x00")
    if tokens and not tokens[-1]:
        tokens.pop()
    return tuple(
        Commit(hash=tokens[i], msg=tokens[i + 1].strip())
        for i in range(0, len(tokens) - 1, 2)
    )


def get_commit(ref: str = "HEAD") -> Commit:
    """Return the commit at *ref*.

    :raises GitError: if *ref* does not resolve to a commit present in the
        repository.
    """
    result = _git("log", "-1", "-z", f"--format={GIT_LOG_FORMAT}", ref)
    return _parse_commit_log(result.stdout)[0]


def list_commits(start: str, end: str) -> tuple[Commit, ...]:
    """Return the commits in the `start..end` range, oldest first.

    Follows git range semantics: *start* is excluded, *end* is included. Both
    endpoints must already share history locally, so a shallow clone goes
    through {func}`deepen_until_related` first.
    """
    result = _git(
        "log", "--reverse", "-z", f"--format={GIT_LOG_FORMAT}", f"{start}..{end}"
    )
    return _parse_commit_log(result.stdout)


def commit_exists(ref: str) -> bool:
    """Return `True` if *ref* resolves to a commit object present locally."""
    return _git("cat-file", "-e", f"{ref}^{{commit}}", check=False).returncode == 0


def head_sha() -> str:
    """Return the full SHA of the current `HEAD` commit."""
    return _git("rev-parse", "HEAD").stdout.strip()


def current_branch() -> str | None:
    """Return the checked-out branch name, or `None` when `HEAD` is detached."""
    result = _git("symbolic-ref", "--short", "--quiet", "HEAD", check=False)
    return result.stdout.strip() or None


def checkout(ref: str) -> None:
    """Check out *ref* (a branch name or commit SHA)."""
    _git("checkout", ref)


def discard_changes() -> None:
    """Put the index and the working tree back to `HEAD`.

    Staged changes go too, a file staged as new included: a commit that failed
    after its `git add` cannot hand its diff to whatever is committed next.
    Untracked files stay where they are.
    """
    _git("reset", "--hard", "HEAD")


def changed_paths() -> tuple[str, ...]:
    """Return the tracked paths whose index or working tree differs from `HEAD`.

    What {func}`discard_changes` would throw away: a staged change and an
    unstaged one both count, an added or a deleted file included, and a rename
    counts as its two paths. Untracked files do not, since no reset to `HEAD`
    touches them.

    :return: The paths, sorted. Empty on a clean tree.
    """
    output = _git(
        "status", "--porcelain", "-z", "--untracked-files=no", "--no-renames"
    ).stdout
    # Each entry is two status letters and a space, then the path.
    return tuple(sorted({entry[3:] for entry in output.split("\0") if entry}))


def show_file(ref: str, path: str) -> str | None:
    """Return the content of *path* at *ref*, read from the object store.

    Neither `HEAD` nor the working tree moves, so the read is safe in any
    checkout, uncommitted changes included.

    :param ref: Any tree-ish: a branch, a tag, or a commit SHA.
    :param path: File path, relative to the current directory.
    :return: The file content, or `None` when *ref* holds no such file.
    """
    shown = _git("show", f"{ref}:./{path}", check=False)
    if shown.returncode:
        logging.debug(f"{ref} holds no {path}: {shown.stderr.strip()}")
        return None
    return shown.stdout


def restore_paths(ref: str, paths: Sequence[str | Path]) -> tuple[str, ...]:
    """Overwrite *paths* in the working tree with the content they hold at *ref*.

    `HEAD` stays where it is, so the checkout keeps the source tree and lock
    file it was cloned with and only the named files travel. That is the whole
    point over a `git checkout` of the branch: a job reading a file back from
    an open pull request still runs the code of the branch it was called on.

    A path *ref* does not carry is skipped rather than deleted. The caller
    names the files it means to read, and one missing from *ref* is the
    first-run case, not an instruction to remove anything.

    :param ref: Any tree-ish: a branch, a tag, or a commit SHA.
    :param paths: Files to restore, absolute or relative to the current
        directory. One outside the working tree is skipped.
    :return: The paths actually restored, as repository-relative pathspecs.
    """
    root = Path(_git("rev-parse", "--show-toplevel").stdout.strip()).resolve()
    restorable = []
    for path in paths:
        try:
            pathspec = Path(path).resolve().relative_to(root).as_posix()
        except ValueError:
            logging.debug(f"{path} sits outside {root}, not restoring it.")
            continue
        if _git("cat-file", "-e", f"{ref}:{pathspec}", check=False).returncode:
            logging.debug(f"{ref} carries no {pathspec}, not restoring it.")
            continue
        restorable.append(pathspec)
    if restorable:
        _git("checkout", ref, "--", *restorable)
    return tuple(restorable)


def is_shallow() -> bool:
    """Return `True` when the repository is a shallow clone."""
    return _git("rev-parse", "--is-shallow-repository").stdout.strip() == "true"


def fetch_commit(sha: str, remote: str = "origin") -> bool:
    """Make the commit *sha* present locally, fetching it when it is absent.

    A shallow clone fetches the commit alone (`--depth=1`): its tree is all a
    comparison needs, and the history behind it can be large. A complete clone
    fetches without a depth, so it stays complete.

    :param sha: Full SHA of the commit.
    :param remote: Remote to fetch from.
    :return: `True` when the commit is present afterwards. `False` when the
        remote does not serve it, like a commit a force-push left unreachable.
    """
    if commit_exists(sha):
        return True
    depth = ("--depth=1",) if is_shallow() else ()
    fetched = _git("fetch", *depth, "--no-tags", remote, sha, check=False)
    if fetched.returncode:
        logging.debug(f"{remote} does not serve {sha}: {fetched.stderr.strip()}")
        return False
    return True


def deepen_until_related(
    start: str, end: str, *, attempts: int = 10, depth: int = 50
) -> bool:
    """Deepen a shallow clone until *start* and *end* share history.

    `start..end` resolves only once git can walk down from *end* to a commit
    that *start* also reaches. A shallow clone cuts that walk at its boundary,
    and a commit fetched on its own by {func}`fetch_commit` is a boundary of
    its own. So both commits being present is not enough: this deepens the
    clone until {func}`merge_base` finds the commit they share.

    :param start: The commit the range starts from.
    :param end: The commit the range ends at.
    :param attempts: Maximum number of deepening fetches.
    :param depth: Commits each fetch adds below the shallow boundary.
    :return: `True` once the two commits share history. `False` when they
        still do not after every fetch, or when the clone is complete and a
        fetch cannot add anything.
    :raises GitError: When a fetch fails.
    """
    for attempt in range(attempts + 1):
        if merge_base(start, end):
            if attempt:
                logging.info(f"{start} and {end} related after {attempt} fetch(es).")
            return True
        if attempt == attempts or not is_shallow():
            break
        logging.info(
            f"Deepening the clone by {depth} commits"
            f" (attempt {attempt + 1}/{attempts})."
        )
        _git("fetch", f"--deepen={depth}")
    logging.warning(f"{start} and {end} share no history in this clone.")
    return False


def diff_names(start: str, end: str) -> tuple[str, ...]:
    """Return the paths that differ between *start* and *end*.

    :raises GitError: if either ref is unknown.
    """
    output = _git("diff", "--name-only", start, end).stdout.strip()
    return tuple(output.splitlines()) if output else ()


def tree_sha(ref: str = "HEAD") -> str:
    """Return the SHA of the tree *ref* points at.

    Two commits sharing a tree SHA carry byte-identical content, whatever their
    message, author or parent. That makes this the cheapest way to ask whether
    re-running a generator produced anything new.
    """
    return _git("rev-parse", f"{ref}^{{tree}}").stdout.strip()


def count_commits_between(start: str, end: str) -> int:
    """Return the number of commits in the `start..end` range.

    Follows git range semantics: *start* is excluded, *end* is included.
    """
    return int(_git("rev-list", "--count", f"{start}..{end}").stdout.strip())


def is_ancestor(maybe_ancestor: str, ref: str) -> bool | None:
    """Return whether *maybe_ancestor* is reachable from *ref*.

    :return: `True` or `False` when git can relate the two commits, and `None`
        when it cannot: a shallow clone whose grafted history stops before
        their common ancestor answers neither yes nor no.
    """
    related = _git("merge-base", "--is-ancestor", maybe_ancestor, ref, check=False)
    if related.returncode in (0, 1):
        return related.returncode == 0
    logging.debug(f"Cannot relate {maybe_ancestor} to {ref}: {related.stderr.strip()}")
    return None


def merge_base(left: str, right: str) -> str | None:
    """Return the best common ancestor of two commits, or `None` if unrelated.

    `None` also covers the shallow-clone case described in {func}`is_ancestor`,
    and a commit absent from the clone.
    """
    common = _git("merge-base", left, right, check=False)
    return common.stdout.strip() if common.returncode == 0 else None


def rebase_onto(new_base: str, old_base: str, branch: str) -> bool:
    """Replay `old_base..branch` on top of *new_base*, keeping the replayed side.

    `--strategy-option=theirs` resolves overlaps in favour of the commits being
    replayed, which for a generated branch means the freshly generated content
    wins over whatever the new base happens to carry.

    :return: `True` on success. On conflict the rebase is aborted and `False`
        returned, leaving *branch* exactly as it was: a branch built on a
        slightly stale base still opens a usable pull request, and the next run
        converges it, so this is not worth failing the job over.
    """
    rebased = _git(
        *_BOT_IDENTITY_ARGS,
        "rebase",
        "--onto",
        new_base,
        old_base,
        branch,
        "--strategy-option=theirs",
        check=False,
    )
    if rebased.returncode:
        _git("rebase", "--abort", check=False)
        logging.warning(
            f"Could not replay {branch} onto {new_base}: {rebased.stderr.strip()}\n"
            "Publishing it on its original base instead."
        )
        return False
    return True


def create_branch(name: str) -> None:
    """Create or reset branch *name* at `HEAD` and switch to it.

    The index and working tree carry over untouched, so staged changes made
    before the call survive into a commit made after it.
    """
    _git("checkout", "-B", name)


def delete_branch(name: str) -> None:
    """Delete the local branch *name*, even when unmerged.

    Tolerates a branch that is not there. Callers reach this from a `finally`
    that cleans up a scratch branch, where the failure being cleaned up after
    may be the very thing that stopped the branch from being created: raising
    here would replace the real error with a confusing one.
    """
    deleted = _git("branch", "--delete", "--force", name, check=False)
    if deleted.returncode:
        logging.debug(f"Could not delete local branch {name}: {deleted.stderr.strip()}")


def stage_all(paths: Sequence[str] = ()) -> bool:
    """Stage working-tree changes, untracked files included.

    Stages the whole tree by default. *paths* narrows that to a git pathspec
    list, for a job whose own steps leave more behind than they mean to commit:
    a linter installed into the checkout, a lock file a package manager rewrote
    on the way past. Anything outside the pathspec stays dirty and is left for
    the caller to restore or discard.

    A pathspec matching nothing is dropped rather than fatal. `git add` exits
    128 on the first one it cannot resolve and stages nothing at all, so a
    glob covering output a run happened not to produce would take the whole job
    down with it. Filtering first also keeps one stale entry in a list from
    silently costing the others their staging.

    :param paths: Git pathspecs to stage. Empty stages everything.
    :return: `True` when the index ends up carrying something to commit.
    """
    if paths:
        matching = [path for path in paths if _pathspec_matches(path)]
        if matching:
            _git("add", "--all", "--", *matching)
    else:
        _git("add", "--all")
    return _git("diff", "--cached", "--quiet", check=False).returncode != 0


def _pathspec_matches(pathspec: str) -> bool:
    """Whether *pathspec* resolves to any tracked or untracked file.

    Both halves are needed: `--cached` covers a tracked file the run modified
    or deleted, `--others` an output file it created. A pathspec matching only
    ignored files reports `False`, which is the honest answer, since staging it
    would have added nothing either.
    """
    listed = _git(
        "ls-files", "--cached", "--others", "--exclude-standard", "--", pathspec
    )
    return bool(listed.stdout.strip())


def commit_staged(message: str) -> str:
    """Commit the staged tree as the CI bot and return the new commit SHA.

    The identity is supplied per-command via `-c`, since CI checkouts carry no
    git identity of their own.
    """
    _git(*_BOT_IDENTITY_ARGS, "commit", "--message", message)
    return head_sha()


def commit_tracked(message: str) -> str:
    """Commit every change to a tracked file as the CI bot.

    Stages what `git commit --all` takes: modified and deleted tracked files.
    An untracked file stays out of the commit, so nothing a tool leaves behind
    in the checkout can ride along.

    :param message: The commit message.
    :return: The SHA of the new commit.
    :raises GitError: When no tracked file changed, or when git cannot commit.
    """
    _git("add", "--update")
    return commit_staged(message)


def fetch_remote_branch(branch: str, remote: str = "origin") -> str | None:
    """Fetch *branch* into its remote-tracking ref and return the SHA.

    The refspec is explicit because `actions/checkout` configures a
    single-branch fetch refspec, under which a bare ``git fetch origin {branch}``
    updates `FETCH_HEAD` but leaves ``refs/remotes/{remote}/{branch}`` absent.
    Writing the remote-tracking ref is what later lets a push take a
    `--force-with-lease` on it.

    :return: The remote branch tip, or `None` when the branch does not exist
        on the remote.
    """
    fetched = _git(
        "fetch",
        "--force",
        remote,
        f"refs/heads/{branch}:refs/remotes/{remote}/{branch}",
        check=False,
    )
    if fetched.returncode:
        logging.debug(f"No {branch!r} branch on {remote}: {fetched.stderr.strip()}")
        return None
    return _git("rev-parse", f"refs/remotes/{remote}/{branch}").stdout.strip()


def force_push_branch(
    local_ref: str,
    branch: str,
    expected_sha: str | None,
    remote: str = "origin",
) -> None:
    """Publish *local_ref* as *branch* on *remote*, overwriting what is there.

    *expected_sha* is the remote tip the caller last observed, which becomes a
    `--force-with-lease` guard: the push is refused when the branch moved in
    between, rather than silently discarding the other writer's commit. Pass
    `None` to create a branch that does not exist yet, where a plain push
    already fails if someone wins the race.

    :raises GitError: When the push is rejected.
    """
    args = ["push"]
    if expected_sha:
        args.append(f"--force-with-lease={branch}:{expected_sha}")
    args.extend([remote, f"{local_ref}:refs/heads/{branch}"])
    _git(*args)
    logging.info(f"Pushed {local_ref} to {remote}/{branch}.")


def delete_remote_branch(branch: str, remote: str = "origin") -> None:
    """Delete *branch* from *remote*, tolerating a branch already gone."""
    deleted = _git("push", "--delete", remote, f"refs/heads/{branch}", check=False)
    if deleted.returncode:
        logging.warning(f"Could not delete {remote}/{branch}: {deleted.stderr.strip()}")
        return
    logging.info(f"Deleted {remote}/{branch}.")


def list_contributor_identities() -> set[str]:
    """Return every author and committer identity found in the history.

    No normalization happens: all variations of author and committer strings
    attached to all commits are returned as-is, in `Name <email>` form.

    For format output syntax, see:
    https://git-scm.com/docs/pretty-formats#Documentation/pretty-formats.txt-aN

    :raises GitError: When git cannot read the history.
    """
    process = _git("log", "--pretty=format:%aN <%aE>%n%cN <%cE>")
    return {line for line in map(str.strip, process.stdout.splitlines()) if line}


def get_repo_slug_from_remote(remote: str = "origin") -> str | None:
    """Extract the `owner/repo` slug from a git remote URL.

    Reads both HTTPS and SSH GitHub remotes, through
    {func}`~repomatic.github.urls.parse_github_repo`. Returns `None` if the
    remote is not set, not a GitHub URL, or git is unavailable.
    """
    try:
        result = _git("remote", "get-url", remote, check=False)
    except FileNotFoundError:
        return None
    if result.returncode:
        return None
    parsed = parse_github_repo(result.stdout.strip())
    return "/".join(parsed) if parsed else None


def _version_tags() -> dict[str, tuple[Version, str]]:
    """List the release tags, with the version each names and its date.

    The one `git tag` call behind {func}`get_latest_tag_version` and
    {func}`get_all_version_tags`. A tag {data}`VERSION_TAG_GLOB` selects but
    that names no PEP 440 version is skipped: one odd tag must not break every
    reader of the others.

    :return: `(version, date)` pairs keyed by tag name, the date in
        `YYYY-MM-DD` format.
    :raises GitError: When git cannot list the tags.
    """
    output = _git(
        "tag",
        "--list",
        VERSION_TAG_GLOB,
        "--format=%(refname:short) %(creatordate:short)",
    ).stdout
    tags = {}
    for line in output.splitlines():
        tag, _, date = line.partition(" ")
        version = parse_tag_version(tag)
        if version is None:
            logging.debug(f"Skipping non-PEP 440 tag: {tag}")
            continue
        tags[tag] = (version, date)
    return tags


def get_latest_tag_version() -> Version | None:
    """Return the highest release version among the `vX.Y.Z` tags.

    :return: The version, or `None` when no tag names one.
    :raises GitError: When git cannot list the tags.
    """
    versions = [version for version, _date in _version_tags().values()]
    if not versions:
        logging.debug("No version tags found in repository.")
        return None
    latest = max(versions)
    logging.debug(f"Latest tag version: {latest}")
    return latest


def get_release_version_from_commits(max_count: int = 10) -> Version | None:
    """Extract release version from recent commit messages.

    Searches recent commits for messages matching the pattern
    `[changelog] Release vX.Y.Z` and returns the version from the most recent match.

    This provides a fallback when tags haven't been pushed yet due to race conditions
    between workflows. The release commit message contains the version information
    before the tag is created.

    :param max_count: Maximum number of commits to search.
    :return: The version from the most recent release commit, or `None` if not found.
    """
    result = _git(
        "log", "-n", str(max_count), "-z", f"--format={GIT_LOG_FORMAT}", "HEAD"
    )
    for commit in _parse_commit_log(result.stdout):
        match = protocol.RELEASE_COMMIT_PATTERN.fullmatch(commit.msg)
        if match:
            version = Version(match.group("version"))
            logging.debug(f"Found release version {version} in commit {commit.hash}")
            return version

    logging.debug("No release commit found in recent history.")
    return None


def get_all_version_tags() -> dict[str, str]:
    """Map every release version to the date of its tag.

    :return: Version strings (without the `v` prefix) mapped to dates in
        `YYYY-MM-DD` format.
    :raises GitError: When git cannot list the tags.
    """
    return {
        tag.removeprefix("v"): date for tag, (_version, date) in _version_tags().items()
    }


def create_and_push_tag(
    tag: str,
    commit: str | None = None,
    push: bool = True,
    skip_existing: bool = True,
) -> bool:
    """Create and optionally push a Git tag.

    This function is idempotent: if the tag already exists and `skip_existing`
    is True, it returns False without failing. This allows safe re-runs of
    workflows that were interrupted after tag creation but before other steps.

    :param tag: The tag name to create.
    :param commit: The commit to tag. Defaults to HEAD.
    :param push: Whether to push the tag to the `origin` remote.
    :param skip_existing: If True, skip silently when tag exists.
        If False, raise an error. Defaults to True.
    :return: True if the tag was created, False if it already existed.
    :raises ValueError: If tag exists and skip_existing is False.
    :raises GitError: If Git operations fail.
    """
    ref = f"refs/tags/{tag}"
    if _git("show-ref", "--verify", "--quiet", ref, check=False).returncode == 0:
        if skip_existing:
            logging.info(f"Tag {tag!r} already exists, skipping.")
            return False
        msg = f"Tag {tag!r} already exists."
        raise ValueError(msg)

    _git("tag", tag, *((commit,) if commit else ()))
    logging.info(f"Created tag {tag!r}")

    if push:
        _git("push", "origin", ref)
        logging.info(f"Pushed tag {tag!r} to remote.")

    return True
