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

"""Pull-request lifecycle management for automated jobs.

{func}`upsert_pr` converges a bot branch and its pull request onto whatever the
working tree currently holds: it opens the PR when there is something to say,
refreshes it when the content moved, leaves it strictly alone when nothing
changed, and retires branch and PR together once the change evaporates. Every
`sync-*`, `update-*`, `format-*` and `fix-*` job funnels through it.

```{note} Why this is not `peter-evans/create-pull-request`

This module ports the algorithm of that action: see `docs/security.md` for the
third-party-action inventory it belongs to. Two properties make the port worth
the code.

The action's cleanup only fires when the action runs. Half these jobs sit
behind an `if:` gate, and a skipped step cannot delete its own branch. A
command that always runs and decides internally retires the branch on the same
code path that opens it.

The pieces are already here: {mod}`repomatic.git_ops` owns the git side,
{mod}`~repomatic.github.gh` the authenticated `gh` side, and
{mod}`~repomatic.github.pr_body` renders title, body and commit message. Only
the decision between them is new.
```

```{note} What a job may hand this

Any mix of the two ways a job produces output: uncommitted working-tree edits
(a formatter) and commits it made itself (a release freeze). Both survive, the
second as separate commits. The checkout may sit on the base branch or be
detached at a commit, as long as the base branch can be named, and the base may
have moved on since: the carried commits are replayed onto its fresh tip.
`tests/test_workflows.py` fails if a workflow reaches for
`peter-evans/create-pull-request` again.

The one input that needs restating rather than inferring is the action's
`add-paths`, ported as `add_paths`. Every job upstream writes only what it
means to publish, so the whole-tree default is right for all of them; a
downstream job that provisions its own tooling into the checkout (an `npm
install` of a linter, a package manager rewriting a lock file on the way past)
has to name its output, or the provisioning rides along into the pull request.
```
"""

from __future__ import annotations

import logging
from enum import StrEnum
from typing import NamedTuple
from uuid import uuid4

from .. import git_ops
from ..release.protocol import bump_branch
from .event import get_default_branch
from .gh import gh_api_pages, gh_json, parse_create_output, run_gh_command
from .pr_body import fit_github_body, temp_body_file
from .threads import ThreadKind, add_labels, run_unlocking

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path
    from typing import Any


STALE_PR_COMMENT = (
    "Closing automatically: this branch no longer differs from its base, so "
    "the change it carried has either landed or become moot."
)
"""Comment left on a pull request retired by {func}`upsert_pr`.

A retired pull request otherwise closes with no trace on the conversation of
*why* it closed.
"""

STALE_BUMP_PR_COMMENT = (
    "Closing stale {part} version-bump PR: `main` already advanced past the "
    "target version, so this branch no longer represents a valid bump."
)
"""Comment left on a version-bump pull request {func}`close_stale_bump_prs`
retires, with the version part to fill in."""

TEMP_BRANCH_PREFIX = "repomatic/pr-sync-"
"""Namespace for the throwaway local branch a sync builds its commit on.

Never pushed and deleted before the command returns. The candidate commit needs
somewhere to live that is not the checked-out base branch, so that a base left
untouched is what a failed run rolls back to.
"""

OPEN_PR_LIMIT = 10
"""Open pull requests {func}`list_open_prs_by_branch` reads at most.

A head branch carries one open pull request, so the bound only caps a listing
gone wrong.
"""


class PrOperation(StrEnum):
    """What a {func}`upsert_pr` call did, mirroring the action's own vocabulary."""

    CREATED = "created"
    """The branch was pushed and a new pull request opened."""

    UPDATED = "updated"
    """The branch was force-pushed and the existing pull request refreshed."""

    CLOSED = "closed"
    """The change evaporated: branch deleted and any open pull request closed."""

    NONE = "none"
    """Nothing to do: the branch already carried exactly this change."""


