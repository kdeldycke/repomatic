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

"""Configuration schema and loading for `[tool.repomatic]` in `pyproject.toml`.

Defines the `Config` dataclass, its TOML serialization helpers, and the
`load_repomatic_config` function that reads, validates, and returns a typed
`Config` instance.
"""

from __future__ import annotations

import ast
import inspect
import logging
import sys
import textwrap
from dataclasses import dataclass, field, fields
from pathlib import Path
from textwrap import dedent

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore[import-not-found]

TYPE_CHECKING = False
if TYPE_CHECKING:
    from typing import Any, Final


@dataclass
class CacheConfig:
    """Nested schema for `[tool.repomatic.cache]`."""

    dir: str = ""
    """Override the binary cache directory path.

    When empty (the default), the cache uses the platform convention:
    `~/Library/Caches/repomatic` on macOS, `$XDG_CACHE_HOME/repomatic`
    or `~/.cache/repomatic` on Linux, `%LOCALAPPDATA%\\repomatic\\Cache`
    on Windows. The `REPOMATIC_CACHE_DIR` environment variable takes
    precedence over this setting.
    """

    github_release_ttl: int = 604800
    """Freshness TTL for cached single-release bodies (seconds).

    GitHub release bodies are immutable once published, so a long TTL (7 days)
    is safe. Set to `0` to disable caching for single-release lookups.
    """

    github_releases_ttl: int = 86400
    """Freshness TTL for cached all-releases responses (seconds).

    New releases can appear at any time, so a shorter TTL (24 hours) balances
    freshness with API savings.
    """

    max_age: int = 30
    """Auto-purge cached entries older than this many days.

    Set to `0` to disable auto-purge. The `REPOMATIC_CACHE_MAX_AGE`
    environment variable takes precedence over this setting.
    """

    pypi_ttl: int = 86400
    """Freshness TTL for cached PyPI metadata (seconds).

    PyPI metadata changes when new versions are published. A 24-hour TTL
    avoids redundant API calls while keeping data reasonably current.
    """


@dataclass
class DependencyGraphConfig:
    """Nested schema for `[tool.repomatic.dependency-graph]`."""

    all_extras: bool = True
    """Whether to include all optional extras in the graph.

    When `True`, the `update-deps-graph` command behaves as if
    `--all-extras` was passed.
    """

    all_groups: bool = True
    """Whether to include all dependency groups in the graph.

    When `True`, the `update-deps-graph` command behaves as if
    `--all-groups` was passed. Projects that want to exclude development
    dependency groups (docs, test, typing) from their published graph can
    set this to `false`.
    """

    level: int | None = None
    """Maximum depth of the dependency graph.

    `None` means unlimited. `1` = primary deps only, `2` = primary +
    their deps, etc. Equivalent to `--level`.
    """

    no_extras: list[str] = field(default_factory=list)
    """Optional extras to exclude from the graph.

    Equivalent to passing `--no-extra` for each entry. Takes precedence
    over `dependency-graph.all-extras`.
    """

    no_groups: list[str] = field(default_factory=list)
    """Dependency groups to exclude from the graph.

    Equivalent to passing `--no-group` for each entry. Takes precedence
    over `dependency-graph.all-groups`.
    """

    output: str = "./docs/assets/dependencies.mmd"
    """Path where the dependency graph Mermaid diagram should be written.

    The dependency graph visualizes the project's dependency tree in Mermaid format.
    """


@dataclass
class DocsConfig:
    """Nested schema for `[tool.repomatic.docs]`."""

    apidoc_exclude: list[str] = field(default_factory=list)
    """Glob patterns for modules to exclude from `sphinx-apidoc`.

    Passed as positional exclude arguments after the source directory
    (e.g., `["setup.py", "tests"]`).
    """

    apidoc_extra_args: list[str] = field(default_factory=list)
    """Extra arguments appended to the `sphinx-apidoc` invocation.

    The base flags `--no-toc --module-first` are always applied.
    Use this for project-specific options (e.g., `["--implicit-namespaces"]`).
    """

    update_script: str = "./docs/docs_update.py"
    """Path to a Python script run after `sphinx-apidoc` to generate dynamic content.

    Resolved relative to the repository root. Must reside under the `docs/`
    directory for security. Set to an empty string to disable.
    """


