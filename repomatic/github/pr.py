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

That action did this job for years, and this module is a deliberate port of its
algorithm rather than a fresh design: see `docs/security.md` for the
third-party-action inventory it belongs to. Two properties made porting it
worth the code.

The first is that the action's cleanup only fires when the action runs. Half
these jobs sit behind an `if:` gate, and a skipped step cannot delete its own
branch, which is why {func}`close_open_prs_on_branch` had to exist as a
separate hand-written reconciler. A command that always runs and decides
internally folds that back into one code path.

The second is that the pieces were already here: {mod}`repomatic.git_ops` owns
the git side, {mod}`~repomatic.github.gh` the authenticated `gh` side, and
{mod}`~repomatic.github.pr_body` renders title, body and commit message. Only
the decision between them lived in YAML.
```

```{note} What a job may hand this

Any mix of the two ways a job produces output: uncommitted working-tree edits
(a formatter) and commits it made itself (a release freeze). Both survive, the
second as separate commits. The checkout may sit on the base branch or be
detached at a commit, as long as `base` names the branch to open against, and
the base may have moved on since — the carried commits are replayed onto its
fresh tip. Nothing here is left for `peter-evans/create-pull-request`, and
`tests/test_workflows.py` fails if a workflow reaches for it again.

The one input that needs restating rather than inferring is the action's
`add-paths`, ported as `add_paths`. Every job upstream writes only what it
means to publish, so the whole-tree default is right for all of them; a
downstream job that provisions its own tooling into the checkout (an `npm
install` of a linter, a package manager rewriting a lock file on the way past)
has to name its output, or the provisioning rides along into the pull request.
```
"""

from __future__ import annotations

import json
import logging
from typing import NamedTuple
from uuid import uuid4

from .. import git_ops
from ..compat import StrEnum
from .gh import parse_create_output, run_gh_command
from .issue import run_unlocking
from .pr_body import fit_github_body, temp_body_file

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

Stands in for the silent close `peter-evans/create-pull-request` performed by
deleting the head branch out from under the PR, which left no trace of *why* on
the conversation.
"""

TEMP_BRANCH_PREFIX = "repomatic/pr-sync-"
"""Namespace for the throwaway local branch a sync builds its commit on.

Never pushed and deleted before the command returns. The candidate commit needs
somewhere to live that is not the checked-out base branch, so that a base left
untouched is what a failed run rolls back to.
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
    output = run_gh_command([
        "pr",
        "list",
        "--state",
        "open",
        "--head",
        branch,
        "--json",
        "number,isDraft",
    ])
    prs: list[dict[str, Any]] = json.loads(output)
    return prs


def list_changed_files(number: int, repository: str = "") -> list[str]:
    """List the repository-relative paths a pull request changes.

    Reads the REST files endpoint with `--paginate` rather than the diff, since
    a diff has to be transferred and parsed to recover names the API already
    hands over one field at a time.

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
    args = [
        "api",
        f"repos/{repository or '{owner}/{repo}'}/pulls/{number}/files",
        "--paginate",
        "--jq",
        ".[].filename",
    ]
    output = run_gh_command(args)
    return [line.strip() for line in output.splitlines() if line.strip()]


def close_pr(number: int, comment: str, delete_branch: bool = True) -> None:
    """Close a pull request with a comment.

    The close comment is refused on a locked conversation, so the write goes
    through {func}`~repomatic.github.issue.run_unlocking`: a hand-locked pull
    request would otherwise wedge {func}`upsert_pr`'s whole retire path.

    :param number: The PR number to close.
    :param comment: The comment to add when closing.
    :param delete_branch: When `True`, also delete the head branch.
    """
    args = [
        "pr",
        "close",
        str(number),
        "--comment",
        comment,
    ]
    if delete_branch:
        args.append("--delete-branch")
    run_unlocking(args, number, is_pr=True)
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


def _apply_pr_attributes(
    number: int,
    labels: Sequence[str] = (),
    assignees: Sequence[str] = (),
) -> None:
    """Attach labels and assignees to a pull request, tolerating refusals.

    Deliberately a second call rather than flags on `gh pr create`, and
    deliberately non-fatal. `github.actor` is the natural assignee and is
    `github-actions[bot]` whenever the bot pushed the triggering commit, which
    GitHub refuses to assign; a label renamed in `repomatic/data/labels.toml`
    but not yet synced is refused the same way. Neither is worth losing the
    pull request over, so both degrade to a warning.
    """
    if not labels and not assignees:
        return
    args = ["pr", "edit", str(number)]
    for label in labels:
        args.extend(["--add-label", label])
    for assignee in assignees:
        args.extend(["--add-assignee", assignee])
    try:
        run_gh_command(args)
    except RuntimeError as error:
        logging.warning(f"Could not set labels/assignees on PR #{number}: {error}")


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
    different number of commits than the candidate does — a human's fixup
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


def _set_draft(number: int, draft: bool) -> None:
    """Force a pull request's draft state, tolerating a refusal.

    Called on every update, not just on creation, which is the `always-true`
    semantics of the action's `draft` input: a bot branch that a maintainer
    marked ready-for-review is put back into draft on the next sync, so a
    version-bump PR cannot drift into looking mergeable while its content is
    still being regenerated underneath it.
    """
    args = ["pr", "ready", str(number)]
    if draft:
        args.append("--undo")
    try:
        run_gh_command(args)
    except RuntimeError as error:
        logging.warning(f"Could not set draft={draft} on PR #{number}: {error}")


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
    once from the remote, so a detached `HEAD` is fine as long as *base* names
    the branch to open against. When the base has moved past this checkout, the
    carried commits are replayed onto its fresh tip.

    All of that happens on a throwaway local branch, and the original checkout
    is restored before returning. That matters to `autofix.yaml`'s `sync-deps`
    job, which opens four pull requests in sequence from one checkout and needs
    each to start from a clean tree.

    :param branch: Head branch to create, update or retire.
    :param title: Pull-request title.
    :param body: Rendered markdown body, trimmed here if oversized.
    :param commit_message: Message for the commit capturing uncommitted
        changes. Unused when the job committed its own work.
    :param base: Base branch. Defaults to the checked-out branch, and is
        required when `HEAD` is detached.
    :param labels: Labels to attach, best-effort.
    :param assignees: Assignees to attach, best-effort.
    :param draft: Hold the pull request in draft, on every update and not just
        at creation. See {func}`_set_draft`.
    :param add_paths: Git pathspecs limiting what the uncommitted-changes
        commit picks up. Empty commits the whole tree, which is right for a job
        whose only writes are the ones it means to publish. A job that also
        provisions its own tooling into the checkout needs the narrower form:
        anything outside the pathspec is left dirty and discarded with the
        throwaway branch.
    :param remote: Remote to publish to.
    :raises RuntimeError: When `HEAD` is detached and no *base* is given, or
        when *base* does not exist on *remote*.
    """
    base_branch = base or git_ops.current_branch()
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
    restore_ref = git_ops.current_branch() or git_ops.head_sha()
    remote_sha = git_ops.fetch_remote_branch(branch, remote=remote)

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
        # Leave the checkout exactly as it was found, whatever happened above.
        git_ops.checkout(restore_ref)
        git_ops.delete_branch(temp_branch)

    if not carried:
        # Nothing to say any more, so the branch and its pull request go. This
        # is the path a skipped action step could never reach, and the reason a
        # stale bump PR used to need its own reconciler command.
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

    if not pushed:
        return PrSyncResult(PrOperation.NONE, branch)

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
            _apply_pr_attributes(number, labels, assignees)
            if draft and not open_prs[0].get("isDraft"):
                _set_draft(number, draft=True)
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
    _apply_pr_attributes(number, labels, assignees)
    return PrSyncResult(PrOperation.CREATED, branch, number=number, url=url)
