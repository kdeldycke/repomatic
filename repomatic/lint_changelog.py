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

"""Check a changelog against the places its releases are published.

The `lint-changelog` command reads every released section of the changelog and
compares it with three sources: PyPI uploads, GitHub releases and git tags. A
date that disagrees, a version published but never documented, and a section
whose availability note is stale are reported, and repaired under `--fix`.

Four content checks run beside it and never change the exit code: a warning on
over-long bullets in the unreleased section, on released sections that hold no
entry, on an entry filed out of the Breaking, then Deprecated, then ordinary
order, and on a `#N` naming no issue. A fifth, on a heading writing its version
with the `v` of a git tag, is fatal, since no check reads such a heading.
`--fix` strips the prefix instead.

The changelog model these checks read and rewrite lives in
{mod}`repomatic.changelog`.
"""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, field, replace
from functools import cached_property
from typing import NamedTuple

from packaging.version import Version

from .changelog import (
    GITHUB_LABEL,
    GITHUB_RELEASE_URL,
    PYPI_LABEL,
    Changelog,
    availability_admonition,
)
from .file_ops import normalize_text, write_if_changed
from .git_ops import GitError, get_all_version_tags
from .github.gh import gh_json
from .github.outputs import AnnotationLevel, emit_annotation
from .github.releases import (
    GitHubRelease,
    GitHubReleasesUnavailable,
    get_github_releases,
)
from .github.urls import parse_github_repo
from .pypi import (
    PYPI_PROJECT_URL,
    PyPIRelease,
    pypi_release_dates,
)
from .pyproject import get_project_name
from .templating import render_template
from .versions import safe_version

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path
    from typing import Final, TypeVar

    ReleaseT = TypeVar("ReleaseT")

EMPTY_PYPI_SANITY_THRESHOLD = 3
"""Minimum number of existing PyPI links in the changelog above which an
empty PyPI lookup is treated as a transient failure rather than a genuine
"package has no releases" state.

```{note}
Two layers of ambiguity make this threshold necessary:

1. `repomatic.pypi._fetch_json` returns `None` on every failure mode
   (HTTP 4xx/5xx, network error, timeout, JSON parse error), collapsing
   "package not on PyPI" and "transient API failure" into the same empty
   result.
2. Even when the HTTP status is preserved, a `404` from
   `/pypi/<name>/json` is not authoritative: Warehouse 404s registered
   projects that have no published releases, and registered packages can
   appear in the `simple` / `list_packages` indexes while still 404'ing
   on the JSON endpoint. See
   [pypi/warehouse#1388](https://github.com/pypi/warehouse/issues/1388)
   and [pypi/warehouse#9536](https://github.com/pypi/warehouse/issues/9536).
```

The threshold guards against a transient failure silently stripping every
PyPI link from the changelog. Re-runs of `lint-changelog --fix` against
a healthy API restore the file.
"""

ENTRY_MARKS: Final[tuple[str, ...]] = ("**Breaking:**", "**Deprecated:**")
"""The bold leads that open a section, in the order they lead it.

A reader scans a release for what breaks before anything else, so every
`**Breaking:**` entry comes first, then every `**Deprecated:**` one, then the
rest. {func}`warn_on_entry_order` holds the unreleased section to it.
"""

LINT_DOCS_URL = "https://repomatic.net/workflows#fix-changelog-fix-changelog"
"""Section of the published documentation on the content checks.

Every check ends its annotation on it. An annotation is read in a downstream
repository, which holds no copy of this project's `claude.md`, so the link
aims at the site.
"""

REFERENCE_RE = re.compile(r"(?<![\w/#&])#(?P<number>\d+)\b")
"""A bare `#N` in prose.

The lookbehind leaves out the `owner/repo#N` slug, where the number follows a
word character, and a URL fragment, where it follows a `#` or an `&`.
"""

CODE_SPAN_RE = re.compile(r"(?<!`)(`+)(?!`).+?(?<!`)\1(?!`)")
"""An inline code span, which {func}`warn_on_positional_references` skips.

A run of backticks opens the span and the next run of the same length closes
it, as CommonMark reads one, so a span opened with two backticks can quote a
single one.
"""

LINK_TARGET_RE = re.compile(r"\]\([^)]*\)")
"""The target of a Markdown link, which a fragment like `#issuecomment-1` sits in."""

REFUSED_EXIT_CODE = 2
"""Exit code of a `--fix` run that refuses to rewrite the changelog.

Returned when an upstream lookup looks unhealthy, so a rewrite would strip
links on bad data. See {func}`lint_changelog_dates`.
"""


def _parsable(releases: dict[str, ReleaseT], source: str) -> dict[str, ReleaseT]:
    """Drop the versions of *releases* that do not parse as PEP 440.

    PyPI keeps legacy releases from before PEP 440, and a GitHub release can
    carry any tag (`v1.2.3-hotfix2`). Every later step sorts or compares
    versions, so an unparsable one is dropped here, once, instead of stopping
    the lint where it is compared.

    :param releases: Releases keyed by version string.
    :param source: The lookup's name, for the log line.
    :return: The releases whose version parses.
    """
    kept = {}
    for version, release in releases.items():
        if safe_version(version) is None:
            logging.debug(f"Skipping {source} version {version!r}: not PEP 440.")
            continue
        kept[version] = release
    return kept