@dataclass
class GitignoreConfig:
    """Nested schema for `[tool.repomatic.gitignore]`."""

    extra_categories: list[str] = field(default_factory=list)
    """Additional gitignore template categories to fetch from gitignore.io.

    List of template names (e.g., `["Python", "Node", "Terraform"]`) to combine
    with the generated `.gitignore` content.
    """

    extra_content: str = field(
        default_factory=lambda: dedent(
            """
            junit.xml

            # Claude Code local files.
            .claude/scheduled_tasks.lock
            .claude/settings.local.json
            """
        ).strip()
    )
    """Additional content to append at the end of the generated `.gitignore` file.
    """

    location: str = "./.gitignore"
    """File path of the `.gitignore` to update, relative to the root of the repository.
    """

    sync: bool = True
    """Whether `.gitignore` sync is enabled for this project.

    Projects that manage their own `.gitignore` and do not want the autofix job
    to overwrite it can set this to `false`.
    """


@dataclass
class LabelsConfig:
    """Nested schema for `[tool.repomatic.labels]`."""

    extra_content_rules: str = ""
    """Additional YAML rules appended to the content-based labeller configuration.

    Appended to the bundled `labeller-content-based.yaml` during export.
    """

    extra_file_rules: str = ""
    """Additional YAML rules appended to the file-based labeller configuration.

    Appended to the bundled `labeller-file-based.yaml` during export.
    """

    extra_files: list[str] = field(default_factory=list)
    """URLs of additional label definition files (JSON, JSON5, TOML, or YAML).

    Each URL is downloaded and applied separately by `labelmaker`.
    """

    sync: bool = True
    """Whether label sync is enabled for this project.

    Projects that manage their own repository labels and do not want the
    labels workflow to overwrite them can set this to `false`.
    """


@dataclass
class TestMatrixConfig:
    """Nested schema for `[tool.repomatic.test-matrix]`.

    Keys inside `replace` and `variations` are GitHub Actions matrix
    identifiers (e.g., `os`, `python-version`) and must not be
    normalized to snake_case. Click Extra's
    `click_extra.normalize_keys = False` metadata on the parent field
    prevents this.
    """

    exclude: list[dict[str, str]] = field(default_factory=list)
    """Extra exclude rules applied to both full and PR test matrices.

    Each entry is a dict of GitHub Actions matrix keys (like
    `{"os": "windows-11-arm"}`) that removes matching combinations.
    Additive to the upstream default excludes.
    """

    include: list[dict[str, str]] = field(default_factory=list)
    """Extra include directives applied to both full and PR test matrices.

    Each entry is a dict of GitHub Actions matrix keys that adds or augments
    matrix combinations. Additive to the upstream default includes.

    Because includes apply to both matrices, a directive whose keys are not PR
    base axes is risky. In the PR matrix only `os` and `python-version` are base
    axes, so a key like `click-version` (injected by another include) has
    nothing to match and GitHub's expansion adds the directive to every PR job,
    overwriting it. To flag a value continue-on-error, prefer `unstable` over an
    `include` carrying `state: unstable`.
    """

    remove: dict[str, list[str]] = field(default_factory=dict)
    """Per-axis value removals applied to both full and PR test matrices.

    Outer key is the variation/axis ID (e.g., `os`, `python-version`).
    Inner list contains values to drop from that axis. Applied after
    replacements but before excludes, includes, and variations.
    """

    replace: dict[str, dict[str, str]] = field(default_factory=dict)
    """Per-axis value replacements applied to both full and PR test matrices.

    Outer key is the variation/axis ID (e.g., `os`, `python-version`).
    Inner dict maps old values to new values. Applied before removals,
    excludes, includes, and variations.
    """

    unstable: list[dict[str, str]] = field(default_factory=list)
    """Full-matrix-only combinations to flag continue-on-error in CI.

    Each entry is a dict of GitHub Actions matrix keys (like
    `{"click-version": "main"}`). Every full-matrix combination matching an
    entry gets a `state: unstable` value, which `tests.yaml` reads to set
    `continue-on-error`. Like `variations`, this applies to the full matrix
    only; the PR matrix stays a curated stable set.

    Prefer this over an `include` entry carrying `state: unstable`. `include`
    applies to both matrices, and in the PR matrix a key like `click-version`
    is not a base axis (another `include` injects it), so GitHub's expansion
    would add the directive to every PR job and overwrite it. `unstable` only
    touches the full matrix, sidestepping that hijack.
    """

    variations: dict[str, list[str]] = field(default_factory=dict)
    """Extra matrix dimension values added to the full test matrix only.

    Each key is a dimension ID (e.g., `os`, `click-version`) and its value
    is a list of additional entries. For existing dimensions, values are merged
    with the upstream defaults. For new dimension IDs, a new axis is created.
    Only affects the full matrix; the PR matrix stays a curated reduced set.
    """


