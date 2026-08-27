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

"""Tests for `repomatic.changelog`: parsing, updating and linting the changelog."""

from __future__ import annotations

import logging
from dataclasses import asdict
from textwrap import dedent

import pytest

from repomatic.changelog import (
    AVAILABLE_VERB,
    NOT_AVAILABLE_VERB,
    Changelog,
    VersionElements,
    build_release_admonition,
    build_unavailable_admonition,
    count_bullet_words,
    lint_changelog_dates,
    split_changelog_bullets,
    warn_on_empty_sections,
    warn_on_long_bullets,
)
from repomatic.github.pr_body import render_template
from repomatic.github.releases import GitHubRelease, GitHubReleasesUnavailable
from repomatic.pypi import PyPIRelease
from repomatic.tooling.tool_runner import verify_via_write_path
from tests.conftest import skip_unless_tool_runs

SAMPLE_CHANGELOG = dedent(
    """\
    # Changelog

    ## [`1.2.3` (unreleased)](https://github.com/user/repo/compare/v1.2.2...main)

    > [!WARNING]
    > This version is **not released yet** and is under active development.

    - Add new feature.
    - Fix bug.

    ## [`1.2.2` (2024-01-15)](https://github.com/user/repo/compare/v1.2.1...v1.2.2)

    - Previous release.
    """
)
"""Reusable changelog fixture for freeze tests."""


@pytest.mark.parametrize(
    ("version", "initial", "updated"),
    [
        ("1.1.1", None, "# Changelog"),
        ("1.1.1", "", "# Changelog"),
        (
            "1.2.1",
            dedent(
                """\
                # Changelog

                ## [1.2.1 (unreleased)](https://github.com/kdeldycke/extra-platforms/compare/v1.2.0...main)

                > [!WARNING]
                > This version is **not released yet** and is under active development.

                - Fix changelog indention.


                """
            ),
            dedent(
                """\
                # Changelog

                ## [1.2.1 (unreleased)](https://github.com/kdeldycke/extra-platforms/compare/v1.2.0...main)

                > [!WARNING]
                > This version is **not released yet** and is under active development.

                - Fix changelog indention."""
            ),
        ),
        (
            "1.0.0",
            dedent(
                """\
                # Changelog

                ## [1.0.0 (2024-08-20)](https://github.com/kdeldycke/extra-platforms/compare/v0.0.1...v1.0.0)

                - Add documentation.
                """
            ),
            dedent(
                """\
                # Changelog

                ## [`1.0.0` (unreleased)](https://github.com/kdeldycke/extra-platforms/compare/v1.0.0...main)

                > [!WARNING]
                > This version is **not released yet** and is under active development.

                ## [1.0.0 (2024-08-20)](https://github.com/kdeldycke/extra-platforms/compare/v0.0.1...v1.0.0)

                - Add documentation."""
            ),
        ),
    ],
)
def test_changelog_update(version, initial, updated):
    changelog = Changelog(initial, current_version=version)
    assert changelog.update() == updated


def test_freeze():
    """Test that freeze applies all three operations."""
    changelog = Changelog(SAMPLE_CHANGELOG, current_version="1.2.3")
    result = changelog.freeze(release_date="2026-02-14")

    assert result is True
    assert "(unreleased)" not in changelog.content
    assert "(2026-02-14)" in changelog.content
    assert "...main)" not in changelog.content
    assert "...v1.2.3)" in changelog.content
    assert "[!WARNING]" not in changelog.content
    # Release notes are preserved.
    assert "- Add new feature." in changelog.content
    assert "- Fix bug." in changelog.content


def test_freeze_idempotent():
    """Test that freezing an already-frozen changelog is a no-op."""
    changelog = Changelog(SAMPLE_CHANGELOG, current_version="1.2.3")
    changelog.freeze(release_date="2026-02-14")
    frozen_content = changelog.content

    # Second freeze should not change anything.
    result = changelog.freeze(release_date="2026-02-14")
    assert result is False
    assert changelog.content == frozen_content


def test_freeze_file(tmp_path):
    """Test that freeze_file freezes a changelog on disk."""
    path = tmp_path / "changelog.md"
    path.write_text(SAMPLE_CHANGELOG, encoding="UTF-8")

    result = Changelog.freeze_file(path, version="1.2.3", release_date="2026-02-14")

    assert result is True
    content = path.read_text(encoding="UTF-8")
    assert "(unreleased)" not in content
    assert "(2026-02-14)" in content
    assert "...main)" not in content
    assert "...v1.2.3)" in content
    assert "[!WARNING]" not in content
    assert "- Add new feature." in content


def test_freeze_file_already_released(tmp_path):
    """Test that freeze_file is a no-op for released changelogs."""
    path = tmp_path / "changelog.md"
    content = "# Changelog\n\n## [1.0.0 (2024-01-01)](https://example.com)\n"
    path.write_text(content, encoding="UTF-8")

    result = Changelog.freeze_file(path, version="1.0.0", release_date="2026-02-14")

    assert result is False
    assert path.read_text(encoding="UTF-8") == content


def test_freeze_file_missing(tmp_path):
    """Test that freeze_file handles missing files gracefully."""
    path = tmp_path / "nonexistent.md"
    result = Changelog.freeze_file(path, version="1.0.0", release_date="2026-02-14")
    assert result is False


# ---------------------------------------------------------------------------
# decompose_version / replace_section tests
# ---------------------------------------------------------------------------

DECOMPOSE_CHANGELOG = dedent(
    """\
    # Changelog

    ## [`3.0.0` (unreleased)](https://github.com/user/repo/compare/v2.0.0...main)

    > [!WARNING]
    > This version is **not released yet** and is under active development.

    - New feature.

    ## [`2.0.0` (2026-02-01)](https://github.com/user/repo/compare/v1.0.0...v2.0.0)

    > [!NOTE]
    > `2.0.0` is available on [🐍 PyPI](https://pypi.org/project/pkg/2.0.0/).

    > [!CAUTION]
    > `2.0.0` has been [yanked from PyPI](https://pypi.org/project/pkg/2.0.0/).

    - Breaking change.
    - New API.

    ## [`1.0.0` (2025-12-01)](https://github.com/user/repo/compare/v0.9.0...v1.0.0)

    - Initial release.
    """
)
"""Changelog with all element types for decompose tests."""


@pytest.mark.parametrize(
    ("version", "expected_date", "expected_url", "contains", "equals"),
    [
        pytest.param(
            "3.0.0",
            "unreleased",
            "https://github.com/user/repo/compare/v2.0.0...main",
            {"development_warning": ("contains", "not released yet")},
            {"changes": "- New feature.", "editorial_admonition": ""},
            id="dev-warning-and-changes",
        ),
        pytest.param(
            "2.0.0",
            "2026-02-01",
            "https://github.com/user/repo/compare/v1.0.0...v2.0.0",
            {
                "availability_admonition": (
                    "contains",
                    ["[!NOTE]", "is available on"],
                ),
                "yanked_admonition": (
                    "contains",
                    ["[!CAUTION]", "yanked from PyPI"],
                ),
                "changes": (
                    "contains",
                    ["- Breaking change.", "- New API."],
                ),
            },
            {"development_warning": "", "editorial_admonition": ""},
            id="availability-and-yanked",
        ),
        pytest.param(
            "1.0.0",
            "2025-12-01",
            "https://github.com/user/repo/compare/v0.9.0...v1.0.0",
            {},
            {
                "development_warning": "",
                "availability_admonition": "",
                "editorial_admonition": "",
                "yanked_admonition": "",
                "changes": "- Initial release.",
            },
            id="changes-only",
        ),
    ],
)
def test_decompose_version(version, expected_date, expected_url, contains, equals):
    """Decompose versions with varying element combinations."""
    changelog = Changelog(DECOMPOSE_CHANGELOG)
    elements = changelog.decompose_version(version)

    assert elements.version == version
    assert elements.date == expected_date
    assert elements.compare_url == expected_url

    for field, (_, targets) in contains.items():
        value = getattr(elements, field)
        for target in targets if isinstance(targets, list) else [targets]:
            assert target in value
    for field, expected in equals.items():
        assert getattr(elements, field) == expected


def test_decompose_version_extracts_editorial_admonitions():
    """Editorial admonitions (e.g. [!TIP]) land in editorial_admonition."""
    changelog_text = dedent(
        """\
        # Changelog

        ## [`1.0.0` (2025-12-01)](https://github.com/u/r/compare/v0.9.0...v1.0.0)

        > [!TIP]
        > This is a hand-written tip.

        - Initial release.
        """
    )
    changelog = Changelog(changelog_text)
    elements = changelog.decompose_version("1.0.0")

    assert "[!TIP]" in elements.editorial_admonition
    assert "hand-written tip" in elements.editorial_admonition
    assert "[!TIP]" not in elements.changes
    assert "- Initial release." in elements.changes
    assert elements.availability_admonition == ""


