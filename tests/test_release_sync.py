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

from __future__ import annotations

from unittest.mock import patch

import pytest

from repomatic.changelog import Changelog
from repomatic.github.release_sync import (
    SyncAction,
    SyncResult,
    SyncRow,
    _normalize_body,
    build_expected_body,
    render_sync_report,
    sync_github_releases,
)
from repomatic.github.releases import GitHubRelease

SAMPLE_CHANGELOG = """\
# Changelog

## [`2.0.0` (2026-01-15)](https://github.com/user/repo/compare/v1.1.0...v2.0.0)

- Breaking change: redesigned API.
- Added new feature X.

## [`1.1.0` (2026-01-10)](https://github.com/user/repo/compare/v1.0.0...v1.1.0)

> [!NOTE]
> `1.1.0` is available on PyPI.

- Fixed bug in parser.
- Improved performance.

## [`1.0.0` (2026-01-01)](https://github.com/user/repo/compare/v0.9.0...v1.0.0)

- Initial release.
"""


def test_build_expected_body():
    """Verbatim extraction from changelog."""
    changelog = Changelog(SAMPLE_CHANGELOG)
    body = build_expected_body(changelog, "2.0.0")
    assert "Breaking change: redesigned API." in body
    assert "Added new feature X." in body


def test_build_expected_body_with_admonition():
    """Extraction preserves admonitions when they appear in the changes group."""
    changelog_with_admonition = """\
# Changelog

## [`3.0.0` (2026-02-01)](https://github.com/user/repo/compare/v2.0.0...v3.0.0)

> [!NOTE]
> First release under the new name on PyPI.

- New feature.
"""
    changelog = Changelog(changelog_with_admonition)
    body = build_expected_body(changelog, "3.0.0")
    assert "> [!NOTE]" in body
    assert "New feature." in body


def test_build_expected_body_admonition_override():
    """Override replaces the changelog's availability admonition."""
    changelog = Changelog(SAMPLE_CHANGELOG)
    override = (
        "> [!NOTE]\n"
        "> `1.1.0` is available on"
        " [🐍 PyPI](https://pypi.org/project/pkg/1.1.0/)"
        " and [🐙 GitHub release](https://github.com/user/repo/releases/tag/v1.1.0)."
    )
    body = build_expected_body(changelog, "1.1.0", admonition_override=override)
    # The override admonition should appear in the rendered body.
    assert "🐍 PyPI" in body
    assert "🐙 GitHub release" in body
    # Original changes should still be present.
    assert "Fixed bug in parser." in body


def test_build_expected_body_admonition_override_no_original():
    """Override injects an admonition where none existed in the changelog."""
    changelog = Changelog(SAMPLE_CHANGELOG)
    override = (
        "> [!NOTE]\n"
        "> `2.0.0` is available on"
        " [🐍 PyPI](https://pypi.org/project/pkg/2.0.0/)."
    )
    body = build_expected_body(changelog, "2.0.0", admonition_override=override)
    assert "🐍 PyPI" in body
    assert "Breaking change: redesigned API." in body


def test_build_expected_body_missing_version():
    """Returns empty string for a missing version."""
    changelog = Changelog(SAMPLE_CHANGELOG)
    assert build_expected_body(changelog, "9.9.9") == ""


def test_build_expected_body_includes_all_elements():
    """The github-releases template includes all elements like release-notes."""
    changelog_with_admonitions = """\
# Changelog

## [`1.0.0` (2025-12-01)](https://github.com/user/repo/compare/v0.9.0...v1.0.0)

> [!NOTE]
> `1.0.0` is available on [🐍 PyPI](https://pypi.org/project/pkg/1.0.0/).

- Initial release.
"""
    changelog = Changelog(changelog_with_admonitions)
    body = build_expected_body(changelog, "1.0.0")
    assert "- Initial release." in body
    assert "[!NOTE]" in body
    assert "is available on" in body


def test_build_expected_body_decomposes_elements():
    """Verifies the decompose → template path produces correct output."""
    changelog = Changelog(SAMPLE_CHANGELOG)
    body = build_expected_body(changelog, "1.1.0")
    # Changes should be present.
    assert "Fixed bug in parser." in body
    assert "Improved performance." in body
    # The availability admonition is included by the github-releases template.
    assert "`1.1.0` is available on" in body


