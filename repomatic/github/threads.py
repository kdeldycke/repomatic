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

"""Write to an issue or a pull request: labels, locks, and lock recovery.

GitHub gives issues and pull requests one number space and one lock endpoint,
so the primitives here take a {class}`ThreadKind` instead of being written
twice. Each one takes the same `repository` keyword: `None` lets `gh` resolve
the repository from `GH_REPO` or the checkout.

{func}`lock_stale_threads` backs the `lock-threads` command and the autolock
workflow behind it.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from click_extra import ColumnSpec

from .gh import gh_json, run_gh_command

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Sequence
    from typing import Any


BOT_ISSUE_LABEL = "🤖 ci"
"""Label carried by every issue repomatic maintains in CI.

The issue lifecycle of {mod}`~repomatic.github.issue` applies it on creation,
and {func}`lock_stale_threads` spares it by default, so it lives at this lower
layer. The value is one of the labels `repomatic/data/labels.toml` declares, so
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
someone is still commenting on keeps resetting the clock.
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

LOCK_REASONS = ("off_topic", "resolved", "spam", "too_heated")
"""The lock reasons GitHub accepts, spelled the way `gh issue lock --reason` wants."""

LOCK_REASON = "resolved"
"""Reason attached to every automated lock.

One of {data}`LOCK_REASONS`, and the only one of them that describes a thread
locked for age rather than for conduct.
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
lower bound keeps one run's reach small while still draining a backlog over a
few weeks. A run that hits the cap says so, rather than reporting a clean sweep
of a set it only partially saw.
"""


class ThreadKind(StrEnum):
    """The two kinds of conversation thread, valued as `gh` names its commands."""

    ISSUE = "issue"
    PR = "pr"

    @classmethod
    def of(cls, is_pr: bool) -> ThreadKind:
        """The kind a `--pr/--issue` flag or an `isPullRequest` field names."""
        return cls.PR if is_pr else cls.ISSUE

    @property
    def label(self) -> str:
        """How a log line or a report names the kind: `issue` or `PR`."""
        return "PR" if self is ThreadKind.PR else "issue"


def repo_args(repository: str | None) -> list[str]:
    """The `--repo` arguments naming *repository*, none when `gh` resolves it."""
    return ["--repo", repository] if repository else []