class PrSyncResult(NamedTuple):
    """Outcome of a {func}`upsert_pr` call."""

    operation: PrOperation
    """Which of the four branches of the algorithm ran."""

    branch: str
    """The head branch the call targeted."""

    number: int | None = None
    """Pull-request number, when one was created, updated or closed."""

    url: str = ""
    """Pull-request URL, populated only on creation."""


def list_open_prs_by_branch(branch: str) -> list[dict[str, Any]]:
    """List open pull requests whose head branch matches `branch`.

    :param branch: The head branch name to filter on.
    :return: List of PR dicts with `number` and `isDraft`. Empty if no open PR
        exists on `branch`.
    """
    prs: list[dict[str, Any]] = (
        gh_json([
            "pr",
            "list",
            "--state",
            "open",
            "--head",
            branch,
            "--limit",
            str(OPEN_PR_LIMIT),
            "--json",
            "number,isDraft",
        ])
        or []
    )
    return prs


def list_changed_files(number: int, repository: str = "") -> list[str]:
    """List the repository-relative paths a pull request changes.

    Reads the REST files endpoint, every page of it, rather than the diff,
    since a diff has to be transferred and parsed to recover names the API
    already hands over one field at a time.

    ```{caution}
    GitHub caps this endpoint at 3,000 files, silently. A pull request past
    that returns a truncated list, so a file-glob rule keyed on the tail of a
    very large diff can miss. Nothing here can lift the cap, and the labeller
    this feeds is a first-pass convenience, so the truncation is tolerated
    rather than reported.
    ```

    :param number: The pull request number.
    :param repository: Repository in `owner/name` form. Left to `gh`'s own
        resolution when empty.
    :return: Changed paths, in the order GitHub returns them.
    """
    endpoint = f"repos/{repository or '{owner}/{repo}'}/pulls/{number}/files"
    files = gh_api_pages(f"{endpoint}?per_page=100")
    return [str(entry["filename"]) for entry in files if entry.get("filename")]


def close_pr(number: int, comment: str) -> None:
    """Close a pull request with a comment, and delete its head branch.

    The close comment is refused on a locked conversation, so the write goes
    through {func}`~repomatic.github.threads.run_unlocking`: a hand-locked pull
    request would otherwise wedge {func}`upsert_pr`'s whole retire path.

    :param number: The PR number to close.
    :param comment: The comment to add when closing.
    """
    run_unlocking(
        ["pr", "close", str(number), "--comment", comment, "--delete-branch"],
        number,
        kind=ThreadKind.PR,
    )
    logging.info(f"Closed PR #{number}")


def close_open_prs_on_branch(branch: str, comment: str) -> list[int]:
    """Close every open PR whose head branch matches `branch`.

    Idempotent: a no-op when no open PR exists on the branch.

    :param branch: The head branch name to match.
    :param comment: The comment to add when closing each PR.
    :return: The list of PR numbers that were closed.
    """
    prs = list_open_prs_by_branch(branch)
    if not prs:
        logging.info(f"No open PR on branch {branch!r}, nothing to close.")
        return []
    closed: list[int] = []
    for pr in prs:
        close_pr(pr["number"], comment)
        closed.append(pr["number"])
    return closed


def close_stale_bump_prs(part: str) -> list[int]:
    """Close the pull request of a version bump that is no longer allowed.

    A scheduled bump that started before a competing push can open its pull
    request against a `main` that already advanced past the target, leaving an
    orphan that later runs cannot refresh. The caller decides the bump is no
    longer allowed, then this deletes the branch with its pull request.

    Idempotent: a no-op when no open pull request exists on the branch.

    :param part: The version part whose bump branch to retire.
    :return: The numbers of the pull requests closed.
    """
    return close_open_prs_on_branch(
        bump_branch(part), comment=STALE_BUMP_PR_COMMENT.format(part=part)
    )


