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

"""Changelog parsing, updating, and release lifecycle management.

This module is the single source of truth for all changelog management
decisions and operations. It handles two phases of the release cycle:

**Post-release (unfreeze)** — {meth}`Changelog.update`:

Decomposes the latest release section via {meth}`Changelog.decompose_version`,
transforms the elements into an unreleased entry (date → `unreleased`,
comparison URL → `...main`, body → development warning), renders via the
`release-notes` template, and prepends the result to the changelog.

**Release preparation (freeze)** — {meth}`Changelog.freeze`:

Decomposes the current unreleased section, sets the release date, freezes
the comparison URL to `...vX.Y.Z`, clears the development warning,
renders via the `release-notes` template, and replaces the section
in place.

Both operations follow the same decompose → modify → render → replace
pattern, with the `release-notes.md` template as the single source of
truth for section layout. Both are idempotent: re-running them produces
the same result. This is critical for CI workflows that may be retried.

```{note}
This is a custom implementation. After evaluating all major
alternatives — [towncrier](https://github.com/twisted/towncrier),
[commitizen](https://github.com/commitizen-tools/commitizen),
[python-semantic-release](https://github.com/python-semantic-release/python-semantic-release),
[generate-changelog](https://github.com/lob/generate-changelog),
[release-please](https://github.com/googleapis/release-please),
[scriv](https://github.com/nedbat/scriv), and
[git-changelog](https://github.com/pawamoy/git-changelog)
(see [issue #94](https://github.com/kdeldycke/repomatic/issues/94)) — none
were found to cover even half of the requirements.
```

Why not use an off-the-shelf tool?
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

Existing tools fall into two camps, neither of which fits:

**Commit-driven** tools ([python-semantic-release](https://github.com/python-semantic-release/python-semantic-release),
[commitizen](https://github.com/commitizen-tools/commitizen),
[generate-changelog](https://github.com/lob/generate-changelog),
[release-please](https://github.com/googleapis/release-please))
auto-generate changelogs from Git history. This conflicts with the
project's philosophy of hand-curated changelogs: entries are written
for *users*, consolidated by hand, and summarize only changes worth
knowing about. Auto-generated logs from developer commits are too
noisy and don't account for back-and-forth during development.

**Fragment-driven** tools ([towncrier](https://github.com/twisted/towncrier),
[scriv](https://github.com/nedbat/scriv)) avoid merge conflicts by using
per-change files, but handle none of the release orchestration:
comparison URL management, GFM warning lifecycle, workflow action
reference freezing, or the two-commit freeze/unfreeze release cycle.
The multiplication of files across the repo adds complexity, and
there is no 1:1 mapping between fragments and changelog entries.

Specific gaps across all evaluated tools:

- **No comparison URL management.** None generate GitHub
  `v1.0.0...v1.1.0` diff links, or update them from `...main`
  to `...vX.Y.Z` at release time.
- **No unreleased section lifecycle.** None manage the
  `[!WARNING]` GFM alert warning that the version is under
  active development, inserting it post-release and removing it at
  release time.
- **No workflow action reference freezing.** None handle the
  freeze/unfreeze cycle for `@main` ↔ `@vX.Y.Z` references
  in workflow files.
- **No two-commit release workflow.** None support the freeze
  commit (`[changelog] Release vX.Y.Z`) plus unfreeze commit
  (`[changelog] Post-release bump`) pattern that
  `changelog.yaml` uses.
- **No citation file integration.** None update `citation.cff`
  release dates.
- **No version bump eligibility checks.** None prevent double
  version increments by comparing the current version against the
  latest Git tag with a commit-message fallback.

The custom implementation in this module is tightly integrated with the
release workflow. Adopting any external tool would require keeping most
of this code *and* adding a new dependency — more complexity, not less.

Related modules
^^^^^^^^^^^^^^^

- {mod}`repomatic.release.prepare_release` orchestrates the full release
  preparation across changelog, citation, and workflow files, delegating
  changelog operations to this module.
- {mod}`repomatic.lint_changelog` checks the release dates and the
  availability notes of every section against PyPI, GitHub and git tags.
- {mod}`repomatic.metadata` handles version bump eligibility checks and
  release commit identification.
- `changelog.yaml` workflow drives the two-commit release PR.
"""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, replace
from functools import cache
from pathlib import Path
from textwrap import indent

