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

"""Unsubscribe from closed, inactive GitHub notification threads.

Processes notification threads in two phases:

1. **REST notification threads** — Fetches all Issue/PullRequest notification
   threads via ``/notifications``, inspects each for closed + stale status,
   and unsubscribes via ``DELETE`` + ``PATCH``.

2. **GraphQL threadless subscriptions** — Searches for closed issues/PRs the
   user is involved in but that lack notification threads, and unsubscribes
   via the ``updateSubscription`` mutation.

Requires the ``gh`` CLI to be installed and authenticated with a token that
has the ``notifications`` scope (classic PAT) or equivalent fine-grained
permissions.
"""

from __future__ import annotations

import calendar
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from .gh import run_gh_command
from .pr_body import render_template
from .token import validate_classic_pat_scope

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Iterator
    from typing import Any

GRAPHQL_PAGE_SIZE = 25
"""Per-page count for GraphQL search results."""

NOTIFICATION_PAGE_SIZE = 50
"""Per-page count for REST ``/notifications`` results."""

NOTIFICATION_SUBJECT_TYPES = frozenset({"Issue", "PullRequest"})
"""Notification subject types to process."""

THREADLESS_SEARCH_QUERY = """
query($searchQuery: String!, $cursor: String, $pageSize: Int!) {
  search(query: $searchQuery, type: ISSUE, first: $pageSize, after: $cursor) {
    pageInfo { hasNextPage endCursor }
    nodes {
      ... on Issue {
        id
        number
        repository { nameWithOwner }
        title
        updatedAt
        url
        viewerSubscription
      }
      ... on PullRequest {
        id
        number
        repository { nameWithOwner }
        title
        updatedAt
        url
        viewerSubscription
      }
    }
  }
}
"""

UNSUBSCRIBE_MUTATION = """
mutation($id: ID!) {
  updateSubscription(input: {subscribableId: $id, state: UNSUBSCRIBED}) {
    subscribable { id }
  }
}
"""


class ItemAction(Enum):
    """Action taken (or to be taken) on a notification item."""

    DRY_RUN = "dry_run"
    FAILED = "failed"
    UNSUBSCRIBED = "unsubscribed"


@dataclass(frozen=True)
class DetailRow:
    """Per-item detail for the markdown report table."""

    action: ItemAction
    html_url: str
    number: int | None
    repo: str
    title: str
    updated_at: datetime | None


@dataclass
class Phase1Result:
    """Accumulated counts and details from REST notification phase."""

    batch_size: int = 0
    cutoff: datetime | None = None
    newest_updated: datetime | None = None
    oldest_updated: datetime | None = None
    rows: list[DetailRow] = field(default_factory=list)
    threads_failed: int = 0
    threads_inspected: int = 0
    threads_skipped_open: int = 0
    threads_skipped_recent: int = 0
    threads_skipped_unknown: int = 0
    threads_total: int = 0
    threads_unsubscribed: int = 0


@dataclass
class Phase2Result:
    """Accumulated counts and details from GraphQL threadless phase."""

    batch_size: int = 0
    cutoff: datetime | None = None
    graphql_failed: int = 0
    graphql_not_subscribed: int = 0
    graphql_total: int = 0
    graphql_unsubscribed: int = 0
    rows: list[DetailRow] = field(default_factory=list)
    search_query: str = ""
    skipped: bool = False
    skip_reason: str = ""


@dataclass
class UnsubscribeResult:
    """Accumulated results from both unsubscribe phases."""

    dry_run: bool = False
    months: int = 3
    phase1: Phase1Result = field(default_factory=Phase1Result)
    phase2: Phase2Result = field(default_factory=Phase2Result)


def _compute_cutoff(months: int) -> datetime:
    """Compute a cutoff datetime by subtracting ``months`` from now.

    Uses ``calendar.monthrange`` for day-clamping to avoid invalid dates
    (e.g., subtracting 1 month from March 31 yields February 28/29).

    :param months: Number of months to subtract.
    :return: Timezone-aware UTC datetime.
    """
    now = datetime.now(timezone.utc)
    year = now.year
    month = now.month - months
    while month < 1:
        month += 12
        year -= 1
    max_day = calendar.monthrange(year, month)[1]
    day = min(now.day, max_day)
    return now.replace(year=year, month=month, day=day)


