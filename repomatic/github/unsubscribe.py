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

1. **REST notification threads**: fetches the Issue/PullRequest notification
   threads last moved before the cutoff via `/notifications`, resolves each
   subject in batched GraphQL lookups, and unsubscribes the closed, stale ones
   via `DELETE` + `PATCH`.

2. **GraphQL threadless subscriptions**: searches for closed issues/PRs the
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
from datetime import UTC, datetime
from functools import partial
from urllib.parse import urlencode

import arrow

from ..config import Config
from ..humanize import format_elapsed, parse_iso_datetime
from ..tabular import ReportAction, render_markdown_table
from ..templating import render_template
from .gh import gh_api_pages, gh_graphql, gh_json, iter_graphql_nodes, run_gh_command

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from typing import Any

GRAPHQL_PAGE_SIZE = 25
"""Per-page count for GraphQL search results."""

NOTIFICATION_PAGE_SIZE = 50
"""Per-page count for REST `/notifications` results."""

NOTIFICATION_SUBJECT_TYPES = frozenset({"Issue", "PullRequest"})
"""Notification subject types to process."""

SUBJECT_BATCH_SIZE = 50
"""Subjects looked up per GraphQL round trip.

Looked up one at a time, each thread costs a REST detail call. A batched
lookup answers 50 in one request for a single rate-limit point, so a 113-thread
pool costs three requests instead of 113.
"""

SUBJECT_STATES = {"OPEN": "open", "CLOSED": "closed", "MERGED": "closed"}
"""GraphQL subject states, mapped onto the REST spelling the phase compares.

REST reports a merged pull request as `closed`, where GraphQL distinguishes
`MERGED`. Collapsing the two here keeps one vocabulary in the caller, and keeps
the batched lookup a drop-in for the one-subject REST lookup,
`_get_thread_details`.
"""

REPORT_TEMPLATE = "unsubscribe-report"
"""The template the report of both phases renders through."""

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
"""Workflow file the backlog warning links to for a manual run with a higher cap.

The report renders inside that workflow's own run, so the reader is one click
from the `Run workflow` form the warning tells them to use. Kept equal to the
filename {data}`repomatic.components.COMPONENTS` ships, since a link to a
workflow a repository does not have is worse than no link.
"""


@dataclass(frozen=True)
class DetailRow:
    """Per-item detail for the markdown report table."""

    action: ReportAction
    """What the run did to the item."""

    html_url: str
    """Browser URL of the issue or pull request."""

    number: int | None
    """Issue or pull request number, `None` when unknown."""

    repo: str
    """Repository the item belongs to, in `owner/name` form."""

    title: str
    """Title of the issue or pull request."""

    updated_at: datetime | None
    """When the item last moved, `None` when unknown."""


@dataclass
class PhaseResult:
    """Counts and rows one phase accumulates."""

    cutoff: datetime | None = None
    """Inactivity boundary the phase compared against."""

    max_unsubscribes: int = 0
    """Most unsubscribes the phase may make."""

    total: int = 0
    """Candidates the phase inspected."""

    unsubscribed: int = 0
    """Items unsubscribed from, or that a dry run would unsubscribe from."""

    failed: int = 0
    """Eligible items whose unsubscribe failed."""

    deferred: int = 0
    """Eligible items left for a later run because the cap was reached."""

    skipped_recent: int = 0
    """Closed items that moved since the cutoff."""

    rows: list[DetailRow] = field(default_factory=list)
    """One row per item acted on."""

    skipped: bool = False
    """Whether the phase could not run at all."""

    skip_reason: str = ""
    """Why the phase could not run, for the report."""

    def skip(self, reason: str, error: Exception) -> None:
        """Mark the phase as unable to run, for the reason the report gives.

        :param reason: What the report says, one sentence.
        :param error: The failure behind it, logged rather than reported.
        """
        logging.warning(f"{reason} {error}")
        self.skipped = True
        self.skip_reason = reason

    def act(
        self,
        label: str,
        make_row: Callable[..., DetailRow],
        unsubscribe: Callable[[], bool],
        *,
        dry_run: bool,
    ) -> None:
        """Unsubscribe from one eligible item, unless the cap is reached.

        An item past the cap is counted as deferred rather than acted on, so the
        report can say how much a later run still owes.

        :param label: How the log line names the item.
        :param make_row: Builds the item's row from the `action` it records.
        :param unsubscribe: Performs the unsubscribe, returning its success.
        :param dry_run: Record what would be done without acting.
        """
        if self.unsubscribed + self.failed >= self.max_unsubscribes:
            self.deferred += 1
            return
        logging.info(f"  {'[dry-run] ' if dry_run else ''}Unsubscribing from {label}.")
        if dry_run:
            action = ReportAction.DRY_RUN
        elif unsubscribe():
            action = ReportAction.UNSUBSCRIBED
        else:
            self.failed += 1
            self.rows.append(make_row(action=ReportAction.FAILED))
            return
        self.unsubscribed += 1
        self.rows.append(make_row(action=action))