def _needs_push(
    candidate_sha: str, remote_sha: str, base_sha: str, expected_commits: int
) -> bool:
    """Decide whether the remote branch has to be overwritten.

    Two independent reasons, ported from the four-condition check in the
    action's `createOrUpdateBranch`. The point of checking at all is that a
    force-push which changes nothing still re-triggers every workflow watching
    the branch, so the whole matrix would re-run on each pass of a job whose
    output never moved.

    The tree comparison is what normally answers this, and it also catches a
    base that advanced: rebuilding on a newer base yields a different tree even
    when the job's own edits are byte-identical. The commit count catches the
    structural cases a tree comparison cannot see, where the branch holds a
    different number of commits than the candidate does: a human's fixup
    pushed onto a bot branch, or a release freeze that grew a commit.

    :param candidate_sha: The commit just built for this branch.
    :param remote_sha: The current tip of the remote branch.
    :param base_sha: The commit both are measured against.
    :param expected_commits: How many commits the candidate carries over base.
    """
    if git_ops.tree_sha(candidate_sha) != git_ops.tree_sha(remote_sha):
        logging.info("Remote branch carries a different tree, updating.")
        return True
    if git_ops.count_commits_between(base_sha, remote_sha) != expected_commits:
        logging.info(
            f"Remote branch is not {expected_commits} commit(s) on base, updating."
        )
        return True
    logging.info("Remote branch already carries this exact change.")
    return False


def _mark_draft(number: int) -> None:
    """Put a pull request back into draft, tolerating a refusal.

    Called on every update, not just on creation, which is the `always-true`
    semantics of the action's `draft` input: a bot branch that a maintainer
    marked ready-for-review is put back into draft on the next sync, so a
    version-bump PR cannot drift into looking mergeable while its content is
    still being regenerated underneath it.
    """
    try:
        run_gh_command(["pr", "ready", str(number), "--undo"])
    except RuntimeError as error:
        logging.warning(f"Could not put PR #{number} back into draft: {error}")


def carry_pr_branch_paths(
    branch: str,
    paths: Sequence[str | Path],
    remote: str = "origin",
) -> tuple[str, ...]:
    """Restore *paths* from an open pull request's branch, before rebuilding them.

    The counterpart of {func}`upsert_pr` for a job whose output *accrues*
    rather than converges. A convergent job rebuilds its file from scratch,
    so starting from the base branch loses nothing. An accruing one appends
    to what it wrote last time, and starting from the base would publish a
    branch holding one entry where the history needs all of them.

    Reading the file back from the branch first makes each run append to what
    is already pending, so the pull request always shows the whole accrual as
    one diff against its base, however many runs went into it.

    Only what accrues has to be carried. Anything the job derives from it is
    rebuilt from the restored file and lands on the same bytes the branch
    already holds, so passing a derived path here buys nothing.

    ```{note}
    `HEAD` never moves: only the named files travel. A job carrying its store
    still runs the code and lock file of the branch it was called on, which a
    `git checkout` of the pull request branch would silently replace with
    whatever that branch was built from.
    ```

    Idempotent and self-healing, whichever way the pull request was merged.
    Once merged, the branch's copy and the base's agree, so the restore
    changes nothing, {func}`upsert_pr` finds no diff, and it closes the pull
    request and deletes the branch. The next run starts a fresh accrual. A
    squash merge is no different, because every comparison here is of file
    content and never of commit history.

    :param branch: Remote branch holding the pending work, by convention the
        one the job's own pull request is opened from.
    :param paths: Files to restore. One the branch does not carry is skipped,
        which is every run that follows a merge.
    :param remote: Remote to read the branch from.
    :return: The paths actually restored, as repository-relative pathspecs.
    """
    tip = git_ops.fetch_remote_branch(branch, remote=remote)
    if tip is None:
        logging.debug(f"No {branch!r} branch on {remote}, nothing pending to carry.")
        return ()
    restored = git_ops.restore_paths(tip, paths)
    if not restored:
        logging.debug(f"{remote}/{branch} carries none of {paths}, nothing to carry.")
        return ()
    logging.info(f"Carried {', '.join(restored)} pending on {remote}/{branch}.")
    return restored


