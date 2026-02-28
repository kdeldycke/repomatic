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

import logging
from textwrap import dedent

import pytest

from repomatic.changelog import (
    Changelog,
    PyPIRelease,
    VersionElements,
    build_unavailable_admonition,
    lint_changelog_dates,
)
from repomatic.github.releases import GitHubRelease


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
            {"changes": "- New feature."},
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
            {"development_warning": ""},
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


def test_decompose_version_preserves_hand_written_admonitions():
    """Custom admonitions (e.g. [!TIP]) stay in changes."""
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

    assert "[!TIP]" in elements.changes
    assert "hand-written tip" in elements.changes
    assert "- Initial release." in elements.changes
    assert elements.availability_admonition == ""


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


def _pypi_mock(releases, package="my-package"):
    """Build a monkeypatch-compatible mock for ``get_pypi_release_dates``.

    Each value in *releases* is a ``(date, yanked)`` tuple. The *package*
    argument is injected as the third ``PyPIRelease`` field so callers
    don't need to repeat it in every entry.
    """
    return lambda pkg: {
        v: PyPIRelease(date=args[0], yanked=args[1], package=package)
        for v, args in releases.items()
    }


def _github_mock(versions):
    """Build a monkeypatch-compatible mock for ``get_github_releases``.

    Accepts either a list of version strings (uses a dummy date) or a
    dict mapping version strings to date strings.
    """
    if isinstance(versions, dict):
        return lambda repo_url: {
            v: GitHubRelease(date=d, body="") for v, d in versions.items()
        }
    return lambda repo_url: {
        v: GitHubRelease(date="2026-01-01", body="") for v in versions
    }


def _tags_mock(tags=None):
    """Build a monkeypatch-compatible mock for ``get_all_version_tags``."""
    return lambda: tags if tags is not None else {}


def _patch_tags(monkeypatch, tags=None):
    """Monkeypatch ``get_all_version_tags`` to return the given dict."""
    monkeypatch.setattr(
        "repomatic.git_ops.get_all_version_tags",
        _tags_mock(tags),
    )


def test_lint_changelog_dates_pypi_all_match(tmp_path, monkeypatch):
    """Test that lint returns 0 when all PyPI dates match."""
    path = tmp_path / "changelog.md"
    path.write_text(MULTI_RELEASE_CHANGELOG, encoding="UTF-8")

    monkeypatch.setattr(
        "repomatic.changelog.get_pypi_release_dates",
        _pypi_mock({
            "1.1.0": ("2026-02-10", False),
            "1.0.0": ("2025-12-01", False),
        }),
    )
    monkeypatch.setattr(
        "repomatic.metadata.get_project_name",
        lambda: "my-package",
    )
    monkeypatch.setattr(
        "repomatic.changelog.get_github_releases",
        _github_mock(["1.1.0", "1.0.0"]),
    )
    _patch_tags(monkeypatch)

    assert lint_changelog_dates(path) == 0


def test_lint_changelog_dates_pypi_mismatch(tmp_path, monkeypatch):
    """Test that lint returns 1 when a PyPI date differs."""
    path = tmp_path / "changelog.md"
    path.write_text(MULTI_RELEASE_CHANGELOG, encoding="UTF-8")

    monkeypatch.setattr(
        "repomatic.changelog.get_pypi_release_dates",
        _pypi_mock({
            "1.1.0": ("2026-02-09", False),
            "1.0.0": ("2025-12-01", False),
        }),
    )
    monkeypatch.setattr(
        "repomatic.metadata.get_project_name",
        lambda: "my-package",
    )
    monkeypatch.setattr(
        "repomatic.changelog.get_github_releases",
        _github_mock(["1.1.0", "1.0.0"]),
    )
    _patch_tags(monkeypatch)

    assert lint_changelog_dates(path) == 1


