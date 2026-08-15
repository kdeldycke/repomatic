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

"""Tests for the binaries catalog generator."""

from __future__ import annotations

from pathlib import Path

import pytest

from repomatic.binaries_page import (
    CSV_HEADERS,
    LEGACY_PAGE_END_MARKER,
    LEGACY_PAGE_START_MARKERS,
    PAGE_END_MARKER,
    PAGE_START_MARKER,
    render_binaries_csv,
    render_chart_section,
    update_binaries_csv,
    update_binaries_page,
)
from repomatic.binary import NUITKA_BUILD_TARGETS
from repomatic.github.releases import ReleaseAsset, ReleaseWithAssets
from repomatic.virustotal import DetectionStats, ScanRecord

REPO = "owner/papaya"

REPO_ROOT = Path(__file__).parent.parent


def _asset(name: str, size: int = 1048576, sha256: str = "") -> ReleaseAsset:
    return ReleaseAsset(
        name=name,
        size=size,
        sha256=sha256,
        download_url=f"https://github.com/{REPO}/releases/download/v1.0.0/{name}",
    )


def _release(
    tag: str,
    assets: tuple[ReleaseAsset, ...],
    date: str = "2026-07-01",
    draft: bool = False,
) -> ReleaseWithAssets:
    return ReleaseWithAssets(
        tag=tag, date=date, draft=draft, prerelease=False, assets=assets
    )


def _record(
    tag: str, sha256: str, scanned: str, flagged: int = 0, total: int = 10
) -> ScanRecord:
    return ScanRecord(
        tag=tag,
        filename="papaya.bin",
        sha256=sha256,
        scanned=scanned,
        stats=DetectionStats(
            malicious=flagged, suspicious=0, undetected=total - flagged, harmless=0
        ),
    )


# --- render_binaries_csv ---


def test_csv_header_row():
    """An empty catalog still carries the header row."""
    assert render_binaries_csv(REPO, [], []) == ",".join(CSV_HEADERS) + "\n"


def test_csv_skips_drafts_and_foreign_tags():
    """Draft releases and non-version tags stay out of the catalog."""
    releases = [
        _release("v1.0.0.dev0", (_asset("papaya-dev.bin"),), draft=True),
        _release("weekly-snapshot", (_asset("papaya-weekly.bin"),)),
        _release("v1.0.0", (_asset("papaya-1.0.0-linux-x64.bin"),)),
    ]
    content = render_binaries_csv(REPO, releases, [])
    assert "papaya-1.0.0-linux-x64.bin" in content
    assert "papaya-dev.bin" not in content
    assert "papaya-weekly.bin" not in content


def test_csv_skips_non_binary_assets():
    """Only .bin and .exe assets become rows."""
    releases = [
        _release(
            "v1.0.0",
            (
                _asset("papaya-1.0.0-linux-x64.bin"),
                _asset("papaya-1.0.0.tar.gz"),
                _asset("notes.md"),
            ),
        ),
    ]
    content = render_binaries_csv(REPO, releases, [])
    assert content.count("\n") == 2
    assert "tar.gz" not in content


def test_csv_collapses_versionless_aliases():
    """A versionless alias collapses onto its versioned digest sibling.

    Assets without a recorded digest predate the aliases and are kept even
    when byte-identical.
    """
    releases = [
        _release(
            "v1.0.0",
            (
                _asset("papaya-1.0.0-linux-x64.bin", sha256="aa" * 32),
                _asset("papaya-linux-x64.bin", sha256="aa" * 32),
                _asset("papaya-1.0.0-windows-x64.exe", sha256="bb" * 32),
                _asset("papaya-windows-x64.exe", sha256="bb" * 32),
                _asset("papaya-1.0.0-macos-arm64.bin"),
                _asset("papaya-macos-arm64.bin"),
            ),
        ),
    ]
    content = render_binaries_csv(REPO, releases, [])
    assert "papaya-1.0.0-linux-x64.bin" in content
    assert "papaya-linux-x64.bin" not in content
    assert "papaya-1.0.0-windows-x64.exe" in content
    assert "papaya-windows-x64.exe" not in content
    # 4 rows survive: 2 versioned digest canonicals, plus both digest-less
    # macos names.
    assert content.count("\n") == 5


