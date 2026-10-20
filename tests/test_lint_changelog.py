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

"""Tests for `repomatic.lint_changelog`: a changelog against its releases."""

from __future__ import annotations

import logging
from textwrap import dedent

import pytest

from repomatic.changelog import (
    AVAILABLE_VERB,
    NOT_AVAILABLE_VERB,
    Changelog,
    build_release_admonition,
    build_unavailable_admonition,
)
from repomatic.git_ops import GitError
from repomatic.github.releases import GitHubRelease, GitHubReleasesUnavailable
from repomatic.lint_changelog import (
    count_bullet_words,
    lint_changelog_dates,
    split_changelog_bullets,
    warn_on_empty_sections,
    warn_on_long_bullets,
)
from repomatic.pypi import PyPIRelease
from tests.conftest import assert_shipped_text_reads_true_downstream
from tests.test_changelog import MULTI_RELEASE_CHANGELOG


@pytest.fixture
def changelog_file(tmp_path):
    """A changelog on disk carrying {data}`MULTI_RELEASE_CHANGELOG`.

    A test needing other content writes over the returned path.
    """
    path = tmp_path / "changelog.md"
    path.write_text(MULTI_RELEASE_CHANGELOG, encoding="UTF-8")
    return path


def _github_unavailable(_repo_url, force_refresh=False):
    """Stand in for `get_github_releases` during a GitHub API outage.

    Mirrors the real signature, `force_refresh` included: the lint reloads
    both sources live when a cached answer shows a gap the changelog does not
    record, and a double that refuses the keyword fails there instead of at
    the behavior under test.
    """
    raise GitHubReleasesUnavailable("simulated 502 Bad Gateway")


