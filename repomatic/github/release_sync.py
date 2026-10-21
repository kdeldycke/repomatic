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

"""Sync GitHub release notes from `changelog.md`.

Compares each GitHub release body against the corresponding
`changelog.md` section and updates any that have drifted.
`changelog.md` is the single source of truth.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field

from ..changelog import GITHUB_RELEASE_URL, Changelog, availability_admonition
from ..pypi import PYPI_PROJECT_URL
from ..tabular import ReportAction, render_markdown_table
from ..templating import render_template
from .gh import GhCommandError
from .releases import (
    GitHubReleasesUnavailable,
    edit_release_notes,
    get_github_releases,
)
from .urls import parse_github_repo

TYPE_CHECKING = False
if TYPE_CHECKING:
    from pathlib import Path


def build_expected_body(
    changelog: Changelog,
    version: str,
    *,
    admonition_override: str | None = None,
) -> str:
    """Build the expected release body from the changelog.

    Decomposes the changelog section into discrete elements and renders
    them through the `github-releases` template. This allows the
    GitHub release body to include a different subset of elements than
    the `release-notes` template used for `changelog.md` entries.

    Lives here, with the release publishers, rather than in
    {mod}`repomatic.changelog`: every caller is a GitHub-release writer (this
    sync, the dev pre-release, the release-notes metadata keys), and the
    changelog module renders changelog entries, not release bodies. The
    release draft is one more: see {func}`build_draft_body`.

    :param changelog: Parsed changelog instance.
    :param version: Version string (e.g. `1.2.3`).
    :param admonition_override: If provided, replaces the
        `availability_admonition` from the changelog. Used by
        {func}`build_pypi_body` to inject a pre-computed admonition at
        release time.
    :return: The rendered release body, or empty string if the
        version has no changelog section.
    """
    elements = changelog.decompose_version(version)
    if not elements.has_body:
        return ""

    if admonition_override is not None:
        elements.availability_admonition = admonition_override
    return render_template(
        "github-releases",
        **asdict(elements),
        tag_range=elements.tag_range,
    )


def build_pypi_body(changelog: Changelog, version: str, package_name: str) -> str:
    """Build the release body of a version published on PyPI.

    The body of {func}`build_expected_body`, with the availability admonition
    linking to PyPI and GitHub injected before `fix-changelog` writes it into
    `changelog.md`. The admonition comes from
    {func}`~repomatic.changelog.availability_admonition`, the renderer
    `fix-changelog` writes the section with, so both word it the same way.

    The draft cannot ask the platforms which release came first: the PyPI
    upload runs beside it, and a failed lookup reads like an empty one. The
    changelog answers instead, as `lint-changelog` will once both platforms
    carry the release.

    :param changelog: Parsed changelog instance.
    :param version: Version string (e.g. `1.2.3`).
    :param package_name: The project name on PyPI.
    :return: The rendered body, or an empty string when the changelog names no
        repository or holds no section for *version*.
    """
    repo_url = changelog.extract_repo_url()
    if not repo_url:
        return ""
    first = changelog.is_first_release(version)
    admonition = availability_admonition(
        version,
        pypi_url=PYPI_PROJECT_URL.format(package=package_name, version=version),
        github_url=GITHUB_RELEASE_URL.format(repo_url=repo_url, version=version),
        first_on_pypi=first,
        first_on_github=first,
    )
    return build_expected_body(changelog, version, admonition_override=admonition)


def build_draft_body(
    changelog: Changelog, version: str, package_name: str | None
) -> str:
    """Build the body a release draft is created with.

    The body carries the PyPI availability admonition from the start: baking it
    into the draft, rather than editing the release from the caller's fast
    `publish-pypi` lane, avoids a race between the lanes. That edit can run
    before the draft exists. The bake is optimistic: it assumes the parallel
    PyPI upload succeeds, which it does on the normal path, and a failed upload
    surfaces as a red `publish-pypi` job.

    :param changelog: Parsed changelog instance.
    :param version: Version string (e.g. `1.2.3`).
    :param package_name: The project name on PyPI, or `None` for a project
        that does not publish there.
    :return: {func}`build_pypi_body` for a PyPI project, else
        {func}`build_expected_body`. Empty when the changelog holds no section
        for *version*.
    """
    if package_name:
        body = build_pypi_body(changelog, version, package_name)
        if body:
            return body
    return build_expected_body(changelog, version)


@dataclass(frozen=True)
class SyncRow:
    """Per-release detail for the markdown report table."""

    action: ReportAction
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
    """Sync GitHub release bodies from `changelog.md`.

    For each released version in the changelog, compares the expected
    body (from `changelog.md`) with the actual GitHub release body.
    In live mode, updates drifted releases via `gh release edit`.

    Live mode reads the release bodies fresh rather than from the day-long
    cache: an edit decided against a stale body would rewrite a release that
    already matches, or miss one that drifted since.

    :param repo_url: Repository URL (like `https://github.com/user/repo`).
    :param changelog_path: Path to `changelog.md`.
    :param dry_run: If `True`, report without making changes.
    :return: Structured sync results.
    """
    result = SyncResult(dry_run=dry_run)

    parsed = parse_github_repo(repo_url)
    if parsed is None:
        logging.warning(f"{repo_url} names no GitHub repository.")
        return result
    repository = "/".join(parsed)

    content = changelog_path.read_text(encoding="UTF-8")
    changelog = Changelog(content)

    try:
        releases = get_github_releases(repo_url, force_refresh=not dry_run)
    except GitHubReleasesUnavailable as exc:
        # Sync is read-only relative to `changelog.md`, so skipping a run
        # when the API is unhealthy just defers the work to the next run.
        logging.warning(f"Skipping release-notes sync: {exc}")
        return result
    if not releases:
        logging.warning("No GitHub releases found.")
        return result

    # Iterate over released versions in the changelog.
    for version, _date in changelog.extract_all_releases():
        result.total += 1

        if version not in releases:
            logging.debug(f"No GitHub release for version {version}.")
            continue

        release_url = GITHUB_RELEASE_URL.format(repo_url=repo_url, version=version)
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
                    action=ReportAction.SKIPPED,
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
                    action=ReportAction.DRY_RUN,
                    version=version,
                    release_url=release_url,
                )
            )
            continue

        # Live mode: update the release body. A failed edit is counted against
        # its release and the run goes on: one transient error must not leave
        # every release after it drifted.
        try:
            edited = edit_release_notes(f"v{version}", repository, expected)
        except GhCommandError:
            edited = False
        if edited:
            result.updated += 1
            result.rows.append(
                SyncRow(
                    action=ReportAction.UPDATED,
                    version=version,
                    release_url=release_url,
                )
            )
            logging.info(f"Updated release notes for v{version}.")
        else:
            result.failed += 1
            result.rows.append(
                SyncRow(
                    action=ReportAction.FAILED,
                    version=version,
                    release_url=release_url,
                )
            )
            logging.warning(f"Failed to update release notes for v{version}.")

    return result


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
        summary_lines.extend((
            f"| \u2705 Updated | {result.updated} |",
            f"| \u26a0\ufe0f Failed | {result.failed} |",
        ))
    if result.missing_changelog:
        summary_lines.append(
            f"| \u2753 Missing changelog | {result.missing_changelog} |"
        )

    # Per-release details.
    drifted_rows = [row for row in result.rows if row.action != ReportAction.SKIPPED]
    details_section = ""
    if drifted_rows:
        table = render_markdown_table(
            ("Version", "Release", "Action"),
            (
                (
                    f"`{row.version}`",
                    f"[`v{row.version}`]({row.release_url})",
                    row.action.value,
                )
                for row in drifted_rows
            ),
        )
        details_section = f"### \U0001f4dd Details\n\n{table}"

    return render_template(
        "release-sync-report",
        mode=mode,
        summary_rows="\n".join(summary_lines),
        details_section=details_section,
    )