def test_csv_rows_newest_version_first():
    """Rows are ordered by descending version, not API order."""
    releases = [
        _release("v1.2.0", (_asset("papaya-1.2.0-linux-x64.bin"),)),
        _release("v1.10.0", (_asset("papaya-1.10.0-linux-x64.bin"),)),
    ]
    content = render_binaries_csv(REPO, releases, [])
    assert content.index("[`1.10.0`") < content.index("[`1.2.0`")


def test_csv_row_cells():
    """Cells link the release, the download, and the VirusTotal analysis."""
    sha = "c" * 64
    releases = [
        _release(
            "v1.0.0",
            (
                _asset("papaya-1.0.0-linux-x64.bin", sha256=sha),
                _asset("papaya-1.0.0-windows-x64.exe"),
            ),
            date="2026-06-30",
        ),
    ]
    records = [_record("v1.0.0", sha, "2026-06-30", flagged=2)]
    content = render_binaries_csv(REPO, releases, records)
    lines = content.splitlines()

    assert lines[0] == ",".join(CSV_HEADERS)
    assert lines[1] == (
        "[`1.0.0` {octicon}`link-external`]"
        f"(https://github.com/{REPO}/releases/tag/v1.0.0),"
        "[{octicon}`download` `linux-x64`]"
        f"(https://github.com/{REPO}/releases/download/v1.0.0/"
        "papaya-1.0.0-linux-x64.bin),"
        "2026-06-30,"
        "[{octicon}`shield;1em;sd-text-danger` 20.0%]"
        f"(https://www.virustotal.com/gui/file/{sha})"
    )
    # A digest-less asset gets an empty VirusTotal cell.
    assert lines[2] == (
        "[`1.0.0` {octicon}`link-external`]"
        f"(https://github.com/{REPO}/releases/tag/v1.0.0),"
        "[{octicon}`download` `windows-x64`]"
        f"(https://github.com/{REPO}/releases/download/v1.0.0/"
        "papaya-1.0.0-windows-x64.exe),"
        "2026-06-30,"
    )


def test_csv_shield_tiers():
    """Clean rows get a green check; low flagged shares a warning tint."""
    clean_sha = "e" * 64
    warn_sha = "f" * 64
    releases = [
        _release(
            "v1.0.0",
            (
                _asset("papaya-1.0.0-linux-arm64.bin", sha256=clean_sha),
                _asset("papaya-1.0.0-linux-x64.bin", sha256=warn_sha),
            ),
        ),
    ]
    records = [
        _record("v1.0.0", clean_sha, "2026-07-01", flagged=0, total=62),
        _record("v1.0.0", warn_sha, "2026-07-01", flagged=1, total=62),
    ]
    content = render_binaries_csv(REPO, releases, records)
    assert (
        "[{octicon}`shield-check;1em;sd-text-success`]"
        f"(https://www.virustotal.com/gui/file/{clean_sha})"
    ) in content
    assert (
        "[{octicon}`shield;1em;sd-text-warning` 1.6%]"
        f"(https://www.virustotal.com/gui/file/{warn_sha})"
    ) in content


def test_csv_no_analysis_link_without_record():
    """A digest with no scan record gets no link: that page does not exist.

    A VirusTotal file URL only resolves for a file that was submitted, so
    deriving one from the GitHub asset digest alone pointed every unscanned
    binary at a blank page.
    """
    sha = "d" * 64
    releases = [
        _release("v1.0.0", (_asset("papaya-1.0.0-macos-arm64.bin", sha256=sha),)),
    ]
    content = render_binaries_csv(REPO, releases, [])
    assert "virustotal.com" not in content
    assert content.endswith("papaya-1.0.0-macos-arm64.bin),2026-07-01,\n")


