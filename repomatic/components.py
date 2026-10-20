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
"""Declarative registry of all components managed by the `init` subcommand.

Every resource the `init` subcommand can create, sync, or merge is declared
here as a {class}`Component` subclass instance in the {data}`COMPONENTS` tuple.
Each component carries all its metadata: what kind it is, whether it is
selected by default, which files it manages, and any per-file properties like
repo-scope gating or config keys.

All derived constants (`REUSABLE_WORKFLOWS`, `BARE_INIT_COMPONENTS` and the
others) are computed from this single registry at the bottom of this module.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import Enum, auto

from .config import Config, location_path
from .file_ops import normalize_text
from .github.workflow_files import WORKFLOW_DIR

# 8.0.0 shim: re-export for callers not migrated yet.
from .upstream import (  # noqa: F401
    DEFAULT_REPO,
    INSTALL_GUIDE_PATH,
    UPSTREAM_ASSET_GLOB,
    UPSTREAM_PACKAGE,
    UPSTREAM_REPO_SLUGS,
    UPSTREAM_SOURCE_GLOB,
    UPSTREAM_SOURCE_PREFIX,
    is_awesome_repo,
)

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Sequence


class InitDefault(Enum):
    """How `init` treats the component when no explicit CLI args are given."""

    INCLUDE = auto()
    """Included by default (like changelog or workflows)."""

    EXCLUDE = auto()
    """In the default set, but excluded unless `[tool.repomatic] include`
    names it (like labels or skills)."""

    EXPLICIT = auto()
    """Only included when explicitly requested (like most tool configs)."""

    @property
    def in_bare_init(self) -> bool:
        """Whether a bare `repomatic init` considers the component at all.

        `EXCLUDE` components are considered, then excluded unless the user
        includes them. `EXPLICIT` components are never considered.
        """
        return self is not InitDefault.EXPLICIT


class SyncMode(Enum):
    """How a `ToolConfigComponent` behaves when the section already exists."""

    BOOTSTRAP = auto()
    """Insert once, skip if section already exists (like ruff or pytest)."""

    ONGOING = auto()
    """Replace template content on every sync, preserving local additions
    (like bumpversion)."""

    def components(self) -> tuple[ToolConfigComponent, ...]:
        """Every registered tool config this mode governs, in registry order."""
        return tuple(
            comp
            for comp in COMPONENTS
            if isinstance(comp, ToolConfigComponent) and comp.sync_mode is self
        )


class RepoScope(Enum):
    """Which repository types a component or file entry applies to.

    The classification has three axes: whether the repo is an `awesome-*` list,
    whether it carries a PEP 621 `pyproject.toml`, and whether that project is
    a distributable package. The first is mutually exclusive with the other two
    (awesome repos are content lists, not Python projects), so a single scope
    value suffices.

    The Python axis is deliberately split in two. `PYTHON_ONLY` covers anything
    that needs Python code to be useful; `PACKAGE_ONLY` covers only what needs
    something to publish. A uv virtual project (`[tool.uv] package = false`)
    sits between the two: it locks dependencies and runs tests, but never
    ships a release. Collapsing the pair would hand every blog and docs site a
    PyPI publish action and a release workflow it can never run.

    The workflow layer mirrors the same split.
    {func}`repomatic.pyproject.is_python_project` is the default gate, since
    `sync-uv-lock` and `sync-dep-sources` apply to a virtual project too.
    {func}`repomatic.pyproject.is_python_package` narrows a job to what has
    something to publish or version: `sync-bumpversion` is the one that needs
    it.

    Scope restrictions are defaults: they apply during bare `repomatic init`
    but are bypassed when components are explicitly named on the CLI or
    covered by `[tool.repomatic] include`.
    """

    ALL = auto()
    """Included in every repository type."""

    AWESOME_ONLY = auto()
    """Only for `awesome-*` repositories."""

    PYTHON_ONLY = auto()
    """Only for Python projects (PEP 621 `[project].name` present).

    Use for anything a uv virtual project still wants: dependency locking,
    coverage config, test tooling.
    """

    PACKAGE_ONLY = auto()
    """Only for Python projects that build a distributable package.

    Strictly narrower than {attr}`PYTHON_ONLY`, excluding uv virtual projects.
    Use for the release lane: publishing, tagging, changelog upkeep.
    """

    def matches(self, traits: RepoTraits) -> bool:
        """Whether this scope applies to a repository with *traits*.

        :param traits: What `init` read off the target repository.
        """
        if self is RepoScope.ALL:
            return True
        if self is RepoScope.AWESOME_ONLY:
            return traits.is_awesome
        if self is RepoScope.PACKAGE_ONLY:
            return traits.is_package
        return traits.is_python


@dataclass(frozen=True)
class RepoTraits:
    """The traits of a repository that decide which {class}`RepoScope` applies.

    `repomatic init` reads them off the target tree once per run, and every
    scope check of that run asks the same instance.
    """

    is_awesome: bool = False
    """Whether the repository is an `awesome-*` curated list."""

    is_python: bool = False
    """Whether `pyproject.toml` declares a PEP 621 `[project]` table, per
    {func}`repomatic.pyproject.is_python_project`."""

    is_package: bool = False
    """Whether that project also builds a distributable package, per
    {func}`repomatic.pyproject.is_python_package`. Always implies
    {attr}`is_python`."""

    is_source: bool = False
    """Whether the repository is the upstream source checkout, per
    {func}`repomatic.upstream.is_source_repo`. No scope reads it: `init` reads
    it to never flag the bundled source files as stale."""


@dataclass(frozen=True)
class FileEntry:
    """A single file managed within a component."""

    source: str
    """Filename in `repomatic/data/`, or a directory when {attr}`tree` is set."""

    target: str = ""
    """Relative output path in the target repository.
    Defaults to `source` (root-level file)."""

    file_id: str = ""
    """Identifier for file-level `--include`/`--exclude`.
    Defaults to the filename portion of `target`."""

    scope: RepoScope = RepoScope.ALL
    """Which repository types get this file."""

    config_key: str = ""
    """`[tool.repomatic]` key that gates this entry.

    Resolved by {meth}`~repomatic.config.Config.is_enabled`, so it names a key
    the user writes, like `debug.sync`. A key that names no field raises.
    """

    reusable: bool = True
    """Workflow-specific: supports `workflow_call` trigger."""

    phase: str = ""
    """Skill-specific: lifecycle phase for `list-skills` display."""

    tree: bool = False
    r"""Whether {attr}`source` and {attr}`target` name directories, not files.

    A tree entry is copied wholesale, so a skill can ship `scripts/`,
    `references/` and `assets/` alongside its `SKILL.md` exactly as the [Agent
    Skills spec](https://agentskills.io/specification) describes, with no
    per-file registration.

    ```{caution}
    Under `repomatic/data/` a tree's directories must be **real** and only its
    leaves may be symlinks back into the authoritative tree. `uv_build` refuses
    a symlinked directory in package data (`Is a directory (os error 21)`) and
    fails the whole wheel, while symlinked files are dereferenced into it
    normally.
    ```
    """

    def is_enabled(self, config: Config) -> bool:
        """Whether *config* enables this entry.

        An entry with no {attr}`config_key` is always enabled.

        :param config: The resolved `[tool.repomatic]` configuration.
        """
        return not self.config_key or config.is_enabled(self.config_key)

    def __post_init__(self) -> None:
        """Derive `target` and `file_id` from `source` when omitted."""
        if not self.target:
            object.__setattr__(self, "target", self.source)
        if not self.file_id:
            object.__setattr__(self, "file_id", self.target.rsplit("/", 1)[-1])


# ---------------------------------------------------------------------------
# Component hierarchy.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Component:
    """Base class for all init components."""

    name: str
    """Component name used on the CLI (like `"skills"`)."""

    description: str
    """Human-readable description for help text."""

    init_default: InitDefault = InitDefault.INCLUDE
    """How `init` treats this component when no explicit CLI selection
    is made."""

    scope: RepoScope = RepoScope.ALL
    """Which repository types get this component.  Checked at the component
    level during auto-exclusion, complementing the file-level
    {attr}`FileEntry.scope`."""

    files: tuple[FileEntry, ...] = ()
    """File entries this component manages."""

    config_key: str = ""
    """`[tool.repomatic]` key that gates this component.

    Resolved like {attr}`FileEntry.config_key`.
    """

    ephemeral: bool = False
    """Whether this component's files are inputs regenerated on demand rather
    than repository content.

    Every consumer of an ephemeral component dumps it right before reading it,
    so a copy in the working tree is never the one that gets used. Bare
    `repomatic init` therefore skips these components, and `[tool.repomatic]
    include` cannot opt into materializing them: only naming the component
    explicitly on the CLI (`repomatic init labels`) writes its files out, which
    is how `sync-labels` stages `labels.toml` into a temporary directory to
    hand to `labelmaker`, leaving the working tree untouched.
    """

    location_field: str = ""
    """{class}`~repomatic.config.Config` field holding this component's
    destination, when the user can move it.

    Set for every component whose destination is configurable: the directories
    `subagents` and `skills` write into, and the single file `plugin`
    merges into. Declared targets are built against the *default*
    location, so a repo that overrode it needs each target rebased onto the
    configured one. {meth}`resolve_target` performs that rebase, and leaving
    this empty means the targets are fixed (`.github/workflows/` is GitHub's,
    not ours to move).
    """

    @property
    def file_ids(self) -> frozenset[str]:
        """The identifiers a `component/file` selector may name.

        Empty for a component without file-level selection, like a tool config.
        """
        return frozenset(entry.file_id for entry in self.files)

    def entry(self, file_id: str) -> FileEntry | None:
        """The file entry *file_id* names, or `None` when none does."""
        return next((entry for entry in self.files if entry.file_id == file_id), None)

    def target_of(self, file_id: str) -> str | None:
        """The declared output path of *file_id*, or `None` when none matches."""
        entry = self.entry(file_id)
        return entry.target if entry else None

    def is_enabled(self, config: Config) -> bool:
        """Whether *config* enables this component.

        A component with no {attr}`config_key` is always enabled.

        :param config: The resolved `[tool.repomatic]` configuration.
        """
        return not self.config_key or config.is_enabled(self.config_key)

    def resolve_target(self, target: str, config: Config | None) -> str:
        """Rebase a declared target path onto this component's configured location.

        A no-op unless {attr}`location_field` is set and the resolved config
        actually moves the destination, so every caller can route every target
        through this method instead of testing the component name first.

        Handles both shapes a location may take. A directory location rebases
        the path under it; a file location (`plugin`) *is* the path, so
        it is replaced outright. Matching only the directory shape would leave a
        moved file reported at its default path, and stale-file detection would
        then hunt for an orphan the repository never wrote there.

        :param target: A path as declared on a {class}`FileEntry` (or a
            {class}`RemovedAsset` tombstone), relative to the repository root and
            expressed against the default location.
        :param config: The resolved configuration. `None` keeps every target
            at its default location.
        :return: The target rebased onto the configured location, or *target*
            unchanged.
        """
        if not self.location_field or config is None:
            return target
        # The Config default carries a "./" prefix the registry targets omit.
        # Annotated because `getattr` on a computed name answers `Any`, which
        # mypy then carries all the way out of this function's `str` return.
        default: str = location_path(getattr(Config, self.location_field))
        custom: str = location_path(getattr(config, self.location_field))
        if custom == default:
            return target
        if target == default:
            return custom
        if not target.startswith(f"{default}/"):
            return target
        return f"{custom}/{target[len(default) + 1 :]}"


@dataclass(frozen=True)
class BundledComponent(Component):
    """Files copied from `repomatic/data/` to a target path."""


@dataclass(frozen=True)
class WorkflowComponent(Component):
    """Thin-caller generation and header sync."""


@dataclass(frozen=True)
class ToolConfigComponent(Component):
    """Merged into `pyproject.toml`.

    ```{note}
    Nothing here declares where the section lands. `init` appends it to the
    `[tool]` table, then `format-pyproject` moves it: `pyproject-fmt` sorts
    `[tool.*]` by its own known-tool order, which no per-component hint can
    override.
    ```
    """

    source_file: str = ""
    """Filename in `repomatic/data/`."""

    tool_section: str = ""
    """The `[tool.X]` section name to check for existence."""

    sync_mode: SyncMode = SyncMode.BOOTSTRAP
    """How this config behaves when the section already exists.

    `BOOTSTRAP`: insert once, skip if the section is present.
    `ONGOING`: re-derive the section from the template on every sync while
    preserving local additions: keys the template omits, extra items in
    shared arrays, and extra keys in shared nested tables. The template wins
    on shared scalars; `preserved_keys` flips that for named top-level keys.
    """

    customizable_entries: tuple[tuple[str, str | None], ...] = ()
    """Template entries every repository is expected to replace with its own.

    Only meaningful when `sync_mode` is `BOOTSTRAP`, where
    {func}`~repomatic.lint_repo.check_bootstrap_config_drift` reports what a
    seeded section never received. A placeholder the template tells the reader
    to change is absent from every *correctly* customized section, so
    reporting it would put a permanent warning on the one knob the template
    asked the repository to own.

    Each entry is `(dotted_path, None)` for a whole key, or
    `(dotted_path, member)` for one item of a list.
    """

    legacy_table: str = ""
    """Sub-table the tool reads in place of the section's own keys.

    Only meaningful when `sync_mode` is `BOOTSTRAP`. pytest reads
    `[tool.pytest.ini_options]` as well as the native `[tool.pytest]` keys the
    template carries, which need pytest 9.0, and refuses a file holding both.
    {func}`~repomatic.lint_repo.check_bootstrap_config_drift` measures a section
    that keeps its keys in this sub-table against the sub-table, so it never
    reports them missing or asks for a native twin the tool would reject.
    """

    preserved_keys: tuple[str, ...] = ()
    """Top-level keys whose existing values survive an ongoing sync.

    Only meaningful when `sync_mode` is `ONGOING`. During replacement,
    these keys keep their value from the existing config rather than being
    overwritten by the template placeholder.
    """

    graft_identity_keys: tuple[str, ...] = ()
    """Keys that identify the "slot" of an array-of-tables entry during a graft.

    Only meaningful when `sync_mode` is `ONGOING`. When set, a local
    array-of-tables entry that shares its identity tuple (the values of these
    keys) with a template entry is treated as a stale copy of that canonical
    entry: the template wins and the local entry is dropped rather than
    appended as a duplicate. Local entries whose identity matches no template
    entry are genuinely local and survive. Leave empty to fall back to a plain
    union-by-value, which cannot tell an evolved canonical entry apart from a
    new local one.

    For `bumpversion`, the slot is `(filename | glob | key_path, replace)`:
    `filename`/`glob`/`key_path` name the target file and `replace` names what
    the entry writes there, so a stale entry whose `search` pattern evolved
    (like one gaining a regex anchor) still maps to the same slot.
    """

    overlay: bool = False
    """Treat the template as a *partial* section owning only its own keys.

    Only meaningful when `sync_mode` is `ONGOING`. The default rebuild-and-graft
    sync rebuilds the whole section from the template and grafts local additions
    *after* it, so template keys always land first. That is wrong for a section
    the project mostly owns and a formatter reorders: `[tool.uv]`, whose keys
    `pyproject-fmt` sorts into a fixed schema order. Emitting the owned keys as a
    leading block would lose to `pyproject-fmt` on the next format pass and churn
    an endless sync PR.

    With `overlay` set, an ongoing sync instead updates only the template's
    top-level keys *in place* within the existing section (the template value
    wins), preserving the existing key order and leaving every other key
    untouched. The merged section is therefore already a `pyproject-fmt`
    fixpoint. A repo missing an owned key has it appended; `pyproject-fmt`
    canonicalizes that one position once, after which steady-state syncs are
    no-ops.
    """

    @property
    def tool_name(self) -> str:
        """The bare tool name, without the `tool.` table prefix.

        The key this component's section sits under inside a parsed
        `[tool]` table, derived once here rather than re-spelled by every
        consumer of `tool_section`.
        """
        return self.tool_section.removeprefix("tool.")

    def __post_init__(self) -> None:
        """Validate required fields."""
        if not self.source_file:
            msg = f"ToolConfigComponent {self.name!r} requires source_file"
            raise ValueError(msg)
        if not self.tool_section:
            msg = f"ToolConfigComponent {self.name!r} requires tool_section"
            raise ValueError(msg)
        if self.files:
            msg = (
                f"ToolConfigComponent {self.name!r} must not have files"
                " (tool configs are merged into pyproject.toml)"
            )
            raise ValueError(msg)


@dataclass(frozen=True)
class TemplateComponent(Component):
    """Directory tree (awesome-template)."""


@dataclass(frozen=True)
class GeneratedComponent(Component):
    """Produced from code (changelog).

    Unlike bundled components, generated components have no `files` tuple.
    The `target` field records the output path so the auto-exclusion logic
    can detect stale copies on disk.
    """

    target: str = ""
    """Relative output path in the target repository."""


@dataclass(frozen=True)
class RemovedAsset:
    """An asset repomatic once shipped and has since dropped.

    ```{note}
    Stale-file detection in `init` only inspects files still listed in
    {data}`COMPONENTS`. An asset removed from the registry (a renamed or
    consolidated skill, a retired workflow) becomes invisible to it, so
    downstream repos accumulate one orphan per upstream removal. Each
    `RemovedAsset` is a tombstone that lets `init` find and prune those
    orphans.
    ```

    `init` finds an on-disk orphan and decides whether to prune it with one of
    two gates, depending on the component:

    - Content-gated (skills, agents, config files): the file is deleted only
      when its normalized content matches one of {attr}`hashes` (a version
      repomatic shipped), proving it is an untouched copy.
    - Fingerprint-gated (workflows): thin-callers are parameterized per repo
      (version pin, `paths:` filters), so they carry no fixed content. The file
      is deleted only when it is a repomatic-lineage thin-caller for this
      workflow (its `uses:` line references an upstream slug, see
      {data}`UPSTREAM_REPO_SLUGS`) with no extra downstream jobs.

    Either way, a locally modified orphan is reported for manual review, never
    deleted. When {attr}`target` is already gone but the asset shipped as a
    folder, an empty {attr}`owned_dir` left behind is pruned on its own: it
    carries nothing anyone could lose.
    """

    component: str
    """Component the asset belonged to (like `"skills"` or `"workflows"`)."""

    target: str
    """Relative output path the asset occupied, in default-location form
    (like `.claude/skills/papaya-report/SKILL.md` or
    `.github/workflows/papaya.yaml`).

    Build skill and subagent targets with `_skill_target` / `_subagent_target`
    so they match the live registry: the `skills.location` and
    `subagents.location` overrides are re-applied at detection time. Workflow
    targets are literal (`.github/workflows/` is fixed by GitHub)."""

    removed_in: str
    """Bare package version that first stopped shipping the asset
    (like `7.11.0`). Surfaced in the prune report."""

    hashes: tuple[str, ...] = ()
    """Content gate for skills, agents and config files: the {meth}`digest` of
    every distinct content repomatic shipped for this asset. An on-disk file
    whose content has any of these digests is an untouched copy of some
    released version and is safe to delete. Listing one digest per distinct
    released revision (not just the last) means a downstream repo that synced
    an older version is still recognized and pruned rather than flagged for
    review.

    Empty for workflows, which are fingerprint-gated by their `uses:` line
    instead (see the class docstring)."""

    owned_dir: str = ""
    """Directory the asset had to itself, in default-location form (like
    `.claude/skills/papaya-report`), for an asset shipped as a folder.

    A skill is a folder, so deleting its `SKILL.md` by any route other than
    `init` (a hand `rm`, a repomatic old enough to unlink the file alone) leaves
    the folder behind, empty. {attr}`target` no longer exists, so the tombstone
    never fires again and the fossil outlives every later `init`. Declaring the
    folder gives detection a second thing to look for. Empty for an asset that
    shipped as a lone file in a shared directory (a subagent, a workflow), whose
    parent must never be swept."""

    successor: str = ""
    """Optional human note describing what replaced the asset, shown in the
    report (like `replaced by repomatic-ship`)."""

    @property
    def content_gated(self) -> bool:
        """Whether an orphan is judged by its content rather than its `uses:` line.

        Every asset is content-gated except a workflow, whose thin caller
        carries no fixed content to compare.
        """
        return self.component != "workflows"

    @staticmethod
    def digest(text: str) -> str:
        """The hex SHA-256 of *text* in the form `init` writes it to disk.

        The text is normalized first, through
        {func}`~repomatic.file_ops.normalize_text`, so trailing whitespace a
        repository added after the write never reads as a local change.

        :param text: The content of a file, as shipped or as found on disk.
        """
        return hashlib.sha256(normalize_text(text).encode("UTF-8")).hexdigest()

    def is_untouched(self, text: str) -> bool:
        """Whether *text* is the content of a released revision of this asset.

        :param text: The content of the orphan found on disk.
        """
        return self.digest(text) in self.hashes

    def locate(self, path: str, config: Config | None) -> str:
        """Rebase *path*, in default-location form, onto the configured location.

        Applies the same `*.location` override as the live targets of
        {attr}`component`, through {meth}`Component.resolve_target`. A dropped
        component the registry no longer lists leaves *path* unchanged.

        :param path: {attr}`target` or {attr}`owned_dir`.
        :param config: The resolved configuration.
        """
        component = COMPONENTS_BY_NAME.get(self.component)
        return component.resolve_target(path, config) if component else path


# ---------------------------------------------------------------------------
# Helpers for computed targets.
# ---------------------------------------------------------------------------


def _subagent_target(agent_id: str) -> str:
    """Build the default target path for a subagent file from the Config default."""
    return f"{location_path(Config.subagents_location)}/{agent_id}.md"


def _subagent_entry(agent_id: str) -> FileEntry:
    """Declare the {class}`FileEntry` of one bundled subagent definition.

    Every subagent follows the same shape (`agent-{id}.md` in the bundle, one
    Markdown file under the subagents directory, `{id}` as its selector), so the
    registry names the id once and derives the rest.
    """
    return FileEntry(f"agent-{agent_id}.md", _subagent_target(agent_id), agent_id)


SKILL_FILENAME = "SKILL.md"
"""Name the Agent Skills spec reserves for a skill's entry point."""

SKILL_SOURCE_ROOT = "skills"
"""Directory under `repomatic/data/` holding one folder per bundled skill."""


def _skill_dir(skill_id: str) -> str:
    """Build the default target directory for a skill from the Config default."""
    return f"{location_path(Config.skills_location)}/{skill_id}"


def _skill_target(skill_id: str) -> str:
    """Build the default target path of a skill's `SKILL.md`.

    Kept alongside {func}`_skill_dir` because a {class}`RemovedAsset` tombstone
    is gated on an individual file on a downstream repo's disk. It names the
    folder too, through {attr}`RemovedAsset.owned_dir`, but only to catch what
    an already-deleted file leaves behind.
    """
    return f"{_skill_dir(skill_id)}/{SKILL_FILENAME}"


def _skill_source(skill_id: str) -> str:
    """Build the bundled source directory for a skill."""
    return f"{SKILL_SOURCE_ROOT}/{skill_id}"


def _skill_entry(
    skill_id: str,
    phase: str,
    scope: RepoScope = RepoScope.ALL,
) -> FileEntry:
    """Declare the {class}`FileEntry` of one bundled skill.

    Every skill is a whole folder copied verbatim (`tree=True`) from
    `data/skills/{id}/` to `{skills_location}/{id}/`, with `{id}` as its
    selector. Only the lifecycle phase and the repo scope vary, so those are the
    only arguments.
    """
    return FileEntry(
        _skill_source(skill_id),
        _skill_dir(skill_id),
        skill_id,
        scope=scope,
        phase=phase,
        tree=True,
    )


def _workflow_entry(
    source: str,
    *,
    target: str = "",
    scope: RepoScope = RepoScope.ALL,
    reusable: bool = True,
    config_key: str = "",
) -> FileEntry:
    """Declare the {class}`FileEntry` of one workflow.

    A workflow's downstream path is `.github/workflows/` plus its own filename,
    so naming the bundled source is enough. *target* overrides the filename for
    the one workflow whose deployed name differs from its source (the release
    entry, generated from `_release-engine.yaml`).
    """
    filename = target or source
    return FileEntry(
        source,
        (WORKFLOW_DIR / filename).as_posix(),
        scope=scope,
        reusable=reusable,
        config_key=config_key,
    )


# ---------------------------------------------------------------------------
# The registry.
# ---------------------------------------------------------------------------

COMPONENTS: tuple[Component, ...] = (
    # --- Bundled file components ---
    BundledComponent(
        name="labels",
        description="Label definitions for labelmaker (labels.toml)",
        init_default=InitDefault.EXCLUDE,
        ephemeral=True,
        # Only the labelmaker definitions remain: the labelling rules live in
        # `repomatic.github.labels.DEFAULT_*_RULES` and `[tool.repomatic.labels]`,
        # with no file staged anywhere since `apply-labels` matches in-process.
        files=(FileEntry("labels.toml"),),
    ),
    BundledComponent(
        name="publish-pypi-action",
        description=(
            "Composite action that publishes to PyPI via Trusted Publishing"
            " (.github/actions/publish-pypi/)"
        ),
        scope=RepoScope.PACKAGE_ONLY,
        files=(
            FileEntry(
                "action-publish-pypi.yaml",
                ".github/actions/publish-pypi/action.yaml",
            ),
        ),
    ),
    BundledComponent(
        name="subagents",
        description="Agent subagent definitions (.claude/agents/)",
        init_default=InitDefault.EXCLUDE,
        location_field="subagents_location",
        files=(
            _subagent_entry("grunt-qa"),
            _subagent_entry("qa-engineer"),
            _subagent_entry("sphinx-docs"),
        ),
    ),
    BundledComponent(
        name="skills",
        description="Claude Code skill definitions (.claude/skills/)",
        init_default=InitDefault.EXCLUDE,
        location_field="skills_location",
        files=(
            _skill_entry("av-false-positive", "Release"),
            _skill_entry("awesome-triage", "Maintenance", RepoScope.AWESOME_ONLY),
            _skill_entry("babysit-ci", "Quality"),
            _skill_entry("benchmark-update", "Development"),
            _skill_entry("brand-assets", "Development"),
            _skill_entry("file-bug-report", "Maintenance"),
            _skill_entry("github-housekeeping", "Maintenance"),
            _skill_entry("probe-workflow", "Quality"),
            _skill_entry("repomatic-audit", "Maintenance"),
            _skill_entry("repomatic-changelog", "Release"),
            _skill_entry("repomatic-deps", "Development"),
            _skill_entry("repomatic-init", "Setup"),
            _skill_entry("repomatic-ship", "Release"),
            _skill_entry("repomatic-test-matrix", "Quality"),
            _skill_entry("repomatic-topics", "Development"),
            _skill_entry("repomatic-upgrade", "Maintenance"),
            _skill_entry("sphinx-docs-sync", "Maintenance"),
            _skill_entry("translation-sync", "Maintenance", RepoScope.AWESOME_ONLY),
            _skill_entry("upstream-audit", "Maintenance"),
        ),
    ),
    # --- Workflow component ---
    WorkflowComponent(
        name="workflows",
        description="Thin-caller workflow files",
        files=(
            _workflow_entry("autofix.yaml"),
            _workflow_entry("autolock.yaml"),
            _workflow_entry("cancel-runs.yaml"),
            _workflow_entry("changelog.yaml", scope=RepoScope.PACKAGE_ONLY),
            _workflow_entry(
                "debug.yaml",
                scope=RepoScope.PYTHON_ONLY,
                config_key="debug.sync",
            ),
            _workflow_entry("docs.yaml"),
            _workflow_entry("labels.yaml"),
            _workflow_entry("lint.yaml"),
            _workflow_entry("metrics.yaml", config_key="metrics.sync"),
            # Downstream artifact is release.yaml (the entry), generated by
            # workflow_sync._generate_release_caller and pointing its `uses:` at
            # the RELEASE_ENGINE_WORKFLOWS lanes. This source records the
            # representative backing reusable (_release-engine.yaml); see
            # WORKFLOW_SOURCES and RELEASE_ENGINE_WORKFLOWS.
            _workflow_entry(
                "_release-engine.yaml",
                target="release.yaml",
                scope=RepoScope.PACKAGE_ONLY,
            ),
            _workflow_entry("tests.yaml", reusable=False),
            _workflow_entry("unsubscribe.yaml", config_key="notification.unsubscribe"),
        ),
    ),
    # --- Special components ---
    TemplateComponent(
        name="awesome-template",
        description="Boilerplate for awesome-* repositories",
        scope=RepoScope.AWESOME_ONLY,
        config_key="awesome-template.sync",
    ),
    GeneratedComponent(
        name="changelog",
        description="Minimal changelog.md",
        scope=RepoScope.PACKAGE_ONLY,
        location_field="changelog_location",
        target=location_path(Config.changelog_location),
    ),
    GeneratedComponent(
        name="plugin",
        description="Claude Code plugin marketplace wiring (.claude/settings.json)",
        # Opt-in like its `skills` and `subagents` siblings, and for a stronger
        # reason: this one asks every collaborator to install something. A bare
        # `repomatic init` never touches it.
        init_default=InitDefault.EXCLUDE,
        location_field="settings_location",
        target=location_path(Config.settings_location),
    ),
    # --- Tool config components (merged into pyproject.toml) ---
    ToolConfigComponent(
        name="uv",
        description="uv resolver pin and dependency cooldown policy",
        init_default=InitDefault.EXPLICIT,
        source_file="uv.toml",
        tool_section="tool.uv",
        # `[tool.uv]` is mostly project-owned (dependencies, sources,
        # exclude-newer-package, build-backend); repomatic owns only the two
        # policy pins in the template. Overlay updates those in place and
        # leaves everything else, so a re-sync stays a `pyproject-fmt`
        # fixpoint and a no-op when the pins already match.
        sync_mode=SyncMode.ONGOING,
        overlay=True,
    ),
    ToolConfigComponent(
        name="lychee",
        description="Lychee link checker configuration",
        scope=RepoScope.AWESOME_ONLY,
        source_file="lychee.toml",
        tool_section="tool.lychee",
        sync_mode=SyncMode.ONGOING,
    ),
    ToolConfigComponent(
        name="ruff",
        description="Ruff linter/formatter configuration",
        init_default=InitDefault.EXPLICIT,
        source_file="ruff.toml",
        tool_section="tool.ruff",
    ),
    ToolConfigComponent(
        name="pytest",
        description="Pytest test configuration",
        init_default=InitDefault.EXPLICIT,
        source_file="pytest.toml",
        tool_section="tool.pytest",
        # `--cov=.` names the package to measure, and the template says to
        # change it. repomatic's own answer is a bare `--cov`, with the source
        # named once in `[tool.coverage] run.source`.
        customizable_entries=(("addopts", "--cov=."),),
        legacy_table="ini_options",
    ),
    ToolConfigComponent(
        name="coverage",
        description="Coverage.py measurement and reporting configuration",
        init_default=InitDefault.EXPLICIT,
        source_file="coverage.toml",
        tool_section="tool.coverage",
    ),
    ToolConfigComponent(
        name="mypy",
        description="Mypy type checking configuration",
        init_default=InitDefault.EXPLICIT,
        source_file="mypy.toml",
        tool_section="tool.mypy",
    ),
    ToolConfigComponent(
        name="mdformat",
        description="mdformat Markdown formatter configuration",
        init_default=InitDefault.EXPLICIT,
        source_file="mdformat.toml",
        tool_section="tool.mdformat",
    ),
    ToolConfigComponent(
        name="bumpversion",
        description="bump-my-version configuration",
        init_default=InitDefault.EXPLICIT,
        source_file="bumpversion.toml",
        tool_section="tool.bumpversion",
        sync_mode=SyncMode.ONGOING,
        preserved_keys=("current_version",),
        graft_identity_keys=("filename", "glob", "key_path", "replace"),
    ),
    ToolConfigComponent(
        name="typos",
        description="Typos spell checker configuration",
        init_default=InitDefault.EXPLICIT,
        source_file="typos.toml",
        tool_section="tool.typos",
        # Re-merge on every sync so the canonical proper-noun identifiers reach
        # repos that already carry a project-specific `[tool.typos]`. typos has
        # `reads_pyproject=True`, so the bundled defaults only take effect once
        # they physically live in `[tool.typos]`; a BOOTSTRAP insert would skip
        # the existing section and leave them inactive. Local additions
        # (extra excludes, identifiers, words) survive via the merge.
        sync_mode=SyncMode.ONGOING,
    ),
)
"""The component registry.

Single source of truth for all resources managed by the `init` subcommand.
Every component declares its kind, selection default, file entries, and
behavioral flags. All derived constants are computed from this tuple.
"""

COMPONENTS_BY_NAME: dict[str, Component] = {c.name: c for c in COMPONENTS}
"""Index for O(1) component lookup by name."""


# ---------------------------------------------------------------------------
# Removed assets (tombstones).
# ---------------------------------------------------------------------------


def _removed_workflow(
    filename: str, removed_in: str, *, successor: str = ""
) -> RemovedAsset:
    """Declare a tombstone for a workflow repomatic no longer ships.

    Workflows are fingerprint-gated by their `uses:` line rather than hashed, so
    they carry no *hashes* (see the {class}`RemovedAsset` docstring).
    """
    return RemovedAsset(
        "workflows",
        (WORKFLOW_DIR / filename).as_posix(),
        removed_in,
        successor=successor,
    )


REMOVED_ASSETS: tuple[RemovedAsset, ...] = (
    RemovedAsset(
        "codecov",
        ".github/codecov.yaml",
        "7.8.0",
        ("e8e96bfead62334599f4ec4c0448f2376352629789a70a76ae6fc3746ff7057b",),
        successor="coverage is now gated by pytest --cov-fail-under",
    ),
    RemovedAsset(
        "labels",
        ".github/labeller-content-based.yaml",
        "7.11.0",
        (
            "1f3e670c0b4c6687a8920fb3738a15fb82b8639b7825d81f76c55bc5784cdb08",
            "adf62c78c539229d34d4d2518a9af7f39df44d599c60852784b0faa47a6defa9",
            "5cf481b4aec2bf98a4056757f41ef5fc50f808dbd7c8a43f1dea0b224ecb7f1f",
            "8a047d53d5449ea0b53517e2f63e126360050127342084b7a705f34fb735d818",
        ),
        successor="rules now live in repomatic.github.labels.DEFAULT_CONTENT_RULES",
    ),
    RemovedAsset(
        "labels",
        ".github/labeller-file-based.yaml",
        "7.11.0",
        (
            "9dc0948e23a3a83d2cec5f11e400c75992fb1ce326eb6c5811c1fc3bfe258b31",
            "b216d370e4d2c6118f46d9bb2eacaf91392e6a6267a4f4857f44a698422cc860",
            "9a4feeb49c37ee7eba1d13957d26aaaa867c791ec12be8cd4197e7526bfbf963",
        ),
        successor="rules now live in repomatic.github.labels.DEFAULT_FILE_RULES",
    ),
    _removed_workflow(
        "renovate.yaml",
        "7.0.0",
        successor="replaced by self-hosted sync-tool-versions, sync-action-pins,"
        " and sync-workflow-pins",
    ),
)
"""Tombstones for assets repomatic has dropped (see {class}`RemovedAsset`).

`init` prunes orphaned copies of these from downstream repos. Ordered by
`(component, target)`. A tombstone stays for the major releases a downstream
repository may still upgrade from: the ones of assets dropped before `7.0.0`
are gone.

When you drop a bundled asset from {data}`COMPONENTS`, add an entry here so
the removal propagates downstream on the next `init` instead of leaving an
orphan. List one digest per distinct content the asset shipped across its
released lifetime, collected from the release tags where its data file
existed:

```python
import subprocess

from repomatic.components import RemovedAsset

src = "repomatic/data/skills/papaya-report/SKILL.md"  # The dropped data file.
tags = subprocess.run(
    ["git", "tag", "--list", "v*"],
    capture_output=True, text=True, encoding="UTF-8", check=True,
).stdout.split()
digests = {}
for tag in tags:
    blob = subprocess.run(
        ["git", "show", f"{tag}:{src}"],
        capture_output=True, text=True, encoding="UTF-8",
    )
    if blob.returncode == 0:
        digests.setdefault(RemovedAsset.digest(blob.stdout), tag)
print(tuple(digests))  # Distinct contents, in first-shipped order.
```

A skill ships as a folder: give its `SKILL.md` as `target` and its folder as
`owned_dir`, both through `_skill_target` and `_skill_dir`. Removed
*workflows* are fingerprint-gated, not hashed: declare them through
`_removed_workflow`, which omits `hashes`.
"""


# ---------------------------------------------------------------------------
# Registry queries.
# ---------------------------------------------------------------------------

BARE_INIT_COMPONENTS: frozenset[str] = frozenset(
    c.name for c in COMPONENTS if c.init_default.in_bare_init
)
"""Components a bare `repomatic init` considers, before any exclusion."""

DEFAULT_EXCLUDED_COMPONENTS: frozenset[str] = frozenset(
    c.name for c in COMPONENTS if c.init_default is InitDefault.EXCLUDE
)
"""Components a bare `repomatic init` excludes unless the user includes them."""

EPHEMERAL_COMPONENTS: frozenset[str] = frozenset(
    c.name for c in COMPONENTS if c.ephemeral
)
"""Components whose files are regenerated on demand (see {attr}`Component.ephemeral`).

A bare `repomatic init` excludes them, and `[tool.repomatic] include` cannot
bring them back: only naming one on the CLI writes its files out.
"""

EPHEMERAL_TARGETS: frozenset[str] = frozenset(
    entry.target
    for component in COMPONENTS
    if component.ephemeral
    for entry in component.files
)
"""Target paths belonging to {attr}`Component.ephemeral` components.

Written only when the component is named explicitly on the CLI, and never worth
committing: whatever reads them regenerates them first. `init` uses this to keep
its closing "commit the generated files" advice off a run that produced nothing
but scratch output.
"""

BUNDLED_VERBATIM_TARGETS: frozenset[str] = frozenset(
    entry.target
    for component in COMPONENTS
    if isinstance(component, BundledComponent)
    for entry in component.files
)
"""Target paths `repomatic init` writes verbatim from a `repomatic/data/` template.

Every {class}`BundledComponent` copies its bundled source byte-for-byte to the
target, so downstream the file's content (including any SHA-pinned `uses:` ref) is
owned by `repomatic init`. `sync-action-pins` and `sync-workflow-pins` skip these
paths for the same reason they skip {data}`~repomatic.upstream.UPSTREAM_REPO_SLUGS`:
a pin the next `sync-repomatic` overwrites turns the two pull requests into a
ping-pong, the bump PR and the init-revert PR chasing each other. The skip lifts
inside the source repo, where each bundled source is a symlink to its in-tree
target and the pin is a normal source-of-truth ref (see
`repomatic.deps.sync._pinnable_files`). Generated workflows
({class}`WorkflowComponent`) are deliberately absent: they carry only
upstream-slug refs (already skipped) and may host downstream-authored extra jobs
whose third-party pins the bumpers should keep current.
"""

REUSABLE_WORKFLOWS: tuple[str, ...] = tuple(
    f.file_id for f in COMPONENTS_BY_NAME["workflows"].files if f.reusable
)
"""Workflow filenames that support `workflow_call` triggers."""

NON_REUSABLE_WORKFLOWS: frozenset[str] = frozenset(
    f.file_id for f in COMPONENTS_BY_NAME["workflows"].files if not f.reusable
)
"""Workflows without `workflow_call` that cannot be used as thin callers."""

ALL_WORKFLOW_FILES: tuple[str, ...] = tuple(
    sorted(COMPONENTS_BY_NAME["workflows"].file_ids)
)
"""All workflow filenames (reusable and non-reusable)."""

WORKFLOW_SOURCES: dict[str, str] = {
    f.file_id: f.source for f in COMPONENTS_BY_NAME["workflows"].files
}
"""Maps each workflow's downstream file_id to its bundled source filename.

For most workflows source == file_id. The release entry is the exception: its
downstream artifact is `release.yaml`, whose backing reusable engine is
`_release-engine.yaml` (the lane the generic "is this a reusable workflow" tests
inspect). The full set of reusable lanes the generated `release.yaml` calls is
{data}`RELEASE_ENGINE_WORKFLOWS`.
"""

RELEASE_ENGINE_WORKFLOWS: tuple[str, ...] = (
    "_release-build.yaml",
    "_release-engine.yaml",
)
"""Reusable workflows the generated `release.yaml` references but that
`repomatic init` never materializes downstream.

The `workflows` component deploys a *generated* `release.yaml` (not a thin
delegation): its `build` job calls `_release-build.yaml` and its `release` job
calls `_release-engine.yaml`, each via `{repo}/.github/workflows/<lane>@<tag>`
resolved from this repo at the release tag rather than copied into the
downstream tree. These lanes live in `.github/workflows/` here (and at every
release tag) but are not `FileEntry` targets and never appear in
{data}`ALL_WORKFLOW_FILES`.

The release entry's `FileEntry` still records `_release-engine.yaml` as its
`source` (see {data}`WORKFLOW_SOURCES`) so the generic backing-reusable tests
and a downstream `repomatic lint` can read it via `get_data_content` to check
the engine lane forwards its secrets; `_release-build.yaml` is not bundled
because nothing reads it at runtime (the build lane declares no secrets). Naming
both lanes here lets stale-file detection and the data-symlink rules treat them
as a group instead of special-casing each by hand.
"""

SELF_MAINTENANCE_WORKFLOWS: frozenset[str] = frozenset(("self-maintenance.yaml",))
"""Workflows that maintain this package's own source and never ship downstream.

Unlike {data}`RELEASE_ENGINE_WORKFLOWS`, which downstream repos still reach
remotely through a `uses:` ref at a release tag, these are invisible outside this
repository: they are not `FileEntry` targets, carry no `repomatic/data/` symlink,
and nothing resolves them at runtime. That is what lets their jobs drop the
`github.repository == 'kdeldycke/repomatic'` guard every in-`autofix.yaml`
upstream-only step needs, and pick a schedule without spending downstream CI.

A workflow belongs here when its write domain is a path that exists only in this
repository (`repomatic/tooling/registry.py` and friends). A workflow that merely
*behaves* differently upstream does not: it still ships, so it still needs the
runtime guard.
"""

FILE_SELECTOR_COMPONENTS: tuple[str, ...] = tuple(c.name for c in COMPONENTS if c.files)
"""Components that support file-level `component/file` selectors."""


def parse_component_entries(
    entries: Sequence[str],
    *,
    context: str = "entry",
) -> tuple[set[str], dict[str, set[str]]]:
    """Parse component entries into full-component and file-level sets.

    Bare names (no `/`) must be component names from
    {data}`COMPONENTS_BY_NAME`. Qualified `component/identifier` entries
    target individual files. Raises `ValueError` on unknown entries.

    Used by both the `exclude` config path and the CLI positional
    selection, with *context* controlling error message wording.

    :param context: Label for error messages (like `"exclude"` or
        `"selection"`).
    :return: `(full_components, file_selections)` where
        `file_selections` maps component names to sets of file
        identifiers.
    """
    full_components: set[str] = set()
    file_selections: dict[str, set[str]] = {}
    names = ", ".join(sorted(COMPONENTS_BY_NAME))

    for entry in entries:
        if "/" in entry:
            component, file_id = entry.split("/", 1)
            comp = COMPONENTS_BY_NAME.get(component)
            if comp is None:
                msg = (
                    f"Unknown component {component!r} in {context}"
                    f" {entry!r}. Valid components: {names}"
                )
                raise ValueError(msg)
            valid = comp.file_ids
            if not valid:
                msg = (
                    f"Component {component!r} does not support"
                    f" file-level selection in {context} {entry!r}."
                    f" Use the bare component name {component!r}"
                    " instead."
                )
                raise ValueError(msg)
            if file_id not in valid:
                msg = (
                    f"Unknown file {file_id!r} in {context}"
                    f" {entry!r}. Valid identifiers for"
                    f" {component!r}:"
                    f" {', '.join(sorted(valid))}"
                )
                raise ValueError(msg)
            file_selections.setdefault(component, set()).add(file_id)
        elif entry in COMPONENTS_BY_NAME:
            full_components.add(entry)
        else:
            msg = (
                f"Unknown {context} {entry!r}. Use a component name"
                f" ({names}) or a"
                " qualified component/file entry"
                " (like 'workflows/autolock.yaml')."
            )
            raise ValueError(msg)

    return full_components, file_selections
