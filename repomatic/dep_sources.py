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
"""Swap git-tracked dependencies back to their released versions, and refuse
to release while one is still in place.

Two halves, both about where a dependency actually comes from.

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

The `lint-deps` gate is the other half, and it covers what the swap does not.
A dependency is **shippable** when whoever installs the published artifact from
an index gets the same code the release was tested against. {func}`scan_project`
reports every way that breaks, and the release lane refuses to build a package
while one stands. See {class}`DepFinding` for the failure classes, and
`docs/dependencies.md` § Shippable sources for the worked example.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

import tomlrt
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

from .compat import StrEnum
from .dep_report import (
    format_released,
    link_name,
    markdown_section,
    parse_iso_datetime,
)
from .github.actions import AnnotationLevel
from .pypi import get_release_dates
from .uv import (
    LockFile,
    freeze_cutoff_after,
    load_lock_data,
    load_pyproject_doc,
    resolve_exclude_newer_cutoff,
    uv_table,
)
from .version_sync import safe_version

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Iterator

LINT_DEPS_HEADER_DEFS: tuple[tuple[str, str], ...] = (
    ("Package", "package"),
    ("Source", "kind"),
    ("Declared in", "location"),
    ("Verdict", "verdict"),
)
"""Column definitions for the `repomatic lint-deps` table.

Lives beside {class}`DepFinding` so the columns and the fields they render
cannot drift apart; the CLI derives its `--sort-by` choices from it.

{attr}`DepFinding.consequence` and {attr}`DepFinding.remedy` are deliberately
not columns: each runs to a couple of sentences, which in a fifth column
pushes the other four off the side of any terminal. They are printed as the
annotation line under the table instead, where the width is the screen's
rather than the widest cell's.
"""

PYPI_INDEX_HOSTS = frozenset({"pypi.org", "www.pypi.org"})
"""Hosts a package may be resolved from and still count as published.

Anything else is a private index, a staging index (TestPyPI lives on
`test.pypi.org`, deliberately absent) or a proxy: a user running
`pip install` or `uvx` against the default index reaches none of them, so a
dependency pinned there is no more installable than one pinned to a git
branch.
"""

WHEEL_METADATA_TABLES = ("project.dependencies", "project.optional-dependencies")
"""Requirement arrays whose entries land in the published `Requires-Dist`.

