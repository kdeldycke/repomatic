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

"""Measure how long each runner image actually takes, from finished runs.

Runner selection is supposed to rest on measurement rather than on architecture
folklore, and this repository has already paid for the alternative: the lean
`ubuntu-slim` image held every mechanical job for years on a benchmark that
timed only the tool pass, where it looked near-parity. Timed end to end it was
20-56% *slower*, because most of the difference sat in checkout and install,
which that measurement could not see.

The counter-measure was a prose warning plus a two-command `gh` recipe and a
median taken by hand. This module is that recipe, which makes the rule
mechanical rather than advisory: the jobs API reports `startedAt` and
`completedAt`, so a duration read from it is *whole-job by construction* and
the mistake above is not expressible.

```{note} Why the job name, and not a second API call
Attributing a duration to an image needs no extra request, because the matrix
job names already carry it (`⁉️ ubuntu-26.04 / py3.15-dev`). Matching them
against {data}`~repomatic.lint_repo.KNOWN_RUNNERS` is the same technique
{func}`~repomatic.lint_repo.literal_runners` uses on `runs-on:` values, and it
degrades honestly: a job whose name carries no known image is reported under
{data}`UNATTRIBUTED` rather than guessed at.
```

```{caution}
These numbers are a sample of a shared, noisy fleet, not a benchmark. A cold
image, a queue stall or a flaky network inflates a single cell, which is why
the report is a median across several runs rather than a mean of one. Read a
gap of a few percent as noise and act only on the systematic ones.
```
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass

from ..dep_report import parse_iso_datetime
from ..lint_repo import KNOWN_RUNNERS
from ..tabular import render_markdown_table
from .gh import gh_api_json

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

UNATTRIBUTED = "(no image in job name)"
"""Bucket for a job whose name matches no known runner image.

Non-matrix jobs land here by design: they carry no image in their name because
they never had a choice of one. Keeping them visible rather than dropping them
is what stops the report reading as though it covered the whole workflow.
"""

JOB_TIMINGS_HEADER_DEFS: tuple[tuple[str, str], ...] = (
    ("Runner", "runner"),
    ("Jobs", "jobs"),
    ("Median", "median"),
    ("Slowest job", "slowest-job"),
    ("Slowest", "slowest"),
)
"""Column definitions for the `job-timings` table."""


@dataclass(frozen=True)
class JobTiming:
    """One finished job, and how long it occupied a runner."""

    name: str
    """The job's name, glyph and matrix cell included."""

    runner: str
    """Runner image matched out of {attr}`name`, or {data}`UNATTRIBUTED`."""

    seconds: float
    """Whole-job wall-clock: the `completedAt` minus `startedAt` delta.

    Deliberately not the compute time. This is what the run costs in billed
    minutes and in wall-clock waiting, and it is the figure that settled the
    `ubuntu-slim` question when the tool-pass figure could not.
    """


def match_runner(job_name: str) -> str:
    """Attribute a job to the runner image named in it.

    :param job_name: Job name as GitHub reports it.
    :return: The matched image, or {data}`UNATTRIBUTED`.
    """
    # Longest first, so `ubuntu-26.04-arm` is never shadowed by `ubuntu-26.04`.
    for image in sorted(KNOWN_RUNNERS, key=len, reverse=True):
        if image in job_name:
            return image
    return UNATTRIBUTED


def _duration(job: dict) -> float | None:
    """Whole-job wall-clock in seconds, or `None` when it cannot be read.

    A job that never started (cancelled in the queue) carries no usable pair,
    and a still-running one has no end: both are skipped rather than counted
    as zero, which would drag a median toward a runner's queue behaviour
    instead of its speed. Timestamps go through the package-wide
    {func}`~repomatic.dep_report.parse_iso_datetime`, which reads the `Z`
    suffix GitHub always writes on every supported Python.
    """
    started, completed = job.get("startedAt"), job.get("completedAt")
    if not started or not completed:
        return None
    started_at = parse_iso_datetime(started)
    completed_at = parse_iso_datetime(completed)
    if started_at is None or completed_at is None:
        logging.warning("Unparsable job timestamps: %r, %r", started, completed)
        return None
    seconds = (completed_at - started_at).total_seconds()
    return seconds if seconds > 0 else None


