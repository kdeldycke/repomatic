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
   threads via `/notifications`, inspects each for closed + stale status,
   and unsubscribes via `DELETE` + `PATCH`.

2. **GraphQL threadless subscriptions** — Searches for closed issues/PRs the
   user is involved in but that lack notification threads, and unsubscribes
   via the `updateSubscription` mutation.

Requires the `gh` CLI to be installed and authenticated with a token that
has the `notifications` scope (classic PAT) or equivalent fine-grained
permissions.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import partial

import arrow

from ..humanize import parse_iso_datetime
from ..tabular import render_markdown_table
from .actions import ReportAction
from .gh import gh_api_json, gh_graphql, iter_graphql_nodes, run_gh_command
from .pr_body import render_template
from .token import validate_classic_pat_scope

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Iterator
    from typing import Any

GRAPHQL_PAGE_SIZE = 25
"""Per-page count for GraphQL search results."""

NOTIFICATION_PAGE_SIZE = 50
"""Per-page count for REST `/notifications` results."""

NOTIFICATION_SUBJECT_TYPES = frozenset({"Issue", "PullRequest"})
"""Notification subject types to process."""

SUBJECT_BATCH_SIZE = 50
"""Subjects looked up per GraphQL round trip.

The whole cost of the REST phase is one detail call per thread. A batched
lookup answers 50 in one request for a single rate-limit point, so a 113-thread
pool costs three requests instead of 113.
"""

SUBJECT_STATES = {"OPEN": "open", "CLOSED": "closed", "MERGED": "closed"}
"""GraphQL subject states, mapped onto the REST spelling the phase compares.

REST reports a merged pull request as `closed`, where GraphQL distinguishes
`MERGED`. Collapsing the two here keeps one vocabulary in the caller, and keeps
the batched lookup a drop-in for {func}`_get_thread_details`.
"""

SUBJECT_URL_PATTERN = re.compile(
    r"/repos/(?P<owner>[^/]+)/(?P<repo>[^/]+)/(?:issues|pulls)/(?P<number>\d+)$"
)
"""Owner, repository and number, read off a notification's subject URL.

A subject URL is the only handle the notification list gives out. GraphQL
addresses the same object by coordinates, and `issueOrPullRequest` covers both
shapes, so the `issues`/`pulls` half of the path is read and discarded.
"""


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


UNSUBSCRIBE_WORKFLOW = "unsubscribe.yaml"
"""Workflow file the backlog warning links to for a manual, larger-batch run.

The report renders inside that workflow's own run, so the reader is one click
from the `Run workflow` form the warning tells them to use. Kept equal to the
filename {data}`repomatic.registry.COMPONENTS` ships, since a link to a
workflow a repository does not have is worse than no link.
"""


@dataclass(frozen=True)
class DetailRow:
    """Per-item detail for the markdown report table."""

    action: ReportAction
    html_url: str
    number: int | None
    repo: str
    title: str
    updated_at: datetime | None


@dataclass
class Phase1Result:
    """Accumulated counts and details from REST notification phase."""

    cutoff: datetime | None = None
    max_unsubscribes: int = 0
    newest_updated: datetime | None = None
    oldest_updated: datetime | None = None
    rows: list[DetailRow] = field(default_factory=list)
    threads_deferred: int = 0
    threads_failed: int = 0
    threads_skipped_open: int = 0
    threads_skipped_recent: int = 0
    threads_skipped_unknown: int = 0
    threads_total: int = 0
    threads_unsubscribed: int = 0


@dataclass
class Phase2Result:
    """Accumulated counts and details from GraphQL threadless phase."""

    cutoff: datetime | None = None
    items_deferred: int = 0
    items_failed: int = 0
    items_not_subscribed: int = 0
    items_skipped_recent: int = 0
    items_total: int = 0
    items_unsubscribed: int = 0
    max_unsubscribes: int = 0
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
    """Compute a cutoff datetime by subtracting `months` from now.

    arrow's `shift` clamps the day for us (subtracting 1 month from March 31
    yields February 28/29), and the library is already loaded for the report's
    `.humanize()` phrasing.

    :param months: Number of months to subtract.
    :return: Timezone-aware UTC datetime.
    """
    now = datetime.now(timezone.utc)
    return arrow.Arrow.fromdatetime(now).shift(months=-months).datetime


