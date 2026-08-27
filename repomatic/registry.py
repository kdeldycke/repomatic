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

All derived constants (`ALL_COMPONENTS`, `REUSABLE_WORKFLOWS`,
`SKILL_PHASES`, etc.) are computed from this single registry at the bottom of
this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto

from .config import Config, location_path
from .frontmatter import split_frontmatter
from .tooling.bundle import get_data_content

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Sequence

GITHUB_YAML_PATTERNS: tuple[str, ...] = (
    ".github/workflows/*.yaml",
    ".github/workflows/*.yml",
    ".github/actions/**/*.yaml",
    ".github/actions/**/*.yml",
)
"""Globs matching every workflow and composite-action file of a repository.

Rooted at the repository root rather than at `.github/`, so the same patterns
work against the current directory and against an arbitrary target tree. Both
`.yml` and `.yaml` are listed because GitHub accepts either, whatever this
project's own [long-extension convention](https://repomatic.net)
prefers: a downstream repository is free to have picked the short one.

Shared by `sync_ops._workflow_and_action_files`, which reads the pins to bump,
and `init_project._highest_upstream_pin`, which reads them to floor a new pin.
The two must agree on which files carry a pin, or `init` would floor against a
file `sync-workflow-pins` never bumps.
"""


def _config_enabled(config: object, config_key: str, config_default: bool) -> bool:
    """Resolve a `[tool.repomatic]` gate against a `Config` object.

    Returns `True` when *config_key* is empty (unconditionally enabled) or when
    the corresponding config field is truthy. Shared by
    {meth}`FileEntry.is_enabled` and {meth}`Component.is_enabled`.

    A `[tool.repomatic]` key reaches its field one of two ways, and both are
    tried because both are in use. `metrics.sync` is a key *on a nested schema*
    ({class}`~repomatic.config.MetricsConfig`), reached by walking the dotted
    path. `notification.unsubscribe` is a scalar whose dotted path is only
    metadata, reached by flattening the whole key to one attribute name.
    Walking first, since a nested schema is the more specific match: a gate
    naming a nested key would otherwise flatten to an attribute nothing
    defines and silently resolve to *config_default*, which reads as a feature
    switched off rather than as a gate wired wrong.
    """
    if not config_key:
        return True
    parts = config_key.replace("-", "_").split(".")
    node: object = config
    for part in parts:
        if not hasattr(node, part):
            break
        node = getattr(node, part)
    else:
        return bool(node)
    return bool(getattr(config, "_".join(parts), config_default))


class InitDefault(Enum):
    """How `init` treats the component when no explicit CLI args are given."""

    INCLUDE = auto()
    """Included by default (like changelog or workflows)."""

    EXCLUDE = auto()
    """In default set but excluded unless explicitly included
    (e.g., labels, skills)."""

    AUTO = auto()
    """Auto-included only for matching repos (e.g., awesome-template)."""

    EXPLICIT = auto()
    """Only included when explicitly requested (e.g., tool configs)."""