@dataclass
class Phase1Result(PhaseResult):
    """Accumulated counts and details from REST notification phase."""

    newest_updated: datetime | None = None
    """Latest activity among the inspected subjects."""

    oldest_updated: datetime | None = None
    """Earliest activity among the inspected subjects."""

    skipped_open: int = 0
    """Subjects still open."""

    skipped_unknown: int = 0
    """Subjects whose state or activity could not be read."""


@dataclass
class Phase2Result(PhaseResult):
    """Accumulated counts and details from GraphQL threadless phase."""

    not_subscribed: int = 0
    """Search results the user is not subscribed to."""

    search_query: str = ""
    """The search the phase ran."""


@dataclass
class UnsubscribeResult:
    """Accumulated results from both unsubscribe phases."""

    dry_run: bool = False
    """Whether the run only reported."""

    months: int = Config.notification_months
    """Inactivity threshold of the run, in months."""

    phase1: Phase1Result = field(default_factory=Phase1Result)
    """Results of the REST notification phase."""

    phase2: Phase2Result = field(default_factory=Phase2Result)
    """Results of the GraphQL threadless phase."""


def _compute_cutoff(months: int) -> datetime:
    """Compute a cutoff datetime by subtracting `months` from now.

    arrow's `shift` clamps the day for us (subtracting 1 month from March 31
    yields February 28/29), and the library is already loaded for the report's
    `.humanize()` phrasing.

    :param months: Number of months to subtract.
    :return: Timezone-aware UTC datetime.
    """
    now = datetime.now(UTC)
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
    worth a subject lookup. Unfiltered, the list also holds every thread still
    active: a probe measured the filter cutting 1637 threads to 113.

    ```{note}
    `before` is a pre-filter, never a verdict, and the same probe showed it
    reads a clock of its own: of 113 threads it returned against a 90-day
    cutoff, 104 predated that cutoff by `updated_at` and 112 by `last_read_at`.
    So it over-returns rather than under-returns, which is the safe direction.
    {func}`_run_rest_phase` decides eligibility per thread, on the subject's own
    state and `updated_at` as {func}`_fetch_subject_details` resolves them.
    ```

    :param cutoff: Inactivity boundary, passed to the API as `before`.
    :return: Every candidate the filter returned, oldest first. Each thread
        dict contains `id`, `updated_at`, `subject_url`, `subject_type`,
        `repo`, `title`.
    :raises RuntimeError: When the notifications cannot be read.
    """
    query = urlencode({
        "all": "true",
        "before": f"{cutoff.astimezone(UTC):%Y-%m-%dT%H:%M:%SZ}",
        "per_page": NOTIFICATION_PAGE_SIZE,
    })
    threads = []
    for notification in gh_api_pages(f"/notifications?{query}"):
        if not isinstance(notification, dict):
            continue
        subject = notification.get("subject") or {}
        if subject.get("type") not in NOTIFICATION_SUBJECT_TYPES:
            continue
        threads.append({
            "id": notification.get("id"),
            "updated_at": notification.get("updated_at"),
            "repo": (notification.get("repository") or {}).get("full_name", ""),
            "subject_type": subject["type"],
            "subject_url": subject.get("url"),
            "title": subject.get("title", ""),
        })

    # Sorted here rather than trusted from the response. The endpoint documents
    # itself as "sorted by most recently updated", but a probe against 1637 real
    # threads found the list ordered by neither `updated_at` nor thread id, in
    # either direction. Oldest first spends the unsubscribe cap on the deepest
    # end of the backlog, so a run that cannot clear the whole pool leaves the
    # next one where it stopped.
    threads.sort(key=lambda thread: thread.get("updated_at") or "")
    return threads


def _get_thread_details(subject_url: str) -> dict[str, Any] | None:
    """Fetch details for a notification thread's subject.

    :param subject_url: The API URL of the thread's subject (issue or PR).
    :return: Dict with `state`, `updated_at`, `html_url`, `number`,
        or `None` if the subject is inaccessible.
    """
    details = gh_json(
        ["api", subject_url, "--jq", "{state, updated_at, html_url, number}"],
        soft=True,
    )
    if not isinstance(details, dict):
        logging.debug(f"Subject inaccessible or malformed: {subject_url}")
    return details


def _subject_query(targets: dict[str, tuple[str, str, str]]) -> str:
    """Render one aliased GraphQL query covering every subject in *targets*.

    :param targets: Alias to `(owner, repo, number)`.
    :return: A query selecting state and timestamps for each alias.
    """
    fields = "state updatedAt number url"
    blocks = []
    for alias, (owner, repo, number) in targets.items():
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


def _batch_subject_details(
    subject_urls: list[str],
) -> dict[str, dict[str, Any] | None]:
    """Look up many subjects in one GraphQL request.

    The batched counterpart of {func}`_get_thread_details`, returning the same
    four fields under the same names so either can feed the phase.

    GitHub answers a query that fails in part with the subjects it could
    resolve, and `null` for the others, so a partial answer is kept. A `null`
    is final: the subject is inaccessible (a repository the token cannot see,
    a deleted issue), and a REST call would answer the same.

    :param subject_urls: Subject URLs from the notification list.
    :return: Subject URL to its detail dict, or to `None` for a subject GraphQL
        answered `null` for. A subject absent from the answer, or whose URL
        this code cannot parse, is absent here too.
    :raises RuntimeError: When the `gh` invocation fails with no answer at
        all, leaving the caller to fall back.
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
    data = gh_graphql(_subject_query(targets), partial=True) or {}

    details: dict[str, dict[str, Any] | None] = {}
    for alias in targets:
        if alias not in data:
            continue
        url = subject_urls[int(alias[1:])]
        node = (data[alias] or {}).get("issueOrPullRequest")
        details[url] = (
            {
                "state": SUBJECT_STATES.get(node.get("state", ""), "unknown"),
                "updated_at": node.get("updatedAt", ""),
                "html_url": node.get("url", ""),
                "number": node.get("number"),
            }
            if node
            else None
        )
    return details


