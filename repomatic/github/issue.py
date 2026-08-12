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

"""GitHub issue and pull-request lifecycle management.

Generic primitives for listing, creating, updating, closing, and triaging
GitHub issues via the `gh` CLI, used by {mod}`repomatic.broken_links` and
other modules that manage bot-created issues. The thin pull-request closers
live here too: GitHub models pull requests as issues (they share one number
space), and both families are the same `gh <kind> <verb>` wrappers over
{mod}`~repomatic.github.gh`.

The life-cycle of issues created in CI jobs is managed here by hand because the
`create-issue-from-file` action blindly creates issues ad-nauseam.

See:
- https://github.com/peter-evans/create-issue-from-file/issues/298
- https://github.com/lycheeverse/lychee-action/issues/74#issuecomment-1587089689
"""

from __future__ import annotations

import json
import logging
import tempfile
from contextlib import contextmanager
from operator import itemgetter
from pathlib import Path

from ..metadata import Metadata
from .gh import run_gh_command
from .pr_body import fit_issue_body, generate_pr_metadata_block

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence
    from typing import Any


BOT_ISSUE_LABEL = "🤖 ci"
"""Label carried by every issue this module's lifecycle helper maintains.

Applied by {func}`~repomatic.github.issue.manage_issue_lifecycle` on creation.
Lives here rather than in a calling module because both callers reach the label
through that helper, and neither should have to import the other to agree on
it. The value is one of the labels `repomatic/data/labels.toml` declares, so
renaming it there means renaming it here: an issue labelled with a name the
registry does not carry is created unlabelled, silently.
"""

LOCKED_CONVERSATION_MARKER = "is locked"
"""Substring GitHub returns when a write is refused on a locked conversation.

The full message is `GraphQL: Unable to create comment because issue is locked
(addComment)`. Matching the tail alone also covers the pull-request phrasing,
which names the other kind in the same slot.
"""


def list_issues(title: str = "") -> list[dict[str, Any]]:
    """List all issues (open and closed), optionally filtered by title.

    ```{note}

    No `--author` filter is applied. When `REPOMATIC_PAT`
    is configured, `gh` authenticates as the token owner (not
    `github-actions[bot]`), so issues may be authored by either identity.
    Filtering by author would miss issues created under the other identity,
    breaking deduplication. The caller ({func}`triage_issues`) already
    matches by exact title, so author-agnostic listing is safe.
    ```

    :param title: If provided, only return issues whose title matches exactly.
    :return: List of issue dicts with `number`, `title`, `createdAt`,
        and `state`.
    """
    args = [
        "issue",
        "list",
        "--state",
        "all",
        "--json",
        "number,title,createdAt,state",
    ]
    if title:
        args.extend(["--search", f"{title} in:title"])
    output = run_gh_command(args)
    issues: list[dict[str, Any]] = json.loads(output)
    # `--search` is full-text, not exact match. Filter to exact title.
    if title:
        issues = [i for i in issues if i["title"] == title]
    return issues


def unlock_issue(number: int) -> None:
    """Unlock an issue's conversation.

    :param number: The issue number to unlock.
    """
    run_gh_command([
        "issue",
        "unlock",
        str(number),
    ])
    logging.info(f"Unlocked issue #{number}")


def _run_unlocking(args: Sequence[str], number: int) -> str:
    """Run a commenting `gh issue` command, clearing a conversation lock if it blocks.

    GitHub refuses `addComment` on a locked conversation, which is how the
    autolock workflow this project ships breaks the recurring issues this module
    manages: `dessant/lock-threads` locks a closed issue after 90 days of
    inactivity, and the next run that needs to reopen it (because the condition
    recurred) has its reopen comment rejected. Nothing downstream distinguishes
    that from a real failure, so the job dies and the report is never filed.

    Unlocking is deliberate rather than incidental. A conversation that repomatic
    is reopening is one it is about to comment on again, so the lock has outlived
    its purpose; autolock re-applies it 90 days after the issue next closes.

    ```{note}
    The lock is cleared only *after* a write actually fails, never
    speculatively. An unlocked conversation therefore costs no extra API call,
    and a lock set by hand on an issue repomatic never writes to is left alone.
    `gh issue list --json` and `gh issue view --json` both omit the `locked`
    field, so a pre-flight check would need a REST round-trip on every run to
    buy nothing.
    ```

    :param args: The `gh` command arguments to run.
    :param number: The issue number the command targets, used to unlock.
    :return: The command's standard output.
    :raises RuntimeError: When the command fails for any reason other than a
        conversation lock, or when it still fails after unlocking.
    """
    try:
        return run_gh_command(list(args))
    except RuntimeError as error:
        if LOCKED_CONVERSATION_MARKER not in str(error):
            raise
        logging.warning(
            f"Issue #{number} conversation is locked, unlocking to write to it."
        )
        unlock_issue(number)
        return run_gh_command(list(args))