def fetch_job_timings(
    workflow: str, branch: str = "main", limit: int = 5
) -> list[JobTiming]:
    """Read finished job durations from the most recent successful runs.

    Only successful runs are sampled. A failed run's jobs stop early, so their
    durations measure where the failure landed rather than what the image
    costs, and a cancelled matrix reports whatever fraction ran before the
    cancellation swept it.

    :param workflow: Workflow filename, like `tests.yaml`.
    :param branch: Branch whose runs to sample.
    :param limit: How many successful runs to sample. A median over several is
        what smooths a queue stall into noise.
    :return: One {class}`JobTiming` per finished job across the sampled runs.
    """
    listing = gh_api_json([
        "run",
        "list",
        f"--workflow={workflow}",
        f"--branch={branch}",
        "--status=success",
        f"--limit={limit}",
        "--json",
        "databaseId",
    ])
    if not isinstance(listing, list) or not listing:
        return []

    timings: list[JobTiming] = []
    for entry in listing:
        detail = gh_api_json([
            "run",
            "view",
            str(entry["databaseId"]),
            "--json",
            "jobs",
        ])
        if not isinstance(detail, dict):
            continue
        for job in detail.get("jobs") or []:
            seconds = _duration(job)
            if seconds is None:
                continue
            name = job.get("name") or ""
            timings.append(JobTiming(name, match_runner(name), seconds))
    return timings


@dataclass(frozen=True)
class RunnerReport:
    """Aggregated timings for one runner image."""

    runner: str
    job_count: int
    median_seconds: float
    slowest_job: str
    slowest_seconds: float


def summarize(timings: Iterable[JobTiming]) -> list[RunnerReport]:
    """Aggregate per-job timings into one row per runner image.

    Sorted slowest-median first: the question this answers is which image is
    holding the matrix up, and that one belongs at the top rather than
    alphabetically buried.

    :param timings: Job timings, typically from {func}`fetch_job_timings`.
    :return: One {class}`RunnerReport` per image seen.
    """
    buckets: dict[str, list[JobTiming]] = {}
    for timing in timings:
        buckets.setdefault(timing.runner, []).append(timing)

    reports = []
    for runner, entries in buckets.items():
        slowest = max(entries, key=lambda entry: entry.seconds)
        reports.append(
            RunnerReport(
                runner=runner,
                job_count=len(entries),
                median_seconds=statistics.median(e.seconds for e in entries),
                slowest_job=slowest.name,
                slowest_seconds=slowest.seconds,
            )
        )
    return sorted(reports, key=lambda report: report.median_seconds, reverse=True)


def format_duration(seconds: float) -> str:
    """Render a duration zero-padded to a fixed width.

    The padding is not cosmetic. `--sort-by` orders the rendered table
    lexicographically, so an unpadded `21s` sorts between `1m37s` and `2m03s`
    and the default view reads as though a 21-second job were slower than a
    97-second one. Fixed-width `00m21s` makes the string order the
    chronological one, for every column and both directions, without the
    table layer needing to know these cells are durations.
    """
    minutes, remainder = divmod(round(seconds), 60)
    return f"{minutes:02d}m{remainder:02d}s"


def render_markdown(reports: Sequence[RunnerReport], workflow: str, runs: int) -> str:
    """Render a report as a Markdown table, for pasting into documentation.

    Emitted on request rather than written by a sync job. Timings move on every
    run, so a job regenerating a checked-in table would open a pull request
    forever and never converge: this is a measurement to take when a decision
    needs one, not a file to keep in sync.

    :param reports: Aggregated rows from {func}`summarize`.
    :param workflow: Workflow the sample came from.
    :param runs: How many runs were sampled.
    :return: A Markdown table, newline-terminated.
    """
    table = render_markdown_table(
        tuple(label for label, _key in JOB_TIMINGS_HEADER_DEFS),
        (
            (
                f"`{report.runner}`",
                report.job_count,
                format_duration(report.median_seconds),
                report.slowest_job,
                format_duration(report.slowest_seconds),
            )
            for report in reports
        ),
        align=("left", "right", "right", "left", "right"),
    )
    lines = [
        f"Median whole-job wall-clock across the {runs} most recent successful",
        f"`{workflow}` runs. Re-measure rather than trusting these: they are one",
        "project's, on a shared fleet, and they drift.",
        "",
        table,
    ]
    return "\n".join(lines) + "\n"
