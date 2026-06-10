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

"""Bundled data files, configuration templates, and repository initialization.

Provides a unified interface for accessing bundled data files from
`repomatic/data/` and orchestrates repository bootstrapping via
`repomatic init`.

Available components (`repomatic init <component>`):

- `workflows` - Thin-caller workflow files
- `labels` - Label definitions (labels.toml + labeller rules)
- `renovate` - Renovate dependency update configuration (renovate.json5)
- `changelog` - Minimal changelog.md
- `ruff` - Merges `[tool.ruff]` into pyproject.toml
- `pytest` - Merges `[tool.pytest]` into pyproject.toml
- `mypy` - Merges `[tool.mypy]` into pyproject.toml
- `bumpversion` - Merges `[tool.bumpversion]` into pyproject.toml
- `agents` - Claude Code agent definitions (.claude/agents/)
- `skills` - Claude Code skill definitions (.claude/skills/)
- `awesome-template` - Boilerplate for `awesome-*` repositories

Selectors use the same `component[/file]` syntax as the `exclude`
config option in `[tool.repomatic]`.  Qualified entries like
`skills/repomatic-topics` select a single file within a component.
"""

from __future__ import annotations

import hashlib
import logging
import re
import sys
from dataclasses import dataclass, field
from importlib.resources import as_file, files
from pathlib import Path, PurePosixPath
from urllib.request import urlretrieve

import tomlrt
from tomlrt import Table

from . import __version__
from .config import Config, load_repomatic_config
from .pyproject import is_python_project, resolve_source_paths
from .registry import (
    _BY_NAME,
    COMPONENTS,
    DEFAULT_REPO,
    REMOVED_ASSETS,
    REUSABLE_WORKFLOWS,
    UPSTREAM_REPO_SLUGS,
    BundledComponent,
    GeneratedComponent,
    InitDefault,
    SyncMode,
    TemplateComponent,
    ToolConfigComponent,
    WorkflowComponent,
    excluded_rel_path,
    parse_component_entries,
)
from .tool_runner import TOOL_REGISTRY, find_unmodified_configs

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore[import-not-found]

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from typing import Any

    if sys.version_info >= (3, 11):
        from importlib.resources.abc import Traversable
    else:
        from importlib.abc import Traversable


RUNTIME_FRAGMENTS: tuple[str, ...] = ("release.yaml",)
"""Bundled YAML files loaded by `repomatic` at runtime, not deployed verbatim.

These files live in `repomatic/data/` so they ship in the wheel and are
discoverable via {func}`get_data_content`, but `repomatic init` never copies them
as-is. `release.yaml` is the canonical caller `repomatic.github.workflow_sync`
reads to assemble each downstream `release.yaml`, copying its jobs and rewriting
the local `uses:` refs (see `_generate_release_caller`); the deployed
`release.yaml` is generated, not this bundled copy. New entries must be added
explicitly so the data-file registry tests stay authoritative.
"""


# Exportable files: all registry entries + tool runner bundled defaults.
EXPORTABLE_FILES: dict[str, str | None] = {
    **{f.source: f.target for c in COMPONENTS for f in c.files},
    **{c.source_file: None for c in COMPONENTS if isinstance(c, ToolConfigComponent)},
    # Standalone linter configs from the tool runner (yamllint, zizmor).
    # These are bundled defaults used at runtime, not init components.
    **{
        spec.default_config: None
        for spec in TOOL_REGISTRY.values()
        if spec.default_config
    },
    # Internal-use YAML fragments loaded by the package at runtime.
    **{name: None for name in RUNTIME_FRAGMENTS},
}
"""Registry of all exportable files: maps filename to default output path.

`None` means the file is bundled but not directly written to a target path
by `repomatic init` (used for `pyproject.toml` templates that need merging,
tool-runner default configs, and runtime fragments).
"""


# ---------------------------------------------------------------------------
# Bundled data access
# ---------------------------------------------------------------------------


def get_data_content(filename: str) -> str:
    """Get the content of a bundled data file.

    This is the low-level function for reading any file from `repomatic/data/`.

    :param filename: Name of the file to retrieve (e.g., "labels.toml").
    :return: Content of the file as a string.
    :raises FileNotFoundError: If the file doesn't exist.
    """
    data_files = files("repomatic.data")
    with as_file(data_files.joinpath(filename)) as path:
        if path.exists():
            return path.read_text(encoding="UTF-8")

    msg = f"Data file not found: {filename}"
    raise FileNotFoundError(msg)


def _strip_renovate_repo_settings(content: str) -> str:
    """Strip upstream repo-specific settings from a Renovate config.

    Removes `assignees` (repo-specific) and the self-referencing uv
    `customManagers` entry that targets `renovate.json5` itself.

    ```{note}
    The self-referencing uv `customManagers` entry is excluded because it
    creates an endless update loop in downstream repos: Renovate bumps the
    pinned uv version, the merged PR triggers `repomatic init`, which
    overwrites `renovate.json5` back to the bundled template (reverting
    the bump), and Renovate opens the same PR again — indefinitely. All
    other `customManagers` entries are included since they target workflow
    files, not `renovate.json5` itself.
    ```

    :param content: Raw Renovate config content.
    :return: The config with repo-specific settings removed.
    """
    # Remove assignees line (repo-specific).
    content = re.sub(r"\s*assignees:\s*\[[^\]]*\],?\n", "\n", content)

    # Remove the self-referencing uv customManagers entry (identified by its
    # unique description). This entry targets renovate.json5 itself and causes
    # an endless update loop when synced to downstream repos.
    return re.sub(
        r'\n    \{\n      description: "Update uv version in postUpgradeTasks'
        r' download URL\.",\n.*?\n    \},',
        "",
        content,
        flags=re.DOTALL,
    )