def _pypi_mock(releases, package="my-package", fresh=None):
    """Build a monkeypatch-compatible mock for ``pypi_release_dates``.

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
        "repomatic.lint_changelog.pypi_release_dates",
        pypi
        if callable(pypi)
        else _pypi_mock(pypi or {}, package or "my-package", pypi_fresh),
    )
    monkeypatch.setattr("repomatic.lint_changelog.get_project_name", lambda: package)
    monkeypatch.setattr(
        "repomatic.lint_changelog.get_github_releases",
        github if callable(github) else _github_mock(github, github_fresh),
    )
    monkeypatch.setattr(
        "repomatic.lint_changelog.get_all_version_tags", lambda: tags or {}
    )


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


@pytest.mark.parametrize(
    ("warn", "content"),
    (
        pytest.param(
            lambda changelog: warn_on_long_bullets(changelog, threshold=5),
            _unreleased_changelog(_LONG_BULLET),
            id="long-bullet",
        ),
        pytest.param(
            warn_on_empty_sections, _released_changelog(""), id="empty-section"
        ),
    ),
)
def test_annotation_reads_true_downstream(warn, content, capsys):
    """A lint annotation cites a section the published documentation declares.

    The annotation shows in the checks of a downstream repository, which holds
    no copy of this project's `claude.md`.
    """
    warn(Changelog(content))
    annotation = capsys.readouterr().out
    assert annotation.startswith("::warning::")
    assert_shipped_text_reads_true_downstream("The changelog annotation", annotation)


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


@pytest.mark.parametrize("fix", (False, True))
def test_lint_finds_a_release_newer_than_the_cache(tmp_path, monkeypatch, caplog, fix):
    """A release the cache predates is re-confirmed live, not reported missing.

    Its section records no availability yet, as `--fix` has not run since the
    release. The cached lookups then have no claim to retract, and their
    answer is still stale.
    """
    path = tmp_path / "changelog.md"
    path.write_text(MULTI_RELEASE_CHANGELOG, encoding="UTF-8")
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

    assert "1.1.0: not found on PyPI" not in caplog.text
    assert NOT_AVAILABLE_VERB not in result
    if fix:
        assert "[🐍 PyPI](https://pypi.org/project/my-package/1.1.0/)" in result


def test_lint_leaves_a_recorded_gap_to_the_cache(tmp_path, monkeypatch):
    """A gap the section already records costs no live request.

    A version that never reached a platform keeps its "not available" warning
    from one run to the next: re-fetching it on every run would defeat the
    cache.
    """
    warning = build_unavailable_admonition("1.1.0", missing_github=True)
    path = tmp_path / "changelog.md"
    path.write_text(
        MULTI_RELEASE_CHANGELOG.replace(
            "- Second release.", f"{warning}\n\n- Second release."
        ),
        encoding="UTF-8",
    )

    def github(repo_url, *, force_refresh=False):
        assert not force_refresh, "A recorded gap triggered a live lookup."
        return {"1.0.0": GitHubRelease(date="2025-12-01", body="")}

    _patch_sources(
        monkeypatch,
        pypi={"1.1.0": ("2026-02-10", False), "1.0.0": ("2025-12-01", False)},
        github=github,
    )

    assert lint_changelog_dates(path) == 0


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


def test_lint_reports_an_unconfirmed_gap_when_github_lookup_fails(
    tmp_path, monkeypatch, caplog
):
    """A failed confirming lookup is reported as such, never as a confirmation."""
    path = _changelog_claiming_1_1_0_available(tmp_path)

    def github(repo_url, *, force_refresh=False):
        if force_refresh:
            raise GitHubReleasesUnavailable("API unreachable")
        return {"1.0.0": GitHubRelease(date="2025-12-01", body="")}

    _patch_sources(
        monkeypatch,
        pypi={"1.0.0": ("2025-12-01", False)},
        github=github,
        pypi_fresh={
            "1.1.0": ("2026-02-10", False),
            "1.0.0": ("2025-12-01", False),
        },
    )

    with caplog.at_level(logging.WARNING):
        lint_changelog_dates(path)

    assert "Could not confirm 1.1.0 live" in caplog.text
    assert "Confirmed live" not in caplog.text


def test_lint_confirms_a_pypi_only_gap_when_github_lookup_fails(
    tmp_path, monkeypatch, caplog
):
    """A gap on PyPI alone is settled by the fresh PyPI answer.

    The GitHub half stays cached, but no GitHub answer is needed to tell
    whether PyPI carries the release.
    """
    path = _changelog_claiming_1_1_0_available(tmp_path)

    def github(repo_url, *, force_refresh=False):
        if force_refresh:
            raise GitHubReleasesUnavailable("API unreachable")
        return {
            "1.1.0": GitHubRelease(date="2026-02-10", body=""),
            "1.0.0": GitHubRelease(date="2025-12-01", body=""),
        }

    _patch_sources(
        monkeypatch,
        pypi={"1.0.0": ("2025-12-01", False)},
        github=github,
        pypi_fresh={"1.0.0": ("2025-12-01", False)},
    )

    with caplog.at_level(logging.WARNING):
        lint_changelog_dates(path)

    assert "Confirmed live: 1.1.0 missing" in caplog.text
    assert "Could not confirm" not in caplog.text


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


@pytest.mark.parametrize("source", ("pypi", "github"))
def test_lint_skips_a_release_whose_version_does_not_parse(
    changelog_file, monkeypatch, source
):
    """A release named outside PEP 440 is dropped, never compared.

    A GitHub release can carry any tag, and PyPI keeps releases from before PEP
    440. Each of them is a version the lint has to read past.
    """
    pypi = {"1.1.0": ("2026-02-10", False), "1.0.0": ("2025-12-01", False)}
    github = ["1.1.0", "1.0.0"]
    if source == "pypi":
        pypi["1.0-papaya"] = ("2025-11-01", False)
    else:
        github.append("1.0.0-hotfix2")
    _patch_sources(monkeypatch, pypi=pypi, github=github)

    assert lint_changelog_dates(changelog_file) == 0


def test_lint_reads_the_tags_once(changelog_file, monkeypatch):
    """The date check and the orphan search share one tag listing."""
    tags = {"1.1.0": "2026-02-10", "1.0.0": "2025-12-01"}
    _patch_sources(monkeypatch, tags=tags)
    calls: list[None] = []

    def count_calls():
        calls.append(None)
        return tags

    monkeypatch.setattr("repomatic.lint_changelog.get_all_version_tags", count_calls)

    assert lint_changelog_dates(changelog_file) == 0
    assert len(calls) == 1


def test_lint_tolerates_an_unreadable_tag_list(changelog_file, monkeypatch, caplog):
    """A checkout the tags cannot be read from degrades to "no tag known"."""
    _patch_sources(
        monkeypatch,
        pypi={"1.1.0": ("2026-02-10", False), "1.0.0": ("2025-12-01", False)},
        github=["1.1.0", "1.0.0"],
    )

    def reject():
        raise GitError(128, ["git", "tag"], stderr="fatal: not a git repository")

    monkeypatch.setattr("repomatic.lint_changelog.get_all_version_tags", reject)

    with caplog.at_level(logging.WARNING):
        assert lint_changelog_dates(changelog_file) == 0
    assert "not a git repository" in caplog.text
