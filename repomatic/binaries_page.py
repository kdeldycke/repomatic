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

"""Generate the binaries catalog: a CSV data file and its `docs/binaries.md` page.

The catalog inventories every compiled binary the repository ever released,
one CSV row per binary: version (linking to the GitHub release), platform
target (linking to the direct download), release date, and the VirusTotal
detection snapshot (linking to the live analysis). It gives alpha and beta
testers a single place to grab binaries from, and the maintainer an overview
of how antivirus engines treat each release.

The data lives in `docs/assets/binaries.csv`, regenerated wholesale on every
release from the GitHub Releases API (the single source of truth for
published assets) and the JSON scan history maintained by `scan-virustotal`.
The Markdown page renders it through a single `csv-table` directive and is
otherwise static: it is created once from {data}`PAGE_TEMPLATE` and only its
marker-delimited region (the detection trend chart) is rewritten afterwards,
so the intro and section prose stay hand-editable per repository.

```{note}
On the documentation site, the table is searchable and sortable client-side
via the `sphinx-datatables` extension, which activates on the
`sphinx-datatable` CSS class. The extension is optional: without it the
`csv-table` directive still renders a plain table, and on GitHub the CSV
file itself gets the built-in searchable grid viewer.
```

```{note}
Development builds are only linked, not cataloged: the rolling dev
pre-release is refreshed on every push to the default branch, so any row
frozen into the CSV would be stale within hours, while the workflow run
artifacts behind the link always are the current builds.
```
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from click_extra.blocks import replace_region

from .binary import BINARY_ASSET_SUFFIXES, NUITKA_BUILD_TARGETS
from .bundle import get_data_content
from .github.releases import parse_release_version
from .tabular import render_csv, write_csv
from .virustotal import VIRUSTOTAL_GUI_URL

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Sequence

    from .github.releases import ReleaseAsset, ReleaseWithAssets
    from .virustotal import ScanRecord

CHART_JS_URL = "https://cdn.jsdelivr.net/npm/chart.js@4.5.0/dist/chart.umd.min.js"
"""Pinned CDN artifact drawing the detections trend chart.

The one external artifact this module publishes into every downstream
repository's docs, so it carries a checksum beside the pin: bump the version
by hand together with {data}`CHART_JS_SRI`.
"""

CHART_JS_SRI = "sha384-XcdcwHqIPULERb2yDEM4R0XaQKU3YnDsrTmjACBZyfdVVqjh6xQ4/DCMd7XLcA6Y"
"""Subresource Integrity digest of {data}`CHART_JS_URL`.

The browser refuses the script if the CDN bytes stop matching. Recompute on
every version bump as the sha384 of the exact artifact, verified against the
same file inside the npm registry tarball before trusting the CDN copy:
`hashlib.sha384(artifact_bytes)` then base64.
"""

CSV_HEADERS = (
    "Version",
    "Platform",
    "Released",
    "VirusTotal",
)
"""Column headers of the binaries CSV.

Deliberately compact: the version cell carries the link to the GitHub
release, the platform cell the direct binary download, and the VirusTotal
cell the analysis link, so no column holds a bare URL, filename, or
64-character checksum.
"""

FLAGGED_DANGER_PCT = 10
"""Flagged-verdict share (percent) at which the catalog shield turns red.

Below it, a flagged binary is the routine Nuitka false-positive tail worth a
warning tint; from one engine in ten upward, the release deserves a
false-positive submission round (see the `/av-false-positive` skill).
"""

LEGACY_PAGE_END_MARKER = "<!-- binaries-end -->"
"""Oldest closing marker, migrated to {data}`PAGE_END_MARKER` on first touch."""

LEGACY_PAGE_START_MARKERS = (
    "<!-- binaries-start -->",
    "<!-- binaries-chart-start -->",
)
"""Superseded opening markers, migrated to {data}`PAGE_START_MARKER` on first touch.

Two generations precede the current bare open: the original
`<!-- binaries-start -->`, then the `<!-- binaries-chart-start -->` of the
short-lived `-start`/`-end` pair. Both collapse to {data}`PAGE_START_MARKER`, so
a page written by any past version refreshes cleanly.
"""

PAGE_REGION = "binaries-chart"
"""Region name spliced by {func}`click_extra.blocks.replace_region`.

