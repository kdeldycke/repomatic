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

"""Refuse to release while a dependency resolves from anywhere but the index.

A dependency is **shippable** when whoever installs the published artifact from
an index gets the same code the release was tested against. {func}`scan_project`
reports every way that breaks, and the release lane refuses to build a package
while one stands. A new rule goes into {func}`scan_pyproject` or
{func}`scan_lock`, never into a call site, so every place that runs the gate
enforces it. See {class}`DepFinding` for the failure classes, and
`docs/dependencies.md` § Shippable sources for the worked example.

The `lint-deps` command renders the findings, and the release PR opens with
{func}`build_release_readiness`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import NamedTuple
from urllib.parse import quote_plus

from click_extra import ColumnSpec
from packaging.utils import canonicalize_name

from ..github.outputs import AnnotationLevel
from ..humanize import format_countdown
from ..pypi import is_pypi_url
from ..pyproject import uv_table
from ..tabular import render_markdown_table
from ..versions import safe_version
from .cooldown import resolve_exclude_newer_cutoff, resolve_exclude_newer_span
from .lockfile import LockFile
from .report import BYPASS_NEEDS_RELEASE, markdown_section
from .requirements import (
    LOWER_BOUND_OPERATORS,
    PIN_OPERATOR,
    RequirementArray,
    dev_floor,
    iter_toml_lines,
    parse_requirement,
    pep503_pattern,
    read_project,
    requirement_arrays,
)

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path
    from typing import Any

LINT_DEPS_HEADER_DEFS: tuple[ColumnSpec, ...] = (
    ColumnSpec("package", "Package"),
    ColumnSpec("kind", "Source"),
    ColumnSpec("location", "Declared in"),
    ColumnSpec("verdict", "Verdict"),
)
"""Column definitions for the `repomatic lint-deps` table.

Lives beside {class}`DepFinding`, whose {attr}`~DepFinding.row` fills the
columns, so the two cannot drift apart. The CLI derives its `--sort-by`
choices from it.

{attr}`DepFinding.consequence` and {attr}`DepFinding.remedy` are deliberately
not columns: each runs to a couple of sentences, which in a fifth column
pushes the other four off the side of any terminal. They are printed as the
annotation line under the table instead, where the width is the screen's
rather than the widest cell's.
"""

BLOCKER_NEEDS_EDIT = "needs an edit"
"""Countdown placeholder for a blocker no automation ever lifts.

The counterpart to {data}`~repomatic.deps.report.BYPASS_NEEDS_RELEASE`,
and what most failure classes get: a path source, a workspace member, a direct
reference and a floor-less git track all wait on a maintainer rather than on a
date. Naming that beats an empty cell, which reads as a date the report failed
to compute. {attr}`DepFinding.remedy` names the edit.
"""

COOLDOWN_PR_BRANCH = "sync-uv-lock"
"""Branch of the pull request that reports a cooldown clearing.

Its `❄️ Cooldown bypasses` table forecasts the same day from the same two
numbers a blocking floor does: the locked release's `upload-time`, plus the
`exclude-newer` span. So a dated blocker and that table always agree, and the
banner links one to the other.
"""

SWAP_PR_BRANCH = "sync-dep-sources"
"""Branch of the pull request that lands a git-to-release swap.

