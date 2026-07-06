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

"""Generate the binaries catalog page (`docs/binaries.md`).

The page inventories every compiled binary the repository ever released: one
table per version with download links, sizes, SHA-256 checksums (linking to
VirusTotal analyses), and detection snapshots, plus a chart of the detection
trend across releases. It gives alpha and beta testers a single place to grab
binaries from, and the maintainer an overview of how antivirus engines treat
each release.

The section between the page markers is regenerated wholesale on every
release: names, sizes, digests, and download URLs all come from the GitHub
Releases API (the single source of truth for published assets), and detection
snapshots come from the JSON history file maintained by `scan-virustotal`.
Only the intro above the markers is hand-editable per repository.

```{note}
Development builds are only linked, not tabulated: the rolling dev
pre-release is refreshed on every push to the default branch, so any row
frozen into this page would be stale within hours, while the release page
behind the link always shows the current assets.
```
"""

from __future__ import annotations

import logging
import math
from pathlib import Path

from packaging.version import InvalidVersion, Version

from .virustotal import VIRUSTOTAL_GUI_URL

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Sequence

    from .github.releases import ReleaseAsset, ReleaseWithAssets
    from .virustotal import ScanRecord

BINARY_ASSET_SUFFIXES = (".bin", ".exe")
"""File extensions identifying compiled binaries among release assets.

Same set the `scan-virustotal` command uploads and the release workflow
downloads (`--pattern` flags in `_release-engine.yaml`).
"""

GRAPH_MAX_RELEASES = 20
"""Number of most recent releases plotted in the detection trend chart.

Mermaid renders every x-axis label; past this point they overlap into an
unreadable smear, and the trend of interest is the recent one anyway.
"""

PAGE_END_MARKER = "<!-- binaries-end -->"
"""Closing marker of the generated region in the binaries page."""

PAGE_START_MARKER = "<!-- binaries-start -->"
"""Opening marker of the generated region in the binaries page."""

PAGE_TEMPLATE = f"""\
---
orphan: true
---

# Binaries

All standalone executables published by this repository, one section per release, freshest first. Sizes and SHA-256 checksums come from the GitHub release assets, and each checksum links to the binary's [VirusTotal](https://www.virustotal.com/) analysis.

Compiled Python binaries are regularly flagged by heuristic antivirus engines, so every release is submitted to VirusTotal: this seeds vendor databases with the new signatures and keeps false positives in check. The **Detections** column counts the `flagged / total` engine verdicts minutes after publication, before false-positive reports get processed: the live analysis behind each checksum link supersedes it.

{PAGE_START_MARKER}

{PAGE_END_MARKER}
"""
"""Initial page content, used when the page does not exist yet.

Everything above the start marker is a hand-editable intro: repositories can
reword it or add context without fighting the generator.

:meta hide-value:
"""


def _format_size(size: int) -> str:
    """Format a byte count as mebibytes with one decimal."""
    return f"{size / 1024 / 1024:.1f} MiB"


def _format_gfm_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    """Render a GFM table in mdformat's canonical layout.

    Cells are padded to the widest cell of their column (minimum 3, the GFM
    delimiter floor) and the delimiter row uses the same width, matching what
    `mdformat` with its tables plugin produces. Emitting the canonical layout
    up front keeps the page a fixed point under the `format-markdown` autofix
    job: see `claude.md` § "Generator/formatter ping-pong is recurrent".

    Cell content is never escaped: filenames, sizes, digests, and GitHub
    URLs cannot contain pipe characters.
    """
    widths = [
        max(3, len(header), *(len(row[i]) for row in rows))
        for i, header in enumerate(headers)
    ]

    def render_row(cells: Sequence[str]) -> str:
        padded = (cell.ljust(width) for cell, width in zip(cells, widths))
        return "| " + " | ".join(padded) + " |"

    lines = [
        render_row(headers),
        render_row(["-" * width for width in widths]),
        *(render_row(row) for row in rows),
    ]
    return "\n".join(lines)