def test_decompose_version_multiple_editorial_admonitions():
    """Multiple editorial admonitions are joined in editorial_admonition."""
    changelog_text = dedent(
        """\
        # Changelog

        ## [`1.0.0` (2025-12-01)](https://github.com/u/r/compare/v0.9.0...v1.0.0)

        > [!NOTE]
        > First editorial note.

        > [!CAUTION]
        > Important editorial caution.

        - Some change.
        """
    )
    changelog = Changelog(changelog_text)
    elements = changelog.decompose_version("1.0.0")

    assert "[!NOTE]" in elements.editorial_admonition
    assert "First editorial note." in elements.editorial_admonition
    assert "[!CAUTION]" in elements.editorial_admonition
    assert "Important editorial caution." in elements.editorial_admonition
    assert "\n\n" in elements.editorial_admonition
    assert "- Some change." in elements.changes
    assert "[!NOTE]" not in elements.changes
    assert "[!CAUTION]" not in elements.changes


def test_decompose_version_editorial_with_auto_generated():
    """Editorial and auto-generated admonitions are correctly separated."""
    changelog_text = dedent(
        """\
        # Changelog

        ## [`2.0.0` (2026-01-15)](https://github.com/u/r/compare/v1.0.0...v2.0.0)

        > [!NOTE]
        > `2.0.0` is available on [🐍 PyPI](https://pypi.org/project/pkg/2.0.0/).

        > [!NOTE]
        > This is a hand-written editorial note.

        - Breaking change.
        """
    )
    changelog = Changelog(changelog_text)
    elements = changelog.decompose_version("2.0.0")

    assert "is available on" in elements.availability_admonition
    assert "hand-written editorial note" in elements.editorial_admonition
    assert "hand-written editorial note" not in elements.availability_admonition
    assert "is available on" not in elements.editorial_admonition
    assert "- Breaking change." in elements.changes


def test_decompose_version_empty():
    """Version not found returns empty elements."""
    changelog = Changelog(DECOMPOSE_CHANGELOG)
    elements = changelog.decompose_version("9.9.9")

    assert elements == VersionElements()


@pytest.mark.parametrize(
    ("version", "new_section", "expected", "present", "absent"),
    [
        pytest.param(
            "1.0.0",
            (
                "## [`1.0.0` (2025-12-15)]"
                "(https://github.com/user/repo/compare/v0.9.0...v1.0.0)\n\n"
                "- Updated release.\n"
            ),
            True,
            ["- Updated release.", "2025-12-15", "- Breaking change.", "3.0.0"],
            ["- Initial release."],
            id="replaces-section",
        ),
        pytest.param(
            "9.9.9",
            "## [`9.9.9` ...]\n\n- x\n",
            False,
            ["- Initial release."],
            [],
            id="no-match",
        ),
    ],
)
def test_replace_section(version, new_section, expected, present, absent):
    """Test replacing an entire section (heading + body)."""
    changelog = Changelog(DECOMPOSE_CHANGELOG)
    changed = changelog.replace_section(version, new_section)

    assert changed is expected
    for text in present:
        assert text in changelog.content
    for text in absent:
        assert text not in changelog.content


MULTI_RELEASE_CHANGELOG = dedent(
    """\
    # Changelog

    ## [2.0.0 (unreleased)](https://github.com/user/repo/compare/v1.1.0...main)

    > [!WARNING]
    > This version is **not released yet** and is under active development.

    ## [1.1.0 (2026-02-10)](https://github.com/user/repo/compare/v1.0.0...v1.1.0)

    - Second release.

    ## [1.0.0 (2025-12-01)](https://github.com/user/repo/compare/v0.9.0...v1.0.0)

    - Initial release.
    """
)
"""Changelog with multiple released versions and one unreleased."""


@pytest.fixture
def changelog_file(tmp_path):
    """A changelog on disk carrying {data}`MULTI_RELEASE_CHANGELOG`.

    A test needing other content writes over the returned path.
    """
    path = tmp_path / "changelog.md"
    path.write_text(MULTI_RELEASE_CHANGELOG, encoding="UTF-8")
    return path


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        pytest.param(
            MULTI_RELEASE_CHANGELOG,
            [("1.1.0", "2026-02-10"), ("1.0.0", "2025-12-01")],
            id="multiple-releases",
        ),
        pytest.param(
            dedent(
                """\
                # Changelog

                ## [1.0.0 (unreleased)](https://github.com/user/repo/compare/v0.0.1...main)

                > [!WARNING]
                > This version is **not released yet** and is under active development.
                """
            ),
            [],
            id="unreleased-only",
        ),
        pytest.param(
            "# Changelog\n",
            [],
            id="empty",
        ),
    ],
)
def test_extract_all_releases(content, expected):
    """Test extraction of released versions from varying changelogs."""
    changelog = Changelog(content)
    assert changelog.extract_all_releases() == expected


def _github_unavailable(_repo_url, force_refresh=False):
    """Stand in for `get_github_releases` during a GitHub API outage.

    Mirrors the real signature, `force_refresh` included: the lint reloads
    both sources live when a cached answer would retract an availability
    claim, and a double that refuses the keyword fails there instead of at
    the behavior under test.
    """
    raise GitHubReleasesUnavailable("simulated 502 Bad Gateway")


def _pypi_mock(releases, package="my-package", fresh=None):
    """Build a monkeypatch-compatible mock for ``get_pypi_release_dates``.

    Each value in *releases* is a ``(date, yanked)`` tuple, optionally
    extended with a yank reason. The *package* argument is injected as the
    third ``PyPIRelease`` field so callers don't need to repeat it in every
    entry.

    Passing *fresh* models a cache written before a publication: *releases*
    is what the cached read returns and *fresh* what a forced refresh does.
    """

    def lookup(pkg, *, force_refresh=False):
        source = fresh if force_refresh and fresh is not None else releases
        return {
            v: PyPIRelease(
                date=args[0],
                yanked=args[1],
                package=package,
                yanked_reason=args[2] if len(args) > 2 else "",
            )
            for v, args in source.items()
        }

    return lookup


def _github_mock(versions, fresh=None):
    """Build a monkeypatch-compatible mock for ``get_github_releases``.

    Accepts either a list of version strings (uses a dummy date) or a
    dict mapping version strings to date strings. *fresh* plays the same
    stale-cache role as in {func}`_pypi_mock`.
    """

    def as_map(source):
        if isinstance(source, dict):
            return {v: GitHubRelease(date=d, body="") for v, d in source.items()}
        return {v: GitHubRelease(date="2026-01-01", body="") for v in source}

    def lookup(repo_url, *, force_refresh=False):
        return as_map(fresh if force_refresh and fresh is not None else versions)

    return lookup


def _patch_sources(
    monkeypatch,
    *,
    pypi=None,
    github=(),
    tags=None,
    package="my-package",
    pypi_fresh=None,
    github_fresh=None,
):
    """Stub every external source `lint_changelog_dates` consults.

    Each source defaults to reachable-but-empty, so a test names only the ones
    whose answers it depends on and the rest read as "nothing published there".

    :param pypi: Version to `(date, yanked[, reason])`, or a callable taking a
        package name for the rename tests.
    :param github: Versions as a list, version to date as a dict, or a callable
        taking a repository URL.
    :param tags: Version to tag date.
    :param package: What `get_project_name` reports, `None` for a project whose
        name cannot be detected.
    :param pypi_fresh: What PyPI answers on a forced refresh, when it should
        differ from the cached *pypi* answer.
    :param github_fresh: Same, for GitHub releases.
    """
    monkeypatch.setattr(
        "repomatic.changelog.get_pypi_release_dates",
        pypi
        if callable(pypi)
        else _pypi_mock(pypi or {}, package or "my-package", pypi_fresh),
    )
    monkeypatch.setattr("repomatic.changelog.get_project_name", lambda: package)
    monkeypatch.setattr(
        "repomatic.changelog.get_github_releases",
        github if callable(github) else _github_mock(github, github_fresh),
    )
    monkeypatch.setattr("repomatic.changelog.get_all_version_tags", lambda: tags or {})


def test_lint_changelog_dates_pypi_all_match(changelog_file, monkeypatch):
    """Test that lint returns 0 when all PyPI dates match."""
    _patch_sources(
        monkeypatch,
        pypi={"1.1.0": ("2026-02-10", False), "1.0.0": ("2025-12-01", False)},
        github=["1.1.0", "1.0.0"],
    )

    assert lint_changelog_dates(changelog_file) == 0