from packaging.version import Version

from .file_ops import write_if_changed
from .humanize import today_utc
from .templating import render_template
from .versions import safe_version

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Sequence

    from .config import Config


def resolved_changelog_path(config: Config) -> Path:
    """Absolute path of the configured changelog.

    The one derivation of `[tool.repomatic] changelog.location` into a
    filesystem path, so every consumer (the CLI commands, the release freeze,
    the metadata singleton, the setup guide) resolves the same file the same
    way.
    """
    return Path(config.changelog_location).resolve()


def load_changelog_repo(config: Config) -> tuple[Path, str] | None:
    """Load the configured changelog and read the repository URL out of it.

    The shared preamble of the two release-notes syncers, which both need the
    changelog's path (to read sections from) and the repository it belongs to
    (to address the GitHub API with). The URL comes from the changelog's own
    comparison links rather than from the git remote, so a project whose
    changelog points elsewhere stays authoritative.

    :param config: The resolved `[tool.repomatic]` configuration.
    :return: `(changelog_path, repo_url)`, or `None` after logging why neither
        could be determined (no changelog on disk, or no comparison link in it).
    """
    changelog_path = resolved_changelog_path(config)
    if not changelog_path.exists():
        logging.warning(f"{changelog_path} not found.")
        return None
    repo_url = Changelog(changelog_path.read_text(encoding="UTF-8")).extract_repo_url()
    if not repo_url:
        logging.warning("Could not extract repository URL from changelog.")
        return None
    return changelog_path, repo_url


AVAILABLE_VERB = "is available on"
"""Verb phrase for versions present on a platform."""

CHANGELOG_HEADER = "# Changelog\n"
"""Default changelog header for empty changelogs."""

FIRST_AVAILABLE_VERB = "is the *first version* available on"
"""Verb phrase for the inaugural release on a platform."""

GITHUB_LABEL = "🐙 GitHub"
"""Display label for GitHub releases in admonitions."""

GITHUB_RELEASE_URL = "{repo_url}/releases/tag/v{version}"
"""GitHub release page URL for a specific version."""

NOT_AVAILABLE_VERB = "is **not available** on"
"""Verb phrase for versions missing from a platform."""

PYPI_LABEL = "🐍 PyPI"
"""Display label for PyPI in admonitions."""

SECTION_START = "##"
"""Markdown heading level for changelog version sections."""

YANKED_DEDUP_MARKER = "yanked from PyPI"
"""Dedup marker for the yanked admonition to prevent duplicate insertion."""

# Patterns below are derived from the constants above, so they follow them
# instead of joining the alphabetical block.

RELEASE_VERSION_TOKEN = r"\d+\.\d+\.\d+"
"""Regex fragment for a final release version, like `1.2.3`.

The strict half of the version vocabulary: it deliberately rejects the
`.devN` suffix {data}`VERSION_TOKEN` accepts, because a changelog documents
only final releases. Anything keyed off a *published* version (a dated
heading, a comparison URL) uses this one.
"""

VERSION_TOKEN = rf"{RELEASE_VERSION_TOKEN}(?:\.\w+)?"
"""Regex fragment for any version a `##` heading may carry.

Widens {data}`RELEASE_VERSION_TOKEN` with the trailing `.devN` a
development section carries between releases. Anything enumerating or
locating headings uses this one, so an unreleased section is never
invisible to a scan that has to account for it.
"""

GFM_ALERT_RE = re.compile(r"(?:^>.*$\n?)+", re.MULTILINE)
"""A GFM alert block: consecutive lines starting with `>`.

The shape every changelog admonition takes (the development warning, the PyPI
availability note, a yanked caution), matched wherever a section's blocks are
inventoried.
"""