def _format_link(row: DetailRow) -> str:
    """Render a markdown link for a detail row.

    Produces `` [`repo#number`](url) `` when a number is available,
    otherwise just the repo name.
    """
    if row.number is not None and row.html_url:
        return f"[`{row.repo}#{row.number}`]({row.html_url})"
    return row.repo


def _fetch_notification_threads(cutoff: datetime) -> list[dict[str, Any]]:
    """Fetch Issue/PullRequest notification threads last moved before *cutoff*.

    The `before` parameter of `GET /notifications` narrows the list to what is
    worth a detail call, which is what makes *batch_size* mean something.
    Against the unfiltered list the batch is spent on whatever the response
    happens to hold, active threads included: a run of 600 inspected 600 and
    found no eligible candidate at all, 588 still active and 12 still open.
    A probe measured the filter cutting 1637 threads to 113.

    ```{note}
    `before` is a pre-filter, never a verdict, and the same probe showed it
    reads a clock of its own: of 113 threads it returned against a 90-day
    cutoff, 104 predated that cutoff by `updated_at` and 112 by `last_read_at`.
    So it over-returns rather than under-returns, which is the safe direction.
    Eligibility is decided per thread by {func}`_get_thread_details`, on the
    subject's own state and `updated_at`.
    ```

    :param cutoff: Inactivity boundary, passed to the API as `before`.
    :return: Every candidate the filter returned, oldest first. Each thread
        dict contains `id`, `updated_at`, `subject_url`, `subject_type`,
        `repo`, `title`.
    """
    # The --jq filter selects Issue/PullRequest types and extracts fields.
    jq_filter = (
        ".[] | select(.subject.type == "
        + " or .subject.type == ".join(
            f'"{t}"' for t in sorted(NOTIFICATION_SUBJECT_TYPES)
        )
        + ")"
        " | {id, updated_at, repo: .repository.full_name,"
        " subject_type: .subject.type, subject_url: .subject.url,"
        " title: .subject.title}"
    )
    try:
        output = run_gh_command([
            "api",
            "--method",
            "GET",
            "/notifications",
            "--paginate",
            "--jq",
            jq_filter,
            "--raw-field",
            "all=true",
            "--raw-field",
            f"before={cutoff.astimezone(timezone.utc):%Y-%m-%dT%H:%M:%SZ}",
            "--raw-field",
            f"per_page={NOTIFICATION_PAGE_SIZE}",
        ])
    except RuntimeError as exc:
        logging.warning(f"Failed to fetch notification threads: {exc}")
        return []

    threads = []
    for line in output.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            threads.append(json.loads(line))
        except json.JSONDecodeError:
            logging.warning(f"Skipping malformed notification line: {line!r}")

    # Sorted here rather than trusted from the response. The endpoint documents
    # itself as "sorted by most recently updated", and a probe against 1637 real
    # threads found the list ordered by neither `updated_at` nor thread id, in
    # either direction, so the reverse this used to do walked an arbitrary slice
    # and called it the oldest one. Sorting on the timestamp the payload already
    # carries is what makes the batch the deepest end of the backlog, so a run
    # that cannot clear the whole pool leaves the next one where it stopped.
    threads.sort(key=lambda thread: thread.get("updated_at") or "")
    return threads


def _get_thread_details(subject_url: str) -> dict[str, Any] | None:
    """Fetch details for a notification thread's subject.

    :param subject_url: The API URL of the thread's subject (issue or PR).
    :return: Dict with `state`, `updated_at`, `html_url`, `number`,
        or `None` if the subject is inaccessible.
    """
    details = gh_api_json([
        "api",
        subject_url,
        "--jq",
        "{state, updated_at, html_url, number}",
    ])
    if details is None:
        logging.debug(f"Subject inaccessible or malformed: {subject_url}")
    return details