def test_lint_changelog_dates_pypi_mismatch(changelog_file, monkeypatch):
    """Test that lint returns 1 when a PyPI date differs."""
    _patch_sources(
        monkeypatch,
        pypi={"1.1.0": ("2026-02-09", False), "1.0.0": ("2025-12-01", False)},
        github=["1.1.0", "1.0.0"],
    )

    assert lint_changelog_dates(changelog_file) == 1


def test_lint_changelog_dates_fallback_to_tags(changelog_file, monkeypatch):
    """Test that lint falls back to git tags when not on PyPI."""
    # PyPI returns empty dict (not published).
    _patch_sources(monkeypatch, tags={"1.1.0": "2026-02-10", "1.0.0": "2025-12-01"})

    assert lint_changelog_dates(changelog_file) == 0


def test_lint_changelog_dates_fallback_no_package(changelog_file, monkeypatch):
    """Test that lint falls back to git tags when no package name is detected."""
    _patch_sources(
        monkeypatch,
        package=None,
        tags={"1.1.0": "2026-02-10", "1.0.0": "2025-12-01"},
    )

    assert lint_changelog_dates(changelog_file) == 0


def test_lint_changelog_dates_warns_missing_pypi(changelog_file, monkeypatch, caplog):
    """Test that versions not on PyPI get a warning if they postdate the first
    PyPI release, and an info log if they predate it."""
    # Only the oldest version is on PyPI; 1.1.0 is an unexpected gap.
    _patch_sources(monkeypatch, pypi={"1.0.0": ("2025-12-01", False)})

    with caplog.at_level(logging.WARNING):
        # Should return 0: 1.0.0 matches, 1.1.0 warned but non-fatal.
        assert lint_changelog_dates(changelog_file) == 0

    assert "1.1.0: not found on PyPI" in caplog.text
    # The warning names its remedy so the maintainer knows how to silence it.
    assert "abandoned-versions" in caplog.text


def test_lint_changelog_dates_skips_pre_pypi(changelog_file, monkeypatch, caplog):
    """Test that versions older than the first PyPI release are skipped."""
    # Only the newest version is on PyPI; 1.0.0 predates publication.
    _patch_sources(monkeypatch, pypi={"1.1.0": ("2026-02-10", False)})

    with caplog.at_level(logging.INFO):
        assert lint_changelog_dates(changelog_file) == 0

    assert "predates PyPI" in caplog.text


def test_lint_changelog_dates_skips_abandoned(changelog_file, monkeypatch, caplog):
    """Test that explicitly-abandoned versions are skipped without warning."""
    # 1.0.0 is on PyPI, 1.1.0 is documented but was abandoned.
    _patch_sources(monkeypatch, pypi={"1.0.0": ("2025-12-01", False)})

    with caplog.at_level(logging.INFO):
        assert lint_changelog_dates(changelog_file, abandoned_versions=["1.1.0"]) == 0

    assert "1.1.0: abandoned" in caplog.text
    assert "1.1.0: not found on PyPI" not in caplog.text


def test_lint_changelog_dates_archive_suppresses_orphans(tmp_path, monkeypatch):
    """Versions documented in the archive are neither flagged nor re-inserted
    as orphans."""
    live = tmp_path / "changelog.md"
    live.write_text(
        "# Changelog\n\n"
        "## [`2.0.0` (2026-02-01)]"
        "(https://github.com/user/repo/compare/v1.0.0...v2.0.0)\n\n"
        "- Latest.\n",
        encoding="UTF-8",
    )
    archive = tmp_path / "changelog-archive.md"
    archive.write_text(
        "# Changelog archive\n\n"
        "## [`1.0.0` (2025-12-01)]"
        "(https://github.com/user/repo/compare/v0.9.0...v1.0.0)\n\n"
        "- Old.\n",
        encoding="UTF-8",
    )

    _patch_sources(
        monkeypatch,
        pypi={"2.0.0": ("2026-02-01", False), "1.0.0": ("2025-12-01", False)},
        github=["2.0.0", "1.0.0"],
    )

    # Without the archive, 1.0.0 is an orphan: on PyPI and GitHub, but missing
    # from the live changelog.
    assert lint_changelog_dates(live) == 1

    # With the archive, 1.0.0 counts as documented: no orphan.
    assert lint_changelog_dates(live, archive_path=archive) == 0

    # Under --fix, the archived version is not resurrected into the live file.
    assert lint_changelog_dates(live, archive_path=archive, fix=True) == 0
    live_headings = Changelog(
        live.read_text(encoding="UTF-8")
    ).extract_all_version_headings()
    assert live_headings == {"2.0.0"}


@pytest.mark.parametrize(
    "prerelease",
    ("2.0.0.dev0", "2.0.0a1", "2.0.0b2", "2.0.0rc3"),
)
def test_lint_changelog_dates_ignores_prerelease_orphans(
    tmp_path, monkeypatch, prerelease
):
    """A published pre-release is never treated as a missing changelog entry.

    The changelog documents only final releases. A pre-release
    (dev/alpha/beta/rc) present as a git tag, PyPI upload, or GitHub release
    must not be flagged as an orphan. The former bug inserted a spurious
    ``## X.Y.Z.dev0`` section (placed *below* its final release, since a
    pre-release sorts lower) and rewrote the final release's comparison-URL
    base to point at it (`v1.0.0...v2.0.0` became `v2.0.0.dev0...v2.0.0`).
    """
    path = tmp_path / "changelog.md"
    path.write_text(
        "# Changelog\n\n"
        "## [`2.0.0` (2026-02-01)]"
        "(https://github.com/user/repo/compare/v1.0.0...v2.0.0)\n\n"
        "- Latest.\n\n"
        "## [`1.0.0` (2025-12-01)]"
        "(https://github.com/user/repo/compare/v0.9.0...v1.0.0)\n\n"
        "- First.\n",
        encoding="UTF-8",
    )

    # The pre-release is published on every external source but absent from
    # the changelog. Each source alone must be enough to exercise the filter.
    _patch_sources(
        monkeypatch,
        pypi={
            "2.0.0": ("2026-02-01", False),
            prerelease: ("2026-01-25", False),
            "1.0.0": ("2025-12-01", False),
        },
        github=["2.0.0", prerelease, "1.0.0"],
        tags={"2.0.0": "2026-02-01", prerelease: "2026-01-25", "1.0.0": "2025-12-01"},
    )

    # The pre-release is not an orphan, so no mismatch is reported.
    assert lint_changelog_dates(path) == 0

    # Under --fix the pre-release is never materialized: no heading is
    # inserted, and the final release keeps its original comparison base.
    assert lint_changelog_dates(path, fix=True) == 0
    content = path.read_text(encoding="UTF-8")
    headings = Changelog(content).extract_all_version_headings()
    assert headings == {"2.0.0", "1.0.0"}
    assert prerelease not in headings
    assert "compare/v1.0.0...v2.0.0" in content
    assert prerelease not in content


@pytest.mark.parametrize(
    ("changes", "expected_count"),
    (
        ("", 0),
        ("- Only one entry.", 1),
        ("- One.\n- Two.\n- Three.", 3),
        ("- Wrapped entry that\n  continues on the next line.", 1),
        ("- Parent entry.\n  - Child A.\n  - Child B.", 1),
        ("Intro prose, not a bullet.\n\n- The only real bullet.", 1),
    ),
)
def test_split_changelog_bullets_count(changes, expected_count):
    """Each top-level `-` starts one entry; wraps and sub-bullets fold in."""
    assert len(split_changelog_bullets(changes)) == expected_count


def test_split_changelog_bullets_folds_continuation_and_sub_bullets():
    """A wrapped line and indented sub-bullets stay with their parent entry."""
    changes = "- Parent entry\n  continues here.\n  - A sub-point.\n- Second entry."
    bullets = split_changelog_bullets(changes)
    assert bullets == [
        "- Parent entry\n  continues here.\n  - A sub-point.",
        "- Second entry.",
    ]


@pytest.mark.parametrize(
    ("bullet", "expected_words"),
    (
        ("- Add a feature.", 3),
        ("- First line of the entry\n  continued onto a second line.", 10),
        ("- Parent entry.\n  - Nested sub-bullet item.", 5),
        ("plain text without any marker", 5),
    ),
)
def test_count_bullet_words(bullet, expected_words):
    """List markers are stripped before counting; everything else counts."""
    assert count_bullet_words(bullet) == expected_words


# A 20-word entry, comfortably over the 5-word ceiling used in the tests below.
_LONG_BULLET = (
    "- This changelog entry is deliberately written far too long to fit "
    "within the strict word ceiling that the check enforces."
)
_SHORT_BULLET = "- Add the seasonal fruit basket."


