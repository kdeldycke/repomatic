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

"""Tests for `repomatic.changelog`: parsing and updating the changelog."""

from __future__ import annotations

import logging
from dataclasses import asdict
from textwrap import dedent

import pytest

from repomatic.changelog import (
    Changelog,
    VersionElements,
    availability_admonition,
)
from repomatic.templating import render_template
from repomatic.tooling.runner import verify_via_write_path
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
    ("version", "initial", "modified", "updated"),
    [
        ("1.1.1", None, False, "# Changelog"),
        ("1.1.1", "", False, "# Changelog"),
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
            False,
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
            True,
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
def test_changelog_update(version, initial, modified, updated):
    """The update reports whether it added an entry.

    An unchanged changelog compares against it: the CLI exits early on `False`.
    """
    changelog = Changelog(initial, current_version=version)
    assert changelog.update() is modified
    assert changelog.content.rstrip() == updated
    if not modified:
        assert changelog.content == (initial or "# Changelog\n")


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
    assert changelog.update(**kwargs) is True
    assert expected in changelog.content


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


@pytest.mark.once
@pytest.mark.real_http
def test_rendered_sections_are_an_mdformat_fixed_point(tmp_path, monkeypatch):
    """Every section this module renders must survive `format-markdown` intact.

    `fix-changelog` writes `changelog.md` and `format-markdown` reformats the
    same file on the same push. When the two disagree on the canonical layout
    they ping-pong: one job rewrites the section, the next reformats it, each
    opening its own PR, and neither converges.

    Both renderings are exercised, because they fail differently. The inserted
    placeholder leaves every optional admonition slot of the `release-notes`
    template empty, which is where stray blank-line runs collect; the
    regenerated section fills the availability slot with a GFM alert, which
    only `mdformat_gfm_alerts` knows how to reflow.

    Verified through the write path rather than `mdformat --check`, whose
    verdict is unreliable for a tool carrying a `post_process` fixup (see
    {func}`~repomatic.tooling.runner.verify_via_write_path`).

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
    elements.availability_admonition = availability_admonition(
        "1.2.2",
        pypi_url="https://pypi.org/project/papaya/1.2.2/",
        missing_github=True,
    )
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


def test_decomposition_follows_every_content_change():
    """The cached sections are dropped whenever the content changes."""
    changelog = Changelog(MULTI_RELEASE_CHANGELOG)
    assert changelog.decompose_version("1.1.0").changes == "- Second release."

    elements = changelog.decompose_version("1.1.0")
    elements.changes = "- Second release, with mangoes."
    assert changelog.replace_section(
        "1.1.0", render_template("release-notes", **asdict(elements))
    )
    assert changelog.decompose_version("1.1.0").changes == (
        "- Second release, with mangoes."
    )

    changelog.insert_version_section(
        "1.0.5", "2026-01-15", "https://github.com/user/repo", ["1.1.0", "1.0.0"]
    )
    assert changelog.decompose_version("1.0.5").date == "2026-01-15"
    assert changelog.decompose_version("1.1.0").compare_url.endswith("v1.0.5...v1.1.0")


def test_decomposition_hands_out_copies():
    """Changing a returned decomposition leaves the next one untouched."""
    changelog = Changelog(MULTI_RELEASE_CHANGELOG)
    changelog.decompose_version("1.0.0").changes = "- Mangled."
    assert changelog.decompose_version("1.0.0").changes == "- Initial release."


def test_insert_version_section_ignores_unparsable_versions():
    """A version PEP 440 refuses takes no part in the ordering."""
    content = MULTI_RELEASE_CHANGELOG.replace(
        "## [1.0.0 (2025-12-01)]", "## [1.0.0.papaya (2025-12-01)]"
    )
    changelog = Changelog(content)
    assert changelog.insert_version_section(
        "1.0.5",
        "2026-01-15",
        "https://github.com/user/repo",
        ["1.1.0", "1.0.0.papaya", "1.0.0"],
    )
    assert "compare/v1.0.0...v1.0.5" in changelog.content
    assert "compare/v1.0.5...v1.1.0" in changelog.content


@pytest.mark.parametrize(
    ("elements", "has_body", "tag_range"),
    (
        pytest.param(VersionElements(), False, "", id="empty"),
        pytest.param(
            VersionElements(
                compare_url="https://github.com/user/repo/compare/v1.0.0...v1.1.0",
                changes="- Add kiwis.",
            ),
            True,
            "v1.0.0...v1.1.0",
            id="released",
        ),
        pytest.param(
            VersionElements(development_warning="> [!WARNING]\n> Unreleased."),
            True,
            "",
            id="warning-only",
        ),
    ),
)
def test_version_elements_queries(elements, has_body, tag_range):
    assert elements.has_body is has_body
    assert elements.tag_range == tag_range


@pytest.mark.parametrize(
    ("changes", "expected"),
    (
        ("", []),
        ("- Only one entry.", ["- Only one entry."]),
        ("- One.\n- Two.\n- Three.", ["- One.", "- Two.", "- Three."]),
        (
            "- Wrapped entry that\n  continues on the next line.",
            ["- Wrapped entry that\n  continues on the next line."],
        ),
        (
            "- Parent entry.\n  - Child A.\n  - Child B.",
            ["- Parent entry.\n  - Child A.\n  - Child B."],
        ),
        (
            "Intro prose, not a bullet.\n\n- The only real bullet.",
            ["- The only real bullet."],
        ),
    ),
)
def test_version_elements_bullets(changes, expected):
    """Each top-level `-` starts one entry; wraps and sub-bullets fold into it."""
    assert VersionElements(changes=changes).bullets == expected


def test_tag_prefixed_headings_are_found_and_stripped():
    """A heading written with the `v` of a tag is invisible until stripped."""
    changelog = Changelog(
        "# Changelog\n\n"
        "## [`v1.2.3` (2026-02-14)]"
        "(https://github.com/user/repo/compare/v1.2.2...v1.2.3)\n\n"
        "- Add a feature.\n\n"
        "## [v1.2.2 (2026-01-01)]"
        "(https://github.com/user/repo/compare/v1.2.1...v1.2.2)\n"
    )
    assert changelog.extract_all_releases() == []
    assert changelog.tag_prefixed_versions() == ["1.2.3", "1.2.2"]
    assert changelog.strip_tag_prefixes() == 2
    assert changelog.tag_prefixed_versions() == []
    assert changelog.extract_all_releases() == [
        ("1.2.3", "2026-02-14"),
        ("1.2.2", "2026-01-01"),
    ]
    assert "compare/v1.2.2...v1.2.3" in changelog.content