class _Candidate(NamedTuple):
    """The commits {func}`_build_candidate` stacked for a pull request."""

    carried: int
    """Commits the candidate carries over the base. `0` means nothing to say."""

    pushed: bool
    """Whether the remote branch was overwritten with the candidate."""


def _build_candidate(
    branch: str,
    base_sha: str,
    remote_sha: str | None,
    commit_message: str,
    add_paths: Sequence[str],
    remote: str,
) -> _Candidate:
    """Build the candidate branch on a throwaway branch, and push it if it moved.

    The original checkout is restored before returning, whatever happens: the
    `sync-deps` job of `autofix.yaml` opens four pull requests in sequence from
    one checkout and needs each to start from a clean tree.

    :param branch: Head branch the candidate is pushed to.
    :param base_sha: The tip of the base branch on the remote.
    :param remote_sha: The tip of *branch* on the remote, `None` when absent.
    :param commit_message: Message for the commit capturing uncommitted changes.
    :param add_paths: Git pathspecs limiting what that commit picks up.
    :param remote: Remote to publish to.
    :return: How many commits the candidate carries, and whether it was pushed.
    """
    restore_ref = git_ops.current_branch() or git_ops.head_sha()
    temp_branch = f"{TEMP_BRANCH_PREFIX}{uuid4().hex[:12]}"
    carried = 0
    pushed = False
    try:
        git_ops.create_branch(temp_branch)
        if git_ops.stage_all(add_paths):
            git_ops.commit_staged(commit_message)
        carried = git_ops.count_commits_between(base_sha, temp_branch)

        if carried and git_ops.is_ancestor(base_sha, temp_branch) is False:
            # The base moved past this checkout. Replay onto its fresh tip so
            # the pull request shows only what this job produced.
            fork_point = git_ops.merge_base(base_sha, temp_branch)
            if fork_point and git_ops.rebase_onto(base_sha, fork_point, temp_branch):
                carried = git_ops.count_commits_between(base_sha, temp_branch)

        if carried:
            candidate_sha = git_ops.head_sha()
            if remote_sha is None or _needs_push(
                candidate_sha, remote_sha, base_sha, carried
            ):
                git_ops.force_push_branch(
                    temp_branch, branch, expected_sha=remote_sha, remote=remote
                )
                pushed = True
    finally:
        git_ops.checkout(restore_ref)
        git_ops.delete_branch(temp_branch)
    return _Candidate(carried, pushed)


def _publish_pr(
    branch: str,
    base_branch: str,
    title: str,
    body: str,
    labels: Sequence[str],
    assignees: Sequence[str],
    draft: bool,
) -> PrSyncResult:
    """Refresh the open pull request of *branch*, or open one.

    :param branch: Head branch, already pushed.
    :param base_branch: Branch to open the pull request against.
    :param title: Pull-request title.
    :param body: Rendered markdown body, trimmed here if oversized.
    :param labels: Labels to attach, best-effort.
    :param assignees: Assignees to attach, best-effort.
    :param draft: Hold the pull request in draft.
    :return: The created or updated pull request.
    """
    open_prs = list_open_prs_by_branch(branch)
    fitted = fit_github_body(body)
    if fitted != body:
        logging.warning("PR body exceeds GitHub's size limit, trimming.")

    with temp_body_file(fitted) as body_path:
        if open_prs:
            number = open_prs[0]["number"]
            run_gh_command([
                "pr",
                "edit",
                str(number),
                "--title",
                title,
                "--body-file",
                str(body_path),
            ])
            logging.info(f"Updated PR #{number}")
            add_labels(number, labels, kind=ThreadKind.PR, assignees=assignees)
            if draft and not open_prs[0].get("isDraft"):
                _mark_draft(number)
            return PrSyncResult(PrOperation.UPDATED, branch, number=number)

        args = [
            "pr",
            "create",
            "--head",
            branch,
            "--base",
            base_branch,
            "--title",
            title,
            "--body-file",
            str(body_path),
        ]
        if draft:
            args.append("--draft")
        output = run_gh_command(args)

    number, url = parse_create_output(output, "pr")
    logging.info(f"Created PR #{number}: {url}")
    # A second call rather than flags on `gh pr create`: a refused label or
    # assignee then degrades to a warning instead of losing the pull request.
    add_labels(number, labels, kind=ThreadKind.PR, assignees=assignees)
    return PrSyncResult(PrOperation.CREATED, branch, number=number, url=url)


