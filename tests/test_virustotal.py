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

"""Tests for VirusTotal module."""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from repomatic.virustotal import (
    SCAN_HEADERS,
    DetectionStats,
    ScanRecord,
    ScanResult,
    load_scan_records,
    poll_detection_stats,
    records_from_release_notes,
    records_from_results,
    upsert_scan_records,
)


@pytest.fixture
def sample_records():
    """Two scan records spanning two releases."""
    return [
        ScanRecord(
            tag="v1.1.0",
            filename="app-1.1.0-windows-x64.exe",
            sha256="b" * 64,
            scanned="2026-07-06",
            stats=DetectionStats(malicious=3, suspicious=1, undetected=60, harmless=8),
        ),
        ScanRecord(
            tag="v1.0.0",
            filename="app-1.0.0-linux-x64.bin",
            sha256="a" * 64,
            scanned="2026-07-02",
            stats=DetectionStats(malicious=0, suspicious=0, undetected=65, harmless=7),
        ),
    ]


# --- DetectionStats ---


def test_detection_stats_clean():
    """Clean file: zero flagged out of total engines."""
    stats = DetectionStats(malicious=0, suspicious=0, undetected=65, harmless=7)
    assert stats.flagged == 0
    assert stats.total == 72
    assert str(stats) == "0 / 72"


def test_detection_stats_flagged():
    """File with detections: malicious + suspicious count."""
    stats = DetectionStats(malicious=3, suspicious=1, undetected=60, harmless=8)
    assert stats.flagged == 4
    assert stats.total == 72
    assert str(stats) == "4 / 72"


# --- ScanRecord ---


def test_scan_record_roundtrip(sample_records):
    """Records survive the CSV row they are committed as."""
    for record in sample_records:
        assert ScanRecord.from_row(dict(zip(SCAN_HEADERS, record.as_row()))) == record


def test_scan_record_key(sample_records):
    """Identity is the (sha256, scanned) pair."""
    assert sample_records[0].key == ("b" * 64, "2026-07-06")


# --- records_from_results ---


def test_records_from_results_skips_pending():
    """Results without detection stats produce no record."""
    results = [
        ScanResult(
            filename="app-linux.bin",
            sha256="a" * 64,
            analysis_url="https://www.virustotal.com/gui/file/" + "a" * 64,
            detection_stats=DetectionStats(
                malicious=0, suspicious=0, undetected=65, harmless=7
            ),
        ),
        ScanResult(
            filename="app-windows.exe",
            sha256="b" * 64,
            analysis_url="https://www.virustotal.com/gui/file/" + "b" * 64,
        ),
    ]
    records = records_from_results(results, "v1.0.0", scanned="2026-07-06")
    assert len(records) == 1
    assert records[0].tag == "v1.0.0"
    assert records[0].filename == "app-linux.bin"
    assert records[0].scanned == "2026-07-06"
    assert records[0].stats.total == 72


def test_records_from_results_stamps_today():
    """The scan date defaults to today in YYYY-MM-DD form."""
    results = [
        ScanResult(
            filename="app.bin",
            sha256="a" * 64,
            analysis_url="https://www.virustotal.com/gui/file/" + "a" * 64,
            detection_stats=DetectionStats(
                malicious=0, suspicious=0, undetected=1, harmless=0
            ),
        ),
    ]
    (record,) = records_from_results(results, "v1.0.0")
    year, month, day = record.scanned.split("-")
    assert len(year) == 4
    assert len(month) == 2
    assert len(day) == 2


# --- records_from_release_notes ---