def test_lint_changelog_dates_fallback_to_tags(tmp_path, monkeypatch):
    """Test that lint falls back to git tags when not on PyPI."""
    path = tmp_path / "changelog.md"
    path.write_text(MULTI_RELEASE_CHANGELOG, encoding="UTF-8")

    # PyPI returns empty dict (not published).
    monkeypatch.setattr(
        "repomatic.changelog.get_pypi_release_dates",
        lambda pkg: {},
    )
    monkeypatch.setattr(
        "repomatic.metadata.get_project_name",
        lambda: "my-package",
    )
    monkeypatch.setattr(
        "repomatic.git_ops.get_tag_date",
        lambda tag: {"v1.1.0": "2026-02-10", "v1.0.0": "2025-12-01"}.get(tag),
    )
    monkeypatch.setattr(
        "repomatic.changelog.get_github_releases",
        _github_mock([]),
    )
    _patch_tags(monkeypatch)

    assert lint_changelog_dates(path) == 0


def test_lint_changelog_dates_fallback_no_package(tmp_path, monkeypatch):
    """Test that lint falls back to git tags when no package name is detected."""
    path = tmp_path / "changelog.md"
    path.write_text(MULTI_RELEASE_CHANGELOG, encoding="UTF-8")

    monkeypatch.setattr(
        "repomatic.metadata.get_project_name",
        lambda: None,
    )
    monkeypatch.setattr(
        "repomatic.git_ops.get_tag_date",
        lambda tag: {"v1.1.0": "2026-02-10", "v1.0.0": "2025-12-01"}.get(tag),
    )
    monkeypatch.setattr(
        "repomatic.changelog.get_github_releases",
        _github_mock([]),
    )
    _patch_tags(monkeypatch)

    assert lint_changelog_dates(path) == 0


def test_lint_changelog_dates_warns_missing_pypi(tmp_path, monkeypatch, caplog):
    """Test that versions not on PyPI get a warning if they postdate the first
    PyPI release, and an info log if they predate it."""
    path = tmp_path / "changelog.md"
    path.write_text(MULTI_RELEASE_CHANGELOG, encoding="UTF-8")

    # Only the oldest version is on PyPI; 1.1.0 is an unexpected gap.
    monkeypatch.setattr(
        "repomatic.changelog.get_pypi_release_dates",
        _pypi_mock({"1.0.0": ("2025-12-01", False)}),
    )
    monkeypatch.setattr(
        "repomatic.metadata.get_project_name",
        lambda: "my-package",
    )
    monkeypatch.setattr(
        "repomatic.changelog.get_github_releases",
        _github_mock([]),
    )
    _patch_tags(monkeypatch)

    with caplog.at_level(logging.WARNING):
        # Should return 0: 1.0.0 matches, 1.1.0 warned but non-fatal.
        assert lint_changelog_dates(path) == 0

    assert "1.1.0: not found on PyPI" in caplog.text


def test_lint_changelog_dates_skips_pre_pypi(tmp_path, monkeypatch, caplog):
    """Test that versions older than the first PyPI release are skipped."""
    path = tmp_path / "changelog.md"
    path.write_text(MULTI_RELEASE_CHANGELOG, encoding="UTF-8")

    # Only the newest version is on PyPI; 1.0.0 predates publication.
    monkeypatch.setattr(
        "repomatic.changelog.get_pypi_release_dates",
        _pypi_mock({"1.1.0": ("2026-02-10", False)}),
    )
    monkeypatch.setattr(
        "repomatic.metadata.get_project_name",
        lambda: "my-package",
    )
    monkeypatch.setattr(
        "repomatic.changelog.get_github_releases",
        _github_mock([]),
    )
    _patch_tags(monkeypatch)

    with caplog.at_level(logging.INFO):
        assert lint_changelog_dates(path) == 0

    assert "predates PyPI" in caplog.text