def _release_version(release: ReleaseWithAssets) -> Version | None:
    """Parse the release tag as a version, or `None` for foreign tag schemes."""
    try:
        return Version(release.tag.removeprefix("v"))
    except InvalidVersion:
        return None


def _binary_assets(release: ReleaseWithAssets) -> list[ReleaseAsset]:
    """Return the release's compiled binaries, sorted by filename."""
    return sorted(
        (a for a in release.assets if a.name.endswith(BINARY_ASSET_SUFFIXES)),
        key=lambda asset: asset.name,
    )


def _at_scan_records(records: Sequence[ScanRecord]) -> dict[str, ScanRecord]:
    """Index records by SHA-256, keeping the earliest snapshot per file.

    The earliest record is the at-release snapshot; later re-scans of the
    same file reflect vendor whitelisting, not the state the binary shipped
    in, so they are excluded from the per-release tables and chart.
    """
    earliest: dict[str, ScanRecord] = {}
    for record in records:
        current = earliest.get(record.sha256)
        if current is None or record.scanned < current.scanned:
            earliest[record.sha256] = record
    return earliest


def _dev_builds_section(repo_url: str) -> str:
    """Render the pointer to the latest development builds.

    Points testers at the release workflow's run artifacts: any signed-in
    GitHub user can download them, unlike the rolling dev pre-release, which
    is a draft only maintainers can see (kept as such so its assets stay
    mutable under GitHub's immutable-releases setting). Anonymous artifact
    downloads are a known GitHub limitation
    ([actions/upload-artifact#51](https://github.com/actions/upload-artifact/issues/51)).
    """
    return (
        "## Development builds\n\n"
        "Fresh binaries are compiled from every push to the default branch "
        f"by the [release workflow]({repo_url}/actions/workflows/"
        "release.yaml). To try the latest development build: open the most "
        "recent successful run and download the artifact matching your "
        "platform (a GitHub account is required, and the binary comes "
        "wrapped in a zip). The same builds are also attached to a rolling "
        "dev pre-release, a draft only visible to repository maintainers."
    )


def _detections_chart(records: Sequence[ScanRecord]) -> str:
    """Render the detection trend across releases as a Mermaid line chart.

    Plots the share of antivirus engine verdicts flagging the release's
    binaries (all platforms aggregated), using the at-release snapshot of
    each file. Clean verdicts are the complement, so a single line carries
    the whole story.

    :return: A `## VirusTotal detections` section with an `xychart-beta`
        fence, or an empty string when fewer than two releases have records
        (a one-point trend is not a trend).
    """
    at_scan = _at_scan_records(records).values()
    totals: dict[str, tuple[int, int]] = {}
    for record in at_scan:
        flagged, total = totals.get(record.tag, (0, 0))
        totals[record.tag] = (
            flagged + record.stats.flagged,
            total + record.stats.total,
        )

    points = []
    for tag, (flagged, total) in totals.items():
        try:
            version = Version(tag.removeprefix("v"))
        except InvalidVersion:
            continue
        if total:
            points.append((version, tag, 100 * flagged / total))
    points.sort()
    points = points[-GRAPH_MAX_RELEASES:]
    if len(points) < 2:
        return ""

    labels = ", ".join(f'"{tag}"' for _version, tag, _pct in points)
    values = ", ".join(f"{pct:.1f}" for _version, _tag, pct in points)
    y_max = max(1, math.ceil(max(pct for _version, _tag, pct in points)))
    return (
        "## VirusTotal detections\n\n"
        "Share of antivirus engine verdicts flagging the binaries of each "
        "release, at scan time:\n\n"
        "```mermaid\n"
        "xychart-beta\n"
        '    title "Antivirus verdicts flagging the binaries (%)"\n'
        f"    x-axis [{labels}]\n"
        f'    y-axis "Flagged verdicts (%)" 0 --> {y_max}\n'
        f"    line [{values}]\n"
        "```"
    )