def _load_pypi_releases(
    package: str | None, history: Sequence[str], *, force_refresh: bool = False
) -> dict[str, PyPIRelease]:
    """Load a package's PyPI releases, merged with its former names.

    A renamed project keeps its old releases under the old name, so the
    changelog's early versions resolve only through
    `[tool.repomatic] pypi.package-history`. The current package wins on
    version collisions.

    :param package: Current PyPI package name; empty or `None` skips PyPI.
    :param history: Former package names, oldest-first.
    :param force_refresh: Ignore cached responses and re-fetch.
    :return: Dict mapping version strings to {class}`PyPIRelease` tuples.
    """
    releases: dict[str, PyPIRelease] = {}
    if package:
        releases = pypi_release_dates(package, force_refresh=force_refresh)
    for former_package in history:
        former_data = pypi_release_dates(former_package, force_refresh=force_refresh)
        if former_data:
            logging.info(
                f"Using PyPI history for {former_package!r}"
                f" ({len(former_data)} releases found)."
            )
        for version, release in former_data.items():
            releases.setdefault(version, release)
    return releases


def _load_tag_dates() -> dict[str, str]:
    """Read the date of every release tag, or nothing when git cannot.

    The tag dates are a fallback reference, so a checkout git cannot read
    degrades to "no tag known" with a warning, instead of stopping the lint.
    """
    try:
        return get_all_version_tags()
    except GitError as error:
        logging.warning(f"Cannot read the release tags: {error}")
        return {}


def _is_platform_gap(
    parsed: Version,
    on_platform: bool,
    platform_known: bool,
    first_version: Version | None,
) -> bool:
    """Whether *parsed* is missing from a platform it should appear on.

    A gap is an absence *after* the platform's first release: versions
    predating it were never expected there, so they are not warned about.

    :param parsed: The version under test.
    :param on_platform: Whether the lookup found it on the platform.
    :param platform_known: Whether the platform is addressable at all (a
        package name for PyPI, a repository URL for GitHub).
    :param first_version: Earliest version seen on that platform.
    :return: `True` when the version is a genuine gap.
    """
    return (
        not on_platform
        and platform_known
        and first_version is not None
        and parsed >= first_version
    )


def _records_absence(admonition: str, label: str) -> bool:
    """Whether an availability admonition already marks a platform missing.

    An available platform renders as a markdown link and a missing one as a
    bare label, so the `[` prefix is what separates a claim of presence from a
    record of absence.

    :param admonition: A section's availability admonition, possibly empty.
    :param label: The platform's display label, like {data}`PYPI_LABEL`.
    :return: `True` when the admonition names the platform without a link.
    """
    return label in admonition and f"[{label}](" not in admonition


def _unreleased_versions(changelog: Changelog) -> list[str]:
    """The versions whose section is not released yet, newest first.

    Only these sections take a content check: a released section is immutable
    history, and re-flagging it on every run would be noise.
    """
    released = {version for version, _date in changelog.extract_all_releases()}
    unreleased = changelog.extract_all_version_headings() - released
    return sorted(
        unreleased, key=lambda v: safe_version(v) or Version("0"), reverse=True
    )


def count_bullet_words(bullet: str) -> int:
    """Count the words in a changelog bullet, ignoring list markers.

    Leading `-`/`*` markers (on the entry and any nested sub-bullets) are
    stripped so they do not inflate the count; everything else, including
    inline code and link text, counts as written.
    """
    return len(re.sub(r"(?m)^\s*[-*]\s+", "", bullet).split())


def warn_on_long_bullets(changelog: Changelog, threshold: int) -> None:
    """Warn about over-long bullets in the unreleased section, non-fatally.

    A changelog entry is a release note, not a commit message: one short
    sentence of 10 to 25 words stating what changed. Each unreleased bullet
    longer than `threshold` words emits a {data}`logging.WARNING` and a GitHub
    Actions warning annotation, without affecting the lint exit code.

    Only the unreleased section is inspected. Released sections are immutable,
    so re-flagging historical entries on every run would be noise.

    :param changelog: The parsed changelog to inspect.
    :param threshold: Word ceiling per bullet. `0` (or less) disables the check.
    """
    if threshold <= 0:
        return
    for version in _unreleased_versions(changelog):
        elements = changelog.decompose_version(version)
        for index, bullet in enumerate(elements.bullets, 1):
            words = count_bullet_words(bullet)
            if words > threshold:
                logging.warning(
                    f"⚠ {version}: changelog entry {index} runs {words} words "
                    f"(over {threshold}). Tighten it to a one-sentence release "
                    f"note; move mechanism and rationale to the commit or PR."
                )
                emit_annotation(
                    AnnotationLevel.WARNING,
                    f"Changelog entry {index} for {version} runs {words} words, "
                    f"over the {threshold}-word guideline. A changelog entry is "
                    f"a release note, not a commit message: keep it to one short "
                    f"sentence, and move mechanism and rationale to the commit "
                    f"or the pull request. See {LINT_DOCS_URL}",
                )