def _unreleased_changelog(bullet: str) -> str:
    return dedent(
        """\
        # Changelog

        ## [`1.2.3` (unreleased)](https://github.com/user/repo/compare/v1.2.2...main)

        > [!WARNING]
        > This version is **not released yet** and is under active development.

        {bullet}
        """
    ).format(bullet=bullet)


def test_warn_on_long_bullets_flags_unreleased(caplog):
    """An over-long unreleased bullet emits a non-fatal warning."""
    changelog = Changelog(_unreleased_changelog(_LONG_BULLET))
    with caplog.at_level(logging.WARNING):
        warn_on_long_bullets(changelog, threshold=5)
    assert "1.2.3: changelog entry 1 runs" in caplog.text


def test_warn_on_long_bullets_short_entry_is_silent(caplog):
    """A short unreleased bullet stays under the ceiling and warns nothing."""
    changelog = Changelog(_unreleased_changelog(_SHORT_BULLET))
    with caplog.at_level(logging.WARNING):
        warn_on_long_bullets(changelog, threshold=5)
    assert caplog.records == []


def test_warn_on_long_bullets_ignores_released_sections(caplog):
    """Immutable released sections are never flagged, only the unreleased one."""
    changelog = Changelog(
        dedent(
            """\
            # Changelog

            ## [`1.2.3` (unreleased)](https://github.com/user/repo/compare/v1.2.2...main)

            - Add the seasonal fruit basket.

            ## [`1.2.2` (2024-01-15)](https://github.com/user/repo/compare/v1.2.1...v1.2.2)

            - This released entry is deliberately written far too long to fit within the strict word ceiling that the check would otherwise enforce.
            """
        )
    )
    with caplog.at_level(logging.WARNING):
        warn_on_long_bullets(changelog, threshold=5)
    assert caplog.records == []


def test_warn_on_long_bullets_threshold_zero_disables(caplog):
    """A threshold of 0 disables the check, even for a very long bullet."""
    changelog = Changelog(_unreleased_changelog(_LONG_BULLET))
    with caplog.at_level(logging.WARNING):
        warn_on_long_bullets(changelog, threshold=0)
    assert caplog.records == []


def _released_changelog(body: str) -> str:
    return dedent(
        """\
        # Changelog

        ## [`1.2.3` (unreleased)](https://github.com/user/repo/compare/v1.2.2...main)

        ## [`1.2.2` (2024-01-15)](https://github.com/user/repo/compare/v1.2.1...v1.2.2)

        {body}
        """
    ).format(body=body)


def test_warn_on_empty_sections_flags_released(caplog):
    """A released section with no entry emits a non-fatal warning."""
    changelog = Changelog(_released_changelog(""))
    with caplog.at_level(logging.WARNING):
        warn_on_empty_sections(changelog)
    assert "1.2.2: released section holds no entry" in caplog.text


def test_warn_on_empty_sections_ignores_the_unreleased_one(caplog):
    """The empty unreleased section the post-release bump creates is expected."""
    changelog = Changelog(_released_changelog("- Add the seasonal fruit basket."))
    with caplog.at_level(logging.WARNING):
        warn_on_empty_sections(changelog)
    assert caplog.records == []


def test_warn_on_empty_sections_counts_an_admonition_as_empty(caplog):
    """An availability caveat is not a statement of what changed."""
    changelog = Changelog(
        _released_changelog(
            "> [!WARNING]\n> This version ships without `windows-arm64` binaries."
        )
    )
    with caplog.at_level(logging.WARNING):
        warn_on_empty_sections(changelog)
    assert "1.2.2: released section holds no entry" in caplog.text


def test_lint_changelog_dates_warns_long_unreleased_bullet(tmp_path, caplog):
    """lint-changelog surfaces the bullet-length warning without failing.

    An unreleased-only changelog has no released versions, so the date check
    returns 0 early; the length warning still fires beforehand.
    """
    path = tmp_path / "changelog.md"
    path.write_text(_unreleased_changelog(_LONG_BULLET), encoding="UTF-8")
    with caplog.at_level(logging.WARNING):
        assert lint_changelog_dates(path, bullet_word_threshold=5) == 0
    assert "1.2.3: changelog entry 1 runs" in caplog.text


def test_lint_fix_corrects_date(changelog_file, monkeypatch):
    """Test that --fix corrects mismatched dates in the changelog."""
    _patch_sources(
        monkeypatch,
        pypi={"1.1.0": ("2026-02-11", False), "1.0.0": ("2025-12-01", False)},
        github=["1.1.0", "1.0.0"],
    )

    # Mismatch on 1.1.0: changelog says 2026-02-10, PyPI says 2026-02-11.
    # fix=True corrects it in-place, so return 0 to let downstream steps proceed.
    result = lint_changelog_dates(changelog_file, fix=True)
    assert result == 0

    content = changelog_file.read_text(encoding="UTF-8")
    assert "(2026-02-11)" in content
    assert "(2026-02-10)" not in content


def test_lint_fix_adds_release_admonition(changelog_file, monkeypatch):
    """Test that --fix adds conditional release admonitions."""
    _patch_sources(
        monkeypatch,
        pypi={"1.1.0": ("2026-02-10", False), "1.0.0": ("2025-12-01", False)},
        github=["1.1.0", "1.0.0"],
    )

    lint_changelog_dates(changelog_file, fix=True)
    content = changelog_file.read_text(encoding="UTF-8")

    # Both sources available — NOTE only, no WARNINGs.
    assert "[🐍 PyPI](https://pypi.org/project/my-package/1.1.0/)" in content
    assert "[🐙 GitHub](https://github.com/user/repo/releases/tag/v1.1.0)" in content
    assert "is **not available** on" not in content
    # 1.0.0 is the first on both platforms — "first version" wording.
    assert "`1.0.0` is the *first version* available on" in content
    # 1.1.0 is not the first on either — normal wording.
    assert "`1.1.0` is available on" in content


def test_lint_fix_github_only(changelog_file, monkeypatch):
    """Test that --fix adds GitHub-only admonition when not on PyPI."""
    # 1.0.0 on PyPI, 1.1.0 only on GitHub (not on PyPI).
    _patch_sources(
        monkeypatch,
        pypi={"1.0.0": ("2025-12-01", False)},
        github=["1.1.0", "1.0.0"],
    )

    lint_changelog_dates(changelog_file, fix=True)
    content = changelog_file.read_text(encoding="UTF-8")

    # 1.1.0: GitHub only — NOTE for GitHub, WARNING for missing PyPI.
    assert "[🐙 GitHub](https://github.com/user/repo/releases/tag/v1.1.0)" in content
    assert "is **not available** on 🐍 PyPI." in content
    assert "my-package/1.1.0" not in content
    # 1.0.0: both sources, first on both — "first version" wording.
    assert "`1.0.0` is the *first version* available on" in content
    assert "[🐍 PyPI](https://pypi.org/project/my-package/1.0.0/)" in content
    assert "[🐙 GitHub](https://github.com/user/repo/releases/tag/v1.0.0)" in content


THREE_RELEASE_CHANGELOG = dedent(
    """\
    # Changelog

    ## [3.0.0 (unreleased)](https://github.com/user/repo/compare/v2.0.0...main)

    > [!WARNING]
    > This version is **not released yet** and is under active development.

    ## [2.0.0 (2026-02-10)](https://github.com/user/repo/compare/v1.0.0...v2.0.0)

    - Second release.

    ## [1.0.0 (2025-12-01)](https://github.com/user/repo/compare/v0.9.0...v1.0.0)

    - First release.

    ## [0.5.0 (2025-06-01)](https://github.com/user/repo/compare/v0.4.0...v0.5.0)

    - Pre-PyPI release.
    """
)
"""Changelog with three released versions for first-version testing."""


def test_lint_fix_first_version_admonition(tmp_path, monkeypatch):
    """Test that --fix uses 'first version' wording for inaugural releases."""
    path = tmp_path / "changelog.md"
    path.write_text(THREE_RELEASE_CHANGELOG, encoding="UTF-8")

    # 0.5.0: GitHub only (predates PyPI).
    # 1.0.0: first on both PyPI and GitHub.
    # 2.0.0: on both, but not first on either.
    _patch_sources(
        monkeypatch,
        pypi={"2.0.0": ("2026-02-10", False), "1.0.0": ("2025-12-01", False)},
        github=["2.0.0", "1.0.0", "0.5.0"],
    )

    lint_changelog_dates(path, fix=True)
    content = path.read_text(encoding="UTF-8")

    # 0.5.0: GitHub only, first on GitHub — "first version" wording.
    assert "`0.5.0` is the *first version* available on" in content
    assert "[🐙 GitHub](https://github.com/user/repo/releases/tag/v0.5.0)" in content
    # 1.0.0: first on PyPI but not first on GitHub — normal wording.
    assert "`1.0.0` is available on" in content
    # 2.0.0: not first on either — normal wording.
    assert "`2.0.0` is available on" in content


