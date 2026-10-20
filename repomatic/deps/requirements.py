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

"""The requirement arrays of a `pyproject.toml`, and the package names in them.

Shared by the three modules that read what a project declares: the version
policy ({mod}`repomatic.deps.policy`), the shippability gate
({mod}`repomatic.deps.shippable`) and the git-to-release swap
({mod}`repomatic.deps.sources`). Each array is located by its TOML path, the one
spelling every report uses.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

from ..pyproject import cached_toml
from ..versions import safe_version

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping
    from pathlib import Path
    from typing import Any

LOWER_BOUND_OPERATORS = frozenset((">", ">="))
"""PEP 440 operators that put a floor under a requirement.

The one spelling of "lower bound" every check tests specifiers against. A site
that also treats an exact pin as a bound unions {data}`PIN_OPERATOR` in
explicitly, so the difference stays a decision rather than an accident.
"""

PIN_OPERATOR = frozenset(("==",))
"""The PEP 440 operator pinning a requirement to one version.

A pin demands its version the way a floor demands its minimum, so the checks
asking "can PyPI serve what this declaration requires" test both; the checks
looking for the managed git-branch floor idiom deliberately do not.
"""

PUBLISHED_TABLES = ("project", "project.optional-dependencies")
"""Tables whose requirement arrays land in the published `Requires-Dist`.

`[dependency-groups]` ([PEP 735](https://peps.python.org/pep-0735/)) is
deliberately absent: it never reaches distribution metadata. Neither does
`[build-system].requires`, which only the build front end reads.
"""

BUILD_TABLE = "build-system"
"""The table whose `requires` array lists the build dependencies."""

TOML_TABLE_HEADER = re.compile(r"\s*\[{1,2}\s*(?P<path>[^]]+?)\s*\]{1,2}\s*$")
"""A `[table]` or `[[array of tables]]` header, capturing its dotted path."""


@dataclass(frozen=True)
class RequirementArray:
    """One requirement array of a `pyproject.toml`."""

    table: str
    """The dotted path of the table holding the array (`project`,
    `project.optional-dependencies`, `dependency-groups`, `build-system`)."""

    key: str
    """The array's key in that table: `dependencies`, an extra, a group, or
    `requires`."""

    items: list[Any]
    """The live array, so a caller can rewrite an item in place."""

    @property
    def location(self) -> str:
        """The TOML path of the array, as every report spells it."""
        return f"{self.table}.{self.key}"

    @property
    def published(self) -> bool:
        """Whether the array reaches the published `Requires-Dist`.

        What separates a finding an installer trips over from one only a
        contributor does: see {data}`PUBLISHED_TABLES`.
        """
        return self.table in PUBLISHED_TABLES

    def requirements(self) -> Iterator[tuple[int, str, Requirement]]:
        """Yield each parsable requirement, with its index and its raw string.

        Non-string items (like `{include-group = …}` entries) and malformed
        specifiers are skipped.
        """
        for index, item in enumerate(self.items):
            requirement = parse_requirement(item)
            if requirement is not None:
                yield index, item, requirement


def read_project(pyproject_path: Path) -> Mapping[str, Any]:
    """Parse *pyproject_path* read-only, through the shared TOML cache.

    :param pyproject_path: The `pyproject.toml` file, whatever its name.
    :return: The parsed document, or an empty mapping when the file is
        missing. Shared with the cache: treat it as read-only.
    """
    try:
        data: Mapping[str, Any] = cached_toml(pyproject_path)
    except OSError:
        return {}
    return data


def requirement_arrays(doc: Mapping[str, Any]) -> Iterator[RequirementArray]:
    """Yield every requirement array of a parsed `pyproject.toml`.

    Covers `[project] dependencies`, each `[project.optional-dependencies]`
    extra, each `[dependency-groups]` group, and `[build-system] requires`.
    Values that are not lists are skipped.

    :param doc: The parsed `pyproject.toml`.
    """
    project = doc.get("project", {})
    if isinstance(dependencies := project.get("dependencies"), list):
        yield RequirementArray("project", "dependencies", dependencies)
    for extra, items in (project.get("optional-dependencies") or {}).items():
        if isinstance(items, list):
            yield RequirementArray("project.optional-dependencies", extra, items)
    for group, items in (doc.get("dependency-groups") or {}).items():
        if isinstance(items, list):
            yield RequirementArray("dependency-groups", group, items)
    if isinstance(requires := doc.get(BUILD_TABLE, {}).get("requires"), list):
        yield RequirementArray(BUILD_TABLE, "requires", requires)


def parse_requirement(item: object) -> Requirement | None:
    """Parse a requirement array item, returning `None` for anything else.

    :param item: One entry of a requirement array.
    :return: The parsed requirement, or `None` for a non-string entry or an
        unparsable specifier.
    """
    if not isinstance(item, str):
        return None
    try:
        return Requirement(item)
    except InvalidRequirement:
        return None


def dev_floor(doc: Mapping[str, Any], name: str) -> str | None:
    """The highest `.dev` lower bound declared for *name*, if any.

    Scans every requirement array for lower-bound clauses (`>=` or `>`) whose
    version is a dev release. The highest one is the project's declared
    "awaited release" threshold.

    :param doc: The parsed `pyproject.toml`.
    :param name: Package name (any capitalization or separator style).
    :return: The floor version string, or `None` when the package has no dev
        floor (a git override is then outside the managed idiom).
    """
    canonical = canonicalize_name(name)
    floors = []
    for array in requirement_arrays(doc):
        for _index, _raw, requirement in array.requirements():
            if canonicalize_name(requirement.name) != canonical:
                continue
            for spec in requirement.specifier:
                if spec.operator not in LOWER_BOUND_OPERATORS:
                    continue
                version = safe_version(spec.version)
                if version is not None and version.is_devrelease:
                    floors.append(version)
    return str(max(floors)) if floors else None


def iter_toml_lines(text: str) -> Iterator[tuple[int, str, str]]:
    """Yield each line of a TOML text, with its number and the table it sits in.

    Enough TOML parsing to place a line: the parsed document holds values and
    comments, but no line numbers, and a reader needs one to find a
    declaration. A header line already belongs to the table it opens.

    :param text: The TOML text.
    :return: `(line number, table path, line)` triples. Numbers start at 1,
        and the table path is empty above the first header.
    """
    table = ""
    for number, line in enumerate(text.splitlines(), start=1):
        header = TOML_TABLE_HEADER.match(line)
        if header:
            table = header["path"]
        yield number, table, line


def pep503_pattern(name: str) -> str:
    """A regex matching *name* in any spelling PEP 503 treats as equal.

    A canonical name separates its parts with `-`, where a declaration may
    spell any of `-`, `_` or `.`. Match it case-insensitively, since the
    declaration also picks its own case.

    :param name: A package name, in any spelling.
    :return: The pattern, with no anchors or flags.
    """
    parts = canonicalize_name(name).split("-")
    return "[-_.]+".join(re.escape(part) for part in parts)