def close_issue(number: int, comment: str) -> None:
    """Close an issue with a comment.

    :param number: The issue number to close.
    :param comment: The comment to add when closing.
    """
    _run_unlocking(
        [
            "issue",
            "close",
            str(number),
            "--comment",
            comment,
        ],
        number,
    )
    logging.info(f"Closed issue #{number}")


def reopen_issue(number: int, comment: str = "") -> None:
    """Reopen a previously closed issue.

    A closed issue old enough to reopen is old enough to have been autolocked,
    so the write goes through {func}`_run_unlocking`.

    :param number: The issue number to reopen.
    :param comment: Optional comment to add when reopening.
    """
    args = [
        "issue",
        "reopen",
        str(number),
    ]
    if comment:
        args.extend(["--comment", comment])
    _run_unlocking(args, number)
    logging.info(f"Reopened issue #{number}")


def _fit_body_file(body_file: Path) -> None:
    """Rewrite the body file in place when it exceeds GitHub's size limit.

    Guards `gh issue create` and `gh issue edit` against the API's hard
    rejection of oversized bodies. See
    {func}`repomatic.github.pr_body.fit_issue_body`.
    """
    body = body_file.read_text(encoding="UTF-8")
    fitted = fit_issue_body(body)
    if fitted != body:
        logging.warning("Issue body exceeds GitHub's size limit, trimming.")
        body_file.write_text(fitted, encoding="UTF-8")


def create_issue(body_file: Path, labels: list[str], title: str) -> int:
    """Create a new issue.

    :param body_file: Path to the file containing the issue body.
    :param labels: List of labels to apply.
    :param title: Issue title.
    :return: The created issue number.
    :raises RuntimeError: When the output carries no parsable issue URL.
    """
    _fit_body_file(body_file)
    args = [
        "issue",
        "create",
        "--title",
        title,
        "--body-file",
        str(body_file),
    ]
    for label in labels:
        args.extend(["--label", label])

    output = run_gh_command(args)
    # `gh issue create` prints the issue URL last, in the form
    # `https://github.com/owner/repo/issues/123`. Read the last line rather
    # than the whole output: gh prepends advisory lines of its own (a
    # deprecation notice, a "Warning: N uncommitted changes" banner), and
    # parsing the joined output turns one of those into a ValueError that
    # reads as a failed creation when the issue was in fact created.
    lines = [line.strip() for line in output.strip().splitlines() if line.strip()]
    tail = lines[-1].rstrip("/").rsplit("/", 1)[-1] if lines else ""
    if not tail.isdigit():
        msg = f"Could not read the issue number from `gh issue create`: {output!r}"
        raise RuntimeError(msg)
    issue_number = int(tail)
    logging.info(f"Created issue #{issue_number}")
    return issue_number


def update_issue(number: int, body_file: Path) -> None:
    """Update an existing issue body.

    :param number: The issue number to update.
    :param body_file: Path to the file containing the new issue body.
    """
    _fit_body_file(body_file)
    run_gh_command([
        "issue",
        "edit",
        str(number),
        "--body-file",
        str(body_file),
    ])
    logging.info(f"Updated issue #{number}")


