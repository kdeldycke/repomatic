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

```{caution} Working-tree changes only

This covers the case where a job's output sits in the working tree and the base
is the checked-out branch. It does **not** yet cover a detached `HEAD`, an
explicit base the checkout is not sitting on, commits the job made itself, or
keeping a pull request in draft across updates. Those jobs (`fix-changelog`,
`bump-version`, `prepare-release`) still call the action, and
`tests/test_workflows.py` holds the split in place.
```
"""

from __future__ import annotations

import json
import logging
from typing import NamedTuple
from uuid import uuid4

from .. import git_ops
from ..compat import StrEnum
from .gh import run_gh_command
from .pr_body import fit_issue_body, temp_body_file

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Sequence
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
    :return: List of PR dicts with `number` and `title`. Empty if no
        open PR exists on `branch`.
    """
    output = run_gh_command([
        "pr",
        "list",
        "--state",
        "open",
        "--head",
        branch,
        "--json",
        "number,title",
    ])
    prs: list[dict[str, Any]] = json.loads(output)
    return prs


def close_pr(number: int, comment: str, delete_branch: bool = True) -> None:
    """Close a pull request with a comment.

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
    run_gh_command(args)
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


def _parse_pr_number(output: str) -> int:
    """Read the pull-request number out of `gh pr create` output.

    `gh` prints the PR URL last, in the form
    `https://github.com/owner/repo/pull/123`. Read the last line rather than
    the whole output: `gh` prepends advisory lines of its own (a deprecation
    notice, a "Warning: N uncommitted changes" banner), and parsing the joined
    output turns one of those into an error that reads as a failed creation
    when the pull request was in fact created.

    :raises RuntimeError: When the output carries no parsable PR URL.
    """
    lines = [line.strip() for line in output.strip().splitlines() if line.strip()]
    tail = lines[-1].rstrip("/").rsplit("/", 1)[-1] if lines else ""
    if not tail.isdigit():
        msg = f"Could not read the PR number from `gh pr create`: {output!r}"
        raise RuntimeError(msg)
    return int(tail)


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
    args = ["pr", "edit", str(number)]
    for label in labels:
        args.extend(["--add-label", label])
    for assignee in assignees:
        args.extend(["--add-assignee", assignee])
    if len(args) == 3:
        return
    try:
        run_gh_command(args)
    except RuntimeError as error:
        logging.warning(f"Could not set labels/assignees on PR #{number}: {error}")


def _needs_push(candidate_sha: str, remote_sha: str, base_sha: str) -> bool:
    """Decide whether the remote branch has to be overwritten.

    Two independent reasons, ported from the four-condition check in the
    action's `createOrUpdateBranch`. The point of checking at all is that a
    force-push which changes nothing still re-triggers every workflow watching
    the branch, so the whole matrix would re-run on each pass of a job whose
    output never moved.

    The tree comparison is what normally answers this, and it also catches a
    base that advanced: rebuilding on a newer base yields a different tree even
    when the job's own edits are byte-identical. The commit count catches the
    structural cases a tree comparison cannot see, where the branch holds
    something other than exactly one bot commit on top of the base.

    :param candidate_sha: The commit just built from the working tree.
    :param remote_sha: The current tip of the remote branch.
    :param base_sha: The commit both are measured against.
    """
    if git_ops.tree_sha(candidate_sha) != git_ops.tree_sha(remote_sha):
        logging.info("Remote branch carries a different tree, updating.")
        return True
    if git_ops.count_commits_between(base_sha, remote_sha) != 1:
        logging.info("Remote branch is not a single commit on base, updating.")
        return True
    logging.info("Remote branch already carries this exact change.")
    return False


def upsert_pr(
    branch: str,
    title: str,
    body: str,
    commit_message: str,
    base: str | None = None,
    labels: Sequence[str] = (),
    assignees: Sequence[str] = (),
    remote: str = "origin",
) -> PrSyncResult:
    """Converge *branch* and its pull request onto the working tree's contents.

    Idempotent by construction: a second call with an unchanged working tree
    performs no write at all and reports {attr}`PrOperation.NONE`. The four
    outcomes are those of {class}`PrOperation`.

    The base is the currently checked-out branch, and its tip is read once at
    the top and used throughout, so a base that advances mid-call cannot
    produce a half-rebased branch. The candidate commit is built on a throwaway
    local branch and the base branch is restored before returning, which
    matters to `autofix.yaml`'s `sync-deps` job: it opens four pull requests in
    sequence from one checkout, and each needs to start from a clean tree.

    :param branch: Head branch to create, update or retire.
    :param title: Pull-request title.
    :param body: Rendered markdown body, trimmed here if oversized.
    :param commit_message: Message for the single commit the branch carries.
    :param base: Base branch. Defaults to the checked-out branch.
    :param labels: Labels to attach, best-effort.
    :param assignees: Assignees to attach, best-effort.
    :param remote: Remote to publish to.
    :raises RuntimeError: When `HEAD` is detached and no *base* is given.
    """
    base_branch = base or git_ops.current_branch()
    if not base_branch:
        msg = (
            "`HEAD` is detached and no base branch was given. Pass `base` "
            "explicitly, or check out a branch before syncing a pull request."
        )
        raise RuntimeError(msg)
    base_sha = git_ops.head_sha()

    remote_sha = git_ops.fetch_remote_branch(branch, remote=remote)

    if not git_ops.stage_all():
        # Nothing in the working tree, so the branch has nothing left to say.
        # This is the path a skipped action step could never reach, and the
        # reason a stale bump PR used to need its own reconciler command.
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

    temp_branch = f"{TEMP_BRANCH_PREFIX}{uuid4().hex[:12]}"
    try:
        git_ops.create_branch(temp_branch)
        candidate_sha = git_ops.commit_staged(commit_message)

        if remote_sha is not None and not _needs_push(
            candidate_sha, remote_sha, base_sha
        ):
            return PrSyncResult(PrOperation.NONE, branch)

        git_ops.force_push_branch(
            temp_branch, branch, expected_sha=remote_sha, remote=remote
        )
    finally:
        # Leave the checkout exactly as it was found, whatever happened above.
        git_ops.checkout(base_branch)
        git_ops.delete_branch(temp_branch)

    open_prs = list_open_prs_by_branch(branch)
    fitted = fit_issue_body(body)
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
            return PrSyncResult(PrOperation.UPDATED, branch, number=number)

        output = run_gh_command([
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
        ])

    number = _parse_pr_number(output)
    url = output.strip().splitlines()[-1].strip()
    logging.info(f"Created PR #{number}: {url}")
    _apply_pr_attributes(number, labels, assignees)
    return PrSyncResult(PrOperation.CREATED, branch, number=number, url=url)