def test_lint_fix_corrects_date(tmp_path, monkeypatch):
    """Test that --fix corrects mismatched dates in the changelog."""
    path = tmp_path / "changelog.md"
    path.write_text(MULTI_RELEASE_CHANGELOG, encoding="UTF-8")

    monkeypatch.setattr(
        "repomatic.changelog.get_pypi_release_dates",
        _pypi_mock({
            "1.1.0": ("2026-02-11", False),
            "1.0.0": ("2025-12-01", False),
        }),
    )
    monkeypatch.setattr(
        "repomatic.metadata.get_project_name",
        lambda: "my-package",
    )
    monkeypatch.setattr(
        "repomatic.changelog.get_github_releases",
        _github_mock(["1.1.0", "1.0.0"]),
    )
    _patch_tags(monkeypatch)

    # Mismatch on 1.1.0: changelog says 2026-02-10, PyPI says 2026-02-11.
    # fix=True corrects it in-place, so return 0 to let downstream steps proceed.
    result = lint_changelog_dates(path, fix=True)
    assert result == 0

    content = path.read_text(encoding="UTF-8")
    assert "(2026-02-11)" in content
    assert "(2026-02-10)" not in content


def test_lint_fix_adds_release_admonition(tmp_path, monkeypatch):
    """Test that --fix adds conditional release admonitions."""
    path = tmp_path / "changelog.md"
    path.write_text(MULTI_RELEASE_CHANGELOG, encoding="UTF-8")

    monkeypatch.setattr(
        "repomatic.changelog.get_pypi_release_dates",
        _pypi_mock({
            "1.1.0": ("2026-02-10", False),
            "1.0.0": ("2025-12-01", False),
        }),
    )
    monkeypatch.setattr(
        "repomatic.metadata.get_project_name",
        lambda: "my-package",
    )
    monkeypatch.setattr(
        "repomatic.changelog.get_github_releases",
        _github_mock(["1.1.0", "1.0.0"]),
    )
    _patch_tags(monkeypatch)

    lint_changelog_dates(path, fix=True)
    content = path.read_text(encoding="UTF-8")

    # Both sources available — NOTE only, no WARNINGs.
    assert "[🐍 PyPI](https://pypi.org/project/my-package/1.1.0/)" in content
    assert "[🐙 GitHub](https://github.com/user/repo/releases/tag/v1.1.0)" in content
    assert "is **not available** on" not in content
    # 1.0.0 is the first on both platforms — "first version" wording.
    assert "`1.0.0` is the *first version* available on" in content
    # 1.1.0 is not the first on either — normal wording.
    assert "`1.1.0` is available on" in content


def test_lint_fix_github_only(tmp_path, monkeypatch):
    """Test that --fix adds GitHub-only admonition when not on PyPI."""
    path = tmp_path / "changelog.md"
    path.write_text(MULTI_RELEASE_CHANGELOG, encoding="UTF-8")

    # 1.0.0 on PyPI, 1.1.0 only on GitHub (not on PyPI).
    monkeypatch.setattr(
        "repomatic.changelog.get_pypi_release_dates",
        _pypi_mock({"1.0.0": ("2025-12-01", False)}),
    )
    monkeypatch.setattr(
        "repomatic.metadata.get_project_name",
        lambda: "my-package",
    )
    monkeypatch.setattr(
        "repomatic.changelog.get_github_releases",
        _github_mock(["1.1.0", "1.0.0"]),
    )
    _patch_tags(monkeypatch)

    lint_changelog_dates(path, fix=True)
    content = path.read_text(encoding="UTF-8")

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
    monkeypatch.setattr(
        "repomatic.changelog.get_pypi_release_dates",
        _pypi_mock({
            "2.0.0": ("2026-02-10", False),
            "1.0.0": ("2025-12-01", False),
        }),
    )
    monkeypatch.setattr(
        "repomatic.metadata.get_project_name",
        lambda: "my-package",
    )
    monkeypatch.setattr(
        "repomatic.changelog.get_github_releases",
        _github_mock(["2.0.0", "1.0.0", "0.5.0"]),
    )
    _patch_tags(monkeypatch)

    lint_changelog_dates(path, fix=True)
    content = path.read_text(encoding="UTF-8")

    # 0.5.0: GitHub only, first on GitHub — "first version" wording.
    assert "`0.5.0` is the *first version* available on" in content
    assert "[🐙 GitHub](https://github.com/user/repo/releases/tag/v0.5.0)" in content
    # 1.0.0: first on PyPI but not first on GitHub — normal wording.
    assert "`1.0.0` is available on" in content
    # 2.0.0: not first on either — normal wording.
    assert "`2.0.0` is available on" in content