def triage_issues(
    issues: list[dict],
    title: str,
    needed: bool,
) -> tuple[bool, int | None, str | None, set[int]]:
    """Triage issues matching a title for deduplication.

    :param issues: List of issue dicts from `gh issue list --json
        number,title,createdAt,state`. The `state` field is optional for
        backward compatibility; when absent it defaults to `"OPEN"`.
    :param title: Issue title to match against.
    :param needed: Whether an issue with this title should exist.
    :return: A tuple of `(issue_needed, issue_to_update, issue_state,
        issues_to_close)`.

    If `needed` is `True`, the most recent matching issue is kept as
    `issue_to_update` (with its `issue_state`) and all older matching
    issues are collected in `issues_to_close`. If `needed` is `False`,
    all open matching issues are placed in `issues_to_close` (already-closed
    issues are skipped).
    """
    issue_to_update: int | None = None
    issue_state: str | None = None
    issues_to_close: set[int] = set()

    for issue in sorted(issues, key=itemgetter("createdAt"), reverse=True):
        logging.debug(f"Processing {issue!r} ...")
        if issue["title"] != title:
            logging.debug(f"{issue!r} does not match title, skip.")
            continue
        state = issue.get("state", "OPEN")
        if needed and issue_to_update is None:
            logging.debug(f"{issue!r} is the most recent matching issue.")
            issue_to_update = issue["number"]
            issue_state = state
        else:
            # Only close open issues; skip already-closed ones.
            if state == "OPEN":
                logging.debug(f"{issue!r} is a duplicate to close.")
                issues_to_close.add(issue["number"])
            else:
                logging.debug(f"{issue!r} is already closed, skip.")

    return needed, issue_to_update, issue_state, issues_to_close


@contextmanager
def _body_file(body: str) -> Iterator[Path]:
    """Materialize an issue body as a temporary file, then remove it.

    The `gh` CLI takes a body only through `--body-file`, so every write path
    here needs one on disk. Owning the temp file at this layer keeps callers
    working in the currency they actually produce (rendered markdown) instead of
    each repeating the same write / `try` / `unlink` envelope.
    """
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".md",
        delete=False,
        encoding="UTF-8",
    ) as handle:
        handle.write(body)
        path = Path(handle.name)
    try:
        yield path
    finally:
        path.unlink(missing_ok=True)


def manage_issue_lifecycle(
    has_issues: bool,
    body: str,
    labels: list[str],
    title: str,
    no_issues_comment: str = "No more issues.",
) -> None:
    """Manage the full issue lifecycle: list, triage, close, create/update.

    This function handles:

    1. Listing all issues (open and closed) via `gh issue list`.
    2. Triaging matching issues (keep newest if needed, close duplicates).
    3. Closing duplicate open issues via `gh issue close`.
    4. Creating, updating, or reopening the main issue via `gh issue create`,
       `gh issue edit`, or `gh issue reopen`.

    When `has_issues` is `True` and the most recent matching issue is
    closed, it is reopened and updated rather than creating a duplicate. A
    conversation lock standing in the way of that reopen is cleared first, so a
    recurring issue survives the autolock workflow that closes over it; see
    {func}`_run_unlocking`.

    :param has_issues: Whether issues were found that warrant an open issue.
    :param body: The rendered markdown issue body. Written to a temporary file
        only when a create or update actually happens, since a run that just
        closes issues never needs one.
    :param labels: Labels to apply when creating a new issue.
    :param title: Issue title to match and create.
    :param no_issues_comment: Comment to add when closing issues because
        the condition no longer applies.
    """
    # List all issues (open and closed) matching this title.
    issues = list_issues(title)
    logging.info(f"Found {len(issues)} issues matching {title!r}")

    # Triage issues.
    _, issue_to_update, issue_state, issues_to_close = triage_issues(
        issues,
        title,
        has_issues,
    )

    # Generate workflow metadata block for issue comments.
    metadata_block = generate_pr_metadata_block(Metadata())

    # Close duplicate/obsolete open issues.
    for issue_number in issues_to_close:
        if issue_to_update:
            comment = f"Superseded by #{issue_to_update}."
        else:
            comment = no_issues_comment
        close_issue(issue_number, f"{comment}\n\n{metadata_block}")

    if not has_issues:
        return

    # Create, update, or reopen the issue.
    with _body_file(body) as body_path:
        if issue_to_update:
            # Reopen the issue if it was closed.
            if issue_state == "CLOSED":
                reopen_issue(
                    issue_to_update,
                    comment=f"Condition recurred.\n\n{metadata_block}",
                )
            update_issue(issue_to_update, body_path)
        else:
            create_issue(body_path, labels, title=title)


# ---------------------------------------------------------------------------
# Pull requests
# ---------------------------------------------------------------------------


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
