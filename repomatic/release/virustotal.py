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

Detection statistics polled after an upload are appended to a CSV history, one
record per binary per scan date. The `sync-binaries` command renders that
history into the binaries catalog page (`docs/binaries.md`).

```{note}
Scan results are deliberately kept out of GitHub release notes: a raw
`flagged / total` count next to a download link reads as a malware verdict to
visitors, when it is almost always Nuitka onefile false positives. See
[kdeldycke/meta-package-manager#1911](https://github.com/kdeldycke/meta-package-manager/issues/1911)
for the confusion this caused. The catalog page provides the context release
notes cannot.
```

```{note}
The free-tier API allows 4 requests per minute. Every API call, upload or
poll, goes through one {class}`RequestPacer`, which spaces the calls to stay
within that budget.
```
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import vt
from packaging.version import Version

from ..github.gh import gh_json, run_gh_command
from ..hashing import compute_file_sha256
from ..humanize import today_utc
from ..tabular import load_records, render_csv, write_csv
from ..versions import parse_tag_version
from .binary import BINARY_ASSET_SUFFIXES

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path
    from typing import Any

FREE_TIER_RATE_LIMIT = 4
"""VirusTotal free-tier request budget, in API calls per minute.

The single source for the upload and polling pace: the `scan-virustotal`
CLI default and {class}`RequestPacer` derive from it.
"""

UPLOAD_REQUESTS = 2
"""API calls one file upload costs.

`vt.Client.scan_file` first asks for an upload URL, then posts the file to it.
"""

RELEASE_ASSET_POLL_ATTEMPTS = 6
"""How many times {func}`download_release_binaries` reads a release's assets."""

RELEASE_ASSET_POLL_DELAY = 10
"""Seconds {func}`download_release_binaries` waits between two reads."""

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


class RequestPacer:
    """Space VirusTotal API calls to stay within a per-minute budget.

    One pacer serves every call of a run, uploads and polls alike, so the
    budget covers them all: the first poll waits on the last upload like any
    other call. A call reserves its share of the minute when it starts, so the
    time a slow upload takes counts toward the wait before the next call.
    """

    def __init__(self, per_minute: int = FREE_TIER_RATE_LIMIT) -> None:
        self.interval = 60.0 / per_minute
        """Seconds one API call reserves out of the budget."""
        self._ready_at = 0.0

    def wait(self, calls: int = 1) -> None:
        """Sleep until the budget allows another request, then reserve it.

        :param calls: The API calls the coming request makes, like
            {data}`UPLOAD_REQUESTS` for an upload.
        """
        delay = self._ready_at - time.monotonic()
        if delay > 0:
            logging.info(f"Rate limiting: waiting {delay:.0f}s before the next call.")
            time.sleep(delay)
        self._ready_at = time.monotonic() + calls * self.interval


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

    Records accumulate in a CSV history (see
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
        """Rebuild a record from one parsed CSV row."""
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


def download_release_binaries(repository: str, tag: str, dest: Path) -> None:
    """Download a release's versioned binaries once its asset list shows them.

    A read landing seconds after a release is published can list no asset, and
    `gh release download` then fails with `no assets to download` on a release
    holding every binary. So the asset list is read first, up to
    {data}`RELEASE_ASSET_POLL_ATTEMPTS` times. The download still runs when the
    list stays empty: a release that ships no binary fails there, with the
    message of `gh`.

    The patterns name the versioned files only. The versionless alias copies
    share their digest with a versioned sibling, so scanning them would only
    submit duplicates.

    :param repository: Repository in `owner/repo` format.
    :param tag: Release tag, like `v1.2.3`. The binary filenames carry its
        version, `1.2.3`.
    :param dest: Directory to download into, created when missing.
    :raises RuntimeError: When `gh release download` fails.
    """
    view = ["release", "view", tag, "--repo", repository, "--json", "assets"]
    for attempt in range(1, RELEASE_ASSET_POLL_ATTEMPTS + 1):
        try:
            if (gh_json(view) or {}).get("assets"):
                break
        except RuntimeError as error:
            logging.warning(f"Attempt {attempt}: could not read {tag}: {error}")
        else:
            logging.warning(f"Attempt {attempt}: {tag} lists no asset yet.")
        if attempt < RELEASE_ASSET_POLL_ATTEMPTS:
            time.sleep(RELEASE_ASSET_POLL_DELAY)

    dest.mkdir(parents=True, exist_ok=True)
    version = tag.removeprefix("v")
    patterns = [
        arg
        for suffix in BINARY_ASSET_SUFFIXES
        for arg in ("--pattern", f"*-{version}-*{suffix}")
    ]
    run_gh_command(
        ["release", "download", tag, "--repo", repository, "--dir", str(dest)]
        + patterns
    )


def scan_files(
    api_key: str,
    file_paths: list[Path],
    pacer: RequestPacer | None = None,
) -> list[ScanResult]:
    """Upload files to VirusTotal and return scan results.

    Uses the synchronous `vt.Client` API, spacing the uploads through
    *pacer* to respect the free-tier rate limit.

    :param api_key: VirusTotal API key.
    :param file_paths: Paths to binary files to upload.
    :param pacer: The pacer every API call of the run shares. A fresh one at
        the free-tier rate when `None`.
    :return: List of scan results with analysis URLs.
    """
    if pacer is None:
        pacer = RequestPacer()
    results: list[ScanResult] = []

    with vt.Client(api_key) as client:
        for path in sorted(file_paths):
            pacer.wait(UPLOAD_REQUESTS)
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
    pacer: RequestPacer | None = None,
    timeout: int = 600,
) -> list[ScanResult]:
    """Poll VirusTotal for detection statistics of previously uploaded files.

    Queries ``GET /files/{sha256}`` for each file until analysis completes or
    the timeout is reached, spacing every query through *pacer*.

    :param api_key: VirusTotal API key.
    :param results: Scan results from a previous upload.
    :param pacer: The pacer the uploads of the run went through, so the first
        poll waits on the last upload. A fresh one at the free-tier rate when
        `None`.
    :param timeout: Maximum seconds to wait for all analyses to complete.
    :return: Results with `detection_stats` populated (or `None` for
        files whose analysis did not complete before the timeout).
    """
    if not results:
        return []
    if pacer is None:
        pacer = RequestPacer()

    deadline = time.monotonic() + timeout
    # Map SHA-256 to result for lookup.
    by_sha = {r.sha256: r for r in results}
    stats: dict[str, DetectionStats] = {}
    pending = set(by_sha)

    with vt.Client(api_key) as client:
        while pending and time.monotonic() < deadline:
            for sha256 in list(pending):
                if time.monotonic() >= deadline:
                    break
                pacer.wait()

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
        scanned = today_utc().isoformat()
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