def _subject_query(targets: dict[str, tuple[str, str, str]]) -> str:
    """Render one aliased GraphQL query covering every subject in *targets*.

    :param targets: Alias to `(owner, repo, number)`.
    :return: A query selecting state and timestamps for each alias.
    """
    blocks = []
    for alias, (owner, repo, number) in targets.items():
        fields = "state updatedAt number url"
        blocks.append(
            f"  {alias}: repository("
            f"owner: {json.dumps(owner)}, name: {json.dumps(repo)}) {{\n"
            f"    issueOrPullRequest(number: {number}) {{\n"
            f"      __typename\n"
            f"      ... on Issue {{ {fields} }}\n"
            f"      ... on PullRequest {{ {fields} }}\n"
            f"    }}\n"
            f"  }}"
        )
    return "{\n" + "\n".join(blocks) + "\n}"


def _batch_subject_details(subject_urls: list[str]) -> dict[str, dict[str, Any]]:
    """Look up many subjects in one GraphQL request.

    The batched counterpart of {func}`_get_thread_details`, returning the same
    four fields under the same names so either can feed the phase.

    :param subject_urls: Subject URLs from the notification list.
    :return: Subject URL to its detail dict. A subject GraphQL answered `null`
        for (an inaccessible repository, a deleted issue) is absent, exactly as
        the single lookup returns `None` for it.
    :raises RuntimeError: When the `gh` invocation fails, leaving the caller to
        fall back.
    """
    targets = {}
    for index, url in enumerate(subject_urls):
        match = SUBJECT_URL_PATTERN.search(url)
        if match is None:
            continue
        targets[f"s{index}"] = (
            match["owner"],
            match["repo"],
            match["number"],
        )
    if not targets:
        return {}
    data = gh_graphql(_subject_query(targets)) or {}

    details = {}
    for alias in targets:
        node = (data.get(alias) or {}).get("issueOrPullRequest")
        if not node:
            continue
        url = subject_urls[int(alias[1:])]
        details[url] = {
            "state": SUBJECT_STATES.get(node.get("state", ""), "unknown"),
            "updated_at": node.get("updatedAt", ""),
            "html_url": node.get("url", ""),
            "number": node.get("number"),
        }
    return details


def _fetch_subject_details(subject_urls: list[str]) -> dict[str, dict[str, Any]]:
    """Resolve every subject's state, batched, with a per-subject fallback.

    Defence in depth rather than a bet on one API: a batch that fails for any
    reason (a `gh` error, a malformed envelope, a subject URL this code cannot
    parse) is re-read one REST call at a time, which is what the phase did for
    every thread before. The run then costs what it used to instead of
    reporting a pool of inaccessible subjects.

    :param subject_urls: Subject URLs from the notification list.
    :return: Subject URL to its detail dict, omitting whatever neither route
        could resolve.
    """
    details: dict[str, dict[str, Any]] = {}
    pending: list[str] = []
    for start in range(0, len(subject_urls), SUBJECT_BATCH_SIZE):
        chunk = subject_urls[start : start + SUBJECT_BATCH_SIZE]
        try:
            resolved = _batch_subject_details(chunk)
        except (RuntimeError, ValueError, KeyError, TypeError) as exc:
            logging.warning(f"Batched subject lookup failed ({exc}); falling back.")
            resolved = {}
            pending.extend(chunk)
            continue
        details.update(resolved)
        pending.extend(url for url in chunk if url not in resolved)

    for url in pending:
        single = _get_thread_details(url)
        if single is not None:
            details[url] = single
    return details