class SyncMode(Enum):
    """How a `ToolConfigComponent` behaves when the section already exists."""

    BOOTSTRAP = auto()
    """Insert once, skip if section already exists (e.g., ruff, pytest)."""

    ONGOING = auto()
    """Replace template content on every sync, preserving local additions
    (e.g., bumpversion)."""


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

    def matches(self, is_awesome: bool, is_python: bool, is_package: bool) -> bool:
        """Whether this scope applies to the given repository traits.

        :param is_awesome: `True` for `awesome-*` repositories.
        :param is_python: `True` for repositories whose `pyproject.toml`
            declares a PEP 621 `[project].name`, per
            {func}`repomatic.pyproject.is_python_project`.
        :param is_package: `True` when that project is also distributable, per
            {func}`repomatic.pyproject.is_python_package`. Always implies
            *is_python*.
        """
        if self is RepoScope.ALL:
            return True
        if self is RepoScope.AWESOME_ONLY:
            return is_awesome
        if self is RepoScope.PACKAGE_ONLY:
            return is_package
        return is_python


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
    """`[tool.repomatic]` key that gates this entry."""

    config_default: bool = False
    """Value assumed when `config_key` is absent from config. `False`
    means opt-in (excluded unless enabled), `True` means opt-out
    (included unless disabled)."""

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

    def is_enabled(self, config: object) -> bool:
        """Whether this entry is enabled by the given `Config` object.

        See {func}`_config_enabled` for the resolution rule.

        :param config: A {class}`~repomatic.config.Config` instance.
        """
        return _config_enabled(config, self.config_key, self.config_default)

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
    """Component name used on the CLI (e.g., `"skills"`)."""

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
    """`[tool.repomatic]` key that gates this component."""

    config_default: bool = True
    """Value assumed when `config_key` is absent from config. `True`
    means opt-out (included unless disabled)."""

    keep_unmodified: bool = False
    """Preserve files on disk even when identical to the bundled default.
    When `False`, unmodified copies are flagged for cleanup by
    `--delete-unmodified`."""

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

    def is_enabled(self, config: object) -> bool:
        """Whether this component is enabled by the given `Config` object.

        See {func}`_config_enabled` for the resolution rule.

        :param config: A {class}`~repomatic.config.Config` instance.
        """
        return _config_enabled(config, self.config_key, self.config_default)

    def resolve_target(self, target: str, config: object) -> str:
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
        :param config: A {class}`~repomatic.config.Config` instance, or `None`.
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
    override. This class used to carry `insert_after` / `insert_before` tuples
    for the purpose; they were read by no code and are gone.
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
    (e.g. gaining a regex anchor) still maps to the same slot.
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
    (like `.claude/skills/repomatic-release/SKILL.md` or
    `.github/workflows/label-sponsors.yaml`).

    Build skill and subagent targets with `_skill_target` / `_subagent_target`
    so they match the live registry: the `skills.location` and
    `subagents.location` overrides are re-applied at detection time. Workflow
    targets are literal (`.github/workflows/` is fixed by GitHub)."""

    removed_in: str
    """Bare package version that first stopped shipping the asset
    (like `6.21.0`). Surfaced in the prune report."""

    hashes: tuple[str, ...] = ()
    r"""Content gate for skills and agents: the hex SHA-256 of every distinct
    normalized content repomatic shipped for this asset (`content.rstrip() +
    "\n"`, exactly as `init` writes it to disk). An on-disk file whose content
    hashes to any of these is an untouched copy of some released version and is
    safe to delete. Listing one hash per distinct released revision (not just
    the last) means a downstream repo that synced an older version is still
    recognized and pruned rather than flagged for review.

    Empty for workflows, which are fingerprint-gated by their `uses:` line
    instead (see the class docstring)."""

    owned_dir: str = ""
    """Directory the asset had to itself, in default-location form (like
    `.claude/skills/repomatic-release`), for an asset shipped as a folder.

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


# ---------------------------------------------------------------------------
# Helpers for computed targets.
# ---------------------------------------------------------------------------


WORKFLOW_TARGET_ROOT = ".github/workflows"
"""Directory GitHub reads workflow files from. Not configurable."""