def _action_emoji(action: ItemAction) -> str:
    """Map an action to its emoji + label for the report table."""
    return {
        ItemAction.DRY_RUN: "\U0001f441\ufe0f Dry-run",
        ItemAction.FAILED: "\u26a0\ufe0f Failed",
        ItemAction.UNSUBSCRIBED: "\U0001f515 Unsubscribed",
    }[action]


def _days_ago(dt: datetime) -> str:
    """Compute a human-readable "N days ago" string from now."""
    now = datetime.now(timezone.utc)
    delta = now - dt
    return f"{delta.days} days ago"


def _format_link(row: DetailRow) -> str:
    """Render a markdown link for a detail row.

    Produces `` [`repo#number`](url) `` when a number is available,
    otherwise just the repo name.
    """
    if row.number is not None and row.html_url:
        return f"[`{row.repo}#{row.number}`]({row.html_url})"
    return row.repo


def _fetch_notification_threads(
    batch_size: int,
) -> tuple[int, list[dict[str, Any]]]:
    """Fetch Issue/PullRequest notification threads via REST API.

    Returns the total count of matching threads and a batch sorted
    oldest-first, truncated to ``batch_size``.

    :param batch_size: Maximum number of threads to return.
    :return: Tuple of ``(total_count, truncated_batch)``. Each thread dict
        contains ``id``, ``subject_url``, ``subject_type``, ``repo``,
        ``title``.
    """
    # The --jq filter selects Issue/PullRequest types and extracts fields.
    jq_filter = (
        ".[] | select(.subject.type == "
        + " or .subject.type == ".join(
            f'"{t}"' for t in sorted(NOTIFICATION_SUBJECT_TYPES)
        )
        + ")"
        " | {id, repo: .repository.full_name,"
        " subject_type: .subject.type, subject_url: .subject.url,"
        " title: .subject.title}"
    )
    try:
        output = run_gh_command([
            "api",
            "/notifications",
            "--paginate",
            "--jq",
            jq_filter,
            "-f",
            "all=true",
            "-f",
            f"per_page={NOTIFICATION_PAGE_SIZE}",
        ])
    except RuntimeError:
        logging.warning("Failed to fetch notification threads.")
        return 0, []

    threads = []
    for line in output.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            threads.append(json.loads(line))
        except json.JSONDecodeError:
            logging.warning(f"Skipping malformed notification line: {line!r}")

    total = len(threads)
    # Reverse to process oldest first, then truncate.
    threads.reverse()
    return total, threads[:batch_size]


def _get_thread_details(subject_url: str) -> dict[str, Any] | None:
    """Fetch details for a notification thread's subject.

    :param subject_url: The API URL of the thread's subject (issue or PR).
    :return: Dict with ``state``, ``updated_at``, ``html_url``, ``number``,
        or ``None`` if the subject is inaccessible.
    """
    try:
        output = run_gh_command([
            "api",
            subject_url,
            "--jq",
            "{state, updated_at, html_url, number}",
        ])
        return json.loads(output)  # type: ignore[no-any-return]
    except RuntimeError:
        logging.debug(f"Subject inaccessible: {subject_url}")
        return None
    except json.JSONDecodeError:
        logging.debug(f"Malformed subject response: {subject_url}")
        return None


def _unsubscribe_rest_thread(thread_id: str) -> bool:
    """Unsubscribe from a notification thread and mark it read.

    Performs two API calls:

    1. ``DELETE /notifications/threads/{id}/subscription``
    2. ``PATCH /notifications/threads/{id}``

    :param thread_id: The notification thread ID.
    :return: ``True`` if both calls succeeded, ``False`` otherwise.
    """
    try:
        run_gh_command([
            "api",
            "--method",
            "DELETE",
            f"/notifications/threads/{thread_id}/subscription",
        ])
    except RuntimeError:
        logging.warning(f"Failed to delete subscription for thread {thread_id}.")
        return False

    try:
        run_gh_command([
            "api",
            "--method",
            "PATCH",
            f"/notifications/threads/{thread_id}",
        ])
    except RuntimeError:
        logging.warning(f"Failed to mark thread {thread_id} as read.")
        return False

    return True