def _get_renovate_config() -> str:
    """Get Renovate config, with repo-specific settings stripped.

    When running from the source repository (via `uv run`), reads the root
    `renovate.json5` and strips repo-specific settings. When installed as a
    package (via `uvx`), falls back to the pre-processed bundled file in
    `repomatic/data/renovate.json5`.

    :return: The clean Renovate configuration content.
    """
    root_path = Path(__file__).parent.parent / "renovate.json5"

    # When installed as a package, the root file won't exist.
    # Fall back to the bundled pre-processed version.
    if not root_path.exists():
        return get_data_content("renovate.json5")

    return _strip_renovate_repo_settings(
        root_path.read_text(encoding="UTF-8"),
    )


def export_content(filename: str) -> str:
    """Get the content of any exportable bundled file.

    :param filename: The filename (e.g., "ruff.toml", "labels.toml", "release.yaml").
    :return: Content of the file as a string.
    :raises ValueError: If the file is not in the registry.
    :raises FileNotFoundError: If the file doesn't exist.
    """
    if filename not in EXPORTABLE_FILES:
        supported = ", ".join(EXPORTABLE_FILES.keys())
        msg = f"Unknown file: {filename!r}. Supported: {supported}"
        raise ValueError(msg)

    # Special handling for renovate.json5: read from root and strip
    # repo-specific settings.
    if filename == "renovate.json5":
        return _get_renovate_config()

    return get_data_content(filename)


# ---------------------------------------------------------------------------
# pyproject.toml config merging
# ---------------------------------------------------------------------------


def _template_to_table(template_text: str) -> Table:
    """Parse native-format template text into a tomlrt section table.

    `Table.section` copies the parsed mapping's values into a fresh, detached
    table. Trivia attached to nested table values carries over, but top-level
    standalone comments and inline trivia are stored on the source document, so
    they are not preserved. The returned table carries no `[tool.X]` header; it
    gets one only when assigned into a document.

    :param template_text: Template content in native format (no `[tool.X]`
        prefix), with file-level header comments already stripped.
    :return: A tomlrt section table suitable for assignment into a document.
    """
    return Table.section(tomlrt.loads(template_text))


# Sentinel marking a key absent from the template, so a legitimate template
# value of `None` is not mistaken for "missing" during the merge walk.
_MISSING = object()


def _entry_identity(
    entry: Mapping[str, Any], identity_keys: tuple[str, ...]
) -> tuple[tuple[str, Any], ...]:
    """Return the slot identity of an array-of-tables entry.

    The identity is the tuple of `(key, value)` pairs for the `identity_keys`
    the entry actually carries, so two entries map to the same slot when they
    agree on every identity key they share. See
    {attr}`~repomatic.registry.ToolConfigComponent.graft_identity_keys`.
    """
    return tuple((key, entry[key]) for key in identity_keys if key in entry)


def _graft_local_additions(
    target: Any,
    template: Mapping[str, Any],
    existing: Mapping[str, Any],
    identity_keys: tuple[str, ...] = (),
) -> None:
    """Graft local-only content from an existing section onto a template table.

    Walks the *existing* tomlrt section against the *template* (a plain-dict
    view) and copies into *target* (the tomlrt table built from the template)
    every piece of local configuration the canonical template does not already
    carry:

    - **Keys the template does not define** are grafted verbatim, so a
      project-specific table like `[tool.typos.files]` survives untouched.
    - **Tables present in both** are merged recursively, so local keys inside a
      table the template also defines are kept (e.g. a downstream entry in
      `[tool.typos.default.extend-identifiers]` next to the canonical ones).
    - **Arrays present in both** gain their local-only items appended after the
      template items. Scalar arrays (`extend-ignore-re`, `serialize`) and
      arrays-of-tables with no `identity_keys` use a plain union by value.
      When `identity_keys` is set, an array-of-tables entry that shares a slot
      identity with a template entry (`[[tool.bumpversion.files]]` whose
      canonical `search` has evolved) is dropped in favour of the template
      instead of appended as a duplicate.
    - **Scalars present in both** are left as the template defines them: the
      canonical value wins, which is the point of an ongoing sync.

    Grafted nodes are copied from *existing*, so comments and inline
    formatting on local additions carry over.

    :param target: tomlrt table built from the bundled template, mutated in
        place.
    :param template: Plain-dict view of the bundled template section.
    :param existing: tomlrt view of the local section being synced, walked for
        structure and used as the source of grafted nodes.
    :param identity_keys: Keys identifying an array-of-tables entry's slot. When
        set, a local entry sharing a slot with a template entry is dropped in
        favour of the template rather than appended as a duplicate.
    """
    for key, existing_value in existing.items():
        template_value = template.get(key, _MISSING)
        if template_value is _MISSING:
            # Key the canonical template does not define: keep it verbatim.
            target[key] = existing_value
        elif isinstance(template_value, dict) and isinstance(existing_value, dict):
            # Standard or inline table: recurse so local-only sub-keys survive.
            _graft_local_additions(
                target[key],
                template_value,
                existing_value,
                identity_keys,
            )
        elif isinstance(template_value, list) and isinstance(existing_value, list):
            # Slots already claimed by the canonical template: a local entry
            # mapping to one of these is a (possibly stale) copy of a template
            # entry, so the template wins and the local copy is not re-added.
            template_slots = (
                {
                    _entry_identity(item, identity_keys)
                    for item in template_value
                    if isinstance(item, dict)
                }
                if identity_keys
                else set()
            )
            # Array in both: append local-only items, preserving their order.
            for item in existing_value:
                if (
                    isinstance(item, dict)
                    and _entry_identity(item, identity_keys) in template_slots
                ):
                    # Same slot as a canonical entry: superseded by the template.
                    continue
                if item not in template_value:
                    target[key].append(item)
        # Scalar in both: the canonical template value wins, nothing to graft.