def test_records_from_release_notes():
    """Legacy release-note table rows become at-release snapshots."""
    body = (
        "## Release notes\n\n- A change.\n\n---\n\n"
        "### \U0001f6e1️ VirusTotal scans\n\n"
        "| Binary | Detections | Analysis |\n"
        "| --- | --- | --- |\n"
        "| [`papaya-1.0.0-linux-x64.bin`](https://github.com/o/r/releases/"
        "download/v1.0.0/papaya-1.0.0-linux-x64.bin) | 0 / 62 "
        f"| [View scan](https://www.virustotal.com/gui/file/{'a' * 64}) |\n"
        "| [`papaya-1.0.0-windows-x64.exe`](https://github.com/o/r/releases/"
        "download/v1.0.0/papaya-1.0.0-windows-x64.exe) | 12 / 70 "
        f"| [View scan](https://www.virustotal.com/gui/file/{'b' * 64}) |\n"
    )
    records = records_from_release_notes(body, "v1.0.0", "2026-06-27")
    assert len(records) == 2
    assert records[0].filename == "papaya-1.0.0-linux-x64.bin"
    assert records[0].sha256 == "a" * 64
    assert records[0].scanned == "2026-06-27"
    assert records[0].tag == "v1.0.0"
    assert str(records[0].stats) == "0 / 62"
    assert records[1].stats.flagged == 12
    assert records[1].stats.total == 70


def test_records_from_release_notes_skips_rows_without_detections():
    """Link-only and pending rows carry no snapshot to recover."""
    body = (
        "| Binary | Analysis |\n"
        "| --- | --- |\n"
        "| [`papaya.bin`](https://example.com/dl) "
        f"| [View scan](https://www.virustotal.com/gui/file/{'a' * 64}) |\n"
        "| `papaya.exe` | *pending* "
        f"| [View scan](https://www.virustotal.com/gui/file/{'b' * 64}) |\n"
    )
    assert records_from_release_notes(body, "v1.0.0", "2026-06-27") == []


def test_records_from_release_notes_plain_body():
    """Notes without a VirusTotal table yield nothing."""
    body = "## Release\n\n- Fixed a bug in the `parser` | tokenizer."
    assert records_from_release_notes(body, "v1.0.0", "2026-01-01") == []


# --- load_scan_records / upsert_scan_records ---


def test_load_scan_records_missing_file(tmp_path):
    """A missing history file loads as an empty list."""
    assert load_scan_records(tmp_path / "missing.csv") == []


def test_load_scan_records_malformed(tmp_path):
    """A corrupt history file raises instead of being silently clobbered."""
    path = tmp_path / "scans.csv"
    path.write_text("", encoding="UTF-8")
    with pytest.raises(ValueError, match="Malformed scan records"):
        load_scan_records(path)


def test_upsert_scan_records_creates_file(tmp_path, sample_records):
    """The history file and its parent directories are created on demand."""
    path = tmp_path / "assets" / "scans.csv"
    assert upsert_scan_records(path, sample_records) is True
    assert load_scan_records(path) == sorted(sample_records, key=lambda r: r.tag)


def test_upsert_scan_records_empty_creates_file(tmp_path):
    """No new records still normalizes an empty history file into existence.

    A header-only CSV is what later pipeline steps can rely on the presence of,
    and what `read_csv` distinguishes from a truncated file.
    """
    path = tmp_path / "scans.csv"
    assert upsert_scan_records(path, []) is True
    assert path.read_text(encoding="UTF-8") == ",".join(SCAN_HEADERS) + "\n"


def test_upsert_scan_records_idempotent(tmp_path, sample_records):
    """Re-upserting the same records reports no change."""
    path = tmp_path / "scans.csv"
    upsert_scan_records(path, sample_records)
    assert upsert_scan_records(path, sample_records) is False


def test_upsert_scan_records_replaces_same_day(tmp_path, sample_records):
    """A record with the same (sha256, scanned) identity is replaced."""
    path = tmp_path / "scans.csv"
    upsert_scan_records(path, sample_records)
    rescan = ScanRecord(
        tag="v1.1.0",
        filename="app-1.1.0-windows-x64.exe",
        sha256="b" * 64,
        scanned="2026-07-06",
        stats=DetectionStats(malicious=1, suspicious=0, undetected=63, harmless=8),
    )
    assert upsert_scan_records(path, [rescan]) is True
    loaded = load_scan_records(path)
    assert len(loaded) == 2
    assert loaded[-1].stats.flagged == 1