def test_lint_fix_pypi_only(changelog_file, monkeypatch):
    """Test that --fix adds PyPI NOTE and GitHub WARNING when only on PyPI."""
    # 1.1.0 on PyPI only, not on GitHub.
    _patch_sources(
        monkeypatch,
        pypi={"1.1.0": ("2026-02-10", False), "1.0.0": ("2025-12-01", False)},
        github=["1.0.0"],
    )

    lint_changelog_dates(changelog_file, fix=True)
    content = changelog_file.read_text(encoding="UTF-8")

    # 1.1.0: PyPI NOTE, GitHub WARNING.
    assert "[🐍 PyPI](https://pypi.org/project/my-package/1.1.0/)" in content
    assert "is **not available** on 🐙 GitHub." in content
    assert "releases/tag/v1.1.0" not in content
    # 1.0.0: both sources.
    assert "[🐍 PyPI](https://pypi.org/project/my-package/1.0.0/)" in content
    assert "[🐙 GitHub](https://github.com/user/repo/releases/tag/v1.0.0)" in content


def test_lint_fix_no_warning_predates_github(changelog_file, monkeypatch):
    """Test that --fix skips GitHub WARNING for versions predating the first
    GitHub release."""
    # Both on PyPI; only 1.1.0 on GitHub (1.0.0 predates GitHub releases).
    _patch_sources(
        monkeypatch,
        pypi={"1.1.0": ("2026-02-10", False), "1.0.0": ("2025-12-01", False)},
        github=["1.1.0"],
    )

    lint_changelog_dates(changelog_file, fix=True)
    content = changelog_file.read_text(encoding="UTF-8")

    # 1.0.0 predates first GitHub release (1.1.0) — no GitHub WARNING.
    assert "is **not available** on" not in content
    # 1.0.0: PyPI NOTE only (no GitHub link, no warning).
    assert "[🐍 PyPI](https://pypi.org/project/my-package/1.0.0/)" in content
    # 1.1.0: both sources.
    assert "[🐍 PyPI](https://pypi.org/project/my-package/1.1.0/)" in content
    assert "[🐙 GitHub](https://github.com/user/repo/releases/tag/v1.1.0)" in content


def test_lint_fix_adds_yanked_admonition(changelog_file, monkeypatch):
    """Test that --fix adds a CAUTION admonition for yanked releases."""
    _patch_sources(
        monkeypatch,
        pypi={"1.1.0": ("2026-02-10", True), "1.0.0": ("2025-12-01", False)},
        github=["1.1.0", "1.0.0"],
    )

    lint_changelog_dates(changelog_file, fix=True)
    content = changelog_file.read_text(encoding="UTF-8")

    # Yanked CAUTION links to the specific PyPI project page.
    assert (
        "`1.1.0` has been [yanked from PyPI]"
        "(https://pypi.org/project/my-package/1.1.0/)"
    ) in content
    # NOTE should show GitHub only, not PyPI (yanked release excluded).
    assert "[🐍 PyPI](https://pypi.org/project/my-package/1.1.0/)" not in content
    assert "[🐙 GitHub](https://github.com/user/repo/releases/tag/v1.1.0)" in content


def test_lint_fix_yanked_admonition_carries_reason(changelog_file, monkeypatch):
    """PyPI's yank reason lands in the CAUTION admonition."""
    _patch_sources(
        monkeypatch,
        pypi={
            "1.1.0": ("2026-02-10", True, "Superseded by a corrected upload."),
            "1.0.0": ("2025-12-01", False),
        },
        github=["1.1.0", "1.0.0"],
    )

    lint_changelog_dates(changelog_file, fix=True)
    content = changelog_file.read_text(encoding="UTF-8")

    # The reason follows the link, and its own period does not double up with
    # the one closing the sentence.
    assert (
        "`1.1.0` has been [yanked from PyPI]"
        "(https://pypi.org/project/my-package/1.1.0/):"
        " Superseded by a corrected upload."
    ) in content
    assert "upload.." not in content


def test_lint_fix_no_admonition_when_nowhere(changelog_file, monkeypatch):
    """Test that --fix adds WARNINGs for both platforms when version is on
    neither PyPI nor GitHub."""
    # 1.0.0 on PyPI; 1.1.0 on neither.
    _patch_sources(monkeypatch, pypi={"1.0.0": ("2025-12-01", False)}, github=["1.0.0"])

    lint_changelog_dates(changelog_file, fix=True)
    content = changelog_file.read_text(encoding="UTF-8")

    # 1.1.0: WARNING listing both missing platforms.
    assert ("is **not available** on 🐍 PyPI and 🐙 GitHub.") in content
    assert "releases/tag/v1.1.0" not in content
    assert "my-package/1.1.0" not in content
    # 1.0.0 has both.
    assert "[🐍 PyPI](https://pypi.org/project/my-package/1.0.0/)" in content


def test_lint_fix_idempotent(changelog_file, monkeypatch):
    """Test that running --fix twice produces the same result."""
    mock = _pypi_mock({
        "1.1.0": ("2026-02-10", False),
        "1.0.0": ("2025-12-01", False),
    })
    _patch_sources(monkeypatch, pypi=mock, github=["1.1.0", "1.0.0"])

    lint_changelog_dates(changelog_file, fix=True)
    first_content = changelog_file.read_text(encoding="UTF-8")

    lint_changelog_dates(changelog_file, fix=True)
    second_content = changelog_file.read_text(encoding="UTF-8")

    assert first_content == second_content


def test_lint_fix_removes_stale_unavailable_warning(tmp_path, monkeypatch):
    """Test that --fix removes stale 'is **not available** on' warnings when
    the version becomes available."""
    # Pre-seed 1.0.0 with a stale unavailable warning.
    stale = build_unavailable_admonition(
        "1.0.0",
        missing_pypi=True,
    )
    content = MULTI_RELEASE_CHANGELOG.replace(
        "- Initial release.",
        stale + "\n\n- Initial release.",
    )
    path = tmp_path / "changelog.md"
    path.write_text(content, encoding="UTF-8")

    # Now 1.0.0 is on both PyPI and GitHub — stale warning should go.
    _patch_sources(
        monkeypatch,
        pypi={"1.1.0": ("2026-02-10", False), "1.0.0": ("2025-12-01", False)},
        github=["1.1.0", "1.0.0"],
    )

    lint_changelog_dates(path, fix=True)
    result = path.read_text(encoding="UTF-8")

    assert "is **not available** on" not in result
    # NOTE admonitions should be present.
    assert "[🐍 PyPI](https://pypi.org/project/my-package/1.0.0/)" in result
    assert "[🐙 GitHub](https://github.com/user/repo/releases/tag/v1.0.0)" in result
    assert "Initial release." in result


def _changelog_claiming_1_1_0_available(tmp_path):
    """Write a changelog whose `1.1.0` section claims both platforms.

    The starting point for the retraction tests: a correct, already-published
    section that a stale lookup would want to demote to "not available".
    """
    note = build_release_admonition(
        "1.1.0",
        pypi_url="https://pypi.org/project/my-package/1.1.0/",
        github_url="https://github.com/user/repo/releases/tag/v1.1.0",
    )
    path = tmp_path / "changelog.md"
    path.write_text(
        MULTI_RELEASE_CHANGELOG.replace(
            "- Second release.", f"{note}\n\n- Second release."
        ),
        encoding="UTF-8",
    )
    return path


@pytest.mark.parametrize("fix", (False, True))
def test_lint_keeps_availability_when_only_the_cache_is_stale(
    tmp_path, monkeypatch, caplog, fix
):
    """A cached lookup predating a release must not retract its availability.

    Both lookups are TTL-cached for a day, so between publishing `1.1.0` and
    the cache expiring they still answer "1.0.0 only". Acting on that would
    stamp a false "not available" warning onto a correct section under `--fix`,
    and report the published version as missing in either mode.
    """
    path = _changelog_claiming_1_1_0_available(tmp_path)
    _patch_sources(
        monkeypatch,
        # Cached: written before 1.1.0 was published.
        pypi={"1.0.0": ("2025-12-01", False)},
        github=["1.0.0"],
        # Live: 1.1.0 is there.
        pypi_fresh={
            "1.1.0": ("2026-02-10", False),
            "1.0.0": ("2025-12-01", False),
        },
        github_fresh=["1.1.0", "1.0.0"],
    )

    with caplog.at_level(logging.WARNING):
        lint_changelog_dates(path, fix=fix)
    result = path.read_text(encoding="UTF-8")

    # The section keeps its links, and gains no contradicting warning.
    assert NOT_AVAILABLE_VERB not in result
    assert "[🐍 PyPI](https://pypi.org/project/my-package/1.1.0/)" in result
    assert "[🐙 GitHub](https://github.com/user/repo/releases/tag/v1.1.0)" in result
    # The read-only symptom: a published version reported as missing. Nothing
    # is written without `--fix`, so this is what the gate fixes there.
    assert "1.1.0: not found on PyPI" not in caplog.text