def _fetch_subject_details(subject_urls: list[str]) -> dict[str, dict[str, Any]]:
    """Resolve every subject's state, batched, with a per-subject fallback.

    Defence in depth rather than a bet on one API: a batch that fails for any
    reason (a `gh` error, a malformed envelope) is re-read one REST call per
    subject through {func}`_get_thread_details`, and so is a subject URL this
    code cannot parse. The run then costs one call per subject instead of
    reporting a pool of inaccessible subjects. A subject the batch answered
    `null` for is not re-read.

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
            pending.extend(chunk)
            continue
        details.update(
            (url, detail) for url, detail in resolved.items() if detail is not None
        )
        pending.extend(url for url in chunk if url not in resolved)

    for url in pending:
        single = _get_thread_details(url)
        if single is not None:
            details[url] = single
    return details


def _unsubscribe_rest_thread(thread_id: str) -> bool:
    """Unsubscribe from a notification thread and mark it read.

    Performs two API calls:

    1. `DELETE /notifications/threads/{id}/subscription`
    2. `PATCH /notifications/threads/{id}`

    ```{note}
    The second call marks the thread read rather than done. `DELETE
    /notifications/threads/{id}` marks it done instead, for the same two calls,
    and the tempting reason to prefer it is that a done thread might leave the
    candidate pool for good. It does not. Marking one done left it in
    `GET /notifications?all=true` and in the `before`-filtered pool, with the
    list the same size either side (measured against 1640 real threads,
    2026-09-14). `all=true` answers with a thread whatever its inbox state, so
    done costs the same, changes nothing here, and no endpoint undoes it.
    ```

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
        gh_graphql(UNSUBSCRIBE_MUTATION, id=node_id)
    except RuntimeError:
        logging.warning(f"GraphQL unsubscribe failed for node {node_id}.")
        return False
    return True


def _render_detail_table(rows: list[DetailRow], now: datetime | None = None) -> str:
    """Render a details table with header and data rows.

    :param rows: Detail rows to render.
    :param now: The instant the last activity is measured from, the current
        one when `None`.
    :return: Details heading and table, or empty string if no rows.
    """
    if not rows:
        return ""
    reference = now or datetime.now(UTC)
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
                format_elapsed(row.updated_at, reference) if row.updated_at else "-",
                row.action.value,
            )
            for row in rows
        ),
    )
    return f"### \U0001f4dd Details\n\n{table}"


