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

"""Accumulate what forges say about a set of repositories, one reading at a time.

Every reading is one row of one table: which repository, which metric, on which
date, what it said, and where the figure came from. A new metric is a
{class}`Metric` entry and one line in the forge reader, not a new file, a new
schema or a new command.

```{note}
How long a reading is kept is a property of the metric, not of the caller.

A **counter** accrues: its whole point is the curve, so every dated reading is
kept and charted. A star count is the one that motivated all this.

An **attribute** does not: the date of a project's newest commit is a fact
about today, nothing reads it chronologically, and a hundred subjects sampled
weekly would pile up thousands of rows a year that no page ever opens. Only the
newest reading is kept, dated when the value last *moved*, so a quiet week
leaves the file untouched rather than restamping every row.

{class}`Retention` is where that choice lives, and {func}`upsert` is the only
code that has to know about it.
```

```{note}
The star history replaces the third-party charts a project used to embed. On
2026-06-30 GitHub restricted the REST stargazer endpoints to a repository's own
admins and collaborators, and closed the equivalent GraphQL field on
2026-07-17, which left every such embed on the web rendering an error card.

GitHub reopened the aggregate half on 2026-09-04, as a star-history endpoint
reporting counts per day without naming a single account. It needs no token and
answers for any public repository, so a curve no longer depends on who holds
the credentials. Sampling still runs on a schedule, because a history that
accrues in the repository cannot be revoked upstream.
```

```{warning}
A reconstruction and a sample do not measure the same thing, and the difference
is deliberate rather than a defect.

GitHub builds the star history from the accounts that *still* have the
repository starred, so a reconstruction attributes today's surviving stars to
the dates they were given: it understates every past date by the number of
stars since withdrawn, converging on the true figure at the present day. Kept
on purpose, since a curve that sags where a project shed followers carries a
signal a monotonic one hides. Each row therefore names its {data}`SOURCES`, so
a reader can always tell which question a point answers.
```
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from enum import Enum, auto
from itertools import accumulate

from click_extra import ColumnSpec

from .forge import GITHUB_HOST, canonical_url, repo_metrics, split_repo_url
from .github.gh import run_gh_command
from .tabular import load_records, render_csv, write_csv

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

METRIC_HEADERS = ("repo", "metric", "date", "value", "source")
"""Columns of the committed store, in file order.

The three key columns first, then the payload, so the file reads top to bottom
as one repository at a time, one metric at a time, chronologically. That is
also the sort order, which is what makes a scheduled commit an append per
subject rather than a reshuffle.
"""

PREDECESSOR_SUFFIX = ":prior"
"""Marks a predecessor's series key, appended to the subject it belongs to.

Keeps the configured subject list exactly the curves a chart plots, while still
letting the collectors and the renderer address the extra one through the same
code paths.
"""

SAMPLE_HEADER_DEFS: tuple[ColumnSpec, ...] = (
    ColumnSpec("subject", "Subject"),
    ColumnSpec("phase", "Phase"),
    ColumnSpec("repository", "Repository"),
    ColumnSpec("stars", "Stars"),
    ColumnSpec("rows", "Rows"),
    ColumnSpec("note", "Note"),
)
"""Column definitions for the `repomatic sample-metrics` table.

Lives beside the rows' domain model so the columns and the fields they render
cannot drift apart; the CLI derives its `--sort-by` choices from it.
"""

SOURCE_RANK: dict[str, int] = {
    "created": 0,
    "github": 3,
    "sample": 2,
    "star-history": 1,
    "wayback": 1,
}
"""How authoritative each provenance is, for resolving two readings of a day.

An exact reconstruction supersedes a mined or imported count; a contemporaneous
sample supersedes those two, since it was taken by this collector against the
live API. A weaker reading never overwrites a stronger one of the same day, so
a collector run against an already-populated store degrades nothing.

