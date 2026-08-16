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

"""Accumulate the star history of a set of repositories, one point per day.

Replaces the third-party star-history charts a project used to embed. On
2026-06-30 GitHub restricted the REST stargazer endpoints to a repository's own
admins and collaborators, and closed the equivalent GraphQL field on
2026-07-17, which left every such embed on the web rendering an error card.

What survived is the aggregate `stargazers_count` on the repository object,
which stays public for everyone. That single scalar is the whole basis of this
module: sampled on a schedule it accumulates into a history nobody can revoke.

```{note}
Four collectors, because the past is not equally knowable for every repository.

For a repository the token administers, the per-star `starred_at` timestamps
are still served, so its curve is reconstructed exactly, back to its first
star, in a handful of calls.

For everything else only the aggregate is readable. Their history comes from
periodic samples going forward, plus a one-time backfill: either mined from
archived copies of their GitHub pages, each of which states the precise count
on the day it was captured, or imported from a CSV a star-history.com user
exported while that service still worked.
```

```{warning}
A reconstruction and a sample do not measure the same thing, and the difference
is deliberate rather than a defect.

The stargazers API lists only the accounts that *still* have the repository
starred, so a reconstruction attributes today's surviving stars to the dates
they were given: it understates every past date by the number of stars since
withdrawn, converging on the true figure at the present day. Kept on purpose,
since a curve that sags where a project shed followers carries a signal a
monotonic one hides. Each record therefore names its {data}`SOURCES`, so a
reader can always tell which question a point answers.
```
"""

from __future__ import annotations

import csv
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
from itertools import accumulate

from .github.gh import gh_api_json, run_gh_command

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping
    from pathlib import Path
    from typing import Any

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

STAR_SAMPLE_HEADER_DEFS: tuple[tuple[str, str], ...] = (
    ("Series", "series"),
    ("Repository", "repository"),
    ("Stars", "stars"),
    ("Points", "points"),
    ("Note", "note"),
)
"""Column definitions for the `repomatic sample-stars` table.

Lives beside the rows' domain model so the columns and the fields they render
cannot drift apart; the CLI derives its `--sort-by` choices from it.
"""

PREDECESSOR_SUFFIX = ":prior"
"""Marks a predecessor's series key, appended to the series it belongs to.

Keeps the configured series list exactly the columns a chart plots, while still
letting the collectors and the renderer address the extra curve through the
same code paths.
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
    "sample": "Aggregate `stargazers_count` snapshot, contemporaneous.",
    "star-history": "Count at a date, imported from a star-history.com export.",
    "wayback": "Contemporaneous count mined from an archived GitHub page.",
}
"""Provenance vocabulary, recorded per point.

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
known to exist returned 23 plain `503`s and 2 truncated bodies, and no clean
response at all. Since a truncated body still carries the counter, the per-try
success rate that matters was 2 in 25, and eight tries is the point past which
more attempts cost more than the captures they recover.
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