VERSION_COMPARE_PATTERN = re.compile(
    rf"v{RELEASE_VERSION_TOKEN}\.\.\.v{RELEASE_VERSION_TOKEN}"
)
"""Pattern matching GitHub comparison URLs like `v1.0.0...v1.0.1`."""

RELEASED_VERSION_PATTERN = re.compile(
    rf"^{SECTION_START}\s*\[`?(?P<version>{RELEASE_VERSION_TOKEN})`?"
    rf"\s+\((?P<date>\d{{4}}-\d{{2}}-\d{{2}})\)\]",
    re.MULTILINE,
)
"""Pattern matching released version headings with dates.

Captures version and date from headings like
``## [`5.9.1` (2026-02-14)](...)``. Skips unreleased versions which
use `(unreleased)` instead of a date. Backticks around the version
are optional.
"""

HEADING_PARTS_PATTERN = re.compile(
    rf"^{SECTION_START}\s*\[`?(?P<version>{VERSION_TOKEN})`?\s+"
    rf"\((?P<date>[^)]+)\)\]"
    rf"\((?P<url>[^)]+)\)",
    re.MULTILINE,
)
"""Pattern extracting version, date/label, and URL from a heading.

Used by {meth}`Changelog.decompose_version` to populate the heading
fields of {class}`VersionElements`.
"""

TAG_PREFIXED_HEADING_RE = re.compile(
    rf"^(?P<prefix>{SECTION_START}\s*\[`?)v(?P<version>{VERSION_TOKEN})\b",
    re.MULTILINE,
)
"""A `##` heading naming its version with the `v` of a git tag.

No scan reads such a heading: {data}`VERSION_TOKEN` opens on a digit, so the
section stays invisible to every check, and its release goes unverified. The
prefix belongs to the tag; a heading names the package version, bare.
"""


def _heading_fragment(version: str | None = None) -> str:
    """Regex fragment matching a `##` version heading up to its version token.

    The shared prefix of every heading-keyed lookup in this module. Callers
    append whatever trails the version: nothing (locate a heading), the rest of
    the line plus a body (a whole section), or a comparison URL.

    :param version: Match this exact version. When `None`, matches any version
        a heading may carry and captures it as the `version` group.
    :return: An uncompiled fragment, meant to be used with {data}`re.MULTILINE`.
    """
    token = re.escape(version) if version else rf"(?P<version>{VERSION_TOKEN})"
    return rf"^{SECTION_START}\s*\[`?{token}`?\s"


@cache
def _heading_re(version: str | None = None) -> re.Pattern[str]:
    """Compile {func}`_heading_fragment` on its own, to locate headings.

    Memoized: the lint loop asks for the same versions' patterns over and
    over.

    :param version: Match this exact version, or any version when `None`.
    :return: A compiled pattern whose `version` group holds the version, for
        the `None` case.
    """
    return re.compile(_heading_fragment(version), re.MULTILINE)


@cache
def _section_re(version: str | None = None) -> re.Pattern[str]:
    """Compile a pattern spanning a whole version section.

    Matches from the heading line of *version* down to the next `##` heading
    or the end of the content, whichever comes first. Everything below the
    heading line is captured as the `body` group. Memoized like
    {func}`_heading_re`.

    :param version: The version whose section to match, or `None` to match
        every section, its version captured as the `version` group.
    :return: A compiled pattern.
    """
    return re.compile(
        rf"{_heading_fragment(version)}[^\n]+\n"
        rf"(?P<body>.*?)(?=^{SECTION_START}|\Z)",
        re.MULTILINE | re.DOTALL,
    )