def test_lint_fix_retracts_availability_confirmed_missing(tmp_path, monkeypatch):
    """A live-confirmed absence still retracts, so the gate is not a veto.

    The counterpart to
    {func}`test_lint_keeps_availability_when_only_the_cache_is_stale`: when the
    forced refresh agrees the release is gone (a deleted GitHub release, a
    removed PyPI file), rewriting the section is the correct repair.
    """
    path = _changelog_claiming_1_1_0_available(tmp_path)
    # Cached and live agree: 1.1.0 is not published anywhere.
    _patch_sources(
        monkeypatch,
        pypi={"1.0.0": ("2025-12-01", False)},
        github=["1.0.0"],
    )

    lint_changelog_dates(path, fix=True)
    result = path.read_text(encoding="UTF-8")

    assert f"`1.1.0` {NOT_AVAILABLE_VERB} 🐍 PyPI and 🐙 GitHub." in result
    assert "[🐍 PyPI](https://pypi.org/project/my-package/1.1.0/)" not in result


def test_lint_fix_refuses_when_confirming_github_lookup_fails(tmp_path, monkeypatch):
    """An unreachable API during confirmation must not license a retraction."""
    path = _changelog_claiming_1_1_0_available(tmp_path)

    def github(repo_url, *, force_refresh=False):
        if force_refresh:
            raise GitHubReleasesUnavailable("API unreachable")
        return {"1.0.0": GitHubRelease(date="2025-12-01", body="")}

    _patch_sources(
        monkeypatch,
        pypi={"1.0.0": ("2025-12-01", False)},
        github=github,
    )
    before = path.read_text(encoding="UTF-8")

    assert lint_changelog_dates(path, fix=True) == 2
    assert path.read_text(encoding="UTF-8") == before
    assert AVAILABLE_VERB in before


def test_extract_all_version_headings():
    """Test that all versions (released and unreleased) are extracted."""
    changelog = Changelog(MULTI_RELEASE_CHANGELOG)
    headings = changelog.extract_all_version_headings()

    assert headings == {"2.0.0", "1.1.0", "1.0.0"}


def test_extract_all_version_headings_empty():
    """Test extraction from an empty changelog."""
    changelog = Changelog("# Changelog\n")
    assert changelog.extract_all_version_headings() == set()


def test_insert_version_section():
    """Test inserting a placeholder section for a missing version."""
    changelog = Changelog(MULTI_RELEASE_CHANGELOG)
    all_versions = ["2.0.0", "1.1.0", "1.0.5", "1.0.0"]
    result = changelog.insert_version_section(
        "1.0.5", "2026-01-15", "https://github.com/user/repo", all_versions
    )

    assert result is True
    assert "## [`1.0.5` (2026-01-15)]" in changelog.content
    assert "compare/v1.0.0...v1.0.5" in changelog.content
    # The next-higher version (1.1.0) should now point to 1.0.5.
    assert "compare/v1.0.5...v1.1.0" in changelog.content


def test_insert_version_section_idempotent():
    """Test that inserting an already-present version is a no-op."""
    changelog = Changelog(MULTI_RELEASE_CHANGELOG)
    all_versions = ["2.0.0", "1.1.0", "1.0.0"]
    result = changelog.insert_version_section(
        "1.1.0", "2026-02-10", "https://github.com/user/repo", all_versions
    )

    assert result is False


def test_insert_version_section_at_end():
    """Test inserting a version older than all existing ones."""
    changelog = Changelog(MULTI_RELEASE_CHANGELOG)
    all_versions = ["2.0.0", "1.1.0", "1.0.0", "0.5.0"]
    result = changelog.insert_version_section(
        "0.5.0", "2025-06-01", "https://github.com/user/repo", all_versions
    )

    assert result is True
    assert "## [`0.5.0` (2025-06-01)]" in changelog.content
    # Should be at the end, with comparison base v0.0.0 (no lower version).
    assert "compare/v0.0.0...v0.5.0" in changelog.content
    # 1.0.0 should now point to 0.5.0.
    assert "compare/v0.5.0...v1.0.0" in changelog.content


@pytest.mark.parametrize(
    ("version", "new_base", "expected", "present", "absent"),
    [
        pytest.param(
            "1.1.0",
            "1.0.5",
            True,
            "compare/v1.0.5...v1.1.0",
            "compare/v1.0.0...v1.1.0",
            id="replaces-base",
        ),
        pytest.param(
            "9.9.9",
            "1.0.0",
            False,
            "compare/v1.0.0...v1.1.0",
            None,
            id="no-match",
        ),
    ],
)
def test_update_comparison_base(version, new_base, expected, present, absent):
    """Test replacing the comparison base in a version heading."""
    changelog = Changelog(MULTI_RELEASE_CHANGELOG)
    result = changelog.update_comparison_base(version, new_base)

    assert result is expected
    assert present in changelog.content
    if absent:
        assert absent not in changelog.content


def test_lint_orphan_detection_returns_1(changelog_file, monkeypatch, caplog):
    """Test that an orphaned version causes lint to return 1."""
    _patch_sources(
        monkeypatch,
        pypi={"1.1.0": ("2026-02-10", False), "1.0.0": ("2025-12-01", False)},
        github=["1.1.0", "1.0.0"],
        tags={"1.0.5": "2026-01-15"},
    )
    # Tag for 1.0.5 exists but has no changelog entry.

    with caplog.at_level(logging.WARNING):
        assert lint_changelog_dates(changelog_file) == 1

    assert "1.0.5: found in external sources" in caplog.text


def test_lint_orphan_fix_inserts_placeholder(changelog_file, monkeypatch):
    """Test that --fix inserts placeholder sections for orphaned versions."""
    _patch_sources(
        monkeypatch,
        pypi={"1.1.0": ("2026-02-10", False), "1.0.0": ("2025-12-01", False)},
        github=["1.1.0", "1.0.0"],
        tags={"1.0.5": "2026-01-15"},
    )
    # Orphan: 1.0.5 exists as a tag but has no changelog entry.

    result = lint_changelog_dates(changelog_file, fix=True)
    assert result == 0

    content = changelog_file.read_text(encoding="UTF-8")
    assert "## [`1.0.5` (2026-01-15)]" in content
    assert "compare/v1.0.0...v1.0.5" in content
    # 1.1.0 comparison URL should now point to 1.0.5.
    assert "compare/v1.0.5...v1.1.0" in content


def test_lint_orphan_fix_idempotent(changelog_file, monkeypatch):
    """Test that running --fix with orphans twice produces the same result."""
    _patch_sources(
        monkeypatch,
        pypi={"1.1.0": ("2026-02-10", False), "1.0.0": ("2025-12-01", False)},
        github=["1.1.0", "1.0.0"],
        tags={"1.0.5": "2026-01-15"},
    )

    lint_changelog_dates(changelog_file, fix=True)
    first_content = changelog_file.read_text(encoding="UTF-8")

    lint_changelog_dates(changelog_file, fix=True)
    second_content = changelog_file.read_text(encoding="UTF-8")

    assert first_content == second_content


def test_lint_orphan_tag_only(changelog_file, monkeypatch, caplog):
    """Test orphan detected from git tag only (not on PyPI or GitHub)."""
    _patch_sources(
        monkeypatch,
        pypi={"1.1.0": ("2026-02-10", False), "1.0.0": ("2025-12-01", False)},
        github=["1.1.0", "1.0.0"],
        tags={"1.0.5": "2026-01-15"},
    )

    with caplog.at_level(logging.WARNING):
        assert lint_changelog_dates(changelog_file) == 1

    assert "1.0.5" in caplog.text