@dataclass
class TestPlanConfig:
    """Nested schema for `[tool.repomatic.test-plan]`."""

    file: str = "./tests/cli-test-plan.yaml"
    """Path to the YAML test plan file for binary testing.

    The test plan file defines a list of test cases to run against compiled binaries.
    Each test case specifies command-line arguments and expected output patterns.
    """

    inline: str | None = None
    """Inline YAML test plan for binaries.

    Alternative to `test_plan_file`. Allows specifying the test plan directly in
    `pyproject.toml` instead of a separate file.
    """

    timeout: int | None = None
    """Timeout in seconds for each binary test.

    If set, each test command will be terminated after this duration. `None` means no
    timeout (tests can run indefinitely).
    """


@dataclass
class VulnerableDepsConfig:
    """Nested schema for `[tool.repomatic.vulnerable-deps]`."""

    sources: list[str] = field(
        default_factory=lambda: ["uv-audit", "github-advisories"],
    )
    """Advisory databases to consult for known vulnerabilities.

    Recognized values:

    - `"uv-audit"`: PyPA Advisory Database via `uv audit` (works locally
      and in CI without a GitHub token).
    - `"github-advisories"`: GitHub Advisory Database via the
      repository's Dependabot alerts (CI-only, requires a token with
      `Dependabot alerts: Read-only`).

    Sources are unioned and deduplicated by `(package, advisory_id)`.
    Repositories that distrust GHSA — or have no Dependabot alerts
    enabled — can opt out with `sources = ["uv-audit"]`.
    """

    sync: bool = True
    """Whether the `fix-vulnerable-deps` job is enabled for this project.

    Projects that manage their own vulnerability remediation flow can set
    this to `false` to skip the autofix job.
    """


@dataclass
class WorkflowConfig:
    """Nested schema for `[tool.repomatic.workflow]`."""

    source_paths: list[str] | None = None
    """Source code directory names for workflow trigger `paths:` filters.

    When set, thin-caller and header-only workflows include `paths:` filters
    using these directory names (as `name/**` globs) alongside universal paths
    like `pyproject.toml` and `uv.lock`.

    When `None` (default), source paths are auto-derived from
    `[project.name]` in `pyproject.toml` by replacing hyphens with
    underscores — the universal Python convention. For example,
    `name = "extra-platforms"` automatically uses `["extra_platforms"]`.
    """

    extra_paths: list[str] = field(default_factory=list)
    """Literal entries to append to every workflow's `paths:` filter.

    Applies to thin-caller and header-only sync. Useful for repo-specific
    files that should re-trigger CI but are not detected by the canonical
    `paths:` filter (e.g., `install.sh`, `dotfiles/**`).

    Per-workflow overrides in `paths` ignore this list: when an entry exists
    for a given filename, that entry is treated as the complete list.
    """

    ignore_paths: list[str] = field(default_factory=list)
    """Literal entries to strip from every workflow's `paths:` filter.

    Useful for canonical entries that don't exist downstream (e.g.,
    `tests/**`, `uv.lock` in repos with no Python tests or lockfile).
    Match is by exact string equality. Applies before `extra_paths`.

    Per-workflow overrides in `paths` ignore this list.
    """

    paths: dict[str, list[str]] = field(default_factory=dict)
    """Per-workflow override of the `paths:` filter, keyed by filename.

    When a workflow filename appears here, its `paths:` blocks (in `push`,
    `pull_request`, etc.) are replaced wholesale with the listed entries.
    `source_paths`, `extra_paths`, and `ignore_paths` do **not** apply when
    a per-workflow override is set: the list is treated as authoritative.

    Override only takes effect on triggers that already have a `paths:`
    filter in the canonical workflow. Workflows without `paths:` upstream
    keep their unrestricted trigger semantics.

    Example:

    ```toml
    [tool.repomatic.workflow.paths]
    "tests.yaml" = ["install.sh", "packages.toml", ".github/workflows/tests.yaml"]
    ```
    """

    sync: bool = True
    """Whether workflow sync is enabled for this project.

    Projects that manage their own workflow files and do not want the autofix job
    to sync thin callers or headers can set this to `false`.
    """