`created` ranks under everything, because it is the weakest claim in the
vocabulary rather than the strongest: it asserts a count of zero from the fact
that a repository cannot be starred before it exists, which stops being true on
the creation day itself. A repository starred within hours of being published
has a real reading for that day, and any measurement of it beats the
assumption.

The store holds one row per day, so that measurement *replaces* the anchor
instead of sitting beside it, and the anchor does not come back: every later
sample re-asserts it and this same rule refuses it. The creation day survives,
since the reading that displaced the anchor carries its date. The zero does
not, and with it goes the marker saying that a series' first point is day zero
rather than merely its first measurement.

```{todo}
Carry the creation day as a non-accruing attribute beside `commit` and
`release`, so no star reading can collide with it, and let
{func}`repomatic.metric_chart.render_chart` read a by-age origin from it.
This is a store schema change: it adds a `metric` value that every consumer's
own store-shape test enumerates, so it lands with a note to downstream
repositories rather than silently.
```
"""

SOURCES: dict[str, str] = {
    "created": "Repository creation, the one date a star count is known to be 0.",
    "github": "Reconstructed from GitHub's star history, surviving stars only.",
    "sample": "Read from the forge's own API, contemporaneous.",
    "star-history": "Count at a date, imported from a star-history.com export.",
    "wayback": "Contemporaneous count mined from an archived GitHub page.",
}
"""Provenance vocabulary, recorded per row.

A chart may mix methodologies it cannot reconcile, so it records which one each
point came from rather than presenting a uniform curve it cannot honestly
claim.

`created` is the outlier: not a measurement but a fact. A repository whose
earliest reading already shows a count, like one only ever sampled forward, has
no knowable first star, so its curve would otherwise begin in mid-air. The row
is also what a by-age chart aligns on, though a repository starred on its
creation day keeps no anchor: that day's measurement outranks it and takes its
place, per {data}`SOURCE_RANK`.

`star-history` and `wayback` are retired: nothing writes them since GitHub
reopened a public star history, and they stay in the vocabulary because a store
populated before that still names them. A reading already in the file is data,
not a collector that has to keep existing.
"""

STAR_HISTORY_MAX_PAGES = 100
"""Pages of star history GitHub serves before refusing with a `422`.

At thirty weeks a page this reaches back about fifty-seven years, longer than
GitHub has existed, so no repository can outrun it and the ceiling is a guard
against a walk that never terminates rather than a limit on coverage.
"""


class Retention(Enum):
    """How long the store keeps a metric's readings."""

    HISTORY = auto()
    """Every dated reading, forever. For a counter, whose curve is the point."""

    LATEST = auto()
    """Only the newest reading, dated when the value last moved.

    For an attribute, which describes today rather than accruing. Nothing reads
    it chronologically, and keeping every sample would bury the file in rows
    restating what the previous one already said.
    """


@dataclass(frozen=True)
class Metric:
    """One thing a forge can be asked about a repository."""

    id: str
    """Value of the store's `metric` column, and the name a chart selects on."""

    retention: Retention
    """Which of {class}`Retention` governs this metric's rows."""

    label: str
    """Human-readable name, for a rendered table or a chart axis."""

    description: str
    """What the reading means, and what it deliberately does not."""

    @property
    def accrues(self) -> bool:
        """Whether this metric's past readings are kept and can be charted."""
        return self.retention is Retention.HISTORY


METRICS: tuple[Metric, ...] = (
    Metric(
        "commit",
        Retention.LATEST,
        "Last commit",
        "Date of the newest commit on the default branch, which stays true for "
        "a rolling repository that never tags a release.",
    ),
    Metric(
        "release",
        Retention.LATEST,
        "Last release",
        "Date of the newest release or tag, whichever is more recent.",
    ),
    Metric(
        "release_source",
        Retention.LATEST,
        "Release kind",
        "Whether the release date came from a release the project announced, "
        "or from the newest tag it merely labelled.",
    ),
    Metric(
        "stars",
        Retention.HISTORY,
        "Stars",
        "Accounts following the repository on its own forge.",
    ),
)
"""Every metric the sampler collects, sorted by ID.

The extension point: a new counter is one entry here plus one `yield` in
{meth}`~repomatic.forge.ForgeMetrics.readings`. Nothing else changes, because
the store, the retention rule and the chart all read this registry.
"""

