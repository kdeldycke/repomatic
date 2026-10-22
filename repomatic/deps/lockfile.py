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

"""Read a `uv.lock` through the views the dependency commands need.

A lock runs to hundreds of kilobytes, and one command asks it many questions:
the locked versions, their upload times, the cooldown it was resolved under,
the specifiers each package declares. {class}`LockFile` is the one entry point.
It parses the file through the shared TOML cache, and builds each view the
first time a caller asks for it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import cached_property

from click_extra import parse_iso8601_duration
from packaging.utils import canonicalize_name

from ..humanize import parse_iso_datetime
from ..pyproject import cached_toml

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import timedelta
    from pathlib import Path
    from typing import Any

LOCK_TIMESTAMP_SENTINEL = "0001-01-01T00:00:00Z"
"""Placeholder uv writes to `options.exclude-newer` in `uv.lock` when the
user-configured value is a relative span. The real cutoff is in
`options.exclude-newer-span` as an ISO 8601 duration."""

UTC_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
"""The RFC 3339 UTC form of an instant, as uv writes it into `uv.lock`.

uv stores a value in this form verbatim, on every machine. That is why every
cutoff this package writes uses it.
"""

EXTRA_MARKER_RE = re.compile(r"\bextra\s*==\s*'([^']+)'")
"""Match the extra a `requires-dist` marker gates its dependency behind.

