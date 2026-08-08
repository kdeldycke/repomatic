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

**Fragment-driven** tools ([towncrier](https://github.com/twisted/towncrier), [scriv](https://github.com/nedbat/scriv)) avoid merge conflicts by using
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

- `prepare_release.py` orchestrates the full release preparation
  across changelog, citation, and workflow files, delegating
  changelog operations to this module.
- `metadata.py` handles version bump eligibility checks and
  release commit identification.
- `changelog.yaml` workflow drives the two-commit release PR.
"""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from textwrap import indent

from packaging.version import Version

from .git_ops import get_all_version_tags, get_tag_date
from .github.actions import AnnotationLevel, emit_annotation
from .github.pr_body import render_template
from .github.releases import (
    GitHubRelease,
    GitHubReleasesUnavailable,
    get_github_releases,
)
from .pypi import (
    PYPI_LABEL,
    PYPI_PROJECT_URL,
    PyPIRelease,
    get_release_dates as get_pypi_release_dates,
)
from .pyproject import get_project_name

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Sequence

    from .config import Config


def resolved_changelog_path(config: Config) -> Path:
    """Absolute path of the configured changelog.

    The one derivation of `[tool.repomatic] changelog.location` into a
    filesystem path, so every command resolves the same file the same way
    (two call sites used to resolve the path and two did not).
    """
    return Path(config.changelog_location).resolve()


AVAILABLE_VERB = "is available on"
"""Verb phrase for versions present on a platform."""

CHANGELOG_HEADER = "# Changelog\n"
"""Default changelog header for empty changelogs."""

EMPTY_PYPI_SANITY_THRESHOLD = 3
"""Minimum number of existing PyPI links in the changelog above which an
empty PyPI lookup is treated as a transient failure rather than a genuine
"package has no releases" state.

```{note}
Two layers of ambiguity make this threshold necessary:

1. {func}`repomatic.pypi._fetch_json` returns `None` on every failure mode
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

FIRST_AVAILABLE_VERB = "is the *first version* available on"
"""Verb phrase for the inaugural release on a platform."""

GITHUB_LABEL = "🐙 GitHub"
"""Display label for GitHub releases in admonitions."""

GITHUB_RELEASE_URL = "{repo_url}/releases/tag/v{version}"
"""GitHub release page URL for a specific version."""

NOT_AVAILABLE_VERB = "is **not available** on"
"""Verb phrase for versions missing from a platform."""

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


def _heading_re(version: str | None = None) -> re.Pattern[str]:
    """Compile {func}`_heading_fragment` on its own, to locate headings.

    :param version: Match this exact version, or any version when `None`.
    :return: A compiled pattern whose `version` group holds the version, for
        the `None` case.
    """
    return re.compile(_heading_fragment(version), re.MULTILINE)


def _section_re(version: str) -> re.Pattern[str]:
    """Compile a pattern spanning a whole version section.

    Matches from the heading line of *version* down to the next `##` heading
    or the end of the content, whichever comes first. Everything below the
    heading line is captured as the `body` group.

    :param version: The version whose section to match.
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


class Changelog:
    """Helpers to manipulate changelog files written in Markdown."""

    def __init__(
        self,
        initial_changelog: str | None = None,
        current_version: str | None = None,
    ) -> None:
        if not initial_changelog:
            self.content = CHANGELOG_HEADER
        else:
            self.content = initial_changelog
        self.current_version = current_version
        logging.debug(f"Initial content set to:\n{self.content}")

    def update(self, default_branch: str = "main") -> str:
        """Add a new unreleased entry at the top of the changelog.

        Decomposes the current version section, transforms it into an
        unreleased entry (date set to `unreleased`, comparison URL
        retargeted to the default branch, body replaced with the
        development warning), and prepends it to the changelog.

        Idempotent: returns the current content unchanged if an
        unreleased entry already exists.

        :param default_branch: Branch name for the comparison URL. Must match
            what {meth}`freeze` is later given, since the two halves of a
            release cycle retarget the same URL in opposite directions: a
            mismatch leaves the released section pointing at a branch that
            does not exist.
        :return: The updated changelog content.
        """
        if not self.current_version:
            return self.content.rstrip()

        elements = self.decompose_version(self.current_version)

        # Idempotent: skip if an unreleased section already exists.
        if elements.date == "unreleased":
            return self.content.rstrip()

        if not elements.version:
            # No existing section to clone. Return header only.
            return self.content.rstrip()

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

        # Split header from sections.
        sections = self.content.split(SECTION_START, 1)
        changelog_header = sections[0] if sections else f"{CHANGELOG_HEADER}\n"
        history = f"{SECTION_START}{sections[1]}" if len(sections) > 1 else ""

        return (changelog_header + new_entry + "\n\n" + history).rstrip()

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
            release_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

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

        path.write_text(changelog.content.rstrip() + "\n", encoding="UTF-8")
        logging.info(f"Updated {path}")
        return True

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

    def extract_all_version_headings(self) -> set[str]:
        """Extract all version strings from `##` headings.

        Includes both released and unreleased versions, so the caller
        can avoid false-positive orphan detection for the current
        development version.

        :return: Set of version strings found in headings.
        """
        return {match["version"] for match in _heading_re().finditer(self.content)}

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

        :param version: Version string (e.g. `1.2.3`).
        :param date: Release date in `YYYY-MM-DD` format.
        :param repo_url: Repository URL for comparison links.
        :param all_versions: All known versions sorted descending.
        :return: True if the content was modified.
        """
        # Idempotent: skip if already present.
        if version in self.extract_all_version_headings():
            return False

        parsed = Version(version)

        # Find the next-lower version for the comparison URL base.
        lower_version = None
        for v in sorted(all_versions, key=Version, reverse=True):
            if Version(v) < parsed:
                lower_version = v
                break

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
            existing = Version(match["version"])
            if existing < parsed:
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
        higher_version = None
        for v in sorted(all_versions, key=Version):
            if Version(v) > parsed:
                higher_version = v
                break
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

        :param version: Version string (e.g. `1.2.3`).
        :return: A {class}`VersionElements` with each field populated.
        """
        section_match = _section_re(version).search(self.content)
        if not section_match:
            return VersionElements()

        # Extract heading fields.
        heading_match = HEADING_PARTS_PATTERN.search(
            self.content[section_match.start() : section_match.end()]
        )
        elements = VersionElements()
        if heading_match:
            elements.compare_url = heading_match.group("url")
            elements.date = heading_match.group("date")
            elements.version = heading_match.group("version")

        body = section_match["body"].strip()
        if not body:
            return elements
        # Match GFM alert blocks: consecutive lines starting with "> ".
        admonition_pattern = re.compile(
            r"(?:^>.*$\n?)+",
            re.MULTILINE,
        )

        # Track regions to exclude from changes.
        exclude_regions: list[tuple[int, int]] = []
        # Accumulate availability admonitions (NOTE + WARNING can coexist).
        availability_parts: list[str] = []
        # Accumulate editorial (hand-written) admonitions.
        editorial_parts: list[str] = []

        for block_match in admonition_pattern.finditer(body):
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

        :param version: Version string (e.g. `1.2.3`).
        :param new_section: New section content including heading.
        :return: True if the content was modified.
        """
        match = _section_re(version).search(self.content)
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

    The shared body of {func}`build_release_admonition` and
    {func}`build_unavailable_admonition`: both state the same sentence about
    the same version over a different set of platforms, and both render nothing
    when that set is empty.

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


def build_release_admonition(
    version: str,
    *,
    pypi_url: str = "",
    github_url: str = "",
    first_on_all: bool = False,
) -> str:
    """Build a GFM release admonition with available distribution links.

    :param version: Version string (e.g. `1.2.3`).
    :param pypi_url: PyPI project URL, or empty if not on PyPI.
    :param github_url: GitHub release URL, or empty if no release exists.
    :param first_on_all: Whether every listed platform is a first appearance.
        When `True`, uses "is the *first version* available on" wording.
    :return: A `> [!NOTE]` admonition block, or empty string if neither
        URL is provided.
    """
    links = [
        f"[{label}]({url})"
        for label, url in ((PYPI_LABEL, pypi_url), (GITHUB_LABEL, github_url))
        if url
    ]
    return _platform_admonition(
        "available-admonition",
        version,
        FIRST_AVAILABLE_VERB if first_on_all else AVAILABLE_VERB,
        links,
    )


def build_unavailable_admonition(
    version: str,
    *,
    missing_pypi: bool = False,
    missing_github: bool = False,
) -> str:
    """Build a GFM warning admonition for platforms missing a version.

    :param version: Version string (e.g. `1.2.3`).
    :param missing_pypi: Whether the version is missing from PyPI.
    :param missing_github: Whether the version is missing from GitHub.
    :return: A `> [!WARNING]` admonition block, or empty string if
        neither platform is missing.
    """
    names = [
        label
        for label, missing in (
            (PYPI_LABEL, missing_pypi),
            (GITHUB_LABEL, missing_github),
        )
        if missing
    ]
    return _platform_admonition(
        "unavailable-admonition", version, NOT_AVAILABLE_VERB, names
    )


def split_changelog_bullets(changes: str) -> list[str]:
    """Split a version section's change body into top-level bullet entries.

    Each returned item is one entry: its `-` marker line plus any wrapped
    continuation lines and indented sub-bullets, joined with newlines.
    Blank lines and prose outside a bullet are dropped.

    :param changes: The hand-written body of a version section, as captured
        in {attr}`VersionElements.changes`.
    :return: One string per top-level bullet, in document order.
    """
    bullets: list[str] = []
    current: list[str] = []
    for line in changes.splitlines():
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
    sentence stating what changed. Canonical guideline:
    https://github.com/kdeldycke/repomatic/blob/main/claude.md#changelog-entry-length
    Each unreleased bullet longer than `threshold` words emits a
    {data}`logging.WARNING` and a GitHub Actions warning annotation, without
    affecting the lint exit code.

    Only the unreleased section is inspected. Released sections are immutable,
    so re-flagging historical entries on every run would be noise.

    :param changelog: The parsed changelog to inspect.
    :param threshold: Word ceiling per bullet. `0` (or less) disables the check.
    """
    if threshold <= 0:
        return
    released = {version for version, _date in changelog.extract_all_releases()}
    unreleased = changelog.extract_all_version_headings() - released
    for version in sorted(unreleased, key=Version, reverse=True):
        elements = changelog.decompose_version(version)
        for index, bullet in enumerate(split_changelog_bullets(elements.changes), 1):
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
                    f"sentence, per the canonical guideline "
                    f"https://github.com/kdeldycke/repomatic/blob/main/claude.md"
                    f"#changelog-entry-length",
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

    When `fix` is enabled, date mismatches are corrected in-place and
    admonitions are added to the changelog:

    - A `[!NOTE]` admonition listing available distribution links
      (PyPI, GitHub) for each version. Links are conditional: only
      sources where the version exists are included.
    - A `[!WARNING]` admonition listing platforms where the version
      is *not* available (missing from PyPI, GitHub, or both).
    - A `[!CAUTION]` admonition for yanked releases.

    ```{caution}

    The `fix-changelog` workflow job skips this function during the
    release cycle (when `release_commits_matrix` is non-empty). At that
    point the release pipeline hasn't published to PyPI or created a
    GitHub release yet, so this function would incorrectly add "not
    available" admonitions to the freshly-released version.
    ```
    - Placeholder sections for orphaned versions, with comparison URLs
      linking to adjacent versions.

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
        `1` if any date mismatch or orphan is found without a fix being
        applied, `2` if the sanity gate refused a destructive rewrite
        because an upstream data source (GitHub Releases or PyPI) appeared
        to be returning incomplete or empty results while the existing
        changelog has substantial coverage on that platform.
    """
    content = changelog_path.read_text(encoding="UTF-8")
    changelog = Changelog(content)
    warn_on_long_bullets(changelog, bullet_word_threshold)
    releases = changelog.extract_all_releases()

    if not releases:
        logging.info("No released versions found in changelog.")
        return 0

    # Auto-detect package name for PyPI lookups.
    if package is None:
        package = get_project_name()

    # Fetch all PyPI release dates in a single API call.
    pypi_data: dict[str, PyPIRelease] = {}
    if package:
        pypi_data = get_pypi_release_dates(package)
        if pypi_data:
            logging.info(
                f"Using PyPI as reference for {package!r}"
                f" ({len(pypi_data)} releases found)."
            )
        else:
            logging.info(
                f"Package {package!r} not found on PyPI, falling back to git tags."
            )
    else:
        logging.info("No package name detected, falling back to git tags.")

    # Merge releases from former package names (for renamed projects).
    for former_package in pypi_package_history:
        former_data = get_pypi_release_dates(former_package)
        if former_data:
            logging.info(
                f"Using PyPI history for {former_package!r}"
                f" ({len(former_data)} releases found)."
            )
        # Current package wins on version collisions.
        for v, rel in former_data.items():
            pypi_data.setdefault(v, rel)

    use_pypi = bool(pypi_data)
    has_mismatch = False
    modified = False
    # Set whenever a flagged problem is left standing: no `--fix`, a fix that
    # could not run, or one that ran without landing. Kept apart from
    # `has_mismatch` so a partial repair still fails, where counting any single
    # file write as success would report green on a changelog still wrong.
    unfixed_problem = False

    # Determine the first version published to PyPI for boundary detection.
    first_pypi_version: Version | None = None
    if use_pypi:
        first_pypi_version = min(Version(v) for v in pypi_data)
        logging.info(f"First PyPI version: {first_pypi_version}")

    # Extract repository URL and fetch GitHub releases.
    repo_url = changelog.extract_repo_url()
    github_releases: dict[str, GitHubRelease] = {}
    github_fetch_failed = False
    if repo_url:
        try:
            github_releases = get_github_releases(repo_url)
        except GitHubReleasesUnavailable as exc:
            github_fetch_failed = True
            logging.warning(f"GitHub releases lookup failed: {exc}")
            emit_annotation(
                AnnotationLevel.WARNING,
                f"GitHub releases lookup failed: {exc}",
            )
        if github_releases:
            logging.info(f"GitHub releases: {len(github_releases)} found.")

    # Determine the first version released on GitHub for boundary detection.
    first_github_version: Version | None = None
    if github_releases:
        first_github_version = min(Version(v) for v in github_releases)
        logging.info(f"First GitHub version: {first_github_version}")

    # Detect orphaned versions: present in external sources but missing
    # from the changelog.
    tag_versions = get_all_version_tags()
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
        for v in (set(pypi_data) | set(github_releases) | set(tag_versions))
        if not Version(v).is_prerelease
    }
    orphans = all_known - changelog_headings - archive_headings
    if orphans:
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
        has_mismatch = True

        if fix and repo_url:
            # Insert orphans oldest-first so each insertion correctly
            # updates the adjacent section's comparison URL.
            all_versions = sorted(
                changelog_headings | orphans,
                key=Version,
                reverse=True,
            )
            for orphan in sorted(orphans, key=Version):
                # Determine date: prefer PyPI, then GitHub, then git tag.
                orphan_date = ""
                if orphan in pypi_data:
                    orphan_date = pypi_data[orphan].date
                elif orphan in github_releases:
                    orphan_date = github_releases[orphan].date
                elif orphan in tag_versions:
                    orphan_date = tag_versions[orphan]
                if not orphan_date:
                    # No source agrees on a date, so there is nothing truthful
                    # to write. A placeholder would satisfy the heading pattern
                    # and then mismatch its reference date on every later run,
                    # which is worse than leaving the orphan reported.
                    logging.warning(
                        f"⚠ {orphan}: no release date found in any source,"
                        " skipping insertion"
                    )
                    unfixed_problem = True
                    continue
                if changelog.insert_version_section(
                    orphan, orphan_date, repo_url, list(all_versions)
                ):
                    modified = True
                else:
                    unfixed_problem = True

            # Re-extract releases so the admonition loop below processes
            # the newly inserted sections.
            releases = changelog.extract_all_releases()
        else:
            # Without `--fix`, or without a repository URL to build comparison
            # links from, the orphans stay orphaned.
            unfixed_problem = True

    date_corrections: dict[str, str] = {}
    abandoned = frozenset(abandoned_versions)
    # Point maintainers at the remedy: a version that is intentionally absent
    # from the reference source is declared once in config, not re-flagged.
    abandoned_hint = (
        "list under [tool.repomatic] abandoned-versions if intentionally unpublished"
    )

    for version, changelog_date in releases:
        if use_pypi:
            release = pypi_data.get(version)
            if release is None:
                if version in abandoned:
                    logging.info(f"  {version}: abandoned (skipped per config)")
                    continue
                parsed = Version(version)
                if first_pypi_version and parsed < first_pypi_version:
                    logging.info(
                        f"  {version}: predates PyPI (first: {first_pypi_version})"
                    )
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
            tag_date = get_tag_date(f"v{version}")
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
        else:
            logging.error(
                f"✗ {version}: changelog={changelog_date}, {source}={ref_date}"
            )
            emit_annotation(
                AnnotationLevel.ERROR,
                (
                    f"Date mismatch for {version}:"
                    f" changelog={changelog_date}, {source}={ref_date}"
                ),
            )
            has_mismatch = True
            if fix:
                date_corrections[version] = ref_date
            else:
                unfixed_problem = True

    # Sanity gate: refuse to rewrite admonitions when an upstream data
    # source returned empty (or failed) while the existing changelog has
    # substantial coverage on that platform. This protects against the
    # `kdeldycke/click-extra#1702` failure mode, where a transient API
    # call returned `{}` and the subsequent rewrite stripped every
    # GitHub (or PyPI) link from the changelog without ever flagging
    # the gap as a warning.
    if fix:
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

        # GitHub side: an explicit `GitHubReleasesUnavailable` is an
        # unambiguous signal that the data is unreliable. Refuse to
        # rewrite as soon as the existing changelog has any GitHub
        # coverage, since the rewrite would silently strip it.
        if github_fetch_failed and existing_github_links > 0:
            msg = (
                f"Refusing to rewrite changelog: GitHub releases lookup "
                f"failed but {existing_github_links} existing version"
                f" section(s) reference a GitHub release. Rewriting now "
                f"would silently strip every GitHub link from the file. "
                f"Re-run when the GitHub API is reachable."
            )
            logging.error(msg)
            emit_annotation(AnnotationLevel.ERROR, msg)
            return 2

        # PyPI side: `pypi_data == {}` cannot distinguish "package not
        # on PyPI" from "transient failure" — see the
        # `EMPTY_PYPI_SANITY_THRESHOLD` docstring for the two layers of
        # ambiguity (client-side catch-all in `_fetch_json`, plus
        # Warehouse's own 404 semantics, pypi/warehouse#1388). Use a
        # coverage threshold to gate: a substantial number of existing
        # PyPI links combined with an empty fetch is the fingerprint of
        # a transient failure rather than a genuine non-PyPI project.
        if (
            package
            and not pypi_data
            and existing_pypi_links >= EMPTY_PYPI_SANITY_THRESHOLD
        ):
            msg = (
                f"Refusing to rewrite changelog: PyPI lookup for "
                f"{package!r} returned no data but {existing_pypi_links} "
                f"existing version section(s) reference a PyPI release. "
                f"Likely a transient API failure. Re-run when PyPI is "
                f"reachable. If the project genuinely no longer publishes to "
                f"PyPI, run with an empty package name (`--package ''`) to "
                f"check dates against git tags instead."
            )
            logging.error(msg)
            emit_annotation(AnnotationLevel.ERROR, msg)
            return 2

    # In fix mode, decompose each version section into elements,
    # apply date corrections, compute updated admonitions, and
    # reassemble via the release-notes template. This is idempotent:
    # decomposing and re-rendering an already-correct section is a
    # no-op.
    if fix:
        for version, _date in releases:
            elements = changelog.decompose_version(version)

            # Apply corrected date if the check loop found a mismatch.
            if version in date_corrections:
                elements.date = date_corrections[version]

            on_pypi = version in pypi_data
            on_github = version in github_releases
            is_yanked = on_pypi and pypi_data[version].yanked

            # Build the NOTE admonition for platforms where available.
            # Use the package name embedded in the PyPIRelease entry so
            # renamed projects point to the correct PyPI page.
            # Yanked releases are excluded from the NOTE — the CAUTION
            # admonition below links to the specific PyPI page instead.
            pypi_url = (
                PYPI_PROJECT_URL.format(
                    package=pypi_data[version].package, version=version
                )
                if on_pypi and not is_yanked
                else ""
            )
            github_url = (
                GITHUB_RELEASE_URL.format(repo_url=repo_url, version=version)
                if on_github and repo_url
                else ""
            )

            # "First version" wording applies when every listed platform
            # is a first appearance for that platform.
            parsed = Version(version)
            is_first_pypi = (
                on_pypi
                and first_pypi_version is not None
                and parsed == first_pypi_version
            )
            is_first_github = (
                on_github
                and first_github_version is not None
                and parsed == first_github_version
            )
            first_on_all = (
                (is_first_pypi or not on_pypi)
                and (is_first_github or not on_github)
                and (is_first_pypi or is_first_github)
            )

            note = build_release_admonition(
                version,
                pypi_url=pypi_url,
                github_url=github_url,
                first_on_all=first_on_all,
            )

            # Build the WARNING admonition for platforms where missing.
            # Only warn about gaps: versions that postdate the first
            # release on that platform but are absent from it.
            pypi_gap = (
                not on_pypi
                and bool(package)
                and first_pypi_version is not None
                and parsed >= first_pypi_version
            )
            github_gap = (
                not on_github
                and bool(repo_url)
                and first_github_version is not None
                and parsed >= first_github_version
            )
            warning = build_unavailable_admonition(
                version,
                missing_pypi=pypi_gap,
                missing_github=github_gap,
            )
            # Combine NOTE and WARNING admonitions. Both can appear
            # when a version is on one platform but not the other.
            admonitions = [a for a in (note, warning) if a]
            elements.availability_admonition = "\n\n".join(admonitions)

            if is_yanked:
                # PyPI accepts a yank with no reason, so the clause carrying
                # it is rendered only when there is one. Its own trailing
                # period is dropped: the template supplies the sentence's.
                reason = pypi_data[version].yanked_reason.strip().rstrip(".")
                elements.yanked_admonition = render_template(
                    "yanked-admonition",
                    version=version,
                    package=pypi_data[version].package,
                    reason=f": {reason}" if reason else "",
                )

            new_section = render_template("release-notes", **asdict(elements))
            modified |= changelog.replace_section(version, new_section)

        # A correction only counts as applied once the rendered section carries
        # it. `replace_section` cannot answer that on its own: it reports "no
        # change" both for a section it failed to touch and for one that was
        # already correct, so the date itself is the evidence.
        for corrected_version, corrected_date in date_corrections.items():
            if changelog.decompose_version(corrected_version).date != corrected_date:
                unfixed_problem = True

    if fix and modified:
        changelog_path.write_text(changelog.content.rstrip() + "\n", encoding="UTF-8")
        logging.info(f"Updated {changelog_path}")

    # Success needs every flagged problem repaired, not merely some file write
    # having happened: a run that inserts one orphan but leaves another without
    # a comparison URL to link it from has not fixed the changelog, and letting
    # downstream steps proceed on that would publish the gap.
    return 1 if has_mismatch and unfixed_problem else 0


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

    :param changelog: Parsed changelog instance.
    :param version: Version string (e.g. `1.2.3`).
    :param admonition_override: If provided, replaces the
        `availability_admonition` from the changelog. Used by
        `release_notes_with_admonition` to inject a pre-computed
        admonition at release time.
    :return: The rendered release body, or empty string if the
        version has no changelog section.
    """
    elements = changelog.decompose_version(version)
    if (
        not elements.changes
        and not elements.availability_admonition
        and not elements.development_warning
        and not elements.editorial_admonition
        and not elements.yanked_admonition
    ):
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