METRICS_BY_ID: dict[str, Metric] = {metric.id: metric for metric in METRICS}
"""Index for O(1) metric lookup by ID."""

CHARTABLE_METRICS: tuple[str, ...] = tuple(m.id for m in METRICS if m.accrues)
"""Metrics a chart can plot, since only an accruing one has a curve."""


@dataclass(frozen=True)
class MetricRecord:
    """One reading: what a forge said about one repository on one date."""

    repo: str
    """Canonical `https://host/owner/name` URL of the subject."""

    metric: str
    """Which {data}`METRICS` entry this reading is of."""

    day: str
    """The reading's date, in `YYYY-MM-DD` form.

    For an accruing metric, when the reading was taken. For an attribute, when
    its value last changed.
    """

    value: str
    """What the forge answered, as text.

    CSV carries no types, so a consumer wanting a number coerces it. The store
    keeps the forge's own answer rather than a parsed one, since a metric added
    later may not be numeric at all.
    """

    source: str
    """Which key of {data}`SOURCES` produced the figure."""

    @property
    def key(self) -> tuple[str, str, str]:
        """Deduplication identity: one reading per subject, metric and day."""
        return (self.repo, self.metric, self.day)

    @property
    def subject_key(self) -> tuple[str, str]:
        """What an attribute keeps only one row of."""
        return (self.repo, self.metric)

    @property
    def count(self) -> int:
        """The reading as an integer, for a counter metric.

        :raises ValueError: When the value is not a number, which means a chart
            was pointed at an attribute.
        """
        return int(self.value)

    def as_row(self) -> tuple[str, ...]:
        """Flatten to one CSV row, in {data}`METRIC_HEADERS` order."""
        return (self.repo, self.metric, self.day, self.value, self.source)

    @classmethod
    def from_row(cls, row: Mapping[str, str]) -> MetricRecord:
        """Rebuild a record from one parsed CSV row.

        :param row: The row, keyed by column name.
        :return: The corresponding record.
        :raises KeyError: When a column is missing.
        """
        return cls(
            repo=row["repo"],
            metric=row["metric"],
            day=row["date"],
            value=row["value"],
            source=row["source"],
        )


@dataclass(frozen=True)
class SampleOutcome:
    """What one subject's sample produced, for the CLI to report."""

    subject: str
    """Name the repository gives this subject."""

    repo: str
    """Canonical URL it read from."""

    phase: str
    """Sampling lane that produced this outcome.

    Either `forward` or `reconstruct`. The CLI reports one row per subject per
    lane, and the columns mean different things in each, so the row names the
    lane whose semantics it carries.
    """

    stars: int | None = None
    """Its current star count, when the collector read one."""

    rows: int = 0
    """How many stored rows this collector added or moved."""

    note: str = ""
    """Why nothing was collected, empty when something was."""