def _unsubscribe_rest_thread(thread_id: str) -> bool:
    """Unsubscribe from a notification thread and mark it read.

    Performs two API calls:

    1. ``DELETE /notifications/threads/{id}/subscription``
    2. ``PATCH /notifications/threads/{id}``

    :param thread_id: The notification thread ID.
    :return: `True` if both calls succeeded, `False` otherwise.
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

    Delegates to {func}`validate_classic_pat_scope` for generic checks,
    then warns if the token has more scopes than needed.

    :raises RuntimeError: If validation fails.
    """
    scope_list = validate_classic_pat_scope("notifications")

    # Notifications-specific: warn about extra scopes.
    if scope_list != ["notifications"]:
        scopes_header = ", ".join(scope_list)
        logging.warning(
            f"GH_TOKEN has more scopes than needed: '{scopes_header}'. Only "
            "'notifications' is required."
        )


def _get_authenticated_username() -> str:
    """Get the login of the authenticated GitHub user.

    :return: The username string.
    :raises RuntimeError: If the API call fails.
    """
    return run_gh_command(["api", "/user", "--jq", ".login"]).strip()


def _iter_closed_items(search_query: str) -> Iterator[dict[str, Any]]:
    """Iterate over closed issues/PRs matching a GraphQL search query.

    Uses cursor-based GraphQL pagination. Yields all items regardless
    of `viewerSubscription`; callers filter as needed.

    :param search_query: The GitHub search query string.
    :yields: Dicts with `id`, `number`, `title`, `repository`,
        `updatedAt`, `url`, `viewerSubscription`.
    """
    return iter_graphql_nodes(
        THREADLESS_SEARCH_QUERY,
        ("search",),
        {"searchQuery": search_query},
        page_size_var="pageSize",
        page_size=GRAPHQL_PAGE_SIZE,
    )