def test_lint_orphan_uses_pypi_date(changelog_file, monkeypatch):
    """Test that orphan fix prefers PyPI date over GitHub and tag dates."""
    _patch_sources(
        monkeypatch,
        pypi={
            "1.1.0": ("2026-02-10", False),
            "1.0.5": ("2026-01-20", False),
            "1.0.0": ("2025-12-01", False),
        },
        github={"1.1.0": "2026-02-10", "1.0.5": "2026-01-18", "1.0.0": "2025-12-01"},
        tags={"1.0.5": "2026-01-15"},
    )

    lint_changelog_dates(changelog_file, fix=True)
    content = changelog_file.read_text(encoding="UTF-8")

    # Should use PyPI date (2026-01-20), not GitHub (2026-01-18) or tag (2026-01-15).
    assert "## [`1.0.5` (2026-01-20)]" in content


def test_lint_unreleased_not_flagged_as_orphan(changelog_file, monkeypatch):
    """Test that the unreleased dev version is not flagged as orphan."""
    _patch_sources(
        monkeypatch,
        pypi={"1.1.0": ("2026-02-10", False), "1.0.0": ("2025-12-01", False)},
        github=["1.1.0", "1.0.0"],
    )

    # 2.0.0 is the unreleased version in MULTI_RELEASE_CHANGELOG.
    # It should not be flagged as orphan.
    assert lint_changelog_dates(changelog_file) == 0


RENAME_CHANGELOG = dedent(
    """\
    # Changelog

    ## [2.0.0 (unreleased)](https://github.com/user/repo/compare/v1.1.0...main)

    > [!WARNING]
    > This version is **not released yet** and is under active development.

    ## [1.1.0 (2026-02-10)](https://github.com/user/repo/compare/v1.0.0...v1.1.0)

    - New release under current name.

    ## [1.0.0 (2025-12-01)](https://github.com/user/repo/compare/v0.9.0...v1.0.0)

    - Release under old name.
    """
)
"""Changelog fixture for package rename tests."""


def test_lint_fix_pypi_package_history(tmp_path, monkeypatch):
    """Versions from former package names get correct PyPI URLs."""
    path = tmp_path / "changelog.md"
    path.write_text(RENAME_CHANGELOG, encoding="UTF-8")

    # Current package has only 1.1.0.
    _patch_sources(
        monkeypatch,
        pypi=lambda pkg, *, force_refresh=False: {
            "new-pkg": {
                "1.1.0": PyPIRelease(date="2026-02-10", yanked=False, package="new-pkg")
            },
            "old-pkg": {
                "1.0.0": PyPIRelease(date="2025-12-01", yanked=False, package="old-pkg")
            },
        }.get(pkg, {}),
        package="new-pkg",
        github=["1.1.0", "1.0.0"],
    )

    lint_changelog_dates(
        path,
        package="new-pkg",
        fix=True,
        pypi_package_history=["old-pkg"],
    )
    content = path.read_text(encoding="UTF-8")

    # 1.1.0 should link to new-pkg on PyPI.
    assert "pypi.org/project/new-pkg/1.1.0/" in content
    # 1.0.0 should link to old-pkg on PyPI.
    assert "pypi.org/project/old-pkg/1.0.0/" in content


def test_lint_pypi_history_current_wins(tmp_path, monkeypatch):
    """Current package name wins when a version exists under both names."""
    path = tmp_path / "changelog.md"
    path.write_text(RENAME_CHANGELOG, encoding="UTF-8")

    # Version 1.0.0 exists under both current and former names.
    _patch_sources(
        monkeypatch,
        pypi=lambda pkg, *, force_refresh=False: {
            "new-pkg": {
                "1.1.0": PyPIRelease(
                    date="2026-02-10", yanked=False, package="new-pkg"
                ),
                "1.0.0": PyPIRelease(
                    date="2025-12-01", yanked=False, package="new-pkg"
                ),
            },
            "old-pkg": {
                "1.0.0": PyPIRelease(date="2025-11-30", yanked=False, package="old-pkg")
            },
        }.get(pkg, {}),
        package="new-pkg",
        github=["1.1.0", "1.0.0"],
    )

    lint_changelog_dates(
        path,
        package="new-pkg",
        fix=True,
        pypi_package_history=["old-pkg"],
    )
    content = path.read_text(encoding="UTF-8")

    # Current package wins: 1.0.0 should link to new-pkg, not old-pkg.
    assert "pypi.org/project/new-pkg/1.0.0/" in content
    assert "pypi.org/project/old-pkg/1.0.0/" not in content


# ---------------------------------------------------------------------------
# Sanity gate: refuse to rewrite when upstream data looks empty/broken.
# ---------------------------------------------------------------------------


CHANGELOG_WITH_ADMONITIONS = dedent(
    """\
    # Changelog

    ## [`1.3.0` (2026-03-01)](https://github.com/user/repo/compare/v1.2.0...v1.3.0)

    > [!NOTE]
    > `1.3.0` is available on [🐍 PyPI](https://pypi.org/project/my-package/1.3.0/) and [🐙 GitHub](https://github.com/user/repo/releases/tag/v1.3.0).

    - Third release.

    ## [`1.2.0` (2026-02-01)](https://github.com/user/repo/compare/v1.1.0...v1.2.0)

    > [!NOTE]
    > `1.2.0` is available on [🐍 PyPI](https://pypi.org/project/my-package/1.2.0/) and [🐙 GitHub](https://github.com/user/repo/releases/tag/v1.2.0).

    - Second release.

    ## [`1.1.0` (2026-01-01)](https://github.com/user/repo/compare/v1.0.0...v1.1.0)

    > [!NOTE]
    > `1.1.0` is available on [🐍 PyPI](https://pypi.org/project/my-package/1.1.0/) and [🐙 GitHub](https://github.com/user/repo/releases/tag/v1.1.0).

    - Minor release.

    ## [`1.0.0` (2025-12-01)](https://github.com/user/repo/compare/v0.9.0...v1.0.0)

    > [!NOTE]
    > `1.0.0` is the *first version* available on [🐍 PyPI](https://pypi.org/project/my-package/1.0.0/) and [🐙 GitHub](https://github.com/user/repo/releases/tag/v1.0.0).

    - Initial release.
    """
)
"""Changelog with four releases, each carrying both PyPI and GitHub links.

Used by sanity-gate tests to assert that a destructive rewrite is refused
when an upstream lookup fails or returns no data.
"""


def test_lint_fix_refuses_rewrite_when_github_lookup_raises(tmp_path, monkeypatch):
    """The sanity gate refuses to strip every GitHub link when the API errors.

    This replays the kdeldycke/click-extra#1702 scenario: GitHub returns
    a transient error, the legacy code treated the empty dict as "no
    GitHub releases," and the rewrite stripped every `🐙 GitHub` link
    from the file. The gate exits non-zero before any write.
    """
    path = tmp_path / "changelog.md"
    path.write_text(CHANGELOG_WITH_ADMONITIONS, encoding="UTF-8")
    original = path.read_text(encoding="UTF-8")

    _patch_sources(
        monkeypatch,
        pypi={
            "1.3.0": ("2026-03-01", False),
            "1.2.0": ("2026-02-01", False),
            "1.1.0": ("2026-01-01", False),
            "1.0.0": ("2025-12-01", False),
        },
        github=_github_unavailable,
    )

    assert lint_changelog_dates(path, fix=True) == 2
    assert path.read_text(encoding="UTF-8") == original


def test_lint_fix_refuses_rewrite_when_pypi_empty_with_coverage(tmp_path, monkeypatch):
    """The sanity gate refuses when PyPI returns nothing but coverage exists.

    PyPI's client conflates `404` with all transient failures, so an
    empty result above the coverage threshold is treated as a failure
    fingerprint rather than a genuine "package no longer published"
    transition.
    """
    path = tmp_path / "changelog.md"
    path.write_text(CHANGELOG_WITH_ADMONITIONS, encoding="UTF-8")
    original = path.read_text(encoding="UTF-8")

    _patch_sources(monkeypatch, github=["1.3.0", "1.2.0", "1.1.0", "1.0.0"])
    assert lint_changelog_dates(path, fix=True) == 2
    assert path.read_text(encoding="UTF-8") == original


def test_lint_fix_proceeds_when_github_fails_but_no_existing_links(
    changelog_file, monkeypatch
):
    """No existing GitHub links → GitHub failure is not destructive, so proceed.

    This is the legitimate "new repo, GitHub API flaky" case: there's
    nothing in the changelog to lose, so the rewrite is a no-op on the
    GitHub side and can safely run.
    """
    _patch_sources(
        monkeypatch,
        pypi={"1.1.0": ("2026-02-10", False), "1.0.0": ("2025-12-01", False)},
        github=_github_unavailable,
    )

    assert lint_changelog_dates(changelog_file, fix=True) == 0
    # Rewrite still happened on the PyPI side.
    assert (
        "[🐍 PyPI](https://pypi.org/project/my-package/1.1.0/)"
        in changelog_file.read_text(encoding="UTF-8")
    )


