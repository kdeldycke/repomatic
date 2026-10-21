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

from functools import partial
from unittest.mock import MagicMock, patch

import pytest
from click_extra.testing import CliRunner

from repomatic.cli.main import repomatic
from repomatic.http import FetchError
from repomatic.release.virustotal import (
    RELEASE_ASSET_POLL_ATTEMPTS,
    SCAN_HEADERS,
    UPLOAD_REQUESTS,
    VIRUSTOTAL_API_URL,
    DetectionStats,
    RequestPacer,
    ScanRecord,
    ScanResult,
    _multipart_body,
    download_release_binaries,
    load_scan_records,
    poll_detection_stats,
    records_from_results,
    scan_files,
    upload_file,
    upsert_scan_records,
)
from tests.conftest import patch_gh as shared_patch_gh

patch_gh = partial(shared_patch_gh, "repomatic.release.virustotal")
"""The conftest `patch_gh`, bound to this module's dispatch points."""


class FakeClock:
    """A monotonic clock that only `sleep` moves, recording every sleep."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    """Drive the module's `time` calls through a {class}`FakeClock`."""
    fake = FakeClock()
    monkeypatch.setattr("repomatic.release.virustotal.time.monotonic", fake.monotonic)
    monkeypatch.setattr("repomatic.release.virustotal.time.sleep", fake.sleep)
    return fake


def _vt_api(**stats: int) -> MagicMock:
    """A `get_json` answering the two VirusTotal reads: upload URLs and reports.

    Every file report carries *stats*, over the four categories at zero.
    """
    report = {
        "malicious": 0,
        "suspicious": 0,
        "undetected": 0,
        "harmless": 0,
        **stats,
    }

    def get_json(url, *, headers=None, timeout=None):
        if url.endswith("/files/upload_url"):
            return {"data": "https://upload.example/one-shot"}
        return {"data": {"attributes": {"last_analysis_stats": report}}}

    return MagicMock(side_effect=get_json)


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


# --- load_scan_records / upsert_scan_records ---


def test_load_scan_records_missing_file(tmp_path):
    """A missing history file loads as an empty list."""
    assert load_scan_records(tmp_path / "missing.csv") == []


def test_load_scan_records_refuses_a_lone_json_history(tmp_path):
    """A history only stored as JSON is reported, never silently replaced."""
    (tmp_path / "scans.json").write_text("[]", encoding="UTF-8")
    with pytest.raises(ValueError, match=r"7\.13\.0"):
        load_scan_records(tmp_path / "scans.csv")


def test_load_scan_records_ignores_json_beside_the_csv(tmp_path, sample_records):
    """Once the CSV exists, a stale JSON beside it is inert."""
    path = tmp_path / "scans.csv"
    upsert_scan_records(path, sample_records)
    (tmp_path / "scans.json").write_text("[]", encoding="UTF-8")
    assert len(load_scan_records(path)) == len(sample_records)


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


# --- RequestPacer / scan_files / poll_detection_stats ---


def test_pacer_spaces_calls_over_the_budget(clock):
    """Each call reserves its share of the minute, an upload two shares."""
    pacer = RequestPacer(per_minute=4)
    pacer.wait(UPLOAD_REQUESTS)
    pacer.wait()
    pacer.wait()
    assert clock.sleeps == [30.0, 15.0]


def test_uploads_and_polls_share_one_budget(tmp_path, clock):
    """The first poll waits on the last upload, like any other call."""
    files = []
    for name in ("papaya-linux-x64.bin", "papaya-windows-x64.exe"):
        path = tmp_path / name
        path.write_bytes(name.encode())
        files.append(path)
    with (
        patch(
            "repomatic.release.virustotal.get_json",
            _vt_api(undetected=65, harmless=7),
        ),
        patch("repomatic.release.virustotal.request") as post,
    ):
        pacer = RequestPacer(per_minute=4)
        results = scan_files("key", files, pacer)
        enriched = poll_detection_stats("key", results, pacer, timeout=600)

    assert post.call_count == 2
    # Two uploads of two calls each, then two polls of one call each.
    assert clock.sleeps == [30.0, 30.0, 15.0]
    assert all(result.detection_stats for result in enriched)


def test_multipart_body_wraps_the_file_part():
    """The body holds one `file` part, framed by the boundary the header names."""
    body, content_type = _multipart_body("papaya.bin", b"\x00\x01binary")
    boundary = content_type.removeprefix("multipart/form-data; boundary=")
    assert boundary and boundary != content_type
    head = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="papaya.bin"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode()
    assert body == head + b"\x00\x01binary" + f"\r\n--{boundary}--\r\n".encode()


def test_upload_file_posts_to_the_single_use_url(tmp_path):
    """An upload reads a one-shot URL, then posts the file there with the key."""
    path = tmp_path / "papaya.bin"
    path.write_bytes(b"binary")
    reads = _vt_api()

    with (
        patch("repomatic.release.virustotal.get_json", reads),
        patch("repomatic.release.virustotal.request") as post,
    ):
        upload_file("key", path)

    assert reads.call_args.args == (f"{VIRUSTOTAL_API_URL}/files/upload_url",)
    assert reads.call_args.kwargs["headers"] == {"x-apikey": "key"}
    assert post.call_args.args == ("https://upload.example/one-shot",)
    assert post.call_args.kwargs["method"] == "POST"
    assert b"binary" in post.call_args.kwargs["data"]
    headers = post.call_args.kwargs["headers"]
    assert headers["x-apikey"] == "key"
    assert headers["Content-Type"].startswith("multipart/form-data; boundary=")


