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

"""Upload release binaries to VirusTotal and record detection snapshots.

Submits compiled binaries (`.bin`, `.exe`) to the VirusTotal API for malware
scanning. This seeds antivirus vendor databases with the signatures of freshly
built binaries, which keeps false-positive rates in check for downstream
distributors.

Detection statistics polled after an upload are appended to a JSON history
file, one record per binary per scan date. The `sync-binaries` command renders
that history into the binaries catalog page (`docs/binaries.md`).

```{note}
Scan results are deliberately kept out of GitHub release notes: a raw
`flagged / total` count next to a download link reads as a malware verdict to
visitors, when it is almost always Nuitka onefile false positives. See
[kdeldycke/meta-package-manager#1911](https://github.com/kdeldycke/meta-package-manager/issues/1911)
for the confusion this caused. The catalog page provides the context release
notes cannot.
```

```{note}
The free-tier API allows 4 requests per minute. All API calls (uploads and
polls) are rate-limited with a sleep between each request.
```
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import vt
from packaging.version import InvalidVersion, Version

from .binary import compute_file_sha256
from .tabular import read_csv, render_csv, write_csv

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Mapping
    from typing import Any

FREE_TIER_RATE_LIMIT = 4
"""VirusTotal free-tier request budget, in API calls per minute.

The single source for the upload and polling pace: the `scan-virustotal`
CLI default and both client functions below derive from it.
"""

SCAN_HEADERS = (
    "tag",
    "filename",
    "sha256",
    "scanned",
    "malicious",
    "suspicious",
    "undetected",
    "harmless",
)
"""Columns of the committed scan history, in file order.

The release and the file it identifies first, then the four verdict counts, so
the table reads left to right from what was scanned to what came back. Rows are
ordered by release version rather than alphabetically, which a plain sort of
the tag strings would get wrong past `v9`.
"""

VIRUSTOTAL_GUI_URL = "https://www.virustotal.com/gui/file/{sha256}"
"""URL template for the VirusTotal file analysis page."""

_LEGACY_NOTES_ROW_RE = re.compile(
    r"\|\s*\[?`(?P<filename>[^`|]+)`[^|]*"
    r"\|\s*(?P<flagged>\d+)\s*/\s*(?P<total>\d+)\s*"
    r"\|[^|]*https://www\.virustotal\.com/gui/file/(?P<sha256>[a-f0-9]{64})"
)
"""Matches one row of the legacy VirusTotal table in GitHub release notes.