def _release_section(
    repo_url: str,
    release: ReleaseWithAssets,
    version: Version,
    at_scan: dict[str, ScanRecord],
) -> str:
    """Render one release's heading and binaries table.

    :return: The section, or an empty string when the release carries no
        compiled binaries (pre-Nuitka releases, pure-Python releases).
    """
    assets = _binary_assets(release)
    if not assets:
        return ""

    rows = []
    for asset in assets:
        if asset.sha256:
            sha_cell = (
                f"[`{asset.sha256}`]"
                f"({VIRUSTOTAL_GUI_URL.format(sha256=asset.sha256)})"
            )
        else:
            sha_cell = ""
        record = at_scan.get(asset.sha256) if asset.sha256 else None
        rows.append((
            f"[`{asset.name}`]({asset.download_url})",
            _format_size(asset.size),
            sha_cell,
            str(record.stats) if record else "",
        ))

    heading = (
        f"## [`{version}` ({release.date})]({repo_url}/releases/tag/{release.tag})"
    )
    table = _format_gfm_table(("Binary", "Size", "SHA-256", "Detections"), rows)
    return f"{heading}\n\n{table}"


def render_binaries_section(
    repo_slug: str,
    releases: Sequence[ReleaseWithAssets],
    records: Sequence[ScanRecord],
) -> str:
    """Render the full generated region of the binaries page.

    :param repo_slug: Repository in `owner/repo` form.
    :param releases: Releases from
        {func}`repomatic.github.releases.get_releases_with_assets`.
    :param records: Detection snapshots from the scan history file.
    :return: Markdown for the region between the page markers: the dev builds
        pointer, the detection trend chart, then one section per published
        release carrying binaries, freshest first.
    """
    repo_url = f"https://github.com/{repo_slug}"
    parts = [_dev_builds_section(repo_url)]

    chart = _detections_chart(records)
    if chart:
        parts.append(chart)

    versioned = [
        (version, release)
        for release in releases
        if not release.draft and (version := _release_version(release)) is not None
    ]
    versioned.sort(key=lambda pair: pair[0], reverse=True)

    at_scan = _at_scan_records(records)
    for version, release in versioned:
        section = _release_section(repo_url, release, version, at_scan)
        if section:
            parts.append(section)

    return "\n\n".join(parts)


def update_binaries_page(page_path: Path, section: str) -> bool:
    """Create or refresh the binaries catalog page.

    Replaces the region between {data}`PAGE_START_MARKER` and
    {data}`PAGE_END_MARKER`, leaving the intro above it untouched. A missing
    page is created (with parent directories) from {data}`PAGE_TEMPLATE`.

    :param page_path: Path to the Markdown page.
    :param section: Rendered region content, from
        {func}`render_binaries_section`.
    :return: `True` when the file was created or its content changed.
    :raises ValueError: When the page exists but lacks the markers. Loud on
        purpose: a page not written by this generator must never be
        overwritten.
    """
    original = None
    if page_path.exists():
        original = page_path.read_text(encoding="UTF-8")
        if PAGE_START_MARKER not in original or PAGE_END_MARKER not in original:
            raise ValueError(
                f"{page_path} lacks the {PAGE_START_MARKER} / {PAGE_END_MARKER} "
                "markers, refusing to overwrite it."
            )
        text = original
    else:
        text = PAGE_TEMPLATE

    before, rest = text.split(PAGE_START_MARKER, 1)
    _, after = rest.split(PAGE_END_MARKER, 1)
    new_text = (
        before
        + PAGE_START_MARKER
        + "\n\n"
        + section.strip()
        + "\n\n"
        + PAGE_END_MARKER
        + after
    )

    if new_text == original:
        logging.info(f"Binaries page {page_path} already up to date.")
        return False
    page_path.parent.mkdir(parents=True, exist_ok=True)
    page_path.write_text(new_text, encoding="UTF-8")
    logging.info(f"Wrote binaries page {page_path}.")
    return True