def test_lint_fix_pypi_only(tmp_path, monkeypatch):
    """Test that --fix adds PyPI NOTE and GitHub WARNING when only on PyPI."""
    path = tmp_path / "changelog.md"
    path.write_text(MULTI_RELEASE_CHANGELOG, encoding="UTF-8")

    # 1.1.0 on PyPI only, not on GitHub.
    monkeypatch.setattr(
        "repomatic.changelog.get_pypi_release_dates",
        _pypi_mock({
            "1.1.0": ("2026-02-10", False),
            "1.0.0": ("2025-12-01", False),
        }),
    )
    monkeypatch.setattr(
        "repomatic.metadata.get_project_name",
        lambda: "my-package",
    )
    monkeypatch.setattr(
        "repomatic.changelog.get_github_releases",
        _github_mock(["1.0.0"]),
    )
    _patch_tags(monkeypatch)

    lint_changelog_dates(path, fix=True)
    content = path.read_text(encoding="UTF-8")

    # 1.1.0: PyPI NOTE, GitHub WARNING.
    assert "[🐍 PyPI](https://pypi.org/project/my-package/1.1.0/)" in content
    assert "is **not available** on 🐙 GitHub." in content
    assert "releases/tag/v1.1.0" not in content
    # 1.0.0: both sources.
    assert "[🐍 PyPI](https://pypi.org/project/my-package/1.0.0/)" in content
    assert "[🐙 GitHub](https://github.com/user/repo/releases/tag/v1.0.0)" in content


def test_lint_fix_no_warning_predates_github(tmp_path, monkeypatch):
    """Test that --fix skips GitHub WARNING for versions predating the first
    GitHub release."""
    path = tmp_path / "changelog.md"
    path.write_text(MULTI_RELEASE_CHANGELOG, encoding="UTF-8")

    # Both on PyPI; only 1.1.0 on GitHub (1.0.0 predates GitHub releases).
    monkeypatch.setattr(
        "repomatic.changelog.get_pypi_release_dates",
        _pypi_mock({
            "1.1.0": ("2026-02-10", False),
            "1.0.0": ("2025-12-01", False),
        }),
    )
    monkeypatch.setattr(
        "repomatic.metadata.get_project_name",
        lambda: "my-package",
    )
    monkeypatch.setattr(
        "repomatic.changelog.get_github_releases",
        _github_mock(["1.1.0"]),
    )
    _patch_tags(monkeypatch)

    lint_changelog_dates(path, fix=True)
    content = path.read_text(encoding="UTF-8")

    # 1.0.0 predates first GitHub release (1.1.0) — no GitHub WARNING.
    assert "is **not available** on" not in content
    # 1.0.0: PyPI NOTE only (no GitHub link, no warning).
    assert "[🐍 PyPI](https://pypi.org/project/my-package/1.0.0/)" in content
    # 1.1.0: both sources.
    assert "[🐍 PyPI](https://pypi.org/project/my-package/1.1.0/)" in content
    assert "[🐙 GitHub](https://github.com/user/repo/releases/tag/v1.1.0)" in content


def test_lint_fix_adds_yanked_admonition(tmp_path, monkeypatch):
    """Test that --fix adds a CAUTION admonition for yanked releases."""
    path = tmp_path / "changelog.md"
    path.write_text(MULTI_RELEASE_CHANGELOG, encoding="UTF-8")

    monkeypatch.setattr(
        "repomatic.changelog.get_pypi_release_dates",
        _pypi_mock({
            "1.1.0": ("2026-02-10", True),
            "1.0.0": ("2025-12-01", False),
        }),
    )
    monkeypatch.setattr(
        "repomatic.metadata.get_project_name",
        lambda: "my-package",
    )
    monkeypatch.setattr(
        "repomatic.changelog.get_github_releases",
        _github_mock(["1.1.0", "1.0.0"]),
    )
    _patch_tags(monkeypatch)

    lint_changelog_dates(path, fix=True)
    content = path.read_text(encoding="UTF-8")

    # Yanked CAUTION links to the specific PyPI project page.
    assert (
        "`1.1.0` has been [yanked from PyPI]"
        "(https://pypi.org/project/my-package/1.1.0/)"
    ) in content
    # NOTE should show GitHub only, not PyPI (yanked release excluded).
    assert "[🐍 PyPI](https://pypi.org/project/my-package/1.1.0/)" not in content
    assert "[🐙 GitHub](https://github.com/user/repo/releases/tag/v1.1.0)" in content