`[dependency-groups]` ([PEP 735](https://peps.python.org/pep-0735/)) is
deliberately absent: it never reaches distribution metadata. That is what
separates a finding an installer trips over from one only a contributor does,
which the report says out loud even though both block.
"""

DEV_BOUND_PATTERN = re.compile(r"(?P<op>>=?)\s*(?P<version>[0-9][A-Za-z0-9.!+]*)")
"""Lower-bound clauses in a PEP 508 requirement string.

Captures the operator and the version literal so {func}`strip_dev_bounds` can
rewrite `>=2.1.0.dev0` into `>=2.1.0` in place, leaving extras, markers, and
every other clause byte-for-byte untouched.
"""

TOML_TABLE_HEADER = re.compile(r"\s*\[{1,2}\s*(?P<path>[^]]+?)\s*\]{1,2}\s*$")
"""A `[table]` or `[[array of tables]]` header, capturing its dotted path.

Enough TOML parsing for {func}`declaration_anchor` to tell which table a line
sits in. The parsed document cannot answer that: `tomllib` and `tomlkit` both
return values, and a line number is what a link needs.
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

        Delegates the margin policy to
        {func}`repomatic.uv.freeze_cutoff_after`: every distribution file of
        the adopted release sits inside the window even when its uploads
        straddle midnight, while the global cooldown still shields anything
        newer.
        """
        return freeze_cutoff_after(date.fromisoformat(self.released))


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
    sources = uv_table(load_pyproject_doc(pyproject_path)).get("sources")
    if not sources:
        return {}
    tracked = {}
    for key, value in sources.items():
        if not isinstance(value, dict):
            continue
        if "git" in value and "branch" in value and "rev" not in value:
            tracked[key] = str(value["branch"])
    return tracked


def requirement_arrays(doc: dict) -> Iterator[tuple[str, list]]:
    """Yield every requirement array in a parsed `pyproject.toml`, labelled.

    Covers `[project.dependencies]`, each `[project.optional-dependencies]`
    extra, each `[dependency-groups]` group, and `[build-system].requires`.
    Non-list values and non-string items (like `{include-group = …}` entries)
    are the callers' concern.

    The label is the TOML path the array sits at, so a finding can name where
    a declaration lives rather than just which package it names. `lint-deps`
    reports it, and it is also what separates the tables that reach the
    published wheel's `Requires-Dist` from the ones that never leave the
    repository.

    :param doc: Parsed `pyproject.toml`.
    :return: `(location, array)` pairs, the array being the live list object
        so callers can rewrite items in place.
    """
    project = doc.get("project", {})
    deps = project.get("dependencies")
    if isinstance(deps, list):
        yield "project.dependencies", deps
    for extra, items in (project.get("optional-dependencies") or {}).items():
        if isinstance(items, list):
            yield f"project.optional-dependencies.{extra}", items
    for group, items in (doc.get("dependency-groups") or {}).items():
        if isinstance(items, list):
            yield f"dependency-groups.{group}", items
    requires = doc.get("build-system", {}).get("requires")
    if isinstance(requires, list):
        yield "build-system.requires", requires


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
    doc = load_pyproject_doc(pyproject_path)
    canonical = canonicalize_name(name)
    floors = []
    for _location, array in requirement_arrays(doc):
        for item in array:
            requirement = parse_requirement(item)
            if requirement is None:
                continue
            if canonicalize_name(requirement.name) != canonical:
                continue
            for spec in requirement.specifier:
                if spec.operator not in (">=", ">"):
                    continue
                version = safe_version(spec.version)
                if version is not None and version.is_devrelease:
                    floors.append(version)
    return str(max(floors)) if floors else None


def floors_inside_cooldown(
    pyproject_path: Path,
    lock_path: Path,
    window: str,
) -> dict[str, str]:
    """Dependency floors that no cooldown-gated resolution can satisfy.

    A floor naming a version published inside the cooldown window makes the
    *published package* uninstallable. Anyone resolving it from an index
    (a downstream repo running a frozen workflow's `uvx 'repomatic==X.Y.Z'`, or
    an end user running `uvx repomatic`) gets a tool environment, which reads
    neither `uv.lock` nor `[tool.uv] exclude-newer-package`. Since uv exposes no
    environment variable for a per-package exemption either, there is nowhere
    for them to record the bypass.

    ```{caution}
    This repository cannot feel the breakage it would ship. Its own workflows
    install from `uv.lock` (see
    {data}`repomatic.prepare_release.LOCAL_CLI_INVOCATION`), which resolves
    through the local `exclude-newer-package` exemption and stays green. The
    failure lands only on whoever installs the release, which is why it needs a
    gate here rather than a red CI run to catch it.
    ```

    Wait for a release to age out of the window before raising a floor onto it.

    The comparison runs against the locked version's upload time, which
    `uv.lock` records, so the check needs no network. A floor is reported when
    the locked version sits inside the window *and* the floor demands at least
    that version: releases reach an index in version order, so nothing
    satisfying such a floor can be older than what is already locked.

    :param pyproject_path: Path to the `pyproject.toml` file.
    :param lock_path: Path to the `uv.lock` file.
    :param window: Cooldown window, in any form `[tool.uv] exclude-newer`
        accepts.
    :return: Mapping of canonical package name to the offending floor version,
        empty when every floor resolves without an exemption.
    """
    cutoff = resolve_exclude_newer_cutoff(window)
    if cutoff is None or not pyproject_path.exists():
        return {}

    lock = LockFile.load(lock_path)
    locked = {
        canonicalize_name(name): version for name, version in lock.versions.items()
    }
    uploads = {
        canonicalize_name(name): stamp for name, stamp in lock.upload_times.items()
    }

    doc = load_pyproject_doc(pyproject_path)
    offenders: dict[str, str] = {}
    for _location, array in requirement_arrays(doc):
        for item in array:
            requirement = parse_requirement(item)
            if requirement is None:
                continue
            name = canonicalize_name(requirement.name)
            locked_version = locked.get(name)
            upload = uploads.get(name)
            if not locked_version or not upload:
                continue
            upload_dt = parse_iso_datetime(upload)
            # A locked version older than the cutoff resolves on its own, so
            # no floor pointing at it can need an exemption.
            if upload_dt is None or upload_dt < cutoff:
                continue
            locked_parsed = safe_version(locked_version)
            if locked_parsed is None:
                continue
            for spec in requirement.specifier:
                if spec.operator not in (">=", "=="):
                    continue
                floor = safe_version(spec.version)
                if floor is not None and floor >= locked_parsed:
                    offenders[name] = str(floor)
    return offenders


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
            version = safe_version(version_str)
            if version is None or version.is_prerelease or version < threshold:
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
        version = safe_version(match["version"])
        if version is None or not version.is_devrelease or version > adopted:
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
    doc = load_pyproject_doc(pyproject_path)
    uv = uv_table(doc)
    sources = uv.get("sources")
    for swap in swaps:
        if sources is not None and swap.source_key in sources:
            del sources[swap.source_key]
        for _location, array in requirement_arrays(doc):
            for index, item in enumerate(array):
                requirement = parse_requirement(item)
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
        del uv["sources"]
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
    return markdown_section(
        "🔀 Source swaps",
        SWAP_SECTION_NOTE,
        ("Package", "Tracked branch", "Adopted", "Released"),
        [
            (
                link_name(swap.name, name_urls),
                f"`{swap.branch}`",
                f"`{swap.release}`",
                format_released(swap.released, reference_date),
            )
            for swap in swaps
        ],
    )


# ---------------------------------------------------------------------------
# Shippability gate (`lint-deps`)
# ---------------------------------------------------------------------------


class SourceKind(StrEnum):
    """Where a dependency is resolved from.

    The vocabulary is shared by `[tool.uv.sources]` and `uv.lock`, which name
    the same concepts with the same keys, so {func}`classify_source` reads
    both. Only {attr}`REGISTRY` describes something an installer of the
    published artifact can reach on its own.
    """

    DIRECT_REFERENCE = "direct reference"
    """A PEP 508 `name @ url` clause written into the requirement itself."""

    GIT = "git"
    """A git repository, whether tracked by branch, tag or commit."""

    INDEX = "index"
    """A named `[[tool.uv.index]]` other than PyPI."""

    PATH = "path"
    """A local directory, including an editable install or a lock `directory`
    entry."""

    REGISTRY = "registry"
    """A package index. Shippable when the index is PyPI."""

    URL = "url"
    """A direct artifact URL (a wheel or sdist served over HTTP)."""

    WORKSPACE = "workspace"
    """Another member of the same uv workspace."""


@dataclass(frozen=True)
class DepFinding:
    """One reason the project cannot be published as it stands.

    Findings are what {func}`scan_project` returns, and they carry their own
    explanation rather than a code the caller has to map: the CLI table, the
    GitHub annotation and the release PR banner all render the same three
    sentences, so a maintainer reads one wording wherever they meet it.
    """

    package: str
    """Normalized name of the offending package."""

    kind: SourceKind
    """How that package is resolved."""

    location: str
    """The TOML path (or `uv.lock`) the declaration was read from."""

    detail: str
    """The declaration as written, for the reader to go find."""

    consequence: str
    """What breaks, for whom, if this ships."""

    remedy: str
    """The next action, naming the automation that already covers it."""

    level: AnnotationLevel = AnnotationLevel.ERROR
    """Severity. Only {attr}`~repomatic.github.actions.AnnotationLevel.ERROR`
    blocks a release."""

    allowed: str = ""
    """The `[tool.repomatic] lint-deps.allow` reason, when one covers this
    package. A non-empty reason downgrades the finding to a notice that still
    renders, so an accepted exception stays visible instead of disappearing."""

    @property
    def blocking(self) -> bool:
        """Whether this finding stops a release."""
        return self.level is AnnotationLevel.ERROR and not self.allowed

    @property
    def verdict(self) -> str:
        """One-word outcome, for the table's last column."""
        if self.allowed:
            return f"allowed ({self.allowed})"
        return "blocks release" if self.blocking else "warning"

    @property
    def message(self) -> str:
        """The finding as a single annotation line."""
        return (
            f"{self.package}: {self.kind} source in {self.location}"
            f" ({self.detail}). {self.consequence} {self.remedy}"
        )


def is_pypi_url(url: str) -> bool:
    """Whether *url* points at the public Python Package Index.

    :param url: An index or registry URL.
    :return: `True` when its host is one of {data}`PYPI_INDEX_HOSTS`.
    """
    return urlsplit(url).hostname in PYPI_INDEX_HOSTS


def classify_source(value: object) -> SourceKind | None:
    """Read a `[tool.uv.sources]` entry or a `uv.lock` source table.

    Both spell the same concepts with the same keys, so one classifier serves
    the declaration and its resolution. Checked most-specific first: a
    `{ path = "…", editable = true }` entry is a path source, not two.

    :param value: The mapping sitting under a source key.
    :return: The kind, or `None` for anything unrecognized (a bare marker
        table, a future uv key). Unknown shapes are not reported: a gate that
        guesses would block releases over syntax it does not understand.
    """
    if not isinstance(value, dict):
        return None
    if "git" in value:
        return SourceKind.GIT
    if "url" in value:
        return SourceKind.URL
    if any(key in value for key in ("path", "directory", "editable")):
        return SourceKind.PATH
    if value.get("workspace"):
        return SourceKind.WORKSPACE
    if "index" in value:
        return SourceKind.INDEX
    if "registry" in value:
        return SourceKind.REGISTRY
    return None


def _reaches_installers(location: str) -> bool:
    """Whether a declaration at *location* lands in published metadata."""
    return location.startswith(WHEEL_METADATA_TABLES)


def declared_requirements(doc: dict, name: str) -> list[tuple[str, str]]:
    """Every requirement string declaring *name*, with its TOML location.

    A `[tool.uv.sources]` entry says where a package comes from but not
    whether anyone downstream will feel it. That answer lives in the
    requirement arrays, and it is what decides the consequence: a package
    named in `[project.dependencies]` ships a requirement the index must
    satisfy, one named only in `[dependency-groups]` ships nothing at all,
    and one named nowhere is a transitive dependency being swapped underneath
    the resolver.

    :param doc: Parsed `pyproject.toml`.
    :param name: Package name, in any capitalization or separator style.
    :return: `(location, requirement string)` pairs, empty when the package
        is not declared directly.
    """
    canonical = canonicalize_name(name)
    declarations = []
    for location, array in requirement_arrays(doc):
        for item in array:
            requirement = parse_requirement(item)
            if requirement is None:
                continue
            if canonicalize_name(requirement.name) == canonical:
                declarations.append((location, str(item)))
    return declarations


def _unreleased_floor(requirement: str) -> bool:
    """Whether *requirement* has a lower bound no index can satisfy.

    A bound naming a dev or pre-release version (`mango>=2.1.0.dev0`) is the
    signature of a floor written against unreleased code: it is what a
    project declares while consuming that code from git, and it is exactly
    what PyPI cannot serve. A bound naming an ordinary release is assumed
    servable, since the project locked against it.
    """
    parsed = parse_requirement(requirement)
    if parsed is None:
        return False
    for spec in parsed.specifier:
        if spec.operator not in (">=", ">", "=="):
            continue
        version = safe_version(spec.version)
        if version is not None and (version.is_devrelease or version.is_prerelease):
            return True
    return False


def _consequence(
    kind: SourceKind,
    declarations: list[tuple[str, str]],
    from_metadata_table: bool | None = None,
) -> str:
    """Spell out what a finding costs, given where the package is declared.

    Five distinct outcomes hide behind "unshippable", and telling them apart
    is most of the report's value. What a reader has to do about a release
    that cannot be uploaded, one that uploads and then fails every install,
    and one that installs a different build than was tested are three
    different jobs, and only the first announces itself.

    :param kind: How the package is resolved.
    :param declarations: Output of {func}`declared_requirements`.
    :param from_metadata_table: Override for the "does it ship" question,
        used by the direct-reference pass which already knows the exact array
        the offending string sits in.
    """
    shipped = [
        requirement
        for location, requirement in declarations
        if _reaches_installers(location)
    ]
    ships = from_metadata_table if from_metadata_table is not None else bool(shipped)

    if kind is SourceKind.DIRECT_REFERENCE and ships:
        return (
            "PyPI refuses an upload whose metadata carries a direct reference,"
            " so the release would be tagged and published on GitHub and only"
            " then fail, at the very last step."
        )
    if ships and any(_unreleased_floor(requirement) for requirement in shipped):
        quoted = next(req for req in shipped if _unreleased_floor(req))
        return (
            f"A source override never reaches published metadata, so the wheel"
            f" uploads cleanly, declares `{quoted}`, and sends installers to an"
            f" index carrying no such release: every install of this release"
            f" fails."
        )
    if ships:
        quoted = f" `{shipped[0]}`" if shipped else " this package"
        return (
            f"A source override never reaches published metadata, so the wheel"
            f" declares{quoted} against the default index: installers resolve"
            f" whatever is published under that name, which is not the code"
            f" this release was built and tested against."
        )
    if not declarations:
        return (
            "Nothing declares this package directly, so the override swaps a"
            " transitive dependency: published metadata still sends installers"
            " to the index version, and this release is tested against code"
            " its users never receive."
        )
    return (
        "This never reaches published metadata, but the lockfile pins it to a"
        " revision the tag cannot outlive: once the branch moves or the fork"
        " goes, the released tag no longer builds for contributors."
    )


def _remedy(kind: SourceKind, package: str, floor: str | None) -> str:
    """The next action for a finding, routed to whatever already automates it."""
    if kind is SourceKind.GIT and floor:
        return (
            f"`sync-dep-sources` already watches for the release satisfying"
            f" `{package}>={floor}` and will open the swap PR on its own: wait"
            f" for it, or release once it lands."
        )
    if kind is SourceKind.GIT:
        return (
            "Pair the override with a `.dev` version floor naming the awaited"
            " release, and `sync-dep-sources` takes the swap from there; drop"
            " the override; or, when upstream has named no awaited release to"
            " floor against and the published metadata resolves to something"
            " tested, add it to `[tool.repomatic] lint-deps.allow` with the"
            " reason it is safe."
        )
    if kind is SourceKind.DIRECT_REFERENCE:
        return (
            "Replace it with an ordinary version specifier, publishing the"
            " fork under its own name if the change is not upstream yet."
        )
    if kind in (SourceKind.PATH, SourceKind.WORKSPACE):
        return (
            "Publish the package and depend on the released version, or add it"
            " to `[tool.repomatic] lint-deps.allow` with the reason it is safe."
        )
    return (
        "Resolve the package from PyPI, or add it to `[tool.repomatic]"
        " lint-deps.allow` with the reason it is safe."
    )


def _uv_index_entries(uv: dict) -> list[dict]:
    """The `[[tool.uv.index]]` array, defensively typed."""
    indexes = uv.get("index") or []
    if not isinstance(indexes, list):
        return []
    return [entry for entry in indexes if isinstance(entry, dict)]


def _uv_index_urls(uv: dict) -> dict[str, str]:
    """Map each `[[tool.uv.index]]` name to its URL."""
    return {
        str(entry["name"]): str(entry.get("url", ""))
        for entry in _uv_index_entries(uv)
        if entry.get("name")
    }


def _finding(
    package: str,
    kind: SourceKind,
    location: str,
    detail: str,
    allow: dict[str, str],
    declarations: list[tuple[str, str]] | None = None,
    floor: str | None = None,
    level: AnnotationLevel = AnnotationLevel.ERROR,
    from_metadata_table: bool | None = None,
) -> DepFinding:
    """Assemble a finding, resolving its allowlist reason and prose."""
    return DepFinding(
        package=package,
        kind=kind,
        location=location,
        detail=detail,
        consequence=_consequence(kind, declarations or [], from_metadata_table),
        remedy=_remedy(kind, package, floor),
        level=level,
        allowed=allow.get(package, ""),
    )


def scan_pyproject(
    pyproject_path: Path,
    allow: dict[str, str] | None = None,
) -> list[DepFinding]:
    """Report every unshippable declaration in `pyproject.toml`.

    Covers what the project *says*, which is the half a reader can act on
    directly:

    - a `[tool.uv.sources]` entry resolving from anywhere but PyPI,
    - a PEP 508 direct reference (`name @ git+…`) in any requirement array,
      `[build-system].requires` included,
    - a `[[tool.uv.index]]` marked `default` that is not PyPI,
    - a non-empty `override-dependencies` or `constraint-dependencies`, which
      is reported as a warning rather than a block: those name a version
      rather than an unreleased artifact, so what they change is the tested
      resolution, not the installability of the result.

    :param pyproject_path: Path to the `pyproject.toml` file.
    :param allow: Package name to the reason it may ship from a non-index
        source, from `[tool.repomatic] lint-deps.allow`.
    :return: Findings, unsorted; {func}`scan_project` orders them.
    """
    allow = allow or {}
    if not pyproject_path.exists():
        return []
    doc = load_pyproject_doc(pyproject_path)
    uv = uv_table(doc)
    index_urls = _uv_index_urls(uv)
    findings: list[DepFinding] = []

    for key, value in (uv.get("sources") or {}).items():
        name = canonicalize_name(str(key))
        # A per-platform override is a list of single-source tables, each
        # carrying its own marker: classify every element, since only one of
        # them needs to be unshippable for the release to be. Reported once
        # per kind, so a package tracked from git on three platforms is one
        # finding rather than three identical ones.
        entries = value if isinstance(value, list) else [value]
        seen: set[SourceKind] = set()
        for entry in entries:
            kind = classify_source(entry)
            if kind is None or kind in seen:
                continue
            if kind is SourceKind.INDEX:
                index_name = str(entry.get("index", ""))
                # Recorded as seen only once it actually reports, or a PyPI
                # element early in the list would mask a private one after it.
                if is_pypi_url(index_urls.get(index_name, "")):
                    continue
                detail = f"`index = {index_name!r}`"
            else:
                detail = f"`[tool.uv.sources] {key} = {entry}`"
            seen.add(kind)
            findings.append(
                _finding(
                    name,
                    kind,
                    "tool.uv.sources",
                    detail,
                    allow,
                    declarations=declared_requirements(doc, name),
                    floor=dev_floor(pyproject_path, name),
                )
            )

    for location, array in requirement_arrays(doc):
        for item in array:
            requirement = parse_requirement(item)
            if requirement is None or not requirement.url:
                continue
            findings.append(
                _finding(
                    canonicalize_name(requirement.name),
                    SourceKind.DIRECT_REFERENCE,
                    location,
                    f"`{item}`",
                    allow,
                    from_metadata_table=_reaches_installers(location),
                )
            )

    for entry in _uv_index_entries(uv):
        if entry.get("default") and not is_pypi_url(str(entry.get("url", ""))):
            findings.append(
                _finding(
                    str(entry.get("name", "?")),
                    SourceKind.INDEX,
                    "tool.uv.index",
                    f"`default = true`, `url = {entry.get('url', '')!r}`",
                    allow,
                )
            )

    for key in ("override-dependencies", "constraint-dependencies"):
        for item in uv.get(key) or []:
            requirement = parse_requirement(item)
            if requirement is None:
                continue
            name = canonicalize_name(requirement.name)
            findings.append(
                DepFinding(
                    package=name,
                    kind=SourceKind.REGISTRY,
                    location=f"tool.uv.{key}",
                    detail=f"`{item}`",
                    consequence=(
                        "This rewrites the resolution for whoever locks this"
                        " project and for nobody else, so the release is"
                        " tested against a tree its users do not get. It names"
                        " a published version rather than an unreleased"
                        " artifact, so the result still installs."
                    ),
                    remedy=(
                        "Move the constraint into the dependency's own"
                        " specifier when it is a real requirement, so"
                        " installers honor it too."
                    ),
                    level=AnnotationLevel.WARNING,
                    allowed=allow.get(name, ""),
                )
            )

    return findings


def scan_lock(
    lock_path: Path,
    allow: dict[str, str] | None = None,
    doc: dict | None = None,
) -> list[DepFinding]:
    """Report every package `uv.lock` resolves from outside PyPI.

    The complement to {func}`scan_pyproject`, and the reason the gate is not
    a list of hand-written rules: the lock records the *resolved* source of
    every package in the tree, so a git dependency pulled in by another git
    dependency shows up here even though no table in `pyproject.toml` names
    it.

    The project's own entry is skipped. uv writes it as `{ editable = "." }`
    for a package and `{ virtual = "." }` for a virtual project, and neither
    describes a dependency. A workspace *member* is a different path (like
    `{ editable = "packages/mango" }`) and is reported, since publishing this
    project does not publish that one.

    :param lock_path: Path to the `uv.lock` file.
    :param allow: Package name to the reason it may ship from a non-index
        source.
    :param doc: Parsed `pyproject.toml`, when the caller has one. Supplying it
        lets each finding say whether the package is declared directly, which
        is what separates "every install fails" from "a transitive dependency
        was swapped underneath the resolver".
    :return: Findings, unsorted.
    """
    allow = allow or {}
    findings: list[DepFinding] = []
    for package in load_lock_data(lock_path).get("package", []):
        source = package.get("source", {})
        name = canonicalize_name(str(package.get("name", "")))
        if not name:
            continue
        if source.get("editable") == "." or source.get("virtual") == ".":
            continue
        kind = classify_source(source)
        if kind is None:
            continue
        if kind is SourceKind.REGISTRY:
            registry = str(source.get("registry", ""))
            if is_pypi_url(registry):
                continue
            detail = f"`registry = {registry!r}`"
            kind = SourceKind.INDEX
        else:
            detail = f"`source = {source}`"
        findings.append(
            _finding(
                name,
                kind,
                "uv.lock",
                detail,
                allow,
                declarations=declared_requirements(doc, name) if doc else None,
            )
        )
    return findings


def scan_project(
    pyproject_path: Path,
    lock_path: Path,
    window: str,
    allow: dict[str, str] | None = None,
) -> list[DepFinding]:
    """Every reason this project cannot be released as it stands.

    Folds the three checks into one ordered report: what `pyproject.toml`
    declares ({func}`scan_pyproject`), what `uv.lock` resolved
    ({func}`scan_lock`), and which floors no cooldown-gated resolution can
    satisfy ({func}`floors_inside_cooldown`). Entirely offline, so it costs
    nothing to run on every push and cannot fail on a flaky index.

    A package flagged by both halves is reported once, keeping the
    `pyproject.toml` finding: that is where the reader has something to edit,
    the lock being a derived file.

    :param pyproject_path: Path to the `pyproject.toml` file.
    :param lock_path: Path to the `uv.lock` file.
    :param window: Cooldown window, in any form `[tool.uv] exclude-newer`
        accepts.
    :param allow: Package name to the reason it may ship from a non-index
        source.
    :return: Findings sorted by package, then by location.
    """
    allow = allow or {}
    doc = load_pyproject_doc(pyproject_path) if pyproject_path.exists() else {}
    findings = scan_pyproject(pyproject_path, allow)
    reported = {finding.package for finding in findings}
    findings += [
        finding
        for finding in scan_lock(lock_path, allow, doc=doc)
        if finding.package not in reported
    ]

    stale_floors = floors_inside_cooldown(pyproject_path, lock_path, window)
    for name, floor in stale_floors.items():
        findings.append(
            DepFinding(
                package=name,
                kind=SourceKind.REGISTRY,
                location="project dependency floor",
                detail=f"`{name}>={floor}`",
                consequence=(
                    f"The release satisfying this floor is younger than the"
                    f" {window} cooldown, so anyone installing from an index"
                    f" resolves nothing: they read neither `uv.lock` nor"
                    f" `[tool.uv] exclude-newer-package`, and uv has no"
                    f" environment variable for a per-package exemption."
                ),
                remedy="Wait for that release to age out of the window.",
                allowed=allow.get(name, ""),
            )
        )

    return sorted(findings, key=lambda f: (f.package, f.location))


BLOCKER_SECTION_NOTE = (
    "A dependency is shippable when whoever installs the published artifact"
    " gets the code this release was tested against. These do not clear that"
    " bar, so the release lane refuses to build a package while they stand."
    " See [Dependency management § Shippable"
    " sources](https://kdeldycke.github.io/repomatic/dependencies.html#shippable-sources)."
)
"""Intro paragraph for the `lint-deps` blocker section."""


def format_blocker_section(
    findings: list[DepFinding],
    *,
    heading: str = "🚧 Unshippable dependencies",
) -> str:
    """Format blocking findings as a markdown section.

    The long form, for the `lint-deps` report: a reader who opened that report
    came for the diagnosis, so it carries the note and the full table. The
    release PR gets {func}`build_release_readiness` instead, which is the same
    findings at banner length.

    :param findings: Findings from {func}`scan_project`.
    :param heading: Section heading, emoji included.
    :return: A markdown string, or an empty string when nothing blocks.
    """
    blocking = [finding for finding in findings if finding.blocking]
    if not blocking:
        return ""
    return markdown_section(
        heading,
        BLOCKER_SECTION_NOTE,
        ("Package", "Source", "Declared in", "Why it cannot ship"),
        [
            (
                f"`{finding.package}`",
                f"`{finding.kind}`",
                f"`{finding.location}`",
                f"{finding.consequence} {finding.remedy}",
            )
            for finding in blocking
        ],
    )


def declaration_anchor(
    finding: DepFinding,
    pyproject_path: Path,
    lock_path: Path,
) -> str:
    """Locate the declaration behind a finding, as a repository-relative link.

    Only two files can hold one: `uv.lock` for a source the resolver picked,
    `pyproject.toml` for everything the project wrote itself, dependency
    floors included.

    :param finding: The finding to locate.
    :param pyproject_path: Path to the `pyproject.toml` file.
    :param lock_path: Path to the `uv.lock` file.
    :return: The file name, suffixed with `#L{n}` once the declaring line is
        found. A line that cannot be found degrades to the bare file rather
        than to a guess: an anchor pointing at the wrong line costs the reader
        more than no anchor at all.
    """
    in_lock = finding.location == "uv.lock"
    path = lock_path if in_lock else pyproject_path
    # A canonical name separates its parts with "-", where the declaration may
    # spell any of "-", "_" or "." and pick its own case.
    stem = "[-_.]+".join(re.escape(part) for part in finding.package.split("-"))
    # `uv.lock` names a package on the `name` key of its `[[package]]` block;
    # `pyproject.toml` writes it inside a requirement string or as a table key,
    # so match it as a bare token there.
    pattern = re.compile(
        rf'name = "{stem}"' if in_lock else rf"(?<![\w.-]){stem}(?![\w-])",
        re.IGNORECASE,
    )
    try:
        lines = path.read_text(encoding="UTF-8").splitlines()
    except OSError:
        return path.name

    first = 0
    best = 0
    best_depth = -1
    table = ""
    for number, line in enumerate(lines, start=1):
        header = TOML_TABLE_HEADER.match(line)
        if header:
            table = header["path"]
        if not pattern.search(line):
            continue
        if not first:
            first = number
        # A package is named wherever it is required, so a plain first-match
        # search lands on the requirement rather than on the source override
        # that made it unshippable. Prefer the deepest table the finding's own
        # location sits under: that is the declaration to edit, where the
        # others merely mention the package.
        in_scope = finding.location == table or finding.location.startswith(f"{table}.")
        if table and in_scope and len(table) > best_depth:
            best, best_depth = number, len(table)

    number = best or first
    return f"{path.name}#L{number}" if number else path.name


RELEASE_READY_SENTENCE = "This PR is ready to be merged. "
"""How the release checklist opens when nothing blocks.

Trailing space included: it runs inline into the sentence the template
follows it with, where the blocked form is a standalone blockquote instead.
"""


UNSHIPPABLE_BANNER_LEAD = (
    "Do not merge yet. This release would ship dependencies its users cannot install:"
)
"""Opening of the blocked form of the release PR's verdict.

The banner is a verdict, not a report: it says what is wrong and names what to
open. Everything else the finding carries (why the source is unshippable, what
to do about it, the general rule) reads as chatter in a pull request whose
body is otherwise a five-step checklist, and it is one click away in the
`lint-deps` report {func}`format_blocker_section` renders.
"""


def build_release_readiness(
    pyproject_path: Path,
    lock_path: Path,
    window: str,
    allow: dict[str, str] | None = None,
    source_url: str | None = None,
) -> str:
    """Build the release PR's opening verdict.

    The `prepare-release` checklist has always opened with "This PR is ready
    to be merged", and that sentence is a lie while a dependency resolves
    from a git branch, a fork or a local path. So the opening is owned here
    rather than hard-coded in the template: it stays that sentence while the
    project is releasable, and becomes a `[!CAUTION]` block naming every
    offending dependency when it is not.

    This is the layer that matters, even though the release lane carries a
    hard gate of its own. By the time that gate fires the freeze commit is
    already on `main`, and the recovery is to burn the version per
    `claude.md` § Skip and move forward. This body is regenerated on every
    push to `main`, so it carries the same answer days earlier, in the one
    place a maintainer reads before deciding to merge.

    ```{note}
    Lives here rather than beside the other `pr-body` template-argument
    builders in {mod}`repomatic.github.pr_body`, which is where it would
    otherwise belong: that module is imported by {mod}`repomatic.dep_report`,
    so reaching {func}`scan_project` from it closes an import cycle.
    ```

    :param pyproject_path: Path to the `pyproject.toml` file.
    :param lock_path: Path to the `uv.lock` file.
    :param window: Cooldown window, from `[tool.repomatic] minimum-release-age`.
    :param allow: Package name to its `lint-deps.allow` reason.
    :param source_url: Blob URL the declarations hang off, without a trailing
        slash (like ``{repo_url}/blob/{sha}``). Each package links into it. A
        caller with no commit to point at passes nothing, and the packages
        render with their file and line as plain text instead.
    :return: {data}`RELEASE_READY_SENTENCE`, or a one-line GitHub-flavored
        markdown `[!CAUTION]` blockquote naming what blocks the release.
    """
    blocking = [
        finding
        for finding in scan_project(pyproject_path, lock_path, window, allow=allow)
        if finding.blocking
    ]
    if not blocking:
        return RELEASE_READY_SENTENCE
    # One link per package: a floor and a source override on the same package
    # send the reader to the same file, and a name repeated in a one-line
    # banner reads as two separate problems. Findings arrive sorted by package
    # then location, so the first one kept is the one nearest to an edit.
    anchors: dict[str, str] = {}
    for finding in blocking:
        if finding.package not in anchors:
            anchors[finding.package] = declaration_anchor(
                finding, pyproject_path, lock_path
            )
    links = ", ".join(
        f"[`{package}`]({source_url}/{anchor})"
        if source_url
        else f"`{package}` ({anchor})"
        for package, anchor in anchors.items()
    )
    return f"> [!CAUTION]\n> **{UNSHIPPABLE_BANNER_LEAD}** {links}\n\n"