def _update_tool_config(
    content: str,
    comp: ToolConfigComponent,
    pyproject_path: Path,
) -> str | None:
    """Re-derive an existing `[tool.X]` section from the bundled template.

    Rebuilds the section from the canonical template, then grafts local-only
    configuration back on via {func}`_graft_local_additions`: keys the template
    omits, extra items in shared arrays, and extra keys in shared nested
    tables. The template wins on shared scalars; `comp.preserved_keys`
    overrides that for named top-level keys (e.g. `current_version`).

    :param content: The current pyproject.toml content.
    :param comp: The component whose config is being synced.
    :param pyproject_path: Path to pyproject.toml (for resolving managed files).
    :return: Modified pyproject.toml content, or `None` if already up to date.
    """
    doc = tomlrt.loads(content)
    tool_name = comp.tool_section.removeprefix("tool.")

    tool_table = doc.get("tool")
    if not tool_table or tool_name not in tool_table:
        return None

    existing_section = tool_table[tool_name]

    # Plain-dict view of the canonical template, walked alongside the existing
    # section to decide what local-only content to graft back on.
    native_source = export_content(comp.source_file)
    template_plain = tomllib.loads(native_source)

    # Build the replacement table from the template, stripping file-level
    # comments that only apply to the standalone format.
    native_lines = native_source.splitlines()
    first_key = next(
        i for i, line in enumerate(native_lines) if line and not line.startswith("#")
    )
    native_stripped = "\n".join(native_lines[first_key:])
    new_section = _template_to_table(native_stripped)

    # Graft local-only configuration on top of the canonical template: keys the
    # template omits, extra items in shared arrays, and extra keys in shared
    # nested tables all survive, while the template wins on shared scalars and
    # on array-of-tables entries that share a `graft_identity_keys` slot.
    # Graft before assigning: the assignment deep-clones `new_section`, so
    # grafted entries must be in place first to carry over with their comments.
    _graft_local_additions(
        new_section,
        template_plain,
        existing_section,
        comp.graft_identity_keys,
    )

    # `preserved_keys` are authoritative locally (e.g. `current_version`): the
    # local value overrides the template placeholder.
    for key in comp.preserved_keys:
        if key in existing_section:
            new_section[key] = existing_section[key]

    # Replace the section in the document.
    tool_table[tool_name] = new_section

    modified = tomlrt.dumps(doc)

    if modified.strip() == content.strip():
        logging.info(f"[{comp.tool_section}] already up to date.")
        return None

    logging.info(f"Replaced [{comp.tool_section}] from bundled template.")
    return modified


def init_config(config_type: str, pyproject_path: Path | None = None) -> str | None:
    """Initialize a configuration by merging it into pyproject.toml.

    Reads the pyproject.toml file, checks if the tool section already exists,
    and if not, inserts the bundled template at the appropriate location.

    The template is stored in native format (without `[tool.X]` prefix) and
    is parsed by tomlrt and added under the `[tool]` table.

    :param config_type: The configuration type (e.g., `"ruff"`,
        `"bumpversion"`).
    :param pyproject_path: Path to pyproject.toml. Defaults to
        `./pyproject.toml`.
    :return: The modified pyproject.toml content, or `None` if no changes
        needed.
    :raises ValueError: If the config type is not supported.
    """
    comp = _BY_NAME.get(config_type)
    if not isinstance(comp, ToolConfigComponent):
        supported = ", ".join(
            c.name for c in COMPONENTS if isinstance(c, ToolConfigComponent)
        )
        msg = f"Unknown config type: {config_type!r}. Supported: {supported}"
        raise TypeError(msg)

    if pyproject_path is None:
        pyproject_path = Path("pyproject.toml")

    if not pyproject_path.exists():
        logging.error(f"File not found: {pyproject_path}")
        return None

    content = pyproject_path.read_text(encoding="UTF-8")
    doc = tomlrt.loads(content)
    tool_name = comp.tool_section.removeprefix("tool.")

    # Check if the config section already exists.
    tool_table = doc.get("tool")
    if tool_table and tool_name in tool_table:
        if comp.sync_mode == SyncMode.ONGOING:
            return _update_tool_config(content, comp, pyproject_path)
        logging.info(f"[{comp.tool_section}] already exists in {pyproject_path.name}")
        return None

    # Load the template and strip file-level comments.
    native_source = export_content(comp.source_file)
    native_lines = native_source.splitlines()
    first_key = next(
        i for i, line in enumerate(native_lines) if line and not line.startswith("#")
    )
    native_stripped = "\n".join(native_lines[first_key:])
    new_section = _template_to_table(native_stripped)

    doc.install(("tool", tool_name), new_section)

    return tomlrt.dumps(doc)


# ---------------------------------------------------------------------------
# Repository initialization
# ---------------------------------------------------------------------------


def default_version_pin() -> str:
    """Derive the default version pin from `__version__`.

    Strips any `.dev0` suffix and prefixes with `v`. For example,
    `"5.10.0.dev0"` becomes `"v5.10.0"`.
    """
    version = re.sub(r"\.dev\d*$", "", __version__)
    return f"v{version}"


@dataclass
class InitResult:
    """Result of a repository initialization run."""

    created: list[str] = field(default_factory=list)
    """Relative paths of newly created files."""

    updated: list[str] = field(default_factory=list)
    """Relative paths of existing files overwritten with new content."""

    skipped: list[str] = field(default_factory=list)
    """Relative paths of skipped (already existing) files."""

    excluded: list[str] = field(default_factory=list)
    """Exclude entries that were applied."""

    excluded_existing: list[str] = field(default_factory=list)
    """Relative paths of excluded files that still exist on disk."""

    unmodified_configs: list[str] = field(default_factory=list)
    """Relative paths of config files identical to bundled defaults."""

    removed_prunable: list[tuple[str, str]] = field(default_factory=list)
    """`(relative_path, successor)` for on-disk orphans of dropped assets
    whose content matches the last-shipped version (safe to auto-delete)."""

    removed_review: list[tuple[str, str]] = field(default_factory=list)
    """`(relative_path, successor)` for on-disk orphans of dropped assets that
    differ from the last-shipped version (locally modified: reported for
    manual review, never auto-deleted)."""

    warnings: list[str] = field(default_factory=list)
    """Warning messages emitted during initialization."""


