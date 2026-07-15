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
"""Swap git-tracked dependencies back to their released versions.

The `sync-dep-sources` updater manages one precise idiom: a dependency
temporarily consumed from a git branch while its next release is awaited.
The idiom is machine-recognizable because it pairs two declarations in
`pyproject.toml`:

- a `[tool.uv.sources]` entry tracking a **branch** (not a `rev` or `tag`
  pin), and
- a dev-version floor on the same package (like `mango>=2.1.0.dev0`), whose
  base version names the awaited release.

Once the awaited release ships on the index, the swap rewrites the project
back to released artifacts: the source override is dropped, the `.dev` floor
is tightened to its base release, and a cooldown-bypass freeze adopts the
release through the `exclude-newer` window (the same deliberate-bypass
mechanism `audit --fix` uses for security fixes). The freeze then ages out
and is pruned by the ordinary `sync-uv-lock` lifecycle.

```{note}
The dev floor is authoritative, deliberately: the project *declares* that
anything from the awaited release onward satisfies it. If the project
quietly grew a dependency on branch commits newer than the release, the swap
PR's CI run exposes the stale declaration, and the correction (bumping the
floor to the next `.dev` version, which retracts the swap on the next run)
is exactly the fix the project needed anyway. Overrides outside the idiom
(path or workspace sources, `rev`/`tag` pins, floor-less branch tracks) are
never touched.
```
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import tomlrt
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from .pypi import get_release_dates
from .uv import _date_to_utc_cutoff, _format_released

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Iterator

DEV_BOUND_PATTERN = re.compile(r"(?P<op>>=?)\s*(?P<version>[0-9][A-Za-z0-9.!+]*)")
"""Lower-bound clauses in a PEP 508 requirement string.

