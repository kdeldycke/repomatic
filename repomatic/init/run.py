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

"""Run `repomatic init`: select the components, then write their files.

Selectors use the same `component[/file]` syntax as the `exclude` config option
in `[tool.repomatic]`. Qualified entries like `skills/repomatic-topics` select a
single file within a component.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field, fields
from enum import Enum
from functools import cached_property
from importlib.resources import files
from pathlib import Path, PurePosixPath

from ..bundle import get_data_content
from ..changelog import CHANGELOG_HEADER
from ..claude_plugin import merge_plugin_settings
from ..components import (
    BARE_INIT_COMPONENTS,
    COMPONENTS,
    COMPONENTS_BY_NAME,
    DEFAULT_EXCLUDED_COMPONENTS,
    EPHEMERAL_COMPONENTS,
    EPHEMERAL_TARGETS,
    NON_REUSABLE_WORKFLOWS,
    REUSABLE_WORKFLOWS,
    SKILL_FILENAME,
    BundledComponent,
    Component,
    GeneratedComponent,
    RepoTraits,
    TemplateComponent,
    ToolConfigComponent,
    WorkflowComponent,
    parse_component_entries,
)
from ..file_ops import normalize_text, unlink_with_empty_parents, write_if_changed
from ..github.labels import extra_label_filename
from ..github.urls import repo_name
from ..github.workflow_files import WORKFLOW_DIR, parse_workflow_texts
from ..github.workflow_sync import (
    PathsSpec,
    generate_workflow_header,
    render_thin_caller_for_target,
)
from ..http import get_bytes
from ..metadata.core import (
    METADATA_SUBCOMMAND,
    REMOVED_METADATA_ALIAS,
    Metadata,
    unknown_metadata_keys,
)
from ..pyproject import (
    is_python_package,
    is_python_project,
    read_pyproject_toml,
    resolve_source_paths,
)
from ..release.self_pin import add_cooldown_exemption, align_inline_pins
from ..skills import UPGRADE_SKILL, skill_launcher
from ..templating import render_template
from ..tooling.runner import find_unmodified_configs
from ..upstream import DEFAULT_REPO, is_awesome_repo, is_source_repo
from ..versions import is_newer
from .removed_assets import detect_removed_assets
from .tool_config import adopted_ongoing_configs, merge_tool_config
from .upgrade import (
    WorkflowPin,
    github_yaml_texts,
    highest_upstream_pin,
    pins_running_release,
    resolve_workflow_pin,
)

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence
    from importlib.resources.abc import Traversable
    from typing import TypeAlias

    from ..config import Config

    _Writer: TypeAlias = (
        "Callable[[_InitRun, Component, frozenset[str] | None, frozenset[str]], None]"
    )


AWESOME_TEMPLATE_REWRITTEN_SUFFIXES = frozenset((".md", ".yaml", ".yml"))
"""Extensions of the awesome-template files whose URLs name the repository."""

AWESOME_TEMPLATE_SLUG = "kdeldycke/awesome-template"
"""Source slug embedded in bundled awesome-template files, rewritten at sync time."""

AWESOME_TEMPLATE_URL_RE = re.compile(rf"/{re.escape(AWESOME_TEMPLATE_SLUG)}(?![\w.-])")
"""A URL path naming the template repository itself.