def run_init(
    output_dir: Path,
    components: Sequence[str] = (),
    version: str | None = None,
    repo: str = DEFAULT_REPO,
    repo_slug: str | None = None,
    config: Config | None = None,
) -> InitResult:
    """Bootstrap a repository for use with `kdeldycke/repomatic`.

    Creates thin-caller workflow files, exports configuration files, and
    generates a minimal `changelog.md` if missing. Managed files (workflows,
    configs, skills) are always overwritten. User-owned files
    (`changelog.md`, `zizmor.yaml`) are created once and never overwritten.

    For `awesome-*` repositories, the `awesome-template` component is
    auto-included when no explicit component selection is made.

    ```{note}
    Scope exclusions (`RepoScope.AWESOME_ONLY`, `PYTHON_ONLY`) and
    user-config exclusions (`[tool.repomatic] exclude`) only apply
    during bare `repomatic init`. When components are explicitly named
    on the CLI, scope is bypassed: the caller knows what they asked for.
    This allows workflows to materialize out-of-scope configs at runtime
    (like `repomatic init publish-pypi-action` in a non-Python repo).
    ```

    :param output_dir: Root directory of the target repository.
    :param components: Components to initialize. Empty means all defaults.
        When non-empty, scope and user-config exclusions are bypassed.
    :param version: Version pin for upstream workflows (e.g., `v5.10.0`).
    :param repo: Upstream repository containing reusable workflows.
    :param repo_slug: Repository `owner/name` slug for awesome-template URL
        rewriting. Auto-detected via {class}`Metadata` if not provided.
    :return: Summary of created, updated, skipped, and warned items.
    """
    if version is None:
        version = default_version_pin()

    # Parse CLI selection.  Entries may be bare component names (e.g.,
    # "skills") or qualified component/file selectors (e.g.,
    # "skills/repomatic-topics").
    if components:
        selected_full, selected_files = parse_component_entries(
            list(components), context="selection"
        )
        # Bare component name overrides file-level selection for the same
        # component — "skills" means all skills even if "skills/x" also
        # appears.
        for name in selected_full:
            selected_files.pop(name, None)
        selected = selected_full | set(selected_files.keys())
    else:
        selected_full = set()
        selected_files = {}
        selected = {
            c.name
            for c in COMPONENTS
            if c.init_default in (InitDefault.INCLUDE, InitDefault.EXCLUDE)
        }
    result = InitResult()

    # Auto-include awesome-template for awesome-* repositories.
    if not repo_slug:
        from .metadata import Metadata

        repo_slug = Metadata().repo_slug
    is_awesome = bool(repo_slug and repo_slug.split("/")[-1].startswith("awesome-"))
    is_python = is_python_project(output_dir)
    logging.debug("Repository traits: awesome=%s python=%s", is_awesome, is_python)
    if is_awesome and not components:
        selected.add("awesome-template")

    # Load config for source path resolution and exclusion rules.
    if config is None:
        config = load_repomatic_config()
    source_paths = resolve_source_paths(config)

    # Parse exclude/include config. User exclude is additive to defaults;
    # user include overrides both. Qualified entries (component/file)
    # implicitly select the parent component.
    user_exclude: list[str] = config.exclude
    user_include: list[str] = config.include
    if user_include:
        include_full, include_files = parse_component_entries(
            user_include, context="include"
        )
    else:
        include_full, include_files = set(), {}
    default_exclusions = {
        c.name for c in COMPONENTS if c.init_default == InitDefault.EXCLUDE
    }
    exclude_entries = sorted(
        (default_exclusions | set(user_exclude))
        - set(user_include)
        - set(include_files)
    )
    if default_exclusions:
        logging.debug("Default exclusions: %s", ", ".join(sorted(default_exclusions)))
    if user_exclude:
        logging.debug("User exclude: %s", ", ".join(user_exclude))
    if user_include:
        logging.debug("User include: %s", ", ".join(user_include))
    excluded_components, excluded_files = parse_component_entries(
        exclude_entries, context="exclude"
    )

    # Apply user-configured exclusions when no explicit components given.
    if not components:
        actually_excluded = excluded_components & selected
        selected -= excluded_components
        result.excluded = sorted(
            list(actually_excluded)
            + [
                f"{c}/{f}"
                for c, fs in sorted(excluded_files.items())
                if c not in actually_excluded
                for f in sorted(fs)
            ]
        )

        # Expand component-level exclusions into file-level entries so
        # detection below is a single unified pass.
        for excl_name in actually_excluded:
            excl_comp = _BY_NAME.get(excl_name)
            if excl_comp and excl_comp.files:
                ids = {e.file_id for e in excl_comp.files}
                excluded_files.setdefault(excl_name, set()).update(ids)

    # Classification pass: determine which components and files to
    # initialize, and which to flag for stale-file detection.
    #
    # Three exclusion mechanisms, applied in order per component:
    #
    # 1. Scope (component-level and file-level `RepoScope`).
    #    Bypassed by explicit CLI naming or `[tool.repomatic] include`.
    #    Scope exclusions on `selected` apply in all repos including the
    #    source repo (an AWESOME_ONLY config should not be merged into the
    #    Python source repo's `pyproject.toml`). Stale-file detection
    #    is suppressed in the source repo so bundled data files are never
    #    flagged for deletion.
    #
    # 2. Config key (component-level and file-level `config_key`).
    #    Always applies, even with explicit CLI naming: the user's
    #    `[tool.repomatic]` config is authoritative for feature flags.
    #
    # 3. User config (`[tool.repomatic] exclude`/`include`).
    #    Already applied above, before this loop.
    is_source = _is_source_repo(output_dir)
    scope_excluded_targets: list[str] = []

    for reg_comp in COMPONENTS:
        # In the source repo, clear any user-config exclusions for bundled
        # components so their data files are never flagged for deletion.
        if is_source and reg_comp.files:
            excluded_files.pop(reg_comp.name, None)

        # Scope is bypassed by explicit CLI naming or config include.
        scope_bypassed = bool(components) or reg_comp.name in include_full

        # --- Component-level scope ---
        if not reg_comp.scope.matches(is_awesome, is_python):
            logging.debug(
                "Scope exclusion: %s (%s) not applicable to repo "
                "(awesome=%s, python=%s).",
                reg_comp.name,
                reg_comp.scope.name,
                is_awesome,
                is_python,
            )
            if not scope_bypassed and reg_comp.name not in include_files:
                selected.discard(reg_comp.name)
                if not is_source:
                    if isinstance(reg_comp, GeneratedComponent) and reg_comp.target:
                        scope_excluded_targets.append(reg_comp.target)
                    elif reg_comp.files:
                        ids = {e.file_id for e in reg_comp.files}
                        excluded_files.setdefault(reg_comp.name, set()).update(ids)
                continue

        # --- Component-level config_key ---
        if reg_comp.name in selected and not reg_comp.is_enabled(config):
            selected.discard(reg_comp.name)
            logging.info(
                "[tool.repomatic] %s is disabled. Skipping %s.",
                reg_comp.config_key,
                reg_comp.name,
            )

        # --- File-level scope and config_key ---
        for entry in reg_comp.files:
            if not entry.scope.matches(is_awesome, is_python):
                logging.debug(
                    "Scope exclusion: %s/%s (%s) not applicable to repo "
                    "(awesome=%s, python=%s).",
                    reg_comp.name,
                    entry.file_id,
                    entry.scope.name,
                    is_awesome,
                    is_python,
                )
                if (
                    not scope_bypassed
                    and not is_source
                    and entry.file_id not in include_files.get(reg_comp.name, set())
                ):
                    excluded_files.setdefault(reg_comp.name, set()).add(entry.file_id)
            if not is_source and not entry.is_enabled(config):
                logging.debug(
                    "Config exclusion: %s/%s (%s disabled).",
                    reg_comp.name,
                    entry.file_id,
                    entry.config_key,
                )
                excluded_files.setdefault(reg_comp.name, set()).add(entry.file_id)

    # Detect excluded files that still exist on disk.
    for comp_name, file_ids in sorted(excluded_files.items()):
        for fid in sorted(file_ids):
            rel = excluded_rel_path(comp_name, fid)
            if rel:
                if comp_name == "agents":
                    rel = _resolve_agents_target(rel, config)
                elif comp_name == "skills":
                    rel = _resolve_skills_target(rel, config)
                if (output_dir / rel).exists():
                    result.excluded_existing.append(rel)
    for rel in sorted(scope_excluded_targets):
        if (output_dir / rel).exists():
            result.excluded_existing.append(rel)

    # Detect orphans of assets repomatic has dropped (renamed or removed
    # upstream). Suppressed in the source repo, where the data files for
    # these tombstones no longer exist anyway.
    if not is_source:
        result.removed_prunable, result.removed_review = _detect_removed_assets(
            output_dir, config
        )

    # Dispatch by component type.
    tool_configs_to_merge: list[str] = []

    logging.debug("Selected components: %s", ", ".join(sorted(selected)))

    for comp in COMPONENTS:
        if comp.name not in selected:
            continue

        file_exclude = frozenset(excluded_files.get(comp.name, set()))
        file_include = (
            frozenset(selected_files[comp.name])
            if comp.name in selected_files
            else None
        )

        if isinstance(comp, WorkflowComponent):
            _init_workflows(
                output_dir,
                repo,
                version,
                result,
                exclude=file_exclude,
                include=file_include,
                source_paths=source_paths,
                config=config,
            )

        elif isinstance(comp, BundledComponent):
            _init_config_files(
                output_dir,
                comp.name,
                result,
                exclude_ids=file_exclude,
                include_ids=file_include,
                config=config,
            )
            # Labels have extra files fetched from [tool.repomatic] config.
            if comp.name == "labels":
                _fetch_extra_labels(output_dir, result, config=config)

        elif isinstance(comp, TemplateComponent):
            if repo_slug:
                init_awesome_template(output_dir, repo_slug, result)

        elif isinstance(comp, GeneratedComponent):
            _init_changelog(output_dir, result, config=config)

        elif isinstance(comp, ToolConfigComponent):
            tool_configs_to_merge.append(comp.name)

    # Merge tool configs into pyproject.toml (batched for efficiency).
    if tool_configs_to_merge:
        _init_tool_configs(output_dir, tool_configs_to_merge, result)

    # Check for native tool config files identical to bundled defaults.
    # Init-managed files (labels, renovate) are already handled inline by
    # _init_config_files, so only check tool_runner configs here.
    for _tool_name, rel_path in find_unmodified_configs():
        result.unmodified_configs.append(rel_path)
        logging.warning(f"Unmodified config (matches bundled default): {rel_path}")

    return result