def test_upsert_scan_records_sorted_by_release_version(tmp_path):
    """Records are ordered by release version, one line each.

    Sorting on the parsed version rather than on the tag string is what keeps
    `v9` before `v10`, and one line per record is what keeps a release's append
    readable in a diff.
    """
    path = tmp_path / "scans.csv"
    upsert_scan_records(
        path,
        [
            ScanRecord(
                tag="v2.0.0",
                filename="app.bin",
                sha256="b" * 64,
                scanned="2026-07-06",
                stats=DetectionStats(
                    malicious=1, suspicious=0, undetected=2, harmless=0
                ),
            ),
            ScanRecord(
                tag="v1.9.0",
                filename="app.bin",
                sha256="a" * 64,
                scanned="2026-07-01",
                stats=DetectionStats(
                    malicious=0, suspicious=0, undetected=3, harmless=0
                ),
            ),
        ],
    )
    expected = (
        ",".join(SCAN_HEADERS) + "\n"
        f"v1.9.0,app.bin,{'a' * 64},2026-07-01,0,0,3,0\n"
        f"v2.0.0,app.bin,{'b' * 64},2026-07-06,1,0,2,0\n"
    )
    assert path.read_text(encoding="UTF-8") == expected


# --- poll_detection_stats ---


def test_poll_detection_stats():
    """Poll returns enriched results when analysis is complete."""
    results = [
        ScanResult(
            filename="app.bin",
            sha256="a" * 64,
            analysis_url="https://www.virustotal.com/gui/file/" + "a" * 64,
        ),
    ]

    mock_file_obj = SimpleNamespace(
        last_analysis_stats={
            "malicious": 0,
            "suspicious": 0,
            "undetected": 65,
            "harmless": 7,
            "type-unsupported": 5,
            "timeout": 0,
            "confirmed-timeout": 0,
            "failure": 0,
        }
    )

    mock_client = MagicMock()
    mock_client.get_object.return_value = mock_file_obj
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)

    with (
        patch("repomatic.virustotal.vt.Client", return_value=mock_client),
        patch("repomatic.virustotal.time.sleep"),
    ):
        enriched = poll_detection_stats("key", results, timeout=60)

    assert len(enriched) == 1
    assert enriched[0].detection_stats is not None
    assert enriched[0].detection_stats.flagged == 0
    assert enriched[0].detection_stats.total == 72


def test_poll_detection_stats_timeout():
    """Polling timeout produces results with None detection_stats."""
    results = [
        ScanResult(
            filename="app.bin",
            sha256="a" * 64,
            analysis_url="https://www.virustotal.com/gui/file/" + "a" * 64,
        ),
    ]

    # Simulate analysis not ready (all zeros).
    mock_file_obj = SimpleNamespace(
        last_analysis_stats={
            "malicious": 0,
            "suspicious": 0,
            "undetected": 0,
            "harmless": 0,
            "type-unsupported": 0,
            "timeout": 0,
            "confirmed-timeout": 0,
            "failure": 0,
        }
    )

    mock_client = MagicMock()
    mock_client.get_object.return_value = mock_file_obj
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)

    # Make time.monotonic() advance past the deadline after first attempt.
    start = time.monotonic()
    times = iter([start, start, start + 1000, start + 1000])

    with (
        patch("repomatic.virustotal.vt.Client", return_value=mock_client),
        patch("repomatic.virustotal.time.sleep"),
        patch("repomatic.virustotal.time.monotonic", side_effect=times),
    ):
        enriched = poll_detection_stats("key", results, timeout=60)

    assert len(enriched) == 1
    assert enriched[0].detection_stats is None


def test_poll_detection_stats_empty():
    """Polling with no results returns empty list."""
    assert poll_detection_stats("key", []) == []