@dataclass
class Config:
    """Configuration schema for `[tool.repomatic]` in `pyproject.toml`.

    This dataclass defines the structure and default values for repomatic configuration.
    Each field has a docstring explaining its purpose.
    """

    awesome_template_sync: bool = field(
        default=True,
        metadata={"click_extra.config_path": "awesome-template.sync"},
    )
    """Whether awesome-template sync is enabled for this project.

    Repositories whose name starts with `awesome-` get their boilerplate synced
    from files bundled in `repomatic`. Set to `false` to opt out.
    """

    bumpversion_sync: bool = field(
        default=True,
        metadata={"click_extra.config_path": "bumpversion.sync"},
    )
    """Whether bumpversion config sync is enabled for this project.

    Projects that manage their own `[tool.bumpversion]` section and do not want
    the autofix job to overwrite it can set this to `false`.
    """

    cache: CacheConfig = field(
        default_factory=CacheConfig,
        metadata={"click_extra.config_path": "cache"},
    )
    """Binary cache configuration."""

    changelog_location: str = field(
        default="./changelog.md",
        metadata={"click_extra.config_path": "changelog.location"},
    )
    """File path of the changelog, relative to the root of the repository."""

    dependency_graph: DependencyGraphConfig = field(
        default_factory=DependencyGraphConfig,
        metadata={"click_extra.config_path": "dependency-graph"},
    )
    """Dependency graph generation configuration."""

    dev_release_sync: bool = field(
        default=True,
        metadata={"click_extra.config_path": "dev-release.sync"},
    )
    """Whether dev pre-release sync is enabled for this project.

    Projects that do not want a rolling draft pre-release maintained on
    GitHub can set this to `false`.
    """

    docs: DocsConfig = field(
        default_factory=DocsConfig,
        metadata={"click_extra.config_path": "docs"},
    )
    """Sphinx documentation generation configuration."""

    exclude: list[str] = field(default_factory=list)
    """Additional components and files to exclude from repomatic operations.

    Additive to the default exclusions (`labels`, `skills`). Bare names
    exclude an entire component (e.g., `"workflows"`). Qualified
    `component/identifier` entries exclude a specific file within a component
    (e.g., `"workflows/debug.yaml"`, `"skills/repomatic-audit"`,
    `"labels/labeller-content-based.yaml"`).

    Affects `repomatic init`, `workflow sync`, and `workflow create`.
    Explicit CLI positional arguments override this list.
    """

    gitignore: GitignoreConfig = field(
        default_factory=GitignoreConfig,
        metadata={"click_extra.config_path": "gitignore"},
    )
    """`.gitignore` sync configuration."""

    include: list[str] = field(default_factory=list)
    """Components and files to force-include, overriding default exclusions.

    Use this to opt into components that are excluded by default (`labels`,
    `skills`). Each entry is subtracted from the effective exclude set
    (defaults + user `exclude`) and bypasses `RepoScope` filtering, so
    scope-restricted components (like awesome-only skills or Python-only
    `publish-pypi-action`) are included regardless of repository type.
    Qualified entries (`component/file`) implicitly select the parent
    component. Same syntax as `exclude`.
    """

    labels: LabelsConfig = field(
        default_factory=LabelsConfig,
        metadata={"click_extra.config_path": "labels"},
    )
    """Repository label sync configuration."""

    mailmap_sync: bool = field(
        default=True,
        metadata={"click_extra.config_path": "mailmap.sync"},
    )
    """Whether `.mailmap` sync is enabled for this project.

    Projects that manage their own `.mailmap` and do not want the autofix job
    to overwrite it can set this to `false`.
    """

    notification_unsubscribe: bool = field(
        default=False,
        metadata={"click_extra.config_path": "notification.unsubscribe"},
    )
    """Whether the unsubscribe-threads workflow is enabled.

    Notifications are per-user across all repos. Enable on the single repo where
    you want scheduled cleanup of closed notification threads. Requires a classic
    PAT with `notifications` scope stored as `REPOMATIC_NOTIFICATIONS_PAT`.
    """

    nuitka_enabled: bool = field(
        default=True,
        metadata={"click_extra.config_path": "nuitka.enabled"},
    )
    """Whether Nuitka binary compilation is enabled for this project.

    Projects with `[project.scripts]` entries that are not intended to produce
    standalone binaries (e.g., libraries with convenience CLI wrappers) can set this
    to `false` to opt out of Nuitka compilation.
    """

    nuitka_entry_points: list[str] = field(
        default_factory=list,
        metadata={"click_extra.config_path": "nuitka.entry-points"},
    )
    """Which `[project.scripts]` entry points produce Nuitka binaries.

    List of CLI IDs (e.g., `["mpm"]`) to compile. When empty (the default),
    deduplicates by callable target: keeps the first entry point for each
    unique `module:callable` pair. This avoids building duplicate binaries
    when a project declares alias entry points (like both `mpm` and
    `meta-package-manager` pointing to the same function).
    """

    nuitka_extra_args: list[str] = field(
        default_factory=list,
        metadata={"click_extra.config_path": "nuitka.extra-args"},
    )
    """Extra Nuitka CLI arguments for binary compilation.

    Project-specific flags (e.g., `--include-data-files`,
    `--include-package-data`) that are passed to the Nuitka build command.
    """

    nuitka_unstable_targets: list[str] = field(
        default_factory=list,
        metadata={"click_extra.config_path": "nuitka.unstable-targets"},
    )
    """Nuitka build targets allowed to fail without blocking the release.

    List of target names (e.g., `["linux-arm64", "windows-x64"]`) that are marked as
    unstable. Jobs for these targets will be allowed to fail without preventing the
    release workflow from succeeding.
    """

    pypi_package_history: list[str] = field(default_factory=list)
    """Former PyPI package names for projects that were renamed.

    When a project changes its PyPI name, older versions remain published under
    the previous name. List former names here so `lint-changelog` can fetch
    release metadata from all names and generate correct PyPI URLs.
    """

    setup_guide: bool = True
    """Whether the setup guide issue is enabled for this project.

    Projects that do not need `REPOMATIC_PAT` or manage their
    own PAT setup can set this to `false` to suppress the setup guide issue.
    """

    agents_location: str = field(
        default="./.claude/agents/",
        metadata={"click_extra.config_path": "agents.location"},
    )
    """Directory prefix for Claude Code agent files, relative to the repository root.

    Agent files are written as `{agents_location}/{agent-id}.md`.
    Useful for repositories where `.claude/` is not at the root (like
    dotfiles repos that store configs under a subdirectory).
    """

    skills_location: str = field(
        default="./.claude/skills/",
        metadata={"click_extra.config_path": "skills.location"},
    )
    """Directory prefix for Claude Code skill files, relative to the repository root.

    Skill files are written as `{skills_location}/{skill-id}/SKILL.md`.
    Useful for repositories where `.claude/` is not at the root (like
    dotfiles repos that store configs under a subdirectory).
    """

    test_matrix: TestMatrixConfig = field(
        default_factory=TestMatrixConfig,
        metadata={
            "click_extra.config_path": "test-matrix",
            "click_extra.normalize_keys": False,
        },
    )
    """Per-project customizations for the GitHub Actions CI test matrix.

    Keys inside this section are GitHub Actions matrix identifiers (e.g.,
    `os`, `python-version`) and must not be normalized to snake_case.
    """

    test_plan: TestPlanConfig = field(
        default_factory=TestPlanConfig,
        metadata={"click_extra.config_path": "test-plan"},
    )
    """Binary test plan configuration."""

    uv_lock_sync: bool = field(
        default=True,
        metadata={"click_extra.config_path": "uv-lock.sync"},
    )
    """Whether `uv.lock` sync is enabled for this project.

    Projects that manage their own lock file strategy and do not want the
    `sync-uv-lock` job to run `uv lock --upgrade` can set this to `false`.
    """

    vulnerable_deps: VulnerableDepsConfig = field(
        default_factory=lambda: VulnerableDepsConfig(),
        metadata={"click_extra.config_path": "vulnerable-deps"},
    )
    """Vulnerable dependency detection and remediation configuration."""

    workflow: WorkflowConfig = field(
        default_factory=WorkflowConfig,
        metadata={"click_extra.config_path": "workflow"},
    )
    """Workflow sync configuration."""