def _validate_notifications_token() -> None:
    """Validate that the current token can access the notifications API.

    Delegates to :func:`validate_classic_pat_scope` for generic checks,
    then warns if the token has more scopes than needed.

    :raises RuntimeError: If validation fails.
    """
    scope_list = validate_classic_pat_scope("notifications")

    # Notifications-specific: warn about extra scopes.
    if scope_list != ["notifications"]:
        scopes_header = ", ".join(scope_list)
        logging.warning(
            "GH_TOKEN has more scopes than needed: '%s'."
            " Only 'notifications' is required.",
            scopes_header,
        )


def _get_authenticated_username() -> str:
    """Get the login of the authenticated GitHub user.

    :return: The username string.
    :raises RuntimeError: If the API call fails.
    """
    return run_gh_command(["api", "/user", "--jq", ".login"]).strip()


def _iter_closed_items(
    search_query: str,
    batch_size: int,
) -> Iterator[dict[str, Any]]:
    """Iterate over closed issues/PRs matching a GraphQL search query.

    Uses cursor-based GraphQL pagination. Yields all items regardless
    of ``viewerSubscription``; callers filter as needed.

    :param search_query: The GitHub search query string.
    :param batch_size: Maximum total items to yield.
    :yields: Dicts with ``id``, ``number``, ``title``, ``repository``,
        ``updatedAt``, ``url``, ``viewerSubscription``.
    """
    cursor = None
    yielded = 0

    while yielded < batch_size:
        page_size = min(GRAPHQL_PAGE_SIZE, batch_size - yielded)
        args = [
            "api",
            "graphql",
            "-f",
            f"query={THREADLESS_SEARCH_QUERY}",
            "-f",
            f"searchQuery={search_query}",
            "-F",
            f"pageSize={page_size}",
        ]
        if cursor:
            args.extend(["-f", f"cursor={cursor}"])

        output = run_gh_command(args)
        data = json.loads(output)
        search_data = data.get("data", {}).get("search", {})

        for node in search_data.get("nodes", []):
            if not node:
                continue
            yield node
            yielded += 1
            if yielded >= batch_size:
                return

        page_info = search_data.get("pageInfo", {})
        if not page_info.get("hasNextPage"):
            break
        cursor = page_info.get("endCursor")


def _graphql_unsubscribe(node_id: str) -> bool:
    """Unsubscribe from an issue or PR via GraphQL mutation.

    :param node_id: The global node ID of the subscribable.
    :return: ``True`` if the mutation succeeded, ``False`` otherwise.
    """
    try:
        run_gh_command([
            "api",
            "graphql",
            "-f",
            f"query={UNSUBSCRIBE_MUTATION}",
            "-f",
            f"id={node_id}",
        ])
    except RuntimeError:
        logging.warning(f"GraphQL unsubscribe failed for node {node_id}.")
        return False
    return True


def _render_detail_table(rows: list[DetailRow]) -> str:
    """Render a details table with header and data rows.

    :param rows: Detail rows to render.
    :return: Details heading and table, or empty string if no rows.
    """
    if not rows:
        return ""
    lines = [
        "### \U0001f4dd Details",
        "",
        (
            "| \U0001f4ac Title | \U0001f517 Link"
            " | \U0001f550 Last activity | \u26a1 Action |"
        ),
        "| --- | --- | --- | --- |",
    ]
    for row in rows:
        ago = _days_ago(row.updated_at) if row.updated_at else "-"
        lines.append(
            f"| {row.title} | {_format_link(row)}"
            f" | {ago} | {_action_emoji(row.action)} |"
        )
    return "\n".join(lines)


