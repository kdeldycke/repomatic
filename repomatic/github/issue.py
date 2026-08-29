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

"""GitHub issue lifecycle management.

Generic primitives for listing, creating, updating, closing, triaging and
locking GitHub issues via the `gh` CLI, used by {mod}`repomatic.broken_links`
and other modules that manage bot-created issues. The pull-request counterpart
lives in {mod}`~repomatic.github.pr`.

Conversation locking covers both kinds rather than issues alone, because
GitHub gives issues and pull requests one number space and one lock endpoint:
{func}`lock_stale_threads` backs the `lock-threads` command and the autolock
workflow behind it.

The life-cycle of issues created in CI jobs is managed here by hand because the
`create-issue-from-file` action blindly creates issues ad-nauseam.

See:
- https://github.com/peter-evans/create-issue-from-file/issues/298
- https://github.com/lycheeverse/lychee-action/issues/74#issuecomment-1587089689
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from operator import itemgetter

from click_extra import ColumnSpec

from ..metadata.core import Metadata
from .gh import parse_create_output, run_gh_command
from .pr_body import (
    fit_github_body,
    generate_pr_metadata_block,
    temp_body_file,
)

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path
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

LOCK_INACTIVE_DAYS = 90
"""Days a closed thread must sit untouched before {func}`lock_stale_threads` locks it.

Counted from the thread's last update, not its closing date, so a closed issue
someone is still commenting on keeps resetting the clock. That is the same
measure `dessant/lock-threads` used, at the same 90-day value this repository
configured it with, so replacing the action changed no thread's fate.
"""

LOCK_ISSUE_COMMENT = (
    "This issue has been automatically locked since there has not been any"
    " recent activity after it was closed. Please open a new issue for related"
    " bugs."
)
"""Comment posted on an issue just before locking it."""

LOCK_PR_COMMENT = (
    "This pull request has been automatically locked since there has not been"
    " any recent activity after it was closed. Please open a new issue for"
    " related bugs."
)
"""Comment posted on a pull request just before locking it."""

LOCK_REASON = "resolved"
"""Reason attached to every automated lock.

One of the four values GitHub accepts (`off_topic`, `resolved`, `spam`,
`too_heated`), spelled the way `gh issue lock --reason` wants it. `resolved` is
what `dessant/lock-threads` defaulted to, and it is the only one of the four
that describes a thread locked for age rather than for conduct.
"""

LOCK_THREADS_HEADER_DEFS: tuple[ColumnSpec, ...] = (
    ColumnSpec("kind", "Kind"),
    ColumnSpec("thread", "Thread"),
    ColumnSpec("title", "Title"),
    ColumnSpec("outcome", "Outcome"),
)
"""Column definitions for the `repomatic lock-threads` table.

Lives beside {func}`lock_stale_threads`, whose rows it names, so the columns
and the tuple they render cannot drift apart; the CLI derives its `--sort-by`
choices from it.
"""

LOCK_SEARCH_LIMIT = 200
"""Threads examined per {func}`lock_stale_threads` run.