SUBCOMMAND_CONFIG_FIELDS: Final[frozenset[str]] = frozenset((
    "agents_location",
    "awesome_template_sync",
    "bumpversion_sync",
    "cache",
    "changelog_location",
    "dependency_graph",
    "dev_release_sync",
    "docs",
    "exclude",
    "gitignore",
    "include",
    "labels",
    "mailmap_sync",
    "notification_unsubscribe",
    "pypi_package_history",
    "setup_guide",
    "skills_location",
    "test_matrix",
    "test_plan",
    "uv_lock_sync",
    "vulnerable_deps",
    "workflow",
))
"""Config fields consumed directly by subcommands, not needed as metadata outputs.

The `test-plan` and `deps-graph` subcommands now read these values directly from
`[tool.repomatic]` in `pyproject.toml`, so they no longer need to be passed through
workflow metadata outputs.
"""


def _field_to_key(name: str, cls: type | None = None) -> str:
    """Convert a dataclass field name to its TOML config key.

    For fields with `click_extra.config_path` metadata, returns that path
    directly. Otherwise, falls back to simple kebab-case conversion
    (e.g., `setup_guide` → `setup-guide`).

    :param cls: Dataclass to inspect for metadata. Defaults to `Config`.
    """
    if cls is None:
        cls = Config
    for f in fields(cls):
        if f.name == name:
            path = f.metadata.get("click_extra.config_path")
            if path:
                return str(path)
            break
    return name.replace("_", "-")