Where a {data}`~repomatic.deps.report.BYPASS_NEEDS_RELEASE` blocker
resolves: the job watches the index and opens the swap once the awaited
release ships.
"""


def clearing_queue_url(repo_url: str, branch: str) -> str:
    """Link to the open pull request an updater has on its own branch.

    A search over the branch rather than a pull request number, because the
    number changes every cycle: `sync-uv-lock` opened eight in the 24 days to
    2026-09-04 and closed seven of them, so a number written into a release PR
    body is stale within days. The branch never moves, and the query costs no
    API call to build, which keeps {func}`scan_project` offline.

    Restricted to open pull requests, because a merged one carries the cooldown
    forecast it computed on its last push, and the date can have moved since. An
    empty result answers the reader instead: nothing is pending on that branch,
    so the lock has converged.

    :param repo_url: Repository homepage, without a trailing slash.
    :param branch: The updater's fixed pull request branch.
    :return: A GitHub pull request search URL.
    """
    return f"{repo_url}/pulls?q={quote_plus(f'is:pr is:open head:{branch}')}"


class SourceKind(StrEnum):
    """Where a dependency is resolved from.

    The vocabulary is shared by `[tool.uv.sources]` and `uv.lock`, which name
    the same concepts with the same keys, so {meth}`classify` reads both. Only
    {attr}`REGISTRY` describes something an installer of the published
    artifact can reach on its own.
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

    @classmethod
    def classify(cls, value: object) -> SourceKind | None:
        """Read a `[tool.uv.sources]` entry or a `uv.lock` source table.

        Both spell the same concepts with the same keys, so one classifier
        serves the declaration and its resolution. Checked most-specific first:
        a `{ path = "…", editable = true }` entry is a path source, not two.

        :param value: The mapping sitting under a source key.
        :return: The kind, or `None` for anything unrecognized (a bare marker
            table, a future uv key). Unknown shapes are not reported: a gate
            that guesses would block releases over syntax it does not
            understand.
        """
        if not isinstance(value, dict):
            return None
        if "git" in value:
            return cls.GIT
        if "url" in value:
            return cls.URL
        if any(key in value for key in ("path", "directory", "editable")):
            return cls.PATH
        if value.get("workspace"):
            return cls.WORKSPACE
        if "index" in value:
            return cls.INDEX
        if "registry" in value:
            return cls.REGISTRY
        return None


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
    """Severity. Only {attr}`~repomatic.github.outputs.AnnotationLevel.ERROR`
    blocks a release."""

    allowed: str = ""
    """The `[tool.repomatic] lint-deps.allow` reason, when one covers this
    package. A non-empty reason downgrades the finding to a notice that still
    renders, so an accepted exception stays visible instead of disappearing."""

    clears: str = ""
    """When this finding lifts without anyone touching `pyproject.toml`.

    Either a date with a countdown (`2026-09-10 (in 3 days)`) for a cooldown
    the clock ages out, or {data}`~repomatic.deps.report.BYPASS_NEEDS_RELEASE`
    for a swap `sync-dep-sources` opens once upstream publishes. Empty when no
    automation clears it and a maintainer has to edit the declaration.
    {attr}`countdown` renders it.
    """

    @property
    def blocking(self) -> bool:
        """Whether this finding stops a release."""
        return self.level is AnnotationLevel.ERROR and not self.allowed

    @property
    def awaits_release(self) -> bool:
        """Whether the finding lifts once upstream publishes a release."""
        return self.clears == BYPASS_NEEDS_RELEASE

    @property
    def countdown(self) -> str:
        """{attr}`clears` as a table cell, marked with what the reader waits on.

        Three states, and the reader's next move differs for each: wait for a
        date, wait for someone else's release, or go edit the declaration. The
        two undated ones carry the marker the `sync-uv-lock` cooldown tables
        already use for them, italicized so a marker never reads as a date.
        """
        if not self.clears:
            return f"✋ *{BLOCKER_NEEDS_EDIT}*"
        if self.awaits_release:
            return f"🚧 *{self.clears}*"
        return self.clears

    @property
    def clearing_job(self) -> str:
        """Pull request branch of the job that lifts this finding.

        Empty for a finding waiting on a maintainer, since no job opens a pull
        request for it. Keyed off {attr}`clears` rather than off
        {attr}`kind`, so the job and the marker a reader sees can never
        disagree about who they are waiting for.
        """
        if not self.clears:
            return ""
        return SWAP_PR_BRANCH if self.awaits_release else COOLDOWN_PR_BRANCH

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

    @property
    def row(self) -> tuple[str, str, str, str]:
        """The finding as a row of {data}`LINT_DEPS_HEADER_DEFS`."""
        return self.package, str(self.kind), self.location, self.verdict


class Declaration(NamedTuple):
    """A requirement string naming a package, and the array declaring it."""

    array: RequirementArray
    """The array the requirement sits in."""

    requirement: str
    """The requirement, as written."""


def declared_requirements(doc: Mapping[str, Any], name: str) -> list[Declaration]:
    """Every requirement string declaring *name*, with the array it sits in.

    A `[tool.uv.sources]` entry says where a package comes from but not
    whether anyone downstream will feel it. That answer lives in the
    requirement arrays, and it is what decides the consequence: a package
    named in `[project.dependencies]` ships a requirement the index must
    satisfy, one named only in `[dependency-groups]` ships nothing at all,
    and one named nowhere is a transitive dependency being swapped underneath
    the resolver.

    :param doc: Parsed `pyproject.toml`.
    :param name: Package name, in any capitalization or separator style.
    :return: The declarations, empty when the package is not declared directly.
    """
    canonical = canonicalize_name(name)
    return [
        Declaration(array, raw)
        for array in requirement_arrays(doc)
        for _index, raw, requirement in array.requirements()
        if canonicalize_name(requirement.name) == canonical
    ]


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
        if spec.operator not in LOWER_BOUND_OPERATORS | PIN_OPERATOR:
            continue
        version = safe_version(spec.version)
        if version is not None and (version.is_devrelease or version.is_prerelease):
            return True
    return False