def warn_on_empty_sections(changelog: Changelog) -> None:
    """Warn about released sections holding no entry, non-fatally.

    A published heading with nothing under it reads as broken to anyone
    scanning release notes, and it is not merely cosmetic: the GitHub
    release body is rebuilt from this section, so an empty one publishes an
    empty release. The fix is to name what actually moved rather than to
    leave the section blank.

    Only *released* sections are inspected. The unreleased section is
    legitimately empty for most of a cycle, since the post-release bump
    creates it with no entries, so flagging it would fire on every push in
    the hours after a release and train the reader to ignore the check.
    That is the mirror of {func}`warn_on_long_bullets`, which inspects the
    unreleased section alone because re-flagging immutable history is the
    noise there.

    Availability, editorial and yanked admonitions live in their own
    {class}`~repomatic.changelog.VersionElements` fields, so a section
    carrying nothing but a `[!WARNING]` about missing binaries still counts as
    empty: an admonition explains a caveat, it does not say what changed.

    :param changelog: The parsed changelog to inspect.
    """
    for version, _date in sorted(
        changelog.extract_all_releases(), key=lambda r: Version(r[0]), reverse=True
    ):
        if changelog.decompose_version(version).bullets:
            continue
        logging.warning(
            f"⚠ {version}: released section holds no entry. Add one naming what "
            f"moved, even on a purely mechanical cycle."
        )
        emit_annotation(
            AnnotationLevel.WARNING,
            f"Changelog section for {version} is empty. A published release "
            f"heading with no entries reads as broken, and the GitHub release "
            f"body is rebuilt from it. Name what moved, even on a purely "
            f"mechanical cycle. See {LINT_DOCS_URL}",
        )


def _entry_rank(bullet: str) -> int:
    """Where *bullet* belongs in its section.

    :return: The index of its {data}`ENTRY_MARKS` lead, or one past them for
        an entry opening on no mark.
    """
    text = bullet.lstrip("- ").lstrip()
    for rank, mark in enumerate(ENTRY_MARKS):
        if text.startswith(mark):
            return rank
    return len(ENTRY_MARKS)


def warn_on_entry_order(changelog: Changelog) -> None:
    """Warn about an entry filed out of the order its mark sets, non-fatally.

    The {data}`ENTRY_MARKS` lead a section in their order: every
    `**Breaking:**` entry first, then every `**Deprecated:**` one, then the
    rest. Only the unreleased section is inspected, per
    `_unreleased_versions`.

    :param changelog: The parsed changelog to inspect.
    """
    for version in _unreleased_versions(changelog):
        highest = 0
        for index, bullet in enumerate(changelog.decompose_version(version).bullets, 1):
            rank = _entry_rank(bullet)
            if rank < highest:
                mark = ENTRY_MARKS[rank]
                logging.warning(
                    f"⚠ {version}: changelog entry {index} opens on {mark} but "
                    f"follows an entry that does not. Move it up: Breaking "
                    f"entries lead the section, then Deprecated ones, then the "
                    f"rest."
                )
                emit_annotation(
                    AnnotationLevel.WARNING,
                    f"Changelog entry {index} for {version} opens on {mark} but "
                    f"follows an entry that does not. Breaking entries lead a "
                    f"section, then Deprecated ones, then the rest. See "
                    f"{LINT_DOCS_URL}",
                )
            highest = max(highest, rank)


def _referenced_numbers(bullet: str) -> list[int]:
    """The numbers of every bare `#N` *bullet* names in prose.

    A code span and a link target are dropped first: a `#N` quoted there is
    not a cross-reference GitHub renders.
    """
    prose = LINK_TARGET_RE.sub("]()", CODE_SPAN_RE.sub("", bullet))
    return [int(match["number"]) for match in REFERENCE_RE.finditer(prose)]


def warn_on_positional_references(changelog: Changelog) -> None:
    """Warn about a `#N` in an unreleased entry naming no issue, non-fatally.

    GitHub links every `#N` to the issue or pull request of that number, so a
    positional `test #1` renders as a cross-reference to whatever carries the
    number. Each number of the unreleased section is looked up on the
    repository the comparison URLs name. The lookup is skipped, with an info
    line, when `gh` cannot read that repository: absent, unauthenticated or
    offline, which a local run often is.

    :param changelog: The parsed changelog to inspect.
    """
    references = sorted({
        (version, index, number)
        for version in _unreleased_versions(changelog)
        for index, bullet in enumerate(changelog.decompose_version(version).bullets, 1)
        for number in _referenced_numbers(bullet)
    })
    if not references:
        return
    repo = parse_github_repo(changelog.extract_repo_url())
    if repo is None:
        logging.info(
            "The changelog names no GitHub repository: #N references are not checked."
        )
        return
    slug = "/".join(repo)
    if gh_json(["api", f"repos/{slug}"], soft=True) is None:
        logging.info(f"gh cannot read {slug}: #N references are not checked.")
        return
    for version, index, number in references:
        if gh_json(["api", f"repos/{slug}/issues/{number}"], soft=True) is not None:
            continue
        logging.warning(
            f"⚠ {version}: changelog entry {index} names #{number}, which is no "
            f"issue or pull request of {slug}. Write a position in words, or "
            f"link the thread meant."
        )
        emit_annotation(
            AnnotationLevel.WARNING,
            f"Changelog entry {index} for {version} names #{number}, which is no "
            f"issue or pull request of {slug}, yet GitHub links it as one. Write "
            f"a position in words, or link the thread meant. See {LINT_DOCS_URL}",
        )


def check_tag_prefixed_headings(changelog: Changelog, *, fix: bool) -> bool:
    """Report each heading naming its version with a `v`, or strip it under *fix*.

    A heading carrying the prefix of a git tag is one no check reads, per
    {data}`~repomatic.changelog.TAG_PREFIXED_HEADING_RE`, so the finding is
    fatal: left standing, the release behind it goes unverified. The rewrite
    is mechanical, so `--fix` applies it rather than failing.

    :param changelog: The parsed changelog to inspect, rewritten under *fix*.
    :param fix: Strip the prefixes instead of reporting them.
    :return: `True` when a prefixed heading is left standing.
    """
    versions = changelog.tag_prefixed_versions()
    if not versions:
        return False
    if fix:
        changelog.strip_tag_prefixes()
        logging.info(
            f"Stripped the v prefix off {len(versions)} heading(s): "
            f"{', '.join(versions)}."
        )
        return False
    for version in versions:
        logging.error(
            f"✗ v{version}: heading carries a tag prefix. Write the bare version, "
            f"`{version}`, or no check reads the section."
        )
        emit_annotation(
            AnnotationLevel.ERROR,
            f"Changelog heading for {version} carries the v prefix of a git tag. "
            f"A heading names the package version, bare: no check reads the "
            f"section otherwise. See {LINT_DOCS_URL}",
        )
    return True


