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

Generic primitives for listing, creating, updating, closing, and triaging
GitHub issues via the `gh` CLI. Used by {mod}`broken_links` and potentially
other modules that manage bot-created issues.

We need to manually manage the life-cycle of issues created in CI jobs because the
`create-issue-from-file` action blindly creates issues ad-nauseam.

See:
- https://github.com/peter-evans/create-issue-from-file/issues/298
- https://github.com/lycheeverse/lychee-action/issues/74#issuecomment-1587089689
"""

from __future__ import annotations

import json
import logging
from operator import itemgetter
from pathlib import Path

from .gh import run_gh_command
from .pr_body import fit_issue_body, generate_pr_metadata_block

TYPE_CHECKING = False
if TYPE_CHECKING:
    from typing import Any


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


def close_issue(number: int, comment: str) -> None:
    """Close an issue with a comment.

    :param number: The issue number to close.
    :param comment: The comment to add when closing.
    """
    run_gh_command([
        "issue",
        "close",
        str(number),
        "--comment",
        comment,
    ])
    logging.info(f"Closed issue #{number}")


def reopen_issue(number: int, comment: str = "") -> None:
    """Reopen a previously closed issue.

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
    run_gh_command(args)
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
    # gh issue create outputs the issue URL, extract the number from it.
    # Format: https://github.com/owner/repo/issues/123
    issue_url = output.strip()
    issue_number = int(issue_url.rstrip("/").split("/")[-1])
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


def manage_issue_lifecycle(
    has_issues: bool,
    body_file: Path,
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
    closed, it is reopened and updated rather than creating a duplicate.

    :param has_issues: Whether issues were found that warrant an open issue.
    :param body_file: Path to the file containing the issue body.
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
    metadata_block = generate_pr_metadata_block()

    # Close duplicate/obsolete open issues.
    for issue_number in issues_to_close:
        if issue_to_update:
            comment = f"Superseded by #{issue_to_update}."
        else:
            comment = no_issues_comment
        close_issue(issue_number, f"{comment}\n\n{metadata_block}")

    # Create, update, or reopen issue if needed.
    if has_issues:
        if issue_to_update:
            # Reopen the issue if it was closed.
            if issue_state == "CLOSED":
                reopen_issue(
                    issue_to_update,
                    comment=f"Condition recurred.\n\n{metadata_block}",
                )
            update_issue(issue_to_update, body_file)
        else:
            create_issue(body_file, labels, title=title)
