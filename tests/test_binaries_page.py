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

"""Tests for the binaries catalog page generator."""

from __future__ import annotations

import pytest

from repomatic.binaries_page import (
    PAGE_END_MARKER,
    PAGE_START_MARKER,
    _format_gfm_table,
    _format_size,
    render_binaries_section,
    update_binaries_page,
)
from repomatic.github.releases import ReleaseAsset, ReleaseWithAssets
from repomatic.virustotal import DetectionStats, ScanRecord

REPO = "owner/papaya"


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


def _record(tag: str, sha256: str, scanned: str, flagged: int = 0) -> ScanRecord:
    return ScanRecord(
        tag=tag,
        filename="papaya.bin",
        sha256=sha256,
        scanned=scanned,
        stats=DetectionStats(
            malicious=flagged, suspicious=0, undetected=10 - flagged, harmless=0
        ),
    )


# --- _format_size ---


@pytest.mark.parametrize(
    ("size", "expected"),
    [
        (1048576, "1.0 MiB"),
        (24956928, "23.8 MiB"),
        (0, "0.0 MiB"),
    ],
)
def test_format_size(size, expected):
    assert _format_size(size) == expected


# --- _format_gfm_table ---


def test_format_gfm_table_matches_mdformat_layout():
    """Cells pad to the widest column entry, delimiters at least 3 dashes.

    The exact layout mdformat's tables plugin produces, so generated pages
    are a fixed point under the `format-markdown` autofix job.
    """
    table = _format_gfm_table(
        ("Binary", "Size"),
        [("papaya-linux-arm64.bin", "1.0 MiB"), ("x", "")],
    )
    assert table == (
        "| Binary                 | Size    |\n"
        "| ---------------------- | ------- |\n"
        "| papaya-linux-arm64.bin | 1.0 MiB |\n"
        "| x                      |         |"
    )


def test_format_gfm_table_minimum_width():
    """A column narrower than 3 characters still gets a 3-dash delimiter."""
    table = _format_gfm_table(("A", "B"), [("x", "y")])
    assert table == ("| A   | B   |\n| --- | --- |\n| x   | y   |")


# --- render_binaries_section ---


def test_render_dev_builds_pointer():
    """The dev builds section always opens the generated region."""
    section = render_binaries_section(REPO, [], [])
    assert section.startswith("## Development builds\n")
    assert f"https://github.com/{REPO}/actions/workflows/release.yaml" in section
    assert "GitHub account is required" in section
    assert "draft" in section


def test_render_skips_drafts_and_foreign_tags():
    """Draft releases and non-version tags stay out of the catalog."""
    releases = [
        _release("v1.0.0.dev0", (_asset("papaya-dev.bin"),), draft=True),
        _release("weekly-snapshot", (_asset("papaya-weekly.bin"),)),
        _release("v1.0.0", (_asset("papaya-1.0.0-linux-x64.bin"),)),
    ]
    section = render_binaries_section(REPO, releases, [])
    assert "papaya-1.0.0-linux-x64.bin" in section
    assert "papaya-dev.bin" not in section
    assert "papaya-weekly.bin" not in section


def test_render_skips_binaryless_releases():
    """Releases without .bin or .exe assets get no section."""
    releases = [
        _release("v1.1.0", (_asset("papaya-1.1.0-linux-x64.bin"),)),
        _release("v1.0.0", (_asset("papaya-1.0.0.tar.gz"), _asset("notes.md"))),
    ]
    section = render_binaries_section(REPO, releases, [])
    assert "## [`1.1.0`" in section
    assert "## [`1.0.0`" not in section


def test_render_sections_newest_first():
    """Release sections are ordered by descending version, not API order."""
    releases = [
        _release("v1.2.0", (_asset("papaya-1.2.0-linux-x64.bin"),)),
        _release("v1.10.0", (_asset("papaya-1.10.0-linux-x64.bin"),)),
    ]
    section = render_binaries_section(REPO, releases, [])
    assert section.index("## [`1.10.0`") < section.index("## [`1.2.0`")