def _graphql_unsubscribe(node_id: str) -> bool:
    """Unsubscribe from an issue or PR via GraphQL mutation.

    :param node_id: The global node ID of the subscribable.
    :return: `True` if the mutation succeeded, `False` otherwise.
    """
    try:
        run_gh_command([
            "api",
            "graphql",
            "--raw-field",
            f"query={UNSUBSCRIBE_MUTATION}",
            "--raw-field",
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
    table = render_markdown_table(
        (
            "\U0001f4ac Title",
            "\U0001f517 Link",
            "\U0001f550 Last activity",
            "\u26a1 Action",
        ),
        (
            (
                row.title,
                _format_link(row),
                arrow.get(row.updated_at).humanize() if row.updated_at else "-",
                row.action.value,
            )
            for row in rows
        ),
    )
    return f"### \U0001f4dd Details\n\n{table}"


def _phase_summary_line(
    candidates: int,
    unsubscribed: int,
    failed: int,
    cutoff: datetime | None,
    months: int,
    dry_run: bool,
) -> str:
    """One phase's summary line, with identical wording for both phases."""
    cutoff_str = cutoff.isoformat() if cutoff else "-"
    if dry_run:
        return (
            f"\U0001f50d **Candidates found:** {candidates}"
            f" \u2014 cutoff: `{cutoff_str}`"
            f" (inactive for more than {months} months, dry-run)"
        )
    return (
        f"\U0001f515 **Unsubscribed:** {unsubscribed}"
        f" | \u26a0\ufe0f **Failed:** {failed}"
        f" \u2014 cutoff: `{cutoff_str}`"
        f" (inactive for more than {months} months)"
    )


def _render_phase1_fragments(
    p1: Phase1Result, months: int, dry_run: bool, repo_url: str | None = None
) -> dict[str, str]:
    """Build the phase-1 fragments of the report template.

    :param repo_url: Repository the run belongs to, used to link the backlog
        warning to its workflow. The warning degrades to plain text without it.
    """
    cutoff_str = p1.cutoff.isoformat() if p1.cutoff else "-"
    summary_line = _phase_summary_line(
        len(p1.rows),
        p1.threads_unsubscribed,
        p1.threads_failed,
        p1.cutoff,
        months,
        dry_run,
    )

    # Whole tables, not rows spliced into a table the template opens: a
    # multi-row value wrapped in one row's pipes gave the first row an empty
    # leading cell and the last a pair of trailing ones. Rendering here also
    # matches {func}`_render_phase2_content`, and puts the markup out of reach
    # of the formatter that owns the template file.
    oldest_str = p1.oldest_updated.isoformat() if p1.oldest_updated else "-"
    newest_str = p1.newest_updated.isoformat() if p1.newest_updated else "-"
    batch_details_table = render_markdown_table(
        ("Metric", "Value"),
        (
            ("\U0001f514 Threads before cutoff", p1.threads_total),
            ("\U0001f4e6 Max unsubscribes", p1.max_unsubscribes),
            ("\U0001f4cb Deferred to a later run", p1.threads_deferred),
            ("\u23ea Oldest activity", oldest_str),
            ("\u23e9 Newest activity", newest_str),
            ("\u2702\ufe0f Cutoff", cutoff_str),
        ),
    )

    stale_count = p1.threads_unsubscribed + p1.threads_failed
    state_breakdown_table = render_markdown_table(
        ("State", "Count"),
        (
            ("\U0001f7e2 Open", p1.threads_skipped_open),
            ("\U0001f7e1 Closed (active since cutoff)", p1.threads_skipped_recent),
            ("\U0001f534 Closed (inactive, eligible)", stale_count),
            ("\u26aa Unknown", p1.threads_skipped_unknown),
        ),
    )

    # Backlog warning (empty string if not applicable).
    backlog_warning = ""
    if p1.threads_deferred:
        manual_run = "run it manually"
        if repo_url:
            workflow_url = f"{repo_url}/actions/workflows/{UNSUBSCRIBE_WORKFLOW}"
            manual_run = f"[{manual_run}]({workflow_url})"
        # One line per sentence, never wrapped to a column: a job summary
        # renders each newline as a line break, so a source-width wrap becomes
        # a visible one.
        backlog_warning = "\n".join([
            "> [!WARNING]",
            (
                f"> {p1.threads_deferred} eligible threads were left alone: this"
                f" run reached its cap of {p1.max_unsubscribes} unsubscribes."
            ),
            (
                f"> Raise `max-unsubscribes`, or {manual_run} with a higher cap,"
                " to clear the rest in one go."
            ),
        ])

    # Details section (includes --- separator).
    detail_table = _render_detail_table(p1.rows)
    details_section = f"---\n\n{detail_table}" if detail_table else ""

    return {
        "summary_line": summary_line,
        "batch_details_table": batch_details_table,
        "state_breakdown_table": state_breakdown_table,
        "backlog_warning": backlog_warning,
        "details_section": details_section,
    }


def _render_phase2_content(p2: Phase2Result, months: int, dry_run: bool) -> str:
    """Build the phase-2 block of the report template."""
    if p2.skipped:
        return f"> [!WARNING]\n> {p2.skip_reason}"

    summary_line = _phase_summary_line(
        len(p2.rows),
        p2.items_unsubscribed,
        p2.items_failed,
        p2.cutoff,
        months,
        dry_run,
    )

    # Search details table.
    subscribed_count = p2.items_unsubscribed + p2.items_failed
    search_table = "### \U0001f4ca Search details\n\n" + render_markdown_table(
        ("Metric", "Value"),
        (
            ("\U0001f50e Search query", f"`{p2.search_query}`"),
            ("\U0001f514 Total results", p2.items_total),
            ("\U0001f4e6 Max unsubscribes", p2.max_unsubscribes),
            ("\U0001f4cb Deferred to a later run", p2.items_deferred),
            ("\u2705 Still subscribed", subscribed_count),
            ("\u23ed\ufe0f Not subscribed", p2.items_not_subscribed),
            ("\U0001f7e1 Active since cutoff", p2.items_skipped_recent),
        ),
    )

    detail_table = _render_detail_table(p2.rows)
    parts = [summary_line, "", search_table]
    if detail_table:
        parts.extend(["", detail_table])
    return "\n".join(parts)


def render_report(result: UnsubscribeResult, repo_url: str | None = None) -> str:
    """Render a markdown report from unsubscribe results.

    Pure function that produces the same markdown structure as the
    downstream `unsubscribe.yaml` workflow's `$GITHUB_STEP_SUMMARY`. The
    repository is passed in rather than probed, so the renderer stays pure and
    the caller keeps the one environment lookup.

    :param result: Structured results from both phases.
    :param repo_url: Repository the run belongs to, linking the backlog warning
        to its workflow. Omitted, that warning renders as plain text.
    :return: Markdown report string.
    """
    return render_template(
        "unsubscribe-phase1",
        "unsubscribe-phase2",
        mode="dry-run" if result.dry_run else "live",
        phase2_content=_render_phase2_content(
            result.phase2, result.months, result.dry_run
        ),
        **_render_phase1_fragments(
            result.phase1, result.months, result.dry_run, repo_url
        ),
    )


def _run_rest_phase(
    cutoff: datetime,
    max_unsubscribes: int,
    dry_run: bool,
) -> Phase1Result:
    """Phase 1: inspect REST notification threads, unsubscribing stale ones.

    Every candidate the cutoff filter returns is inspected, and the cap bounds
    the unsubscribes alone. Inspection costs one GraphQL point per fifty
    subjects, where each unsubscribe is two REST calls and about a second of
    wall clock, so the cap sits where the cost is. Eligible threads past it are
    counted as deferred and left for the next run.

    :param cutoff: Inactivity boundary; only threads whose subject closed and
        last moved before it are acted on.
    :param max_unsubscribes: Maximum threads to unsubscribe from.
    :param dry_run: If `True`, record what would be done without acting. The
        cap still applies, so a dry run forecasts the live one.
    :return: The phase's structured result.
    """
    logging.info("Phase 1: Processing REST notification threads...")
    prefix = "[dry-run] " if dry_run else ""
    p1 = Phase1Result(cutoff=cutoff, max_unsubscribes=max_unsubscribes)

    threads = _fetch_notification_threads(cutoff)
    p1.threads_total = len(threads)
    subject_details = _fetch_subject_details([t["subject_url"] for t in threads])

    for thread in threads:
        thread_id = thread["id"]
        subject_url = thread["subject_url"]
        thread_repo = thread.get("repo", "")
        thread_title = thread.get("title", "")

        details = subject_details.get(subject_url)
        if details is None:
            p1.threads_skipped_unknown += 1
            logging.info(f"  Thread {thread_id}: subject inaccessible, skipping.")
            continue

        state = details.get("state", "unknown")
        updated_at = parse_iso_datetime(details.get("updated_at", ""))

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

        # Closed and stale: this one is eligible. Everything past the cap is
        # counted rather than acted on, so the report can say how much a later
        # run still owes.
        if p1.threads_unsubscribed + p1.threads_failed >= max_unsubscribes:
            p1.threads_deferred += 1
            continue

        # The three outcomes below differ only in the action recorded, so the
        # row is built once here.
        html_url = details.get("html_url", subject_url)
        row = partial(
            DetailRow,
            html_url=html_url,
            number=details.get("number"),
            repo=thread_repo,
            title=thread_title,
            updated_at=updated_at,
        )

        logging.info(f"  {prefix}Unsubscribing from thread {thread_id} ({html_url}).")
        if dry_run:
            p1.threads_unsubscribed += 1
            p1.rows.append(row(action=ReportAction.DRY_RUN))
            continue

        if _unsubscribe_rest_thread(str(thread_id)):
            p1.threads_unsubscribed += 1
            p1.rows.append(row(action=ReportAction.UNSUBSCRIBED))
        else:
            p1.threads_failed += 1
            p1.rows.append(row(action=ReportAction.FAILED))

    return p1


def _run_graphql_phase(
    cutoff: datetime,
    max_unsubscribes: int,
    dry_run: bool,
) -> Phase2Result:
    """Phase 2: unsubscribe from threadless subscriptions found by search.

    :param cutoff: Inactivity boundary; only items closed and last moved
        before it are acted on.
    :param max_unsubscribes: Maximum items to unsubscribe from. The search walk
        itself is bounded by GitHub's own thousand-result ceiling.
    :param dry_run: If `True`, record what would be done without acting.
    :return: The phase's structured result, marked skipped when the account
        or the search cannot be read.
    """
    logging.info("Phase 2: Processing GraphQL threadless subscriptions...")
    prefix = "[dry-run] " if dry_run else ""
    p2 = Phase2Result(cutoff=cutoff, max_unsubscribes=max_unsubscribes)

    try:
        username = _get_authenticated_username()
    except RuntimeError as exc:
        logging.warning(
            f"Failed to get authenticated username. Skipping Phase 2: {exc}"
        )
        p2.skipped = True
        p2.skip_reason = "Failed to get authenticated username. Skipping Phase 2."
        return p2

    cutoff_date = cutoff.strftime("%Y-%m-%d")
    p2.search_query = f"involves:{username} is:closed updated:<{cutoff_date}"

    try:
        for item in _iter_closed_items(p2.search_query):
            p2.items_total += 1
            node_id = item["id"]
            repo = item.get("repository", {}).get("nameWithOwner", "unknown")
            number = item.get("number")

            # Filter: only act on items the user is subscribed to.
            if item.get("viewerSubscription") != "SUBSCRIBED":
                p2.items_not_subscribed += 1
                continue

            # Parse updatedAt and url from GraphQL result.
            gql_updated_at = parse_iso_datetime(item.get("updatedAt", ""))

            # Re-validate staleness client-side, mirroring phase 1: the
            # search query's `updated:<` filter is day-granular and served
            # by GitHub's search index, which can lag. Trusting it alone
            # could unsubscribe an item phase 1's stricter check would
            # keep. An unparsable timestamp is not a green light either.
            if gql_updated_at is None or gql_updated_at >= cutoff:
                p2.items_skipped_recent += 1
                continue

            if p2.items_unsubscribed + p2.items_failed >= max_unsubscribes:
                p2.items_deferred += 1
                continue

            row = partial(
                DetailRow,
                html_url=item.get("url", ""),
                number=number,
                repo=repo,
                title=item.get("title", ""),
                updated_at=gql_updated_at,
            )

            logging.info(f"  {prefix}Unsubscribing from {repo}#{number} (GraphQL).")
            if dry_run:
                p2.items_unsubscribed += 1
                p2.rows.append(row(action=ReportAction.DRY_RUN))
                continue

            if _graphql_unsubscribe(node_id):
                p2.items_unsubscribed += 1
                p2.rows.append(row(action=ReportAction.UNSUBSCRIBED))
            else:
                p2.items_failed += 1
                p2.rows.append(row(action=ReportAction.FAILED))
    except RuntimeError as exc:
        logging.warning(
            "GraphQL search failed. Phase 2 may be incomplete. Fine-grained PATs may "
            f"not support GraphQL search: {exc}"
        )
        p2.skipped = True
        p2.skip_reason = (
            "GraphQL search failed. Fine-grained PATs may not support GraphQL search."
        )

    return p2


def unsubscribe_threads(
    months: int,
    max_unsubscribes: int,
    dry_run: bool,
) -> UnsubscribeResult:
    """Unsubscribe from closed, inactive notification threads.

    Runs two phases, each behind its own runner:

    1. **REST notification threads** ({func}`_run_rest_phase`) — Fetches
       notification threads, inspects each subject for closed + stale status,
       and unsubscribes.
    2. **GraphQL threadless subscriptions** ({func}`_run_graphql_phase`) —
       Searches for closed issues/PRs the user is involved in and
       unsubscribes via mutation.

    :param months: Inactivity threshold in months.
    :param max_unsubscribes: Maximum unsubscribes per phase.
    :param dry_run: If `True`, report what would be done without acting.
    :return: Structured results from both phases.
    """
    cutoff = _compute_cutoff(months)
    logging.info(f"Cutoff date: {cutoff.strftime('%Y-%m-%d')} ({months} months ago).")
    return UnsubscribeResult(
        dry_run=dry_run,
        months=months,
        phase1=_run_rest_phase(cutoff, max_unsubscribes, dry_run),
        phase2=_run_graphql_phase(cutoff, max_unsubscribes, dry_run),
    )