def render_report(result: UnsubscribeResult) -> str:
    """Render a markdown report from unsubscribe results.

    Pure function that produces the same markdown structure as the
    downstream ``unsubscribe.yaml`` workflow's ``$GITHUB_STEP_SUMMARY``.

    :param result: Structured results from both phases.
    :return: Markdown report string.
    """
    mode = "dry-run" if result.dry_run else "live"
    p1 = result.phase1
    p2 = result.phase2

    # Phase 1 summary line.
    cutoff_str = p1.cutoff.isoformat() if p1.cutoff else "-"
    if result.dry_run:
        summary_line = (
            f"\U0001f50d **Candidates found:** {len(p1.rows)}"
            f" \u2014 cutoff: `{cutoff_str}`"
            f" (inactive for more than {result.months} months, dry-run)"
        )
    else:
        summary_line = (
            f"\U0001f515 **Unsubscribed:** {p1.threads_unsubscribed}"
            f" | \u26a0\ufe0f **Failed:** {p1.threads_failed}"
            f" \u2014 cutoff: `{cutoff_str}`"
            f" (inactive for more than {result.months} months)"
        )

    # Phase 1 batch details rows.
    remaining = p1.threads_total - p1.threads_inspected
    oldest_str = p1.oldest_updated.isoformat() if p1.oldest_updated else "-"
    newest_str = p1.newest_updated.isoformat() if p1.newest_updated else "-"
    batch_details_rows = "\n".join([
        f"| \U0001f514 Total notifications | {p1.threads_total} |",
        f"| \U0001f4e6 Batch size | {p1.batch_size} |",
        f"| \U0001f50e Inspected | {p1.threads_inspected} |",
        f"| \U0001f4cb Remaining backlog | {remaining} |",
        f"| \u23ea Oldest activity | {oldest_str} |",
        f"| \u23e9 Newest activity | {newest_str} |",
        f"| \u2702\ufe0f Cutoff | {cutoff_str} |",
    ])

    # Phase 1 state breakdown rows.
    stale_count = p1.threads_unsubscribed + p1.threads_failed
    state_breakdown_rows = "\n".join([
        f"| \U0001f7e2 Open | {p1.threads_skipped_open} |",
        (f"| \U0001f7e1 Closed (active since cutoff) | {p1.threads_skipped_recent} |"),
        f"| \U0001f534 Closed (inactive, eligible) | {stale_count} |",
        f"| \u26aa Unknown | {p1.threads_skipped_unknown} |",
    ])

    # Backlog warning (empty string if not applicable).
    backlog_warning = ""
    if (
        p1.oldest_updated is not None
        and p1.cutoff is not None
        and remaining > 0
        and p1.oldest_updated >= p1.cutoff
    ):
        backlog_warning = "\n".join([
            "> [!WARNING]",
            (
                "> Oldest activity seen in this batch:"
                f" `{oldest_str}` (cutoff: `{cutoff_str}`)."
            ),
            ("> The notification API does not sort by issue activity, so the current"),
            (
                f"> batch of {p1.threads_inspected} threads did not reach"
                " any old-enough candidates."
            ),
            (
                f"> Consider increasing `batch-size`"
                f" (currently {p1.batch_size})"
                " or running manually"
            ),
            "> with a larger batch to clear the backlog faster.",
        ])

    # Phase 1 details section (includes --- separator).
    p1_detail_table = _render_detail_table(p1.rows)
    details_section = f"---\n\n{p1_detail_table}" if p1_detail_table else ""

    phase1 = render_template(
        "unsubscribe-phase1",
        mode=mode,
        summary_line=summary_line,
        batch_details_rows=batch_details_rows,
        state_breakdown_rows=state_breakdown_rows,
        backlog_warning=backlog_warning,
        details_section=details_section,
    )

    # Phase 2 content.
    if p2.skipped:
        phase2_content = f"> [!WARNING]\n> {p2.skip_reason}"
    else:
        # Phase 2 summary line.
        p2_cutoff_str = p2.cutoff.isoformat() if p2.cutoff else "-"
        if result.dry_run:
            p2_summary = (
                f"\U0001f50d **Candidates found:** {len(p2.rows)}"
                f" \u2014 cutoff: `{p2_cutoff_str}`"
                f" (inactive for more than {result.months} months, dry-run)"
            )
        else:
            p2_summary = (
                f"\U0001f515 **Unsubscribed:** {p2.graphql_unsubscribed}"
                f" | \u26a0\ufe0f **Failed:** {p2.graphql_failed}"
                f" \u2014 cutoff: `{p2_cutoff_str}`"
                f" (inactive for more than {result.months} months)"
            )

        # Phase 2 search details table.
        subscribed_count = p2.graphql_unsubscribed + p2.graphql_failed
        p2_table = "\n".join([
            "### \U0001f4ca Search details",
            "",
            "| Metric | Value |",
            "| --- | --- |",
            f"| \U0001f50e Search query | `{p2.search_query}` |",
            f"| \U0001f514 Total results | {p2.graphql_total} |",
            f"| \U0001f4e6 Batch size | {p2.batch_size} |",
            f"| \u2705 Still subscribed | {subscribed_count} |",
            f"| \u23ed\ufe0f Not subscribed | {p2.graphql_not_subscribed} |",
        ])

        p2_detail_table = _render_detail_table(p2.rows)
        p2_parts = [p2_summary, "", p2_table]
        if p2_detail_table:
            p2_parts.extend(["", p2_detail_table])
        phase2_content = "\n".join(p2_parts)

    phase2 = render_template(
        "unsubscribe-phase2",
        mode=mode,
        phase2_content=phase2_content,
    )

    return phase1 + "\n\n---\n\n" + phase2 + "\n"


