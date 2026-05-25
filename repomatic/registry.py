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

All derived constants (`ALL_COMPONENTS`, `COMPONENT_FILES`,
`REUSABLE_WORKFLOWS`, `SKILL_PHASES`, etc.) are computed from this single
registry in {mod}`repomatic.init_project`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto

from .config import Config

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Sequence


class InitDefault(Enum):
    """How `init` treats the component when no explicit CLI args are given."""

    INCLUDE = auto()
    """Included by default (e.g., changelog, renovate, workflows)."""

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

    The classification has two axes: whether the repo is an `awesome-*` list
    and whether it carries a PEP 621 `pyproject.toml`. In practice these are
    mutually exclusive (awesome repos are content lists, not Python packages),
    so a single scope value suffices.

    Scope restrictions are defaults: they apply during bare `repomatic init`
    but are bypassed when components are explicitly named on the CLI or
    covered by `[tool.repomatic] include`.
    """

    ALL = auto()
    """Included in every repository type."""

    AWESOME_ONLY = auto()
    """Only for `awesome-*` repositories."""

    PYTHON_ONLY = auto()
    """Only for Python projects (PEP 621 `[project].name` present)."""

    def matches(self, is_awesome: bool, is_python: bool) -> bool:
        """Whether this scope applies to the given repository traits.

        :param is_awesome: `True` for `awesome-*` repositories.
        :param is_python: `True` for repositories whose `pyproject.toml`
            declares a PEP 621 `[project].name`.
        """
        if self is RepoScope.ALL:
            return True
        if self is RepoScope.AWESOME_ONLY:
            return is_awesome
        return is_python


@dataclass(frozen=True)
class FileEntry:
    """A single file managed within a component."""

    source: str
    """Filename in `repomatic/data/`."""

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

    def is_enabled(self, config: object) -> bool:
        """Whether this entry is enabled by the given `Config` object.

        Returns `True` when no `config_key` is set (unconditionally
        enabled) or when the corresponding config field is truthy.

        :param config: A {class}`~repomatic.config.Config` instance.
        """
        if not self.config_key:
            return True
        field = self.config_key.replace("-", "_").replace(".", "_")
        return getattr(config, field, self.config_default)

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

    def is_enabled(self, config: object) -> bool:
        """Whether this component is enabled by the given `Config` object.

        Returns `True` when no `config_key` is set (unconditionally
        enabled) or when the corresponding config field is truthy.

        :param config: A {class}`~repomatic.config.Config` instance.
        """
        if not self.config_key:
            return True
        field = self.config_key.replace("-", "_").replace(".", "_")
        return getattr(config, field, self.config_default)


@dataclass(frozen=True)
class BundledComponent(Component):
    """Files copied from `repomatic/data/` to a target path."""


@dataclass(frozen=True)
class WorkflowComponent(Component):
    """Thin-caller generation and header sync."""


@dataclass(frozen=True)
class ToolConfigComponent(Component):
    """Merged into `pyproject.toml`."""

    source_file: str = ""
    """Filename in `repomatic/data/`."""

    tool_section: str = ""
    """The `[tool.X]` section name to check for existence."""

    insert_after: tuple[str, ...] = ()
    """Sections to insert after in `pyproject.toml`
    (in priority order)."""

    insert_before: tuple[str, ...] = ()
    """Sections to insert before in `pyproject.toml`
    (if `insert_after` not found)."""

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


# ---------------------------------------------------------------------------
# Helpers for computed targets.
# ---------------------------------------------------------------------------


def _agent_target(agent_id: str) -> str:
    """Build the default target path for an agent file from the Config default."""
    prefix = Config.agents_location.removeprefix("./").rstrip("/")
    return f"{prefix}/{agent_id}.md"


def _skill_target(skill_id: str) -> str:
    """Build the default target path for a skill file from the Config default."""
    prefix = Config.skills_location.removeprefix("./").rstrip("/")
    return f"{prefix}/{skill_id}/SKILL.md"


# ---------------------------------------------------------------------------
# The registry.
# ---------------------------------------------------------------------------

COMPONENTS: tuple[Component, ...] = (
    # --- Bundled file components ---
    BundledComponent(
        name="labels",
        description="Label config files (labels.toml + labeller rules)",
        init_default=InitDefault.EXCLUDE,
        files=(
            FileEntry(
                "labeller-content-based.yaml",
                ".github/labeller-content-based.yaml",
            ),
            FileEntry(
                "labeller-file-based.yaml",
                ".github/labeller-file-based.yaml",
            ),
            FileEntry("labels.toml"),
        ),
    ),
    BundledComponent(
        name="codecov",
        description="Codecov PR comment config (.github/codecov.yaml)",
        scope=RepoScope.PYTHON_ONLY,
        # Codecov reads config directly from the repo; the file must stay on
        # disk for the settings to take effect.
        keep_unmodified=True,
        files=(FileEntry("codecov.yaml", ".github/codecov.yaml"),),
    ),
    BundledComponent(
        name="renovate",
        description="Renovate config (renovate.json5)",
        init_default=InitDefault.EXCLUDE,
        files=(FileEntry("renovate.json5"),),
    ),
    BundledComponent(
        name="publish-pypi-action",
        description=(
            "Composite action that publishes to PyPI via Trusted Publishing"
            " (.github/actions/publish-pypi/)"
        ),
        scope=RepoScope.PYTHON_ONLY,
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
        name="agents",
        description="Claude Code agent definitions (.claude/agents/)",
        init_default=InitDefault.EXCLUDE,
        # Agents are user-facing documents that Claude Code auto-invokes by
        # description. Keep them on disk even when unmodified so the runtime
        # can always discover them.
        keep_unmodified=True,
        files=(
            FileEntry(
                "agent-grunt-qa.md",
                _agent_target("grunt-qa"),
                "grunt-qa",
            ),
            FileEntry(
                "agent-qa-engineer.md",
                _agent_target("qa-engineer"),
                "qa-engineer",
            ),
            FileEntry(
                "agent-sphinx-docs.md",
                _agent_target("sphinx-docs"),
                "sphinx-docs",
            ),
        ),
    ),
    BundledComponent(
        name="skills",
        description="Claude Code skill definitions (.claude/skills/)",
        init_default=InitDefault.EXCLUDE,
        # Skills are user-facing documents, not machine configs. Keep them
        # on disk even when unmodified so Claude Code can always find them.
        keep_unmodified=True,
        files=(
            FileEntry(
                "skill-av-false-positive.md",
                _skill_target("av-false-positive"),
                "av-false-positive",
                phase="Release",
            ),
            FileEntry(
                "skill-awesome-triage.md",
                _skill_target("awesome-triage"),
                "awesome-triage",
                scope=RepoScope.AWESOME_ONLY,
                phase="Maintenance",
            ),
            FileEntry(
                "skill-babysit-ci.md",
                _skill_target("babysit-ci"),
                "babysit-ci",
                phase="Quality",
            ),
            FileEntry(
                "skill-benchmark-update.md",
                _skill_target("benchmark-update"),
                "benchmark-update",
                phase="Development",
            ),
            FileEntry(
                "skill-brand-assets.md",
                _skill_target("brand-assets"),
                "brand-assets",
                phase="Development",
            ),
            FileEntry(
                "skill-file-bug-report.md",
                _skill_target("file-bug-report"),
                "file-bug-report",
                phase="Maintenance",
            ),
            FileEntry(
                "skill-repomatic-audit.md",
                _skill_target("repomatic-audit"),
                "repomatic-audit",
                phase="Maintenance",
            ),
            FileEntry(
                "skill-repomatic-changelog.md",
                _skill_target("repomatic-changelog"),
                "repomatic-changelog",
                phase="Release",
            ),
            FileEntry(
                "skill-repomatic-deps.md",
                _skill_target("repomatic-deps"),
                "repomatic-deps",
                phase="Development",
            ),
            FileEntry(
                "skill-repomatic-init.md",
                _skill_target("repomatic-init"),
                "repomatic-init",
                phase="Setup",
            ),
            FileEntry(
                "skill-repomatic-ship.md",
                _skill_target("repomatic-ship"),
                "repomatic-ship",
                phase="Release",
            ),
            FileEntry(
                "skill-repomatic-topics.md",
                _skill_target("repomatic-topics"),
                "repomatic-topics",
                phase="Development",
            ),
            FileEntry(
                "skill-sphinx-docs-sync.md",
                _skill_target("sphinx-docs-sync"),
                "sphinx-docs-sync",
                phase="Maintenance",
            ),
            FileEntry(
                "skill-translation-sync.md",
                _skill_target("translation-sync"),
                "translation-sync",
                scope=RepoScope.AWESOME_ONLY,
                phase="Maintenance",
            ),
            FileEntry(
                "skill-upstream-audit.md",
                _skill_target("upstream-audit"),
                "upstream-audit",
                phase="Maintenance",
            ),
        ),
    ),
    # --- Workflow component ---
    WorkflowComponent(
        name="workflows",
        description="Thin-caller workflow files",
        files=(
            FileEntry("autofix.yaml", ".github/workflows/autofix.yaml"),
            FileEntry("autolock.yaml", ".github/workflows/autolock.yaml"),
            FileEntry("cancel-runs.yaml", ".github/workflows/cancel-runs.yaml"),
            FileEntry(
                "changelog.yaml",
                ".github/workflows/changelog.yaml",
                scope=RepoScope.PYTHON_ONLY,
            ),
            FileEntry(
                "debug.yaml",
                ".github/workflows/debug.yaml",
                scope=RepoScope.PYTHON_ONLY,
            ),
            FileEntry("docs.yaml", ".github/workflows/docs.yaml"),
            FileEntry("labels.yaml", ".github/workflows/labels.yaml"),
            FileEntry("lint.yaml", ".github/workflows/lint.yaml"),
            FileEntry(
                "release.yaml",
                ".github/workflows/release.yaml",
                scope=RepoScope.PYTHON_ONLY,
            ),
            FileEntry("renovate.yaml", ".github/workflows/renovate.yaml"),
            FileEntry(
                "tests.yaml",
                ".github/workflows/tests.yaml",
                reusable=False,
            ),
            FileEntry(
                "unsubscribe.yaml",
                ".github/workflows/unsubscribe.yaml",
                config_key="notification.unsubscribe",
            ),
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
        scope=RepoScope.PYTHON_ONLY,
        target=Config.changelog_location.removeprefix("./"),
    ),
    # --- Tool config components (merged into pyproject.toml) ---
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
        insert_after=("tool.uv", "tool.uv.build-backend"),
        insert_before=("tool.pytest",),
    ),
    ToolConfigComponent(
        name="pytest",
        description="Pytest test configuration",
        init_default=InitDefault.EXPLICIT,
        source_file="pytest.toml",
        tool_section="tool.pytest",
        insert_after=("tool.ruff", "tool.ruff.format"),
        insert_before=("tool.mypy",),
    ),
    ToolConfigComponent(
        name="mypy",
        description="Mypy type checking configuration",
        init_default=InitDefault.EXPLICIT,
        source_file="mypy.toml",
        tool_section="tool.mypy",
        insert_after=("tool.pytest",),
        insert_before=("tool.nuitka", "tool.bumpversion"),
    ),
    ToolConfigComponent(
        name="mdformat",
        description="mdformat Markdown formatter configuration",
        init_default=InitDefault.EXPLICIT,
        source_file="mdformat.toml",
        tool_section="tool.mdformat",
        insert_after=("tool.coverage",),
        insert_before=("tool.bumpversion",),
    ),
    ToolConfigComponent(
        name="bumpversion",
        description="bump-my-version configuration",
        init_default=InitDefault.EXPLICIT,
        source_file="bumpversion.toml",
        tool_section="tool.bumpversion",
        insert_after=("tool.mdformat", "tool.nuitka", "tool.mypy"),
        insert_before=("tool.typos",),
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
        insert_after=("tool.bumpversion",),
        insert_before=("tool.pytest",),
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

_BY_NAME: dict[str, Component] = {c.name: c for c in COMPONENTS}
"""Index for O(1) component lookup by name."""

DEFAULT_REPO: str = "kdeldycke/repomatic"
"""Default upstream repository for reusable workflows."""

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
they reference files that only exist in the upstream repository (e.g.,
`repomatic/data/renovate.json5`).
"""