@dataclass
class VersionElements:
    """Discrete building blocks of a changelog version section.

    Each field is a pre-formatted markdown block (or empty string when absent).
    Templates compose these elements into the final section layout. Empty
    variables produce empty strings, which `render_template`'s 3+ newline
    collapsing handles gracefully.

    Heading fields (`compare_url`, `date`, `version`) are populated by
    {meth}`Changelog.decompose_version` and used by the `release-notes`
    template to render the `##` heading line. Body fields are unchanged.
    """

    compare_url: str = ""
    """GitHub comparison URL from the heading (e.g. `repo/compare/vA...vB`)."""

    date: str = ""
    """Release date or `unreleased` label from the heading."""

    version: str = ""
    """Version string extracted from the heading (e.g. `1.2.3`)."""

    # Body fields.

    availability_admonition: str = ""
    """`[!NOTE]` or `[!WARNING]` block for platform availability."""

    changes: str = ""
    """Hand-written changelog entries (bullet points, prose)."""

    development_warning: str = ""
    """`[!WARNING]` block for unreleased versions under active development."""

    editorial_admonition: str = ""
    """Hand-written GFM alert blocks not matching auto-generated patterns.

    Multiple blocks are joined with double newlines.
    """

    yanked_admonition: str = ""
    """`[!CAUTION]` block for releases yanked from PyPI."""

    @property
    def has_body(self) -> bool:
        """Whether the section carries anything below its heading."""
        return any((
            self.changes,
            self.availability_admonition,
            self.development_warning,
            self.editorial_admonition,
            self.yanked_admonition,
        ))

    @property
    def bullets(self) -> list[str]:
        """The top-level entries of {attr}`changes`, one string per `-` bullet.

        Each entry holds its marker line plus the wrapped continuation lines
        and indented sub-bullets under it, joined with newlines. Blank lines
        and prose outside a bullet are dropped.
        """
        bullets: list[str] = []
        current: list[str] = []
        for line in self.changes.splitlines():
            if re.match(r"^- ", line):
                if current:
                    bullets.append("\n".join(current))
                current = [line]
            elif current and (not line.strip() or line[:1] in {" ", "\t"}):
                # Wrapped continuation, indented sub-bullet, or interior blank line.
                current.append(line)
            elif current:
                # A non-indented, non-bullet line closes the current entry.
                bullets.append("\n".join(current))
                current = []
        if current:
            bullets.append("\n".join(current))
        return bullets

    @property
    def tag_range(self) -> str:
        """The tag range the comparison URL names, like `v1.1.0...v2.0.0`.

        Empty when the heading carries no comparison URL.
        """
        return self.compare_url.rsplit("/compare/", 1)[-1] if self.compare_url else ""