def _init_workflows(
    output_dir: Path,
    repo: str,
    version: str,
    result: InitResult,
    *,
    exclude: frozenset[str] = frozenset(),
    include: frozenset[str] | None = None,
    source_paths: list[str] | None = None,
    config: Config | None = None,
) -> None:
    """Generate thin-caller workflows and sync non-reusable workflow headers.

    :param include: When not `None`, only generate files in this set.
    """
    # Lazy import to avoid circular dependency with workflow_sync.
    from . import __git_tag_sha__
    from .github.workflow_sync import (
        PathsSpec,
        extract_extra_jobs,
        generate_thin_caller,
        generate_workflow_header,
    )

    # Use the build-time SHA for pinning, if available.
    commit_sha: str | None = __git_tag_sha__ or None

    workflows = REUSABLE_WORKFLOWS
    if include is not None:
        workflows = tuple(w for w in workflows if w in include)
    if exclude:
        workflows = tuple(w for w in workflows if w not in exclude)

    # Exclude config-gated workflows whose toggle is off.
    if config is None:
        config = load_repomatic_config()
    for entry in _BY_NAME["workflows"].files:
        if entry.file_id in workflows and not entry.is_enabled(config):
            workflows = tuple(w for w in workflows if w != entry.file_id)

    paths_spec = PathsSpec(
        source_paths=source_paths,
        extra_paths=list(config.workflow.extra_paths),
        ignore_paths=list(config.workflow.ignore_paths),
        workflow_paths={k: list(v) for k, v in config.workflow.paths.items()},
    )

    workflows_dir = output_dir / ".github" / "workflows"
    workflows_dir.mkdir(parents=True, exist_ok=True)

    # Generate thin-caller workflows for reusable workflows.
    for filename in workflows:
        target = workflows_dir / filename
        rel = target.relative_to(output_dir).as_posix()
        existed = target.exists()
        content = generate_thin_caller(
            filename,
            repo,
            version,
            paths_spec=paths_spec,
            commit_sha=commit_sha,
        )
        # Preserve extra downstream jobs from the existing file.
        existing_content: str | None = None
        if existed:
            existing_content = target.read_text(encoding="UTF-8")
            extra = extract_extra_jobs(existing_content, repo)
            if extra:
                content += extra
            if existing_content == content:
                logging.debug(f"Unchanged: {rel}")
                continue
        target.write_text(content, encoding="UTF-8")
        if existed:
            result.updated.append(rel)
            logging.info(f"Updated: {rel}")
        else:
            result.created.append(rel)
            logging.info(f"Created: {rel}")

    # Sync headers for non-reusable workflows that already exist on disk.
    non_reusable = sorted(
        f.file_id for f in _BY_NAME["workflows"].files if not f.reusable
    )
    for filename in non_reusable:
        if include is not None and filename not in include:
            continue
        if exclude and filename in exclude:
            continue
        target = workflows_dir / filename
        if not target.exists():
            continue
        rel = target.relative_to(output_dir).as_posix()
        try:
            canonical_header = generate_workflow_header(filename, paths_spec=paths_spec)
        except (ValueError, FileNotFoundError):
            logging.warning(f"Cannot extract header for {filename}. Skipping.")
            continue
        existing = target.read_text(encoding="UTF-8")
        jobs_match = re.search(r"^jobs:", existing, re.MULTILINE)
        if jobs_match is None:
            continue
        content = canonical_header + existing[jobs_match.start() :]
        if content == existing:
            logging.debug(f"Header already in sync: {rel}")
            continue
        target.write_text(content, encoding="UTF-8")
        result.updated.append(rel)
        logging.info(f"Synced header: {rel}")