The retired release-body format was
`| [\\`file\\`](url) | flagged / total | [View scan](analysis-url) |`.
Rows without a numeric Detections cell (link-only tables, `*pending*`
placeholders) deliberately don't match: they carry no snapshot to recover.
"""


@dataclass(frozen=True)
class DetectionStats:
    """Detection statistics from a completed VirusTotal analysis.

    Stores only the four categories that constitute a definitive verdict.
    `type-unsupported`, `timeout`, and `failure` from the API response
    are excluded from the total.
    """

    malicious: int
    """Number of engines that flagged the file as malicious."""

    suspicious: int
    """Number of engines that flagged the file as suspicious."""

    undetected: int
    """Number of engines that found no threat."""

    harmless: int
    """Number of engines that classified the file as harmless."""

    @property
    def flagged(self) -> int:
        """Total engines that flagged the file (malicious + suspicious)."""
        return self.malicious + self.suspicious

    @property
    def total(self) -> int:
        """Total engines that produced a definitive verdict."""
        return self.malicious + self.suspicious + self.undetected + self.harmless

    def __str__(self) -> str:
        return f"{self.flagged} / {self.total}"


@dataclass(frozen=True)
class ScanResult:
    """Result of uploading a single file to VirusTotal."""

    filename: str
    """Original filename of the uploaded binary."""

    sha256: str
    """SHA-256 hash of the file content."""

    analysis_url: str
    """VirusTotal web GUI URL for the file analysis."""

    detection_stats: DetectionStats | None = None
    """Detection statistics, or `None` if analysis is still pending."""


@dataclass(frozen=True)
class ScanRecord:
    """A detection snapshot for one binary, taken on a given date.

    Records accumulate in a JSON history file (see
    {func}`upsert_scan_records`) committed to the repository. Each record
    freezes the `flagged / total` verdict counts at scan time, so the history
    supports trend analysis across releases even after VirusTotal re-analyzes
    the files or vendors process false-positive reports.
    """

    tag: str
    """Git tag of the release the binary belongs to (e.g. `v1.2.3`)."""

    filename: str
    """Filename of the scanned binary."""

    sha256: str
    """SHA-256 hash of the file content."""

    scanned: str
    """Scan date in `YYYY-MM-DD` format."""

    stats: DetectionStats
    """Detection statistics at scan time."""

    @property
    def key(self) -> tuple[str, str]:
        """Deduplication identity: the same file scanned on the same day."""
        return (self.sha256, self.scanned)

    def as_row(self) -> tuple[str, ...]:
        """Flatten to one CSV row, in {data}`SCAN_HEADERS` order."""
        return (
            self.tag,
            self.filename,
            self.sha256,
            self.scanned,
            str(self.stats.malicious),
            str(self.stats.suspicious),
            str(self.stats.undetected),
            str(self.stats.harmless),
        )

    @classmethod
    def from_row(cls, data: Mapping[str, Any]) -> ScanRecord:
        """Rebuild a record from one parsed CSV row, or a legacy JSON mapping.

        Both shapes carry the same eight keys, so one reader covers a store
        mid-migration as well as one already converted.
        """
        return cls(
            tag=data["tag"],
            filename=data["filename"],
            sha256=data["sha256"],
            scanned=data["scanned"],
            stats=DetectionStats(
                malicious=int(data["malicious"]),
                suspicious=int(data["suspicious"]),
                undetected=int(data["undetected"]),
                harmless=int(data["harmless"]),
            ),
        )


def scan_files(
    api_key: str,
    file_paths: list[Path],
    rate_limit: int = FREE_TIER_RATE_LIMIT,
) -> list[ScanResult]:
    """Upload files to VirusTotal and return scan results.

    Uses the synchronous `vt.Client` API. Sleeps between uploads to respect
    the free-tier rate limit.

    :param api_key: VirusTotal API key.
    :param file_paths: Paths to binary files to upload.
    :param rate_limit: Maximum requests per minute (free tier: 4).
    :return: List of scan results with analysis URLs.
    """
    results: list[ScanResult] = []
    delay = 60.0 / rate_limit

    with vt.Client(api_key) as client:
        for i, path in enumerate(sorted(file_paths)):
            if i > 0:
                logging.info(f"Rate limiting: waiting {delay:.0f}s before next upload.")
                time.sleep(delay)

            sha256 = compute_file_sha256(path)
            analysis_url = VIRUSTOTAL_GUI_URL.format(sha256=sha256)

            try:
                logging.info(f"Uploading {path.name} to VirusTotal...")
                with path.open("rb") as f:
                    client.scan_file(f)
                logging.info(f"Uploaded {path.name}: {analysis_url}")
                results.append(
                    ScanResult(
                        filename=path.name,
                        sha256=sha256,
                        analysis_url=analysis_url,
                    )
                )
            except vt.APIError:
                logging.exception(f"Failed to upload {path.name}, skipping.")

    return results


def poll_detection_stats(
    api_key: str,
    results: list[ScanResult],
    rate_limit: int = FREE_TIER_RATE_LIMIT,
    timeout: int = 600,
) -> list[ScanResult]:
    """Poll VirusTotal for detection statistics of previously uploaded files.

    Queries ``GET /files/{sha256}`` for each file until analysis completes or
    the timeout is reached. Respects the free-tier rate limit for all API calls.

    :param api_key: VirusTotal API key.
    :param results: Scan results from a previous upload.
    :param rate_limit: Maximum API requests per minute (shared with uploads).
    :param timeout: Maximum seconds to wait for all analyses to complete.
    :return: Results with `detection_stats` populated (or `None` for
        files whose analysis did not complete before the timeout).
    """
    if not results:
        return []

    delay = 60.0 / rate_limit
    deadline = time.monotonic() + timeout
    # Map SHA-256 to result for lookup.
    by_sha = {r.sha256: r for r in results}
    stats: dict[str, DetectionStats] = {}
    pending = set(by_sha)
    request_count = 0

    with vt.Client(api_key) as client:
        while pending and time.monotonic() < deadline:
            for sha256 in list(pending):
                if time.monotonic() >= deadline:
                    break

                if request_count > 0:
                    logging.info(
                        f"Rate limiting: waiting {delay:.0f}s before next poll."
                    )
                    time.sleep(delay)
                request_count += 1

                try:
                    file_obj = client.get_object(f"/files/{sha256}")
                    raw_stats = file_obj.last_analysis_stats
                    # A freshly uploaded file may return all zeros before
                    # analysis begins.
                    if sum(raw_stats.values()) > 0:
                        stats[sha256] = DetectionStats(
                            malicious=raw_stats.get("malicious", 0),
                            suspicious=raw_stats.get("suspicious", 0),
                            undetected=raw_stats.get("undetected", 0),
                            harmless=raw_stats.get("harmless", 0),
                        )
                        pending.discard(sha256)
                        r = by_sha[sha256]
                        logging.info(
                            f"Analysis complete for {r.filename}: {stats[sha256]}"
                        )
                except vt.APIError:
                    logging.debug(f"File {sha256[:12]}... not yet indexed, will retry.")

    if pending:
        filenames = [by_sha[s].filename for s in pending]
        logging.warning(
            f"Polling timed out after {timeout}s. Missing results for: {filenames}"
        )

    return [
        ScanResult(
            filename=r.filename,
            sha256=r.sha256,
            analysis_url=r.analysis_url,
            detection_stats=stats.get(r.sha256),
        )
        for r in results
    ]


def records_from_results(
    results: list[ScanResult],
    tag: str,
    scanned: str | None = None,
) -> list[ScanRecord]:
    """Build history records from scan results whose analysis completed.

    Results still pending (no detection statistics) are skipped: a record
    without verdict counts carries no information the release assets don't
    already provide.

    :param results: Scan results, typically from {func}`poll_detection_stats`.
    :param tag: Git tag of the release the binaries belong to.
    :param scanned: Snapshot date in `YYYY-MM-DD` format. Today (UTC) when
        `None`.
    :return: One record per result with detection statistics.
    """
    if scanned is None:
        scanned = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return [
        ScanRecord(
            tag=tag,
            filename=r.filename,
            sha256=r.sha256,
            scanned=scanned,
            stats=r.detection_stats,
        )
        for r in results
        if r.detection_stats is not None
    ]


def records_from_release_notes(
    body: str,
    tag: str,
    scanned: str,
) -> list[ScanRecord]:
    """Recover detection snapshots from a legacy release-notes table.

    Before the scan history file existed, the release pipeline appended a
    VirusTotal table to GitHub release notes, with a `flagged / total`
    Detections cell frozen minutes after publication. Those cells are genuine
    at-release snapshots, so `sync-binaries --backfill-records` harvests them
    to seed the history for releases that predate the file.

    ```{note}
    The legacy table only recorded the flagged and total aggregates, not the
    malicious/suspicious/undetected/harmless split. The split is rebuilt as
    flagged = malicious and the remainder = undetected, which is lossless for
    everything the catalog consumes (`flagged` and `total`).
    ```

    :param body: Release notes markdown.
    :param tag: Git tag of the release.
    :param scanned: Snapshot date, normally the release publication date.
    :return: One record per table row carrying a numeric Detections cell.
    """
    records = []
    for match in _LEGACY_NOTES_ROW_RE.finditer(body):
        flagged = int(match["flagged"])
        total = int(match["total"])
        if total < flagged:
            continue
        records.append(
            ScanRecord(
                tag=tag,
                filename=match["filename"],
                sha256=match["sha256"],
                scanned=scanned,
                stats=DetectionStats(
                    malicious=flagged,
                    suspicious=0,
                    undetected=total - flagged,
                    harmless=0,
                ),
            )
        )
    return records


def _record_sort_key(record: ScanRecord) -> tuple[Version, str, str, str]:
    """Order records by release version, then filename, then scan date.

    Tags that don't parse as versions sort first under `Version("0")`, with
    the raw tag string as tie-breaker.
    """
    try:
        version = Version(record.tag.removeprefix("v"))
    except InvalidVersion:
        version = Version("0")
    return (version, record.tag, record.filename, record.scanned)


def load_scan_records(path: Path) -> list[ScanRecord]:
    """Load scan records from the CSV history file.

    ```{note}
    A repository whose history predates the CSV store carries the same records
    in a sibling `.json`, and is read from there when the CSV is absent. The
    next {func}`upsert_scan_records` write lands as CSV, so a repository
    migrates on its first release after upgrading without anyone converting
    anything. The stale `.json` is then inert and can be deleted.
    ```

    :param path: Path to the CSV file.
    :return: The records, or an empty list when neither file exists.
    :raises ValueError: When a file exists but cannot be parsed. Loud on
        purpose: a corrupt history must never be silently clobbered by the
        next {func}`upsert_scan_records` write.
    """
    try:
        rows: list[Mapping[str, Any]] = list(read_csv(path))
        if not rows:
            legacy = path.with_suffix(".json")
            if legacy.exists():
                logging.info(f"Reading legacy scan records from {legacy}.")
                rows = json.loads(legacy.read_text(encoding="UTF-8"))
        return [ScanRecord.from_row(row) for row in rows]
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"Malformed scan records file {path}: {exc}") from exc


def upsert_scan_records(path: Path, new_records: list[ScanRecord]) -> bool:
    """Merge new records into the JSON history file at *path*.

    Records sharing the same `(sha256, scanned)` identity are replaced, so
    re-running a scan the same day is idempotent. The file is created (with
    its parent directories) when missing, and always rewritten in normalized
    form: sorted by version, filename, and scan date, serialized with the
    same layout Biome's JSON formatter produces so the `format-json` autofix
    job never rewrites it.

    :param path: Path to the JSON history file.
    :param new_records: Records to merge in.
    :return: `True` when the file content changed.
    """
    merged = {record.key: record for record in load_scan_records(path)}
    for record in new_records:
        merged[record.key] = record
    ordered = sorted(merged.values(), key=_record_sort_key)
    content = render_csv(SCAN_HEADERS, [record.as_row() for record in ordered])
    if not write_csv(path, content):
        logging.info(f"Scan records in {path} already up to date.")
        return False
    logging.info(f"Wrote {len(ordered)} scan record(s) to {path}.")
    return True