def _extract_field_docstrings(
    cls: type | None = None,
    *,
    full: bool = False,
) -> dict[str, str]:
    """Extract attribute docstrings from a dataclass via AST.

    Attribute docstrings are string literals immediately following an annotated
    assignment in a class body (PEP 257 convention). By default, returns a
    mapping of field name to the first paragraph of its docstring (stripped,
    dedented, single-spaced). When `full=True`, returns the entire docstring
    with paragraph breaks preserved.

    :param cls: Dataclass to inspect. Defaults to `Config`.
    :param full: When `True`, keep the complete docstring. When `False`,
        keep only the first paragraph collapsed onto a single line.
    """
    if cls is None:
        cls = Config
    source = inspect.getsource(cls)
    tree = ast.parse(textwrap.dedent(source))
    cls_node = tree.body[0]
    assert isinstance(cls_node, ast.ClassDef)

    docstrings: dict[str, str] = {}
    current_field = None
    for node in cls_node.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            current_field = node.target.id
        elif (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            if current_field:
                # cleandoc trims the first line and dedents the rest based on
                # the common indent of subsequent lines (PEP 257), unlike
                # textwrap.dedent which fails when the first line has no
                # leading whitespace but subsequent lines do.
                text = inspect.cleandoc(node.value.value)
                if full:
                    docstrings[current_field] = text
                else:
                    # Collapse the first paragraph onto a single line.
                    docstrings[current_field] = " ".join(text.split("\n\n")[0].split())
                current_field = None
        else:
            current_field = None

    return docstrings


def _format_default(value: object) -> str:
    """Format a `Config` field default for the reference table."""
    if value is None:
        return "*(none)*"
    if isinstance(value, bool):
        return f"`{str(value).lower()}`"
    if isinstance(value, int):
        return f"`{value}`"
    if isinstance(value, str):
        if "\n" in value:
            return "*(see example)*"
        return f'`"{value}"`'
    if isinstance(value, list):
        if not value:
            return "`[]`"
        return f"`{value!r}`"
    return str(value)


def _format_type(annotation: str) -> str:
    """Simplify a type annotation string for the reference table.

    Strips `| None` suffixes since the default column already shows whether
    `None` is the default.
    """
    return annotation.replace(" | None", "")


def escape_type_for_gfm_table(ftype: str) -> str:
    """Escape outer brackets of nested generics for raw GFM table cells.

    Nested generics like `list[dict[str, str]]` would otherwise be
    interpreted by mdformat as a markdown link reference and re-escaped on
    every reformat. Escaping the outermost brackets up front keeps the
    cell stable under mdformat. Simple generics like `list[str]` have no
    nested brackets and stay unescaped.

    Apply this only when the value lands directly in a raw GFM table cell
    (e.g. CLI `show-config` output). Do not apply when wrapping the value
    in inline code backticks: inside a code span, backslashes are literal
    characters in CommonMark and would render visibly as `\\[`.
    """
    if "[" in ftype:
        first = ftype.index("[")
        last = ftype.rindex("]")
        inner = ftype[first + 1 : last]
        if "[" in inner:
            return ftype[:first] + "\\[" + inner + "\\]" + ftype[last + 1 :]
    return ftype


CONFIG_REFERENCE_HEADER_DEFS: tuple[tuple[str, str], ...] = (
    ("Option", "option"),
    ("Type", "type"),
    ("Default", "default"),
    ("Description", "description"),
)
"""Column definitions for the `[tool.repomatic]` configuration reference table."""


def config_full_descriptions() -> dict[str, str]:
    """Return full attribute docstrings keyed by TOML option name.

    Unlike `config_reference()` which truncates to a one-line summary
    suitable for the `show-config` CLI table, this returns the entire
    docstring for use in long-form documentation.
    """
    schema = Config()
    descriptions: dict[str, str] = {}
    for f in fields(Config):
        sub = getattr(schema, f.name)
        if hasattr(sub, "__dataclass_fields__"):
            prefix = _field_to_key(f.name)
            sub_cls = type(sub)
            sub_docs = _extract_field_docstrings(sub_cls, full=True)
            for sf in fields(sub_cls):
                key = f"{prefix}.{sf.name.replace('_', '-')}"
                descriptions[key] = sub_docs.get(sf.name, "")
        else:
            key = _field_to_key(f.name)
            full_docs = _extract_field_docstrings(full=True)
            descriptions[key] = full_docs.get(f.name, "")
    return descriptions


def config_reference() -> list[tuple[str, str, str, str]]:
    """Build the `[tool.repomatic]` configuration reference as table rows.

    Introspects the `Config` dataclass fields, their type annotations,
    defaults, and attribute docstrings. Nested dataclass fields are expanded
    into individual rows with dotted keys. Returns a list of
    `(option, type, default, description)` tuples suitable for
    `click_extra.table.print_table`.
    """
    schema = Config()
    docstrings = _extract_field_docstrings()
    sorted_fields = sorted(fields(Config), key=lambda f: _field_to_key(f.name))

    rows = []
    for f in sorted_fields:
        sub_type = getattr(schema, f.name)
        if hasattr(sub_type, "__dataclass_fields__"):
            # Expand nested dataclass into individual rows.
            prefix = _field_to_key(f.name)
            sub_cls = type(sub_type)
            sub_docstrings = _extract_field_docstrings(sub_cls)
            for sf in sorted(fields(sub_cls), key=lambda sf: sf.name):
                key = f"`{prefix}.{sf.name.replace('_', '-')}`"
                ftype = _format_type(sub_cls.__annotations__[sf.name])
                default = _format_default(getattr(sub_type, sf.name))
                desc = sub_docstrings.get(sf.name, "")
                rows.append((key, ftype, default, desc))
        else:
            key = f"`{_field_to_key(f.name)}`"
            ftype = _format_type(Config.__annotations__[f.name])
            default = _format_default(getattr(schema, f.name))
            desc = docstrings.get(f.name, "")
            rows.append((key, ftype, default, desc))

    return rows


def load_repomatic_config(
    pyproject_data: dict[str, Any] | None = None,
) -> Config:
    """Load `[tool.repomatic]` config merged with `Config` defaults.

    Delegates to click-extra's schema-aware dataclass instantiation, which
    handles normalization, flattening, nested dataclasses, and opaque field
    extraction automatically based on field metadata and type hints.

    :param pyproject_data: Pre-parsed `pyproject.toml` dict. If `None`,
        reads and parses `pyproject.toml` from the current working directory.
    """
    from click_extra.config import _make_schema_callable

    if pyproject_data is None:
        pyproject_path = Path() / "pyproject.toml"
        if pyproject_path.exists() and pyproject_path.is_file():
            pyproject_data = tomllib.loads(pyproject_path.read_text(encoding="UTF-8"))
        else:
            pyproject_data = {}

    tool_section = pyproject_data.get("tool", {})
    user_config: dict[str, Any] = tool_section.get("repomatic", {})

    # Warn about unknown top-level keys before loading. Collect all known
    # TOML key prefixes (e.g., "test-matrix", "nuitka", "workflow") so we
    # can flag unrecognized entries without crashing.
    known_keys = {_field_to_key(f.name).split(".")[0] for f in fields(Config)}
    for key in user_config:
        if key.replace("_", "-") not in known_keys:
            logging.warning(
                "Unknown [tool.repomatic] option: %s (ignored).",
                key,
            )

    schema_callable = _make_schema_callable(Config, strict=False)
    assert schema_callable is not None
    config: Config = schema_callable(user_config)
    return config
