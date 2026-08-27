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

"""How a dependency is *declared*, as opposed to where it resolves from.

{mod}`~repomatic.deps.dep_sources` answers "can this ship": a git branch or a local
path breaks the install for whoever pulls the published artifact, so those
findings block a release. This module answers a narrower question that never
blocks anything: is the declaration written the way the project's own version
policy says to write it.

The split is what keeps both halves honest. A style finding that could stop a
release would eventually be silenced rather than fixed; a shippability finding
that only warned would ship a broken wheel.

Only rules decidable from `pyproject.toml` alone live here. Whether a floor is
*justified* by the APIs the code actually calls is the judgment call
`/repomatic-deps review` exists for, and it stays there: no amount of parsing
settles it, and a checker that guessed would train people to ignore it.

The rules, and what each one costs the reader when broken:

- An **upper bound** on a runtime dependency caps everyone downstream, and the
  cap outlives whatever release prompted it. See
  [Should You Use Upper Bound Version
  Constraints?](https://iscinumpy.dev/post/bound-version-constraints/)
- A **bare dependency** pins nothing, so the install that passed CI and the one
  a user gets can differ by a major version.
- An **unsorted list** makes every addition a merge conflict candidate and
  hides duplicates.
- A **type stub outside the `typing` group** installs at runtime for users who
  will never type-check.
- A **floor with no comment** cannot be audited: the next reader has no way to
  tell a deliberate API minimum from a number a bot last touched.
- A **floor comment that runs long** has stopped justifying the floor and
  started narrating how it got there. Each bump appends a paragraph about a
  version no longer in force, and the one claim that matters (what breaks
  below the floor that is declared) ends up buried in superseded history the
  git log already keeps.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

from ..pyproject import read_pyproject_toml

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Iterator

RUNTIME_LOCATION = "[project] dependencies"
"""Where a runtime dependency is declared, as the report spells it."""

STUB_PREFIX = "types-"
"""Distribution-name prefix marking a [PEP 561](https://peps.python.org/pep-0561/)
stub-only package."""

STUB_GROUP = "typing"
"""Dependency group stub-only packages belong in.

They are build-time inputs to a type checker, so installing them anywhere a
user's runtime environment reaches is pure weight.
"""

UPPER_BOUND_OPERATORS = ("<", "<=", "==", "!=", "~=")
"""Specifier operators that cap a runtime dependency from above.

`~=` is included because it implies a ceiling: `~=1.2` is `>=1.2, ==1.*`.
Conditional markers (`python_version<'3.11'`) are not specifiers and never
reach this list.
"""

_ENTRY_RE = re.compile(r"""^\s*["'](?P<requirement>[^"']+)["']\s*,?\s*(?:#.*)?$""")
"""One dependency per line, the layout every check below assumes.

A list written inline on a single line parses fine but carries no per-entry
line to hang a comment on, so {func}`_entry_lines` reports it as unscannable
rather than guessing.
"""


@dataclass(frozen=True)
class PolicyFinding:
    """One declaration that departs from the project's version policy.

    Deliberately not a {class}`~repomatic.deps.dep_sources.DepFinding`: that type
    carries a {class}`~repomatic.deps.dep_sources.SourceKind` because every one of
    its findings is about where a package resolves from, and a style finding
    has no answer to give there.
    """

    package: str
    """Normalized name of the package the finding is about."""

    location: str
    """The TOML path the declaration was read from."""

    detail: str
    """The declaration as written, for the reader to go find."""

    consequence: str
    """What it costs to leave this as it is."""

    remedy: str
    """The next action."""

    @property
    def message(self) -> str:
        """The finding as a single annotation line."""
        return (
            f"{self.package}: {self.location} ({self.detail}). "
            f"{self.consequence} {self.remedy}"
        )


def _entry_lines(text: str, table: str, key: str) -> list[tuple[int, str]] | None:
    """Locate each entry of a one-per-line dependency array in the raw text.

    Reading the file rather than the parsed document is what makes the comment
    check possible at all: a comment is trivia no plain-data parser keeps.

    :param text: Full `pyproject.toml` contents.
    :param table: Table header to find, without brackets.
    :param key: Array key inside that table.
    :return: `(line index, requirement string)` per entry, or `None` when the
        array is written inline, where there is no per-entry line. The array's
        own opening line is included as the first tuple with an empty
        requirement, so a caller can ask whether a comment documents the whole
        array rather than each entry.
    """
    lines = text.splitlines()
    in_table = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and not stripped.startswith("[" * 2):
            in_table = stripped == f"[{table}]"
            continue
        if not in_table:
            continue
        if not re.match(rf"^\s*{re.escape(key)}\s*=\s*\[\s*$", line):
            continue
        entries = [(index, "")]
        for offset in range(index + 1, len(lines)):
            if lines[offset].strip().startswith("]"):
                return entries
            match = _ENTRY_RE.match(lines[offset])
            if match:
                entries.append((offset, match.group("requirement")))
        return entries
    return None


def _comment_above(text: str, line_index: int) -> list[str]:
    """The contiguous run of comment lines sitting above *line_index*.

    Read bottom-up from the adjacent line and stopping at the first line that
    is not a comment, so a comment separated from the entry by a blank line is
    not attached to it. Returned in reading order, each line stripped of its
    `#` marker, which is what makes the run countable as prose.

    :return: The comment lines, or an empty list when the entry carries none.
    """
    lines = text.splitlines()
    comment: list[str] = []
    cursor = line_index - 1
    while cursor >= 0 and lines[cursor].strip().startswith("#"):
        comment.append(lines[cursor].strip().removeprefix("#").strip())
        cursor -= 1
    comment.reverse()
    return comment


def count_comment_words(comment: list[str]) -> int:
    """Count the words of a comment run, ignoring the `#` markers.

    Everything else counts as written, URLs and inline code included: a
    rationale leaning on three links is still three links the reader walks
    past on the way to the floor.
    """
    return len(" ".join(comment).split())


def _requirements(raw: object) -> Iterator[str]:
    """Yield each requirement string of a dependency array, skipping junk."""
    if not isinstance(raw, list):
        return
    for entry in raw:
        if isinstance(entry, str):
            yield entry


def _parse(requirement: str) -> Requirement | None:
    """Parse a PEP 508 requirement, or `None` when it is malformed."""
    try:
        return Requirement(requirement)
    except InvalidRequirement:
        return None


def _check_specifier(
    requirement: Requirement,
    raw: str,
    location: str,
    runtime: bool,
    floored: frozenset[str] = frozenset(),
) -> Iterator[PolicyFinding]:
    """Flag a missing specifier, and an upper bound on a runtime dependency.

    :param floored: Canonical names carrying a specifier somewhere else in the
        same file, plus the project's own name. A second mention needs no floor
        of its own: a `docs` group pulling `click-extra[sphinx]` is selecting an
        extra of a package the runtime list already constrains, and repeating
        the floor there would just be a second number to keep in step. A
        self-reference needs none either, and can carry none.
    """
    if not str(requirement.specifier):
        if canonicalize_name(requirement.name) in floored:
            return
        yield PolicyFinding(
            package=canonicalize_name(requirement.name),
            location=location,
            detail=f"`{raw}`",
            consequence=(
                "Nothing constrains which version installs, so the release "
                "this was tested against and the one a user gets can differ "
                "by a major version."
            ),
            remedy="Add a `>=` floor naming the oldest version the code works with.",
        )
        return
    if not runtime:
        return
    capped = sorted(
        str(specifier)
        for specifier in requirement.specifier
        if specifier.operator in UPPER_BOUND_OPERATORS
    )
    if capped:
        yield PolicyFinding(
            package=canonicalize_name(requirement.name),
            location=location,
            detail=f"`{raw}`",
            consequence=(
                f"The cap {', '.join(capped)} propagates to "
                f"every project installing this one, and outlives whatever "
                f"release prompted it."
            ),
            remedy=(
                "Drop the upper bound, per "
                "https://iscinumpy.dev/post/bound-version-constraints/"
            ),
        )


def _over_long(
    package: str, location: str, detail: str, threshold: int
) -> PolicyFinding:
    """The finding for a floor comment that has outgrown its floor.

    Shared by the per-entry comment and the block above the array, which fail
    the same way and take the same remedy.
    """
    return PolicyFinding(
        package=package,
        location=location,
        detail=detail,
        consequence=(
            "A floor comment answers one question: what breaks below the "
            f"version declared here. Past {threshold} words it is narrating "
            "how the floor got there instead, and the answer is buried in "
            "versions no longer in force."
        ),
        remedy=(
            "Cut it to the paragraph justifying the floor as it stands. "
            "Superseded floors are in the git history, and belong in no "
            "comment."
        ),
    )


def _check_ordering(names: list[str], location: str) -> Iterator[PolicyFinding]:
    """Flag the first entry that breaks alphabetical order.

    Only the first: a single misplaced entry makes every later one look
    out of order too, and a report naming them all buries the one to move.
    """
    for previous, current in pairwise(names):
        if canonicalize_name(current) < canonicalize_name(previous):
            yield PolicyFinding(
                package=canonicalize_name(current),
                location=location,
                detail=f"`{current}` follows `{previous}`",
                consequence=(
                    "An unsorted list turns every addition into a merge "
                    "conflict candidate and hides duplicates."
                ),
                remedy="Sort the list alphabetically.",
            )
            return


def scan_policy(
    pyproject_path: Path, comment_word_threshold: int = 0
) -> list[PolicyFinding]:
    """Every declaration in *pyproject_path* that departs from version policy.

    Entirely offline, reading only `pyproject.toml`, so it costs nothing to
    run on every push.

    :param pyproject_path: Path to the `pyproject.toml` file.
    :param comment_word_threshold: Word ceiling per floor comment. `0` (or
        less) disables the check, which is what a caller with no configuration
        to read gets.
    :return: Findings sorted by location, then by package.
    """
    if not pyproject_path.exists():
        return []
    # Takes the directory holding the file, not the file itself.
    data = read_pyproject_toml(pyproject_path.parent)
    text = pyproject_path.read_text(encoding="UTF-8")
    findings: list[PolicyFinding] = []

    arrays: list[tuple[str, str, str, object, bool]] = [
        (
            RUNTIME_LOCATION,
            "project",
            "dependencies",
            data.get("project", {}).get("dependencies"),
            True,
        )
    ]
    for group, entries in (data.get("dependency-groups") or {}).items():
        arrays.append((
            f"[dependency-groups] {group}",
            "dependency-groups",
            group,
            entries,
            False,
        ))
    for extra, entries in (
        data.get("project", {}).get("optional-dependencies") or {}
    ).items():
        arrays.append((
            f"[project.optional-dependencies] {extra}",
            "project.optional-dependencies",
            extra,
            entries,
            True,
        ))

    # Every name floored anywhere in the file, so a second mention selecting an
    # extra is not read as an unconstrained dependency. The project's own name
    # joins them: an aggregate extra selecting the project's other extras
    # (`orchard[toml,xml]` inside orchard) resolves to the very version being
    # installed, so no other release exists for a floor to exclude, and a
    # project can never floor itself in its own file anyway.
    own_name = data.get("project", {}).get("name")
    floored = frozenset(
        canonicalize_name(requirement.name)
        for _location, _table, _key, raw_entries, _runtime in arrays
        for requirement in map(_parse, _requirements(raw_entries))
        if requirement is not None and str(requirement.specifier)
    ) | ({canonicalize_name(own_name)} if own_name else set())

    for location, table, key, raw_entries, runtime in arrays:
        raws = list(_requirements(raw_entries))
        if not raws:
            continue

        parsed = [(raw, _parse(raw)) for raw in raws]
        for raw, requirement in parsed:
            if requirement is None:
                continue
            findings.extend(
                _check_specifier(requirement, raw, location, runtime, floored)
            )

        findings.extend(
            _check_ordering(
                [req.name for _raw, req in parsed if req is not None], location
            )
        )

        # Stub packages belong in the typing group, wherever else they appear.
        if key != STUB_GROUP:
            for raw, requirement in parsed:
                if requirement is None:
                    continue
                if not canonicalize_name(requirement.name).startswith(STUB_PREFIX):
                    continue
                findings.append(
                    PolicyFinding(
                        package=canonicalize_name(requirement.name),
                        location=location,
                        detail=f"`{raw}`",
                        consequence=(
                            "A stub-only package is an input to a type "
                            "checker, so installing it here reaches "
                            "environments that never type-check."
                        ),
                        remedy=f"Move it to the `{STUB_GROUP}` dependency group.",
                    )
                )

        # The comment check needs a line per entry, which an inline array
        # cannot offer.
        entry_lines = _entry_lines(text, table, key)
        if entry_lines is None:
            continue
        # The first tuple is the array's own opening line. A comment above it
        # documents the whole array, which is how a run of related entries
        # (three `types-*` stubs, say) is usually justified in one block. It
        # excuses a missing per-entry comment, and only that: an over-long
        # comment is over-long wherever it sits, or a wall would escape the
        # length check by moving up one line.
        (array_line, _marker), *entries = entry_lines
        block_comment = _comment_above(text, array_line)
        # The first entry with nothing of its own, so the block above the array
        # is what documents it. Absent one, that block justifies no floor at
        # all: a project restating its version policy there is writing a
        # preamble, and length is not its measure.
        leans_on_block: tuple[str, str] | None = None
        for line_index, raw in entries:
            requirement = _parse(raw)
            if requirement is None or not str(requirement.specifier):
                continue
            comment = _comment_above(text, line_index)
            if not comment:
                if block_comment:
                    if leans_on_block is None:
                        leans_on_block = (canonicalize_name(requirement.name), raw)
                    continue
                findings.append(
                    PolicyFinding(
                        package=canonicalize_name(requirement.name),
                        location=location,
                        detail=f"`{raw}` at line {line_index + 1}",
                        consequence=(
                            "An uncommented floor cannot be audited: nothing "
                            "distinguishes a deliberate API minimum from a "
                            "number a bot last touched."
                        ),
                        remedy=(
                            "Add a comment above it naming the feature or fix "
                            "that version introduced."
                        ),
                    )
                )
                continue
            if comment_word_threshold <= 0:
                continue
            words = count_comment_words(comment)
            if words > comment_word_threshold:
                findings.append(
                    _over_long(
                        canonicalize_name(requirement.name),
                        location,
                        f"`{raw}` documented in {words} words",
                        comment_word_threshold,
                    )
                )

        if comment_word_threshold > 0 and leans_on_block is not None:
            words = count_comment_words(block_comment)
            if words > comment_word_threshold:
                name, raw = leans_on_block
                findings.append(
                    _over_long(
                        name,
                        location,
                        f"`{raw}` documented in a {words}-word block above the array",
                        comment_word_threshold,
                    )
                )

    return sorted(findings, key=lambda finding: (finding.location, finding.package))
