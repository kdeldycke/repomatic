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

    - Content-gated (skills, agents): the file is deleted only when its
      normalized content matches one of {attr}`hashes` (a version repomatic
      shipped), proving it is an untouched copy.
    - Fingerprint-gated (workflows): thin-callers are parameterized per repo
      (version pin, `paths:` filters), so they carry no fixed content. The file
      is deleted only when it is a repomatic-lineage thin-caller for this
      workflow (its `uses:` line references an upstream slug, see
      {data}`UPSTREAM_REPO_SLUGS`) with no extra downstream jobs.

    Either way, a locally modified orphan is reported for manual review, never
    deleted.
    """

    component: str
    """Component the asset belonged to (like `"skills"` or `"workflows"`)."""

    target: str
    """Relative output path the asset occupied, in default-location form
    (like `.claude/skills/repomatic-release/SKILL.md` or
    `.github/workflows/label-sponsors.yaml`).

    Build skill and agent targets with `_skill_target` / `_agent_target` so
    they match the live registry: the `skills.location` and `agents.location`
    overrides are re-applied at detection time. Workflow targets are literal
    (`.github/workflows/` is fixed by GitHub)."""

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

    successor: str = ""
    """Optional human note describing what replaced the asset, shown in the
    report (like `replaced by repomatic-ship`)."""


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
                # Downstream artifact is release.yaml (the entry), generated from
                # and pointing its `uses:` at the _release-engine.yaml reusable.
                # See WORKFLOW_SOURCES and workflow_sync.generate_thin_caller.
                "_release-engine.yaml",
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


# ---------------------------------------------------------------------------
# Removed assets (tombstones).
# ---------------------------------------------------------------------------

REMOVED_ASSETS: tuple[RemovedAsset, ...] = (
    RemovedAsset(
        "skills",
        _skill_target("gha-changelog"),
        "6.0.0",
        ("2c178a58e1106f08aa6e540cd022eff12c4e954942ec5d794282c7b640adf768",),
        successor="renamed to repomatic-changelog",
    ),
    RemovedAsset(
        "skills",
        _skill_target("gha-deps"),
        "6.0.0",
        ("d0bcb44f81335f4aabcadb82085f5048be12db252fc0a1f8c6bda8d9e5292efd",),
        successor="renamed to repomatic-deps",
    ),
    RemovedAsset(
        "skills",
        _skill_target("gha-init"),
        "6.0.0",
        ("0f4f23f424c73774dd6253d9cb547e7a1d52ed64266c93b5b7271f4bee492a25",),
        successor="renamed to repomatic-init",
    ),
    RemovedAsset(
        "skills",
        _skill_target("gha-lint"),
        "6.0.0",
        ("7079f4d79c6347b03b4788de97db2e1839006b606e9dbacbfeb51e9cca04db20",),
        successor="now handled by lint.yaml on every push",
    ),
    RemovedAsset(
        "skills",
        _skill_target("gha-metadata"),
        "6.0.0",
        ("74c6f7d3574236d20aa7011b92f174abd2f8fdda162131e7f61851dfee7145fa",),
        successor="now handled by the repomatic metadata CLI command",
    ),
    RemovedAsset(
        "skills",
        _skill_target("gha-release"),
        "6.0.0",
        ("99a466bc4d377bb056c5696de8f0eae2b025b34505ac951d504bee55a42bdd1c",),
        successor="replaced by repomatic-ship",
    ),
    RemovedAsset(
        "skills",
        _skill_target("gha-sync"),
        "6.0.0",
        ("f856f143db3f0ad37adb6c80b89c33efa5112e1307927ff3331f82857a71fef4",),
        successor="now handled by autofix.yaml on every push",
    ),
    RemovedAsset(
        "skills",
        _skill_target("gha-test"),
        "6.0.0",
        ("4a00dac78e0ca3c598c2a3ae6e649f354f73e754c5aaea531d8409f1eff23434",),
        successor="now handled by tests.yaml on every push",
    ),
    RemovedAsset(
        "skills",
        _skill_target("repokit-changelog"),
        "6.0.1",
        ("6e176d9d0090afb9d9a10035e4c6721fff8fac4a1c313010fc04a7ab631be399",),
        successor="renamed to repomatic-changelog",
    ),
    RemovedAsset(
        "skills",
        _skill_target("repokit-deps"),
        "6.0.1",
        ("577687ae8481cc67b992497ee0de9fb38c0f26cd20a9b907a4bf78f834803cc0",),
        successor="renamed to repomatic-deps",
    ),
    RemovedAsset(
        "skills",
        _skill_target("repokit-init"),
        "6.0.1",
        ("c68a9108ead81c4bb5b33912770155f6a587188ca72c8ba8d08f7283fdcad281",),
        successor="renamed to repomatic-init",
    ),
    RemovedAsset(
        "skills",
        _skill_target("repokit-lint"),
        "6.0.1",
        ("1c05f0fb8c5ff8eed38ac02af2fff016e931fdf8866fd93a3fc6c61f84d4df52",),
        successor="now handled by lint.yaml on every push",
    ),
    RemovedAsset(
        "skills",
        _skill_target("repokit-metadata"),
        "6.0.1",
        ("0322f70cdd8e53d03fce2befbf904be1f0dc5596b79e41557ce8ec788a202cff",),
        successor="now handled by the repomatic metadata CLI command",
    ),
    RemovedAsset(
        "skills",
        _skill_target("repokit-release"),
        "6.0.1",
        ("a6ceb0394f084f481765bb834f275af0cb1cf58a9383059358ceec50ea87b93a",),
        successor="replaced by repomatic-ship",
    ),
    RemovedAsset(
        "skills",
        _skill_target("repokit-sync"),
        "6.0.1",
        ("412811337a541b6c4518e588240ce2cb13f3f476bcd311f32edcf04394e17ade",),
        successor="now handled by autofix.yaml on every push",
    ),
    RemovedAsset(
        "skills",
        _skill_target("repokit-test"),
        "6.0.1",
        ("63f0b532f379aa4400eea5a6284c3004ddc09749c8f476f4ea5a5e8ce3c4716f",),
        successor="now handled by tests.yaml on every push",
    ),
    RemovedAsset(
        "skills",
        _skill_target("repomatic-lint"),
        "6.21.0",
        (
            "11131553c99adb7daf880b6b19b84e4d4573eedbe7b951092aa7d4a1f9357aab",
            "d72cada008b46db93eff0b7a167f1f57346c528ec317fca73857205895fb1395",
            "058b9cc3248cd1d537d8fbf7a0c1133e3107c6ed405859457e88625b9301d3d8",
            "7ec6520cba0a14af07ed1bb4e2f0388109ac8db0509ca92ffa0829cf2967bd11",
        ),
        successor="now handled by lint.yaml on every push",
    ),
    RemovedAsset(
        "skills",
        _skill_target("repomatic-metadata"),
        "6.3.0",
        (
            "e94ba4246c0bf56b8dfb6a7e4d3ea2e9521c000e8322130b1746e7a54d3f260b",
            "58c6eec756177f445893366960464c2d5872de994a692399440df0eb30b11e35",
        ),
        successor="now handled by the repomatic metadata CLI command",
    ),
    RemovedAsset(
        "skills",
        _skill_target("repomatic-release"),
        "6.21.0",
        (
            "0ecfa8ff5d55b33394d83bce76d39015450403124ff63e131fea14adf685c00b",
            "8546a42c1ea44b2a4fa0ed1bc49f71eaf8be3b5656a323ee93957ea1fdb0bb38",
            "778783f3ef6093d9892a4772fc312747155b399e18ba33f416fa9b138897b43d",
            "b076cae374b3104f50996cf8b92eae6f53ec9546d3b0fab2c033c90cb1e8a107",
            "8e93d723827042e90acbe22d038516400bcd743bf39f3fb45a65c115008a97d0",
        ),
        successor="replaced by repomatic-ship",
    ),
    RemovedAsset(
        "skills",
        _skill_target("repomatic-sync"),
        "6.21.0",
        (
            "3b36a8b4fc76282c280f6cc19fdc24aa826db8a81ee91a66737b24cb921c84d9",
            "1460738708f7e878c17ef578a7fad14710962a5fd7e7789f3bc08ae6bc49247b",
            "54a2b2aa40799c05d666295ee0a1f4d65946605c5397a006185123e4c2e9f1d0",
            "771d4e15efab4739fb00a7c1ba20495e063025842beb2e54d84207e1410f40a1",
            "687c7f9cae7271ee56f4d35b754325ba7a2c3b13537eee057679cc160e39471e",
            "ceaf3141599850847ee51b2e4f85c76a4cae130a01b2a4fd820dd3b5c0dd0dc0",
            "91add2c0b7686f64f810bb86fa70c3ac99d3940b37ba6fbe57c01a4d427cc902",
        ),
        successor="now handled by autofix.yaml on every push",
    ),
    RemovedAsset(
        "skills",
        _skill_target("repomatic-test"),
        "6.21.0",
        (
            "8bc5f054507b369f9be34dd4a34183e00b6a8e0186c34d4deb385032e6682e1a",
            "cb987bfe342c2d00ea1a6226585238f19bc5a351a678124f7e6225d5c6122c2c",
            "17bae80a4b98518b6037518ad340a60d117d35a4fa26725fa2ab685ebd23e8dd",
        ),
        successor="now handled by tests.yaml on every push",
    ),
    RemovedAsset(
        "workflows",
        ".github/workflows/label-sponsors.yaml",
        "4.25.0",
        successor="merged into labels.yaml",
    ),
    RemovedAsset(
        "workflows",
        ".github/workflows/labeller-content-based.yaml",
        "4.25.0",
        successor="merged into labels.yaml",
    ),
    RemovedAsset(
        "workflows",
        ".github/workflows/labeller-file-based.yaml",
        "4.25.0",
        successor="merged into labels.yaml",
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
        ["git", "show", f"{tag}:{src}"], capture_output=True, text=True, encoding="UTF-8"
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

UPSTREAM_REPO_SLUGS: tuple[str, ...] = (
    "kdeldycke/repomatic",
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

WORKFLOW_SOURCES: dict[str, str] = {
    f.file_id: f.source for f in _BY_NAME["workflows"].files
}
"""Maps each workflow's downstream file_id to its bundled source filename.

For most workflows source == file_id. The release entry is the exception: its
downstream artifact is `release.yaml`, generated from and pointing its `uses:`
at the `_release-engine.yaml` reusable engine."""

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
