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

Lists, creates, updates, closes and reopens the issues CI jobs maintain, used
by {mod}`repomatic.broken_links` and the setup guide. The writes that issues
share with pull requests (labels, locks) live in
{mod}`~repomatic.github.threads`.

The life-cycle of issues created in CI jobs is managed here by hand because the
`create-issue-from-file` action blindly creates issues ad-nauseam.

See:
- https://github.com/peter-evans/create-issue-from-file/issues/298
- https://github.com/lycheeverse/lychee-action/issues/74#issuecomment-1587089689
"""

from __future__ import annotations

import logging
from operator import itemgetter
from typing import NamedTuple

from ..metadata.core import Metadata
from .gh import gh_json, parse_create_output, run_gh_command
from .pr_body import fit_github_body, generate_pr_metadata_block, temp_body_file
from .threads import repo_args, run_unlocking

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path
    from typing import Any


ISSUE_LIST_LIMIT = 100
"""Issues {func}`list_issues` reads at most.

`gh issue list` stops at 30 unless told otherwise. A title search matches a
handful of issues, since a recurring condition reopens its one issue instead of
filing a new one, so this bound is never reached in practice.
"""


class Triage(NamedTuple):
    """What {func}`triage_issues` decided for the issues sharing one title."""

    keep: int | None
    """The issue to update, or `None` when no issue is needed or none exists."""

    keep_state: str | None
    """The state of the kept issue, `OPEN` or `CLOSED`."""

    close: set[int]
    """The open issues to close as duplicates or as resolved."""


def list_issues(
    title: str = "", *, repository: str | None = None
) -> list[dict[str, Any]]:
    """List all issues (open and closed), optionally filtered by title.

    ```{note}

    No `--author` filter is applied. When `REPOMATIC_PAT`
    is configured, `gh` authenticates as the token owner (not
    `github-actions[bot]`), so issues may be authored by either identity.
    Filtering by author would miss issues created under the other identity,
    breaking deduplication.
    ```

    :param title: If provided, only return issues whose title matches exactly.
    :param repository: Repository in `owner/name` form, or `None` to let `gh`
        resolve it.
    :return: List of issue dicts with `number`, `title`, `createdAt`,
        and `state`.
    """
    args = [
        "issue",
        "list",
        *repo_args(repository),
        "--state",
        "all",
        "--limit",
        str(ISSUE_LIST_LIMIT),
        "--json",
        "number,title,createdAt,state",
    ]
    if title:
        args.extend(["--search", f"{title} in:title"])
    issues: list[dict[str, Any]] = gh_json(args) or []
    # `--search` is full-text, not exact match. Filter to exact title.
    if title:
        issues = [i for i in issues if i["title"] == title]
    return issues


def close_issue(number: int, comment: str, *, repository: str | None = None) -> None:
    """Close an issue with a comment.

    :param number: The issue number to close.
    :param comment: The comment to add when closing.
    :param repository: Repository in `owner/name` form, or `None` to let `gh`
        resolve it.
    """
    run_unlocking(
        ["issue", "close", str(number), "--comment", comment],
        number,
        repository=repository,
    )
    logging.info(f"Closed issue #{number}")


def reopen_issue(
    number: int, comment: str = "", *, repository: str | None = None
) -> None:
    """Reopen a previously closed issue.

    A closed issue old enough to reopen is old enough to have been autolocked,
    so the write goes through {func}`~repomatic.github.threads.run_unlocking`.

    :param number: The issue number to reopen.
    :param comment: Optional comment to add when reopening.
    :param repository: Repository in `owner/name` form, or `None` to let `gh`
        resolve it.
    """
    args = ["issue", "reopen", str(number)]
    if comment:
        args.extend(["--comment", comment])
    run_unlocking(args, number, repository=repository)
    logging.info(f"Reopened issue #{number}")


def create_issue(
    body_file: Path,
    labels: Sequence[str],
    title: str,
    *,
    repository: str | None = None,
) -> int:
    """Create a new issue.

    :param body_file: Path to the file containing the issue body, already
        trimmed to GitHub's size limit (see
        {func}`~repomatic.github.pr_body.fit_github_body`).
    :param labels: Labels to apply.
    :param title: Issue title.
    :param repository: Repository in `owner/name` form, or `None` to let `gh`
        resolve it.
    :return: The created issue number.
    :raises RuntimeError: When the output carries no parsable issue URL.
    """
    args = [
        "issue",
        "create",
        *repo_args(repository),
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


def update_issue(
    number: int, body_file: Path, *, repository: str | None = None
) -> None:
    """Update an existing issue body.

    :param number: The issue number to update.
    :param body_file: Path to the file containing the new issue body, already
        trimmed to GitHub's size limit (see
        {func}`~repomatic.github.pr_body.fit_github_body`).
    :param repository: Repository in `owner/name` form, or `None` to let `gh`
        resolve it.
    """
    run_gh_command([
        "issue",
        "edit",
        str(number),
        *repo_args(repository),
        "--body-file",
        str(body_file),
    ])
    logging.info(f"Updated issue #{number}")


def triage_issues(issues: Sequence[dict[str, Any]], needed: bool) -> Triage:
    """Triage the issues sharing one title, for deduplication.

    If *needed*, the most recent issue is kept (with its state) and every
    older open one is closed. If not, every open issue is closed. An issue
    already closed is never closed again.

    :param issues: Issue dicts from `gh issue list --json
        number,title,createdAt,state`, already filtered to one title (see
        {func}`list_issues`).
    :param needed: Whether an issue with this title should exist.
    :return: The issue to keep, its state, and the issues to close.
    """
    keep: int | None = None
    keep_state: str | None = None
    close: set[int] = set()

    for issue in sorted(issues, key=itemgetter("createdAt"), reverse=True):
        logging.debug(f"Processing {issue!r} ...")
        if needed and keep is None:
            logging.debug(f"{issue!r} is the most recent matching issue.")
            keep = issue["number"]
            keep_state = issue["state"]
        elif issue["state"] == "OPEN":
            logging.debug(f"{issue!r} is a duplicate to close.")
            close.add(issue["number"])
        else:
            logging.debug(f"{issue!r} is already closed, skip.")

    return Triage(keep, keep_state, close)


def manage_issue_lifecycle(
    has_issues: bool,
    body: str,
    labels: Sequence[str],
    title: str,
    no_issues_comment: str = "No more issues.",
    *,
    repository: str | None = None,
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
    {func}`~repomatic.github.threads.run_unlocking`.

    :param has_issues: Whether issues were found that warrant an open issue.
    :param body: The rendered markdown issue body. Written to a temporary file
        only when a create or update actually happens, since a run that just
        closes issues never needs one.
    :param labels: Labels to apply when creating a new issue.
    :param title: Issue title to match and create.
    :param no_issues_comment: Comment to add when closing issues because
        the condition no longer applies.
    :param repository: Repository in `owner/name` form, or `None` to let `gh`
        resolve it.
    """
    issues = list_issues(title, repository=repository)
    logging.info(f"Found {len(issues)} issues matching {title!r}")

    triage = triage_issues(issues, has_issues)

    # Generate workflow metadata block for issue comments.
    metadata_block = generate_pr_metadata_block(Metadata())

    # Close duplicate/obsolete open issues.
    for issue_number in triage.close:
        if triage.keep:
            comment = f"Superseded by #{triage.keep}."
        else:
            comment = no_issues_comment
        close_issue(
            issue_number, f"{comment}\n\n{metadata_block}", repository=repository
        )

    if not has_issues:
        return

    # Create, update, or reopen the issue. The body is fitted to GitHub's
    # size limit before it ever touches disk, so the write paths below need
    # no rewrite-in-place dance.
    fitted = fit_github_body(body)
    if fitted != body:
        logging.warning("Issue body exceeds GitHub's size limit, trimming.")

    with temp_body_file(fitted) as body_path:
        if triage.keep:
            if triage.keep_state == "CLOSED":
                reopen_issue(
                    triage.keep,
                    comment=f"Condition recurred.\n\n{metadata_block}",
                    repository=repository,
                )
            update_issue(triage.keep, body_path, repository=repository)
        else:
            create_issue(body_path, labels, title=title, repository=repository)