def test_render_release_table_cells():
    """Table rows carry download link, size, VirusTotal link, and snapshot."""
    sha = "c" * 64
    releases = [
        _release(
            "v1.0.0",
            (
                _asset("papaya-1.0.0-linux-x64.bin", size=24956928, sha256=sha),
                _asset("papaya-1.0.0-windows-x64.exe"),
            ),
            date="2026-06-30",
        ),
    ]
    records = [_record("v1.0.0", sha, "2026-06-30", flagged=2)]
    section = render_binaries_section(REPO, releases, records)

    assert (
        f"## [`1.0.0` (2026-06-30)](https://github.com/{REPO}/releases/tag/v1.0.0)"
        in section
    )
    assert "| Binary" in section
    assert (
        f"[`papaya-1.0.0-linux-x64.bin`]"
        f"(https://github.com/{REPO}/releases/download/v1.0.0/"
        f"papaya-1.0.0-linux-x64.bin)"
    ) in section
    assert "23.8 MiB" in section
    assert f"[`{sha}`](https://www.virustotal.com/gui/file/{sha})" in section
    assert "2 / 10" in section
    # The digest-less asset renders empty SHA-256 and Detections cells.
    exe_row = next(
        line for line in section.splitlines() if "windows-x64.exe" in line
    )
    assert exe_row.rstrip("| ").endswith("1.0 MiB")


def test_chart_requires_two_releases():
    """A single release with records renders no trend chart."""
    releases = [_release("v1.0.0", (_asset("papaya.bin", sha256="a" * 64),))]
    records = [_record("v1.0.0", "a" * 64, "2026-07-01")]
    section = render_binaries_section(REPO, releases, records)
    assert "xychart-beta" not in section


def test_chart_content():
    """The chart plots flagged verdict share per release, oldest first."""
    records = [
        _record("v1.1.0", "b" * 64, "2026-07-06", flagged=1),
        _record("v1.0.0", "a" * 64, "2026-07-01", flagged=0),
    ]
    section = render_binaries_section(REPO, [], records)
    assert "## VirusTotal detections" in section
    assert "```mermaid\nxychart-beta" in section
    assert 'x-axis ["v1.0.0", "v1.1.0"]' in section
    assert "line [0.0, 10.0]" in section
    assert 'y-axis "Flagged verdicts (%)" 0 --> 10' in section


def test_chart_uses_earliest_snapshot():
    """Later re-scans of the same file don't alter the at-release trend."""
    records = [
        _record("v1.0.0", "a" * 64, "2026-07-01", flagged=5),
        # Vendor whitelisting processed: a later re-scan shows fewer flags.
        _record("v1.0.0", "a" * 64, "2026-07-20", flagged=0),
        _record("v1.1.0", "b" * 64, "2026-07-06", flagged=1),
    ]
    section = render_binaries_section(REPO, [], records)
    assert "line [50.0, 10.0]" in section


# --- update_binaries_page ---


def test_update_binaries_page_creates_from_template(tmp_path):
    """A missing page is created with frontmatter, intro, and markers."""
    page = tmp_path / "docs" / "binaries.md"
    assert update_binaries_page(page, "## Development builds\n\ncontent") is True
    text = page.read_text(encoding="utf-8")
    assert text.startswith("---\norphan: true\n---\n")
    assert "# Binaries" in text
    assert PAGE_START_MARKER in text
    assert PAGE_END_MARKER in text
    assert text.index(PAGE_START_MARKER) < text.index("content")
    assert text.index("content") < text.index(PAGE_END_MARKER)


def test_update_binaries_page_preserves_intro(tmp_path):
    """A hand-edited intro above the markers survives regeneration."""
    page = tmp_path / "binaries.md"
    page.write_text(
        f"# Custom intro\n\nHand-written context.\n\n{PAGE_START_MARKER}\n\n"
        f"old content\n\n{PAGE_END_MARKER}\n",
        encoding="utf-8",
    )
    assert update_binaries_page(page, "new content") is True
    text = page.read_text(encoding="utf-8")
    assert "Hand-written context." in text
    assert "old content" not in text
    assert "new content" in text


def test_update_binaries_page_idempotent(tmp_path):
    """Rewriting identical content reports no change."""
    page = tmp_path / "binaries.md"
    update_binaries_page(page, "content")
    assert update_binaries_page(page, "content") is False


def test_update_binaries_page_refuses_markerless_file(tmp_path):
    """An existing page without markers is never overwritten."""
    page = tmp_path / "binaries.md"
    page.write_text("# Not ours\n", encoding="utf-8")
    with pytest.raises(ValueError, match="markers"):
        update_binaries_page(page, "content")
