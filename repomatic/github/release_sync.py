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

"""Sync GitHub release notes from ``changelog.md``.

Compares each GitHub release body against the corresponding
``changelog.md`` section and updates any that have drifted.
``changelog.md`` is the single source of truth.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from .gh import run_gh_command
from .pr_body import render_template
from .releases import get_github_releases

TYPE_CHECKING = False
if TYPE_CHECKING:
    from ..changelog import Changelog


class SyncAction(Enum):
    """Action taken (or to be taken) on a release body."""

    DRY_RUN = "dry_run"
    FAILED = "failed"
    SKIPPED = "skipped"
    UPDATED = "updated"


@dataclass(frozen=True)
class SyncRow:
    """Per-release detail for the markdown report table."""

    action: SyncAction
    version: str
    release_url: str


@dataclass
class SyncResult:
    """Accumulated results from a release-notes sync run."""

    dry_run: bool = True
    rows: list[SyncRow] = field(default_factory=list)
    total: int = 0
    in_sync: int = 0
    drifted: int = 0
    updated: int = 0
    failed: int = 0
    missing_changelog: int = 0


def build_expected_body(
    changelog: Changelog,
    version: str,
    *,
    admonition_override: str | None = None,
) -> str:
    """Build the expected release body from the changelog.

    Decomposes the changelog section into discrete elements and renders
    them through the ``github-releases`` template. This allows the
    GitHub release body to include a different subset of elements than
    the ``release-notes`` template used for ``changelog.md`` entries.

    :param changelog: Parsed changelog instance.
    :param version: Version string (e.g. ``1.2.3``).
    :param admonition_override: If provided, replaces the
        ``availability_admonition`` from the changelog. Used by
        ``release_notes_with_admonition`` to inject a pre-computed
        admonition at release time.
    :return: The rendered release body, or empty string if the
        version has no changelog section.
    """
    from dataclasses import asdict

    elements = changelog.decompose_version(version)
    if not elements.changes and not elements.availability_admonition:
        return ""

    if admonition_override is not None:
        elements.availability_admonition = admonition_override
    # Extract tag range from compare URL (e.g. "v1.1.0...v2.0.0").
    tag_range = (
        elements.compare_url.rsplit("/compare/", 1)[-1] if elements.compare_url else ""
    )
    return render_template(
        "github-releases",
        **asdict(elements),
        tag_range=tag_range,
    )


def _normalize_body(text: str) -> str:
    """Normalize a release body for comparison.

    Strips trailing whitespace from each line and trailing newlines
    from the whole text so insignificant formatting differences don't
    cause false positives.

    :param text: Raw release body text.
    :return: Normalized text.
    """
    lines = [line.rstrip() for line in text.splitlines()]
    return "\n".join(lines).strip()


def sync_github_releases(
    repo_url: str,
    changelog_path: Path,
    dry_run: bool = True,
) -> SyncResult:
    """Sync GitHub release bodies from ``changelog.md``.

    For each released version in the changelog, compares the expected
    body (from ``changelog.md``) with the actual GitHub release body.
    In live mode, updates drifted releases via ``gh release edit``.

    :param repo_url: Repository URL (e.g.
        ``https://github.com/user/repo``).
    :param changelog_path: Path to ``changelog.md``.
    :param dry_run: If ``True``, report without making changes.
    :return: Structured sync results.
    """
    from ..changelog import Changelog

    result = SyncResult(dry_run=dry_run)

    content = changelog_path.read_text(encoding="UTF-8")
    changelog = Changelog(content)

    releases = get_github_releases(repo_url)
    if not releases:
        logging.warning("No GitHub releases found.")
        return result

    # Parse owner/repo for gh CLI.
    parts = repo_url.rstrip("/").split("/")
    nwo = f"{parts[-2]}/{parts[-1]}" if len(parts) >= 2 else ""

    # Iterate over released versions in the changelog.
    for version, _date in changelog.extract_all_releases():
        result.total += 1

        if version not in releases:
            logging.debug(f"No GitHub release for version {version}.")
            continue

        release_url = f"{repo_url}/releases/tag/v{version}"
        expected = build_expected_body(changelog, version)

        if not expected:
            result.missing_changelog += 1
            logging.debug(f"Changelog section for {version} is empty.")
            continue

        actual = releases[version].body

        if _normalize_body(expected) == _normalize_body(actual):
            result.in_sync += 1
            result.rows.append(
                SyncRow(
                    action=SyncAction.SKIPPED,
                    version=version,
                    release_url=release_url,
                )
            )
            continue

        result.drifted += 1

        if dry_run:
            logging.info(f"[dry-run] Would update release notes for v{version}.")
            result.rows.append(
                SyncRow(
                    action=SyncAction.DRY_RUN,
                    version=version,
                    release_url=release_url,
                )
            )
            continue

        # Live mode: update the release body.
        try:
            run_gh_command([
                "release",
                "edit",
                f"v{version}",
                "--repo",
                nwo,
                "--notes",
                expected,
            ])
            result.updated += 1
            result.rows.append(
                SyncRow(
                    action=SyncAction.UPDATED,
                    version=version,
                    release_url=release_url,
                )
            )
            logging.info(f"Updated release notes for v{version}.")
        except RuntimeError:
            result.failed += 1
            result.rows.append(
                SyncRow(
                    action=SyncAction.FAILED,
                    version=version,
                    release_url=release_url,
                )
            )
            logging.warning(f"Failed to update release notes for v{version}.")

    return result


def _action_emoji(action: SyncAction) -> str:
    """Map a sync action to its emoji + label for the report table."""
    return {
        SyncAction.DRY_RUN: "\U0001f441\ufe0f Dry-run",
        SyncAction.FAILED: "\u26a0\ufe0f Failed",
        SyncAction.SKIPPED: "\u2705 In sync",
        SyncAction.UPDATED: "\U0001f504 Updated",
    }[action]


def render_sync_report(result: SyncResult) -> str:
    """Render a markdown report from sync results.

    :param result: Structured results from the sync run.
    :return: Markdown report string.
    """
    mode = "dry-run" if result.dry_run else "live"

    # Summary table rows.
    summary_lines = [
        f"| \U0001f4e6 Total releases | {result.total} |",
        f"| \u2705 In sync | {result.in_sync} |",
        f"| \U0001f504 Drifted | {result.drifted} |",
    ]
    if not result.dry_run:
        summary_lines.append(f"| \u2705 Updated | {result.updated} |")
        summary_lines.append(f"| \u26a0\ufe0f Failed | {result.failed} |")
    if result.missing_changelog:
        summary_lines.append(
            f"| \u2753 Missing changelog | {result.missing_changelog} |"
        )

    # Per-release details.
    drifted_rows = [row for row in result.rows if row.action != SyncAction.SKIPPED]
    details_section = ""
    if drifted_rows:
        detail_lines = [
            "### \U0001f4dd Details",
            "",
            "| Version | Release | Action |",
            "| --- | --- | --- |",
        ]
        for row in drifted_rows:
            detail_lines.append(
                f"| `{row.version}`"
                f" | [`v{row.version}`]({row.release_url})"
                f" | {_action_emoji(row.action)} |"
            )
        details_section = "\n".join(detail_lines)

    return render_template(
        "release-sync-report",
        mode=mode,
        summary_rows="\n".join(summary_lines),
        details_section=details_section,
    )