@dataclass(frozen=True)
class ReleaseSources:
    """The external lookups a changelog's dates and availability are checked against.

    The PyPI and GitHub lookups are TTL-cached. The boundary versions are
    derived from them here, so a forced refresh cannot leave a stale boundary
    behind. Frozen so the boundary properties can be cached: a refreshed half
    swaps in through `dataclasses.replace`, which guarantees the derivations
    never go stale against an in-place edit.

    Every version these lookups hold parses as PEP 440: {meth}`load` drops the
    others.
    """

    package: str | None
    """PyPI package name, `None` when the project publishes nothing."""

    pypi_data: dict[str, PyPIRelease]
    """Released versions on PyPI, keyed by version string."""

    repo_url: str
    """Repository URL read out of the changelog's own comparison links."""

    github_releases: dict[str, GitHubRelease]
    """Published GitHub releases, keyed by version string."""

    tag_dates: dict[str, str] = field(default_factory=dict)
    """Release tag dates in `YYYY-MM-DD` format, keyed by version string.

    Read once per load: the date check and the orphan search both consult it.
    """

    github_fetch_failed: bool = False
    """Whether the GitHub lookup errored, as opposed to answering empty."""

    @property
    def use_pypi(self) -> bool:
        """Whether PyPI is the reference source, rather than git tags."""
        return bool(self.pypi_data)

    @cached_property
    def first_pypi_version(self) -> Version | None:
        """Oldest version on PyPI, for the predates-the-index boundary.

        Cached: the fix loop reads the boundary several times per version.
        """
        return min(map(Version, self.pypi_data), default=None)

    @cached_property
    def first_github_version(self) -> Version | None:
        """Oldest GitHub release, for the predates-the-releases boundary.

        Cached, like {attr}`first_pypi_version`.
        """
        return min(map(Version, self.github_releases), default=None)

    @classmethod
    def load(
        cls,
        changelog: Changelog,
        package: str | None,
        pypi_package_history: Sequence[str] | None,
        *,
        force_refresh: bool = False,
    ) -> ReleaseSources:
        """Fetch every release source, reporting what each answered.

        :param changelog: Parsed changelog, read for its repository URL.
        :param package: PyPI package name, auto-detected when `None`.
        :param pypi_package_history: Former package names to merge in.
        :param force_refresh: Bypass the caches, for a live re-confirmation.
        """
        if package is None:
            package = get_project_name()
        pypi_data = _parsable(
            _load_pypi_releases(
                package, pypi_package_history or (), force_refresh=force_refresh
            ),
            "PyPI",
        )
        if not package:
            logging.info("No package name detected, falling back to git tags.")
        elif pypi_data:
            logging.info(
                f"Using PyPI as reference for {package!r}"
                f" ({len(pypi_data)} releases found)."
            )
        else:
            logging.info(
                f"Package {package!r} not found on PyPI, falling back to git tags."
            )

        repo_url = changelog.extract_repo_url()
        github_releases: dict[str, GitHubRelease] = {}
        github_fetch_failed = False
        if repo_url:
            try:
                github_releases = _parsable(
                    get_github_releases(repo_url, force_refresh=force_refresh),
                    "GitHub",
                )
            except GitHubReleasesUnavailable as exc:
                github_fetch_failed = True
                logging.warning(f"GitHub releases lookup failed: {exc}")
                emit_annotation(
                    AnnotationLevel.WARNING,
                    f"GitHub releases lookup failed: {exc}",
                )
            if github_releases:
                logging.info(f"GitHub releases: {len(github_releases)} found.")

        sources = cls(
            package=package,
            pypi_data=pypi_data,
            repo_url=repo_url or "",
            github_releases=github_releases,
            tag_dates=_load_tag_dates(),
            github_fetch_failed=github_fetch_failed,
        )
        if sources.first_pypi_version:
            logging.info(f"First PyPI version: {sources.first_pypi_version}")
        if sources.first_github_version:
            logging.info(f"First GitHub version: {sources.first_github_version}")
        return sources

    def platform_gaps(self, version: str) -> tuple[bool, bool]:
        """Whether *version* is a gap on PyPI and on GitHub, in that order.

        See `_is_platform_gap` for what counts as a gap.
        """
        parsed = Version(version)
        return (
            _is_platform_gap(
                parsed,
                version in self.pypi_data,
                bool(self.package),
                self.first_pypi_version,
            ),
            _is_platform_gap(
                parsed,
                version in self.github_releases,
                bool(self.repo_url),
                self.first_github_version,
            ),
        )

    def unrecorded_gaps(
        self,
        changelog: Changelog,
        releases: Sequence[tuple[str, str]],
        *,
        github_only: bool = False,
    ) -> set[str]:
        """Versions these lookups miss where their section records no absence.

        A section claiming the platform is one a stale snapshot would retract,
        and a section recording nothing yet belongs to a release that may
        postdate the snapshot: both need a live answer. A gap the section
        already records is left to the cache, so a permanent one costs no
        request per run.

        :param github_only: Report only the versions whose unrecorded gap is on
            GitHub. A PyPI-only gap needs no GitHub answer, so a failed GitHub
            lookup still leaves it settled.
        """
        found = set()
        for candidate, _candidate_date in releases:
            pypi_gap, github_gap = self.platform_gaps(candidate)
            if not (pypi_gap or github_gap):
                continue
            existing = changelog.decompose_version(candidate).availability_admonition
            unrecorded_pypi = pypi_gap and not _records_absence(existing, PYPI_LABEL)
            unrecorded_github = github_gap and not _records_absence(
                existing, GITHUB_LABEL
            )
            if unrecorded_github or (unrecorded_pypi and not github_only):
                found.add(candidate)
        return found