def _record_sort_key(record: ScanRecord) -> tuple[Version, str, str, str]:
    """Order records by release version, then filename, then scan date.

    Tags that don't parse as versions sort first under `Version("0")`, with
    the raw tag string as tie-breaker.
    """
    version = parse_tag_version(record.tag) or Version("0")
    return (version, record.tag, record.filename, record.scanned)


def load_scan_records(path: Path) -> list[ScanRecord]:
    """Load scan records from the CSV history file.

    :param path: Path to the CSV file.
    :return: The records, or an empty list when the file does not exist.
    :raises ValueError: When the file exists but cannot be parsed, or when it
        does not exist but a JSON history sits beside it. Loud on purpose: a
        history must never be silently clobbered by the next
        {func}`upsert_scan_records` write, and a JSON one is the store of a
        release older than `7.13.0`, which nothing reads any more.
    """
    legacy = path.with_suffix(".json")
    if not path.exists() and legacy.exists():
        msg = (
            f"{legacy} holds the scan history in the JSON format of releases"
            f" older than 7.13.0, and {path} does not exist. Convert it to CSV"
            f" with the columns {', '.join(SCAN_HEADERS)}, or delete it to"
            " start a new history."
        )
        raise ValueError(msg)
    return load_records(path, ScanRecord.from_row, "scan records file")


def upsert_scan_records(path: Path, new_records: list[ScanRecord]) -> bool:
    """Merge new records into the CSV history at *path*.

    Records sharing the same `(sha256, scanned)` identity are replaced, so
    re-running a scan the same day is idempotent. The file is created (with
    its parent directories) when missing, and always rewritten in normalized
    form, sorted by version, filename and scan date.

    CSV also keeps the store out of the autofix lane: nothing there rewrites a
    `.csv`, where a committed JSON file has to match whatever layout Biome is
    configured for or `format-json` reformats it right back. See
    `docs/operation-contracts.md` § Naming rules, rule 8, for the rest of that
    reasoning.

    :param path: Path to the CSV history.
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