def unsubscribe_threads(
    months: int,
    batch_size: int,
    dry_run: bool,
) -> UnsubscribeResult:
    """Unsubscribe from closed, inactive notification threads.

    Runs two phases:

    1. **REST notification threads** — Fetches notification threads, inspects
       each subject for closed + stale status, and unsubscribes.
    2. **GraphQL threadless subscriptions** — Searches for closed issues/PRs
       the user is involved in and unsubscribes via mutation.

    :param months: Inactivity threshold in months.
    :param batch_size: Maximum threads/items to process per phase.
    :param dry_run: If ``True``, report what would be done without acting.
    :return: Structured results from both phases.
    """
    result = UnsubscribeResult(dry_run=dry_run, months=months)
    cutoff = _compute_cutoff(months)
    prefix = "[dry-run] " if dry_run else ""

    logging.info(f"Cutoff date: {cutoff.strftime('%Y-%m-%d')} ({months} months ago).")

    # Phase 1: REST notification threads.
    logging.info("Phase 1: Processing REST notification threads...")
    p1 = result.phase1
    p1.cutoff = cutoff
    p1.batch_size = batch_size

    total, threads = _fetch_notification_threads(batch_size)
    p1.threads_total = total

    for thread in threads:
        p1.threads_inspected += 1
        thread_id = thread["id"]
        subject_url = thread["subject_url"]
        thread_repo = thread.get("repo", "")
        thread_title = thread.get("title", "")

        details = _get_thread_details(subject_url)
        if details is None:
            p1.threads_skipped_unknown += 1
            logging.info(f"  Thread {thread_id}: subject inaccessible, skipping.")
            continue

        state = details.get("state", "unknown")
        updated_str = details.get("updated_at", "")
        updated_at = None
        try:
            updated_at = datetime.fromisoformat(updated_str.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            pass

        # Track oldest/newest across all items with valid timestamps.
        if updated_at is not None:
            if p1.oldest_updated is None or updated_at < p1.oldest_updated:
                p1.oldest_updated = updated_at
            if p1.newest_updated is None or updated_at > p1.newest_updated:
                p1.newest_updated = updated_at

        if state == "unknown" or updated_at is None:
            p1.threads_skipped_unknown += 1
            logging.info(f"  Thread {thread_id}: state={state}, skipping.")
            continue

        if state != "closed":
            p1.threads_skipped_open += 1
            logging.info(f"  Thread {thread_id}: state={state}, skipping.")
            continue

        if updated_at >= cutoff:
            p1.threads_skipped_recent += 1
            logging.info(f"  Thread {thread_id}: updated recently, skipping.")
            continue

        # Closed + stale: candidate for unsubscription.
        html_url = details.get("html_url", subject_url)
        number = details.get("number")

        logging.info(f"  {prefix}Unsubscribing from thread {thread_id} ({html_url}).")
        if dry_run:
            p1.threads_unsubscribed += 1
            p1.rows.append(
                DetailRow(
                    action=ItemAction.DRY_RUN,
                    html_url=html_url,
                    number=number,
                    repo=thread_repo,
                    title=thread_title,
                    updated_at=updated_at,
                )
            )
            continue

        if _unsubscribe_rest_thread(str(thread_id)):
            p1.threads_unsubscribed += 1
            p1.rows.append(
                DetailRow(
                    action=ItemAction.UNSUBSCRIBED,
                    html_url=html_url,
                    number=number,
                    repo=thread_repo,
                    title=thread_title,
                    updated_at=updated_at,
                )
            )
        else:
            p1.threads_failed += 1
            p1.rows.append(
                DetailRow(
                    action=ItemAction.FAILED,
                    html_url=html_url,
                    number=number,
                    repo=thread_repo,
                    title=thread_title,
                    updated_at=updated_at,
                )
            )

    # Phase 2: GraphQL threadless subscriptions.
    logging.info("Phase 2: Processing GraphQL threadless subscriptions...")
    p2 = result.phase2
    p2.cutoff = cutoff
    p2.batch_size = batch_size

    try:
        username = _get_authenticated_username()
    except RuntimeError:
        logging.warning("Failed to get authenticated username. Skipping Phase 2.")
        p2.skipped = True
        p2.skip_reason = "Failed to get authenticated username. Skipping Phase 2."
        return result

    cutoff_date = cutoff.strftime("%Y-%m-%d")
    p2.search_query = f"involves:{username} is:closed updated:<{cutoff_date}"

    try:
        for item in _iter_closed_items(p2.search_query, batch_size):
            p2.graphql_total += 1
            node_id = item["id"]
            repo = item.get("repository", {}).get("nameWithOwner", "unknown")
            number = item.get("number")

            # Filter: only act on items the user is subscribed to.
            if item.get("viewerSubscription") != "SUBSCRIBED":
                p2.graphql_not_subscribed += 1
                continue

            # Parse updatedAt and url from GraphQL result.
            gql_updated_str = item.get("updatedAt", "")
            gql_updated_at = None
            try:
                gql_updated_at = datetime.fromisoformat(
                    gql_updated_str.replace("Z", "+00:00")
                )
            except (ValueError, AttributeError):
                pass

            gql_url = item.get("url", "")
            title = item.get("title", "")

            logging.info(f"  {prefix}Unsubscribing from {repo}#{number} (GraphQL).")
            if dry_run:
                p2.graphql_unsubscribed += 1
                p2.rows.append(
                    DetailRow(
                        action=ItemAction.DRY_RUN,
                        html_url=gql_url,
                        number=number,
                        repo=repo,
                        title=title,
                        updated_at=gql_updated_at,
                    )
                )
                continue

            if _graphql_unsubscribe(node_id):
                p2.graphql_unsubscribed += 1
                p2.rows.append(
                    DetailRow(
                        action=ItemAction.UNSUBSCRIBED,
                        html_url=gql_url,
                        number=number,
                        repo=repo,
                        title=title,
                        updated_at=gql_updated_at,
                    )
                )
            else:
                p2.graphql_failed += 1
                p2.rows.append(
                    DetailRow(
                        action=ItemAction.FAILED,
                        html_url=gql_url,
                        number=number,
                        repo=repo,
                        title=title,
                        updated_at=gql_updated_at,
                    )
                )
    except RuntimeError:
        logging.warning(
            "GraphQL search failed. Phase 2 may be incomplete. "
            "Fine-grained PATs may not support GraphQL search."
        )
        p2.skipped = True
        p2.skip_reason = (
            "GraphQL search failed. Fine-grained PATs may not support GraphQL search."
        )

    return result