class DateCheck(NamedTuple):
    """What comparing every changelog date against its reference source found."""

    corrections: dict[str, str]
    """Version to the date it should carry, for the versions `--fix` repairs."""

    mismatched: bool
    """Whether any version's date disagreed with its reference source."""

    unfixed: bool
    """Whether a mismatch was left standing (no `--fix` to apply)."""


def _check_release_dates(
    releases: Sequence[tuple[str, str]],
    sources: ReleaseSources,
    *,
    fix: bool,
    abandoned_versions: Sequence[str],
) -> DateCheck:
    """Compare each documented release date against its reference source.

    PyPI upload dates are canonical when the project publishes there, since
    they are immutable; a project without a PyPI presence falls back to git
    tag dates, which can be recreated. A version absent from the reference is
    reported rather than corrected: it may predate the source, be
    deliberately abandoned, or be a genuine gap.

    :param releases: `(version, date)` pairs read from the changelog.
    :param sources: The loaded release lookups.
    :param fix: Whether a mismatch is collected for repair or left standing.
    :param abandoned_versions: Versions never published, reported as skipped.
    :return: The collected corrections and the two problem flags.
    """
    corrections: dict[str, str] = {}
    mismatched = False
    unfixed = False
    abandoned = frozenset(abandoned_versions)
    # Point maintainers at the remedy: a version that is intentionally absent
    # from the reference source is declared once in config, not re-flagged.
    abandoned_hint = (
        "list under [tool.repomatic] abandoned-versions if intentionally unpublished"
    )

    for version, changelog_date in releases:
        if sources.use_pypi:
            release = sources.pypi_data.get(version)
            if release is None:
                if version in abandoned:
                    logging.info(f"  {version}: abandoned (skipped per config)")
                    continue
                first = sources.first_pypi_version
                if first and Version(version) < first:
                    logging.info(f"  {version}: predates PyPI (first: {first})")
                    continue
                logging.warning(f"⚠ {version}: not found on PyPI ({abandoned_hint})")
                emit_annotation(
                    AnnotationLevel.WARNING,
                    f"Version {version} not found on PyPI ({abandoned_hint})",
                )
                continue
            ref_date = release.date
            source = "PyPI"
        else:
            tag_date = sources.tag_dates.get(version)
            source = "tag"
            if tag_date is None:
                if version in abandoned:
                    logging.info(f"  {version}: abandoned (skipped per config)")
                    continue
                logging.warning(
                    f"⚠ {version}: not found on {source} ({abandoned_hint})"
                )
                emit_annotation(
                    AnnotationLevel.WARNING,
                    f"Version {version} not found on {source} ({abandoned_hint})",
                )
                continue
            ref_date = tag_date

        if changelog_date == ref_date:
            logging.info(f"✓ {version}: {changelog_date} ({source})")
            continue

        logging.error(f"✗ {version}: changelog={changelog_date}, {source}={ref_date}")
        emit_annotation(
            AnnotationLevel.ERROR,
            (
                f"Date mismatch for {version}:"
                f" changelog={changelog_date}, {source}={ref_date}"
            ),
        )
        mismatched = True
        if fix:
            corrections[version] = ref_date
        else:
            unfixed = True

    return DateCheck(corrections, mismatched, unfixed)


class OrphanReconciliation(NamedTuple):
    """What reconciling versions missing from the changelog produced."""

    releases: list[tuple[str, str]]
    """Documented releases, re-read when insertions changed the file."""

    found: bool
    """Whether any orphan was detected at all."""

    unfixed: bool
    """Whether an orphan was left in place, unrepaired."""