def _consequence(
    kind: SourceKind,
    declarations: list[Declaration],
    ships: bool | None = None,
) -> str:
    """Spell out what a finding costs, given where the package is declared.

    Five distinct outcomes hide behind "unshippable", and telling them apart
    is most of the report's value. What a reader has to do about a release
    that cannot be uploaded, one that uploads and then fails every install,
    and one that installs a different build than was tested are three
    different jobs, and only the first announces itself.

    :param kind: How the package is resolved.
    :param declarations: Output of {func}`declared_requirements`.
    :param ships: Override for the "does it ship" question, used by the
        direct-reference pass, which already knows the array the offending
        string sits in.
    """
    shipped = [
        declaration.requirement
        for declaration in declarations
        if declaration.array.published
    ]
    if ships is None:
        ships = bool(shipped)

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
        return (
            f"A source override never reaches published metadata, so the wheel"
            f" declares `{shipped[0]}` against the default index: installers"
            f" resolve whatever is published under that name, which is not the"
            f" code this release was built and tested against."
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


def _uv_index_entries(uv: Mapping[str, Any]) -> list[dict]:
    """The `[[tool.uv.index]]` array, defensively typed."""
    indexes = uv.get("index") or []
    if not isinstance(indexes, list):
        return []
    return [entry for entry in indexes if isinstance(entry, dict)]


def _finding(
    package: str,
    kind: SourceKind,
    location: str,
    detail: str,
    allow: Mapping[str, str],
    declarations: list[Declaration] | None = None,
    floor: str | None = None,
    ships: bool | None = None,
) -> DepFinding:
    """Assemble a blocking finding, resolving its allowlist reason and prose."""
    return DepFinding(
        package=package,
        kind=kind,
        location=location,
        detail=detail,
        consequence=_consequence(kind, declarations or [], ships),
        remedy=_remedy(kind, package, floor),
        allowed=allow.get(package, ""),
        # The one class of finding another job already watches: a git track
        # inside the managed idiom clears when upstream ships the release the
        # floor names, and `sync-dep-sources` opens the swap PR on sight of it.
        # The condition mirrors `_remedy`, which routes on the same pair.
        clears=BYPASS_NEEDS_RELEASE if kind is SourceKind.GIT and floor else "",
    )


def scan_pyproject(
    doc: Mapping[str, Any], allow: Mapping[str, str] | None = None
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

    :param doc: Parsed `pyproject.toml`.
    :param allow: Package name to the reason it may ship from a non-index
        source, from `[tool.repomatic] lint-deps.allow`.
    :return: Findings, unsorted; {func}`scan_project` orders them.
    """
    allow = allow or {}
    uv = uv_table(doc)
    index_urls = {
        str(entry["name"]): str(entry.get("url", ""))
        for entry in _uv_index_entries(uv)
        if entry.get("name")
    }
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
            kind = SourceKind.classify(entry)
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
                    floor=dev_floor(doc, name),
                )
            )

    for array in requirement_arrays(doc):
        for _index, raw, requirement in array.requirements():
            if not requirement.url:
                continue
            findings.append(
                _finding(
                    canonicalize_name(requirement.name),
                    SourceKind.DIRECT_REFERENCE,
                    array.location,
                    f"`{raw}`",
                    allow,
                    ships=array.published,
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
            override = parse_requirement(item)
            if override is None:
                continue
            name = canonicalize_name(override.name)
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
    lock: LockFile,
    allow: Mapping[str, str] | None = None,
    doc: Mapping[str, Any] | None = None,
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

    :param lock: The parsed `uv.lock`.
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
    for package in lock.data.get("package", []):
        source = package.get("source", {})
        name = canonicalize_name(str(package.get("name", "")))
        if not name:
            continue
        if source.get("editable") == "." or source.get("virtual") == ".":
            continue
        kind = SourceKind.classify(source)
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


@dataclass(frozen=True)
class StaleFloor:
    """A dependency floor no cooldown-gated resolution can satisfy.

    What {func}`floors_inside_cooldown` reports: the offending bound, and the
    day the clock alone lifts it.
    """

    floor: str
    """The offending lower bound, as the requirement writes it."""

    clears: str
    """Date the locked release ages past the cooldown, with a countdown
    (`2026-09-10 (in 3 days)`), or empty when the window is an absolute cutoff
    nothing ages past on its own."""


def floors_inside_cooldown(
    doc: Mapping[str, Any],
    lock: LockFile,
    window: str,
    now: datetime | None = None,
) -> dict[str, StaleFloor]:
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
    {func}`repomatic.release.self_pin.local_cli_invocation`), which resolves
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

    :param doc: Parsed `pyproject.toml`.
    :param lock: The parsed `uv.lock`.
    :param window: Cooldown window, in any form `[tool.uv] exclude-newer`
        accepts.
    :param now: The instant the window counts back from. Read from the clock
        when `None`.
    :return: Mapping of canonical package name to the offending floor and the
        day it clears, empty when every floor resolves without an exemption.
    """
    if now is None:
        now = datetime.now(UTC)
    cutoff = resolve_exclude_newer_cutoff(window, now)
    if cutoff is None:
        return {}
    span = resolve_exclude_newer_span(window)

    offenders: dict[str, StaleFloor] = {}
    for array in requirement_arrays(doc):
        for _index, _raw, requirement in array.requirements():
            name = canonicalize_name(requirement.name)
            upload = lock.uploaded_at(name)
            # A locked version older than the cutoff resolves on its own, so
            # no floor pointing at it can need an exemption.
            if upload is None or upload < cutoff:
                continue
            locked = safe_version(lock.version_of(name))
            if locked is None:
                continue
            # The locked release ages out of a rolling window on its own, which
            # is the whole remedy for this finding. An absolute cutoff never
            # moves, so it leaves no date to wait for.
            clears = format_countdown(upload + span, now) if span is not None else ""
            for spec in requirement.specifier:
                if spec.operator not in LOWER_BOUND_OPERATORS | PIN_OPERATOR:
                    continue
                floor = safe_version(spec.version)
                if floor is not None and floor >= locked:
                    offenders[name] = StaleFloor(str(floor), clears)
    return offenders


def scan_project(
    pyproject_path: Path,
    lock_path: Path,
    window: str,
    allow: Mapping[str, str] | None = None,
) -> list[DepFinding]:
    """Every reason this project cannot be released as it stands.

    Folds the three checks into one ordered report: what `pyproject.toml`
    declares ({func}`scan_pyproject`), what `uv.lock` resolved
    ({func}`scan_lock`), and which floors no cooldown-gated resolution can
    satisfy ({func}`floors_inside_cooldown`). Entirely offline, so it costs
    nothing to run on every push and cannot fail on a flaky index. Each file
    is parsed once.

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
    doc = read_project(pyproject_path)
    lock = LockFile.load(lock_path)
    findings = scan_pyproject(doc, allow)
    reported = {finding.package for finding in findings}
    findings += [
        finding
        for finding in scan_lock(lock, allow, doc=doc)
        if finding.package not in reported
    ]

    for name, stale in floors_inside_cooldown(doc, lock, window).items():
        findings.append(
            DepFinding(
                package=name,
                kind=SourceKind.REGISTRY,
                location="project dependency floor",
                detail=f"`{name}>={stale.floor}`",
                consequence=(
                    f"The release satisfying this floor is younger than the"
                    f" {window} cooldown, so anyone installing from an index"
                    f" resolves nothing: they read neither `uv.lock` nor"
                    f" `[tool.uv] exclude-newer-package`, and uv has no"
                    f" environment variable for a per-package exemption."
                ),
                remedy="Wait for that release to age out of the window.",
                allowed=allow.get(name, ""),
                clears=stale.clears,
            )
        )

    return sorted(findings, key=lambda f: (f.package, f.location))


BLOCKER_SECTION_NOTE = (
    "A dependency is shippable when whoever installs the published artifact"
    " gets the code this release was tested against. These do not clear that"
    " bar, so the release lane refuses to build a package while they stand."
    " See [Dependency management § Shippable"
    " sources](https://repomatic.net/dependencies#shippable-sources)."
)
"""Intro paragraph for the `lint-deps` blocker section."""


def format_blocker_section(findings: list[DepFinding]) -> str:
    """Format blocking findings as a markdown section.

    The long form, for the `lint-deps` report: a reader who opened that report
    came for the diagnosis, so it carries the note and the full table. The
    release PR gets {func}`build_release_readiness` instead, which is the same
    findings at banner length.

    :param findings: Findings from {func}`scan_project`.
    :return: A markdown string, or an empty string when nothing blocks.
    """
    blocking = [finding for finding in findings if finding.blocking]
    if not blocking:
        return ""
    return markdown_section(
        "🚧 Unshippable dependencies",
        BLOCKER_SECTION_NOTE,
        ("Package", "Source", "Declared in", "Clears", "Why it cannot ship"),
        [
            (
                f"`{finding.package}`",
                f"`{finding.kind}`",
                f"`{finding.location}`",
                finding.countdown,
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
    stem = pep503_pattern(finding.package)
    # `uv.lock` names a package on the `name` key of its `[[package]]` block;
    # `pyproject.toml` writes it inside a requirement string or as a table key,
    # so match it as a bare token there.
    pattern = re.compile(
        rf'name = "{stem}"' if in_lock else rf"(?<![\w.-]){stem}(?![\w-])",
        re.IGNORECASE,
    )
    try:
        text = path.read_text(encoding="UTF-8")
    except OSError:
        return path.name

    first = 0
    best = 0
    best_depth = -1
    for number, table, line in iter_toml_lines(text):
        # A comment naming the package does not declare it, and a floor
        # comment is required to name the version it demands, so it matches
        # everything the declaration matches. It also sits directly above it,
        # which is enough to take the first match and send the reader a few
        # lines short of the line to edit.
        if line.lstrip().startswith("#"):
            continue
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

The banner is a verdict, not a report: it says what is wrong, names what to
open, and says when the block lifts. Everything else the finding carries (why
the source is unshippable, what to do about it, the general rule) reads as
chatter in a pull request whose body is otherwise a five-step checklist, and it
is one click away in the `lint-deps` report {func}`format_blocker_section`
renders.
"""

BANNER_COLUMNS = ("Package", "Clears")
"""Columns of the release PR's blocker table.

Two, where {func}`format_blocker_section` renders five. The banner answers the
one question a maintainer has while looking at a merge button: is this still
blocked, and until when. Nothing else on the page answers either half; the
diagnosis stays in the `lint-deps` report that section renders.
"""


def build_release_readiness(
    pyproject_path: Path,
    lock_path: Path,
    window: str,
    allow: Mapping[str, str] | None = None,
    source_url: str | None = None,
    repo_url: str | None = None,
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
    already on `main`, and the recovery is to burn the version and release
    the next one. This body is regenerated on every push to `main`, so it
    carries the same answer days earlier, in the one place a maintainer reads
    before deciding to merge.

    ```{note}
    Lives here rather than beside the other `pr-body` template-argument
    builders in {mod}`repomatic.github.pr_body`, which is where it would
    otherwise belong: that module is imported by {mod}`repomatic.deps.report`,
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
    :param repo_url: Repository homepage, without a trailing slash. Each dated
        or awaited countdown links to the pull request queue of the job that
        clears it. Omitted, the countdowns render as plain text.
    :return: {data}`RELEASE_READY_SENTENCE`, or a GitHub-flavored markdown
        `[!CAUTION]` blockquote naming what blocks the release and when each
        blocker lifts.
    """
    blocking = [
        finding
        for finding in scan_project(pyproject_path, lock_path, window, allow=allow)
        if finding.blocking
    ]
    if not blocking:
        return RELEASE_READY_SENTENCE
    # One row per package: a floor and a source override on the same package
    # send the reader to the same file, and a name repeated down a two-column
    # table reads as two separate problems. Findings arrive sorted by package
    # then location, so the first one kept is the one nearest to an edit.
    rows: list[tuple[str, ...]] = []
    seen: set[str] = set()
    for finding in blocking:
        if finding.package in seen:
            continue
        seen.add(finding.package)
        anchor = declaration_anchor(finding, pyproject_path, lock_path)
        name = (
            f"[`{finding.package}`]({source_url}/{anchor})"
            if source_url
            else f"`{finding.package}` ({anchor})"
        )
        countdown = finding.countdown
        if repo_url and finding.clearing_job:
            queue = clearing_queue_url(repo_url, finding.clearing_job)
            countdown = f"[{countdown}]({queue})"
        rows.append((name, countdown))
    table = render_markdown_table(
        BANNER_COLUMNS, rows, align=("left",) * len(BANNER_COLUMNS)
    )
    # Every line quoted, or GitHub renders the table outside the admonition.
    quoted = "\n".join(f"> {line}" for line in table.splitlines())
    return f"> [!CAUTION]\n> **{UNSHIPPABLE_BANNER_LEAD}**\n>\n{quoted}\n\n"