def upsert_pr(
    branch: str,
    title: str,
    body: str,
    commit_message: str,
    base: str | None = None,
    labels: Sequence[str] = (),
    assignees: Sequence[str] = (),
    draft: bool = False,
    add_paths: Sequence[str] = (),
    remote: str = "origin",
) -> PrSyncResult:
    """Converge *branch* and its pull request onto what the checkout now holds.

    Idempotent by construction: a second call over an unchanged checkout
    performs no write at all and reports {attr}`PrOperation.NONE`. The four
    outcomes are those of {class}`PrOperation`.

    The candidate branch is whatever this checkout has that the base does not:
    commits the job made itself are carried through as separate commits, and
    any uncommitted working-tree change becomes one more commit on top, so a
    job that commits (a release freeze) and a job that only edits files (a
    formatter) both work without saying which they are. The base commit is read
    once from the remote, so a detached `HEAD` is fine as long as the base
    branch can be named. When the base has moved past this checkout, the
    carried commits are replayed onto its fresh tip.

    :param branch: Head branch to create, update or retire.
    :param title: Pull-request title.
    :param body: Rendered markdown body, trimmed here if oversized.
    :param commit_message: Message for the commit capturing uncommitted
        changes. Unused when the job committed its own work.
    :param base: Base branch. Defaults to the checked-out branch, then, on a
        detached `HEAD`, to the repository default branch the CI event payload
        names.
    :param labels: Labels to attach, best-effort.
    :param assignees: Assignees to attach, best-effort.
    :param draft: Hold the pull request in draft, on every update and not just
        at creation. See `_mark_draft`.
    :param add_paths: Git pathspecs limiting what the uncommitted-changes
        commit picks up. Empty commits the whole tree, which is right for a job
        whose only writes are the ones it means to publish. A job that also
        provisions its own tooling into the checkout needs the narrower form:
        anything outside the pathspec is left dirty and discarded with the
        throwaway branch.
    :param remote: Remote to publish to.
    :raises RuntimeError: When no base branch can be named, or when the base
        does not exist on *remote*.
    """
    base_branch = base or git_ops.current_branch() or get_default_branch()
    if not base_branch:
        msg = (
            "`HEAD` is detached and no base branch was given. Pass `base` "
            "explicitly, or check out a branch before syncing a pull request."
        )
        raise RuntimeError(msg)
    base_sha = git_ops.fetch_remote_branch(base_branch, remote=remote)
    if base_sha is None:
        msg = f"Base branch {base_branch!r} does not exist on {remote}."
        raise RuntimeError(msg)
    remote_sha = git_ops.fetch_remote_branch(branch, remote=remote)

    candidate = _build_candidate(
        branch, base_sha, remote_sha, commit_message, add_paths, remote
    )

    if not candidate.carried:
        # Nothing to say any more, so the branch and its pull request go: the
        # path a job skipped behind an `if:` gate can never reach.
        if remote_sha is None:
            logging.info(f"No changes and no {branch!r} branch: nothing to do.")
            return PrSyncResult(PrOperation.NONE, branch)
        closed = close_open_prs_on_branch(branch, STALE_PR_COMMENT)
        if not closed:
            # An orphan branch with no open PR: `gh pr close --delete-branch`
            # never ran, so the branch needs deleting on its own.
            git_ops.delete_remote_branch(branch, remote=remote)
        return PrSyncResult(
            PrOperation.CLOSED, branch, number=closed[0] if closed else None
        )

    if not candidate.pushed:
        return PrSyncResult(PrOperation.NONE, branch)

    return _publish_pr(branch, base_branch, title, body, labels, assignees, draft)