def test_csv_no_analysis_link_for_empty_record():
    """A record whose verdict counts are all zero is no evidence of a page."""
    sha = "d" * 64
    releases = [
        _release("v1.0.0", (_asset("papaya-1.0.0-macos-arm64.bin", sha256=sha),)),
    ]
    records = [_record("v1.0.0", sha, "2026-07-01", flagged=0, total=0)]
    assert "virustotal.com" not in render_binaries_csv(REPO, releases, records)


def test_csv_platform_fallback_to_filename():
    """A filename without a known target keeps the full name as label."""
    releases = [_release("v1.0.0", (_asset("papaya-portable.exe"),))]
    content = render_binaries_csv(REPO, releases, [])
    assert "`papaya-portable.exe`]" in content


# --- render_chart_section ---


def test_chart_requires_two_releases():
    """A single release with records renders no trend chart."""
    records = [_record("v1.0.0", "a" * 64, "2026-07-01")]
    assert render_chart_section(records) == ""


def test_chart_content():
    """The chart embeds per-release timeline points, oldest first."""
    records = [
        _record("v1.1.0", "b" * 64, "2026-07-06", flagged=1),
        _record("v1.0.0", "a" * 64, "2026-07-01", flagged=0),
    ]
    section = render_chart_section(records)
    assert section.startswith("## VirusTotal detections\n")
    assert '<canvas id="vt-trend">' in section
    assert "cdn.jsdelivr.net/npm/chart.js@" in section
    assert "const VT_DANGER_PCT = 10;" in section
    assert (
        '{"date": "2026-07-01", "flagged": 0, "pct": 0.0, "tag": "v1.0.0", "total": 10}'
    ) in section
    assert (
        '{"date": "2026-07-06", "flagged": 1, "pct": 10.0, '
        '"tag": "v1.1.0", "total": 10}'
    ) in section
    assert section.index('"tag": "v1.0.0"') < section.index('"tag": "v1.1.0"')


def test_chart_uses_earliest_snapshot():
    """Later re-scans of the same file don't alter the at-release trend."""
    records = [
        _record("v1.0.0", "a" * 64, "2026-07-01", flagged=5),
        # Vendor whitelisting processed: a later re-scan shows fewer flags.
        _record("v1.0.0", "a" * 64, "2026-07-20", flagged=0),
        _record("v1.1.0", "b" * 64, "2026-07-06", flagged=1),
    ]
    section = render_chart_section(records)
    assert '"pct": 50.0, "tag": "v1.0.0"' in section
    assert '"date": "2026-07-01"' in section


# --- update_binaries_csv ---


def test_update_binaries_csv_creates_file(tmp_path):
    """The CSV and its parent directories are created on demand."""
    path = tmp_path / "assets" / "binaries.csv"
    assert update_binaries_csv(path, "Version\n1.0.0\n") is True
    assert path.read_text(encoding="UTF-8") == "Version\n1.0.0\n"


def test_update_binaries_csv_idempotent(tmp_path):
    """Rewriting identical content reports no change."""
    path = tmp_path / "binaries.csv"
    update_binaries_csv(path, "Version\n")
    assert update_binaries_csv(path, "Version\n") is False


# --- update_binaries_page ---


def test_update_binaries_page_creates_from_template(tmp_path):
    """A missing page is created with frontmatter, prose, and directives."""
    page = tmp_path / "docs" / "binaries.md"
    assert update_binaries_page(page, "chart content", REPO) is True
    text = page.read_text(encoding="UTF-8")
    assert text.startswith("---\norphan: true\n---\n")
    assert "# Binaries" in text
    assert f"https://github.com/{REPO}/actions/workflows/release.yaml" in text
    assert "binaries.html#minimum-os-requirements" in text
    assert "GitHub account is required" in text
    assert "```{csv-table}\n:file: assets/binaries.csv" in text
    assert ":class: sphinx-datatable" in text
    assert text.index(PAGE_START_MARKER) < text.index("chart content")
    assert text.index("chart content") < text.index(PAGE_END_MARKER)


