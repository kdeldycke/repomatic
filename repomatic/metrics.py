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

What survived is the aggregate count on the repository object, which stays
public for everyone. Sampled on a schedule it accumulates into a history nobody
can revoke.
```

```{warning}
A reconstruction and a sample do not measure the same thing, and the difference
is deliberate rather than a defect.

The stargazers API lists only the accounts that *still* have the repository
starred, so a reconstruction attributes today's surviving stars to the dates
they were given: it understates every past date by the number of stars since
withdrawn, converging on the true figure at the present day. Kept on purpose,
since a curve that sags where a project shed followers carries a signal a
monotonic one hides. Each row therefore names its {data}`SOURCES`, so a reader
can always tell which question a point answers.
```
"""

from __future__ import annotations

import http.client
import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import Enum, auto
from itertools import accumulate

from .forge import GITHUB_HOST, canonical_url, repo_metrics, split_repo_url
from .git_ops import fetch_remote_branch, restore_paths
from .github.gh import run_gh_command
from .tabular import read_csv, render_csv, write_csv

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping
    from pathlib import Path

GITHUB_EPOCH = date(2008, 1, 1)
"""No star predates GitHub, so nothing earlier can be a real reading.

The guard that tells a star-history.com calendar export from its by-age
sibling: the latter measures each curve from epoch zero, so its rows land in
the 1970s and would otherwise enter the store as genuine points four decades
before the repository existed.
"""

MAX_RETRY_DELAY = 15.0
"""Ceiling on {func}`fetch`'s exponential backoff, in seconds.

Doubling without a bound spends the whole attempt budget waiting, which is the
wrong trade against a service that fails most requests but recovers within
seconds on the next one.
"""

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

SAMPLE_HEADER_DEFS: tuple[tuple[str, str], ...] = (
    ("Subject", "subject"),
    ("Phase", "phase"),
    ("Repository", "repository"),
    ("Stars", "stars"),
    ("Rows", "rows"),
    ("Note", "note"),
)
"""Column definitions for the `repomatic sample-metrics` table.

Lives beside the rows' domain model so the columns and the fields they render
cannot drift apart; the CLI derives its `--sort-by` choices from it.
"""

SOURCE_RANK: dict[str, int] = {
    "created": 3,
    "github": 3,
    "sample": 2,
    "star-history": 1,
    "wayback": 1,
}
"""How authoritative each provenance is, for resolving two readings of a day.

An exact reconstruction supersedes a mined or imported count; a
contemporaneous sample supersedes both, since it was taken by this collector
against the live API. A backfill never overwrites something stronger, which is
what lets a one-off import run against an already-populated store without
degrading it.
"""

SOURCES: dict[str, str] = {
    "created": "Repository creation, the one date a star count is known to be 0.",
    "github": "Exact per-star timestamps, surviving stars only (admin token).",
    "sample": "Read from the forge's own API, contemporaneous.",
    "star-history": "Count at a date, imported from a star-history.com export.",
    "wayback": "Contemporaneous count mined from an archived GitHub page.",
}
"""Provenance vocabulary, recorded per row.

A chart may mix methodologies it cannot reconcile, so it records which one each
point came from rather than presenting a uniform curve it cannot honestly
claim.

`created` is the outlier: not a measurement but a fact, and the only origin
every series shares. A repository backfilled from the archives has no knowable
first star, since its earliest capture already shows a count, so its curve
would otherwise begin in mid-air. It is also what a by-age chart aligns on.
"""

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120 Safari/537.36"
)
"""Sent to the Wayback Machine, which serves robots a reduced index."""

WAYBACK_PAGE_TRIES = 8
"""Attempts per archived page.

Sized against a measurement rather than a guess: 25 requests for one capture
known to exist returned 23 plain `503` responses and 2 truncated bodies, and no
clean response at all. Since a truncated body still carries the counter, the
per-try success rate that matters was 2 in 25, and eight tries is the point
past which more attempts cost more than the captures they recover.
"""

WAYBACK_REFUSAL_LIMIT = 10
"""Consecutive refused captures tolerated before the run abandons the archive.

A served page proves the archive healthy whatever it holds, so only refusals
extend the streak, and any payload resets it. Sized against the healthy
success rate {data}`WAYBACK_PAGE_TRIES` buys: with eight tries a capture
lands about half the time, so ten misses in a row happens by luck roughly
once in a thousand runs. Past it the per-IP budget is spent for a while, and
every further capture only burns a full retry schedule proving it again.
"""

WAYBACK_REQUEST_DELAY = 3.0
"""Seconds to wait between two archived pages.

The backfill is a one-off that nobody watches, so trading minutes for a higher
completion rate is free. Its counterpart is the retry backoff in {func}`fetch`,
which handles a single hiccup; this handles the sustained budget.
"""

WAYBACK_STAR_PATTERNS = (
    re.compile(r'id="repo-stars-counter-star"[^>]*title="([\d,]+)"', re.IGNORECASE),
    re.compile(r'title="([\d,]+)"[^>]*id="repo-stars-counter-star"', re.IGNORECASE),
    re.compile(r'aria-label="([\d,]+) users? starred', re.IGNORECASE),
    re.compile(
        r'href="/[^"]+/stargazers"[^>]*class="social-count[^"]*"[^>]*>\s*([\d,]+)',
        re.IGNORECASE,
    ),
    re.compile(
        r'class="social-count[^"]*"[^>]*href="/[^"]+/stargazers"[^>]*>\s*([\d,]+)',
        re.IGNORECASE,
    ),
)
"""Star-counter markups GitHub has shipped over the years, newest first.

An archived page states the exact figure in an attribute rather than the
abbreviated `4.4k` shown to readers, so a capture yields an integer, not an
estimate. The layout was reworked twice in the window these mine, hence the
alternatives.
"""

_CSV_DATE_RE = re.compile(
    r"^\w{3} (?P<moment>\w{3} \d{2} \d{4} \d{2}:\d{2}:\d{2}) GMT(?P<offset>[-+]\d{4})"
)
"""Matches the JavaScript `Date.toString()` stamps a star-history.com CSV carries.

The exporter writes whatever the browser's locale produced, so the stamp
carries a weekday, a numeric offset and a parenthesized zone name. Only the
calendar day survives into the store, but the offset has to be applied first:
a late-evening reading in a positive zone falls on the previous UTC day.
"""

_LAST_FETCH_REASONS: Counter[str] = Counter()
"""Why the most recent {func}`fetch` gave up, tallied by outcome.

Module-level rather than returned, so the retry loop keeps its `bytes | None`
signature while the caller can still report what went wrong. Only ever read
straight after a `None`, through {func}`last_fetch_failure`.
"""

_WAYBACK_REFUSAL_STREAK = 0
"""Captures refused in a row across the whole backfill, not just one subject.

The archive's budget is per IP and shared by every subject a run mines, so
the streak outlives the subject it started in: a backfill that trips
{data}`WAYBACK_REFUSAL_LIMIT` on one repository must not spend the next
one's retry schedule too.
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

    One of `forward`, `reconstruct`, `import` or `wayback`. The CLI reports
    one row per subject per lane, and the columns mean different things in
    each, so the row names the lane whose semantics it carries.
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


def last_fetch_failure() -> str:
    """Summarize why the most recent {func}`fetch` gave up.

    :return: A tally like `6x HTTP 503, 2x truncated`, or `no response` when
        nothing was recorded.
    """
    tally = ", ".join(
        f"{count}x {reason}" for reason, count in _LAST_FETCH_REASONS.most_common()
    )
    return tally or "no response"


def carry_pending_readings(branch: str, path: Path, remote: str = "origin") -> bool:
    """Read the store back from *branch* before sampling appends to it.

    A sampled reading cannot be fetched again, so the store is the only place
    that history exists. Once the accrual is delivered through a pull request
    instead of a direct push, a run starting from the default branch would
    measure against a store missing every reading still waiting in that pull
    request, and publish a branch holding one row where the history needs all
    of them. Restoring the store from the branch first makes each run append
    to what is already pending, so the pull request always shows the whole
    accrual as one diff against its base.

    Idempotent and self-healing, whichever way the pull request was merged.
    Once merged, the branch's store and the base's agree, so the restore
    changes nothing and the next run starts a fresh accrual from a store the
    base already holds. A squash merge is no different here: the comparison is
    of file content, never of commit history.

    ```{note}
    Only the store travels. Anything drawn from it is a pure function of the
    history (see {func}`~repomatic.metric_chart.render_chart`), so a chart
    redrawn from the restored store lands on the same bytes the branch holds
    without being carried over.
    ```

    :param branch: Remote branch holding the pending readings, usually the one
        the job's pull request is opened from.
    :param path: The CSV store to restore.
    :param remote: Remote to read the branch from.
    :return: `True` when readings were carried over.
    """
    tip = fetch_remote_branch(branch, remote=remote)
    if tip is None:
        logging.debug(f"No {branch!r} branch on {remote}, nothing pending to carry.")
        return False
    if not restore_paths(tip, (path,)):
        logging.debug(f"{remote}/{branch} carries no {path}, nothing to carry.")
        return False
    logging.info(f"Carried the store pending on {remote}/{branch} into {path}.")
    return True


def load_metrics(path: Path) -> dict[tuple[str, str, str], MetricRecord]:
    """Read the committed store, keyed by subject, metric and date.

    :param path: Path to the CSV store.
    :return: The records, empty when the file does not exist.
    :raises ValueError: When the file exists but cannot be parsed. Loud on
        purpose: a corrupt store must never be silently clobbered by the next
        {func}`save_metrics` write.
    """
    try:
        rows = read_csv(path)
        records = [MetricRecord.from_row(row) for row in rows]
    except (KeyError, TypeError, ValueError) as error:
        msg = f"Malformed metric store {path}: {error}"
        raise ValueError(msg) from error
    return {record.key: record for record in records}


def save_metrics(
    path: Path,
    records: Mapping[tuple[str, str, str], MetricRecord],
) -> bool:
    """Write the store back, sorted by subject, metric and date.

    Merges whatever is on disk under the caller's own records rather than
    overwriting the file wholesale. A slow backfill flushes after every point
    across a run lasting hours, so it holds a snapshot that goes stale the
    moment anything else records a reading: without the merge its next flush
    would silently drop those rows.

    ```{caution}
    The merge is additive, so it cannot express a *deletion*. An attribute
    whose older rows {func}`upsert` just pruned would come back from disk. The
    prune therefore happens against a store that was loaded from that same
    file, which is what every collector here does; a caller assembling records
    from nothing must write with a store it loaded first.
    ```

    :param path: Path to the CSV store.
    :param records: The records to write.
    :return: `True` when the file content changed.
    """
    merged = {**load_metrics(path), **records}
    # An attribute keeps one row per subject: whatever came back from disk for
    # a metric the caller just pruned is dropped again here, so the merge
    # cannot resurrect a superseded reading.
    for key, record in list(merged.items()):
        metric = METRICS_BY_ID.get(record.metric)
        if metric is None or metric.accrues:
            continue
        newest = max(
            (r.day for r in merged.values() if r.subject_key == record.subject_key),
            default=record.day,
        )
        if record.day != newest:
            del merged[key]
    rows = [merged[key].as_row() for key in sorted(merged)]
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


def gunzip(blob: bytes) -> bytes:
    """Decompress a gzip payload, tolerating one cut short mid-stream.

    {class}`gzip.GzipFile` needs the trailer to finish, so it raises on the
    truncated bodies a degraded archive delivers, discarding the megabyte that
    did arrive. Feeding the same bytes to a raw decompressor returns everything
    decodable before the cut and simply never reports the end of stream.

    :param blob: The compressed payload.
    :return: Everything that could be decoded, empty on an undecodable blob.
    """
    decompressor = zlib.decompressobj(zlib.MAX_WBITS | 16)
    try:
        return decompressor.decompress(blob)
    except zlib.error:
        return b""


def fetch(
    url: str,
    tries: int = 3,
    timeout: int = 45,
    user_agent: str = USER_AGENT,
) -> bytes | None:
    """Fetch a URL with capped backoff, returning `None` once every try failed.

    Deliberately separate from {mod}`repomatic.http`, whose single-retry policy
    is right for an API that either answers or does not. The Wayback Machine's
    replay service is frequently only partly healthy: its load balancer answers
    `503` for most requests while a minority succeed, with neighbouring
    requests for the same capture landing on different backends. A failure
    therefore says nothing about whether the capture exists, and repeating the
    request is the lever that works. Pacing is not: the whole service is
    degraded, not this client's budget.

    :param url: The URL to fetch.
    :param tries: How many attempts to make before giving up.
    :param timeout: Socket timeout in seconds.
    :param user_agent: Identity to send, defaulting to the browser one the
        archive wants and every forge refuses.
    :return: The body, or `None` once every attempt failed. Consult
        {func}`last_fetch_failure` for why.
    """
    delay = 2.0
    reasons: Counter[str] = Counter()
    for attempt in range(1, tries + 1):
        try:
            request = urllib.request.Request(
                url, headers={"User-Agent": user_agent, "Accept-Encoding": "gzip"}
            )
            with urllib.request.urlopen(request, timeout=timeout) as response:
                gzipped = response.headers.get("Content-Encoding") == "gzip"
                try:
                    payload: bytes = response.read()
                except http.client.IncompleteRead as truncation:
                    # Kept, not discarded. A degraded backend routinely cuts
                    # the connection after sending most of the page, and the
                    # star counter sits in markup that arrives well before the
                    # end: measured over 25 requests, every delivery that was
                    # not a 503 arrived this way, so dropping them threw away
                    # the only payloads the run produced.
                    payload = truncation.partial
                    reasons["truncated"] += 1
                if gzipped:
                    payload = gunzip(payload)
                if payload:
                    return payload
                reasons["empty body"] += 1
        except urllib.error.HTTPError as error:
            reasons[f"HTTP {error.code}"] += 1
        except Exception as error:  # noqa: BLE001
            reasons[type(error).__name__] += 1
        if attempt < tries:
            time.sleep(delay)
            delay = min(delay * 2, MAX_RETRY_DELAY)
    # Named rather than swallowed: a run reporting only "unreachable" cannot
    # tell a service refusing every request from one this collector is asking
    # wrongly, and those call for opposite responses.
    _LAST_FETCH_REASONS.clear()
    _LAST_FETCH_REASONS.update(reasons)
    return None


def sample_subject(
    records: dict[tuple[str, str, str], MetricRecord],
    subject: str,
    repo: str,
    extra_forges: Mapping[str, str] | None = None,
    day: str | None = None,
) -> SampleOutcome:
    """Read every metric of one subject, through whichever forge hosts it.

    The scheduled collector, and the only one that works for a repository the
    token does not administer, or that lives outside GitHub entirely.

    :param records: The in-memory store, mutated in place.
    :param subject: Name the repository gives this subject.
    :param repo: Its canonical URL.
    :param extra_forges: Host-to-forge entries for self-hosted instances.
    :param day: Reading date in `YYYY-MM-DD` form. Today (UTC) when `None`.
    :return: What the sample produced.
    """
    if day is None:
        day = datetime.now(tz=timezone.utc).date().isoformat()
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
    """Reconstruct one repository's star curve from per-star timestamps.

    Only works on GitHub, and only where the token administers the repository;
    GitHub answers `404` rather than `403` on the restricted endpoint for every
    other. Collapses to one cumulative reading per day on which the count
    moved, rather than one per star.

    Pagination is all-or-nothing on purpose. A transient failure halfway
    through would otherwise write a truncated cumulative curve over a correct
    one, and every point of it would look exactly as legitimate as the rest.

    :param records: The in-memory store, mutated in place once the whole walk
        succeeded.
    :param subject: Name the repository gives this subject.
    :param repo: Its canonical URL.
    :return: What the reconstruction produced.
    """
    host, path = split_repo_url(repo)
    if host != GITHUB_HOST:
        return SampleOutcome(
            subject,
            repo,
            phase="reconstruct",
            note=f"{host} serves no per-star timestamps",
        )

    per_day: Counter[str] = Counter()
    page = 1
    while True:
        try:
            batch = json.loads(
                run_gh_command([
                    "api",
                    f"repos/{path}/stargazers?per_page=100&page={page}",
                    "--header",
                    "Accept: application/vnd.github.star+json",
                ])
            )
        except RuntimeError as error:
            detail = str(error).strip().splitlines()
            reason = detail[0][:80] if detail else "unknown error"
            if page == 1 and "Not Found" in str(error):
                return SampleOutcome(
                    subject,
                    repo,
                    phase="reconstruct",
                    note="not an admin, not readable",
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
        for entry in batch:
            per_day[str(entry["starred_at"])[:10]] += 1
        page += 1

    if not per_day:
        return SampleOutcome(
            subject, repo, phase="reconstruct", note="the endpoint answered empty"
        )

    days = sorted(per_day)
    rows = 0
    for day, total in zip(days, accumulate(per_day[each] for each in days)):
        rows += int(
            upsert(records, MetricRecord(repo, "stars", day, str(total), "github"))
        )
    return SampleOutcome(
        subject, repo, phase="reconstruct", stars=per_day.total(), rows=rows
    )


def wayback_captures(path: str) -> list[str] | None:
    """List one archived capture per month of a repository's GitHub page.

    :param path: The repository's `owner/name` path.
    :return: The capture timestamps, or `None` when the index itself could not
        be read. That is not the same answer as an empty list and must not be
        reported as one: the archive fails this query as readily as any other,
        and a run treating the outage as "never archived" skips the repository
        silently and for good.
    """
    query = urllib.parse.urlencode({
        "url": f"github.com/{path}",
        "output": "json",
        "fl": "timestamp",
        "filter": "statuscode:200",
        "collapse": "timestamp:6",
    })
    payload = fetch("https://web.archive.org/cdx/search/cdx?" + query, tries=6)
    if payload is None:
        return None
    if not payload.strip():
        # A genuinely empty index answers 200 with no rows.
        return []
    try:
        return [row[0] for row in json.loads(payload)[1:]]
    except (json.JSONDecodeError, IndexError):
        # Unparsable is a fault, not an absence: same reasoning as above.
        return None


def backfill_wayback(
    records: dict[tuple[str, str, str], MetricRecord],
    subject: str,
    repo: str,
    store: Path | None = None,
    on_status: Callable[[str], None] | None = None,
    on_row: Callable[[str], None] | None = None,
) -> SampleOutcome:
    """Mine contemporaneous star counts from archived copies of a GitHub page.

    The only route to the past of a repository the token cannot administer, and
    the only one reporting what the counter actually read on the day rather
    than what survives today.

    :param records: The in-memory store, mutated in place.
    :param subject: Name the repository gives this subject.
    :param repo: Its canonical URL.
    :param store: Store to flush to after every recovered point, since a run
        spans many minutes of a flaky remote. Skipped when `None`.
    :param on_status: Called with whatever the backfill is reaching for next, so
        a caller can animate a live label. One subject is a single call spanning
        minutes, and a watcher hears nothing at all without this.
    :param on_row: Called with each recovered point, for a caller keeping a
        persistent line per result. Misses stay on the `INFO` log instead: the
        archive refuses far more captures than it serves, and a line each would
        bury the handful that landed.
    :return: What the backfill produced. When the archive refuses
        {data}`WAYBACK_REFUSAL_LIMIT` captures in a row, the run is abandoned
        with a retry-later note and every later subject is skipped: the budget
        is per IP, so no following subject stands a better chance.
    """
    global _WAYBACK_REFUSAL_STREAK
    host, path = split_repo_url(repo)
    if host != GITHUB_HOST:
        return SampleOutcome(
            subject, repo, phase="wayback", note=f"{host} pages are not mined"
        )
    if any(
        held.repo == repo and held.metric == "stars" and held.source == "github"
        for held in records.values()
    ):
        # An exact reconstruction already covers this repository, and mining it
        # would only add a second, differently-measured curve over the same
        # dates. The archives are slow and rate-limited: spend them on the
        # repositories that have no other source of history.
        return SampleOutcome(
            subject, repo, phase="wayback", note="already reconstructed exactly"
        )
    if _WAYBACK_REFUSAL_STREAK >= WAYBACK_REFUSAL_LIMIT:
        # A streak this long means the per-IP budget is spent for a while, and
        # the next subject shares it: skipping the index read spares one more
        # request to an archive answering nothing but refusals.
        return SampleOutcome(
            subject,
            repo,
            phase="wayback",
            note="skipped, the archive refused this run's earlier captures",
        )

    if on_status:
        on_status(f"{path}: reading the capture index")
    stamps = wayback_captures(path)
    if stamps is None:
        # Loud, and distinct from "nothing was ever archived": this repository
        # still has a past to mine, so the next run must come back to it.
        note = f"capture index unreadable ({last_fetch_failure()}), retry later"
        return SampleOutcome(subject, repo, phase="wayback", note=note)

    rows = 0
    for index, stamp in enumerate(stamps, start=1):
        day = f"{stamp[0:4]}-{stamp[4:6]}-{stamp[6:8]}"
        if (repo, "stars", day) in records:
            continue
        if on_status:
            # Carries the tally as well as the position, since most captures
            # yield nothing: without it a watcher sees the counter advance for
            # minutes with no way to tell a working run from a refused one.
            on_status(f"{path} {day} ({index}/{len(stamps)}, {rows} recovered)")
        # Paced deliberately. The archive answers 503 to every URL form once a
        # sustained crawl exhausts its budget, and it stays shut for a while: a
        # run racing through the captures finishes by collecting nothing.
        time.sleep(WAYBACK_REQUEST_DELAY)
        payload = fetch(
            f"https://web.archive.org/web/{stamp}id_/https://github.com/{path}",
            tries=WAYBACK_PAGE_TRIES,
        )
        if not payload:
            _WAYBACK_REFUSAL_STREAK += 1
            logging.info(f"  {path} {day}: unreachable ({last_fetch_failure()})")
            if _WAYBACK_REFUSAL_STREAK >= WAYBACK_REFUSAL_LIMIT:
                note = (
                    f"{_WAYBACK_REFUSAL_STREAK} captures refused in a row "
                    f"({last_fetch_failure()}), retry later"
                )
                return SampleOutcome(
                    subject, repo, phase="wayback", rows=rows, note=note
                )
            continue
        # A served page proves the archive healthy whatever it holds.
        _WAYBACK_REFUSAL_STREAK = 0
        stars = read_star_counter(payload.decode("utf-8", errors="replace"))
        if stars is None:
            logging.info(f"  {path} {day}: no counter found")
            continue
        if upsert(records, MetricRecord(repo, "stars", day, str(stars), "wayback")):
            rows += 1
            if store is not None:
                save_metrics(store, records)
            if on_row:
                on_row(f"{path} {day}: {stars:,} stars")
            else:
                logging.info(f"  {path} {day}: {stars} stars")
    return SampleOutcome(
        subject, repo, phase="wayback", rows=rows, note=f"{len(stamps)} captures"
    )


def read_star_counter(html: str) -> int | None:
    """Read the exact star count out of an archived GitHub repository page.

    :param html: The archived page's markup.
    :return: The count, or `None` when no known counter markup matched.
    """
    for pattern in WAYBACK_STAR_PATTERNS:
        match = pattern.search(html)
        if match:
            return int(match.group(1).replace(",", ""))
    return None


def parse_csv_day(stamp: str) -> date | None:
    """Read the UTC calendar day out of a star-history.com CSV timestamp.

    :param stamp: A JavaScript `Date.toString()` stamp.
    :return: The day in UTC, or `None` when the stamp does not parse.
    """
    match = _CSV_DATE_RE.match(stamp.strip())
    if not match:
        return None
    try:
        moment = datetime.strptime(
            f"{match['moment']} {match['offset']}", "%b %d %Y %H:%M:%S %z"
        )
    except ValueError:
        return None
    return moment.astimezone(timezone.utc).date()


def import_star_history_csv(
    records: dict[tuple[str, str, str], MetricRecord],
    path: Path,
    repos: Iterable[str] | None = None,
) -> list[SampleOutcome]:
    """Import the calendar export a star-history.com user downloaded.

    That service reconstructed its curves from the same stargazer endpoint
    GitHub has since closed, so an export taken while it worked is the only
    surviving record of the past for a repository nobody administers and the
    archives never captured.

    ```{caution}
    A replacement export cannot be obtained today. The service now inherits the
    restriction it reports: asked for a repository the visitor neither owns nor
    collaborates on, it answers that star history is unavailable instead of
    exporting anything. So a file reaching this function was either downloaded
    before the endpoints closed, or covers a repository its downloader
    administers, which {func}`reconstruct_from_github` already rebuilds exactly
    and at finer resolution. For a competitor, {func}`backfill_wayback` and
    forward sampling are what is left.
    ```

    Its by-age export is refused rather than imported: that variant measures
    every curve from epoch zero, so its rows land in the 1970s and would enter
    the store as readings four decades before the repository existed.

    :param records: The in-memory store, mutated in place.
    :param path: The exported CSV.
    :param repos: Only import rows whose repository canonicalizes into this
        set. Every row when `None`.
    :return: One outcome per repository the file covered.
    :raises ValueError: When the file carries no usable row, naming the by-age
        export as the likely cause.
    """
    wanted = set(repos) if repos is not None else None
    imported: Counter[str] = Counter()
    seen: Counter[str] = Counter()
    rows = 0
    for row in read_csv(path):
        slug = (row.get("Repository") or "").strip()
        if not slug:
            continue
        repo = canonical_url(slug)
        if wanted is not None and repo not in wanted:
            continue
        rows += 1
        day = parse_csv_day(row.get("Date") or "")
        if day is None or day < GITHUB_EPOCH:
            continue
        try:
            stars = int((row.get("Stars") or "").strip())
        except ValueError:
            continue
        seen[repo] += 1
        record = MetricRecord(
            repo, "stars", day.isoformat(), str(stars), "star-history"
        )
        if upsert(records, record):
            imported[repo] += 1
    if rows and not seen:
        msg = (
            f"No usable row in {path}: every date predates GitHub. This is the "
            "by-age export, which measures each curve from epoch zero. Export "
            "the calendar variant instead."
        )
        raise ValueError(msg)
    return [
        SampleOutcome(
            repo,
            repo,
            phase="import",
            rows=imported[repo],
            note=f"{seen[repo]} rows",
        )
        for repo in sorted(seen)
    ]


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

    grouped: dict[str, list[tuple[date, int]]] = {}
    for name, repo in collected_subjects(subjects, predecessors).items():
        points = sorted(
            (date.fromisoformat(held.day), held.count)
            for held in records.values()
            if held.repo == repo and held.metric == metric
        )
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