Captures the operator and the version literal so {func}`strip_dev_bounds` can
rewrite `>=2.1.0.dev0` into `>=2.1.0` in place, leaving extras, markers, and
every other clause byte-for-byte untouched.
"""


@dataclass(frozen=True)
class ReleaseSwap:
    """A git-tracked dependency whose awaited release has shipped.

    Built by {func}`find_ready_swaps`; consumed by {func}`apply_release_swaps`
    (the `pyproject.toml` rewrite) and {func}`format_swap_section` (the PR
    report).
    """

    name: str
    """Normalized package name, as it appears on PyPI and in `uv.lock`."""

    source_key: str
    """The entry key as written in `[tool.uv.sources]` (may differ from
    {attr}`name` in case or separators)."""

    branch: str
    """The git branch the override tracks."""

    floor: str
    """The `.dev` version floor that named the awaited release."""

    release: str
    """The adopted release version, the newest stable satisfying the floor."""

    released: str
    """Upload date of {attr}`release` (`YYYY-MM-DD`), from the index."""

    @property
    def freeze_cutoff(self) -> str:
        """The `exclude-newer-package` cutoff adopting {attr}`release`.

        One day of margin past the release's earliest upload date, rendered
        as an explicit UTC timestamp, mirroring `_freeze_cutoff` (see there
        for the margin and timezone rationale): every distribution file of
        the adopted release sits inside the window even when its uploads
        straddle midnight, while the global cooldown still shields anything
        newer.
        """
        day_after = date.fromisoformat(self.released) + timedelta(days=1)
        return _date_to_utc_cutoff(day_after)


def tracked_git_overrides(pyproject_path: Path) -> dict[str, str]:
    """Read the `[tool.uv.sources]` entries tracking a git branch.

    Only single-source entries carrying both a `git` URL and a `branch` are
    returned: a `rev` or `tag` pin is a deliberate point-in-time choice, a
    path or workspace source is a local development arrangement, and a
    multi-source list (per-platform markers) is too bespoke to rewrite. None
    of those encode "waiting for the next release".

    :param pyproject_path: Path to the `pyproject.toml` file.
    :return: Source-entry key to tracked branch name; empty when the file or
        the table is absent.
    """
    if not pyproject_path.exists():
        return {}
    content = pyproject_path.read_text(encoding="UTF-8")
    sources = tomlrt.loads(content).get("tool", {}).get("uv", {}).get("sources")
    if not sources:
        return {}
    tracked = {}
    for key, value in sources.items():
        if not isinstance(value, dict):
            continue
        if "git" in value and "branch" in value and "rev" not in value:
            tracked[key] = str(value["branch"])
    return tracked


def _requirement_arrays(doc: dict) -> Iterator[list]:
    """Yield every requirement array in a parsed `pyproject.toml`.

    Covers `[project.dependencies]`, each `[project.optional-dependencies]`
    extra, and each `[dependency-groups]` group. Non-list values and non-string
    items (like `{include-group = …}` entries) are the callers' concern.
    """
    project = doc.get("project", {})
    deps = project.get("dependencies")
    if isinstance(deps, list):
        yield deps
    for table_name in ("optional-dependencies",):
        for extra in (project.get(table_name) or {}).values():
            if isinstance(extra, list):
                yield extra
    for group in (doc.get("dependency-groups") or {}).values():
        if isinstance(group, list):
            yield group


def _parse_requirement(item: object) -> Requirement | None:
    """Parse a requirement array item, returning `None` for anything else."""
    if not isinstance(item, str):
        return None
    try:
        return Requirement(item)
    except InvalidRequirement:
        return None


def dev_floor(pyproject_path: Path, name: str) -> str | None:
    """The highest `.dev` lower bound declared for *name*, if any.

    Scans every requirement array for lower-bound clauses (`>=` or `>`) whose
    version is a dev release. The highest one is the project's declared
    "awaited release" threshold.

    :param pyproject_path: Path to the `pyproject.toml` file.
    :param name: Package name (any capitalization or separator style).
    :return: The floor version string, or `None` when the package has no dev
        floor (the override is then outside the managed idiom).
    """
    if not pyproject_path.exists():
        return None
    doc = tomlrt.loads(pyproject_path.read_text(encoding="UTF-8"))
    canonical = canonicalize_name(name)
    floors = []
    for array in _requirement_arrays(doc):
        for item in array:
            requirement = _parse_requirement(item)
            if requirement is None:
                continue
            if canonicalize_name(requirement.name) != canonical:
                continue
            for spec in requirement.specifier:
                if spec.operator not in (">=", ">"):
                    continue
                try:
                    version = Version(spec.version)
                except InvalidVersion:
                    continue
                if version.is_devrelease:
                    floors.append(version)
    return str(max(floors)) if floors else None


def find_ready_swaps(pyproject_path: Path) -> list[ReleaseSwap]:
    """Probe the index for git-tracked packages whose awaited release shipped.

    For each branch-tracking override inside the managed idiom, the awaited
    release is considered shipped once PyPI carries a stable (non-prerelease,
    non-yanked) version satisfying the dev floor. The newest such release is
    adopted. Index misses (an unpublished package, a network failure) read as
    "not ready": a swap needs positive confirmation, so the failure mode is
    always a skipped run, never a wrong rewrite.

    :param pyproject_path: Path to the `pyproject.toml` file.
    :return: Ready swaps sorted by package name; empty when there is nothing
        to do.
    """
    swaps = []
    for key, branch in sorted(tracked_git_overrides(pyproject_path).items()):
        name = canonicalize_name(key)
        floor = dev_floor(pyproject_path, name)
        if floor is None:
            logging.debug(f"No dev floor for git-tracked {name}: not managed.")
            continue
        threshold = Version(floor)
        candidates = []
        for version_str, release in get_release_dates(name).items():
            if release.yanked:
                continue
            try:
                version = Version(version_str)
            except InvalidVersion:
                continue
            if version.is_prerelease or version < threshold:
                continue
            candidates.append((version, release.date))
        if not candidates:
            logging.debug(f"No stable release >= {floor} for {name} yet.")
            continue
        version, released = max(candidates)
        swaps.append(
            ReleaseSwap(
                name=name,
                source_key=key,
                branch=branch,
                floor=floor,
                release=str(version),
                released=released,
            )
        )
    return swaps


def strip_dev_bounds(requirement: str, release: str) -> str:
    """Tighten a requirement string's `.dev` lower bounds to their release.

    Rewrites only the version literal of `>=`/`>` clauses whose version is a
    dev release older than or equal to *release*, replacing it with its base
    version (`>=2.1.0.dev0` becomes `>=2.1.0`). Everything else in the string
    (extras, markers, other clauses, spacing) is preserved byte-for-byte.

    :param requirement: The PEP 508 requirement string.
    :param release: The adopted release; bounds newer than it are left alone
        (they await a later release).
    :return: The rewritten string, or the original when nothing matched.
    """
    adopted = Version(release)

    def _tighten(match: re.Match[str]) -> str:
        try:
            version = Version(match["version"])
        except InvalidVersion:
            return match[0]
        if not version.is_devrelease or version > adopted:
            return match[0]
        return f"{match['op']}{version.base_version}"

    return DEV_BOUND_PATTERN.sub(_tighten, requirement)


def apply_release_swaps(pyproject_path: Path, swaps: list[ReleaseSwap]) -> None:
    """Rewrite `pyproject.toml` for the given swaps, in one pass.

    Two of the three swap edits happen here: the `[tool.uv.sources]` override
    is removed (and the emptied table with it), and every `.dev` floor on the
    swapped packages is tightened to its base release. The third edit, the
    cooldown-bypass freeze at {attr}`ReleaseSwap.freeze_cutoff`, goes through
    {func}`repomatic.uv.upsert_exclude_newer_packages` so the insertion
    position and inline-table formatting stay canonical.

    :param pyproject_path: Path to the `pyproject.toml` file.
    :param swaps: Ready swaps from {func}`find_ready_swaps`.
    """
    doc = tomlrt.loads(pyproject_path.read_text(encoding="UTF-8"))
    uv_table = doc.get("tool", {}).get("uv", {})
    sources = uv_table.get("sources")
    for swap in swaps:
        if sources is not None and swap.source_key in sources:
            del sources[swap.source_key]
        for array in _requirement_arrays(doc):
            for index, item in enumerate(array):
                requirement = _parse_requirement(item)
                if requirement is None:
                    continue
                if canonicalize_name(requirement.name) != swap.name:
                    continue
                rewritten = strip_dev_bounds(item, swap.release)
                if rewritten != item:
                    array[index] = rewritten
        logging.info(
            f"Swapped {swap.name} from git branch {swap.branch!r} to the"
            f" released {swap.release}."
        )
    if sources is not None and not sources:
        del uv_table["sources"]
    pyproject_path.write_text(tomlrt.dumps(doc), encoding="UTF-8")


SWAP_SECTION_NOTE = (
    "Dependencies tracked from a git branch while awaiting a release, swapped"
    " back to the package index: the `[tool.uv.sources]` override is dropped,"
    " the `.dev` version floor is tightened to its release form, and a"
    " cooldown bypass freezes the adoption until it ages past the"
    " [`exclude-newer`](https://docs.astral.sh/uv/reference/settings/#exclude-newer)"
    " cutoff."
)
"""Intro paragraph for the `sync-dep-sources` swap section."""


def format_swap_section(
    swaps: list[ReleaseSwap],
    *,
    name_urls: dict[str, str] | None = None,
    reference_date: date | None = None,
) -> str:
    """Format the release swaps as a markdown section.

    The `sync-dep-sources` report section explaining the `pyproject.toml`
    hunks: one row per swapped package, with the branch it tracked, the
    release it adopted, and when that release shipped.

    :param swaps: Ready swaps from {func}`find_ready_swaps`.
    :param name_urls: Optional mapping of names to a URL the name links to.
        Names absent from the mapping render plain.
    :param reference_date: When set, the "Released" date gains a relative
        hint measured from this date.
    :return: A markdown string with a `## 🔀 Source swaps` heading and table,
        or an empty string when *swaps* is empty.
    """
    if not swaps:
        return ""
    name_urls = name_urls or {}
    lines = [
        "## 🔀 Source swaps",
        "",
        SWAP_SECTION_NOTE,
        "",
        "| Package | Tracked branch | Adopted | Released |",
        "| :-- | :-- | :-- | :-- |",
    ]
    for swap in swaps:
        link = (
            f"[{swap.name}]({name_urls[swap.name]})"
            if swap.name in name_urls
            else swap.name
        )
        released = _format_released(swap.released, reference_date)
        lines.append(
            f"| {link} | `{swap.branch}` | `{swap.release}` | {released} |"
        )
    return "\n".join(lines)