def test_update_binaries_page_empty_chart(tmp_path):
    """An empty chart leaves a bare marker pair."""
    page = tmp_path / "binaries.md"
    assert update_binaries_page(page, "", REPO) is True
    assert f"{PAGE_START_MARKER}\n\n{PAGE_END_MARKER}" in page.read_text(
        encoding="UTF-8"
    )


def test_update_binaries_page_preserves_prose(tmp_path):
    """Hand-edited prose around the markers survives regeneration."""
    page = tmp_path / "binaries.md"
    page.write_text(
        f"# Custom intro\n\nHand-written context.\n\n{PAGE_START_MARKER}\n\n"
        f"old chart\n\n{PAGE_END_MARKER}\n\nTrailing prose.\n",
        encoding="UTF-8",
    )
    assert update_binaries_page(page, "new chart", REPO) is True
    text = page.read_text(encoding="UTF-8")
    assert "Hand-written context." in text
    assert "Trailing prose." in text
    assert "old chart" not in text
    assert "new chart" in text


@pytest.mark.parametrize(
    ("legacy_open", "close_marker"),
    [
        # Oldest generation: bare open paired with the bare close.
        (LEGACY_PAGE_START_MARKERS[0], LEGACY_PAGE_END_MARKER),
        # Short-lived `-start`/`-end` pair: its close is already the current one.
        (LEGACY_PAGE_START_MARKERS[1], PAGE_END_MARKER),
    ],
)
def test_update_binaries_page_migrates_legacy_markers(
    tmp_path, legacy_open, close_marker
):
    """Pages carrying any superseded open marker are migrated in place."""
    page = tmp_path / "binaries.md"
    page.write_text(
        f"# Intro\n\n{legacy_open}\n\nold chart\n\n{close_marker}\n\nTrailing prose.\n",
        encoding="UTF-8",
    )
    assert update_binaries_page(page, "new chart", REPO) is True
    text = page.read_text(encoding="UTF-8")
    assert legacy_open not in text
    assert PAGE_START_MARKER in text
    assert PAGE_END_MARKER in text
    assert "old chart" not in text
    assert "new chart" in text
    assert "Trailing prose." in text


def test_update_binaries_page_idempotent(tmp_path):
    """Rewriting identical content reports no change."""
    page = tmp_path / "binaries.md"
    update_binaries_page(page, "chart", REPO)
    assert update_binaries_page(page, "chart", REPO) is False


def test_update_binaries_page_refuses_markerless_file(tmp_path):
    """An existing page without markers is never overwritten."""
    page = tmp_path / "binaries.md"
    page.write_text("# Not ours\n", encoding="UTF-8")
    with pytest.raises(ValueError, match="markers"):
        update_binaries_page(page, "chart", REPO)


def test_docs_floor_table_matches_build_targets():
    """The Minimum OS requirements table in docs/binaries.md reflects the
    floors declared in NUITKA_BUILD_TARGETS, which verify-binary enforces.
    """
    text = (REPO_ROOT / "docs" / "binaries.md").read_text(encoding="UTF-8")
    assert "## Minimum OS requirements" in text

    for target, target_data in NUITKA_BUILD_TARGETS.items():
        assert f"`{target}`" in text, f"target {target} missing from the table"
        if target_data["platform_id"] == "linux":
            assert f"glibc `{target_data['glibc_floor']}`" in text
        elif target_data["platform_id"] == "macos":
            assert f"macOS {target_data['min_os'].removesuffix('.0')}" in text
        else:
            assert f"Windows {target_data['min_os']}" in text
