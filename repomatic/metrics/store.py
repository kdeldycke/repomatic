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
every quarter would still pile up over a thousand rows a year that no page ever
opens. Only the newest reading is kept, dated when the value last *moved*, so a
run that finds it unchanged leaves the file alone rather than restamping every
row.

{class}`Retention` is where that choice lives, and {meth}`MetricStore.upsert`
is the only code that has to know about it.
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
signal a monotonic one hides. Each row therefore names its {class}`Source`, so
a reader can always tell which question a point answers.
```
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import Enum, StrEnum, auto
from itertools import accumulate

from click_extra import ColumnSpec

from ..github.gh import GhCommandError, GhOutputError, gh_json
from ..humanize import today_utc
from ..tabular import load_records, render_csv, write_csv
from .forge import GITHUB_HOST, canonical_url, repo_metrics, split_repo_url

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping
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

RecordKey = tuple[str, str, str]
"""Identity of one reading: its subject's URL, its metric and its day."""

SAMPLE_HEADER_DEFS: tuple[ColumnSpec, ...] = (
    ColumnSpec("subject", "Subject"),
    ColumnSpec("phase", "Phase"),
    ColumnSpec("repository", "Repository"),
    ColumnSpec("stars", "Stars"),
    ColumnSpec("rows", "Rows"),
    ColumnSpec("note", "Note"),
)
"""Column definitions for the `repomatic sample-metrics` table.

Lives beside {meth}`SampleOutcome.row`, so the columns and the fields they
render cannot drift apart; the CLI derives its `--sort-by` choices from it.
"""

STAR_HISTORY_MAX_PAGES = 100
"""Pages of star history GitHub serves before refusing with a `422`.

At thirty weeks a page this reaches back about fifty-seven years, longer than
GitHub has existed, so no repository can outrun it and the ceiling is a guard
against a walk that never terminates rather than a limit on coverage.
"""


class Source(StrEnum):
    """Where a reading came from, recorded on every row.

    A chart may mix methodologies it cannot reconcile, so it records which one
    each point came from rather than presenting a uniform curve it cannot
    honestly claim.

    `STAR_HISTORY` and `WAYBACK` are retired: nothing writes them since GitHub
    reopened a public star history. They stay because a store populated before
    that still names them, and a reading already in the file is data, not a
    collector that has to keep existing.
    """

    CREATED = "created"
    """Repository creation, the one date a star count is known to be 0.

    Not a measurement but a fact. A repository whose earliest reading already
    shows a count, like one only ever sampled forward, has no knowable first
    star, so its curve would otherwise begin in mid-air. The row is also what
    a by-age chart aligns on, though a repository starred on its creation day
    keeps no anchor: that day's measurement outranks it and takes its place,
    per {attr}`rank`.
    """

    GITHUB = "github"
    """Reconstructed from GitHub's star history, surviving stars only."""

    SAMPLE = "sample"
    """Read from the forge's own API, contemporaneous."""

    STAR_HISTORY = "star-history"
    """Count at a date, imported from a star-history.com export. Retired."""

    WAYBACK = "wayback"
    """Contemporaneous count mined from an archived GitHub page. Retired."""

    @property
    def rank(self) -> int:
        """How authoritative this provenance is, to settle two readings of a day.

        An exact reconstruction supersedes a mined or imported count; a
        contemporaneous sample supersedes those two, since it was taken by this
        collector against the live API. A weaker reading never overwrites a
        stronger one of the same day, so a collector run against an
        already-populated store degrades nothing.

        `CREATED` ranks under everything, because it is the weakest claim in
        the vocabulary rather than the strongest: it asserts a count of zero
        from the fact that a repository cannot be starred before it exists,
        which stops being true on the creation day itself. A repository starred
        within hours of being published has a real reading for that day, and
        any measurement of it beats the assumption.

        The store holds one row per day, so that measurement *replaces* the
        anchor instead of sitting beside it, and the anchor does not come back:
        every later sample re-asserts it and this same rule refuses it. The
        creation day survives, since the reading that displaced the anchor
        carries its date. The zero does not, and with it goes the marker saying
        that a series' first point is day zero rather than merely its first
        measurement.

        ```{todo}
        Carry the creation day as a non-accruing attribute beside `commit` and
        `release`, so no star reading can collide with it, and let
        {func}`repomatic.metrics.chart.render_chart` read a by-age origin from
        it. This is a store schema change: it adds a `metric` value that every
        consumer's own store-shape test enumerates, so it lands with a note to
        downstream repositories rather than silently.
        ```
        """
        match self:
            case Source.CREATED:
                return 0
            case Source.STAR_HISTORY | Source.WAYBACK:
                return 1
            case Source.SAMPLE:
                return 2
            case Source.GITHUB:
                return 3


class SamplePhase(StrEnum):
    """The collector that produced a {class}`SampleOutcome`.

    The CLI reports one row per subject per collector, and the columns mean
    different things in each, so the row names the collector whose meaning it
    carries.
    """

    FORWARD = "forward"
    """{func}`sample_subject`: today's readings, from the subject's own forge."""

    RECONSTRUCT = "reconstruct"
    """{func}`reconstruct_from_github`: a whole star curve, rebuilt."""


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

    @property
    def accrues(self) -> bool:
        """Whether this metric's past readings are kept and can be charted."""
        return self.retention is Retention.HISTORY


METRICS: tuple[Metric, ...] = (
    # Date of the newest commit on the default branch, which stays true for a
    # rolling repository that never tags a release.
    Metric("commit", Retention.LATEST, "Last commit"),
    # Date of the newest release or tag, whichever is more recent.
    Metric("release", Retention.LATEST, "Last release"),
    # Whether the release date came from a release the project announced, or
    # from the newest tag it merely labelled.
    Metric("release_source", Retention.LATEST, "Release kind"),
    # Accounts following the repository on its own forge.
    Metric("stars", Retention.HISTORY, "Stars"),
)
"""Every metric the sampler collects, sorted by ID.

The extension point: a new counter is one entry here plus one `yield` in
{meth}`~repomatic.metrics.forge.ForgeMetrics.readings`. Nothing else changes, because
the store, the retention rule and the chart all read this registry.
"""

METRICS_BY_ID: dict[str, Metric] = {metric.id: metric for metric in METRICS}
"""Index for O(1) metric lookup by ID."""

CHARTABLE_METRICS: tuple[str, ...] = tuple(m.id for m in METRICS if m.accrues)
"""Metrics a chart can plot, since only an accruing one has a curve."""


def chartable_metric(metric_id: str) -> Metric:
    """Look up a metric a chart can plot.

    :param metric_id: The `metric` a chart or a series names.
    :return: The registered metric.
    :raises ValueError: When *metric_id* names no metric, or one that does not
        accrue and so has no curve to plot.
    """
    metric = METRICS_BY_ID.get(metric_id)
    if metric is None or not metric.accrues:
        chartable = ", ".join(CHARTABLE_METRICS)
        msg = f"Metric {metric_id!r} has no history to chart. Pick one of: {chartable}."
        raise ValueError(msg)
    return metric


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

    source: Source
    """Where the figure came from."""

    @property
    def key(self) -> RecordKey:
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
        return (self.repo, self.metric, self.day, self.value, self.source.value)

    @classmethod
    def from_row(cls, row: Mapping[str, str]) -> MetricRecord:
        """Rebuild a record from one parsed CSV row.

        :param row: The row, keyed by column name.
        :return: The corresponding record.
        :raises KeyError: When a column is missing.
        :raises ValueError: When the source is not a {class}`Source`.
        """
        return cls(
            repo=row["repo"],
            metric=row["metric"],
            day=row["date"],
            value=row["value"],
            source=Source(row["source"]),
        )


class MetricStore:
    """Every reading of one store, keyed by subject, metric and day.

    Indexes the readings by subject and metric once, when built, so recording
    a reading looks up the rows it may replace rather than scanning the whole
    store.
    """

    def __init__(self, records: Iterable[MetricRecord] = ()) -> None:
        self.records: dict[RecordKey, MetricRecord] = {}
        """Every reading, keyed by {attr}`MetricRecord.key`.

        Read it freely. Change it through {meth}`upsert` and {meth}`discard`
        only, which keep the index in step.
        """
        self._index: dict[tuple[str, str], set[RecordKey]] = {}
        for record in records:
            self._add(record)

    def __len__(self) -> int:
        return len(self.records)

    @classmethod
    def load(cls, path: Path) -> MetricStore:
        """Read the committed store.

        :param path: Path to the CSV store.
        :return: The store, empty when the file does not exist.
        :raises ValueError: When the file exists but cannot be parsed. Loud on
            purpose: a corrupt store must never be silently clobbered by the
            next {meth}`save`.
        """
        return cls(load_records(path, MetricRecord.from_row, "metric store"))

    def save(self, path: Path) -> bool:
        """Write the store back, sorted by subject, metric and date.

        The file becomes exactly what the store holds, so a row dropped in
        memory is dropped on disk. That is what lets a collector re-derive a
        whole curve: a reconstruction that no longer dates a reading to a given
        week has to be able to retire that week's row, and a week left behind
        would state a total the rest of the curve contradicts.

        ```{caution}
        A caller must therefore save a store it loaded from this same file.
        One assembled from nothing truncates every reading it did not collect,
        which is every collector's own habit here and worth keeping.
        ```

        :param path: Path to the CSV store.
        :return: `True` when the file content changed.
        """
        rows = [self.records[key].as_row() for key in sorted(self.records)]
        return write_csv(path, render_csv(METRIC_HEADERS, rows))

    def held(self, repo: str, metric: str) -> list[MetricRecord]:
        """Every reading of one metric of one subject, oldest first.

        :param repo: The subject's canonical URL.
        :param metric: The metric ID.
        """
        keys = self._index.get((repo, metric), ())
        return [self.records[key] for key in sorted(keys)]

    def _add(self, record: MetricRecord) -> None:
        self.records[record.key] = record
        self._index.setdefault(record.subject_key, set()).add(record.key)

    def discard(self, record: MetricRecord) -> None:
        """Drop the reading stored under the key of *record*."""
        del self.records[record.key]
        self._index[record.subject_key].discard(record.key)

    def upsert(self, record: MetricRecord) -> bool:
        """Record one reading, returning whether it changed anything.

        Re-running on the same day overwrites rather than appends, which is
        what keeps the scheduled job idempotent. Beyond that the metric's
        {class}`Retention` decides:

        - An accruing metric keeps every day, and a more authoritative source
          wins over a weaker one for the same day, per {attr}`Source.rank`.
        - An attribute keeps one row. An unchanged value leaves the stored date
          alone, so a quiet run rewrites nothing; a moved value replaces the
          row and takes the new date, which is therefore when the value last
          changed rather than when it was last confirmed.

        :param record: The reading to store.
        :return: `True` when the store moved.
        :raises KeyError: When the metric is not in {data}`METRICS_BY_ID`.
        """
        if METRICS_BY_ID[record.metric].accrues:
            previous = self.records.get(record.key)
            if previous == record:
                return False
            if previous and record.source.rank < previous.source.rank:
                return False
            self._add(record)
            return True

        existing = self.held(record.repo, record.metric)
        if existing:
            if existing[-1].value == record.value:
                return False
            for held in existing:
                self.discard(held)
        self._add(record)
        return True


@dataclass(frozen=True)
class SampleOutcome:
    """What one subject's sample produced, for the CLI to report."""

    subject: str
    """Name the repository gives this subject."""

    repo: str
    """Canonical URL it read from."""

    phase: SamplePhase
    """The collector that produced this outcome."""

    stars: int | None = None
    """Its current star count, when the collector read one."""

    rows: int = 0
    """How many stored rows this collector added or moved."""

    note: str = ""
    """Why nothing was collected, empty when something was."""

    def row(self) -> tuple[str, ...]:
        """Render the outcome as one row of {data}`SAMPLE_HEADER_DEFS`."""
        return (
            self.subject,
            self.phase.value,
            self.repo,
            "—" if self.stars is None else str(self.stars),
            str(self.rows),
            self.note or "—",
        )


def predecessor_key(name: str) -> str:
    """Name the series of the forerunner of subject *name*."""
    return name + PREDECESSOR_SUFFIX


def is_predecessor(key: str) -> bool:
    """Whether series *key* is a forerunner's, rather than a subject's own."""
    return key.endswith(PREDECESSOR_SUFFIX)


def subject_of(key: str) -> str:
    """Name the subject series *key* belongs to.

    :return: *key* itself for a subject's own series, or the subject a
        forerunner precedes.
    """
    return key.removesuffix(PREDECESSOR_SUFFIX)


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
        collected[predecessor_key(name)] = canonical_url(target)
    return collected


def sample_subject(
    store: MetricStore,
    subject: str,
    repo: str,
    extra_forges: Mapping[str, str] | None = None,
    day: str | None = None,
) -> SampleOutcome:
    """Read every metric of one subject, through whichever forge hosts it.

    The collector every run applies to every subject, and the only one that
    reads a repository outside GitHub.

    :param store: The in-memory store, changed in place.
    :param subject: Name the repository gives this subject.
    :param repo: Its canonical URL.
    :param extra_forges: Host-to-forge entries for self-hosted instances.
    :param day: Reading date in `YYYY-MM-DD` form. Today (UTC) when `None`.
    :return: What the sample produced.
    """
    if day is None:
        day = today_utc().isoformat()
    phase = SamplePhase.FORWARD
    try:
        metrics = repo_metrics(repo, extra_forges)
    except (RuntimeError, ValueError, KeyError, IndexError, TypeError) as error:
        # Caught wide and per subject: a repository gone private, a host
        # answering a payload of a shape nobody anticipated, or a forge added
        # without its API declared must cost one row, not every other reading
        # the run collected.
        return SampleOutcome(subject, repo, phase, note=str(error)[:110])
    if metrics is None:
        return SampleOutcome(subject, repo, phase, note="unreadable")

    rows = 0
    for metric_id, value in metrics.readings():
        record = MetricRecord(repo, metric_id, day, value, Source.SAMPLE)
        rows += int(store.upsert(record))
    if metrics.created:
        # Immutable, and free to re-assert: the repository object carries it on
        # every sample, so the origin is recorded without a second call.
        origin = MetricRecord(repo, "stars", metrics.created, "0", Source.CREATED)
        rows += int(store.upsert(origin))
    return SampleOutcome(subject, repo, phase, stars=metrics.stars, rows=rows)


def reconstruct_from_github(
    store: MetricStore,
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
    single unstar rewrites thousands of committed rows.

    The week is the endpoint's own bucket and the unit the charts plot over
    years, not the cadence the job runs at: a run rebuilds every week's row
    whatever the gap since the last one, so a quarterly schedule keeps the same
    resolution and only dates the newest weeks later.

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

    :param store: The in-memory store, changed in place once the whole walk
        succeeded.
    :param subject: Name the repository gives this subject.
    :param repo: Its canonical URL.
    :return: What the reconstruction produced.
    """
    phase = SamplePhase.RECONSTRUCT
    host, path = split_repo_url(repo)
    if host != GITHUB_HOST:
        return SampleOutcome(
            subject, repo, phase, note=f"{host} serves no star history"
        )

    per_week: dict[date, tuple[str, int]] = {}
    for page in range(1, STAR_HISTORY_MAX_PAGES + 1):
        try:
            batch = gh_json(["api", f"repos/{path}/stargazers/history?page={page}"])
        except GhOutputError:
            return SampleOutcome(subject, repo, phase, note=f"unparsable page {page}")
        except GhCommandError as error:
            if page == 1 and error.status == 404:
                return SampleOutcome(subject, repo, phase, note="no such repository")
            detail = str(error).strip().splitlines()
            reason = detail[0][:80] if detail else "unknown error"
            return SampleOutcome(
                subject, repo, phase, note=f"abandoned on page {page}: {reason}"
            )
        if not batch:
            break
        for week in batch:
            # Read as a plain calendar date, not converted: see the caution above.
            start = datetime.fromtimestamp(week["week"], UTC).date()
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

    if not per_week:
        return SampleOutcome(subject, repo, phase, note="no star on record")

    # This source owns every row it ever wrote for this subject, so the walk
    # replaces them rather than merging into them. A curve is re-derived whole
    # on each run, and a week that no longer carries a reading has to disappear:
    # left behind, it would state a total the rest of the curve contradicts.
    # Held first, so the report can still count what actually moved: every
    # rewritten row is a fresh key to `upsert`, which would otherwise make an
    # unchanged curve read as if the whole of it had just been collected.
    owned = [held for held in store.held(repo, "stars") if held.source is Source.GITHUB]
    previous = {held.key: held.value for held in owned}
    for held in owned:
        store.discard(held)

    weeks = sorted(per_week)
    rows = 0
    for week, total in zip(weeks, accumulate(per_week[each][1] for each in weeks)):
        record = MetricRecord(
            repo, "stars", per_week[week][0], str(total), Source.GITHUB
        )
        store.upsert(record)
        rows += int(previous.get(record.key) != record.value)
    return SampleOutcome(
        subject,
        repo,
        phase,
        stars=sum(count for _day, count in per_week.values()),
        rows=rows + len(set(previous) - set(store.records)),
    )


def series(
    store: MetricStore,
    subjects: Mapping[str, str],
    metric: str = "stars",
    predecessors: Mapping[str, str] | None = None,
) -> dict[str, list[tuple[date, int]]]:
    """Group one metric's readings into a chronological series per subject.

    :param store: The store.
    :param subjects: Tracked subjects, mapping each name to a slug or URL.
    :param metric: Which accruing metric to plot.
    :param predecessors: Retired forerunners, keyed by the subject they precede.
    :return: One sorted list of `(day, value)` per subject that has any
        reading, forerunners under their {data}`PREDECESSOR_SUFFIX` key.
    :raises ValueError: When *metric* does not accrue, so has no curve to plot.
    """
    chartable_metric(metric)
    grouped: dict[str, list[tuple[date, int]]] = {}
    for name, repo in collected_subjects(subjects, predecessors).items():
        points = [
            (date.fromisoformat(held.day), held.count)
            for held in store.held(repo, metric)
        ]
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
        key = predecessor_key(name)
        if key not in grouped or name not in grouped:
            continue
        handover = grouped[name][0][0]
        clipped = [point for point in grouped[key] if point[0] <= handover]
        if clipped:
            grouped[key] = clipped
        else:
            del grouped[key]
    return grouped
