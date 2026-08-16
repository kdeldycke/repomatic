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
timing guarantees (e.g. `workflow_run` ensures tags exist) with idempotent
guards (e.g. `skip_existing` on tag creation). This ensures correctness
in the face of race conditions, API eventual consistency, and partial failures
that are common in GitHub Actions.

```{warning} Tag push requires `REPOMATIC_PAT`

Tags pushed with the default `GITHUB_TOKEN` do not trigger downstream
`on.push.tags` workflows. The custom PAT is required so that tagging
a release commit actually fires the publish and release creation jobs.
```
"""

from __future__ import annotations

import logging
import re
import subprocess
from typing import NamedTuple

from packaging.version import InvalidVersion, Version

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

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

GITHUB_REMOTE_PATTERN = re.compile(r"github\.com[:/](?P<slug>[^/]+/[^/]+?)(?:\.git)?$")
"""Extracts an `owner/repo` slug from a GitHub remote URL.

Handles both HTTPS (`https://github.com/owner/repo.git`) and SSH
(`git@github.com:owner/repo.git`) formats.
"""

CHANGELOG_COMMIT_PREFIX = "[changelog] "
"""Marker prefix carried by every machine-authored version-machinery commit.

The one bracketed prefix commit messages may carry (see `claude.md` § Commit
messages): release freezes, post-release bumps and manual version bumps all
start with it, so a workflow can skip machinery pushes with a single
`startsWith(github.event.head_commit.message, '[changelog] ')` clause instead
of enumerating each message shape. Conformance tests in
`tests/test_workflows.py` hold every member of
{data}`VERSION_BUMP_COMMIT_PREFIXES`, {data}`RELEASE_COMMIT_PATTERN`, the
`bump-version` template title, and the workflow gates to this prefix.
"""

RELEASE_COMMIT_PREFIX = f"{CHANGELOG_COMMIT_PREFIX}Release"
"""Head-commit-message prefix marking a push that carries the release commit.

The coarser sibling of {data}`RELEASE_COMMIT_PATTERN`: where that one
validates and extracts a version, this is the prefix test every workflow's
`cancel-in-progress` gate performs, so a release run is never cancelled by a
later push entering its concurrency group. A prefix is deliberately weaker
than the full pattern here, because the question is "does this push carry a
release" rather than "which version is it", and answering it must not depend
on the version number parsing.

{func}`repomatic.github.actions.cancel_superseded_runs` applies the same test
from the API side, which is the half GitHub's own concurrency mechanism
cannot cover: a manual sweep of a branch's live runs enters no concurrency
group at all.
"""

RELEASE_COMMIT_PATTERN = re.compile(
    r"^\[changelog\] Release v(?P<version>[0-9]+\.[0-9]+\.[0-9]+)$"
)
"""Pre-compiled regex for release commit messages.

Matches the full message and captures the version number. Use `fullmatch`
to validate a commit is a release commit, or `match`/`search` with
`.group("version")` to extract the version string.