def test_lint_fix_proceeds_when_pypi_empty_below_threshold(tmp_path, monkeypatch):
    """Empty PyPI fetch with < threshold existing PyPI links → proceed.

    A project that's never been on PyPI (or just migrated off) has at
    most a couple of PyPI links in its history. The threshold lets that
    legitimate case through while still catching the transient-failure
    fingerprint.
    """
    # Only one existing PyPI link, well below EMPTY_PYPI_SANITY_THRESHOLD.
    content = dedent(
        """\
        # Changelog

        ## [1.1.0 (2026-02-10)](https://github.com/user/repo/compare/v1.0.0...v1.1.0)

        > [!NOTE]
        > `1.1.0` is available on [🐍 PyPI](https://pypi.org/project/my-package/1.1.0/).

        - Second release.

        ## [1.0.0 (2025-12-01)](https://github.com/user/repo/compare/v0.9.0...v1.0.0)

        - Initial release.
        """
    )
    path = tmp_path / "changelog.md"
    path.write_text(content, encoding="UTF-8")

    _patch_sources(monkeypatch, tags={"v1.1.0": "2026-02-10", "v1.0.0": "2025-12-01"})

    # Below threshold: gate doesn't fire; falls back to git tag mode.
    assert lint_changelog_dates(path, fix=True) == 0


DEV_ONLY_CHANGELOG = dedent(
    """\
    # Changelog

    ## [`1.0.0.dev0` (unreleased)](https://github.com/user/repo/compare/v0.9.0...main)

    > [!WARNING]
    > This version is **not released yet** and is under active development.
    """
)
"""Changelog mid-development cycle, carrying no final-release heading."""


FROZEN_CHANGELOG = dedent(
    """\
    # Changelog

    ## [`1.2.3` (2026-03-01)](https://github.com/user/repo/compare/v1.2.2...v1.2.3)

    - Add papaya sorting.
    """
)
"""Changelog whose newest section is already frozen, ready for a post-release bump."""


def test_insert_version_section_orders_against_dev_headings():
    """A `.devN` heading takes part in the insertion-point scan.

    The published `1.0.0` outranks the `1.0.0.dev0` section still sitting in the
    changelog, so it belongs above it. A scan blind to development headings
    finds no lower version, falls through to the append-at-end branch, and files
    the newest release at the bottom.
    """
    changelog = Changelog(DEV_ONLY_CHANGELOG)
    result = changelog.insert_version_section(
        "1.0.0", "2026-03-01", "https://github.com/user/repo", ["1.0.0.dev0", "1.0.0"]
    )

    assert result is True
    released_at = changelog.content.index("## [`1.0.0` (2026-03-01)]")
    development_at = changelog.content.index("## [`1.0.0.dev0` (unreleased)]")
    assert released_at < development_at


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        pytest.param({}, "compare/v1.2.3...main", id="default-branch"),
        pytest.param(
            {"default_branch": "trunk"}, "compare/v1.2.3...trunk", id="custom-branch"
        ),
    ],
)
def test_update_retargets_comparison_url_to_default_branch(kwargs, expected):
    """The new unreleased entry points at the branch the caller names.

    `freeze` retargets this same URL back to a tag, so a hard-coded `main` here
    leaves any repository on another default branch unable to round-trip.
    """
    changelog = Changelog(FROZEN_CHANGELOG, current_version="1.2.3")
    assert expected in changelog.update(**kwargs)


def test_freeze_warns_when_section_is_missing(caplog):
    """A version with no changelog section warns instead of quietly no-opping."""
    changelog = Changelog(MULTI_RELEASE_CHANGELOG, current_version="9.9.9")
    with caplog.at_level(logging.WARNING):
        assert changelog.freeze(release_date="2026-03-01") is False
    assert "No changelog section found for version 9.9.9" in caplog.text


def test_freeze_already_frozen_stays_silent(caplog):
    """The idempotent no-op path must not warn, or every re-run cries wolf."""
    changelog = Changelog(MULTI_RELEASE_CHANGELOG, current_version="1.1.0")
    with caplog.at_level(logging.WARNING):
        assert changelog.freeze(release_date="2026-03-01") is False
    assert caplog.records == []


def test_lint_fix_fails_when_orphans_cannot_be_inserted(tmp_path, monkeypatch):
    """A date fix must not mask an orphan the run was unable to insert.

    Orphan insertion needs a repository URL to build comparison links from.
    Without one the orphan stays missing, so reporting success on the strength
    of an unrelated date correction would publish the gap.
    """
    path = tmp_path / "changelog.md"
    path.write_text(
        dedent(
            """\
            # Changelog

            ## [`1.0.0` (2020-01-01)](https://example.com/notes)

            - Initial release.
            """
        ),
        encoding="UTF-8",
    )

    _patch_sources(
        monkeypatch,
        pypi={"2.0.0": ("2026-02-10", False), "1.0.0": ("2025-12-01", False)},
    )

    assert lint_changelog_dates(path, fix=True) == 1
    result = path.read_text(encoding="UTF-8")
    # The date it *could* fix was fixed, and the orphan it could not is absent.
    assert "(2025-12-01)" in result
    assert "2.0.0" not in result


def test_lint_fix_skips_orphan_without_a_release_date(
    changelog_file, monkeypatch, caplog
):
    """An orphan no source can date is reported, not stamped with a placeholder.

    A placeholder date satisfies the released-heading pattern, so it would be
    re-checked against the reference source on every later run and mismatch
    forever.
    """
    _patch_sources(
        monkeypatch,
        pypi={"1.1.0": ("2026-02-10", False), "1.0.0": ("2025-12-01", False)},
        tags={"3.0.0": ""},
    )
    # A tag with no resolvable date: nothing truthful to write a heading with.

    with caplog.at_level(logging.WARNING):
        assert lint_changelog_dates(changelog_file, fix=True) == 1
    assert "3.0.0: no release date found in any source" in caplog.text
    assert "0000-00-00" not in changelog_file.read_text(encoding="UTF-8")


@pytest.mark.once
def test_rendered_sections_are_an_mdformat_fixed_point(tmp_path, monkeypatch):
    """Every section this module renders must survive `format-markdown` intact.

    `fix-changelog` writes `changelog.md` and `format-markdown` reformats the
    same file on the same push. When the two disagree on the canonical layout
    they ping-pong: one job rewrites the section, the next reformats it, each
    opening its own PR, and neither converges. See `claude.md` §
    "Generator/formatter ping-pong is recurrent".

    Both renderings are exercised, because they fail differently. The inserted
    placeholder leaves every optional admonition slot of the `release-notes`
    template empty, which is where stray blank-line runs collect; the
    regenerated section fills the availability slot with a GFM alert, which
    only `mdformat_gfm_alerts` knows how to reflow.

    Verified through the write path rather than `mdformat --check`, whose
    verdict is unreliable for a tool carrying a `post_process` fixup (see
    {func}`~repomatic.tooling.tool_runner.verify_via_write_path`).

    Marked `once`: it resolves mdformat and its fifteen plugins through uvx, so
    one runner suffices.
    """
    # mdformat has no --config flag, so `run_tool` stages one in the working
    # directory; chdir keeps that write, and verify_via_write_path's scratch
    # copies, inside this test's tmp_path rather than the repository root.
    monkeypatch.chdir(tmp_path)
    skip_unless_tool_runs("mdformat")

    changelog = Changelog(SAMPLE_CHANGELOG)
    changelog.insert_version_section(
        "1.2.1",
        "2023-11-02",
        "https://github.com/user/repo",
        ["1.2.3", "1.2.2", "1.2.1"],
    )
    # The composition fix-changelog performs: a NOTE for the platforms carrying
    # the version, a WARNING for the one that is missing it.
    elements = changelog.decompose_version("1.2.2")
    elements.availability_admonition = "\n\n".join((
        build_release_admonition(
            "1.2.2", pypi_url="https://pypi.org/project/papaya/1.2.2/"
        ),
        build_unavailable_admonition("1.2.2", missing_github=True),
    ))
    assert changelog.replace_section(
        "1.2.2", render_template("release-notes", **asdict(elements))
    )

    target = tmp_path / "changelog.md"
    target.write_text(changelog.content.rstrip() + "\n", encoding="UTF-8")

    _, drifted = verify_via_write_path("mdformat", extra_args=(str(target),))

    assert not drifted, (
        "mdformat rewrites a freshly rendered changelog section, so "
        "fix-changelog and format-markdown will ping-pong on every push. "
        "Align the templates in repomatic/templates/ with mdformat's output "
        f"rather than reformatting changelog.md by hand. Rendered:\n"
        f"{target.read_text(encoding='UTF-8')}"
    )