The generated chart lives between the `<!-- binaries-chart -->` and
`<!-- binaries-chart-end -->` markers that {data}`PAGE_START_MARKER` and
{data}`PAGE_END_MARKER` spell out, following click-extra's
`<!-- name --> / <!-- name-end -->` marker grammar with `name` = this value.
"""

PAGE_END_MARKER = f"<!-- {PAGE_REGION}-end -->"
"""Closing marker of the generated chart region in the binaries page."""

PAGE_START_MARKER = f"<!-- {PAGE_REGION} -->"
"""Opening marker of the generated chart region in the binaries page."""

PAGE_TEMPLATE = """\
---
orphan: true
---

# Binaries

All standalone executables published by this repository, one row per binary, newest release first. The version links to its GitHub release, the platform to the direct binary download, and the VirusTotal cell to the file's public analysis.

Each target's minimum OS requirement (glibc floor on Linux, deployment target on macOS) and the distributions it opens execution to are documented in the [repomatic binaries page](https://kdeldycke.github.io/repomatic/binaries.html#minimum-os-requirements).

Compiled Python binaries are regularly flagged by heuristic antivirus engines, so every release is submitted to [VirusTotal](https://www.virustotal.com/): this seeds vendor databases with the new signatures and keeps false positives in check. The VirusTotal cell tracks those false positives: a green check marks binaries no engine flags, and flagged binaries show the share of engine verdicts flagging them, snapshotted minutes after publication and before false-positive reports get processed. The live analysis behind the link supersedes it. An empty cell means the binary was never submitted, so VirusTotal holds no analysis to link to: this covers every release predating the scan pipeline.

## Development builds

Fresh binaries are compiled from every push to the default branch by the [release workflow]({repo_url}/actions/workflows/release.yaml). To try the latest development build: open the most recent successful run and download the artifact matching your platform (a GitHub account is required, and the binary comes wrapped in a zip). The same builds are also attached to a rolling dev pre-release, a draft only visible to repository maintainers.

<!-- binaries-chart -->

<!-- binaries-chart-end -->

## Catalog

The table is searchable and sortable on the documentation site; the raw data lives in [`binaries.csv`](assets/binaries.csv).

```{csv-table}
:file: assets/binaries.csv
:header-rows: 1
:class: sphinx-datatable
```
"""
"""Initial page content, used when the page does not exist yet.

The ``{repo_url}`` placeholder is substituted with `str.replace` (not
`str.format`, which would choke on the csv-table directive's braces).
Everything outside the marker pair is written once and never touched again:
repositories can reword the prose without fighting the generator.

:meta hide-value:
"""


def _platform_target(name: str) -> str:
    """Extract the platform-arch target from a binary filename.

    Matches against the known Nuitka build targets (`linux-arm64`, …), the
    same vocabulary the compile matrix uses. Falls back to the full filename
    for foreign naming schemes, so the cell always identifies the file.
    """
    for target in NUITKA_BUILD_TARGETS:
        if target in name:
            return target
    return name


def _binary_assets(release: ReleaseWithAssets) -> list[ReleaseAsset]:
    """Return the release's compiled binaries, sorted by filename.

    Releases also carry versionless alias copies of each binary, backing the
    stable ``releases/latest/download`` URLs (created by
    {func}`~repomatic.binary.pack_binary_assets`, which
    `release-assets` runs in the release engine). An alias shares its digest
    with its versioned sibling: collapse each digest group onto its longest
    name (the versioned one, since {func}`~repomatic.binary.versionless_alias`
    only ever strips the version segment) so the catalog lists each binary
    once. Assets without a recorded digest (uploaded before GitHub started
    recording them, mid-2025) predate the aliases and are kept as-is.
    """
    binaries = [a for a in release.assets if a.name.endswith(BINARY_ASSET_SUFFIXES)]
    digest_groups: dict[str, list[ReleaseAsset]] = {}
    for asset in binaries:
        if asset.sha256:
            digest_groups.setdefault(asset.sha256, []).append(asset)
    aliases: set[str] = set()
    for group in digest_groups.values():
        canonical = max(group, key=lambda asset: (len(asset.name), asset.name))
        aliases.update(asset.name for asset in group if asset.name != canonical.name)
    return sorted(
        (a for a in binaries if a.name not in aliases),
        key=lambda asset: asset.name,
    )


def _at_scan_records(records: Sequence[ScanRecord]) -> dict[str, ScanRecord]:
    """Index records by SHA-256, keeping the earliest snapshot per file.

    The earliest record is the at-release snapshot; later re-scans of the
    same file reflect vendor whitelisting, not the state the binary shipped
    in, so they are excluded from the catalog and chart.
    """
    earliest: dict[str, ScanRecord] = {}
    for record in records:
        current = earliest.get(record.sha256)
        if current is None or record.scanned < current.scanned:
            earliest[record.sha256] = record
    return earliest


def render_chart_section(records: Sequence[ScanRecord]) -> str:
    """Render the detection trend across releases as a Chart.js timeline.

    Plots the share of antivirus engine verdicts flagging each release's
    binaries (all platforms aggregated), using the at-release snapshot of
    every file, on a true time axis: spacing reflects the actual gaps
    between releases. Points reuse the catalog shields' color language,
    read at view time from sphinx-design's CSS variables so they match the
    theme exactly (with hardcoded fallbacks). The data is embedded in the
    page rather than fetched, so the chart also works on `file://`
    previews; only the Chart.js bundle comes from its CDN, mirroring how
    the table's DataTables assets load.

    :return: A `## VirusTotal detections` section with a `raw` HTML fence,
        or an empty string when fewer than two releases have records (a
        one-point trend is not a trend).
    """
    at_scan = _at_scan_records(records).values()
    per_tag: dict[str, tuple[str, int, int]] = {}
    for record in at_scan:
        date, flagged, total = per_tag.get(record.tag, (record.scanned, 0, 0))
        per_tag[record.tag] = (
            min(date, record.scanned),
            flagged + record.stats.flagged,
            total + record.stats.total,
        )

    points = []
    for tag, (date, flagged, total) in per_tag.items():
        version = parse_release_version(tag)
        if version is None:
            continue
        if total:
            points.append((version, tag, date, flagged, total))
    points.sort()
    if len(points) < 2:
        return ""

    payload = json.dumps(
        [
            {
                "date": date,
                "flagged": flagged,
                "pct": round(100 * flagged / total, 1),
                "tag": tag,
                "total": total,
            }
            for _version, tag, date, flagged, total in points
        ],
        sort_keys=True,
    )
    # The chart script ships as a bundled data file so it reads (and lints) as
    # JavaScript instead of sixty quoted Python strings. Placeholder tokens are
    # substituted with str.replace, never str.format: the script is full of
    # literal braces.
    script = (
        get_data_content("vt-trend-chart.js")
        .replace("__VT_TREND_PAYLOAD__", payload)
        .replace("__VT_DANGER_PCT__", str(FLAGGED_DANGER_PCT))
    )
    return (
        "## VirusTotal detections\n\n"
        "Share of antivirus engine verdicts flagging the binaries of each "
        "release, at scan time. Colors follow the catalog shields: green "
        f"for zero detections, amber below {FLAGGED_DANGER_PCT}%, red from "
        "there up.\n\n"
        "```{raw} html\n"
        '<div style="height: 320px;"><canvas id="vt-trend"></canvas></div>\n'
        f'<script src="{CHART_JS_URL}" integrity="{CHART_JS_SRI}" '
        'crossorigin="anonymous"></script>\n'
        f"<script>\n{script}</script>\n"
        "```"
    )


def render_binaries_csv(
    repo_slug: str,
    releases: Sequence[ReleaseWithAssets],
    records: Sequence[ScanRecord],
) -> str:
    """Render the catalog data as CSV, one row per released binary.

    Rows cover every published release carrying compiled binaries, ordered
    by descending version then filename. Cells hold Markdown links (parsed
    by MyST inside the `csv-table` directive): the version to the GitHub
    release, the platform to the binary download, and the VirusTotal cell to
    the file's analysis. The VirusTotal cell renders the at-release snapshot
    as a green check when no engine flags the binary, and as the
    flagged-verdict share (tinted by {data}`FLAGGED_DANGER_PCT`) otherwise.

    ```{caution}
    Only a binary backed by a scan record gets a VirusTotal cell; every
    other row leaves it empty. A file page exists on VirusTotal solely
    because the file was submitted, while the catalog spans every release a
    repository ever published, including those predating `scan-virustotal`
    and those whose upload failed. Deriving the URL from the GitHub asset
    digest alone therefore sent readers to a blank page for each binary
    nobody ever uploaded, which was most of the catalog on older projects.
    ```

    ```{caution}
    The version and platform cells decorate their links with sphinx-design's
    `octicon` role, so the rendering repository needs `sphinx-design` in its
    documentation build (already true across this ecosystem's docs stacks).
    ```

    :param repo_slug: Repository in `owner/repo` form.
    :param releases: Releases from
        {func}`repomatic.github.releases.get_releases_with_assets`.
    :param records: Detection snapshots from the scan history file.
    :return: The full CSV content, header row included.
    """
    versioned = [
        (version, release)
        for release in releases
        if not release.draft
        and (version := parse_release_version(release.tag)) is not None
    ]
    versioned.sort(key=lambda pair: pair[0], reverse=True)

    at_scan = _at_scan_records(records)
    rows: list[tuple[str, ...]] = []
    for version, release in versioned:
        release_url = f"https://github.com/{repo_slug}/releases/tag/{release.tag}"
        for asset in _binary_assets(release):
            record = at_scan.get(asset.sha256) if asset.sha256 else None
            if record and record.stats.total:
                vt_url = VIRUSTOTAL_GUI_URL.format(sha256=record.sha256)
                # The KPI is the distance to the goal (zero false positives),
                # so flagged rows show the failure share and clean rows reduce
                # to a green check: exceptions stand out, the goal state stays
                # calm.
                if record.stats.flagged == 0:
                    vt_cell = (
                        f"[{{octicon}}`shield-check;1em;sd-text-success`]({vt_url})"
                    )
                else:
                    pct = 100 * record.stats.flagged / record.stats.total
                    tint = (
                        "sd-text-danger"
                        if pct >= FLAGGED_DANGER_PCT
                        else "sd-text-warning"
                    )
                    vt_cell = f"[{{octicon}}`shield;1em;{tint}` {pct:.1f}%]({vt_url})"
            else:
                vt_cell = ""
            # The octicon role comes from sphinx-design, same as the icons in
            # the docs page titles; the icons are part of the link text so
            # they stay clickable with their label.
            rows.append((
                f"[`{version}` {{octicon}}`link-external`]({release_url})",
                (
                    f"[{{octicon}}`download` `{_platform_target(asset.name)}`]"
                    f"({asset.download_url})"
                ),
                release.date,
                vt_cell,
            ))
    return render_csv(CSV_HEADERS, rows)


def update_binaries_csv(csv_path: Path, content: str) -> bool:
    """Write the catalog CSV, creating parent directories as needed.

    :param csv_path: Path to the CSV file.
    :param content: Rendered CSV from {func}`render_binaries_csv`.
    :return: `True` when the file was created or its content changed.
    """
    if not write_csv(csv_path, content):
        logging.info(f"Binaries CSV {csv_path} already up to date.")
        return False
    logging.info(f"Wrote binaries CSV {csv_path}.")
    return True


def update_binaries_page(page_path: Path, chart_section: str, repo_slug: str) -> bool:
    """Create the binaries page if missing and refresh its chart region.

    A missing page is created (with parent directories) from
    {data}`PAGE_TEMPLATE`. On an existing page only the region between
    {data}`PAGE_START_MARKER` and {data}`PAGE_END_MARKER` is replaced by
    {func}`click_extra.blocks.replace_region`, leaving all surrounding prose
    untouched. Pages carrying any {data}`LEGACY_PAGE_START_MARKERS` open or the
    {data}`LEGACY_PAGE_END_MARKER` close are migrated to the current markers in
    the same pass.

    :param page_path: Path to the Markdown page.
    :param chart_section: Rendered chart from {func}`render_chart_section`,
        or an empty string to leave the region empty.
    :param repo_slug: Repository in `owner/repo` form, interpolated into the
        template on first creation.
    :return: `True` when the file was created or its content changed.
    :raises ValueError: When the page exists but lacks the markers. Loud on
        purpose: a page not written by this generator must never be
        overwritten.
    """
    original = None
    if page_path.exists():
        original = page_path.read_text(encoding="UTF-8")
        # Migrate pages written before the marker grammar aligned on
        # click-extra's bare `<!-- name -->` open.
        text = original
        for legacy_open in LEGACY_PAGE_START_MARKERS:
            text = text.replace(legacy_open, PAGE_START_MARKER)
        text = text.replace(LEGACY_PAGE_END_MARKER, PAGE_END_MARKER)
        if PAGE_START_MARKER not in text or PAGE_END_MARKER not in text:
            raise ValueError(
                f"{page_path} lacks the {PAGE_START_MARKER} / {PAGE_END_MARKER} "
                "markers, refusing to overwrite it."
            )
    else:
        text = PAGE_TEMPLATE.replace("{repo_url}", f"https://github.com/{repo_slug}")

    new_text = replace_region(text, PAGE_REGION, chart_section.strip())

    if new_text == original:
        logging.info(f"Binaries page {page_path} already up to date.")
        return False
    page_path.parent.mkdir(parents=True, exist_ok=True)
    page_path.write_text(new_text, encoding="UTF-8")
    logging.info(f"Wrote binaries page {page_path}.")
    return True