def add_labels(
    number: int,
    labels: Sequence[str],
    *,
    kind: ThreadKind = ThreadKind.ISSUE,
    assignees: Sequence[str] = (),
    repository: str | None = None,
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

    :param number: The issue or pull request number.
    :param labels: Labels to add.
    :param kind: Whether *number* names an issue or a pull request.
    :param assignees: Accounts to assign. A no-op when both this and *labels*
        are empty.
    :param repository: Repository in `owner/name` form, or `None` to let `gh`
        resolve it.
    :return: Whether the attributes were applied. `False` on an API failure,
        which is logged rather than raised, per the refusals above.
    """
    if not labels and not assignees:
        return True
    args = [kind, "edit", str(number), *repo_args(repository)]
    for label in labels:
        args.extend(["--add-label", label])
    for assignee in assignees:
        args.extend(["--add-assignee", assignee])
    try:
        run_gh_command(args)
    except RuntimeError as error:
        logging.warning(
            f"Could not set labels/assignees on {kind.label} #{number}: {error}"
        )
        return False
    added = ", ".join(map(repr, (*labels, *assignees)))
    target = f" in {repository}" if repository else ""
    logging.info(f"Added {added} to {kind.label} #{number}{target}")
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

    :param repository: Repository in `owner/name` form. Required: a search
        without one covers every repository on GitHub.
    :param inactive_days: Days without an update before a closed thread
        qualifies.
    :param limit: Maximum number of threads to return.
    :return: Search result dicts carrying `number`, `title`, `url`,
        `isPullRequest`, `labels` and `updatedAt`, newest first.
    """
    cutoff = datetime.now(UTC).date() - timedelta(days=inactive_days)
    threads: list[dict[str, Any]] = (
        gh_json([
            "search",
            "issues",
            *repo_args(repository),
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
        or []
    )
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
    number: int,
    *,
    kind: ThreadKind,
    comment: str = "",
    reason: str = LOCK_REASON,
    repository: str | None = None,
) -> None:
    """Comment on a closed thread, then lock its conversation.

    The comment goes first on purpose: posting it after the lock would need the
    lock lifted again, and a reader arriving at a locked thread with no
    explanation has no way to learn where to go instead.

    :param number: The issue or pull request number to lock.
    :param kind: Whether *number* names an issue or a pull request.
    :param comment: Comment to post before locking. Skipped when empty.
    :param reason: Lock reason, one of {data}`LOCK_REASONS`. Omitted from the
        call when empty.
    :param repository: Repository in `owner/name` form, or `None` to let `gh`
        resolve it.
    """
    target = repo_args(repository)
    if comment:
        run_gh_command([kind, "comment", str(number), *target, "--body", comment])
    args = [kind, "lock", str(number), *target]
    if reason:
        args.extend(["--reason", reason])
    run_gh_command(args)
    logging.info(f"Locked {kind.label} #{number}")


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
    {func}`~repomatic.github.issue.manage_issue_lifecycle` maintains are
    *designed* to be reopened when their condition recurs, and GitHub refuses
    `addComment` on a locked conversation. Locking one turns the next reopen
    into a failed job, which is the hole {func}`run_unlocking` patches after
    the fact. Excluding the label stops the collision at the source; the
    recovery path stays in place for locks applied by hand.
    ```

    Label exclusion is applied here rather than folded into the search query.
    GitHub's `-label:` qualifier would work, but it starts with a hyphen, which
    `gh search` parses as a flag and needs shell-level escaping to survive: a
    client-side filter over a field the search already returns costs one
    comparison and no quoting.

    :param repository: Repository in `owner/name` form.
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
        kind = ThreadKind.of(bool(thread["isPullRequest"]))
        number = int(thread["number"])
        labels = {label["name"].casefold() for label in thread.get("labels", ())}
        row = (kind.label, f"#{number}", thread["title"])

        if held := sorted(labels & excluded):
            logging.debug(f"Skipping {kind.label} #{number}, labelled {held}.")
            rows.append((*row, "skipped (labelled)"))
            continue

        if dry_run:
            rows.append((*row, "would lock"))
            continue

        lock_thread(
            number,
            kind=kind,
            comment=pr_comment if kind is ThreadKind.PR else issue_comment,
            reason=reason,
            repository=repository,
        )
        rows.append((*row, "locked"))

    return rows


def unlock_thread(
    number: int,
    *,
    kind: ThreadKind = ThreadKind.ISSUE,
    repository: str | None = None,
) -> None:
    """Unlock an issue or pull request's conversation.

    :param number: The issue or pull request number to unlock.
    :param kind: Whether *number* names an issue or a pull request.
    :param repository: Repository in `owner/name` form, or `None` to let `gh`
        resolve it.
    """
    run_gh_command([kind, "unlock", str(number), *repo_args(repository)])
    logging.info(f"Unlocked {kind.label} #{number}")


def run_unlocking(
    args: Sequence[str],
    number: int,
    *,
    kind: ThreadKind = ThreadKind.ISSUE,
    repository: str | None = None,
) -> str:
    """Run a commenting `gh` command, clearing a conversation lock if it blocks.

    GitHub refuses `addComment` on a locked conversation, which is how a lock
    breaks the recurring issues {mod}`~repomatic.github.issue` manages: the
    next run that needs to reopen one has its reopen comment rejected. The
    same refusal breaks `gh pr close --comment` on a locked pull request, which
    is {func}`~repomatic.github.pr.close_pr`'s whole retire path. Nothing
    downstream tells either from a real failure, so the job dies and the report
    is never filed.

    {func}`lock_stale_threads` skips anything carrying {data}`BOT_ISSUE_LABEL`,
    so this path covers the locks it does not own, like one applied by hand.

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

    :param args: The `gh` command arguments to run, without `--repo`.
    :param number: The thread number the command targets, used to unlock.
    :param kind: Whether *number* names an issue or a pull request.
    :param repository: Repository in `owner/name` form, or `None` to let `gh`
        resolve it. The command and the unlock both target it.
    :return: The command's standard output.
    :raises RuntimeError: When the command fails for any reason other than a
        conversation lock, or when it still fails after unlocking.
    """
    command = [*args, *repo_args(repository)]
    try:
        return run_gh_command(command)
    except RuntimeError as error:
        if LOCKED_CONVERSATION_MARKER not in str(error):
            raise
        logging.warning(
            f"{kind.label} #{number} conversation is locked, unlocking it to"
            " write to it."
        )
        unlock_thread(number, kind=kind, repository=repository)
        return run_gh_command(command)