The lookahead stops a longer repository name, like `awesome-template-v2`, from
matching. Whatever else follows the slug matches: a `/blob/` path, a `#readme`
fragment, or the end of the link.
"""

TEMPLATE_TREE_SKIPPED = frozenset(("__init__.py", "__pycache__"))
"""Entries of a bundled tree that make it a package, not part of the template."""


class ReportSection(Enum):
    """A section of the `init` report that lists files a run may delete.

    The value is the name of the {class}`InitResult` field holding the paths.
    """

    EXCLUDED = "excluded_existing"
    """Files the configuration excludes, still on disk."""

    UNMODIFIED = "unmodified_configs"
    """Tool configs identical to the bundled defaults."""

    REMOVED = "removed_prunable"
    """Untouched orphans of assets repomatic no longer ships."""

    REMOVED_MODIFIED = "removed_review"
    """Orphans of dropped assets, edited since repomatic shipped them."""

    @property
    def prunes_parents(self) -> bool:
        """Whether deleting a path also removes the parents it leaves empty.

        On for the sections whose targets sit in directories repomatic itself
        created (`.claude/skills/<name>/`). Off for unmodified tool configs,
        which share `.github/` and the repository root with files repomatic
        does not own.
        """
        return self is not ReportSection.UNMODIFIED


@dataclass(frozen=True)
class DeletionPolicy:
    """Which {class}`ReportSection` an `init` run deletes from disk.

    Each field is named after the `init` option that sets it, which is what
    {meth}`flags` reads back to hand the same policy to a child `init`.
    """

    delete_excluded: bool = False
    """Delete the files the configuration excludes."""

    delete_unmodified: bool = False
    """Delete the tool configs identical to the bundled defaults."""

    keep_removed: bool = False
    """Keep the untouched orphans of dropped assets, deleted by default."""

    delete_removed_modified: bool = False
    """Also delete the orphans edited since repomatic shipped them."""

    def __post_init__(self) -> None:
        """Refuse a policy that both keeps and force-deletes the orphans.

        :raises ValueError: When `keep_removed` and `delete_removed_modified`
            are both set.
        """
        if self.keep_removed and self.delete_removed_modified:
            msg = "--keep-removed and --delete-removed-modified are mutually exclusive."
            raise ValueError(msg)

    def deletes(self, section: ReportSection) -> bool:
        """Whether this policy deletes the paths listed in *section*."""
        if section is ReportSection.EXCLUDED:
            return self.delete_excluded
        if section is ReportSection.UNMODIFIED:
            return self.delete_unmodified
        if section is ReportSection.REMOVED:
            return not self.keep_removed
        return self.delete_removed_modified

    def flags(self) -> list[str]:
        """The `init` options that set this policy, for a child `init`."""
        return [
            f"--{policy_field.name.replace('_', '-')}"
            for policy_field in fields(self)
            if getattr(self, policy_field.name)
        ]


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

    previous_pin: str | None = None
    """Bare version of the highest upstream `uses:` pin the repository carried
    before the run, read only when a workflow component was selected. `None`
    for a first adoption, or for a run generating no workflow."""

    adopted_pin: str | None = None
    """Bare version the run wrote its thin callers at, or `None` when the
    workflow sync was skipped."""

    def record(self, path: str, *, existed: bool, verb: str = "") -> None:
        """File a written *path* as created or updated, and log it.

        :param path: The repository-relative path of the file written.
        :param existed: Whether the file was on disk before the write.
        :param verb: Overrides the log line's leading word (`Synced header`).
        """
        if existed:
            self.updated.append(path)
            logging.info(f"{verb or 'Updated'}: {path}")
        else:
            self.created.append(path)
            logging.info(f"{verb or 'Created'}: {path}")

    def warn(self, message: str) -> None:
        """Log *message* and keep it for the closing summary of the run."""
        self.warnings.append(message)
        logging.warning(message)

    def entries(self, section: ReportSection) -> list[str] | list[tuple[str, str]]:
        """The entries *section* lists: paths, or `(path, successor)` pairs."""
        entries: list[str] | list[tuple[str, str]] = getattr(self, section.value)
        return entries

    def paths(self, section: ReportSection) -> list[str]:
        """The repository-relative paths *section* lists."""
        return [
            entry[0] if isinstance(entry, tuple) else entry
            for entry in self.entries(section)
        ]

    def prune(self, output_dir: Path, policy: DeletionPolicy) -> None:
        """Delete every section of the report *policy* asks to delete.

        :param output_dir: Repository root the paths are relative to.
        :param policy: The sections to delete.
        """
        for section in ReportSection:
            if not policy.deletes(section):
                continue
            for path in self.paths(section):
                target = output_dir / path
                if section.prunes_parents:
                    unlink_with_empty_parents(target, output_dir)
                else:
                    target.unlink()

    def upgraded_pin(self) -> tuple[str, str] | None:
        """`(previous, adopted)` when the run moved the upstream pin forward.

        The pair `init`'s closing hint and the upgrade review are keyed on. A
        first adoption has no previous release to review against, a
        regeneration at the pinned version moves nothing, and a rollback is a
        deliberate step back no upgrade review should chase, so all three
        answer `None`.
        """
        if self.previous_pin is None or self.adopted_pin is None:
            return None
        if not is_newer(self.adopted_pin, self.previous_pin):
            return None
        return self.previous_pin, self.adopted_pin

    def next_steps(self, output_dir: Path, config: Config) -> list[str]:
        """The steps left to the maintainer after the run, in order.

        A run that only staged ephemeral files (`init labels`) produced scratch
        input for the command about to read it, so it has nothing to commit and
        no step at all.

        :param output_dir: Root of the target repository.
        :param config: The resolved configuration, for the skills location.
        :return: One text per step. A step spanning several lines indents its
            commands by two spaces against its prose.
        """
        touched = {*self.created, *self.updated}
        if not touched - EPHEMERAL_TARGETS:
            return []
        steps = ["Commit the generated files and push."]
        workflow_prefix = f"{WORKFLOW_DIR.as_posix()}/"
        if any(path.startswith(workflow_prefix) for path in touched):
            steps.append(
                "On first push, workflows will detect missing configuration and"
                " open issues\nwith setup instructions."
            )
        moved = self.upgraded_pin()
        if moved:
            previous, adopted = moved
            # A moved pin is the moment to read what the new release offers,
            # and the one this run knows both versions of. The same session then
            # audits the drift against the adopted tag.
            lines = [
                (
                    f"Apply what repomatic v{previous} to v{adopted} lets this"
                    " repository adopt or drop, then audit its drift:"
                ),
                f"  {skill_launcher(UPGRADE_SKILL, f'v{previous}', f'v{adopted}')}",
            ]
            skills = COMPONENTS_BY_NAME["skills"]
            skill_dir = skills.resolve_target(
                skills.target_of(UPGRADE_SKILL) or "", config
            )
            if not (output_dir / skill_dir / SKILL_FILENAME).is_file():
                lines.append(
                    f'The skill is not installed here: run "repomatic init'
                    f' skills/{UPGRADE_SKILL}" first, or use the Claude Code plugin.'
                )
            steps.append("\n".join(lines))
        return steps


@dataclass
class _InitRun:
    """The state one `init` run shares across the writers of its components."""

    output_dir: Path
    """Root directory of the target repository."""

    config: Config
    """The resolved `[tool.repomatic]` configuration."""

    repo: str
    """Upstream repository containing the reusable workflows."""

    given_slug: str | None
    """The `owner/name` slug the caller passed, if any."""

    result: InitResult = field(default_factory=InitResult)
    """The report the writers fill."""

    yaml_texts: dict[Path, str] = field(default_factory=dict)
    """Workflow and composite-action texts, read once and kept current.

    Filled only when a workflow component is selected. Each workflow written
    replaces its entry, so a later pass reads what the run wrote.
    """

    pin: WorkflowPin | None = None
    """The upstream pin of the generated workflows, once resolved."""

    @cached_property
    def repo_slug(self) -> str | None:
        """The `owner/name` slug of the target repository.

        Resolved on first use only: the lookup may call `gh`, and most runs
        never need it.
        """
        return self.given_slug or Metadata().repo_slug

    @cached_property
    def source_paths(self) -> list[str] | None:
        """The source paths the workflow `paths:` filters name, per the target."""
        return resolve_source_paths(self.config, read_pyproject_toml(self.output_dir))

    def label(self, target: Path) -> str:
        """Render *target* for the report, relative to the repository root.

        Every report entry and log line names a repository-relative POSIX path,
        so a run reads uniformly whatever wrote the file. Falls back to the path
        as given when *target* sits outside the root.
        """
        try:
            return target.relative_to(self.output_dir).as_posix()
        except ValueError:
            return str(target)

    def write(
        self,
        target: Path,
        content: str,
        *,
        previous: str | None = None,
        normalize: bool = True,
        verb: str = "",
    ) -> None:
        """Write *content* to *target* when it changes, and report the outcome.

        The single writer behind every text file `init` materializes.
        Normalization matters beyond cosmetics:
        {func}`~repomatic.init.removed_assets.detect_removed_assets` and
        {func}`~repomatic.tooling.runner.find_unmodified_configs` both compare
        the {func}`~repomatic.file_ops.normalize_text` form against what `init`
        writes. Both sides of the comparison are normalized, so a file that
        differs only in trailing whitespace still counts as unchanged and is left
        alone.

        :param target: Absolute path to write.
        :param content: The new text.
        :param previous: What *target* holds, when the caller read it already.
        :param normalize: Collapse trailing whitespace to a single newline. Off
            for content whose exact bytes matter (a generated workflow).
        :param verb: Overrides the log line's leading word (`Synced header`).
        """
        if previous is None and target.exists():
            previous = target.read_text(encoding="UTF-8")
        existed = previous is not None
        if normalize:
            content = normalize_text(content)
            if previous is not None:
                previous = normalize_text(previous)
        if write_if_changed(target, content, previous):
            self.result.record(self.label(target), existed=existed, verb=verb)

    def record_tree(self, created: Sequence[Path], updated: Sequence[Path]) -> None:
        """Report the files a {func}`_copy_template_tree` call wrote."""
        for path in created:
            self.result.record(self.label(path), existed=False)
        for path in updated:
            self.result.record(self.label(path), existed=True)

    def workflow_texts(self) -> dict[Path, str]:
        """The workflow texts of {attr}`yaml_texts`, composite actions left out."""
        workflows_dir = self.output_dir / WORKFLOW_DIR
        return {
            path: text
            for path, text in sorted(self.yaml_texts.items())
            if path.parent == workflows_dir
        }


@dataclass
class _Selection:
    """The components and files one run writes, and the files it reports."""

    names: set[str]
    """Components to initialize."""

    files: dict[str, set[str]] = field(default_factory=dict)
    """File-level selection from the CLI, per component. A component missing
    here has all its files selected."""

    excluded_files: dict[str, set[str]] = field(default_factory=dict)
    """File-level exclusions, per component."""

    stale_targets: list[str] = field(default_factory=list)
    """Targets of scope-excluded components that carry no file entry."""

    def include(self, component: str) -> frozenset[str] | None:
        """The files of *component* to write, or `None` for all of them."""
        selected = self.files.get(component)
        return frozenset(selected) if selected is not None else None

    def exclude(self, component: str) -> frozenset[str]:
        """The files of *component* to leave out."""
        return frozenset(self.excluded_files.get(component, ()))


def _detect_traits(output_dir: Path, repo_slug: str | None) -> RepoTraits:
    """Read the {class}`~repomatic.components.RepoTraits` of the target tree.

    :param output_dir: Root directory of the target repository.
    :param repo_slug: The `owner/name` slug of the repository, when known.
    """
    pyproject = read_pyproject_toml(output_dir)
    traits = RepoTraits(
        is_awesome=bool(repo_slug and is_awesome_repo(repo_name(repo_slug))),
        is_python=is_python_project(pyproject_data=pyproject),
        is_package=is_python_package(pyproject_data=pyproject),
        is_source=is_source_repo(output_dir),
    )
    logging.debug(f"Repository traits: {traits}")
    return traits


def _select(run: _InitRun, components: Sequence[str], traits: RepoTraits) -> _Selection:
    """Decide which components and files the run writes.

    Three exclusion mechanisms apply, in order:

    1. User config (`[tool.repomatic] exclude`/`include`). The user exclude is
       additive to the default exclusions; the user include overrides both.
       Qualified entries (`component/file`) implicitly select the parent
       component. Ephemeral components stay out of a bare init, and `include`
       cannot override that: their files are regenerated by whatever reads
       them, so a committed copy is never the one that gets used.
    2. Scope (component-level and file-level `RepoScope`). Bypassed by explicit
       CLI naming or `[tool.repomatic] include`. Scope exclusions on the
       selection apply in all repos including the source repo (an
       `AWESOME_ONLY` config should not be merged into the Python source repo's
       `pyproject.toml`). Stale-file detection is suppressed in the source repo
       so bundled data files are never flagged for deletion.
    3. Config key (component-level and file-level `config_key`). Always
       applies, even with explicit CLI naming: the user's `[tool.repomatic]`
       config is authoritative for feature flags.

    :param run: The run being prepared. Its report gets the applied exclusions.
    :param components: The CLI selection. Empty for a bare init.
    :param traits: The traits of the target repository.
    """
    config = run.config
    if components:
        selected_full, selected_files = parse_component_entries(
            list(components), context="selection"
        )
        # A bare component name overrides a file-level selection of the same
        # component: "skills" means all skills even if "skills/x" also appears.
        for name in selected_full:
            selected_files.pop(name, None)
        selection = _Selection(selected_full | set(selected_files), selected_files)
    else:
        selection = _Selection(
            set(BARE_INIT_COMPONENTS) | adopted_ongoing_configs(run.output_dir)
        )

    user_include = config.include
    include_full, include_files = (
        parse_component_entries(user_include, context="include")
        if user_include
        else (set(), {})
    )
    if not components:
        for name in sorted(EPHEMERAL_COMPONENTS & {*user_include, *include_files}):
            logging.warning(
                f"`include` lists the ephemeral component {name!r}, which is "
                "ignored: its files are regenerated on demand by the commands "
                f"that read them. Run `repomatic init {name}` to write them out."
            )
    exclude_entries = sorted(
        (
            (DEFAULT_EXCLUDED_COMPONENTS | set(config.exclude))
            - set(user_include)
            - set(include_files)
        )
        | EPHEMERAL_COMPONENTS
    )
    logging.debug(
        f"Default exclusions: {', '.join(sorted(DEFAULT_EXCLUDED_COMPONENTS))}"
    )
    if config.exclude:
        logging.debug(f"User exclude: {', '.join(config.exclude)}")
    if user_include:
        logging.debug(f"User include: {', '.join(user_include)}")
    excluded_components, selection.excluded_files = parse_component_entries(
        exclude_entries, context="exclude"
    )
    excluded_files = selection.excluded_files

    # User-configured exclusions apply only when no component is named.
    if not components:
        actually_excluded = excluded_components & selection.names
        selection.names -= excluded_components
        run.result.excluded = sorted([
            *actually_excluded,
            *(
                f"{name}/{file_id}"
                for name, file_ids in sorted(excluded_files.items())
                if name not in actually_excluded
                for file_id in sorted(file_ids)
            ),
        ])
        # Expand component-level exclusions into file-level entries, so the
        # stale-file detection is a single unified pass.
        for name in actually_excluded:
            if COMPONENTS_BY_NAME[name].files:
                excluded_files.setdefault(name, set()).update(
                    COMPONENTS_BY_NAME[name].file_ids
                )

    for comp in COMPONENTS:
        # In the source repo, the data files of a bundled component are the
        # source of truth, never stale, whatever the user config excludes.
        if traits.is_source and comp.files:
            excluded_files.pop(comp.name, None)
        # Scope is bypassed by explicit CLI naming or a config include.
        scope_bypassed = bool(components) or comp.name in include_full
        _apply_scope_and_gates(
            run, comp, traits, selection, include_files, scope_bypassed=scope_bypassed
        )
    return selection


def _apply_scope_and_gates(
    run: _InitRun,
    comp: Component,
    traits: RepoTraits,
    selection: _Selection,
    include_files: dict[str, set[str]],
    *,
    scope_bypassed: bool,
) -> None:
    """Apply the scope and the config-key gates of *comp* to *selection*.

    :param include_files: File-level entries of `[tool.repomatic] include`.
    :param scope_bypassed: Whether the user named *comp*, on the CLI or in
        `[tool.repomatic] include`.
    """
    config = run.config
    excluded_files = selection.excluded_files

    if not comp.scope.matches(traits):
        logging.debug(
            f"Scope exclusion: {comp.name} ({comp.scope.name}) not applicable to"
            f" the repository ({traits})."
        )
        if not scope_bypassed and comp.name not in include_files:
            selection.names.discard(comp.name)
            if not traits.is_source:
                if isinstance(comp, GeneratedComponent) and comp.target:
                    selection.stale_targets.append(
                        comp.resolve_target(comp.target, config)
                    )
                elif comp.files:
                    excluded_files.setdefault(comp.name, set()).update(comp.file_ids)
            return

    if comp.name in selection.names and not comp.is_enabled(config):
        selection.names.discard(comp.name)
        logging.info(
            f"[tool.repomatic] {comp.config_key} is disabled. Skipping {comp.name}."
        )

    for entry in comp.files:
        if not entry.scope.matches(traits):
            logging.debug(
                f"Scope exclusion: {comp.name}/{entry.file_id} ({entry.scope.name})"
                f" not applicable to the repository ({traits})."
            )
            if (
                not scope_bypassed
                and not traits.is_source
                and entry.file_id not in include_files.get(comp.name, set())
            ):
                excluded_files.setdefault(comp.name, set()).add(entry.file_id)
        if not traits.is_source and not entry.is_enabled(config):
            logging.debug(
                f"Config exclusion: {comp.name}/{entry.file_id}"
                f" ({entry.config_key} disabled)."
            )
            excluded_files.setdefault(comp.name, set()).add(entry.file_id)


def run_init(
    output_dir: Path,
    components: Sequence[str] = (),
    *,
    config: Config,
    version: str | None = None,
    cooldown: bool = True,
    repo: str = DEFAULT_REPO,
    repo_slug: str | None = None,
) -> InitResult:
    """Bootstrap a repository for use with `kdeldycke/repomatic`.

    Creates thin-caller workflow files, exports configuration files, and
    generates a minimal `changelog.md` if missing. Managed files (workflows,
    configs, skills) are always overwritten. The user-owned changelog is
    created once and never overwritten.

    For `awesome-*` repositories, the `awesome-template` component joins a bare
    `init` through its `AWESOME_ONLY` scope.

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
    :param config: The resolved `[tool.repomatic]` configuration of the target.
    :param version: Version pin for upstream workflows (like `v5.10.0`). When
        `None`, derived from the running package version, gated by *cooldown*.
    :param cooldown: When `True` (and *version* is unset), hold the derived pin
        back to the newest release past the `[tool.repomatic]
        minimum-release-age` window instead of pinning a fresh running version
        (see {func}`~repomatic.init.upgrade.resolve_default_pin`). Ignored when
        *version* is explicit.
    :param repo: Upstream repository containing reusable workflows.
    :param repo_slug: Repository `owner/name` slug, for the awesome-template URL
        rewriting and the changelog stub. Read from
        {class}`~repomatic.metadata.core.Metadata` on first use when omitted.
    :return: Summary of created, updated, skipped, and warned items.
    """
    run = _InitRun(output_dir, config, repo, repo_slug)
    result = run.result
    # The awesome trait only decides scope, which a named component bypasses:
    # the slug lookup is spared to such a run.
    traits = _detect_traits(output_dir, run.given_slug if components else run.repo_slug)
    selection = _select(run, components, traits)

    # Detect excluded files that still exist on disk.
    for name, file_ids in sorted(selection.excluded_files.items()):
        comp = COMPONENTS_BY_NAME[name]
        for file_id in sorted(file_ids):
            rel = comp.resolve_target(comp.target_of(file_id) or "", config)
            if rel and (output_dir / rel).exists():
                result.excluded_existing.append(rel)
    for rel in sorted(selection.stale_targets):
        if (output_dir / rel).exists():
            result.excluded_existing.append(rel)

    # Detect orphans of assets repomatic has dropped. Suppressed in the source
    # repo, where the data files for these tombstones no longer exist anyway.
    if not traits.is_source:
        result.removed_prunable, result.removed_review = detect_removed_assets(
            output_dir, config
        )

    logging.debug(f"Selected components: {', '.join(sorted(selection.names))}")

    # The pin is resolved only when a workflow is generated: config-only inits
    # (labels, bumpversion) need none and must not pay for the cooldown lookup.
    # The pin already on disk is the floor: the cooldown gates an adoption,
    # never a regeneration. It is also the previous half of the pair the closing
    # hint prints, so it is read before any file is rewritten.
    workflow_names = {
        name
        for name in selection.names
        if isinstance(COMPONENTS_BY_NAME[name], WorkflowComponent)
    }
    if workflow_names:
        run.yaml_texts = github_yaml_texts(output_dir)
        floor = highest_upstream_pin(run.yaml_texts.values(), repo)
        if floor is not None:
            result.previous_pin = floor.version
        run.pin = resolve_workflow_pin(
            config,
            version=version,
            cooldown=cooldown,
            repo=repo,
            floor=floor,
            warnings=result.warnings,
        )
        if not run.pin.adopts_content:
            selection.names -= workflow_names

    tool_configs: list[ToolConfigComponent] = []
    for comp in COMPONENTS:
        if comp.name not in selection.names:
            continue
        if isinstance(comp, ToolConfigComponent):
            # Merged in one pass below, so pyproject.toml is written once.
            tool_configs.append(comp)
            continue
        _writer(comp)(
            run, comp, selection.include(comp.name), selection.exclude(comp.name)
        )
    if tool_configs:
        _write_tool_configs(run, tool_configs)

    # Check for native tool config files identical to bundled defaults. The
    # scan roots at output_dir so `--delete-unmodified` (which joins these
    # paths against output_dir) deletes from the tree it scanned.
    for _tool_name, rel_path in find_unmodified_configs(output_dir):
        result.unmodified_configs.append(rel_path)
        logging.warning(f"Unmodified config (matches bundled default): {rel_path}")

    return result


def _write_workflows(
    run: _InitRun,
    comp: Component,
    include: frozenset[str] | None,
    exclude: frozenset[str],
) -> None:
    """Generate thin-caller workflows and sync non-reusable workflow headers.

    The `uses:` refs are pinned to `@sha # version`, from the pin `run_init`
    resolved. A `None` SHA yields a bare `@version` tag pin the next
    `sync-action-pins` run re-hardens.
    """
    config = run.config
    # Respect the `workflow.sync` opt-out: skip thin callers and headers, leaving
    # any existing workflow files untouched (matches the sibling `*.sync`
    # toggles). See {class}`~repomatic.config.WorkflowConfig`.
    if not config.workflow.sync:
        logging.info(
            "[tool.repomatic] workflow.sync is disabled. Skipping workflow sync."
        )
        return
    assert run.pin is not None, "the pin is resolved before any workflow is written"
    pin = run.pin
    run.result.adopted_pin = pin.version.removeprefix("v")

    paths_spec = PathsSpec(
        source_paths=tuple(run.source_paths or ()),
        extra_paths=list(config.workflow.extra_paths),
        ignore_paths=list(config.workflow.ignore_paths),
        workflow_paths={k: list(v) for k, v in config.workflow.paths.items()},
    )
    workflows_dir = run.output_dir / WORKFLOW_DIR
    workflows_dir.mkdir(parents=True, exist_ok=True)

    for filename in REUSABLE_WORKFLOWS:
        entry = comp.entry(filename)
        # A config-gated workflow whose toggle is off is left alone.
        if (
            (include is not None and filename not in include)
            or filename in exclude
            or (entry is not None and not entry.is_enabled(config))
        ):
            continue
        target = workflows_dir / filename
        content, existing = render_thin_caller_for_target(
            filename,
            target,
            repo=run.repo,
            version=pin.version,
            paths_spec=paths_spec,
            commit_sha=pin.commit_sha,
            config=config,
        )
        # A generated workflow is written byte-for-byte: its trailing layout is
        # the generator's business, not something to normalize here.
        run.write(target, content, previous=existing, normalize=False)
        run.yaml_texts[target] = content

    # Sync headers for non-reusable workflows that already exist on disk.
    for filename in sorted(NON_REUSABLE_WORKFLOWS):
        if (include is not None and filename not in include) or filename in exclude:
            continue
        target = workflows_dir / filename
        existing = run.yaml_texts.get(target)
        if existing is None:
            continue
        canonical_header = generate_workflow_header(
            filename, paths_spec=paths_spec, config=config
        )
        jobs_match = re.search(r"^jobs:", existing, re.MULTILINE)
        if jobs_match is None:
            continue
        content = canonical_header + existing[jobs_match.start() :]
        # The body below `jobs:` is the downstream repository's own, so only the
        # header is replaced and the file is written exactly as assembled.
        run.write(
            target, content, previous=existing, normalize=False, verb="Synced header"
        )
        run.yaml_texts[target] = content

    _realign_inline_pins(run, pin.version)
    # Runs after the realignment, so it reads the pin the workflows now carry.
    _check_metadata_keys(run, pin.version)


def _realign_inline_pins(run: _InitRun, version: str) -> None:
    """Realign inline `<package>==X.Y.Z` literals onto the pin just written.

    A workflow that reaches the upstream toolkit from a `run:` shell command
    (`uvx 'repomatic==1.2.3' show-metadata`) must keep that version in lockstep
    with the SHA-pinned `uses:` refs above it, which
    {func}`~repomatic.lint_repo.pins.check_inline_pins_match_upstream` enforces.
    `init` writes those refs and is the only writer of them, so it is also the
    only place that knows the pin changed. Realigning the literal here leaves no
    window, between the two, where that lint is red.

    Rewrites in either direction, through
    {func}`~repomatic.release.self_pin.align_inline_pins`, the rewrite
    `sync-workflow-pins` runs too: the refs are the source of truth, and a
    literal ahead of them is drift the same way one behind them is.

    Scope is every workflow file, not only the ones `init` generated. A
    downstream repository carries the literal inside job bodies it owns (the
    `pr-body` calls of a release workflow), which header-only sync never
    touches; that is exactly where the lag hides.

    Also splices in the cooldown exemption of the package
    ({func}`~repomatic.release.self_pin.cooldown_exemption`) wherever a `uvx`
    command pinning it lacks one. Every workflow exports a `UV_EXCLUDE_NEWER`
    covering all resolution, and the pin this function writes routinely names a
    release younger than that window, so realigning it without the exemption
    produces a command that cannot resolve at all. `--no-cooldown` makes that
    the normal case rather than the rare one: it adopts the running version the
    day it is published.

    The package is the name of the `--upstream-repo`, for the pin and its
    exemption alike, so a renamed fork gets an exemption naming its own package.

    ```{note}
    The exemption's own single-`=` literal (`repomatic=P0D`) is untouched by the
    version rewrite: it names a package, not a version. Splicing is idempotent,
    so a command already carrying it is left byte-identical.
    ```

    :param run: The run, whose workflow texts are read and kept current.
    :param version: Version just pinned into the `uses:` refs, in its `vX.Y.Z`
        tag spelling.
    """
    package = repo_name(run.repo)

    for target, content in run.workflow_texts().items():
        # A `uses:` ref names a git tag and keeps the `v`; a requirement
        # specifier names the PyPI package and must not carry it.
        aligned, _changes = align_inline_pins(
            content, package, version.removeprefix("v")
        )
        new_content = add_cooldown_exemption(aligned, package)
        if new_content == content:
            continue
        run.write(
            target,
            new_content,
            previous=content,
            normalize=False,
            verb=f"Realigned inline {package} pin",
        )
        run.yaml_texts[target] = new_content


def _check_metadata_keys(run: _InitRun, version: str) -> None:
    """Warn when a workflow asks `show-metadata` for a key this version dropped.

    A workflow reaches the toolkit's metadata through a `run:` command naming
    the keys it wants, and an unknown key is a hard `UsageError`. So retiring a
    key breaks every downstream workflow still asking for it, at the first push
    after the adoption, in the job every other job hangs off. A call of the
    `metadata` alias, which `8.0.0` removed, breaks the same way, and is warned
    about the same way.

    Reads the workflows through
    {func}`~repomatic.metadata.core.unknown_metadata_keys`, the scan the
    `lint-repo` check runs too.

    `init` is where the version changes, and the job bodies holding these
    commands belong to the downstream repository, so header-only sync never
    reads them. That makes this the one moment the mismatch is both introduced
    and fixable, ahead of the commit rather than after the red run.

    Scope is every workflow file, matching {func}`_realign_inline_pins`: the
    invocation hides in a job body wherever the repository chose to put it.

    ```{note}
    The key set is the running version's, read in-process, so the check applies
    only when the workflows end up pinned to that same version. A cooldown
    holding the pin back leaves the answer unknowable from here: the older
    release's key set is not importable, and judging it by this one's would
    blame a workflow that works.
    ```

    :param run: The run, whose workflow texts are read.
    :param version: Version just pinned into the workflows, `vX.Y.Z` spelling.
    """
    if not pins_running_release(version):
        logging.debug(
            f"Workflows pin {version}, not the running release: skipping the"
            " metadata-key check, whose key set is this version's."
        )
        return

    package = repo_name(run.repo)
    command = f"{package} {METADATA_SUBCOMMAND}"
    release = version.removeprefix("v")
    unknown: dict[Path, set[str]] = {}
    removed_alias: set[Path] = set()
    workflows = parse_workflow_texts(run.workflow_texts())
    for request in unknown_metadata_keys(workflows, package):
        unknown.setdefault(request.path, set()).update(request.unknown)
        if request.removed_alias:
            removed_alias.add(request.path)
    for target in sorted(unknown):
        label = run.label(target)
        if target in removed_alias:
            run.result.warn(
                f"{label} calls `{package} {REMOVED_METADATA_ALIAS}`, an alias"
                f" {package} `8.0.0` removed. The job will fail; call"
                f" `{command}` instead."
            )
        if unknown[target]:
            run.result.warn(
                f"{label} asks `{command}` for"
                f" {', '.join(repr(key) for key in sorted(unknown[target]))}, which"
                f" {package} {release} does not emit. The job will fail; drop"
                " the key or pin an older version."
            )


def _write_bundled_files(
    run: _InitRun,
    comp: Component,
    include: frozenset[str] | None,
    exclude: frozenset[str],
) -> None:
    """Export the bundled files of *comp*, at their configured location.

    A tree entry is a whole folder (a skill and its optional `scripts/`,
    `references/` and `assets/`), copied verbatim rather than rendered.
    """
    for entry in comp.files:
        if (include is not None and entry.file_id not in include) or (
            entry.file_id in exclude
        ):
            continue
        target = run.output_dir / comp.resolve_target(entry.target, run.config)
        if entry.tree:
            run.record_tree(
                *_copy_template_tree(
                    files("repomatic.data").joinpath(entry.source), target
                )
            )
            continue
        run.write(target, get_data_content(entry.source))


def _write_labels(
    run: _InitRun,
    comp: Component,
    include: frozenset[str] | None,
    exclude: frozenset[str],
) -> None:
    """Export the label definitions, then the extra label files of the config.

    The extra files come from `labels.extra-files` URLs, downloaded to an
    `extra-labels/` subdirectory. Fetched through the shared
    {func}`~repomatic.http.get_bytes` seam, so the download carries the standard
    timeout, `User-Agent` and truncation retry. These URLs come from a
    downstream repository's own config and are reached on every labels sync, so
    an unresponsive host has to fail the step rather than hang the job.
    """
    _write_bundled_files(run, comp, include, exclude)
    urls = [url.strip() for url in run.config.labels.extra_files if url.strip()]
    if not urls:
        logging.debug("No labels.extra-files configured.")
        return
    target_dir = run.output_dir / "extra-labels"
    target_dir.mkdir(exist_ok=True)
    for url in urls:
        target = target_dir / extra_label_filename(url)
        logging.info(f"Downloading {url} -> {run.label(target)}")
        data = get_bytes(url)
        existed = target.exists()
        if existed and target.read_bytes() == data:
            continue
        target.write_bytes(data)
        run.result.record(run.label(target), existed=existed)


def _copy_template_tree(
    root: Traversable,
    dest: Path,
    *,
    transform: Callable[[PurePosixPath, bytes], bytes] | None = None,
) -> tuple[list[Path], list[Path]]:
    """Recursively copy files from a traversable resource tree to disk.

    Skips {data}`TEMPLATE_TREE_SKIPPED` entries, and leaves a file whose content
    already matches untouched, so re-running is a no-op.

    Byte-based rather than routed through
    {func}`~repomatic.file_ops.write_if_changed`: a template tree can carry
    binary assets (images), which a text round-trip would corrupt.

    :param root: The bundled tree to copy.
    :param dest: The directory the tree lands in.
    :param transform: Rewrites the bytes of a file before the comparison, given
        its path relative to *root*. A file the rewrite already matches on disk
        is left alone, like any other.
    :return: `(created, updated)` lists of the paths actually written.
    """
    created: list[Path] = []
    updated: list[Path] = []
    for source, relative in _walk_tree(root):
        target = dest / relative
        data = source.read_bytes()
        if transform is not None:
            data = transform(relative, data)
        existed = target.exists()
        if existed and target.read_bytes() == data:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        (updated if existed else created).append(target)
    return created, updated


def _walk_tree(
    root: Traversable, prefix: PurePosixPath | None = None
) -> Iterator[tuple[Traversable, PurePosixPath]]:
    """Yield every file of *root* with its path relative to it, in name order.

    :param prefix: The path of *root* relative to the top of the walk.
    """
    for entry in sorted(root.iterdir(), key=lambda child: child.name):
        if entry.name in TEMPLATE_TREE_SKIPPED:
            continue
        relative = prefix / entry.name if prefix else PurePosixPath(entry.name)
        if entry.is_dir():
            yield from _walk_tree(entry, relative)
        else:
            yield entry, relative


def _write_awesome_template(
    run: _InitRun,
    comp: Component,
    include: frozenset[str] | None,
    exclude: frozenset[str],
) -> None:
    """Copy the bundled awesome-template files, with URLs naming this repository.

    The `kdeldycke/awesome-template` URLs of the `.github/` Markdown and YAML
    files are rewritten in memory, before the comparison with the copy on disk.
    So a file already carrying the rewritten URLs reads as unchanged, and a
    re-run writes nothing.

    Every copied file is recorded on the report by its own relative path, the
    way the skills and agents trees already are. Those lists are consumed as
    paths (`--delete-excluded` joins them against the output directory), so a
    roll-up entry among them would be a path that resolves nowhere.
    """
    repo_slug = run.repo_slug
    if not repo_slug:
        logging.warning(
            "No repository slug found: skipping the awesome-template URL rewrite"
            " and its files."
        )
        return

    def rewrite_urls(relative: PurePosixPath, data: bytes) -> bytes:
        if (
            relative.parts[0] != ".github"
            or relative.suffix not in AWESOME_TEMPLATE_REWRITTEN_SUFFIXES
        ):
            return data
        text = AWESOME_TEMPLATE_URL_RE.sub(f"/{repo_slug}", data.decode("UTF-8"))
        return text.encode("UTF-8")

    created, updated = _copy_template_tree(
        files("repomatic.data").joinpath("awesome_template"),
        run.output_dir,
        transform=rewrite_urls,
    )
    run.record_tree(created, updated)
    if created or updated:
        logging.info(
            f"awesome-template: {len(created)} file(s) created, {len(updated)} updated."
        )


def _changelog_stub(output_dir: Path, repo_slug: str | None) -> str:
    """The content of a new changelog: its header, then the unreleased section.

    The section is rendered by the `release-notes` template, like the section
    the `changelog` command adds after each release, so the changelog commands
    read it like any other. The version comes off `pyproject.toml`. Without a
    version or a repository to compare against, the stub is the header alone.

    :param output_dir: Root directory of the target repository.
    :param repo_slug: The `owner/name` slug of the repository, when known.
    """
    pyproject = read_pyproject_toml(output_dir)
    version = pyproject.get("project", {}).get("version") or (
        pyproject.get("tool", {}).get("bumpversion", {}).get("current_version")
    )
    if not version or not repo_slug:
        return CHANGELOG_HEADER
    section = render_template(
        "release-notes",
        version=version,
        date="unreleased",
        compare_url=f"https://github.com/{repo_slug}/compare/main...main",
        development_warning=render_template("development-warning"),
        yanked_admonition="",
        availability_admonition="",
        editorial_admonition="",
        changes="",
    )
    return f"{CHANGELOG_HEADER}\n{section}"


def _write_changelog(
    run: _InitRun,
    comp: Component,
    include: frozenset[str] | None,
    exclude: frozenset[str],
) -> None:
    """Create a minimal changelog when the repository has none.

    The changelog stub is only useful for bootstrapping new repositories. An
    existing changelog is never overwritten: it contains real release history
    that the stub would destroy. It is guarded here rather than left to
    {meth}`_InitRun.write`, which converges a file onto the given content.
    """
    assert isinstance(comp, GeneratedComponent)
    target = run.output_dir / comp.resolve_target(comp.target, run.config)
    if target.exists():
        rel = run.label(target)
        run.result.skipped.append(rel)
        logging.debug(f"Skipped existing: {rel}")
        return
    run.write(target, _changelog_stub(run.output_dir, run.repo_slug))


def _write_plugin_settings(
    run: _InitRun,
    comp: Component,
    include: frozenset[str] | None,
    exclude: frozenset[str],
) -> None:
    """Wire the repomatic Claude Code plugin into the project settings.

    Unlike the changelog stub, an existing file is merged into rather than
    skipped: it is a live settings document the repository keeps editing, and
    only the marketplace and enablement keys the plugin owns are touched. That
    also makes a re-run a no-op, reported as unchanged.
    """
    assert isinstance(comp, GeneratedComponent)
    target = run.output_dir / comp.resolve_target(comp.target, run.config)
    existed = target.is_file()
    if merge_plugin_settings(target, run.output_dir):
        run.result.record(run.label(target), existed=existed)


def _write_tool_configs(
    run: _InitRun, components: Sequence[ToolConfigComponent]
) -> None:
    """Merge the selected tool configs into `pyproject.toml`, in one write.

    The file is read once, each section merged into the text in turn, and the
    result written once. The file exists by then, so it is always reported as
    updated, whichever sections were new.
    """
    pyproject_path = run.output_dir / "pyproject.toml"
    if not pyproject_path.exists():
        run.result.warn(
            "pyproject.toml not found; skipping tool config initialization."
        )
        return
    original = pyproject_path.read_text(encoding="UTF-8")
    content = original
    for comp in components:
        merged = merge_tool_config(content, comp)
        if merged is not None:
            content = merged
    run.write(pyproject_path, content, previous=original, normalize=False)


_WRITERS_BY_NAME: dict[str, _Writer] = {
    "changelog": _write_changelog,
    "labels": _write_labels,
    "plugin": _write_plugin_settings,
}
"""Writers of the components that need their own, keyed by component name.

Each generated component is produced by code of its own, so its name picks the
producer. `labels` adds the extra label files of the config to its bundled one.
"""

_WRITERS_BY_KIND: dict[type[Component], _Writer] = {
    BundledComponent: _write_bundled_files,
    TemplateComponent: _write_awesome_template,
    WorkflowComponent: _write_workflows,
}
"""Writers of every other component, keyed by its kind.

Tool configs are absent: `run_init` merges them in one pass of its own.
"""


def _writer(comp: Component) -> _Writer:
    """The writer of *comp*.

    :raises LookupError: When no writer handles *comp*, which a component added
        to the registry without one would hit on its first selection.
    """
    writer = _WRITERS_BY_NAME.get(comp.name) or _WRITERS_BY_KIND.get(type(comp))
    if writer is None:
        msg = f"No init writer handles the {comp.name!r} component."
        raise LookupError(msg)
    return writer