class Changelog:
    """Helpers to manipulate changelog files written in Markdown.

    Every lookup of a section goes through an index of the sections, built in
    one scan of {attr}`content` and dropped whenever the content changes. The
    lint loop reads each section several times: a scan of the whole changelog
    per read would make it quadratic in the number of releases.
    """

    _content: str
    _sections: dict[str, re.Match[str]] | None
    _elements: dict[str, VersionElements]

    def __init__(
        self,
        initial_changelog: str | None = None,
        current_version: str | None = None,
    ) -> None:
        self.content = initial_changelog or CHANGELOG_HEADER
        self.current_version = current_version

    @property
    def content(self) -> str:
        """The Markdown source of the changelog."""
        return self._content

    @content.setter
    def content(self, value: str) -> None:
        self._content = value
        self._sections = None
        self._elements = {}

    def _section(self, version: str) -> re.Match[str] | None:
        """Return the match spanning the section of *version*.

        Read from the section index, built on the first lookup after a change
        of {attr}`content`. A version the index cannot hold, because its shape
        falls outside {data}`VERSION_TOKEN`, is searched for directly.

        :param version: Version string (like `1.2.3`).
        :return: The first section carrying *version*, or `None`.
        """
        if self._sections is None:
            self._sections = {}
            for section in _section_re().finditer(self._content):
                self._sections.setdefault(section["version"], section)
        match = self._sections.get(version)
        if match is None:
            match = _section_re(version).search(self._content)
        return match

    def update(self, default_branch: str = "main") -> bool:
        """Add a new unreleased entry at the top of the changelog.

        Decomposes the current version section, transforms it into an
        unreleased entry (date set to `unreleased`, comparison URL
        retargeted to the default branch, body replaced with the
        development warning), and prepends it to the changelog.

        Idempotent: the content stays unchanged when an unreleased entry
        already exists, and when there is no current version or no section
        to clone.

        :param default_branch: Branch name for the comparison URL. Must match
            what {meth}`freeze` is later given, since the two halves of a
            release cycle retarget the same URL in opposite directions: a
            mismatch leaves the released section pointing at a branch that
            does not exist.
        :return: `True` if an entry was added to {attr}`content`.
        """
        if not self.current_version:
            return False

        elements = self.decompose_version(self.current_version)

        # Idempotent: skip if an unreleased section already exists.
        if elements.date == "unreleased":
            return False

        if not elements.version:
            # No existing section to clone.
            return False

        # Transform frozen entry into an unreleased entry.
        elements.date = "unreleased"
        elements.compare_url = VERSION_COMPARE_PATTERN.sub(
            f"v{self.current_version}...{default_branch}",
            elements.compare_url,
        )
        elements.development_warning = render_template("development-warning")
        elements.changes = ""
        elements.availability_admonition = ""
        elements.editorial_admonition = ""
        elements.yanked_admonition = ""

        new_entry = render_template("release-notes", **asdict(elements))
        logging.info("New generated section:\n" + indent(new_entry, " " * 2))

        # A section exists, so the content holds a heading to split on.
        header, heading_mark, history = self.content.partition(SECTION_START)
        self.content = header + new_entry + "\n\n" + heading_mark + history
        return True

    def freeze(
        self,
        release_date: str | None = None,
        default_branch: str = "main",
    ) -> bool:
        """Freeze the current unreleased section for release.

        Decomposes the current version section, sets the release date,
        freezes the comparison URL to the release tag, clears the
        development warning, and re-renders via the `release-notes`
        template.

        Returns `False` for three different situations, only one of which is
        benign: an already-frozen section (idempotent no-op), no version to
        freeze, and a version whose section is missing. The last one is what a
        release would otherwise ship an `(unreleased)` heading over, so it is
        logged as a warning rather than left to look like the no-op.

        :param release_date: Date in `YYYY-MM-DD` format.
            Defaults to today (UTC).
        :param default_branch: Branch name for comparison URL. Must match what
            {meth}`update` used to write it, per that method's note.
        :return: True if the content was modified.
        """
        if release_date is None:
            release_date = today_utc().isoformat()

        if not self.current_version:
            logging.warning("No current version set: nothing to freeze.")
            return False

        elements = self.decompose_version(self.current_version)
        if not elements.version:
            logging.warning(
                f"⚠ No changelog section found for version"
                f" {self.current_version}: nothing was frozen. The release will"
                " ship without a dated entry unless the heading is fixed to"
                " match the version being released."
            )
            return False

        # Already frozen: nothing to do.
        if elements.date != "unreleased":
            return False

        elements.date = release_date
        elements.compare_url = elements.compare_url.replace(
            f"...{default_branch}", f"...v{self.current_version}"
        )
        elements.development_warning = ""

        new_section = render_template("release-notes", **asdict(elements))
        return self.replace_section(self.current_version, new_section)

    @classmethod
    def freeze_file(
        cls,
        path: Path,
        version: str,
        release_date: str | None = None,
        default_branch: str = "main",
    ) -> bool:
        """Freeze a changelog file in place.

        Reads the file, applies all freeze operations via
        {meth}`freeze`, and writes the result back.

        :param path: Path to the changelog file.
        :param version: Current version string.
        :param release_date: Date in `YYYY-MM-DD` format.
            Defaults to today (UTC).
        :param default_branch: Branch name for comparison URL.
        :return: True if the file was modified.
        """
        if not path.exists():
            logging.warning(f"Changelog not found: {path}")
            return False

        original = path.read_text(encoding="UTF-8")
        changelog = cls(original, current_version=version)
        if not changelog.freeze(
            release_date=release_date, default_branch=default_branch
        ):
            logging.debug(f"No changes to {path}")
            return False

        return write_if_changed(
            path, changelog.content.rstrip() + "\n", previous=original
        )

    def extract_repo_url(self) -> str:
        """Extract the repository URL from changelog comparison links.

        Parses the first `## [...](<repo_url>/compare/...)` heading
        and returns the base repository URL (e.g.
        `https://github.com/user/repo`).

        :return: The repository URL, or empty string if not found.
        """
        match = re.search(
            rf"^{SECTION_START}\s*\[.+?\]\((?P<repo>https?://[^/]+/[^/]+/[^/]+)/compare/",
            self.content,
            flags=re.MULTILINE,
        )
        if not match:
            return ""
        return match.group("repo")

    def extract_all_releases(self) -> list[tuple[str, str]]:
        """Extract all released versions and their dates from the changelog.

        Scans for headings matching `## [X.Y.Z (YYYY-MM-DD)](...)`.
        Unreleased versions (with `(unreleased)`) are skipped.

        :return: List of `(version, date)` tuples ordered as they
            appear in the changelog (newest first).
        """
        return RELEASED_VERSION_PATTERN.findall(self.content)

    def is_first_release(self, version: str) -> bool:
        """Whether no released section of the changelog predates *version*.

        :param version: Version string (like `1.2.3`).
        :return: `False` when *version* does not parse, or when an older version
            carries a dated section.
        """
        parsed = safe_version(version)
        return parsed is not None and not any(
            (released := safe_version(other)) is not None and released < parsed
            for other, _date in self.extract_all_releases()
        )

    def extract_all_version_headings(self) -> set[str]:
        """Extract all version strings from `##` headings.

        Includes both released and unreleased versions, so the caller
        can avoid false-positive orphan detection for the current
        development version.

        :return: Set of version strings found in headings.
        """
        return {match["version"] for match in _heading_re().finditer(self.content)}

    def tag_prefixed_versions(self) -> list[str]:
        """The versions of the `##` headings written with a `v` tag prefix.

        :return: The bare versions, in document order.
        """
        return [
            match["version"] for match in TAG_PREFIXED_HEADING_RE.finditer(self.content)
        ]

    def strip_tag_prefixes(self) -> int:
        """Drop the `v` tag prefix off every `##` heading carrying one.

        The comparison URL of the heading keeps its `v`: that one names a tag.

        :return: The number of headings rewritten.
        """
        self.content, count = TAG_PREFIXED_HEADING_RE.subn(
            r"\g<prefix>\g<version>", self.content
        )
        return count

    def insert_version_section(
        self,
        version: str,
        date: str,
        repo_url: str,
        all_versions: list[str],
    ) -> bool:
        """Insert a placeholder section for a missing version.

        The section is placed at the correct position in descending
        version order. The comparison URL points from the next-lower
        version to this one. After insertion, the next-higher version's
        comparison URL base is updated to reference this version, keeping
        the timeline coherent.

        Idempotent: returns False if the version heading already exists.

        :param version: Version string (like `1.2.3`).
        :param date: Release date in `YYYY-MM-DD` format.
        :param repo_url: Repository URL for comparison links.
        :param all_versions: All known versions, in any order. A version that
            does not parse as PEP 440 takes no part in the ordering.
        :return: True if the content was modified.
        """
        # Idempotent: skip if already present.
        if version in self.extract_all_version_headings():
            return False

        parsed = Version(version)
        known = sorted(
            (parsed_other, other)
            for other in all_versions
            if (parsed_other := safe_version(other)) is not None
        )

        # The next-lower version is the comparison URL base.
        lower_version = next(
            (other for parsed_other, other in reversed(known) if parsed_other < parsed),
            None,
        )
        compare_base = f"v{lower_version}" if lower_version else "v0.0.0"
        elements = VersionElements(
            compare_url=f"{repo_url}/compare/{compare_base}...v{version}",
            date=date,
            version=version,
        )
        heading = render_template("release-notes", **asdict(elements)) + "\n"

        # Find the right insertion point: before the first heading whose
        # version is lower than this one. Development headings count, so a
        # changelog carrying only a `.devN` section still orders correctly
        # instead of falling through to the append-at-end branch.
        insert_pos = None
        for match in _heading_re().finditer(self.content):
            existing = safe_version(match["version"])
            if existing is not None and existing < parsed:
                insert_pos = match.start()
                break

        if insert_pos is not None:
            self.content = (
                self.content[:insert_pos] + heading + "\n" + self.content[insert_pos:]
            )
        else:
            # Append at the end (oldest version).
            self.content = self.content.rstrip() + "\n\n" + heading

        # Update the next-higher version's comparison URL to point to
        # this newly inserted version.
        higher_version = next(
            (other for parsed_other, other in known if parsed_other > parsed), None
        )
        if higher_version:
            self.update_comparison_base(higher_version, version)

        return True

    def update_comparison_base(self, version: str, new_base: str) -> bool:
        """Replace the base version in a version heading's comparison URL.

        Changes `compare/vOLD...vX.Y.Z` to `compare/vNEW...vX.Y.Z`
        in the heading for the given version.

        :param version: The version whose heading to update.
        :param new_base: New base version (without `v` prefix).
        :return: True if the content was modified.
        """
        pattern = re.compile(
            rf"({_heading_fragment(version)}[^\n]*/compare/)"
            rf"v{RELEASE_VERSION_TOKEN}(\.\.\.v{re.escape(version)}\))",
            re.MULTILINE,
        )
        updated = pattern.sub(rf"\g<1>v{new_base}\g<2>", self.content, count=1)
        if updated == self.content:
            return False
        self.content = updated
        return True

    def decompose_version(self, version: str) -> VersionElements:
        """Decompose a version section into discrete elements.

        Parses both the heading (version, date, URL) and the body
        (admonitions, changes).

        Classifies each GFM alert block (consecutive `>` lines) as one
        of the auto-generated element types. Everything not classified
        as auto-generated is preserved as `changes`.

        The decomposition is cached until {attr}`content` changes. Each call
        returns its own copy, which the caller is free to modify.

        :param version: Version string (like `1.2.3`).
        :return: A {class}`VersionElements` with each field populated.
        """
        elements = self._elements.get(version)
        if elements is None:
            elements = self._elements[version] = self._decompose(version)
        return replace(elements)

    def _decompose(self, version: str) -> VersionElements:
        """Decompose a version section, without caching.

        See {meth}`decompose_version`.
        """
        section_match = self._section(version)
        if not section_match:
            return VersionElements()

        # Extract heading fields.
        heading_match = HEADING_PARTS_PATTERN.search(section_match.group(0))
        elements = VersionElements()
        if heading_match:
            elements.compare_url = heading_match.group("url")
            elements.date = heading_match.group("date")
            elements.version = heading_match.group("version")

        body = section_match["body"].strip()
        if not body:
            return elements

        # Track regions to exclude from changes.
        exclude_regions: list[tuple[int, int]] = []
        # Accumulate availability admonitions (NOTE + WARNING can coexist).
        availability_parts: list[str] = []
        # Accumulate editorial (hand-written) admonitions.
        editorial_parts: list[str] = []

        for block_match in GFM_ALERT_RE.finditer(body):
            block_text = block_match.group(0)
            exclude_regions.append((block_match.start(), block_match.end()))
            if "not released yet" in block_text:
                elements.development_warning = block_text.rstrip("\n")
            elif YANKED_DEDUP_MARKER in block_text:
                elements.yanked_admonition = block_text.rstrip("\n")
            elif f"> `{version}` is " in block_text:
                availability_parts.append(block_text.rstrip("\n"))
            else:
                # Hand-written admonition, not auto-generated.
                editorial_parts.append(block_text.rstrip("\n"))

        elements.availability_admonition = "\n\n".join(availability_parts)
        elements.editorial_admonition = "\n\n".join(editorial_parts)

        # Build changes from everything not in excluded regions.
        parts: list[str] = []
        prev_end = 0
        for start, end in exclude_regions:
            parts.append(body[prev_end:start])
            prev_end = end
        parts.append(body[prev_end:])
        changes = "".join(parts)
        # Collapse runs of 3+ newlines and strip.
        changes = re.sub(r"\n{3,}", "\n\n", changes).strip()
        elements.changes = changes

        return elements

    def replace_section(self, version: str, new_section: str) -> bool:
        """Replace the entire section (heading + body) for a version.

        Locates the version heading and replaces everything up to the
        next `##` heading (or EOF) with `new_section`.

        :param version: Version string (like `1.2.3`).
        :param new_section: New section content including heading.
        :return: True if the content was modified.
        """
        match = self._section(version)
        if not match:
            return False
        old_section = match.group(0)
        # Normalize: ensure trailing newlines for consistent formatting.
        formatted = new_section.rstrip() + "\n\n"
        if old_section == formatted:
            return False
        self.content = (
            self.content[: match.start()] + formatted + self.content[match.end() :]
        )
        return True