def test_build_expected_body_includes_full_changelog_link():
    """The github-releases template appends a full changelog link."""
    changelog = Changelog(SAMPLE_CHANGELOG)
    body = build_expected_body(changelog, "2.0.0")
    assert "**Full changelog**: [`v1.1.0...v2.0.0`](" in body
    assert "compare/v1.1.0...v2.0.0)" in body


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("hello  \n  world  \n", "hello\n  world"),
        ("\n\nfoo\n\n", "foo"),
        ("no trailing", "no trailing"),
        ("", ""),
        ("  \n  \n  ", ""),
    ],
)
def test_normalize_body(text, expected):
    """Whitespace normalization edge cases."""
    assert _normalize_body(text) == expected


def test_sync_all_in_sync(tmp_path):
    """No updates when all bodies match."""
    changelog_path = tmp_path / "changelog.md"
    changelog_path.write_text(SAMPLE_CHANGELOG, encoding="UTF-8")

    changelog = Changelog(SAMPLE_CHANGELOG)
    # Use build_expected_body to get the template-rendered release body.
    body_200 = build_expected_body(changelog, "2.0.0")
    body_110 = build_expected_body(changelog, "1.1.0")
    body_100 = build_expected_body(changelog, "1.0.0")

    mock_releases = {
        "2.0.0": GitHubRelease(date="2026-01-15", body=body_200),
        "1.1.0": GitHubRelease(date="2026-01-10", body=body_110),
        "1.0.0": GitHubRelease(date="2026-01-01", body=body_100),
    }

    with patch(
        "repomatic.github.release_sync.get_github_releases",
        return_value=mock_releases,
    ):
        result = sync_github_releases(
            "https://github.com/user/repo", changelog_path, dry_run=True
        )

    assert result.total == 3
    assert result.in_sync == 3
    assert result.drifted == 0
    assert result.updated == 0
    assert result.failed == 0


def test_sync_detects_drift(tmp_path):
    """Flags releases with different bodies in dry-run."""
    changelog_path = tmp_path / "changelog.md"
    changelog_path.write_text(SAMPLE_CHANGELOG, encoding="UTF-8")

    changelog = Changelog(SAMPLE_CHANGELOG)
    body_110 = build_expected_body(changelog, "1.1.0")

    mock_releases = {
        "2.0.0": GitHubRelease(date="2026-01-15", body="old body"),
        "1.1.0": GitHubRelease(date="2026-01-10", body=body_110),
        "1.0.0": GitHubRelease(date="2026-01-01", body="stale content"),
    }

    with patch(
        "repomatic.github.release_sync.get_github_releases",
        return_value=mock_releases,
    ):
        result = sync_github_releases(
            "https://github.com/user/repo", changelog_path, dry_run=True
        )

    assert result.total == 3
    assert result.in_sync == 1
    assert result.drifted == 2
    # Dry-run rows should use DRY_RUN action.
    drifted_rows = [r for r in result.rows if r.action == SyncAction.DRY_RUN]
    assert len(drifted_rows) == 2


def test_sync_dry_run_no_mutations(tmp_path):
    """Dry-run doesn't call ``gh release edit``."""
    changelog_path = tmp_path / "changelog.md"
    changelog_path.write_text(SAMPLE_CHANGELOG, encoding="UTF-8")

    mock_releases = {
        "2.0.0": GitHubRelease(date="2026-01-15", body="old body"),
    }

    with (
        patch(
            "repomatic.github.release_sync.get_github_releases",
            return_value=mock_releases,
        ),
        patch(
            "repomatic.github.release_sync.run_gh_command",
        ) as mock_gh,
    ):
        sync_github_releases(
            "https://github.com/user/repo", changelog_path, dry_run=True
        )

    mock_gh.assert_not_called()