def collected_subjects(
    subjects: Mapping[str, str],
    predecessors: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Every repository a collector touches, keyed by its subject name.

    :param subjects: Tracked subjects, mapping each name to a slug or URL.
    :param predecessors: Retired forerunners, mapping the name of the subject
        they belong to onto their own slug or URL.
    :return: The subjects, plus one entry per forerunner whose key carries
        {data}`PREDECESSOR_SUFFIX` so a caller can tell the two apart. Every
        value is a canonical URL.
    :raises ValueError: When a declared subject parses as neither a slug nor a
        URL.
    """
    collected = {name: canonical_url(target) for name, target in subjects.items()}
    for name, target in (predecessors or {}).items():
        collected[name + PREDECESSOR_SUFFIX] = canonical_url(target)
    return collected


def load_metrics(path: Path) -> dict[tuple[str, str, str], MetricRecord]:
    """Read the committed store, keyed by subject, metric and date.

    :param path: Path to the CSV store.
    :return: The records, empty when the file does not exist.
    :raises ValueError: When the file exists but cannot be parsed. Loud on
        purpose: a corrupt store must never be silently clobbered by the next
        {func}`save_metrics` write.
    """
    records = load_records(path, MetricRecord.from_row, "metric store")
    return {record.key: record for record in records}


def save_metrics(
    path: Path,
    records: Mapping[tuple[str, str, str], MetricRecord],
) -> bool:
    """Write the store back, sorted by subject, metric and date.

    The file becomes exactly what the caller holds, so a row dropped in memory
    is dropped on disk. That is what lets a collector re-derive a whole curve:
    a reconstruction that no longer dates a reading to a given week has to be
    able to retire that week's row, and a week left behind would state a total
    the rest of the curve contradicts.

    ```{caution}
    A caller must therefore write with a store it loaded from this same file.
    One assembling records from nothing truncates every reading it did not
    collect, which is every collector's own habit here and worth keeping.
    ```

    :param path: Path to the CSV store.
    :param records: The records to write.
    :return: `True` when the file content changed.
    """
    rows = [records[key].as_row() for key in sorted(records)]
    return write_csv(path, render_csv(METRIC_HEADERS, rows))


def upsert(
    records: dict[tuple[str, str, str], MetricRecord],
    record: MetricRecord,
) -> bool:
    """Record one reading, returning whether it changed anything.

    Re-running on the same day overwrites rather than appends, which is what
    keeps the scheduled job idempotent. Beyond that the metric's
    {class}`Retention` decides:

    - An accruing metric keeps every day, and a more authoritative source wins
      over a weaker one for the same day, per {data}`SOURCE_RANK`.
    - An attribute keeps one row. An unchanged value leaves the stored date
      alone, so a quiet week rewrites nothing; a moved value replaces the row
      and takes the new date, which is therefore when the value last changed
      rather than when it was last confirmed.

    :param records: The in-memory store, mutated in place.
    :param record: The reading to store.
    :return: `True` when the store moved.
    :raises KeyError: When the metric is not in {data}`METRICS_BY_ID`.
    """
    metric = METRICS_BY_ID[record.metric]
    if metric.accrues:
        previous = records.get(record.key)
        if previous == record:
            return False
        if previous and SOURCE_RANK[record.source] < SOURCE_RANK[previous.source]:
            return False
        records[record.key] = record
        return True

    existing = [
        key for key, held in records.items() if held.subject_key == record.subject_key
    ]
    if existing:
        newest = max(existing, key=lambda key: key[2])
        if records[newest].value == record.value:
            return False
        for key in existing:
            del records[key]
    records[record.key] = record
    return True


def sample_subject(
    records: dict[tuple[str, str, str], MetricRecord],
    subject: str,
    repo: str,
    extra_forges: Mapping[str, str] | None = None,
    day: str | None = None,
) -> SampleOutcome:
    """Read every metric of one subject, through whichever forge hosts it.

    The collector every run applies to every subject, and the only one that
    reads a repository outside GitHub.

    :param records: The in-memory store, mutated in place.
    :param subject: Name the repository gives this subject.
    :param repo: Its canonical URL.
    :param extra_forges: Host-to-forge entries for self-hosted instances.
    :param day: Reading date in `YYYY-MM-DD` form. Today (UTC) when `None`.
    :return: What the sample produced.
    """
    if day is None:
        day = datetime.now(timezone.utc).date().isoformat()
    try:
        metrics = repo_metrics(repo, extra_forges)
    except (RuntimeError, ValueError, KeyError, IndexError, TypeError) as error:
        # Caught wide and per subject: a repository gone private, a host
        # answering a payload of a shape nobody anticipated, or a forge added
        # without its API declared must cost one row, not every other reading
        # the run collected.
        return SampleOutcome(subject, repo, phase="forward", note=str(error)[:110])
    if metrics is None:
        return SampleOutcome(subject, repo, phase="forward", note="unreadable")

    rows = 0
    for metric_id, value in metrics.readings():
        rows += int(
            upsert(records, MetricRecord(repo, metric_id, day, value, "sample"))
        )
    if metrics.created:
        # Immutable, and free to re-assert: the repository object carries it on
        # every sample, so the origin is recorded without a second call.
        rows += int(
            upsert(
                records, MetricRecord(repo, "stars", metrics.created, "0", "created")
            )
        )
    return SampleOutcome(subject, repo, phase="forward", stars=metrics.stars, rows=rows)


def reconstruct_from_github(
    records: dict[tuple[str, str, str], MetricRecord],
    subject: str,
    repo: str,
) -> SampleOutcome:
    """Rebuild one repository's star curve from GitHub's star history.

    Reads the aggregate endpoint GitHub opened on 2026-09-04, which reports how
    many stars a repository gained on each day without naming who gave them.
    Anonymous and public, so this reaches every subject a project tracks rather
    than only the ones a token administers.

    ```{note}
    The endpoint pages by week, not by star, so its cost follows a repository's
    age and not its popularity: a ten-year repository costs the same eighteen
    requests whether it holds six hundred stars or thirty thousand.
    ```

    One reading is kept per week that gained a star, dated on the last such day
    of that week. The endpoint resolves to the day, but the store deliberately
    does not: a cumulative curve counts the stars a repository *still* holds, so
    one withdrawal in 2019 lowers every later point, and at daily resolution a
    single unstar rewrites thousands of committed rows. A week is also the
    cadence the job samples at and the unit the charts plot over years.

    ```{caution}
    GitHub buckets the days in its own timezone, `America/Los_Angeles`, and
    honours daylight saving. Measured against 612 exact star timestamps, that
    zone puts every one of them in the bucket the endpoint reported, where
    reading the days as UTC misplaces about a fifth of them by one day.

    Honouring it needs no conversion: a week's `week` field is midnight UTC on
    the Sunday *labelling* that Los Angeles week, so the calendar date of that
    instant plus a day's offset is already the local day. Converting the
    timestamp into a zone would reintroduce the error.
    ```

    Pagination is all-or-nothing on purpose, and more sharply than it looks.
    The walk runs newest first, so a run abandoned halfway holds only the recent
    weeks: totalling those would date a fraction of the stars as if it were the
    whole history, and every point of the resulting curve would look exactly as
    legitimate as the rest.

    :param records: The in-memory store, mutated in place once the whole walk
        succeeded.
    :param subject: Name the repository gives this subject.
    :param repo: Its canonical URL.
    :return: What the reconstruction produced.
    """
    host, path = split_repo_url(repo)
    if host != GITHUB_HOST:
        return SampleOutcome(
            subject, repo, phase="reconstruct", note=f"{host} serves no star history"
        )

    per_week: dict[date, tuple[str, int]] = {}
    page = 1
    while page <= STAR_HISTORY_MAX_PAGES:
        try:
            batch = json.loads(
                run_gh_command(["api", f"repos/{path}/stargazers/history?page={page}"])
            )
        except RuntimeError as error:
            detail = str(error).strip().splitlines()
            reason = detail[0][:80] if detail else "unknown error"
            if page == 1 and "Not Found" in str(error):
                return SampleOutcome(
                    subject, repo, phase="reconstruct", note="no such repository"
                )
            return SampleOutcome(
                subject,
                repo,
                phase="reconstruct",
                note=f"abandoned on page {page}: {reason}",
            )
        except json.JSONDecodeError:
            return SampleOutcome(
                subject, repo, phase="reconstruct", note=f"unparsable page {page}"
            )
        if not batch:
            break
        for week in batch:
            # Read as a plain calendar date, not converted: see the caution above.
            start = datetime.fromtimestamp(week["week"], timezone.utc).date()
            gained = [
                (start + timedelta(days=offset), count)
                for offset, count in enumerate(week["days"])
                if count
            ]
            if gained:
                per_week[start] = (
                    gained[-1][0].isoformat(),
                    sum(count for _day, count in gained),
                )
        page += 1

    if not per_week:
        return SampleOutcome(
            subject, repo, phase="reconstruct", note="no star on record"
        )

    # This source owns every row it ever wrote for this subject, so the walk
    # replaces them rather than merging into them. A curve is re-derived whole
    # on each run, and a week that no longer carries a reading has to disappear:
    # left behind, it would state a total the rest of the curve contradicts.
    # Held first, so the report can still count what actually moved: every
    # rewritten row is a fresh key to `upsert`, which would otherwise make an
    # unchanged curve read as if the whole of it had just been collected.
    previous = {
        key: held.value
        for key, held in records.items()
        if held.repo == repo and held.metric == "stars" and held.source == "github"
    }
    for key in previous:
        del records[key]

    weeks = sorted(per_week)
    rows = 0
    for week, total in zip(weeks, accumulate(per_week[each][1] for each in weeks)):
        record = MetricRecord(repo, "stars", per_week[week][0], str(total), "github")
        upsert(records, record)
        rows += int(previous.get(record.key) != record.value)
    return SampleOutcome(
        subject,
        repo,
        phase="reconstruct",
        stars=sum(count for _day, count in per_week.values()),
        rows=rows + len(set(previous) - set(records)),
    )


def series(
    records: Mapping[tuple[str, str, str], MetricRecord],
    subjects: Mapping[str, str],
    metric: str = "stars",
    predecessors: Mapping[str, str] | None = None,
) -> dict[str, list[tuple[date, int]]]:
    """Group one metric's readings into a chronological series per subject.

    :param records: The store.
    :param subjects: Tracked subjects, mapping each name to a slug or URL.
    :param metric: Which accruing metric to plot.
    :param predecessors: Retired forerunners, keyed by the subject they precede.
    :return: One sorted list of `(day, value)` per subject that has any
        reading, forerunners under their {data}`PREDECESSOR_SUFFIX` key.
    :raises ValueError: When *metric* does not accrue, so has no curve to plot.
    """
    known = METRICS_BY_ID.get(metric)
    if known is None or not known.accrues:
        chartable = ", ".join(CHARTABLE_METRICS)
        msg = f"Metric {metric!r} has no history to chart. Pick one of: {chartable}."
        raise ValueError(msg)

    # One pass over the store, then one lookup per subject: scanning every
    # record once per subject grew with the product of the two.
    by_repo: dict[str, list[tuple[date, int]]] = {}
    for held in records.values():
        if held.metric == metric:
            by_repo.setdefault(held.repo, []).append((
                date.fromisoformat(held.day),
                held.count,
            ))
    grouped: dict[str, list[tuple[date, int]]] = {}
    for name, repo in collected_subjects(subjects, predecessors).items():
        points = sorted(by_repo.get(repo, ()))
        if points:
            grouped[name] = points

    # A forerunner's line stops where its successor's begins. An archived
    # repository keeps collecting the odd star to this day, and plotting that
    # tail would run it the whole width of the chart alongside the successor,
    # reading as two projects living side by side. Cutting it at the handover
    # shows what actually happened: one audience stopped being counted here and
    # started being counted there. The store keeps the discarded rows, so the
    # record stays complete even though the chart does not draw them.
    for name in predecessors or {}:
        key = name + PREDECESSOR_SUFFIX
        if key not in grouped or name not in grouped:
            continue
        handover = grouped[name][0][0]
        clipped = [point for point in grouped[key] if point[0] <= handover]
        if clipped:
            grouped[key] = clipped
        else:
            del grouped[key]
    return grouped