Searched rather than anchored: uv writes the bare `extra == 'x'` form most of
the time, but combines it with a version guard (`python_full_version >= '3.11'
and extra == 'x'`) when the declaration carries one. Anchoring would read those
as unconditional dependencies.
"""


@dataclass
class LockSpecifiers:
    """Dependency specifiers extracted from a `uv.lock` file.

    Three views of the same data, built in a single pass over the lock packages:

    `by_package`
        `{package_name: {dep_name: specifier}}`. Every dependency declared by
        a package (main and dev) keyed by the declaring package name. Used for
        edge labels in dependency graphs.

    `by_subgraph`
        `{subgraph_name: {dep_name: specifier}}`. Primary dependencies keyed
        by dev-group name or extra name. Used for node labels inside subgraphs.

    `by_main`
        `{package_name: {dep_name: specifier}}`. Only the dependencies a
        package declares unconditionally, behind neither an extra nor a dev
        group. This is the authoritative answer to "what does installing this
        project pull in by default", which a CycloneDX SBOM does not reliably
        give. See {func}`~repomatic.deps.graph.filter_root_edges`. A package
        with no `metadata` table is absent from the mapping entirely, telling
        "declares nothing unconditionally" apart from "not described here".
    """

    by_package: dict[str, dict[str, str]]
    by_subgraph: dict[str, dict[str, str]]
    by_main: dict[str, dict[str, str]] = field(default_factory=dict)


def _read_lock(lock_path: Path) -> Mapping[str, Any]:
    """Parse *lock_path* through the shared TOML cache.

    The parse is memoized per file identity (see
    {func}`~repomatic.pyproject.cached_toml`), so however many passes a command
    makes over the same lock, the document is decoded once. Every rewrite of the
    file (`uv lock` itself, a byte-level restore) moves that identity, which
    keeps the cache honest across the upgrade-then-diff flows. Treat the result
    as read-only.

    :param lock_path: Path to the `uv.lock` file.
    :return: The parsed document, or an empty mapping when the file is absent.
    """
    try:
        data: Mapping[str, Any] = cached_toml(lock_path)
    except OSError:
        return {}
    return data


class LockFile:
    """A parsed `uv.lock`, with one lazily built view per question.

    A missing lock reads as an empty one rather than as an error: several
    callers run before the first `uv lock`.
    """

    def __init__(self, data: Mapping[str, Any] | None = None) -> None:
        self.data: Mapping[str, Any] = data or {}
        """The parsed document. Shared with the TOML cache: read-only."""

    @classmethod
    def load(cls, lock_path: Path) -> LockFile:
        """Read *lock_path*, or an empty lock when the file does not exist."""
        return cls(_read_lock(lock_path))

    @cached_property
    def versions(self) -> dict[str, str]:
        """Package name to locked version, keyed by the canonical name uv writes."""
        return {
            pkg["name"]: pkg["version"]
            for pkg in self.data.get("package", [])
            if "name" in pkg and "version" in pkg
        }

    @cached_property
    def upload_times(self) -> dict[str, str]:
        """Package name to the ISO 8601 `upload-time` of its `sdist` entry.

        Packages with no `sdist` or no upload time are absent: a git or path
        source has no release to date.
        """
        upload_times = {}
        for pkg in self.data.get("package", []):
            name = pkg.get("name", "")
            upload_time = pkg.get("sdist", {}).get("upload-time", "")
            if name and upload_time:
                upload_times[name] = upload_time
        return upload_times

    @cached_property
    def registries(self) -> dict[str, str]:
        """Package name to the URL of the index it is locked from.

        Packages from a git, path or URL source are absent: they come from no
        index.
        """
        return {
            pkg["name"]: pkg["source"]["registry"]
            for pkg in self.data.get("package", [])
            if "name" in pkg
            and isinstance(pkg.get("source"), dict)
            and isinstance(pkg["source"].get("registry"), str)
        }

    @cached_property
    def cooldown_span(self) -> timedelta | None:
        """Width of the rolling cooldown, from `options.exclude-newer-span`.

        `None` when the lock records an absolute cutoff instead of a span, which
        leaves the cooldown-expiry forecasts nothing to project against.
        """
        raw = self.data.get("options", {}).get("exclude-newer-span", "")
        return parse_iso8601_duration(raw) if raw else None

    @cached_property
    def exclude_newer(self) -> str:
        """Effective `options.exclude-newer` cutoff, as an ISO 8601 instant.

        When the project configures a relative span, uv writes
        {data}`LOCK_TIMESTAMP_SENTINEL` here and the real width to
        `options.exclude-newer-span`. The cutoff then resolves to `now - span`
        the first time it is read. Empty when neither field is present.
        """
        timestamp: str = self.data.get("options", {}).get("exclude-newer", "")
        if timestamp and timestamp != LOCK_TIMESTAMP_SENTINEL:
            return timestamp
        if self.cooldown_span is not None:
            cutoff = datetime.now(UTC) - self.cooldown_span
            return cutoff.strftime(UTC_TIMESTAMP_FORMAT)
        return ""

    @cached_property
    def specifiers(self) -> LockSpecifiers:
        """The dependency specifiers of every package, in three views.

        A single pass reads `[package.metadata].requires-dist` and
        `[package.metadata.requires-dev]`. See {class}`LockSpecifiers`.
        """
        by_package: dict[str, dict[str, str]] = {}
        by_subgraph: dict[str, dict[str, str]] = {}
        by_main: dict[str, dict[str, str]] = {}

        for package in self.data.get("package", []):
            pkg_name = package.get("name", "")
            if not pkg_name:
                continue

            pkg_deps: dict[str, str] = {}
            main_deps: dict[str, str] = {}
            metadata = package.get("metadata", {})

            for dep in metadata.get("requires-dist", []):
                if not isinstance(dep, dict):
                    continue
                dep_name = dep.get("name", "")
                specifier = dep.get("specifier", "")
                if dep_name and specifier:
                    pkg_deps[dep_name] = specifier
                if not dep_name:
                    continue
                # Index by extra when the marker gates the dependency behind one;
                # everything else is declared unconditionally.
                match = EXTRA_MARKER_RE.search(dep.get("marker", ""))
                if match:
                    by_subgraph.setdefault(match.group(1), {})[dep_name] = specifier
                else:
                    main_deps[dep_name] = specifier

            for group_name, group_deps in metadata.get("requires-dev", {}).items():
                group_specs: dict[str, str] = {}
                for dep in group_deps:
                    if not isinstance(dep, dict):
                        continue
                    dep_name = dep.get("name", "")
                    specifier = dep.get("specifier", "")
                    if dep_name:
                        group_specs[dep_name] = specifier
                        if specifier:
                            pkg_deps[dep_name] = specifier
                if group_specs:
                    by_subgraph[group_name] = group_specs

            if pkg_deps:
                by_package[pkg_name] = pkg_deps
            # Only the workspace's own packages carry a `metadata` table, and
            # those are the ones a dependency graph can be rooted on. Record the
            # key even when nothing is declared unconditionally, so an empty
            # mapping stays distinguishable from a package the lock omits.
            if metadata:
                by_main[pkg_name] = main_deps

        return LockSpecifiers(
            by_package=by_package, by_subgraph=by_subgraph, by_main=by_main
        )

    def version_of(self, name: str) -> str:
        """Locked version of *name*, in any PEP 503 spelling.

        uv writes every package under its canonical name, while a declaration
        or an `exclude-newer-package` key keeps the spelling its author typed.

        :param name: A package name, in any spelling.
        :return: The version, or empty when the lock does not hold the package.
        """
        return self.versions.get(canonicalize_name(name), "")

    def upload_time_of(self, name: str) -> str:
        """Upload time of the locked version of *name*, in any PEP 503 spelling.

        :param name: A package name, in any spelling.
        :return: The ISO 8601 upload time. Empty for a package the lock does
            not hold, and for one it holds from a git or path source:
            {meth}`version_of` tells the two apart.
        """
        return self.upload_times.get(canonicalize_name(name), "")

    def uploaded_at(self, name: str) -> datetime | None:
        """Parsed {meth}`upload_time_of`, or `None` when there is none to parse."""
        return parse_iso_datetime(self.upload_time_of(name))


def diff_lock_versions(
    before: Mapping[str, str],
    after: Mapping[str, str],
) -> list[tuple[str, str, str]]:
    """Compare two version mappings and return the list of changes.

    :param before: Package versions before the upgrade.
    :param after: Package versions after the upgrade.
    :return: A sorted list of `(name, old_version, new_version)` tuples.
        `old_version` is empty for added packages; `new_version` is empty
        for removed packages.
    """
    changes = []
    for name in sorted(set(before) | set(after)):
        old = before.get(name, "")
        new = after.get(name, "")
        if old != new:
            changes.append((name, old, new))
    return changes