A rebase merge preserves the original commit messages, so release commits
match this pattern. A squash merge replaces them with the PR title
(e.g. ``Release `v1.2.3` (#42)``), which does **not** match. This mismatch
is the mechanism by which squash merges are safely skipped: the `create-tag`
job only processes commits matching this pattern, so no tag, PyPI publish, or
GitHub release is created from a squash merge. The `detect-squash-merge`
job in `release.yaml` detects this and opens an issue to notify the
maintainer.
"""

VERSION_BUMP_BRANCHES: frozenset[str] = frozenset((
    "major-version-increment",
    "minor-version-increment",
    "prepare-release",
))
"""PR branches that carry only automated version-bump and lockfile churn.

Members are bot-authored draft PRs created by the `bump-version` and
`prepare-release` jobs in `changelog.yaml`. Their working tree is
byte-identical to `main` except for the version string in
`pyproject.toml`, `**/__init__.py`, `changelog.md`, `citation.cff`,
and `uv.lock`. Heavy PR-time workflows (`tests.yaml`, `lint.yaml`,
`labels.yaml`) list these branches under `pull_request.branches-ignore`
so the matrix doesn't burn CI minutes for a guaranteed-passing run.

```{note}
These branches are *not* binary-neutral: the rewritten version string
is baked into the Nuitka binary, so they are deliberately absent from
{data}`repomatic.binary.SKIP_BINARY_BUILD_BRANCHES`. Post-merge release
artifacts on `main` are still produced.
```
"""


MANUAL_VERSION_BUMP_COMMIT_PREFIXES: frozenset[str] = frozenset((
    f"{CHANGELOG_COMMIT_PREFIX}Bump major version to ",
    f"{CHANGELOG_COMMIT_PREFIX}Bump minor version to ",
))
"""Head-commit-message prefixes for user-initiated version bumps.

Members are the `bump-version` job's
`[changelog] Bump $part version to \\`v$version\\`` commit messages (rendered
from the `bump-version` template's title), carrying
{data}`CHANGELOG_COMMIT_PREFIX` like every other version-machinery commit.
These merges land as a single commit on `main` and carry no other payload, so
workflows can short-circuit on them safely.

The release-cycle prefix `[changelog] Post-release bump ` is deliberately
absent from this set because the `prepare-release` merge bundles the
post-release-bump commit with the actual release commit
(`[changelog] Release vX.Y.Z`) in a single push. Workflows that gate on
the *head* commit message (`tests.yaml`, `release.yaml::compile-binaries`)
must run on those pushes to test the release commit and build its binary
— so they consult only this subset.
"""


VERSION_BUMP_COMMIT_PREFIXES: frozenset[str] = (
    MANUAL_VERSION_BUMP_COMMIT_PREFIXES
    | frozenset({f"{CHANGELOG_COMMIT_PREFIX}Post-release bump "})
)
"""Full set of head-commit-message prefixes that mark a version-bump push.

Combines {data}`MANUAL_VERSION_BUMP_COMMIT_PREFIXES` with the
`[changelog] Post-release bump ` prefix produced by `prepare-release`
merges. Every member starts with {data}`CHANGELOG_COMMIT_PREFIX`, so
workflows without a release-artifact dependency (`lint.yaml`,
`labels.yaml`, `autofix.yaml`) gate their `metadata` job on that single
prefix and the entire job graph skips for any push generated by the
version-bump PR family. Workflows that *do* produce release artifacts on
the same push use {data}`MANUAL_VERSION_BUMP_COMMIT_PREFIXES` instead.
"""


GIT_LOG_FORMAT = "%H%x00%B"
"""`git log` pretty-format placeholders for a single commit: full SHA, then a
`NUL`, then the raw body.

Paired with `git log -z` (which terminates each commit's output with a `NUL`),
this frames the stream as alternating `(hash, message)` tokens. Commit messages
may contain newlines but never `NUL` bytes, so splitting on `NUL` recovers the
fields unambiguously even for multi-line messages.
"""


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

    A failure is logged before the exception propagates: `CalledProcessError`'s
    default traceback prints only the command and exit code, never the
    captured `stderr`, which otherwise strands a CI failure with no way to
    tell a lease rejection from a permission error from a rate limit.
    """
    process = subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        encoding="UTF-8",
        check=False,
    )
    if check and process.returncode:
        logging.error(f"git {' '.join(args)} failed:\n{process.stderr.strip()}")
        process.check_returncode()
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

    :raises subprocess.CalledProcessError: if *ref* does not resolve to a
        commit present in the repository.
    """
    result = _git("log", "-1", "-z", f"--format={GIT_LOG_FORMAT}", ref)
    return _parse_commit_log(result.stdout)[0]


def list_commits(start: str, end: str) -> tuple[Commit, ...]:
    """Return the commits in the `start..end` range, oldest first.

    Follows git range semantics: *start* is excluded, *end* is included. Both
    endpoints must already exist locally, so deepen a shallow clone before
    calling if necessary.
    """
    result = _git(
        "log", "--reverse", "-z", f"--format={GIT_LOG_FORMAT}", f"{start}..{end}"
    )
    return _parse_commit_log(result.stdout)


def commit_exists(ref: str) -> bool:
    """Return `True` if *ref* resolves to a commit object present locally."""
    return _git("cat-file", "-e", f"{ref}^{{commit}}", check=False).returncode == 0


def count_commits(ref: str = "HEAD") -> int:
    """Return the number of commits reachable from *ref*."""
    return int(_git("rev-list", "--count", ref).stdout.strip())


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


def stash() -> None:
    """Stash the working tree's local changes."""
    _git("stash")


def stash_pop() -> None:
    """Restore the most recently stashed local changes."""
    _git("stash", "pop")


def stash_count() -> int:
    """Return the number of entries on the stash reflog."""
    result = _git(
        "rev-list", "--walk-reflogs", "--ignore-missing", "--count", "refs/stash"
    )
    return int(result.stdout.strip())


def fetch_deepen(depth: int) -> None:
    """Deepen a shallow clone by fetching *depth* more commits.

    :raises subprocess.CalledProcessError: if the fetch fails.
    """
    _git("fetch", f"--deepen={depth}")


def diff_names(start: str, end: str) -> tuple[str, ...]:
    """Return the paths that differ between *start* and *end*.

    :raises subprocess.CalledProcessError: if either ref is unknown.
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
        when it cannot — a shallow clone whose grafted history stops before
        their common ancestor answers neither yes nor no.
    """
    related = _git("merge-base", "--is-ancestor", maybe_ancestor, ref, check=False)
    if related.returncode in (0, 1):
        return related.returncode == 0
    logging.debug(f"Cannot relate {maybe_ancestor} to {ref}: {related.stderr.strip()}")
    return None


def merge_base(left: str, right: str) -> str | None:
    """Return the best common ancestor of two commits, or `None` if unrelated.

    `None` also covers the shallow-clone case described in {func}`is_ancestor`.
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
    git identity of their own. Mirrors {func}`commit_and_push_files`, which
    does the same for the direct-to-default-branch case.
    """
    _git(*_BOT_IDENTITY_ARGS, "commit", "--message", message)
    return head_sha()


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

    :raises subprocess.CalledProcessError: When the push is rejected.
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

    :raises RuntimeError: When git fails, carrying its stderr.
    """
    process = _git("log", "--pretty=format:%aN <%aE>%n%cN <%cE>", check=False)
    if process.returncode:
        raise RuntimeError(process.stderr)
    return {line for line in map(str.strip, process.stdout.splitlines()) if line}


def get_repo_slug_from_remote(remote: str = "origin") -> str | None:
    """Extract the `owner/repo` slug from a git remote URL.

    Parses both HTTPS and SSH GitHub remote formats. Returns `None` if the
    remote is not set, not a GitHub URL, or git is unavailable.
    """
    try:
        result = _git("remote", "get-url", remote, check=False)
    except FileNotFoundError:
        return None
    if result.returncode:
        return None
    match = GITHUB_REMOTE_PATTERN.search(result.stdout.strip())
    return match.group("slug") if match else None


def get_latest_tag_version() -> Version | None:
    """Returns the latest release version from Git tags.

    Looks for tags matching the pattern `vX.Y.Z` and returns the highest version.
    Returns `None` if no matching tags are found.
    """
    # Get all tags matching the version pattern.
    tags = _git("tag", "--list", "v[0-9]*.[0-9]*.[0-9]*").stdout.splitlines()

    if not tags:
        logging.debug("No version tags found in repository.")
        return None

    # Parse and find the highest version.
    versions = []
    for tag in tags:
        # The `--list` glob above is a shell pattern, not a PEP 440 filter, so
        # a tag like `v1.2.3_hotfix` still matches: skip what the parser
        # refuses rather than letting one odd tag break every metadata
        # consumer (mirrors `releases.parse_release_version`).
        try:
            versions.append(Version(tag.removeprefix("v")))
        except InvalidVersion:
            logging.debug(f"Skipping non-PEP 440 tag: {tag}")

    if not versions:
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
    if max_count <= 0:
        return None

    result = _git(
        "log", "-n", str(max_count), "-z", f"--format={GIT_LOG_FORMAT}", "HEAD"
    )
    for commit in _parse_commit_log(result.stdout):
        match = RELEASE_COMMIT_PATTERN.fullmatch(commit.msg)
        if match:
            version = Version(match.group("version"))
            logging.debug(f"Found release version {version} in commit {commit.hash}")
            return version

    logging.debug("No release commit found in recent history.")
    return None


def get_all_version_tags() -> dict[str, str]:
    """Get all version tags and their dates.

    Runs a single `git tag` command to list all tags matching the
    `vX.Y.Z` pattern and extracts their dates.

    :return: Dict mapping version strings (without `v` prefix) to
        dates in `YYYY-MM-DD` format.
    """
    result = _git(
        "tag",
        "-l",
        "v[0-9]*.[0-9]*.[0-9]*",
        "--format=%(refname:short) %(creatordate:short)",
        check=False,
    )
    tags: dict[str, str] = {}
    for line in result.stdout.strip().splitlines():
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) == 2:
            tag, date = parts
            if tag.startswith("v"):
                tags[tag[1:]] = date
    return tags


def tag_exists(tag: str) -> bool:
    """Check if a Git tag already exists locally.

    :param tag: The tag name to check.
    :return: True if the tag exists, False otherwise.
    """
    return _git("show-ref", "--tags", tag, "--quiet", check=False).returncode == 0


def create_tag(tag: str, commit: str | None = None) -> None:
    """Create a local Git tag.

    :param tag: The tag name to create.
    :param commit: The commit to tag. Defaults to HEAD.
    :raises subprocess.CalledProcessError: If tag creation fails.
    """
    args = ["tag", tag]
    if commit:
        args.append(commit)
    logging.debug(f"Creating tag: git {' '.join(args)}")
    _git(*args)


def push_tag(tag: str, remote: str = "origin") -> None:
    """Push a Git tag to a remote repository.

    :param tag: The tag name to push.
    :param remote: The remote name. Defaults to "origin".
    :raises subprocess.CalledProcessError: If push fails.
    """
    logging.debug(f"Pushing tag: git push {remote} {tag}")
    _git("push", remote, tag)


def commit_and_push_files(
    paths: Sequence[Path | str],
    message: str,
    remote: str = "origin",
    branch: str = "main",
    attempts: int = 3,
    all_changes: bool = False,
) -> bool:
    """Commit the given files and push, rebasing and retrying on rejection.

    Designed for CI jobs that append to tracked files (scan records, the
    binaries page) and publish the result on the default branch. The commit
    is authored as {data}`COMMIT_IDENTITY_NAME` via per-command `-c` config,
    since CI checkouts carry no git identity.

    Idempotent: when the files are unchanged, no commit is created and the
    function returns `False`. A rejected push (another job or the maintainer
    pushed meanwhile) is retried after fetching and rebasing onto the fresh
    remote tip. Works from a detached `HEAD`: the push targets
    ``HEAD:{branch}`` explicitly.

    :param paths: Files to stage and commit. Ignored when *all_changes* is set.
    :param message: Commit message.
    :param remote: Remote to push to.
    :param branch: Remote branch to push to.
    :param attempts: Maximum push attempts before giving up.
    :param all_changes: Stage every change in the working tree instead of the
        named files. For a job whose output paths come from configuration and
        are therefore unknown to the workflow that runs it: the runner starts
        from a pristine checkout and the preceding steps are the only writers,
        so "everything that changed" is exactly the job's own output. Never
        reach for it in a job that also runs a formatter or an installer.
    :return: `True` when a commit was pushed, `False` when there was nothing
        to commit.
    :raises RuntimeError: When the rebase hits a conflict (the local change
        overlaps a concurrent push) or every push attempt is rejected.
    :raises subprocess.CalledProcessError: When a git command fails outright.
    """
    if all_changes:
        _git("add", "--all")
    else:
        _git("add", "--", *(str(path) for path in paths))
    if _git("diff", "--cached", "--quiet", check=False).returncode == 0:
        logging.info("No changes to commit.")
        return False

    _git(*_BOT_IDENTITY_ARGS, "commit", "--message", message)
    logging.info(f"Committed: {message}")

    for attempt in range(1, attempts + 1):
        push = _git("push", remote, f"HEAD:{branch}", check=False)
        if push.returncode == 0:
            logging.info(f"Pushed to {remote}/{branch}.")
            return True
        logging.warning(
            f"Push attempt {attempt}/{attempts} rejected: "
            f"{push.stderr.strip()}\nRebasing onto fresh {remote}/{branch}."
        )
        _git("fetch", remote, branch)
        # Replaying the commit needs a committer identity too.
        rebase = _git(*_BOT_IDENTITY_ARGS, "rebase", "FETCH_HEAD", check=False)
        if rebase.returncode:
            _git("rebase", "--abort", check=False)
            raise RuntimeError(
                f"Rebase onto {remote}/{branch} conflicted, aborted. "
                "A concurrent push touched the same files; re-run the job "
                "once it settles."
            )
    raise RuntimeError(f"Push to {remote}/{branch} failed after {attempts} attempts.")


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
    :param push: Whether to push the tag to the remote. Defaults to True.
    :param skip_existing: If True, skip silently when tag exists.
        If False, raise an error. Defaults to True.
    :return: True if the tag was created, False if it already existed.
    :raises ValueError: If tag exists and skip_existing is False.
    :raises subprocess.CalledProcessError: If Git operations fail.
    """
    if tag_exists(tag):
        if skip_existing:
            logging.info(f"Tag {tag!r} already exists, skipping.")
            return False
        msg = f"Tag {tag!r} already exists."
        raise ValueError(msg)

    create_tag(tag, commit)
    logging.info(f"Created tag {tag!r}")

    if push:
        push_tag(tag)
        logging.info(f"Pushed tag {tag!r} to remote.")

    return True