def _reconcile_orphans(
    changelog: Changelog,
    releases: list[tuple[str, str]],
    sources: ReleaseSources,
    *,
    fix: bool,
    archive_path: Path | None,
) -> OrphanReconciliation:
    """Report versions published somewhere but absent from the changelog.

    An orphan is a version the outside world knows (a git tag, a GitHub
    release, a PyPI upload) that the changelog never documents. With *fix*
    they are inserted oldest-first, so each insertion updates the adjacent
    section's comparison URL correctly.

    :param changelog: Parsed changelog, mutated in place when inserting.
    :param releases: Documented `(version, date)` pairs.
    :param sources: The loaded release lookups.
    :param fix: Whether to insert the missing sections.
    :param archive_path: Frozen changelog whose versions count as documented.
    :return: The (possibly re-read) releases and the two outcome flags.
    """
    changelog_headings = changelog.extract_all_version_headings()
    # Versions documented in the frozen archive count as present, so entries
    # split out of the live changelog are neither flagged nor re-inserted as
    # orphans.
    archive_headings: set[str] = set()
    if archive_path and archive_path.exists():
        archive_headings = Changelog(
            archive_path.read_text(encoding="UTF-8")
        ).extract_all_version_headings()
    # The changelog documents only final releases. Exclude pre-releases
    # (dev/alpha/beta/rc) so a published `X.Y.Z.dev0` tag, PyPI upload, or
    # GitHub prerelease is never mistaken for a missing changelog entry: an
    # orphan insertion would both drop a spurious pre-release section and
    # rewrite the adjacent release's comparison-URL base to point at it.
    all_known = {
        v
        for v in (
            set(sources.pypi_data)
            | set(sources.github_releases)
            | set(sources.tag_dates)
        )
        if not Version(v).is_prerelease
    }
    orphans = all_known - changelog_headings - archive_headings
    if not orphans:
        return OrphanReconciliation(releases, False, False)

    for orphan in sorted(orphans, key=Version):
        logging.warning(
            f"⚠ {orphan}: found in external sources but missing from changelog"
        )
        emit_annotation(
            AnnotationLevel.WARNING,
            (
                f"Version {orphan} exists as a tag, GitHub release,"
                " or PyPI package but has no changelog entry"
            ),
        )

    if not (fix and sources.repo_url):
        # Without `--fix`, or without a repository URL to build comparison
        # links from, the orphans stay orphaned.
        return OrphanReconciliation(releases, True, True)

    unfixed = False
    all_versions = [*changelog_headings, *orphans]
    # Insert orphans oldest-first so each insertion correctly updates the
    # adjacent section's comparison URL.
    for orphan in sorted(orphans, key=Version):
        # Determine date: prefer PyPI, then GitHub, then git tag.
        orphan_date = ""
        if orphan in sources.pypi_data:
            orphan_date = sources.pypi_data[orphan].date
        elif orphan in sources.github_releases:
            orphan_date = sources.github_releases[orphan].date
        elif orphan in sources.tag_dates:
            orphan_date = sources.tag_dates[orphan]
        if not orphan_date:
            # No source agrees on a date, so there is nothing truthful to
            # write. A placeholder would satisfy the heading pattern and then
            # mismatch its reference date on every later run, which is worse
            # than leaving the orphan reported.
            logging.warning(
                f"⚠ {orphan}: no release date found in any source, skipping insertion"
            )
            unfixed = True
            continue
        if not changelog.insert_version_section(
            orphan, orphan_date, sources.repo_url, all_versions
        ):
            unfixed = True

    # Re-extract releases so the admonition phase processes the newly
    # inserted sections.
    return OrphanReconciliation(changelog.extract_all_releases(), True, unfixed)


def _refuse(message: str) -> None:
    """Report a refused rewrite, as an error log line and an annotation."""
    logging.error(message)
    emit_annotation(AnnotationLevel.ERROR, message)


def _reconfirm_live(
    changelog: Changelog,
    releases: Sequence[tuple[str, str]],
    sources: ReleaseSources,
    pypi_package_history: Sequence[str],
    *,
    fix: bool,
) -> ReleaseSources | None:
    """Re-check live every gap the cached lookups report.

    A release is never reported or recorded as missing on cached data alone.
    Both lookups are TTL-cached for a day, so a snapshot taken before a release
    landed reports that release as missing, which is indistinguishable here
    from one that was never published. The sanity gate of
    {func}`_sanity_gate_refuses` catches only a *wholly* empty result: a
    stale-but-populated snapshot walks past it, and the window where it lies is
    exactly the hours after a release, when this command is most likely to
    run. The section of a fresh release usually records nothing yet, so a gap
    is re-checked unless its section already records it.

    :return: The lookups to lint against: refreshed where the live answers
        could be read, the cached ones otherwise. `None` when `--fix` must
        refuse, because the confirming GitHub lookup failed.
    """
    unconfirmed = sources.unrecorded_gaps(changelog, releases)
    if not unconfirmed:
        return sources
    logging.info(
        f"Lookups miss {', '.join(sorted(unconfirmed))}, which the"
        " changelog does not mark missing; re-confirming live."
    )
    confirmed = ReleaseSources.load(
        changelog, sources.package, pypi_package_history, force_refresh=True
    )
    if fix and confirmed.github_fetch_failed:
        _refuse(
            f"Refusing to rewrite changelog: cached data would mark"
            f" {', '.join(sorted(unconfirmed))} as unpublished,"
            f" and the confirming GitHub lookup failed."
            f" Re-run when the GitHub API is reachable."
        )
        return None
    if not confirmed.github_fetch_failed:
        sources = confirmed
    else:
        # Keep the fresh PyPI half; the GitHub half stays as fetched.
        sources = replace(sources, pypi_data=confirmed.pypi_data)
    still_missing = sources.unrecorded_gaps(changelog, releases)
    # Only reachable without `--fix`, which refuses above: the GitHub half is
    # still the cached answer, so a gap needing it stays unconfirmed. A
    # PyPI-only gap was just confirmed against the fresh PyPI answer.
    unconfirmable = (
        sources.unrecorded_gaps(changelog, releases, github_only=True)
        if confirmed.github_fetch_failed
        else set()
    )
    if unconfirmable:
        logging.warning(
            f"Could not confirm {', '.join(sorted(unconfirmable))} live:"
            " the GitHub releases lookup failed."
        )
    still_missing -= unconfirmable
    if still_missing:
        # Confirmed against the live APIs: the release really is missing (a
        # failed upload, a deleted GitHub release, a removed PyPI file).
        # Recording the absence is then the correct repair, not a stale-cache
        # artifact.
        logging.warning(
            f"Confirmed live: {', '.join(sorted(still_missing))} missing"
            " from a platform the changelog does not mark."
        )
    elif not unconfirmable:
        logging.info(
            "Live lookups list every release the cache missed; the cache was stale."
        )
    return sources