def _is_source_repo(output_dir: Path) -> bool:
    """Detect whether `output_dir` is the repomatic source repository root.

    Returns `True` when `output_dir` contains the `repomatic` Python
    package source tree (`repomatic/__init__.py` and `repomatic/data/`).
    Only the upstream source repo has these. This prevents auto-exclusion from
    deleting files that are the source of truth (skills, opt-in workflows,
    bundled configs).

    ```{note}
    Detection is based on `output_dir` contents, not on `__file__`,
    because `uvx --from .` installs the package into a temp venv where
    `__file__` no longer points to the source checkout.
    ```
    """
    resolved = output_dir.resolve()
    return (resolved / "repomatic" / "__init__.py").exists() and (
        resolved / "repomatic" / "data"
    ).is_dir()


def _is_renovate_source_repo(output_dir: Path) -> bool:
    """Detect whether `output_dir` has the upstream renovate config pair.

    Returns `True` when the directory is the source repo and contains the
    root `renovate.json5`. Used by {func}`_init_config_files` to regenerate
    the bundled copy instead of overwriting the root config.
    """
    resolved = output_dir.resolve()
    return _is_source_repo(output_dir) and (resolved / "renovate.json5").exists()


def _resolve_agents_target(entry_target: str, config: Config | None) -> str:
    """Apply `agents.location` override to an agent file's target path.

    Replaces the default `.claude/agents/` prefix with the configured
    `agents_location` when the config specifies a non-default value.
    """
    default = Config.agents_location.removeprefix("./").rstrip("/") + "/"
    if not config or not entry_target.startswith(default):
        return entry_target
    custom = config.agents_location.removeprefix("./").rstrip("/") + "/"
    if custom == default:
        return entry_target
    return custom + entry_target[len(default) :]


def _resolve_skills_target(entry_target: str, config: Config | None) -> str:
    """Apply `skills.location` override to a skill file's target path.

    Replaces the default `.claude/skills/` prefix with the configured
    `skills_location` when the config specifies a non-default value.
    """
    # Normalize the Config default (which has a "./" prefix) to match the
    # registry target format (which omits it).
    default = Config.skills_location.removeprefix("./").rstrip("/") + "/"
    if not config or not entry_target.startswith(default):
        return entry_target
    custom = config.skills_location.removeprefix("./").rstrip("/") + "/"
    if custom == default:
        return entry_target
    return custom + entry_target[len(default) :]