def test_lint_fix_no_admonition_when_nowhere(tmp_path, monkeypatch):
    """Test that --fix adds WARNINGs for both platforms when version is on
    neither PyPI nor GitHub."""
    path = tmp_path / "changelog.md"
    path.write_text(MULTI_RELEASE_CHANGELOG, encoding="UTF-8")

    # 1.0.0 on PyPI; 1.1.0 on neither.
    monkeypatch.setattr(
        "repomatic.changelog.get_pypi_release_dates",
        _pypi_mock({"1.0.0": ("2025-12-01", False)}),
    )
    monkeypatch.setattr(
        "repomatic.metadata.get_project_name",
        lambda: "my-package",
    )
    monkeypatch.setattr(
        "repomatic.changelog.get_github_releases",
        _github_mock(["1.0.0"]),
    )
    _patch_tags(monkeypatch)

    lint_changelog_dates(path, fix=True)
    content = path.read_text(encoding="UTF-8")

    # 1.1.0: WARNING listing both missing platforms.
    assert ("is **not available** on 🐍 PyPI and 🐙 GitHub.") in content
    assert "releases/tag/v1.1.0" not in content
    assert "my-package/1.1.0" not in content
    # 1.0.0 has both.
    assert "[🐍 PyPI](https://pypi.org/project/my-package/1.0.0/)" in content


def test_lint_fix_idempotent(tmp_path, monkeypatch):
    """Test that running --fix twice produces the same result."""
    path = tmp_path / "changelog.md"
    path.write_text(MULTI_RELEASE_CHANGELOG, encoding="UTF-8")

    mock = _pypi_mock({
        "1.1.0": ("2026-02-10", False),
        "1.0.0": ("2025-12-01", False),
    })
    monkeypatch.setattr("repomatic.changelog.get_pypi_release_dates", mock)
    monkeypatch.setattr(
        "repomatic.metadata.get_project_name",
        lambda: "my-package",
    )
    monkeypatch.setattr(
        "repomatic.changelog.get_github_releases",
        _github_mock(["1.1.0", "1.0.0"]),
    )
    _patch_tags(monkeypatch)

    lint_changelog_dates(path, fix=True)
    first_content = path.read_text(encoding="UTF-8")

    lint_changelog_dates(path, fix=True)
    second_content = path.read_text(encoding="UTF-8")

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
    monkeypatch.setattr(
        "repomatic.changelog.get_pypi_release_dates",
        _pypi_mock({
            "1.1.0": ("2026-02-10", False),
            "1.0.0": ("2025-12-01", False),
        }),
    )
    monkeypatch.setattr(
        "repomatic.metadata.get_project_name",
        lambda: "my-package",
    )
    monkeypatch.setattr(
        "repomatic.changelog.get_github_releases",
        _github_mock(["1.1.0", "1.0.0"]),
    )
    _patch_tags(monkeypatch)

    lint_changelog_dates(path, fix=True)
    result = path.read_text(encoding="UTF-8")

    assert "is **not available** on" not in result
    # NOTE admonitions should be present.
    assert "[🐍 PyPI](https://pypi.org/project/my-package/1.0.0/)" in result
    assert "[🐙 GitHub](https://github.com/user/repo/releases/tag/v1.0.0)" in result
    assert "Initial release." in result


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