def test_sync_live_updates(tmp_path):
    """Live mode calls ``gh release edit`` with correct args."""
    changelog_path = tmp_path / "changelog.md"
    changelog_path.write_text(SAMPLE_CHANGELOG, encoding="UTF-8")

    changelog = Changelog(SAMPLE_CHANGELOG)
    expected_body = build_expected_body(changelog, "2.0.0")

    mock_releases = {
        "2.0.0": GitHubRelease(date="2026-01-15", body="old body"),
    }

    with (
        patch(
            "repomatic.github.release_sync.get_github_releases",
            return_value=mock_releases,
        ),
        patch(
            "repomatic.github.release_sync.run_gh_command",
        ) as mock_gh,
    ):
        result = sync_github_releases(
            "https://github.com/user/repo", changelog_path, dry_run=False
        )

    mock_gh.assert_called_once_with([
        "release",
        "edit",
        "v2.0.0",
        "--repo",
        "user/repo",
        "--notes",
        expected_body,
    ])
    assert result.updated == 1
    assert result.drifted == 1


def test_sync_missing_changelog_section(tmp_path):
    """Handles releases without changelog entries."""
    changelog_path = tmp_path / "changelog.md"
    changelog_path.write_text(SAMPLE_CHANGELOG, encoding="UTF-8")

    # Version 3.0.0 has a GitHub release but no changelog section.
    mock_releases = {
        "2.0.0": GitHubRelease(date="2026-01-15", body="body"),
        "1.1.0": GitHubRelease(date="2026-01-10", body="body"),
        "1.0.0": GitHubRelease(date="2026-01-01", body="body"),
        "3.0.0": GitHubRelease(date="2026-02-01", body="something"),
    }

    with patch(
        "repomatic.github.release_sync.get_github_releases",
        return_value=mock_releases,
    ):
        result = sync_github_releases(
            "https://github.com/user/repo", changelog_path, dry_run=True
        )

    # 3.0.0 isn't in the changelog, so it won't be iterated.
    # Only changelog versions are iterated.
    assert result.total == 3


def test_sync_live_failure(tmp_path):
    """Records failure when ``gh release edit`` raises."""
    changelog_path = tmp_path / "changelog.md"
    changelog_path.write_text(SAMPLE_CHANGELOG, encoding="UTF-8")

    mock_releases = {
        "2.0.0": GitHubRelease(date="2026-01-15", body="old body"),
    }

    with (
        patch(
            "repomatic.github.release_sync.get_github_releases",
            return_value=mock_releases,
        ),
        patch(
            "repomatic.github.release_sync.run_gh_command",
            side_effect=RuntimeError("auth failed"),
        ),
    ):
        result = sync_github_releases(
            "https://github.com/user/repo", changelog_path, dry_run=False
        )

    assert result.failed == 1
    assert result.updated == 0
    failed_rows = [r for r in result.rows if r.action == SyncAction.FAILED]
    assert len(failed_rows) == 1


def test_render_sync_report_dry_run():
    """Report format for dry-run mode."""
    result = SyncResult(
        dry_run=True,
        total=5,
        in_sync=3,
        drifted=2,
        rows=[
            SyncRow(
                action=SyncAction.SKIPPED,
                version="1.0.0",
                release_url="https://github.com/user/repo/releases/tag/v1.0.0",
            ),
            SyncRow(
                action=SyncAction.DRY_RUN,
                version="2.0.0",
                release_url="https://github.com/user/repo/releases/tag/v2.0.0",
            ),
            SyncRow(
                action=SyncAction.DRY_RUN,
                version="3.0.0",
                release_url="https://github.com/user/repo/releases/tag/v3.0.0",
            ),
        ],
    )
    report = render_sync_report(result)

    assert "dry-run" in report
    assert "Total releases | 5" in report
    assert "In sync | 3" in report
    assert "Drifted | 2" in report
    # Skipped rows are excluded from details.
    assert "`1.0.0`" not in report
    assert "`2.0.0`" in report
    assert "`3.0.0`" in report
    # Dry-run mode doesn't show Updated/Failed counts.
    assert "Updated" not in report


def test_render_sync_report_live():
    """Report format for live mode."""
    result = SyncResult(
        dry_run=False,
        total=3,
        in_sync=1,
        drifted=2,
        updated=1,
        failed=1,
        rows=[
            SyncRow(
                action=SyncAction.UPDATED,
                version="2.0.0",
                release_url="https://github.com/user/repo/releases/tag/v2.0.0",
            ),
            SyncRow(
                action=SyncAction.FAILED,
                version="3.0.0",
                release_url="https://github.com/user/repo/releases/tag/v3.0.0",
            ),
        ],
    )
    report = render_sync_report(result)

    assert "live" in report
    assert "Updated | 1" in report
    assert "Failed | 1" in report