The search API caps a query at 1,000 results and the job runs weekly, so a
lower bound keeps one run's blast radius small while still draining a backlog
over a few weeks. A run that hits the cap says so, rather than reporting a
clean sweep of a set it only partially saw.
"""


def add_labels(
    repository: str | None,
    number: int,
    labels: Sequence[str],
    *,
    assignees: Sequence[str] = (),
    is_pr: bool = False,
) -> bool:
    """Add labels, and optionally assignees, to an issue or pull request.

    Additive only: labels already on the thread are left in place, and none is
    ever removed. Every automated labeller here pre-labels for the
    maintainer's first pass, so it must never undo a classification made by
    hand.

    Deliberately non-fatal, because refusals are routine: `github.actor` is
    the natural assignee and is `github-actions[bot]` whenever the bot pushed
    the triggering commit, which GitHub refuses to assign, and a label renamed
    in the labels config but not yet synced is refused the same way. Neither
    is worth losing the thread over, so both degrade to a warning.

    :param repository: GitHub repository in `owner/name` form, or `None` to
        let `gh` resolve it from the working directory's remote.
    :param number: The issue or pull request number.
    :param labels: Labels to add.
    :param assignees: Accounts to assign. A no-op when both this and *labels*
        are empty.
    :param is_pr: Whether *number* names a pull request rather than an issue.
    :return: Whether the attributes were applied. `False` on an API failure,
        which is logged rather than raised, per the refusals above.
    """
    if not labels and not assignees:
        return True
    resource = "pr" if is_pr else "issue"
    args = [resource, "edit", str(number)]
    if repository:
        args.extend(["--repo", repository])
    for label in labels:
        args.extend(["--add-label", label])
    for assignee in assignees:
        args.extend(["--add-assignee", assignee])
    try:
        run_gh_command(args)
    except RuntimeError as error:
        logging.warning(
            f"Could not set labels/assignees on {resource} #{number}: {error}"
        )
        return False
    added = ", ".join(map(repr, (*labels, *assignees)))
    target = f" in {repository}" if repository else ""
    logging.info(f"Added {added} to {resource} #{number}{target}")
    return True


def search_stale_threads(
    repository: str,
    inactive_days: int = LOCK_INACTIVE_DAYS,
    limit: int = LOCK_SEARCH_LIMIT,
) -> list[dict[str, Any]]:
    """Search closed, unlocked issues and pull requests left inactive too long.

    Issues and pull requests share one number space and one search index, so a
    single `--include-prs` query covers both and `isPullRequest` sorts them
    afterwards. Halving the round-trips matters less than the ordering it
    buys: results come back newest-first across both kinds, so a `limit` that
    truncates cuts the same slice from either.

    ```{note}
    The `is:unlocked` half of the filter is what makes the whole operation
    idempotent, and it needs no state of its own: locking a thread removes it
    from this result set permanently. A second run minutes after the first
    therefore finds nothing, which is also why the search is authoritative
    enough to skip a per-thread `locked` re-check before writing.
    ```

    :param repository: GitHub repository in `owner/name` form.
    :param inactive_days: Days without an update before a closed thread
        qualifies.
    :param limit: Maximum number of threads to return.
    :return: Search result dicts carrying `number`, `title`, `url`,
        `isPullRequest`, `labels` and `updatedAt`, newest first.
    """
    cutoff = datetime.now(timezone.utc).date() - timedelta(days=inactive_days)
    output = run_gh_command([
        "search",
        "issues",
        "--repo",
        repository,
        "--state",
        "closed",
        "--locked=false",
        "--include-prs",
        "--updated",
        f"<{cutoff.isoformat()}",
        "--sort",
        "updated",
        "--limit",
        str(limit),
        "--json",
        "number,title,url,isPullRequest,labels,updatedAt",
    ])
    threads: list[dict[str, Any]] = json.loads(output)
    logging.info(
        f"Found {len(threads)} closed thread(s) in {repository} with no activity"
        f" since {cutoff.isoformat()}."
    )
    if len(threads) >= limit:
        logging.warning(
            f"Search returned the full {limit}-thread limit, so older threads may"
            " remain unlocked. They are picked up by the next run."
        )
    return threads


def lock_thread(
    repository: str,
    number: int,
    *,
    is_pr: bool,
    comment: str = "",
    reason: str = LOCK_REASON,
) -> None:
    """Comment on a closed thread, then lock its conversation.

    The comment goes first on purpose: posting it after the lock would need the
    lock lifted again, and a reader arriving at a locked thread with no
    explanation has no way to learn where to go instead.

    :param repository: GitHub repository in `owner/name` form.
    :param number: The issue or pull request number to lock.
    :param is_pr: Whether *number* names a pull request rather than an issue.
    :param comment: Comment to post before locking. Skipped when empty.
    :param reason: Lock reason, one of GitHub's four accepted values. Omitted
        from the call when empty.
    """
    kind = "pr" if is_pr else "issue"
    if comment:
        run_gh_command([
            kind,
            "comment",
            str(number),
            "--repo",
            repository,
            "--body",
            comment,
        ])
    args = [kind, "lock", str(number), "--repo", repository]
    if reason:
        args.extend(["--reason", reason])
    run_gh_command(args)
    logging.info(f"Locked {kind} #{number}")


def lock_stale_threads(
    repository: str,
    inactive_days: int = LOCK_INACTIVE_DAYS,
    issue_comment: str = LOCK_ISSUE_COMMENT,
    pr_comment: str = LOCK_PR_COMMENT,
    exclude_labels: Sequence[str] = (BOT_ISSUE_LABEL,),
    limit: int = LOCK_SEARCH_LIMIT,
    reason: str = LOCK_REASON,
    *,
    dry_run: bool = True,
) -> list[tuple[str, str, str, str]]:
    """Lock every closed thread left inactive for *inactive_days*.

    ```{caution}
    `exclude_labels` defaults to {data}`BOT_ISSUE_LABEL` because the issues
    {func}`manage_issue_lifecycle` maintains are *designed* to be reopened when
    their condition recurs, and GitHub refuses `addComment` on a locked
    conversation. Locking one turns the next reopen into a failed job, which is
    the hole {func}`run_unlocking` exists to patch after the fact. Excluding
    the label stops the collision at the source; the recovery path stays in
    place for locks applied by hand.
    ```

    Label exclusion is applied here rather than folded into the search query.
    GitHub's `-label:` qualifier would work, but it starts with a hyphen, which
    `gh search` parses as a flag and needs shell-level escaping to survive: a
    client-side filter over a field the search already returns costs one
    comparison and no quoting.

    :param repository: GitHub repository in `owner/name` form.
    :param inactive_days: Days without an update before a closed thread
        qualifies.
    :param issue_comment: Comment posted on an issue before locking it.
    :param pr_comment: Comment posted on a pull request before locking it.
    :param exclude_labels: Threads carrying any of these labels are left alone.
    :param limit: Maximum number of threads to examine in one run.
    :param reason: Lock reason passed to `gh {issue,pr} lock --reason`.
    :param dry_run: Report what would be locked without writing anything.
    :return: One `(kind, number, title, outcome)` row per examined thread.
    """
    excluded = {label.casefold() for label in exclude_labels}
    rows: list[tuple[str, str, str, str]] = []

    for thread in search_stale_threads(repository, inactive_days, limit):
        is_pr = bool(thread["isPullRequest"])
        number = int(thread["number"])
        kind = "PR" if is_pr else "issue"
        labels = {label["name"].casefold() for label in thread.get("labels", ())}

        if held := sorted(labels & excluded):
            logging.debug(f"Skipping {kind} #{number}, labelled {held}.")
            rows.append((kind, f"#{number}", thread["title"], "skipped (labelled)"))
            continue

        if dry_run:
            rows.append((kind, f"#{number}", thread["title"], "would lock"))
            continue

        lock_thread(
            repository,
            number,
            is_pr=is_pr,
            comment=pr_comment if is_pr else issue_comment,
            reason=reason,
        )
        rows.append((kind, f"#{number}", thread["title"], "locked"))

    return rows


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


def unlock_thread(number: int, *, is_pr: bool = False) -> None:
    """Unlock an issue or pull request's conversation.

    :param number: The issue or pull request number to unlock.
    :param is_pr: Whether *number* names a pull request rather than an issue.
    """
    kind = "pr" if is_pr else "issue"
    run_gh_command([
        kind,
        "unlock",
        str(number),
    ])
    logging.info(f"Unlocked {kind} #{number}")


def run_unlocking(args: Sequence[str], number: int, *, is_pr: bool = False) -> str:
    """Run a commenting `gh` command, clearing a conversation lock if it blocks.

    GitHub refuses `addComment` on a locked conversation, which is how a lock
    breaks the recurring issues this module manages: the next run that needs to
    reopen one (because the condition recurred) has its reopen comment
    rejected. The same refusal breaks `gh pr close --comment` on a locked pull
    request, which is {func}`~repomatic.github.pr.close_pr`'s whole retire
    path. Nothing downstream distinguishes either from a real failure, so the
    job dies and the report is never filed.

    {func}`lock_stale_threads` no longer causes that, since it skips anything
    carrying {data}`BOT_ISSUE_LABEL`. This path remains for the locks it does
    not own: one applied by hand, or one left behind by the
    `dessant/lock-threads` action this command replaced, which had no such
    exclusion configured.

    Unlocking is deliberate rather than incidental. A conversation that repomatic
    is reopening is one it is about to comment on again, so the lock has outlived
    its purpose; autolock re-applies it 90 days after the thread next closes.

    ```{note}
    The lock is cleared only *after* a write actually fails, never
    speculatively. An unlocked conversation therefore costs no extra API call,
    and a lock set by hand on a thread repomatic never writes to is left alone.
    `gh issue list --json` and `gh issue view --json` both omit the `locked`
    field, so a pre-flight check would need a REST round-trip on every run to
    buy nothing.
    ```

    :param args: The `gh` command arguments to run.
    :param number: The thread number the command targets, used to unlock.
    :param is_pr: Whether *number* names a pull request rather than an issue.
    :return: The command's standard output.
    :raises RuntimeError: When the command fails for any reason other than a
        conversation lock, or when it still fails after unlocking.
    """
    try:
        return run_gh_command(list(args))
    except RuntimeError as error:
        if LOCKED_CONVERSATION_MARKER not in str(error):
            raise
        kind = "PR" if is_pr else "issue"
        logging.warning(
            f"{kind} #{number} conversation is locked, unlocking to write to it."
        )
        unlock_thread(number, is_pr=is_pr)
        return run_gh_command(list(args))


def close_issue(number: int, comment: str) -> None:
    """Close an issue with a comment.

    :param number: The issue number to close.
    :param comment: The comment to add when closing.
    """
    run_unlocking(
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
    so the write goes through {func}`run_unlocking`.

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
    run_unlocking(args, number)
    logging.info(f"Reopened issue #{number}")


def create_issue(body_file: Path, labels: list[str], title: str) -> int:
    """Create a new issue.

    :param body_file: Path to the file containing the issue body, already
        trimmed to GitHub's size limit (see
        {func}`~repomatic.github.pr_body.fit_github_body`).
    :param labels: List of labels to apply.
    :param title: Issue title.
    :return: The created issue number.
    :raises RuntimeError: When the output carries no parsable issue URL.
    """
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
    issue_number, _url = parse_create_output(output, "issue")
    logging.info(f"Created issue #{issue_number}")
    return issue_number


def update_issue(number: int, body_file: Path) -> None:
    """Update an existing issue body.

    :param number: The issue number to update.
    :param body_file: Path to the file containing the new issue body, already
        trimmed to GitHub's size limit (see
        {func}`~repomatic.github.pr_body.fit_github_body`).
    """
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
    {func}`run_unlocking`.

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

    # Create, update, or reopen the issue. The body is fitted to GitHub's
    # size limit before it ever touches disk, so the write paths below need
    # no rewrite-in-place dance.
    fitted = fit_github_body(body)
    if fitted != body:
        logging.warning("Issue body exceeds GitHub's size limit, trimming.")

    with temp_body_file(fitted) as body_path:
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