def test_lint_orphan_detection_returns_1(tmp_path, monkeypatch, caplog):
    """Test that an orphaned version causes lint to return 1."""
    path = tmp_path / "changelog.md"
    path.write_text(MULTI_RELEASE_CHANGELOG, encoding="UTF-8")

    monkeypatch.setattr(
        "repomatic.changelog.get_pypi_release_dates",
        _pypi_mock({
            "1.1.0": ("2026-02-10", False),
            "1.0.0": ("2025-12-01", False),
        }),
    )
    monkeypatch.setattr(
        "repomatic.metadata.get_project_name",
        lambda: "my-package",
    )
    monkeypatch.setattr(
        "repomatic.changelog.get_github_releases",
        _github_mock(["1.1.0", "1.0.0"]),
    )
    # Tag for 1.0.5 exists but has no changelog entry.
    _patch_tags(monkeypatch, {"1.0.5": "2026-01-15"})

    with caplog.at_level(logging.WARNING):
        assert lint_changelog_dates(path) == 1

    assert "1.0.5: found in external sources" in caplog.text


def test_lint_orphan_fix_inserts_placeholder(tmp_path, monkeypatch):
    """Test that --fix inserts placeholder sections for orphaned versions."""
    path = tmp_path / "changelog.md"
    path.write_text(MULTI_RELEASE_CHANGELOG, encoding="UTF-8")

    monkeypatch.setattr(
        "repomatic.changelog.get_pypi_release_dates",
        _pypi_mock({
            "1.1.0": ("2026-02-10", False),
            "1.0.0": ("2025-12-01", False),
        }),
    )
    monkeypatch.setattr(
        "repomatic.metadata.get_project_name",
        lambda: "my-package",
    )
    monkeypatch.setattr(
        "repomatic.changelog.get_github_releases",
        _github_mock(["1.1.0", "1.0.0"]),
    )
    # Orphan: 1.0.5 exists as a tag but has no changelog entry.
    _patch_tags(monkeypatch, {"1.0.5": "2026-01-15"})

    result = lint_changelog_dates(path, fix=True)
    assert result == 0

    content = path.read_text(encoding="UTF-8")
    assert "## [`1.0.5` (2026-01-15)]" in content
    assert "compare/v1.0.0...v1.0.5" in content
    # 1.1.0 comparison URL should now point to 1.0.5.
    assert "compare/v1.0.5...v1.1.0" in content


def test_lint_orphan_fix_idempotent(tmp_path, monkeypatch):
    """Test that running --fix with orphans twice produces the same result."""
    path = tmp_path / "changelog.md"
    path.write_text(MULTI_RELEASE_CHANGELOG, encoding="UTF-8")

    monkeypatch.setattr(
        "repomatic.changelog.get_pypi_release_dates",
        _pypi_mock({
            "1.1.0": ("2026-02-10", False),
            "1.0.0": ("2025-12-01", False),
        }),
    )
    monkeypatch.setattr(
        "repomatic.metadata.get_project_name",
        lambda: "my-package",
    )
    monkeypatch.setattr(
        "repomatic.changelog.get_github_releases",
        _github_mock(["1.1.0", "1.0.0"]),
    )
    _patch_tags(monkeypatch, {"1.0.5": "2026-01-15"})

    lint_changelog_dates(path, fix=True)
    first_content = path.read_text(encoding="UTF-8")

    lint_changelog_dates(path, fix=True)
    second_content = path.read_text(encoding="UTF-8")

    assert first_content == second_content


def test_lint_orphan_tag_only(tmp_path, monkeypatch, caplog):
    """Test orphan detected from git tag only (not on PyPI or GitHub)."""
    path = tmp_path / "changelog.md"
    path.write_text(MULTI_RELEASE_CHANGELOG, encoding="UTF-8")

    monkeypatch.setattr(
        "repomatic.changelog.get_pypi_release_dates",
        _pypi_mock({
            "1.1.0": ("2026-02-10", False),
            "1.0.0": ("2025-12-01", False),
        }),
    )
    monkeypatch.setattr(
        "repomatic.metadata.get_project_name",
        lambda: "my-package",
    )
    monkeypatch.setattr(
        "repomatic.changelog.get_github_releases",
        _github_mock(["1.1.0", "1.0.0"]),
    )
    _patch_tags(monkeypatch, {"1.0.5": "2026-01-15"})

    with caplog.at_level(logging.WARNING):
        assert lint_changelog_dates(path) == 1

    assert "1.0.5" in caplog.text