def _sanity_gate_refuses(
    changelog: Changelog,
    releases: Sequence[tuple[str, str]],
    sources: ReleaseSources,
) -> bool:
    """Refuse a rewrite when a lookup came back empty against a covered changelog.

    Guards against the `kdeldycke/click-extra#1702` failure mode, where a
    transient API call returned `{}` and the subsequent rewrite stripped every
    GitHub (or PyPI) link from the changelog without ever flagging the gap as
    a warning.

    :return: `True` when the rewrite is refused, after reporting why.
    """
    existing_github_links = 0
    existing_pypi_links = 0
    for version, _date in releases:
        existing_admonition = changelog.decompose_version(
            version
        ).availability_admonition
        if GITHUB_LABEL in existing_admonition:
            existing_github_links += 1
        if PYPI_LABEL in existing_admonition:
            existing_pypi_links += 1

    # GitHub side: an explicit `GitHubReleasesUnavailable` is an unambiguous
    # signal that the data is unreliable. Refuse to rewrite as soon as the
    # existing changelog has any GitHub coverage, since the rewrite would
    # silently strip it.
    if sources.github_fetch_failed and existing_github_links > 0:
        _refuse(
            f"Refusing to rewrite changelog: GitHub releases lookup "
            f"failed but {existing_github_links} existing version"
            f" section(s) reference a GitHub release. Rewriting now "
            f"would silently strip every GitHub link from the file. "
            f"Re-run when the GitHub API is reachable."
        )
        return True

    # PyPI side: an empty lookup cannot distinguish "package not on PyPI" from
    # "transient failure", see `EMPTY_PYPI_SANITY_THRESHOLD`. A substantial
    # number of existing PyPI links combined with an empty fetch is the
    # fingerprint of a transient failure rather than a genuine non-PyPI
    # project.
    if (
        sources.package
        and not sources.pypi_data
        and existing_pypi_links >= EMPTY_PYPI_SANITY_THRESHOLD
    ):
        _refuse(
            f"Refusing to rewrite changelog: PyPI lookup for "
            f"{sources.package!r} returned no data but {existing_pypi_links} "
            f"existing version section(s) reference a PyPI release. "
            f"Likely a transient API failure. Re-run when PyPI is "
            f"reachable. If the project genuinely no longer publishes to "
            f"PyPI, run with an empty package name (`--package ''`) to "
            f"check dates against git tags instead."
        )
        return True
    return False


def _rebuild_admonitions(
    changelog: Changelog,
    releases: Sequence[tuple[str, str]],
    sources: ReleaseSources,
    corrections: dict[str, str],
) -> None:
    """Re-render every released section with its corrected date and admonitions.

    Each section is decomposed into elements, given its corrected date and
    fresh availability admonitions, and reassembled through the
    `release-notes` template. Idempotent: decomposing and re-rendering an
    already-correct section changes nothing.

    :param changelog: Parsed changelog, rewritten in place.
    :param releases: Documented `(version, date)` pairs.
    :param sources: The loaded release lookups.
    :param corrections: Version to the date it should carry.
    """
    for version, _date in releases:
        elements = changelog.decompose_version(version)

        if version in corrections:
            elements.date = corrections[version]

        on_pypi = version in sources.pypi_data
        on_github = version in sources.github_releases
        is_yanked = on_pypi and sources.pypi_data[version].yanked

        # Build the NOTE admonition for platforms where available. The package
        # name comes from the PyPIRelease entry, so a renamed project points to
        # the right PyPI page. Yanked releases are excluded from the NOTE: the
        # CAUTION admonition below links to the specific PyPI page instead.
        pypi_url = (
            PYPI_PROJECT_URL.format(
                package=sources.pypi_data[version].package, version=version
            )
            if on_pypi and not is_yanked
            else ""
        )
        github_url = (
            GITHUB_RELEASE_URL.format(repo_url=sources.repo_url, version=version)
            if on_github and sources.repo_url
            else ""
        )

        # Only gaps warn: versions that postdate the first release on that
        # platform but are absent from it.
        pypi_gap, github_gap = sources.platform_gaps(version)
        parsed = Version(version)
        elements.availability_admonition = availability_admonition(
            version,
            pypi_url=pypi_url,
            github_url=github_url,
            first_on_pypi=on_pypi and parsed == sources.first_pypi_version,
            first_on_github=on_github and parsed == sources.first_github_version,
            missing_pypi=pypi_gap,
            missing_github=github_gap,
        )

        if is_yanked:
            # PyPI accepts a yank with no reason, so the clause carrying it is
            # rendered only when there is one. Its own trailing period is
            # dropped: the template supplies the sentence's.
            reason = sources.pypi_data[version].yanked_reason.strip().rstrip(".")
            elements.yanked_admonition = render_template(
                "yanked-admonition",
                version=version,
                package=sources.pypi_data[version].package,
                reason=f": {reason}" if reason else "",
            )

        changelog.replace_section(
            version, render_template("release-notes", **asdict(elements))
        )