SKILL_PHASE_ORDER: tuple[str, ...] = (
    "Setup",
    "Development",
    "Quality",
    "Maintenance",
    "Release",
)
"""Canonical display order for lifecycle phases in `list-skills` output."""


# ---------------------------------------------------------------------------
# Registry queries.
# ---------------------------------------------------------------------------

ALL_COMPONENTS: dict[str, str] = {c.name: c.description for c in COMPONENTS}
"""All available init components."""

REUSABLE_WORKFLOWS: tuple[str, ...] = tuple(
    f.file_id for f in _BY_NAME["workflows"].files if f.reusable
)
"""Workflow filenames that support `workflow_call` triggers."""

NON_REUSABLE_WORKFLOWS: frozenset[str] = frozenset(
    f.file_id for f in _BY_NAME["workflows"].files if not f.reusable
)
"""Workflows without `workflow_call` that cannot be used as thin callers."""

ALL_WORKFLOW_FILES: tuple[str, ...] = tuple(
    sorted(f.file_id for f in _BY_NAME["workflows"].files)
)
"""All workflow filenames (reusable and non-reusable)."""

SKILL_PHASES: dict[str, str] = {
    f.file_id: f.phase for f in _BY_NAME["skills"].files if f.phase
}
"""Maps skill names to lifecycle phases for display grouping."""


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
    comp = _BY_NAME.get(component)
    if comp is None:
        return frozenset()
    return frozenset(entry.file_id for entry in comp.files)


def excluded_rel_path(component: str, file_id: str) -> str | None:
    """Map a component and file identifier to its relative output path.

    Returns `None` when the identifier cannot be resolved (e.g., for tool
    config components that have no file-level exclusion support).
    """
    comp = _BY_NAME.get(component)
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
                " (e.g., 'workflows/debug.yaml')."
            )
            raise ValueError(msg)

    return full_components, file_selections