def test_poll_detection_stats():
    """Poll returns enriched results when analysis is complete."""
    results = [
        ScanResult(
            filename="app.bin",
            sha256="a" * 64,
            analysis_url="https://www.virustotal.com/gui/file/" + "a" * 64,
        ),
    ]

    with (
        patch(
            "repomatic.release.virustotal.get_json",
            _vt_api(undetected=65, harmless=7, **{"type-unsupported": 5}),
        ),
        patch("repomatic.release.virustotal.time.sleep"),
    ):
        enriched = poll_detection_stats("key", results, timeout=60)

    assert len(enriched) == 1
    assert enriched[0].detection_stats is not None
    assert enriched[0].detection_stats.flagged == 0
    assert enriched[0].detection_stats.total == 72


def test_poll_detection_stats_timeout(clock):
    """Polling timeout produces results with None detection_stats."""
    results = [
        ScanResult(
            filename="app.bin",
            sha256="a" * 64,
            analysis_url="https://www.virustotal.com/gui/file/" + "a" * 64,
        ),
    ]

    # Analysis not ready: all zeros, until the deadline passes.
    with patch("repomatic.release.virustotal.get_json", _vt_api()):
        enriched = poll_detection_stats("key", results, timeout=60)

    assert len(enriched) == 1
    assert enriched[0].detection_stats is None
    assert clock.now >= 1060


def test_poll_detection_stats_retries_an_unindexed_file(clock):
    """A report VirusTotal has not indexed yet is polled again until the deadline."""
    results = [
        ScanResult(
            filename="app.bin",
            sha256="a" * 64,
            analysis_url="https://www.virustotal.com/gui/file/" + "a" * 64,
        ),
    ]
    missing = MagicMock(side_effect=FetchError("HTTP 404", status=404))

    with patch("repomatic.release.virustotal.get_json", missing):
        enriched = poll_detection_stats("key", results, timeout=60)

    assert enriched[0].detection_stats is None
    assert missing.call_count > 1


def test_poll_detection_stats_empty():
    """Polling with no results returns empty list."""
    assert poll_detection_stats("key", []) == []


# --- download_release_binaries ---


@patch("repomatic.release.virustotal.time.sleep")
def test_download_release_binaries_waits_for_the_asset_list(mock_sleep, tmp_path):
    """The download starts once the release lists an asset, not before."""
    dest = tmp_path / "binaries"
    with patch_gh(
        side_effect=[
            '{"assets": []}',
            '{"assets": [{"name": "papaya-1.2.3-linux-arm64.bin"}]}',
            "",
        ]
    ) as mock_gh:
        download_release_binaries("orchard/papaya", "v1.2.3", dest)

    assert mock_sleep.call_count == 1
    assert mock_gh.call_args_list[-1].args[0] == [
        "release",
        "download",
        "v1.2.3",
        "--repo",
        "orchard/papaya",
        "--dir",
        str(dest),
        "--pattern",
        "*-1.2.3-*.bin",
        "--pattern",
        "*-1.2.3-*.exe",
    ]
    assert dest.is_dir()


@patch("repomatic.release.virustotal.time.sleep")
def test_download_release_binaries_retries_a_failed_read(mock_sleep, tmp_path):
    """A read that fails is retried like one that lists no asset."""
    with patch_gh(
        side_effect=[
            RuntimeError("HTTP 502"),
            '{"assets": [{"name": "papaya-1.2.3-windows-x64.exe"}]}',
            "",
        ]
    ) as mock_gh:
        download_release_binaries("orchard/papaya", "v1.2.3", tmp_path)

    assert mock_gh.call_count == 3
    assert mock_sleep.call_count == 1


@patch("repomatic.release.virustotal.time.sleep")
def test_download_release_binaries_downloads_after_the_last_empty_read(
    mock_sleep, tmp_path
):
    """An asset list that stays empty leaves the verdict to the download.

    No wait follows the last read: the download runs at once, and its own
    failure is what reports a release that ships no binary.
    """
    with (
        patch_gh(
            side_effect=[
                *(['{"assets": []}'] * RELEASE_ASSET_POLL_ATTEMPTS),
                RuntimeError("no assets to download"),
            ]
        ),
        pytest.raises(RuntimeError, match="no assets to download"),
    ):
        download_release_binaries("orchard/papaya", "v1.2.3", tmp_path)

    assert mock_sleep.call_count == RELEASE_ASSET_POLL_ATTEMPTS - 1


# --- scan-virustotal CLI ---


@pytest.mark.parametrize(
    ("args", "message"),
    (
        (["--download"], "--download requires --repo."),
        (["--no-download"], "does not exist."),
    ),
)
def test_scan_virustotal_rejects_an_unusable_target(args, message, tmp_path):
    """Nothing is read or uploaded when the binaries cannot be located."""
    result = CliRunner().invoke(
        repomatic,
        [
            "scan-virustotal",
            "--tag",
            "v1.2.3",
            "--api-key",
            "mango",
            "--binaries-dir",
            str(tmp_path / "missing"),
            *args,
        ],
        env={"GITHUB_REPOSITORY": None},
    )

    assert result.exit_code == 2, result.output
    assert message in result.output
