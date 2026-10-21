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

"""Read and cancel GitHub Actions workflow runs.

Every call to the runs API goes through this module: the listings
{mod}`~repomatic.github.ci_status` and {mod}`~repomatic.github.job_timings`
read, and the cancellation sweep behind the `cancel-runs` command. The
repository is resolved by the `gh` CLI from `GH_REPO` or the checkout.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import quote, urlencode

from ..release.protocol import RELEASE_PUSH_PREFIXES
from .gh import GhCommandError, gh_api_pages, gh_json, run_gh_command

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Sequence
    from typing import Any


RUN_LISTING_WINDOW = timedelta(days=90)
"""How far back every runs listing here reaches, through its `created` filter.

```{warning}
GitHub's runs listings can answer with a stale snapshot: a page weeks or
months old, presented as the newest runs, on a branch that ran every hour.
`gh run list` reads the same endpoints and carries the same risk. A listing
that sends a `created` filter has not been seen to do it, so every listing
here sends one.
```

Ninety days holds the few runs any caller needs on an active branch. A
workflow idle for longer reads as having no run.
"""

RUNS_PAGE_SIZE = 100
"""Runs per page of a listing, the most the REST API serves."""

LIVE_RUN_STATUSES = ("in_progress", "queued")
"""Run statuses {func}`cancel_superseded_runs` sweeps: the ones still to finish."""

_RUNS_ENDPOINT = "repos/{owner}/{repo}/actions/runs"
"""The repository-wide runs listing, with the placeholders `gh api` fills in."""

ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
"""A terminal control sequence, as a CI log colours its lines with."""


def runs_created_since() -> str:
    """The `created` filter a runs listing sends: runs from the last window.

    See {data}`RUN_LISTING_WINDOW` for why every listing carries one.
    """
    start = datetime.now(UTC) - RUN_LISTING_WINDOW
    return f">={start:%Y-%m-%d}"


def workflow_runs(
    workflow: str,
    branch: str,
    *,
    status: str = "",
    limit: int = 1,
    strict: bool = False,
) -> list[dict[str, Any]]:
    """The newest runs of *workflow* on *branch*, newest first.

    Reads the workflow's runs endpoint with a {func}`runs_created_since`
    filter, never `gh run list`: see {data}`RUN_LISTING_WINDOW`. Pages are read
    one at a time, so a listing stops at *limit* instead of walking the window.

    :param workflow: Workflow filename, like `tests.yaml`.
    :param branch: Branch whose runs to list.
    :param status: A run status or conclusion to keep, like `success`. Empty
        keeps every run.
    :param limit: How many runs to return at most.
    :param strict: Raise the error of a `gh` call that could not run or did
        not answer JSON, instead of returning the runs read so far.
    :return: Run payloads as the REST API returns them.
    """
    per_page = min(limit, RUNS_PAGE_SIZE)
    params = {
        "branch": branch,
        "created": runs_created_since(),
        "per_page": per_page,
    }
    if status:
        params["status"] = status
    endpoint = f"repos/{{owner}}/{{repo}}/actions/workflows/{quote(workflow)}/runs"
    runs: list[dict[str, Any]] = []
    page = 1
    while len(runs) < limit:
        payload = gh_json(
            ["api", f"{endpoint}?{urlencode({**params, 'page': page})}"],
            soft=not strict,
        )
        batch = payload.get("workflow_runs") if isinstance(payload, dict) else None
        if not batch:
            break
        runs.extend(batch)
        if len(batch) < per_page:
            break
        page += 1
    return runs[:limit]


def commit_runs(sha: str) -> list[dict[str, Any]]:
    """Every run GitHub created for commit *sha*, newest first.

    One page of the largest size covers every run a single commit carries.

    :param sha: Full commit SHA.
    :return: Run payloads as the REST API returns them.
    :raises GhCommandError: When the `gh` call fails.
    :raises GhOutputError: When the answer is not JSON.
    """
    query = urlencode({"head_sha": sha, "per_page": RUNS_PAGE_SIZE})
    payload = gh_json(["api", f"{_RUNS_ENDPOINT}?{query}"])
    if not isinstance(payload, dict):
        return []
    return payload.get("workflow_runs") or []


def read_run(
    run_id: int | str, fields: Sequence[str], *, soft: bool = False
) -> dict[str, Any] | None:
    """Read one run by ID, with the `gh run view --json` fields named.

    :param run_id: The run's numeric ID.
    :param fields: The JSON fields to request, like `jobs` or `headSha`.
    :param soft: Return `None` when the call fails, as
        {func}`~repomatic.github.gh.gh_json` does.
    :return: The run payload, or `None` when the answer is not a JSON object.
    :raises GhCommandError: When the `gh` call fails and *soft* is not set.
    :raises GhOutputError: When the answer is not JSON and *soft* is not set.
    """
    detail = gh_json(
        ["run", "view", str(run_id), "--json", ",".join(fields)], soft=soft
    )
    return detail if isinstance(detail, dict) else None


def job_log(job_id: int | str) -> str:
    """The log of one job, control sequences stripped.

    Readable the moment the job settles, while the rest of its run still goes:
    the run-scoped `gh run view --log-failed` answers nothing until the whole
    run is terminal. `gh` refuses to print a body holding terminal escape
    sequences, which a CI log always does, unless told to, and the colour
    codes are then stripped so a reader greps plain text.

    :param job_id: The job's numeric ID, `databaseId` in a run's job listing.
    :return: The log, one line per step line, timestamps kept.
    :raises GhCommandError: When the `gh` call fails.
    """
    text = run_gh_command([
        "api",
        f"repos/{{owner}}/{{repo}}/actions/jobs/{job_id}/logs",
        "--allow-escape-sequences",
    ])
    return ANSI_ESCAPE_RE.sub("", text)


def live_runs(branch: str) -> list[dict[str, Any]]:
    """The in-progress and queued runs of *branch*, every page of them.

    A live run is recent by definition, so the listing window drops none.

    :param branch: Head branch whose runs to list.
    :return: Run payloads, the in-progress ones first.
    :raises GhCommandError: When a listing fails.
    :raises GhOutputError: When a listing does not answer JSON.
    """
    runs: list[dict[str, Any]] = []
    for status in LIVE_RUN_STATUSES:
        query = urlencode({
            "branch": branch,
            "status": status,
            "created": runs_created_since(),
            "per_page": RUNS_PAGE_SIZE,
        })
        runs.extend(gh_api_pages(f"{_RUNS_ENDPOINT}?{query}", key="workflow_runs"))
    return runs


def cancel_superseded_runs(branch: str, current_run_id: str) -> int:
    """Cancel the in-progress and queued workflow runs of *branch*.

    Backs the `cancel-runs` command, fired when a pull request closes:
    GitHub's `concurrency` mechanism only cancels a run when a *new* run
    enters the same group, and closing a PR fires no such run, so the
    branch's live runs would otherwise burn CI minutes to completion.

    Every listed run is cancelled except two: *current_run_id* (the
    cancelling run itself), and any run whose head commit carries one of the
    {data}`~repomatic.release.protocol.RELEASE_PUSH_PREFIXES`. A run that fails
    to cancel (already finished, insufficient token scope) is logged and
    skipped so one straggler never aborts the sweep.

    ```{caution}
    The release guard is what makes this safe to point at a default branch.
    The concurrency groups of `release.yaml` and `tests.yaml` already spare a
    release run from *automatic* supersession, but a sweep like this one
    enters no concurrency group, so nothing else would stop it from killing
    the matrix that publishes a release. Cancelling a release run mid-flight
    costs the version its binaries permanently, since publishing locks the
    asset list (see the `publish-release` job of `_release-engine.yaml`).
    ```

    :param branch: Head branch whose runs to cancel.
    :param current_run_id: Run ID to spare (the caller's own run).
    :return: Number of runs cancelled.
    """
    cancelled = 0
    # A run that moves from queued to in progress between the two listings
    # appears in both.
    seen = {current_run_id}
    for run in live_runs(branch):
        run_id = str(run.get("id", ""))
        display_title = str(run.get("display_title") or "")
        if not run_id or run_id in seen:
            continue
        seen.add(run_id)
        if display_title.startswith(RELEASE_PUSH_PREFIXES):
            logging.info(
                f"Sparing release run {run_id}: {display_title!r} names a release push."
            )
            continue
        logging.info(f"Cancelling run {run_id} (status: {run.get('status')}).")
        try:
            run_gh_command([
                "api",
                "--method",
                "POST",
                f"{_RUNS_ENDPOINT}/{run_id}/cancel",
            ])
        except GhCommandError as exc:
            logging.warning(f"Failed to cancel run {run_id}: {exc}")
            continue
        cancelled += 1
    return cancelled