def lint_changelog_dates(
    changelog_path: Path,
    package: str | None = None,
    *,
    archive_path: Path | None = None,
    fix: bool = False,
    pypi_package_history: Sequence[str] = (),
    abandoned_versions: Sequence[str] = (),
    bullet_word_threshold: int = 0,
) -> int:
    """Verify that changelog release dates match canonical release dates.

    Uses PyPI upload dates as the canonical reference when the project
    is published to PyPI. Falls back to git tag dates for projects not
    on PyPI.

    Versions older than the first PyPI release are expected to be absent
    and logged at info level. Versions newer than the first PyPI release
    but missing from PyPI are unexpected and logged as warnings.

    Also detects **orphaned versions**: versions that exist as git tags,
    GitHub releases, or PyPI packages but have no corresponding changelog
    entry. Orphans are logged as warnings and cause a non-zero exit code.

    Four non-fatal content checks run first and never affect the exit code:
    {func}`warn_on_long_bullets`, {func}`warn_on_entry_order` and
    {func}`warn_on_positional_references` over the unreleased section, and
    {func}`warn_on_empty_sections` over the released ones. A fifth,
    {func}`check_tag_prefixed_headings`, fails the run on a heading no check
    can read, and rewrites it under `fix`.

    When `fix` is enabled, date mismatches are corrected in-place and
    admonitions are added to the changelog:

    - A `[!NOTE]` admonition listing available distribution links
      (PyPI, GitHub) for each version. Links are conditional: only
      sources where the version exists are included.
    - A `[!WARNING]` admonition listing platforms where the version
      is *not* available (missing from PyPI, GitHub, or both).
    - A `[!CAUTION]` admonition for yanked releases.
    - Placeholder sections for orphaned versions, with comparison URLs
      linking to adjacent versions.

    ```{caution}

    The `fix-changelog` workflow job skips this function during the
    release cycle (when `release_commits_matrix` is non-empty). At that
    point the release pipeline hasn't published to PyPI or created a
    GitHub release yet, so this function would incorrectly add "not
    available" admonitions to the freshly-released version.
    ```

    :param changelog_path: Path to the changelog file.
    :param archive_path: Optional path to a frozen changelog archive. Versions
        documented there are treated as present, suppressing false-positive
        orphan detection (and re-insertion under `fix`) for entries split out
        of the live changelog. Archived dates are not re-validated.
    :param package: PyPI package name. If `None`, auto-detected from
        `pyproject.toml`. If detection fails, falls back to git tags.
    :param fix: If True, fix dates and add admonitions to the file.
    :param pypi_package_history: Former PyPI package names for renamed
        projects. Releases from each former name are merged into the
        lookup table so versions published under old names are recognized.
        The current package name wins on version collisions.
    :param abandoned_versions: Versions documented in the changelog but
        never published. Each listed version is reported as skipped (info
        log) instead of triggering the `not found on PyPI` warning, for
        both the PyPI lookup and the git-tag fallback. Use for releases
        that were frozen but skipped per the "skip and move forward"
        practice (botched build, broken artifact).
    :param bullet_word_threshold: Word count above which an unreleased-section
        bullet triggers a non-fatal length warning (see
        {func}`warn_on_long_bullets`). `0` disables the check. Never affects
        the exit code.
    :return: `0` if all dates match or references were corrected in-place,
        `1` if any date mismatch, orphan or tag-prefixed heading is found
        without a fix being applied, {data}`REFUSED_EXIT_CODE` if the sanity
        gate refused a destructive rewrite because an upstream data source
        (GitHub Releases or PyPI) appeared to be returning incomplete or empty
        results while the existing changelog has substantial coverage on that
        platform.
    """
    changelog = Changelog(changelog_path.read_text(encoding="UTF-8"))
    # First, since a heading no check reads hides its release from the rest.
    prefixed_left = check_tag_prefixed_headings(changelog, fix=fix)
    if fix and write_if_changed(changelog_path, normalize_text(changelog.content)):
        logging.info(f"Updated {changelog_path}")
    warn_on_long_bullets(changelog, bullet_word_threshold)
    warn_on_empty_sections(changelog)
    warn_on_entry_order(changelog)
    warn_on_positional_references(changelog)
    releases = changelog.extract_all_releases()

    if not releases:
        logging.info("No released versions found in changelog.")
        return 1 if prefixed_left else 0

    sources = _reconfirm_live(
        changelog,
        releases,
        ReleaseSources.load(changelog, package, pypi_package_history),
        pypi_package_history,
        fix=fix,
    )
    if sources is None:
        return REFUSED_EXIT_CODE

    orphan_result = _reconcile_orphans(
        changelog, releases, sources, fix=fix, archive_path=archive_path
    )
    releases = orphan_result.releases

    date_check = _check_release_dates(
        releases, sources, fix=fix, abandoned_versions=abandoned_versions
    )
    has_mismatch = orphan_result.found or date_check.mismatched
    # Set whenever a flagged problem is left standing: no `--fix`, a fix that
    # could not run, or one that ran without landing. Kept apart from
    # `has_mismatch` so a partial repair still fails, where counting any single
    # file write as success would report green on a changelog still wrong.
    unfixed_problem = orphan_result.unfixed or date_check.unfixed

    if fix:
        if _sanity_gate_refuses(changelog, releases, sources):
            return REFUSED_EXIT_CODE
        _rebuild_admonitions(changelog, releases, sources, date_check.corrections)
        # A correction only counts as applied once the rendered section
        # carries it. `replace_section` cannot answer that on its own: it
        # reports "no change" both for a section it failed to touch and for one
        # that was already correct, so the date itself is the evidence.
        if any(
            changelog.decompose_version(version).date != date
            for version, date in date_check.corrections.items()
        ):
            unfixed_problem = True
        if write_if_changed(changelog_path, normalize_text(changelog.content)):
            logging.info(f"Updated {changelog_path}")

    # Success needs every flagged problem repaired, not merely some file write
    # having happened: a run that inserts one orphan but leaves another without
    # a comparison URL to link it from has not fixed the changelog, and letting
    # downstream steps proceed on that would publish the gap.
    return 1 if (has_mismatch and unfixed_problem) or prefixed_left else 0