def test_lint_orphan_uses_pypi_date(tmp_path, monkeypatch):
    """Test that orphan fix prefers PyPI date over GitHub and tag dates."""
    path = tmp_path / "changelog.md"
    path.write_text(MULTI_RELEASE_CHANGELOG, encoding="UTF-8")

    monkeypatch.setattr(
        "repomatic.changelog.get_pypi_release_dates",
        _pypi_mock({
            "1.1.0": ("2026-02-10", False),
            "1.0.5": ("2026-01-20", False),
            "1.0.0": ("2025-12-01", False),
        }),
    )
    monkeypatch.setattr(
        "repomatic.metadata.get_project_name",
        lambda: "my-package",
    )
    monkeypatch.setattr(
        "repomatic.changelog.get_github_releases",
        _github_mock({
            "1.1.0": "2026-02-10",
            "1.0.5": "2026-01-18",
            "1.0.0": "2025-12-01",
        }),
    )
    _patch_tags(monkeypatch, {"1.0.5": "2026-01-15"})

    lint_changelog_dates(path, fix=True)
    content = path.read_text(encoding="UTF-8")

    # Should use PyPI date (2026-01-20), not GitHub (2026-01-18) or tag (2026-01-15).
    assert "## [`1.0.5` (2026-01-20)]" in content


def test_lint_unreleased_not_flagged_as_orphan(tmp_path, monkeypatch):
    """Test that the unreleased dev version is not flagged as orphan."""
    path = tmp_path / "changelog.md"
    path.write_text(MULTI_RELEASE_CHANGELOG, encoding="UTF-8")

    monkeypatch.setattr(
        "repomatic.changelog.get_pypi_release_dates",
        _pypi_mock({
            "1.1.0": ("2026-02-10", False),
            "1.0.0": ("2025-12-01", False),
        }),
    )
    monkeypatch.setattr(
        "repomatic.metadata.get_project_name",
        lambda: "my-package",
    )
    monkeypatch.setattr(
        "repomatic.changelog.get_github_releases",
        _github_mock(["1.1.0", "1.0.0"]),
    )
    _patch_tags(monkeypatch)

    # 2.0.0 is the unreleased version in MULTI_RELEASE_CHANGELOG.
    # It should not be flagged as orphan.
    assert lint_changelog_dates(path) == 0


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
    monkeypatch.setattr(
        "repomatic.changelog.get_pypi_release_dates",
        lambda pkg: {
            "new-pkg": {
                "1.1.0": PyPIRelease(
                    date="2026-02-10", yanked=False, package="new-pkg"
                ),
            },
            "old-pkg": {
                "1.0.0": PyPIRelease(
                    date="2025-12-01", yanked=False, package="old-pkg"
                ),
            },
        }.get(pkg, {}),
    )
    monkeypatch.setattr(
        "repomatic.metadata.get_project_name",
        lambda: "new-pkg",
    )
    monkeypatch.setattr(
        "repomatic.changelog.get_github_releases",
        _github_mock(["1.1.0", "1.0.0"]),
    )
    _patch_tags(monkeypatch)

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
    monkeypatch.setattr(
        "repomatic.changelog.get_pypi_release_dates",
        lambda pkg: {
            "new-pkg": {
                "1.1.0": PyPIRelease(
                    date="2026-02-10", yanked=False, package="new-pkg"
                ),
                "1.0.0": PyPIRelease(
                    date="2025-12-01", yanked=False, package="new-pkg"
                ),
            },
            "old-pkg": {
                "1.0.0": PyPIRelease(
                    date="2025-11-30", yanked=False, package="old-pkg"
                ),
            },
        }.get(pkg, {}),
    )
    monkeypatch.setattr(
        "repomatic.metadata.get_project_name",
        lambda: "new-pkg",
    )
    monkeypatch.setattr(
        "repomatic.changelog.get_github_releases",
        _github_mock(["1.1.0", "1.0.0"]),
    )
    _patch_tags(monkeypatch)

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