def _phase_summary_line(phase: PhaseResult, months: int, dry_run: bool) -> str:
    """One phase's summary line, with identical wording for both phases."""
    cutoff_str = phase.cutoff.isoformat() if phase.cutoff else "-"
    if dry_run:
        return (
            f"\U0001f50d **Candidates found:** {len(phase.rows)}"
            f" \u2014 cutoff: `{cutoff_str}`"
            f" (inactive for more than {months} months, dry-run)"
        )
    return (
        f"\U0001f515 **Unsubscribed:** {phase.unsubscribed}"
        f" | \u26a0\ufe0f **Failed:** {phase.failed}"
        f" \u2014 cutoff: `{cutoff_str}`"
        f" (inactive for more than {months} months)"
    )


def _skip_warning(phase: PhaseResult) -> str:
    """The warning a skipped phase renders in place of its content."""
    return f"> [!WARNING]\n> {phase.skip_reason}"


def _render_phase1_content(
    p1: Phase1Result, months: int, dry_run: bool, repo_url: str | None = None
) -> str:
    """Build the phase-1 block of the report.

    Every table is rendered whole here, never as rows spliced into a table the
    template opens, which also keeps the markup out of reach of the formatter
    that owns the template file.

    :param repo_url: Repository the run belongs to, used to link the backlog
        warning to its workflow. The warning degrades to plain text without it.
    """
    if p1.skipped:
        return _skip_warning(p1)

    cutoff_str = p1.cutoff.isoformat() if p1.cutoff else "-"
    oldest_str = p1.oldest_updated.isoformat() if p1.oldest_updated else "-"
    newest_str = p1.newest_updated.isoformat() if p1.newest_updated else "-"
    notification_details = render_markdown_table(
        ("Metric", "Value"),
        (
            ("\U0001f514 Threads before cutoff", p1.total),
            ("\U0001f4e6 Max unsubscribes", p1.max_unsubscribes),
            ("\U0001f4cb Deferred to a later run", p1.deferred),
            ("\u23ea Oldest activity", oldest_str),
            ("\u23e9 Newest activity", newest_str),
            ("\u2702\ufe0f Cutoff", cutoff_str),
        ),
    )
    state_breakdown = render_markdown_table(
        ("State", "Count"),
        (
            ("\U0001f7e2 Open", p1.skipped_open),
            ("\U0001f7e1 Closed (active since cutoff)", p1.skipped_recent),
            ("\U0001f534 Closed (inactive, eligible)", p1.unsubscribed + p1.failed),
            ("\u26aa Unknown", p1.skipped_unknown),
        ),
    )
    parts = [
        _phase_summary_line(p1, months, dry_run),
        "### \U0001f4ca Notification details",
        notification_details,
        "### \U0001f3f7\ufe0f Breakdown by state",
        state_breakdown,
    ]

    if p1.deferred:
        manual_run = "run it manually"
        if repo_url:
            workflow_url = f"{repo_url}/actions/workflows/{UNSUBSCRIBE_WORKFLOW}"
            manual_run = f"[{manual_run}]({workflow_url})"
        # One line per sentence, never wrapped to a column: a job summary
        # renders each newline as a line break, so a source-width wrap becomes
        # a visible one.
        parts.append(
            "\n".join([
                "> [!WARNING]",
                (
                    f"> {p1.deferred} eligible threads were left alone: this"
                    f" run reached its cap of {p1.max_unsubscribes} unsubscribes."
                ),
                (
                    f"> Raise `max-unsubscribes`, or {manual_run} with a higher cap,"
                    " to clear the rest in one go."
                ),
            ])
        )

    detail_table = _render_detail_table(p1.rows)
    if detail_table:
        parts.extend(["---", detail_table])
    return "\n\n".join(parts)