def _detect_removed_assets(
    output_dir: Path, config: Config | None
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Find on-disk orphans of assets repomatic has dropped.

    Walks {data}`~repomatic.registry.REMOVED_ASSETS`, resolves each tombstone's
    target through the same path overrides as live files, and classifies any
    that still exist on disk:

    - *prunable*: an untouched copy, safe to delete. For skills and agents the
      normalized content matches one of the recorded shipped hashes; for
      workflows the file is a repomatic-lineage thin-caller for the removed
      workflow with no extra downstream jobs.
    - *review*: present but customized locally (content differs, or a
      thin-caller carries extra jobs). Reported, never deleted.

    A file that is not recognizable as a repomatic-shipped orphan (a user's own
    workflow that merely shares a name) is left untouched.

    :param output_dir: Root directory of the target repository.
    :param config: Repomatic config for `skills.location` / `agents.location`
        path overrides.
    :return: `(prunable, review)`, each a list of `(relative_path,
        successor)` tuples sorted by path.
    """
    prunable: list[tuple[str, str]] = []
    review: list[tuple[str, str]] = []
    for asset in REMOVED_ASSETS:
        rel = asset.target
        if asset.component == "agents":
            rel = _resolve_agents_target(rel, config)
        elif asset.component == "skills":
            rel = _resolve_skills_target(rel, config)
        path = output_dir / rel
        if not path.exists():
            continue
        entry = (rel, asset.successor)
        if asset.component == "workflows":
            verdict = _classify_removed_workflow(path)
            if verdict == "prune":
                prunable.append(entry)
            elif verdict == "review":
                review.append(entry)
            # "skip": not a lineage thin-caller, leave it untouched.
        else:
            # Compare against the on-disk form init itself writes: rstrip()ed
            # with a single trailing newline. A match against any released
            # revision means the copy is untouched and safe to prune.
            normalized = path.read_text(encoding="UTF-8").rstrip() + "\n"
            digest = hashlib.sha256(normalized.encode("UTF-8")).hexdigest()
            if digest in asset.hashes:
                prunable.append(entry)
            else:
                review.append(entry)
    prunable.sort()
    review.sort()
    return prunable, review


def _classify_removed_workflow(path: Path) -> str:
    """Classify an on-disk workflow file as an orphan of a removed workflow.

    Thin-callers are parameterized per repo (version pin, `paths:` filters), so
    they carry no fixed content to hash. The file is matched instead by its
    `uses:` fingerprint against the upstream slugs in
    {data}`~repomatic.registry.UPSTREAM_REPO_SLUGS`.

    :return: `"prune"` for a pure repomatic-lineage thin-caller for this
        workflow, `"review"` when the user appended extra jobs, or `"skip"`
        when the file is not a lineage thin-caller (leave it untouched).
    """
    # Lazy import to avoid a circular dependency with workflow_sync.
    from .github.workflow_sync import extract_extra_jobs, identify_canonical_workflow

    for slug in UPSTREAM_REPO_SLUGS:
        if identify_canonical_workflow(path, slug) == path.name:
            extra = extract_extra_jobs(path.read_text(encoding="UTF-8"), slug)
            return "review" if extra.strip() else "prune"
    return "skip"


def _init_config_files(
    output_dir: Path,
    component_name: str,
    result: InitResult,
    *,
    exclude_ids: frozenset[str] = frozenset(),
    include_ids: frozenset[str] | None = None,
    config: Config | None = None,
) -> None:
    """Export bundled config files for a component.

    For components without `keep_unmodified`, files already on disk that
    are identical to the bundled template are flagged as unmodified and not
    overwritten.

    **Upstream renovate handling:** When running in the repomatic source
    repository, the root `renovate.json5` is the authoritative config (it
    contains `assignees` and self-referencing `customManagers`). Instead
    of overwriting it with the stripped template, this function regenerates
    `repomatic/data/renovate.json5` — the bundled copy shipped to downstream
    repos.

    :param exclude_ids: File identifiers to skip within this component.
    :param include_ids: When not `None`, only export files in this set.
    :param config: Repomatic config for path overrides (like `skills.location`
        or `agents.location`).
    """
    comp = _BY_NAME[component_name]
    for entry in comp.files:
        if include_ids is not None and entry.file_id not in include_ids:
            continue
        if exclude_ids and entry.file_id in exclude_ids:
            continue
        if component_name == "agents":
            effective_target = _resolve_agents_target(entry.target, config)
        elif component_name == "skills":
            effective_target = _resolve_skills_target(entry.target, config)
        else:
            effective_target = entry.target
        target = output_dir / effective_target
        rel = target.relative_to(output_dir).as_posix()

        # In the repomatic source repo, the root renovate.json5 is
        # authoritative. Read it from output_dir (not __file__-relative,
        # which breaks under uvx), strip repo-specific settings, and
        # regenerate the bundled data copy instead of overwriting the root.
        if component_name == "renovate" and _is_renovate_source_repo(output_dir):
            root_content = (output_dir / entry.source).read_text(encoding="UTF-8")
            normalized = _strip_renovate_repo_settings(root_content).rstrip() + "\n"
            bundled = output_dir / "repomatic" / "data" / entry.source
            existing = bundled.read_text(encoding="UTF-8").rstrip() + "\n"
            bundled_rel = f"repomatic/data/{entry.source}"
            if existing == normalized:
                # Do not mark as unmodified — it is package data, not a
                # user-facing config that can be safely deleted.
                logging.info(f"Bundled config up to date: {bundled_rel}")
            else:
                bundled.write_text(normalized, encoding="UTF-8")
                result.updated.append(bundled_rel)
                logging.info(f"Regenerated bundled config: {bundled_rel}")
            continue

        content = export_content(entry.source)
        # Normalize trailing whitespace to a single newline, matching the
        # convention used by sync commands (echo(content.rstrip(), ...)).
        normalized = content.rstrip() + "\n"

        if target.exists():
            existing = target.read_text(encoding="UTF-8").rstrip() + "\n"
            if existing == normalized:
                if not comp.keep_unmodified:
                    result.unmodified_configs.append(rel)
                    logging.info(f"Unmodified (matches bundled default): {rel}")
                else:
                    logging.debug(f"Unchanged: {rel}")
                continue
            target.write_text(normalized, encoding="UTF-8")
            result.updated.append(rel)
            logging.info(f"Updated: {rel}")
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(normalized, encoding="UTF-8")
            result.created.append(rel)
            logging.info(f"Created: {rel}")


AWESOME_TEMPLATE_SLUG = "kdeldycke/awesome-template"
"""Source slug embedded in bundled awesome-template files, rewritten at sync time."""


def _copy_template_tree(root: Traversable, dest: Path) -> tuple[int, int]:
    """Recursively copy files from a traversable resource tree to disk.

    Skips `__init__.py` and `__pycache__` entries. Returns
    `(created, updated)` counts.
    """
    created = 0
    updated = 0
    for entry in root.iterdir():
        if entry.name in ("__init__.py", "__pycache__"):
            continue
        if entry.is_dir():
            c, u = _copy_template_tree(entry, dest / entry.name)
            created += c
            updated += u
        else:
            target = dest / entry.name
            existed = target.exists()
            new_bytes = entry.read_bytes()
            if existed and target.read_bytes() == new_bytes:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(new_bytes)
            if existed:
                updated += 1
                logging.info(f"Updated: {target}")
            else:
                created += 1
                logging.info(f"Created: {target}")
    return created, updated


def init_awesome_template(
    output_dir: Path,
    repo_slug: str,
    result: InitResult,
) -> None:
    """Copy bundled awesome-template files and rewrite URLs.

    Copies all files from the `repomatic/data/awesome_template/` bundle into
    *output_dir* and rewrites `kdeldycke/awesome-template` URLs in
    `.github/` markdown and YAML files to match *repo_slug*.

    :param output_dir: Root directory of the target repository.
    :param repo_slug: Target `owner/name` slug for URL rewriting.
    :param result: {class}`InitResult` accumulator for created/updated files.
    """
    template_root = files("repomatic.data").joinpath("awesome_template")
    created, updated = _copy_template_tree(template_root, output_dir)
    if created:
        result.created.append(f"awesome-template ({created} files)")
    if updated:
        result.updated.append(f"awesome-template ({updated} files)")

    # Rewrite template URLs in .github/ markdown and YAML files.
    github_dir = output_dir / ".github"
    if github_dir.is_dir():
        for path in github_dir.rglob("*"):
            if not path.is_file() or path.suffix not in (".md", ".yaml", ".yml"):
                continue
            content = path.read_text(encoding="UTF-8")
            new_content = content.replace(
                f"/{AWESOME_TEMPLATE_SLUG}/", f"/{repo_slug}/"
            )
            if new_content != content:
                path.write_text(new_content, encoding="UTF-8")
                logging.info(f"Rewrote URLs in: {path}")


def find_unmodified_init_files() -> list[tuple[str, str]]:
    """Find init-managed config files identical to their bundled defaults.

    Checks bundled components without `keep_unmodified` for files on disk
    whose content matches the bundled template (via {func}`export_content`)
    after trailing-whitespace normalization (`.rstrip() + "\\n"`).

    Mirrors the API of {func}`tool_runner.find_unmodified_configs`, returning
    `(component_name, relative_path)` tuples.

    :return: List of `(component_name, relative_path)` tuples for each
        unmodified file found.
    """
    unmodified: list[tuple[str, str]] = []
    for comp in COMPONENTS:
        if not isinstance(comp, BundledComponent):
            continue
        if comp.keep_unmodified or not comp.files:
            continue
        for entry in comp.files:
            path = Path(entry.target)
            if not path.exists():
                continue
            bundled = export_content(entry.source).rstrip() + "\n"
            on_disk = path.read_text(encoding="UTF-8").rstrip() + "\n"
            if on_disk == bundled:
                unmodified.append((comp.name, entry.target))
    return unmodified


def find_all_unmodified_configs() -> list[tuple[str, str]]:
    """Find all config files identical to their bundled defaults.

    Combines tool configs (yamllint, zizmor, etc.) from
    {func}`tool_runner.find_unmodified_configs` and init-managed configs
    (labels, renovate) from {func}`find_unmodified_init_files`.

    :return: List of `(label, relative_path)` tuples for each unmodified
        file found.
    """
    return find_unmodified_configs() + find_unmodified_init_files()


def _init_changelog(
    output_dir: Path,
    result: InitResult,
    *,
    config: Config | None = None,
) -> None:
    """Create a minimal changelog.md if it doesn't exist.

    The changelog stub is only useful for bootstrapping new repositories.
    An existing `changelog.md` is never overwritten — it contains real
    release history that would be destroyed by the stub template.
    """
    location = (config or Config()).changelog_location.removeprefix("./")
    changelog_path = output_dir / location
    rel = changelog_path.relative_to(output_dir).as_posix()
    if changelog_path.exists():
        result.skipped.append(rel)
        logging.debug(f"Skipped existing: {rel}")
        return
    changelog_content = (
        "# Changelog\n"
        "\n"
        "## [Unreleased](https://github.com/USER/REPO/compare/main...main)\n"
    )
    changelog_path.write_text(changelog_content, encoding="UTF-8")
    result.created.append(rel)
    logging.info(f"Created: {rel}")


def _fetch_extra_labels(
    output_dir: Path,
    result: InitResult,
    config: Config | None = None,
) -> None:
    """Download extra label files from `[tool.repomatic]` config.

    Reads `labels.extra-files` URLs and downloads each file to an
    `extra-labels/` subdirectory under `output_dir`.
    Does nothing if no URLs are configured.
    """
    if config is None:
        config = load_repomatic_config()
    urls = config.labels.extra_files
    if not urls:
        logging.debug("No labels.extra-files configured.")
        return

    target_dir = output_dir / "extra-labels"
    target_dir.mkdir(exist_ok=True)
    for url in urls:
        url = url.strip()
        if not url:
            continue
        filename = PurePosixPath(url).name
        target = target_dir / filename
        rel = target.relative_to(output_dir).as_posix()
        logging.info(f"Downloading {url} -> {target}")
        urlretrieve(url, target)
        result.created.append(rel)


def _init_tool_configs(
    output_dir: Path,
    tool_configs: Sequence[str],
    result: InitResult,
) -> None:
    """Merge selected tool configs into pyproject.toml."""
    pyproject_path = output_dir / "pyproject.toml"
    if not pyproject_path.exists():
        result.warnings.append(
            "pyproject.toml not found; skipping tool config initialization."
        )
        logging.warning(result.warnings[-1])
        return
    rel = pyproject_path.relative_to(output_dir).as_posix()
    for config_type in tool_configs:
        tc = _BY_NAME[config_type]
        assert isinstance(tc, ToolConfigComponent)
        section = tc.tool_section
        had_section = re.search(
            rf"^\[{re.escape(section)}\]",
            pyproject_path.read_text(encoding="UTF-8"),
            re.MULTILINE,
        )
        merged = init_config(config_type, pyproject_path)
        if merged is None:
            logging.info(f"[{section}] already up to date, skipped.")
        else:
            pyproject_path.write_text(merged, encoding="UTF-8")
            if had_section:
                logging.info(f"Updated [{section}].")
                if rel not in result.updated:
                    result.updated.append(rel)
            else:
                logging.info(f"Merged [{section}].")
                if rel not in result.created:
                    result.created.append(rel)