INSTALL_GUIDE_PATH = "docs/install.md"
"""Install guide the release freeze pins download URLs in.

Shared by {class}`~repomatic.release.prepare_release.PrepareRelease`, which rewrites
those URLs, and {func}`~repomatic.lint_repo.check_install_guide_downloads`,
which verifies the release they name actually carries the files.
"""


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
        f"{WORKFLOW_TARGET_ROOT}/{filename}",
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
        # `repomatic.labels.DEFAULT_*_RULES` and `[tool.repomatic.labels]`,
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
        # GitHub Actions resolves `uses: ./.github/actions/publish-pypi` and
        # `uses: kdeldycke/repomatic/.github/actions/publish-pypi@vX.Y.Z`
        # directly from the repo path; the file must stay on disk even when
        # byte-identical to the bundled default.
        keep_unmodified=True,
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
        # Subagents are user-facing documents the runtime auto-invokes by
        # description. Keep them on disk even when unmodified so it can always
        # discover them.
        keep_unmodified=True,
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
        # Skills are user-facing documents, not machine configs. Keep them
        # on disk even when unmodified so Claude Code can always find them.
        keep_unmodified=True,
        location_field="skills_location",
        files=(
            _skill_entry("av-false-positive", "Release"),
            _skill_entry("awesome-triage", "Maintenance", RepoScope.AWESOME_ONLY),
            _skill_entry("babysit-ci", "Quality"),
            _skill_entry("benchmark-update", "Development"),
            _skill_entry("brand-assets", "Development"),
            _skill_entry("file-bug-report", "Maintenance"),
            _skill_entry("github-housekeeping", "Maintenance"),
            _skill_entry("repomatic-audit", "Maintenance"),
            _skill_entry("repomatic-changelog", "Release"),
            _skill_entry("repomatic-deps", "Development"),
            _skill_entry("repomatic-init", "Setup"),
            _skill_entry("repomatic-ship", "Release"),
            _skill_entry("repomatic-test-matrix", "Quality"),
            _skill_entry("repomatic-topics", "Development"),
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
        init_default=InitDefault.AUTO,
        config_key="awesome-template.sync",
    ),
    GeneratedComponent(
        name="changelog",
        description="Minimal changelog.md",
        scope=RepoScope.PACKAGE_ONLY,
        target=location_path(Config.changelog_location),
    ),
    GeneratedComponent(
        name="plugin",
        description="Claude Code plugin marketplace wiring (.claude/settings.json)",
        # Opt-in like its `skills` and `subagents` siblings, and for a stronger
        # reason: this one asks every collaborator to install something. A bare
        # `repomatic init` never touches it.
        init_default=InitDefault.EXCLUDE,
        # The wiring is merged into a file the repository owns, so an unchanged
        # document is the steady state rather than a stale copy to clean up.
        keep_unmodified=True,
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

# Successors shared by several tombstones: the 2026 reorganization retired one
# skill per CI concern in favor of the workflow that now runs on every push, and
# each rename generation (gha-* → repokit-* → repomatic-*) left a tombstone
# pointing at the same replacement.
_NOW_IN_AUTOFIX = "now handled by autofix.yaml on every push"
_NOW_IN_LINT = "now handled by lint.yaml on every push"
_NOW_IN_METADATA_CMD = "now handled by the repomatic metadata CLI command"
_NOW_IN_TESTS = "now handled by tests.yaml on every push"
_REPLACED_BY_SHIP = "replaced by repomatic-ship"


def _removed_skill(
    skill_id: str,
    removed_in: str,
    *hashes: str,
    successor: str = "",
) -> RemovedAsset:
    """Declare a tombstone for a skill repomatic no longer ships.

    Skills are content-gated: the *hashes* are the normalized contents the skill
    shipped across its released lifetime (see {attr}`RemovedAsset.hashes` for the
    recipe that collects them). A skill owns its whole folder, so the tombstone
    carries that too (see {attr}`RemovedAsset.owned_dir`).
    """
    return RemovedAsset(
        "skills",
        _skill_target(skill_id),
        removed_in,
        hashes,
        owned_dir=_skill_dir(skill_id),
        successor=successor,
    )


def _removed_workflow(
    filename: str, removed_in: str, *, successor: str = ""
) -> RemovedAsset:
    """Declare a tombstone for a workflow repomatic no longer ships.

    Workflows are fingerprint-gated by their `uses:` line rather than hashed, so
    they carry no *hashes* (see the {class}`RemovedAsset` docstring).
    """
    return RemovedAsset(
        "workflows",
        f"{WORKFLOW_TARGET_ROOT}/{filename}",
        removed_in,
        successor=successor,
    )


REMOVED_ASSETS: tuple[RemovedAsset, ...] = (
    RemovedAsset(
        "codecov",
        ".github/codecov.yaml",
        "7.8.0.dev0",
        ("e8e96bfead62334599f4ec4c0448f2376352629789a70a76ae6fc3746ff7057b",),
        successor="coverage is now gated by pytest --cov-fail-under",
    ),
    RemovedAsset(
        "labels",
        ".github/labeller-content-based.yaml",
        "7.11.0.dev0",
        (
            "1f3e670c0b4c6687a8920fb3738a15fb82b8639b7825d81f76c55bc5784cdb08",
            "adf62c78c539229d34d4d2518a9af7f39df44d599c60852784b0faa47a6defa9",
            "5cf481b4aec2bf98a4056757f41ef5fc50f808dbd7c8a43f1dea0b224ecb7f1f",
            "8a047d53d5449ea0b53517e2f63e126360050127342084b7a705f34fb735d818",
        ),
        successor="rules now live in repomatic.labels.DEFAULT_CONTENT_RULES",
    ),
    RemovedAsset(
        "labels",
        ".github/labeller-file-based.yaml",
        "7.11.0.dev0",
        (
            "9dc0948e23a3a83d2cec5f11e400c75992fb1ce326eb6c5811c1fc3bfe258b31",
            "b216d370e4d2c6118f46d9bb2eacaf91392e6a6267a4f4857f44a698422cc860",
            "9a4feeb49c37ee7eba1d13957d26aaaa867c791ec12be8cd4197e7526bfbf963",
        ),
        successor="rules now live in repomatic.labels.DEFAULT_FILE_RULES",
    ),
    _removed_skill(
        "gha-changelog",
        "6.0.0",
        "2c178a58e1106f08aa6e540cd022eff12c4e954942ec5d794282c7b640adf768",
        successor="renamed to repomatic-changelog",
    ),
    _removed_skill(
        "gha-deps",
        "6.0.0",
        "d0bcb44f81335f4aabcadb82085f5048be12db252fc0a1f8c6bda8d9e5292efd",
        successor="renamed to repomatic-deps",
    ),
    _removed_skill(
        "gha-init",
        "6.0.0",
        "0f4f23f424c73774dd6253d9cb547e7a1d52ed64266c93b5b7271f4bee492a25",
        successor="renamed to repomatic-init",
    ),
    _removed_skill(
        "gha-lint",
        "6.0.0",
        "7079f4d79c6347b03b4788de97db2e1839006b606e9dbacbfeb51e9cca04db20",
        successor=_NOW_IN_LINT,
    ),
    _removed_skill(
        "gha-metadata",
        "6.0.0",
        "74c6f7d3574236d20aa7011b92f174abd2f8fdda162131e7f61851dfee7145fa",
        successor=_NOW_IN_METADATA_CMD,
    ),
    _removed_skill(
        "gha-release",
        "6.0.0",
        "99a466bc4d377bb056c5696de8f0eae2b025b34505ac951d504bee55a42bdd1c",
        successor=_REPLACED_BY_SHIP,
    ),
    _removed_skill(
        "gha-sync",
        "6.0.0",
        "f856f143db3f0ad37adb6c80b89c33efa5112e1307927ff3331f82857a71fef4",
        successor=_NOW_IN_AUTOFIX,
    ),
    _removed_skill(
        "gha-test",
        "6.0.0",
        "4a00dac78e0ca3c598c2a3ae6e649f354f73e754c5aaea531d8409f1eff23434",
        successor=_NOW_IN_TESTS,
    ),
    _removed_skill(
        "repokit-changelog",
        "6.0.1",
        "6e176d9d0090afb9d9a10035e4c6721fff8fac4a1c313010fc04a7ab631be399",
        successor="renamed to repomatic-changelog",
    ),
    _removed_skill(
        "repokit-deps",
        "6.0.1",
        "577687ae8481cc67b992497ee0de9fb38c0f26cd20a9b907a4bf78f834803cc0",
        successor="renamed to repomatic-deps",
    ),
    _removed_skill(
        "repokit-init",
        "6.0.1",
        "c68a9108ead81c4bb5b33912770155f6a587188ca72c8ba8d08f7283fdcad281",
        successor="renamed to repomatic-init",
    ),
    _removed_skill(
        "repokit-lint",
        "6.0.1",
        "1c05f0fb8c5ff8eed38ac02af2fff016e931fdf8866fd93a3fc6c61f84d4df52",
        successor=_NOW_IN_LINT,
    ),
    _removed_skill(
        "repokit-metadata",
        "6.0.1",
        "0322f70cdd8e53d03fce2befbf904be1f0dc5596b79e41557ce8ec788a202cff",
        successor=_NOW_IN_METADATA_CMD,
    ),
    _removed_skill(
        "repokit-release",
        "6.0.1",
        "a6ceb0394f084f481765bb834f275af0cb1cf58a9383059358ceec50ea87b93a",
        successor=_REPLACED_BY_SHIP,
    ),
    _removed_skill(
        "repokit-sync",
        "6.0.1",
        "412811337a541b6c4518e588240ce2cb13f3f476bcd311f32edcf04394e17ade",
        successor=_NOW_IN_AUTOFIX,
    ),
    _removed_skill(
        "repokit-test",
        "6.0.1",
        "63f0b532f379aa4400eea5a6284c3004ddc09749c8f476f4ea5a5e8ce3c4716f",
        successor=_NOW_IN_TESTS,
    ),
    _removed_skill(
        "repomatic-lint",
        "6.21.0",
        "11131553c99adb7daf880b6b19b84e4d4573eedbe7b951092aa7d4a1f9357aab",
        "d72cada008b46db93eff0b7a167f1f57346c528ec317fca73857205895fb1395",
        "058b9cc3248cd1d537d8fbf7a0c1133e3107c6ed405859457e88625b9301d3d8",
        "7ec6520cba0a14af07ed1bb4e2f0388109ac8db0509ca92ffa0829cf2967bd11",
        successor=_NOW_IN_LINT,
    ),
    _removed_skill(
        "repomatic-metadata",
        "6.3.0",
        "e94ba4246c0bf56b8dfb6a7e4d3ea2e9521c000e8322130b1746e7a54d3f260b",
        "58c6eec756177f445893366960464c2d5872de994a692399440df0eb30b11e35",
        successor=_NOW_IN_METADATA_CMD,
    ),
    _removed_skill(
        "repomatic-release",
        "6.21.0",
        "0ecfa8ff5d55b33394d83bce76d39015450403124ff63e131fea14adf685c00b",
        "8546a42c1ea44b2a4fa0ed1bc49f71eaf8be3b5656a323ee93957ea1fdb0bb38",
        "778783f3ef6093d9892a4772fc312747155b399e18ba33f416fa9b138897b43d",
        "b076cae374b3104f50996cf8b92eae6f53ec9546d3b0fab2c033c90cb1e8a107",
        "8e93d723827042e90acbe22d038516400bcd743bf39f3fb45a65c115008a97d0",
        successor=_REPLACED_BY_SHIP,
    ),
    _removed_skill(
        "repomatic-sync",
        "6.21.0",
        "3b36a8b4fc76282c280f6cc19fdc24aa826db8a81ee91a66737b24cb921c84d9",
        "1460738708f7e878c17ef578a7fad14710962a5fd7e7789f3bc08ae6bc49247b",
        "54a2b2aa40799c05d666295ee0a1f4d65946605c5397a006185123e4c2e9f1d0",
        "771d4e15efab4739fb00a7c1ba20495e063025842beb2e54d84207e1410f40a1",
        "687c7f9cae7271ee56f4d35b754325ba7a2c3b13537eee057679cc160e39471e",
        "ceaf3141599850847ee51b2e4f85c76a4cae130a01b2a4fd820dd3b5c0dd0dc0",
        "91add2c0b7686f64f810bb86fa70c3ac99d3940b37ba6fbe57c01a4d427cc902",
        successor=_NOW_IN_AUTOFIX,
    ),
    _removed_skill(
        "repomatic-test",
        "6.21.0",
        "8bc5f054507b369f9be34dd4a34183e00b6a8e0186c34d4deb385032e6682e1a",
        "cb987bfe342c2d00ea1a6226585238f19bc5a351a678124f7e6225d5c6122c2c",
        "17bae80a4b98518b6037518ad340a60d117d35a4fa26725fa2ab685ebd23e8dd",
        successor=_NOW_IN_TESTS,
    ),
    _removed_workflow(
        "label-sponsors.yaml",
        "4.25.0",
        successor="merged into labels.yaml",
    ),
    _removed_workflow(
        "labeller-content-based.yaml",
        "4.25.0",
        successor="merged into labels.yaml",
    ),
    _removed_workflow(
        "labeller-file-based.yaml",
        "4.25.0",
        successor="merged into labels.yaml",
    ),
    _removed_workflow(
        "renovate.yaml",
        "7.0.0.dev0",
        successor="replaced by self-hosted sync-tool-versions, sync-action-pins,"
        " and sync-workflow-pins",
    ),
)
r"""Tombstones for assets repomatic has dropped (see {class}`RemovedAsset`).

`init` prunes orphaned copies of these from downstream repos. Ordered by
`(component, target)`.

When you drop a bundled asset from {data}`COMPONENTS`, add an entry here so
the removal propagates downstream on the next `init` instead of leaving an
orphan. List one hash per distinct content the asset shipped across its
released lifetime, collected from the release tags where its data file
existed:

```python
import hashlib, subprocess

src = "repomatic/data/skill-repomatic-release.md"  # the dropped data file
tags = subprocess.run(
    ["git", "tag", "--list", "v*"], capture_output=True, text=True, check=True
).stdout.split()
hashes = {}
for tag in tags:
    blob = subprocess.run(
        ["git", "show", f"{tag}:{src}"],
        capture_output=True, text=True, encoding="UTF-8",
    )
    if blob.returncode == 0:
        normalized = blob.stdout.rstrip() + "\n"
        hashes.setdefault(hashlib.sha256(normalized.encode("UTF-8")).hexdigest(), tag)
print(tuple(hashes))  # distinct contents, in first-shipped order
```

Removed *workflows* are fingerprint-gated, not hashed: omit `hashes` and give
the workflow's downstream path as `target` (`.github/workflows/{name}`).
"""

DEFAULT_REPO: str = "kdeldycke/repomatic"
"""Default upstream repository for reusable workflows."""


def is_awesome_repo(name: str) -> bool:
    """Whether a repository name marks an `awesome-*` curated list.

    The one spelling of the prefix test: repository-trait detection, the
    broken-links label choice and the awesome-template auto-inclusion all
    classify on it.
    """
    return name.startswith("awesome-")


def package_of(repo: str) -> str:
    """The package name an `owner/repo` slug implies: its repository half.

    The one spelling of that derivation, shared by every check and rewriter
    that maps a `--upstream-repo` value onto the inline `package==X.Y.Z` pin
    it governs.
    """
    return repo.rsplit("/", 1)[-1]


UPSTREAM_PACKAGE: str = package_of(DEFAULT_REPO)
"""Distribution name of the upstream toolkit, derived from {data}`DEFAULT_REPO`.

The freeze, cooldown-exemption, and lint code that handles the `uses:` refs
and the inline self-pin all key on this name: deriving it here keeps the
writer/checker pairs in lockstep and makes a rename a one-line change.
"""

UPSTREAM_REPO_SLUGS: tuple[str, ...] = (
    DEFAULT_REPO,
    "kdeldycke/repokit",
    "kdeldycke/workflows",
)
"""Upstream repository slugs across the project's renames, current first.

A downstream thin-caller's `uses:` line references whichever slug was current
when it was generated. Workflow-tombstone detection matches against all of
them (current first, since most callers are recent) so an orphaned thin-caller
is recognized regardless of which era set it up."""

UPSTREAM_SOURCE_GLOB: str = "repomatic/**"
"""Path glob for the upstream source directory in canonical workflows.

Canonical workflow `paths:` filters use this glob to match source code
changes. In downstream repos, this is replaced with the project's own source
directory.
"""

UPSTREAM_SOURCE_PREFIX: str = "repomatic/"
"""Path prefix for upstream-specific files in canonical workflows.

Paths starting with this prefix (but not matching
{data}`UPSTREAM_SOURCE_GLOB`) are dropped in downstream thin callers because
they reference files that only exist in the upstream repository (like
`repomatic/data/labels.toml`).
"""

SKILL_PHASE_ORDER: tuple[str, ...] = (
    "Setup",
    "Development",
    "Quality",
    "Maintenance",
    "Release",
)
"""Canonical display order for lifecycle phases in `list-skills` output."""

SKILL_LIST_HEADER_DEFS: tuple[tuple[str, str], ...] = (
    ("Phase", "phase"),
    ("Skill", "skill"),
    ("Description", "description"),
)
"""Column definitions for the `repomatic list-skills` table.

Lives beside {func}`skill_catalog`, whose triples these columns render, so the
two cannot drift apart. The columns mirror the hand-maintained roster of
`docs/agent-skills.md`, which the page renders this command right above.
"""


# ---------------------------------------------------------------------------
# Registry queries.
# ---------------------------------------------------------------------------

ALL_COMPONENTS: dict[str, str] = {c.name: c.description for c in COMPONENTS}
"""All available init components."""

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
paths for the same reason they skip {data}`UPSTREAM_REPO_SLUGS`: a pin the next
`sync-repomatic` overwrites turns the two pull requests into a ping-pong, the bump
PR and the init-revert PR chasing each other. The skip lifts inside the source
repo, where each bundled source is a symlink to its in-tree target and the pin is
a normal source-of-truth ref (see `repomatic.sync_ops._pinnable_files`). Generated
workflows ({class}`WorkflowComponent`) are deliberately absent: they carry only
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
    sorted(f.file_id for f in COMPONENTS_BY_NAME["workflows"].files)
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
repository (`repomatic/tooling/tool_registry.py` and friends). A workflow that merely
*behaves* differently upstream does not: it still ships, so it still needs the
runtime guard.
"""

SKILL_PHASES: dict[str, str] = {
    f.file_id: f.phase for f in COMPONENTS_BY_NAME["skills"].files if f.phase
}
"""Maps skill names to lifecycle phases for display grouping."""


def skill_catalog() -> list[tuple[str, str, str]]:
    """Read every bundled skill's display metadata off its frontmatter.

    :return: One `(phase, name, description)` tuple per bundled skill, in
        registry order, with the description's trailing period stripped for
        table display. Phases are keyed by the registry `file_id`, not the
        frontmatter name, so a skill renamed in frontmatter still lands in
        its phase.
    """
    skills = []
    for entry in COMPONENTS_BY_NAME["skills"].files:
        # Each skill is a bundled folder, so reach past it for the entry point.
        content = get_data_content(f"{entry.source}/{SKILL_FILENAME}")
        meta, _body = split_frontmatter(content)
        name = str(meta.get("name", entry.file_id))
        description = str(meta.get("description", "")).removesuffix(".")
        skills.append((SKILL_PHASES[entry.file_id], name, description))
    return skills


FILE_SELECTOR_COMPONENTS: tuple[str, ...] = tuple(c.name for c in COMPONENTS if c.files)
"""Components that support file-level `component/file` selectors."""

_MAX_NAME = max(len(c.name) for c in COMPONENTS)
COMPONENT_HELP_TABLE: str = "\n".join(
    f"    {c.name:<{_MAX_NAME + 4}s}{c.description}" for c in COMPONENTS
)
"""Formatted component table for CLI help text."""


def valid_file_ids(component: str) -> frozenset[str]:
    """Return valid file identifiers for a component.

    Components with file entries report their declared `file_id` values.
    Returns an empty set for components without file-level selection
    (e.g., changelog, tool configs).
    """
    comp = COMPONENTS_BY_NAME.get(component)
    if comp is None:
        return frozenset()
    return frozenset(entry.file_id for entry in comp.files)


def excluded_rel_path(component: str, file_id: str) -> str | None:
    """Map a component and file identifier to its relative output path.

    Returns `None` when the identifier cannot be resolved (e.g., for tool
    config components that have no file-level exclusion support).
    """
    comp = COMPONENTS_BY_NAME.get(component)
    if comp is None:
        return None
    for entry in comp.files:
        if entry.file_id == file_id:
            return entry.target
    return None


def parse_component_entries(
    entries: Sequence[str],
    *,
    context: str = "entry",
) -> tuple[set[str], dict[str, set[str]]]:
    """Parse component entries into full-component and file-level sets.

    Bare names (no `/`) must be component names from
    {data}`ALL_COMPONENTS`. Qualified `component/identifier` entries
    target individual files. Raises `ValueError` on unknown entries.

    Used by both the `exclude` config path and the CLI positional
    selection, with *context* controlling error message wording.

    :param context: Label for error messages (e.g., `"exclude"`,
        `"selection"`).
    :return: `(full_components, file_selections)` where
        `file_selections` maps component names to sets of file
        identifiers.
    """
    full_components: set[str] = set()
    file_selections: dict[str, set[str]] = {}

    for entry in entries:
        if "/" in entry:
            component, file_id = entry.split("/", 1)
            if component not in ALL_COMPONENTS:
                msg = (
                    f"Unknown component {component!r} in {context}"
                    f" {entry!r}. Valid components:"
                    f" {', '.join(sorted(ALL_COMPONENTS))}"
                )
                raise ValueError(msg)
            valid = valid_file_ids(component)
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
        elif entry in ALL_COMPONENTS:
            full_components.add(entry)
        else:
            msg = (
                f"Unknown {context} {entry!r}. Use a component name"
                f" ({', '.join(sorted(ALL_COMPONENTS))}) or a"
                " qualified component/file entry"
                " (e.g., 'workflows/autolock.yaml')."
            )
            raise ValueError(msg)

    return full_components, file_selections