def _platform_admonition(
    template: str, version: str, verb: str, platforms: Sequence[str]
) -> str:
    """Render a GFM admonition naming the platforms a version stands on.

    The shared body of the two blocks {func}`availability_admonition` renders:
    both state the same sentence about the same version over a different set
    of platforms, and both render nothing when that set is empty.

    :param template: Template name supplying the alert type and sentence.
    :param version: Version string (e.g. `1.2.3`).
    :param verb: Verb phrase joining the version to its platforms.
    :param platforms: Platform labels, already linked where they should be.
    :return: The rendered admonition block, or an empty string when
        *platforms* is empty.
    """
    if not platforms:
        return ""
    return render_template(
        template,
        version=version,
        verb=verb,
        platforms=" and ".join(platforms),
    )


def availability_admonition(
    version: str,
    *,
    pypi_url: str = "",
    github_url: str = "",
    first_on_pypi: bool = False,
    first_on_github: bool = False,
    missing_pypi: bool = False,
    missing_github: bool = False,
) -> str:
    """Render the availability admonitions of a release section.

    The one renderer of {attr}`VersionElements.availability_admonition`. Two
    writers share it, so a release reads the same in both: `lint-changelog
    --fix` writes it into the changelog, and the draft GitHub release body
    bakes it in before that fix runs.

    A `> [!NOTE]` links the platforms carrying the version. It uses the
    *first version* wording when the version is the first one on every
    platform it links. A `> [!WARNING]` names the platforms missing the
    version. Both blocks appear when a version is on one platform and missing
    from the other.

    :param version: Version string (like `1.2.3`).
    :param pypi_url: PyPI page of the version, empty to leave PyPI out of the
        note.
    :param github_url: GitHub release page of the version, empty to leave
        GitHub out of the note.
    :param first_on_pypi: Whether the version is the first one on PyPI.
    :param first_on_github: Whether the version is the first GitHub release.
    :param missing_pypi: Whether the version is missing from PyPI.
    :param missing_github: Whether the version is missing from GitHub.
    :return: The admonition blocks, separated by a blank line. Empty when the
        version is neither linked nor missing anywhere.
    """
    linked = [
        (f"[{label}]({url})", first)
        for label, url, first in (
            (PYPI_LABEL, pypi_url, first_on_pypi),
            (GITHUB_LABEL, github_url, first_on_github),
        )
        if url
    ]
    first_on_all = bool(linked) and all(first for _link, first in linked)
    note = _platform_admonition(
        "available-admonition",
        version,
        FIRST_AVAILABLE_VERB if first_on_all else AVAILABLE_VERB,
        [link for link, _first in linked],
    )
    missing = [
        label
        for label, gap in ((PYPI_LABEL, missing_pypi), (GITHUB_LABEL, missing_github))
        if gap
    ]
    warning = _platform_admonition(
        "unavailable-admonition", version, NOT_AVAILABLE_VERB, missing
    )
    return "\n\n".join(block for block in (note, warning) if block)