@dataclass(frozen=True)
class StarRecord:
    """One repository's star count on one day, and where the figure came from."""

    repo: str
    """The repository's `owner/name` slug."""

    day: str
    """The reading's date, in `YYYY-MM-DD` form."""

    stars: int
    """How many accounts had the repository starred."""

    source: str
    """Which key of {data}`SOURCES` produced the figure."""

    @property
    def key(self) -> tuple[str, str]:
        """Deduplication identity: one reading per repository per day."""
        return (self.repo, self.day)

    def to_dict(self) -> dict[str, int | str]:
        """Flatten to a JSON-ready mapping, `day` spelled `date` on disk."""
        return {
            "date": self.day,
            "repo": self.repo,
            "source": self.source,
            "stars": self.stars,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> StarRecord:
        """Rebuild a record from its flattened JSON mapping."""
        return cls(
            repo=data["repo"],
            day=data["date"],
            stars=int(data["stars"]),
            source=data["source"],
        )


@dataclass(frozen=True)
class CollectOutcome:
    """What one repository's collection produced, for the CLI to report."""

    name: str
    """Series name the repository is plotted under."""

    repo: str
    """The repository's `owner/name` slug."""

    stars: int | None = None
    """Its current star count, when the collector read one."""

    points: int = 0
    """How many stored points this collector added or moved."""

    note: str = ""
    """Why nothing was collected, empty when something was."""


def collected_repos(
    series_map: Mapping[str, str],
    predecessors: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Every repository a collector touches, keyed by its series name.

    :param series_map: Plotted series, mapping each name to an `owner/name` slug.
    :param predecessors: Retired forerunners, mapping the name of the series
        they belong to onto their own slug.
    :return: The series, plus one entry per forerunner whose key carries
        {data}`PREDECESSOR_SUFFIX` so a caller can tell the two apart.
    """
    repos = dict(series_map)
    for name, slug in (predecessors or {}).items():
        repos[name + PREDECESSOR_SUFFIX] = slug
    return repos


def last_fetch_failure() -> str:
    """Summarize why the most recent {func}`fetch` gave up.

    :return: A tally like `6x HTTP 503, 2x truncated`, or `no response` when
        nothing was recorded.
    """
    tally = ", ".join(
        f"{count}x {reason}" for reason, count in _LAST_FETCH_REASONS.most_common()
    )
    return tally or "no response"


def load_star_records(path: Path) -> dict[tuple[str, str], StarRecord]:
    """Read the committed star history, keyed by repository and date.

    :param path: Path to the JSON history file.
    :return: The records, empty when the file does not exist.
    :raises ValueError: When the file exists but cannot be parsed. Loud on
        purpose: a corrupt history must never be silently clobbered by the
        next {func}`save_star_records` write.
    """
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="UTF-8"))
        records = [StarRecord.from_dict(entry) for entry in data]
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
        msg = f"Malformed star history file {path}: {error}"
        raise ValueError(msg) from error
    return {record.key: record for record in records}


def save_star_records(
    path: Path,
    records: Mapping[tuple[str, str], StarRecord],
) -> bool:
    """Write the history back, sorted and tab-indented.

    Merges whatever is on disk under the caller's own records rather than
    overwriting the file wholesale. A slow backfill flushes after every point
    across a run lasting hours, so it holds a snapshot that goes stale the
    moment anything else records a sample: without the merge its next flush
    would silently drop those points. The history only ever grows, so
    preferring the in-memory copy on a conflict resolves it correctly.

    Serialized with the layout Biome's JSON formatter produces, so the
    `format-json` autofix job never rewrites it.

    :param path: Path to the JSON history file.
    :param records: The records to write, keyed by repository and date.
    :return: `True` when the file content changed.
    """
    merged = {**load_star_records(path), **records}
    ordered = [merged[key].to_dict() for key in sorted(merged)]
    content = json.dumps(ordered, indent="\t", sort_keys=True) + "\n"
    if path.exists() and path.read_text(encoding="UTF-8") == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="UTF-8")
    return True


def upsert(
    records: dict[tuple[str, str], StarRecord],
    record: StarRecord,
) -> bool:
    """Record one point, returning whether it changed anything.

    A re-run on the same day overwrites rather than appends, which is what
    keeps the scheduled job idempotent. A more authoritative source wins over a
    weaker one for the same day, per {data}`SOURCE_RANK`.

    :param records: The in-memory store, mutated in place.
    :param record: The point to store.
    :return: `True` when the store moved.
    """
    previous = records.get(record.key)
    if previous == record:
        return False
    if previous and SOURCE_RANK[record.source] < SOURCE_RANK[previous.source]:
        return False
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


def sample_current(
    records: dict[tuple[str, str], StarRecord],
    name: str,
    repo: str,
    day: str | None = None,
) -> CollectOutcome:
    """Snapshot today's aggregate star count of one repository.

    The scheduled collector, and the only one that works for a repository the
    token does not administer.

    :param records: The in-memory store, mutated in place.
    :param name: Series name the repository is plotted under.
    :param repo: The repository's `owner/name` slug.
    :param day: Reading date in `YYYY-MM-DD` form. Today (UTC) when `None`.
    :return: What the sample produced.
    """
    if day is None:
        day = datetime.now(tz=timezone.utc).date().isoformat()
    payload = gh_api_json(["api", f"repos/{repo}"])
    if not payload:
        # A single unreachable repository must not lose the other samples.
        return CollectOutcome(name, repo, note="unreadable")
    stars = int(payload["stargazers_count"])
    points = int(upsert(records, StarRecord(repo, day, stars, "sample")))
    # Immutable, and free to re-assert: the repository object carries it on
    # every sample, so the origin is recorded without a second call.
    created = str(payload["created_at"])[:10]
    points += int(upsert(records, StarRecord(repo, created, 0, "created")))
    return CollectOutcome(name, repo, stars=stars, points=points)


def reconstruct_from_github(
    records: dict[tuple[str, str], StarRecord],
    name: str,
    repo: str,
) -> CollectOutcome:
    """Reconstruct one repository's curve from per-star timestamps.

    Only works where the token administers the repository; GitHub answers `404`
    rather than `403` on the restricted endpoint for every other. Collapses to
    one cumulative point per day on which the count moved, rather than one per
    star.

    Pagination is all-or-nothing on purpose. A transient failure halfway
    through would otherwise write a truncated cumulative curve over a correct
    one, and every point of it would look exactly as legitimate as the rest.

    :param records: The in-memory store, mutated in place once the whole walk
        succeeded.
    :param name: Series name the repository is plotted under.
    :param repo: The repository's `owner/name` slug.
    :return: What the reconstruction produced.
    """
    per_day: Counter[str] = Counter()
    page = 1
    while True:
        try:
            output = run_gh_command([
                "api",
                f"repos/{repo}/stargazers?per_page=100&page={page}",
                "--header",
                "Accept: application/vnd.github.star+json",
            ])
            batch = json.loads(output)
        except RuntimeError as error:
            detail = str(error).strip().splitlines()
            reason = detail[0][:80] if detail else "unknown error"
            if page == 1 and "Not Found" in str(error):
                return CollectOutcome(name, repo, note="not an admin, not readable")
            return CollectOutcome(
                name, repo, note=f"abandoned on page {page}: {reason}"
            )
        except json.JSONDecodeError:
            return CollectOutcome(name, repo, note=f"unparsable page {page}")
        if not batch:
            break
        for entry in batch:
            per_day[str(entry["starred_at"])[:10]] += 1
        page += 1

    if not per_day:
        return CollectOutcome(name, repo, note="the endpoint answered empty")

    days = sorted(per_day)
    points = 0
    for day, total in zip(days, accumulate(per_day[each] for each in days)):
        points += upsert(records, StarRecord(repo, day, total, "github"))
    return CollectOutcome(name, repo, stars=per_day.total(), points=points)


def wayback_captures(repo: str) -> list[str] | None:
    """List one archived capture per month of a repository's GitHub page.

    :param repo: The repository's `owner/name` slug.
    :return: The capture timestamps, or `None` when the index itself could not
        be read. That is not the same answer as an empty list and must not be
        reported as one: the archive fails this query as readily as any other,
        and a run treating the outage as "never archived" skips the repository
        silently and for good.
    """
    query = urllib.parse.urlencode({
        "url": f"github.com/{repo}",
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
        # Unparseable is a fault, not an absence: same reasoning as above.
        return None


def backfill_wayback(
    records: dict[tuple[str, str], StarRecord],
    name: str,
    repo: str,
    store: Path | None = None,
) -> CollectOutcome:
    """Mine contemporaneous star counts from archived copies of a GitHub page.

    The only route to the past of a repository the token cannot administer, and
    the only one reporting what the counter actually read on the day rather
    than what survives today.

    :param records: The in-memory store, mutated in place.
    :param name: Series name the repository is plotted under.
    :param repo: The repository's `owner/name` slug.
    :param store: History file to flush to after every recovered point, since
        a run spans many minutes of a flaky remote. Skipped when `None`.
    :return: What the backfill produced.
    """
    if any(
        record.repo == repo and record.source == "github" for record in records.values()
    ):
        # An exact reconstruction already covers this repository, and mining it
        # would only add a second, differently-measured curve over the same
        # dates. The archives are slow and rate-limited: spend them on the
        # repositories that have no other source of history.
        return CollectOutcome(name, repo, note="already reconstructed exactly")

    stamps = wayback_captures(repo)
    if stamps is None:
        # Loud, and distinct from "nothing was ever archived": this repository
        # still has a past to mine, so the next run must come back to it.
        note = f"capture index unreadable ({last_fetch_failure()}), retry later"
        return CollectOutcome(name, repo, note=note)

    points = 0
    for stamp in stamps:
        day = f"{stamp[0:4]}-{stamp[4:6]}-{stamp[6:8]}"
        if (repo, day) in records:
            continue
        # Paced deliberately. The archive answers 503 to every URL form once a
        # sustained crawl exhausts its budget, and it stays shut for a while: a
        # run racing through the captures finishes by collecting nothing.
        time.sleep(WAYBACK_REQUEST_DELAY)
        payload = fetch(
            f"https://web.archive.org/web/{stamp}id_/https://github.com/{repo}",
            tries=WAYBACK_PAGE_TRIES,
        )
        if not payload:
            logging.info(f"  {repo} {day}: unreachable ({last_fetch_failure()})")
            continue
        stars = read_star_counter(payload.decode("utf-8", errors="replace"))
        if stars is None:
            logging.info(f"  {repo} {day}: no counter found")
            continue
        if upsert(records, StarRecord(repo, day, stars, "wayback")):
            points += 1
            if store is not None:
                save_star_records(store, records)
        logging.info(f"  {repo} {day}: {stars} stars")
    return CollectOutcome(name, repo, points=points, note=f"{len(stamps)} captures")


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
    records: dict[tuple[str, str], StarRecord],
    path: Path,
    slugs: Iterable[str] | None = None,
) -> list[CollectOutcome]:
    """Import the calendar export a star-history.com user downloaded.

    That service reconstructed its curves from the same stargazer endpoint
    GitHub has since closed, so an export taken while it worked is the only
    surviving record of the past for a repository nobody administers and the
    archives never captured.

    Its by-age export is refused rather than imported: that variant measures
    every curve from epoch zero, so its rows land in the 1970s and would enter
    the store as readings four decades before the repository existed.

    :param records: The in-memory store, mutated in place.
    :param path: The exported CSV.
    :param slugs: Only import rows whose repository is listed here. Every row
        when `None`.
    :return: One outcome per repository the file covered.
    :raises ValueError: When the file carries no usable row, naming the by-age
        export as the likely cause.
    """
    wanted = set(slugs) if slugs is not None else None
    imported: Counter[str] = Counter()
    seen: Counter[str] = Counter()
    rows = 0
    with path.open(encoding="UTF-8", newline="") as handle:
        for row in csv.DictReader(handle):
            repo = (row.get("Repository") or "").strip()
            if not repo or (wanted is not None and repo not in wanted):
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
            record = StarRecord(repo, day.isoformat(), stars, "star-history")
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
        CollectOutcome(repo, repo, points=imported[repo], note=f"{seen[repo]} rows")
        for repo in sorted(seen)
    ]


def series(
    records: Mapping[tuple[str, str], StarRecord],
    series_map: Mapping[str, str],
    predecessors: Mapping[str, str] | None = None,
) -> dict[str, list[tuple[date, int]]]:
    """Group the history into one chronological series per plotted name.

    :param records: The store, keyed by repository and date.
    :param series_map: Plotted series, mapping each name to an `owner/name` slug.
    :param predecessors: Retired forerunners, keyed by the series they precede.
    :return: One sorted list of `(day, stars)` per series that has any point,
        forerunners under their {data}`PREDECESSOR_SUFFIX` key.
    """
    grouped: dict[str, list[tuple[date, int]]] = {}
    for name, repo in collected_repos(series_map, predecessors).items():
        points = sorted(
            (date.fromisoformat(record.day), record.stars)
            for record in records.values()
            if record.repo == repo
        )
        if points:
            grouped[name] = points

    # A forerunner's line stops where its successor's begins. An archived
    # repository keeps collecting the odd star to this day, and plotting that
    # tail would run it the whole width of the chart alongside the successor,
    # reading as two projects living side by side. Cutting it at the handover
    # shows what actually happened: one audience stopped being counted here and
    # started being counted there. The store keeps the discarded points, so the
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