def _render_phase2_content(p2: Phase2Result, months: int, dry_run: bool) -> str:
    """Build the phase-2 block of the report."""
    if p2.skipped:
        return _skip_warning(p2)

    search_table = "### \U0001f4ca Search details\n\n" + render_markdown_table(
        ("Metric", "Value"),
        (
            ("\U0001f50e Search query", f"`{p2.search_query}`"),
            ("\U0001f514 Total results", p2.total),
            ("\U0001f4e6 Max unsubscribes", p2.max_unsubscribes),
            ("\U0001f4cb Deferred to a later run", p2.deferred),
            ("\u2705 Still subscribed", p2.unsubscribed + p2.failed),
            ("\u23ed\ufe0f Not subscribed", p2.not_subscribed),
            ("\U0001f7e1 Active since cutoff", p2.skipped_recent),
        ),
    )

    detail_table = _render_detail_table(p2.rows)
    parts = [_phase_summary_line(p2, months, dry_run), "", search_table]
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
        REPORT_TEMPLATE,
        mode="dry-run" if result.dry_run else "live",
        phase1_content=_render_phase1_content(
            result.phase1, result.months, result.dry_run, repo_url
        ),
        phase2_content=_render_phase2_content(
            result.phase2, result.months, result.dry_run
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
    :return: The phase's structured result, marked skipped when the
        notifications cannot be read.
    """
    logging.info("Phase 1: Processing REST notification threads...")
    p1 = Phase1Result(cutoff=cutoff, max_unsubscribes=max_unsubscribes)

    try:
        threads = _fetch_notification_threads(cutoff)
    except RuntimeError as exc:
        p1.skip("Failed to fetch notification threads. Skipping Phase 1.", exc)
        return p1
    p1.total = len(threads)
    subject_details = _fetch_subject_details([t["subject_url"] for t in threads])

    for thread in threads:
        thread_id = thread["id"]
        subject_url = thread["subject_url"]

        details = subject_details.get(subject_url)
        if details is None:
            p1.skipped_unknown += 1
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
            p1.skipped_unknown += 1
            logging.info(f"  Thread {thread_id}: state={state}, skipping.")
            continue

        if state != "closed":
            p1.skipped_open += 1
            logging.info(f"  Thread {thread_id}: state={state}, skipping.")
            continue

        if updated_at >= cutoff:
            p1.skipped_recent += 1
            logging.info(f"  Thread {thread_id}: updated recently, skipping.")
            continue

        html_url = details.get("html_url", subject_url)
        p1.act(
            f"thread {thread_id} ({html_url})",
            partial(
                DetailRow,
                html_url=html_url,
                number=details.get("number"),
                repo=thread.get("repo", ""),
                title=thread.get("title", ""),
                updated_at=updated_at,
            ),
            partial(_unsubscribe_rest_thread, str(thread_id)),
            dry_run=dry_run,
        )

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
    p2 = Phase2Result(cutoff=cutoff, max_unsubscribes=max_unsubscribes)

    try:
        username = _get_authenticated_username()
    except RuntimeError as exc:
        p2.skip("Failed to get authenticated username. Skipping Phase 2.", exc)
        return p2

    cutoff_date = cutoff.strftime("%Y-%m-%d")
    p2.search_query = f"involves:{username} is:closed updated:<{cutoff_date}"

    try:
        for item in _iter_closed_items(p2.search_query):
            p2.total += 1
            repo = item.get("repository", {}).get("nameWithOwner", "unknown")
            number = item.get("number")

            # Filter: only act on items the user is subscribed to.
            if item.get("viewerSubscription") != "SUBSCRIBED":
                p2.not_subscribed += 1
                continue

            gql_updated_at = parse_iso_datetime(item.get("updatedAt", ""))

            # Re-validate staleness client-side, mirroring phase 1: the
            # search query's `updated:<` filter is day-granular and served
            # by GitHub's search index, which can lag. Trusting it alone
            # could unsubscribe an item phase 1's stricter check would
            # keep. An unparsable timestamp is not a green light either.
            if gql_updated_at is None or gql_updated_at >= cutoff:
                p2.skipped_recent += 1
                continue

            p2.act(
                f"{repo}#{number} (GraphQL)",
                partial(
                    DetailRow,
                    html_url=item.get("url", ""),
                    number=number,
                    repo=repo,
                    title=item.get("title", ""),
                    updated_at=gql_updated_at,
                ),
                partial(_graphql_unsubscribe, item["id"]),
                dry_run=dry_run,
            )
    except RuntimeError as exc:
        p2.skip(
            "GraphQL search failed. Fine-grained PATs may not support GraphQL search.",
            exc,
        )

    return p2


def unsubscribe_threads(
    months: int,
    max_unsubscribes: int,
    dry_run: bool,
) -> UnsubscribeResult:
    """Unsubscribe from closed, inactive notification threads.

    Runs two phases, each behind its own runner:

    1. **REST notification threads** (`_run_rest_phase`): fetches
       notification threads, inspects each subject for closed + stale status,
       and unsubscribes.
    2. **GraphQL threadless subscriptions** (`_run_graphql_phase`):
       searches for closed issues/PRs the user is involved in and
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
