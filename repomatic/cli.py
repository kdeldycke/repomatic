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

from __future__ import annotations

import functools
import logging
import os
import re
import subprocess
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

from click_extra import (
    UNPROCESSED,
    Choice,
    ClickException,
    Context,
    EnumChoice,
    FloatRange,
    IntRange,
    ParameterSource,
    ParamType,
    Section,
    SortByOption,
    UsageError,
    argument,
    dir_path,
    echo,
    file_path,
    get_tool_config,
    group,
    jobs_option,
    option,
    option_group,
    pass_context,
    style,
)
from extra_platforms import is_github_ci

from .binaries_page import (
    BINARY_ASSET_SUFFIXES,
    render_binaries_csv,
    render_chart_section,
    update_binaries_csv,
    update_binaries_page,
)
from .binary import (
    NUITKA_BUILD_TARGETS,
    verify_binary_arch,
    verify_binary_floor,
)
from .broken_links import manage_combined_broken_links_issue
from .cache import (
    cache_dir as _cache_dir,
    cache_info,
    clear_cache,
    clear_config_cache,
    clear_http_cache,
    config_cache_info,
    http_cache_info,
)
from .changelog import Changelog, lint_changelog_dates
from .checksums import update_registry_checksums
from .config import (
    CONFIG_REFERENCE_HEADER_DEFS,
    Config,
    config_reference,
    escape_type_for_gfm_table,
)
from .dep_report import (
    format_released,
    format_upload_date,
)
from .deps_graph import (
    SubgraphKind,
    generate_dependency_graph,
    resolve_subgraph_selection,
)
from .docs import update_docs as _update_docs
from .git_ops import commit_and_push_files, create_and_push_tag
from .github import token as _token_mod, unsubscribe as _unsub_mod
from .github.actions import cancel_superseded_runs, format_multiline_output
from .github.dev_release import (
    cleanup_dev_releases as _cleanup_dev_releases,
    sync_dev_release as _sync_dev_release,
)
from .github.pr import close_open_prs_on_branch
from .github.pr_body import (
    build_pr_body,
    generate_pr_metadata_block,
    generate_refresh_tip,
    get_template_names,
    render_commit_message,
    render_template,
    render_title,
    template_args,
    template_docs_url,
)
from .github.release_sync import (
    render_sync_report as _render_sync_report,
    sync_github_releases as _sync_github_releases,
)
from .github.releases import (
    GitHubReleasesUnavailable,
    get_releases_with_assets,
    owner_repo,
)
from .github.sponsor import (
    add_sponsor_label,
    get_default_author,
    get_default_number,
    get_default_owner,
    is_pull_request,
    is_sponsor,
)
from .github.unsubscribe import (
    render_report as _render_report,
    unsubscribe_threads as _unsubscribe_threads,
)
from .github.workflow_sync import run_workflow_lint
from .gitignore import build_gitignore
from .images import (
    DEFAULT_MIN_SAVINGS_BYTES,
    DEFAULT_MIN_SAVINGS_PCT,
    format_file_size,
    generate_markdown_summary,
    optimize_images,
)
from .init_project import export_content, find_all_unmodified_configs, run_init
from .labels import apply_labels
from .lint_repo import (
    run_repo_lint,
)
from .mailmap import Mailmap
from .metadata import (
    METADATA_KEYS_HEADER_DEFS,
    Dialect,
    Metadata,
    all_metadata_keys,
    is_version_bump_allowed,
    metadata_keys_reference,
)
from .prepare_release import PrepareRelease
from .pyproject import get_project_name
from .registry import (
    ALL_COMPONENTS,
    COMPONENT_HELP_TABLE,
    COMPONENTS_BY_NAME,
    DEFAULT_REPO,
    FILE_SELECTOR_COMPONENTS,
    SKILL_PHASE_ORDER,
    SKILL_PHASES,
    parse_component_entries,
    valid_file_ids,
)
from .setup_guide import manage_setup_guide
from .sync_ops import (
    OPERATIONS_BY_NAME,
    ResolveContext,
    SyncPlan,
    render_plan_markdown,
    run_sync_operations,
    selected_operations,
)
from .tool_registry import (
    TOOL_REGISTRY,
    generated_header,
)
from .tool_runner import (
    resolve_config_source,
    run_tool,
)
from .virustotal import (
    load_scan_records,
    poll_detection_stats,
    records_from_release_notes,
    records_from_results,
    scan_files,
    upsert_scan_records,
)
from .vulnerable_deps import (
    AdvisorySource,
    collect_vulnerable_packages,
    fix_vulnerable_deps as _fix_vulnerable_deps,
    format_vulnerability_table,
)

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import IO

    from .dep_report import BypassForecast, HeldBackPackage


output_format_option = option(
    "--output-format",
    type=Choice(["markdown", "github-actions"]),
    default="markdown",
    help=(
        "Format for --output."
        " github-actions produces format for PR template"
        " consumption in workflows."
    ),
)

# Shared opt-in flags for the three version-sync updaters (sync-tool-versions,
# sync-action-pins, sync-workflow-pins), mirroring sync-uv-lock. Held-back is
# free here (the candidates are already fetched), so it defaults on; release
# notes cost GitHub API calls, so they stay opt-in and CI passes the flag.
sync_release_notes_option = option(
    "--release-notes/--no-release-notes",
    default=False,
    help="Fetch release notes from GitHub (markdown, appended after the table).",
)
sync_held_back_option = option(
    "--held-back/--no-held-back",
    default=True,
    help="Report newer releases withheld by the minimum-release-age cooldown.",
)
lockfile_option = option(
    "--lockfile",
    type=file_path(resolve_path=True),
    default="uv.lock",
    help="Path to the uv.lock file.",
)


def is_stdout(filepath: Path) -> bool:
    """Check if a file path is set to stdout.

    Prevents the creation of a `-` file in the current directory.
    """
    return str(filepath) == "-"


def prep_path(filepath: Path) -> IO:
    """Prepare the output file parameter for Click's echo function.

    Always returns a UTF-8 encoded file object, including for stdout. This avoids
    `UnicodeEncodeError` on Windows where the default stdout encoding is `cp1252`.

    For non-stdout paths, parent directories are created automatically if they don't
    exist. This absorbs the `mkdir -p` step that workflows previously had to do.

    ```{note}
    When stdout is a captured in-memory stream with no backing file descriptor
    (Click's test runner, the Sphinx `{click:run}` directive that live-renders CLI
    output in the docs), `fileno()` raises and we write to the stream directly. Such
    streams are already Python text objects, so the Windows `cp1252` concern does not
    apply: that only bites a real terminal, which always has a descriptor.
    ```
    """
    if is_stdout(filepath):
        try:
            fd = sys.stdout.fileno()
        except (OSError, ValueError):
            return sys.stdout
        return open(fd, "w", encoding="UTF-8", closefd=False)
    filepath.parent.mkdir(parents=True, exist_ok=True)
    return filepath.open("w", encoding="UTF-8")


def exit_if_disabled(ctx: Context, enabled: bool, key: str) -> None:
    """Exit successfully when a `[tool.repomatic]` feature flag is off.

    The shared guard of every sync command: a disabled feature is a normal,
    configured state, so the command logs the flag and exits `0` instead of
    failing the workflow that invoked it.

    :param ctx: The Click context to exit through.
    :param enabled: The resolved feature flag value.
    :param key: The `[tool.repomatic]` key, in kebab-case, for the log line.
    """
    if not enabled:
        logging.info(f"[tool.repomatic] {key} is disabled. Skipping.")
        ctx.exit(0)


def emit_report(
    body: str,
    output: Path | None,
    output_format: str,
    key: str = "diff_table",
) -> None:
    """Write a markdown report to `--output`, optionally as a step output.

    The shared tail of every report-producing command: nothing is written
    when no output path is set or the body is empty; with
    `--output-format github-actions` the body is wrapped as a heredoc step
    output variable named *key* for `$GITHUB_OUTPUT` consumption.

    :param body: The markdown report.
    :param output: The `--output` path (`None` to skip, `-` for stdout).
    :param output_format: `markdown` or `github-actions`.
    :param key: The step output variable name for the `github-actions` format.
    """
    if output is None or not body:
        return
    if output_format == "github-actions":
        content = format_multiline_output(key, body)
    else:
        content = body
    echo(content, file=prep_path(output))


def generate_header(ctx: Context) -> str:
    """Generate metadata to be left as comments to the top of a file generated by
    this CLI.
    """
    header = generated_header(ctx.command_path)
    logging.debug(f"Generated header:\n{header}")
    return header


def remove_header(content: str) -> str:
    """Return content without blank lines and header metadata from above."""
    logging.debug(f"Removing header from:\n{content}")
    lines = []
    still_in_header = True
    for line in content.splitlines():
        if still_in_header:
            # We are still in the header as long as we have blank lines or we have
            # comment lines matching the format produced by the method above.
            if not line.strip() or line.startswith((
                "# Generated by ",
                "# Timestamp: ",
            )):
                continue
            else:
                still_in_header = False
        # We are past the header, so keep all the lines: we have nothing left to remove.
        lines.append(line)

    headerless_content = "\n".join(lines)
    logging.debug(f"Result of header removal:\n{headerless_content}")
    return headerless_content


def _require_token(module, attr):
    """Decorator that runs a token validator before the Click command body.

    Uses late-bound `getattr(module, attr)` so that
    `unittest.mock.patch` can replace the module attribute after import
    and the decorator sees the mock at call time.
    """

    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            try:
                getattr(module, attr)()
            except RuntimeError as exc:
                raise ClickException(str(exc))
            return func(*args, **kwargs)

        return wrapper

    return decorator


# included_params=() disables merge_default_map: all [tool.repomatic] keys are
# config-only (not CLI params), so merging them would collide with subcommand
# names (e.g., "setup-guide" is both a config key and a subcommand). Config
# access goes exclusively through config_schema + get_tool_config().
@group(config_schema=Config, schema_strict=False, included_params=())
@jobs_option()
def repomatic():
    pass


_section_github = Section("GitHub issues & PRs")
_section_lint = Section("Linting & checks")
_section_release = Section("Release & versioning")
_section_setup = Section("Project setup")
_section_sync = Section("Sync")


class ComponentSelector(ParamType):
    """Accepts bare component names or qualified `component/file` selectors.

    Bare names (e.g., `skills`) select an entire component.  Qualified
    entries (e.g., `skills/repomatic-topics`) select a single file within
    a component.  Validation delegates to
    {func}`~repomatic.registry.parse_component_entries`, the same code path
    the `exclude` and `include` config options go through, so the CLI and
    config agree on syntax and error messages.
    """

    name = "selector"

    def get_metavar(self, param, ctx=None):
        return "[COMPONENT[/FILE]]"

    def convert(self, value, param, ctx):
        try:
            parse_component_entries([value], context="selection")
        except ValueError as e:
            self.fail(str(e), param, ctx)
        return value

    def shell_complete(self, ctx, param, incomplete):
        from click.shell_completion import CompletionItem

        completions: list[CompletionItem] = [
            CompletionItem(name)
            for name in sorted(ALL_COMPONENTS)
            if name.startswith(incomplete)
        ]
        if "/" in incomplete:
            comp_part = incomplete.split("/", 1)[0]
            for key in ALL_COMPONENTS:
                if key.lower() == comp_part.lower():
                    for fid in sorted(valid_file_ids(key)):
                        qualified = f"{key}/{fid}"
                        if qualified.startswith(incomplete):
                            completions.append(CompletionItem(qualified))
        return completions


def _unlink_with_empty_parents(target: Path, root: Path) -> None:
    """Delete `target`, then prune now-empty parent directories up to `root`."""
    target.unlink()
    parent = target.parent
    while parent != root:
        try:
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent


def _removed_line(path: str, successor: str, color: str) -> str:
    """Format one removed-asset report line, with an optional successor note."""
    note = style(f"  ({successor})", dim=True) if successor else ""
    return f"  {style(path, fg=color)}{note}"


@repomatic.command(
    name="init",
    short_help="Bootstrap a repository to use reusable workflows",
    section=_section_setup,
)
@argument(
    "components",
    nargs=-1,
    type=ComponentSelector(),
)
@option(
    "--version",
    "version_pin",
    default=None,
    help="Version pin for upstream workflows (e.g., v5.10.0). "
    "Defaults to the latest release derived from the package version.",
)
@option(
    "--repo",
    default=DEFAULT_REPO,
    help="Upstream repository containing reusable workflows.",
)
@option(
    "--output-dir",
    type=dir_path(resolve_path=True),
    default=".",
    help="Root directory of the target repository.",
)
@option(
    "--delete-excluded",
    is_flag=True,
    default=False,
    help="Delete files that are excluded by config but still on disk.",
)
@option(
    "--delete-unmodified",
    is_flag=True,
    default=False,
    help="Delete config files identical to bundled defaults.",
)
@option(
    "--keep-removed",
    is_flag=True,
    default=False,
    help="Keep orphaned files of assets repomatic no longer ships "
    "(report them instead of auto-pruning).",
)
@option(
    "--delete-removed-modified",
    is_flag=True,
    default=False,
    help="Also delete orphaned files of removed assets that were modified "
    "locally (normally reported for manual review, never deleted).",
)
def init_project(
    components,
    version_pin,
    repo,
    output_dir,
    delete_excluded,
    delete_unmodified,
    keep_removed,
    delete_removed_modified,
):
    """Bootstrap a repository to use reusable workflows from kdeldycke/repomatic.

    With no arguments, generates thin-caller workflow files, exports
    configuration files (labels, labeller rules), and creates a
    minimal changelog. Specify COMPONENTS to initialize only selected parts.

    Scope restrictions (awesome-only, non-awesome) and [tool.repomatic]
    exclude entries only apply during bare init (no arguments). Explicitly
    naming a component bypasses scope, allowing workflows to materialize
    out-of-scope configs at runtime.

    Selectors use the same syntax as the exclude config in
    [tool.repomatic]: bare names select an entire component, qualified
    component/file entries select a single file.

    \b
    Components:
    {component_table}

    \b
    File-level selectors ({file_selector_names}):
        workflows/autofix.yaml    A single workflow
        skills/repomatic-topics   A single skill
        labels/labels.toml        A single label config file

    \b
    Examples:
        # Full bootstrap (workflows + labels + changelog)
        repomatic init

    \b
        # Pin to a specific version
        repomatic init --version v5.9.1

    \b
        # Install a single skill
        repomatic init skills/repomatic-topics

    \b
        # One workflow + all labels
        repomatic init workflows/autofix.yaml labels

    \b
        # Only merge ruff config into pyproject.toml
        repomatic init ruff

    \b
        # Multiple components
        repomatic init ruff bumpversion

    """
    if keep_removed and delete_removed_modified:
        raise UsageError(
            "--keep-removed and --delete-removed-modified are mutually exclusive."
        )

    result = run_init(
        output_dir=output_dir,
        components=components,
        version=version_pin,
        repo=repo,
        config=get_tool_config(),
    )

    # Print summary.
    if result.excluded:
        echo(
            style("Excluded by config: ", dim=True)
            + ", ".join(style(e, fg="yellow") for e in result.excluded)
        )
    if result.created:
        echo(style(f"Created {len(result.created)} file(s):", fg="green", bold=True))
        for path in result.created:
            echo(f"  {style(path, fg='green')}")
    if result.updated:
        echo(
            style(
                f"Updated {len(result.updated)} existing file(s):",
                fg="yellow",
                bold=True,
            )
        )
        for path in result.updated:
            echo(f"  {style(path, fg='yellow')}")
    if result.skipped:
        echo(
            style(
                f"Skipped {len(result.skipped)} existing file(s) (never overwritten):",
                dim=True,
            )
        )
        for path in result.skipped:
            echo(f"  {style(path, dim=True)}")
    if result.excluded_existing:
        if delete_excluded:
            for path in result.excluded_existing:
                _unlink_with_empty_parents(output_dir / path, output_dir)
            echo(
                style(
                    f"Deleted {len(result.excluded_existing)} excluded"
                    " file(s) still on disk:",
                    fg="red",
                    bold=True,
                )
            )
        else:
            echo(
                style(
                    f"Excluded: {len(result.excluded_existing)} file(s) still on disk",
                    fg="red",
                )
                + style(" (use --delete-excluded to remove):", dim=True)
            )
        for path in result.excluded_existing:
            echo(f"  {style(path, fg='red')}")
    if result.unmodified_configs:
        if delete_unmodified:
            for path in result.unmodified_configs:
                (output_dir / path).unlink()
            echo(
                style(
                    f"Deleted {len(result.unmodified_configs)} unmodified"
                    " file(s) identical to bundled defaults:",
                    fg="red",
                    bold=True,
                )
            )
        else:
            echo(
                style(
                    f"Unmodified: {len(result.unmodified_configs)} file(s)"
                    " identical to bundled defaults",
                    fg="cyan",
                )
                + style(" (use --delete-unmodified to remove):", dim=True)
            )
        for path in result.unmodified_configs:
            echo(f"  {style(path, fg='cyan' if not delete_unmodified else 'red')}")
    if result.removed_prunable or result.removed_review:
        if result.removed_prunable and not keep_removed:
            for path, _ in result.removed_prunable:
                _unlink_with_empty_parents(output_dir / path, output_dir)
            echo(
                style(
                    f"Pruned {len(result.removed_prunable)} removed-upstream"
                    " file(s) (unmodified orphans):",
                    fg="red",
                    bold=True,
                )
            )
            for path, successor in result.removed_prunable:
                echo(_removed_line(path, successor, "red"))
        elif result.removed_prunable:
            echo(
                style(
                    f"Removed upstream: {len(result.removed_prunable)} unmodified"
                    " orphan(s) on disk",
                    fg="red",
                )
                + style(" (--keep-removed set; delete manually):", dim=True)
            )
            for path, successor in result.removed_prunable:
                echo(_removed_line(path, successor, "red"))
        if result.removed_review:
            if delete_removed_modified:
                for path, _ in result.removed_review:
                    _unlink_with_empty_parents(output_dir / path, output_dir)
                echo(
                    style(
                        f"Force-deleted {len(result.removed_review)} removed-upstream"
                        " file(s) (locally modified):",
                        fg="red",
                        bold=True,
                    )
                )
                for path, successor in result.removed_review:
                    echo(_removed_line(path, successor, "red"))
            else:
                echo(
                    style(
                        f"Review manually: {len(result.removed_review)} removed-upstream"
                        " file(s) modified since repomatic shipped them:",
                        fg="yellow",
                    )
                )
                for path, successor in result.removed_review:
                    echo(_removed_line(path, successor, "yellow"))
    if result.warnings:
        for warning in result.warnings:
            echo(style("Warning: ", fg="yellow", bold=True) + warning)

    has_changes = result.created or result.updated
    if has_changes:
        echo("")
        echo(style("Next steps:", bold=True))
        step = 1
        echo(f"  {step}. Commit the generated files and push.")
        step += 1
        workflows_touched = any(
            p.startswith(".github/workflows/")
            for p in (*result.created, *result.updated)
        )
        if workflows_touched:
            echo(
                f"  {step}. On first push, workflows will detect missing"
                " configuration and open issues"
            )
            echo("     with setup instructions.")


# Format the init_project help text with registry-generated content. Click
# captures the docstring into the command's help attribute at decoration time,
# so we must patch help directly rather than __doc__.
assert init_project.help is not None
init_project.help = init_project.help.format(
    component_table=COMPONENT_HELP_TABLE,
    file_selector_names=", ".join(FILE_SELECTOR_COMPONENTS),
)


_metadata_sort = SortByOption(*METADATA_KEYS_HEADER_DEFS, default="key")


@repomatic.command(
    short_help="Output project metadata",
    section=_section_setup,
    params=[_metadata_sort],
)
@option(
    "--format",
    type=EnumChoice(Dialect),
    default=Dialect.github,
    help="Rendering format of the metadata.",
)
@option(
    "--overwrite/--no-overwrite",
    "--force/--no-force",
    "--replace/--no-replace",
    default=True,
    help="Overwrite output file if it already exists.",
)
@option(
    "-o",
    "--output",
    type=file_path(writable=True, resolve_path=True, allow_dash=True),
    default="-",
    help="Output file path. Defaults to stdout.",
)
@option(
    "--list-keys",
    is_flag=True,
    default=False,
    help="List all available metadata keys with descriptions and exit.",
)
@argument("keys", nargs=-1)
@pass_context
def metadata(ctx, format, overwrite, output, list_keys, keys):
    """Dump project metadata to a file.

    Prints all metadata keys to stdout by default. Use --output to write to
    a file. Pass key names as arguments to filter output.

    \b
    Examples:
        repomatic metadata current_version is_python_project
        repomatic metadata --list-keys
        repomatic metadata --format github-json --output "$GITHUB_OUTPUT" \\
            current_version is_python_project
    """
    if list_keys:
        ctx.print_table(metadata_keys_reference(), METADATA_KEYS_HEADER_DEFS)
        ctx.exit(0)

    # Validate requested keys.
    if keys:
        valid_keys = all_metadata_keys()
        unknown = sorted(set(keys) - valid_keys)
        if unknown:
            raise UsageError(
                f"Unknown metadata key(s): {', '.join(unknown)}. "
                "Use --list-keys to see all available keys."
            )

    if is_stdout(output):
        # The overwrite flag is moot for stdout. Warn only when the user set it
        # explicitly: firing on the default value warns on every bare stdout run.
        if (
            overwrite
            and ctx.get_parameter_source("overwrite") is not ParameterSource.DEFAULT
        ):
            logging.warning("Ignore the --overwrite/--force/--replace option.")
        logging.info(f"Print metadata to {sys.stdout.name}")
    else:
        logging.info(f"Dump all metadata to {output}")

        if output.exists():
            msg = "Target file exists and will be overwritten."
            if overwrite:
                logging.warning(msg)
            else:
                logging.critical(msg)
                ctx.exit(2)

    meta = Metadata()

    # Output a warning in GitHub runners if metadata are not saved to $GITHUB_OUTPUT.
    # Skip the warning for stdout: writing to stdout is a legitimate use case
    # (human inspection, --format json piping, Sphinx docs rendering) even in CI.
    if is_github_ci() and not is_stdout(output):
        env_file = os.getenv("GITHUB_OUTPUT")
        if env_file and Path(env_file) != output:
            logging.warning(
                "Output path is not the same as $GITHUB_OUTPUT environment variable,"
                " which is generally what we're looking to do in GitHub CI runners for"
                " other jobs to consume the produced metadata."
            )

    content = meta.dump(dialect=format, keys=keys)

    # When writing to a file, copy the content to stderr so the computed
    # metadata is visible in CI logs without an extra debug step.
    if not is_stdout(output):
        echo(content, err=True)

    echo(content, file=prep_path(output))


_show_config_sort = SortByOption(*CONFIG_REFERENCE_HEADER_DEFS, default="option")


@repomatic.command(
    name="show-config",
    short_help="Print [tool.repomatic] configuration reference",
    section=_section_setup,
    params=[_show_config_sort],
)
@pass_context
def show_config(ctx):
    """Print the [tool.repomatic] configuration reference table.

    Renders a table of all available options, their types, defaults,
    and descriptions — generated from the Config dataclass docstrings.
    Respects the global --table-format and --sort-by options.
    """
    rows = [
        (option, escape_type_for_gfm_table(ftype), default, desc)
        for option, ftype, default, desc in config_reference()
    ]
    ctx.print_table(rows, CONFIG_REFERENCE_HEADER_DEFS)


TEST_MATRIX_STATE_DISPLAY = {
    "stable": "✅ stable",
    "unstable": "⁉️ unstable",
}
"""Emoji-decorated labels for job states in the `show-test-matrix` grid."""


@repomatic.command(
    name="show-test-matrix",
    short_help="Render the CI test matrix as a grid",
    section=_section_setup,
)
@option(
    "--emoji/--no-emoji",
    default=True,
    help="Decorate cells with a status emoji. Use --no-emoji for plain words.",
)
@argument(
    "matrix_name",
    metavar="[full|pr]",
    type=Choice(["full", "pr"]),
    default="full",
    required=False,
)
@pass_context
def show_test_matrix(ctx, emoji, matrix_name):
    """Render the computed CI test matrix as a Python-version by OS grid.

    Each cell shows whether that combination runs as a stable or unstable
    (continue-on-error) job, or is absent from the matrix. Pass "full" for the
    push and schedule matrix (the default), or "pr" for the reduced
    pull-request matrix. Respects the global --table-format option.

    \b
    Examples:
        repomatic show-test-matrix
        repomatic show-test-matrix pr --no-emoji
        repomatic --table-format github show-test-matrix full
    """
    meta = Metadata()
    matrix = meta.test_matrix if matrix_name == "full" else meta.test_matrix_pr
    col_values, rows = matrix.pivot()
    headers = ("Python", *col_values)
    if emoji:
        rows = tuple(
            (row[0], *(TEST_MATRIX_STATE_DISPLAY.get(cell, cell) for cell in row[1:]))
            for row in rows
        )
    ctx.print_table(rows, headers)


@repomatic.command(
    short_help="Maintain a Markdown-formatted changelog", section=_section_release
)
@option(
    "--source",
    type=file_path(exists=True, readable=True, resolve_path=True),
    default=None,
    help="Changelog source file. Defaults to the configured changelog.location.",
)
@argument(
    "changelog_path",
    type=file_path(writable=True, resolve_path=True, allow_dash=True),
    default="-",
)
@pass_context
def changelog(ctx, source, changelog_path):
    """Stamp the changelog with the current version's release header."""
    if source is None:
        source = Path(get_tool_config(ctx).changelog_location)
    initial_content = None
    if source.exists():
        logging.info(f"Read initial changelog from {source}")
        initial_content = source.read_text(encoding="UTF-8")

    changelog = Changelog(initial_content, Metadata.get_current_version())
    content = changelog.update()
    if content == initial_content:
        logging.warning("Changelog already up to date. Do nothing.")
        ctx.exit()

    if is_stdout(changelog_path):
        logging.info(f"Print updated results to {sys.stdout.name}")
    else:
        logging.info(f"Save updated results to {changelog_path}")
    echo(content, file=prep_path(changelog_path))


@repomatic.command(short_help="Prepare files for a release", section=_section_release)
@option(
    "--changelog",
    "changelog_path",
    type=file_path(exists=True, readable=True, writable=True, resolve_path=True),
    default=None,
    help="Path to the changelog file. Defaults to the configured changelog.location.",
)
@option(
    "--citation",
    "citation_path",
    type=file_path(readable=True, writable=True, resolve_path=True),
    default="citation.cff",
    help="Path to the citation file.",
)
@option(
    "--workflow-dir",
    type=dir_path(resolve_path=True),
    default=".github/workflows",
    help="Path to the GitHub workflows directory.",
)
@option(
    "--default-branch",
    default="main",
    help="Name of the default branch for workflow URL updates.",
)
@option(
    "--update-workflows/--no-update-workflows",
    default=None,
    help="Update workflow URLs to use versioned tag instead of default branch."
    " Defaults to True when $GITHUB_REPOSITORY is the canonical workflows repo.",
)
@option(
    "--post-release",
    is_flag=True,
    default=False,
    help="Run post-release steps (retarget workflow URLs to default branch).",
)
@pass_context
def prepare_release(
    ctx,
    changelog_path,
    citation_path,
    workflow_dir,
    default_branch,
    update_workflows,
    post_release,
):
    """Prepare files for a release or post-release version bump.

    This command consolidates all release preparation steps:

    \b
    - Set release date in changelog (replaces "(unreleased)" with today's date).
    - Set release date in citation.cff.
    - Update changelog comparison URL from "...main" to "...v{version}".
    - Remove the "[!WARNING]" development warning block from changelog.
    - Optionally update workflow URLs to use versioned tag.

    \b
    When running in GitHub Actions, --update-workflows is auto-detected:
    it defaults to True when $GITHUB_REPOSITORY matches the canonical
    workflows repository (kdeldycke/repomatic).

    For post-release (after the release commit), use --post-release to retarget
    workflow URLs back to the default branch.

    \b
    Examples:
        # Prepare release (changelog + citation)
        repomatic prepare-release
        # Post-release: retarget workflows to main branch
        repomatic prepare-release --post-release
    """
    # Auto-detect --update-workflows from CI context.
    if update_workflows is None:
        repo_slug = Metadata().repo_slug
        update_workflows = repo_slug == DEFAULT_REPO
        if update_workflows:
            logging.info(
                f"Auto-detected --update-workflows: repo_slug={repo_slug!r}"
                f" matches canonical repo {DEFAULT_REPO!r}"
            )

    if changelog_path is None:
        changelog_path = Path(Config.changelog_location).resolve()
    prep = PrepareRelease(
        changelog_path=changelog_path,
        citation_path=citation_path if citation_path.exists() else None,
        workflow_dir=workflow_dir,
        default_branch=default_branch,
    )

    if post_release:
        modified = prep.post_release(update_workflows=update_workflows)
        action = "Post-release"
    else:
        modified = prep.prepare_release(update_workflows=update_workflows)
        action = "Release preparation"

    if modified:
        logging.info(f"{action} complete. Modified {len(modified)} file(s):")
        for path in modified:
            echo(f"  {path}")
    else:
        logging.warning(f"{action}: no files were modified.")


@repomatic.command(
    short_help="Check if a version bump is allowed", section=_section_release
)
@option(
    "--part",
    type=Choice(["minor", "major"], case_sensitive=False),
    required=True,
    help="The version part to check for bump eligibility.",
)
def check_version(part: str) -> None:
    """Check if a version bump is allowed for the specified part.

    Compares the current version from pyproject.toml against the latest Git
    tag to detect if a bump has already been applied but not released.
    Prints "true" if allowed, "false" otherwise.

    \b
    Examples:
        repomatic check-version --part minor
        repomatic check-version --part major
    """
    allowed = is_version_bump_allowed(part)  # type: ignore[arg-type]
    echo("true" if allowed else "false")


@repomatic.command(short_help="Close a stale version-bump PR", section=_section_release)
@option(
    "--part",
    type=Choice(["minor", "major"], case_sensitive=False),
    required=True,
    help="The version part whose bump PR should be reconciled.",
)
def close_stale_bump_pr(part: str) -> None:
    """Close the minor/major version-increment PR when a bump is no longer allowed.

    The changelog workflow's bump-version job opens a draft PR on the
    "<part>-version-increment" branch whenever a bump is allowed. A scheduled
    run that started before a competing push can open this PR against a main
    branch that has already advanced past the target, leaving an orphan that
    subsequent scheduled runs cannot refresh.

    This command reconciles that state: it re-evaluates the gate against the
    current checkout and, when the bump is no longer allowed, closes any open
    PR on the matching branch (deleting the branch). When the bump is still
    allowed, it leaves the PR alone so the standard bump flow can update it.

    Idempotent: a no-op when no open PR exists on the target branch.

    \b
    Examples:
        repomatic close-stale-bump-pr --part minor
        repomatic close-stale-bump-pr --part major
    """
    if is_version_bump_allowed(part):  # type: ignore[arg-type]
        logging.info(f"{part} bump still allowed, leaving any open PR untouched.")
        return
    branch = f"{part}-version-increment"
    closed = close_open_prs_on_branch(
        branch,
        comment=(
            f"Closing stale {part} version-bump PR: `main` already advanced "
            f"past the target version, so this branch no longer represents a "
            f"valid bump."
        ),
    )
    if closed:
        logging.info(f"Closed {len(closed)} stale {part} bump PR(s): {closed}")


@repomatic.command(
    short_help="Sync .gitignore from gitignore.io templates", section=_section_sync
)
@option(
    "--output",
    "output_path",
    type=file_path(writable=True, resolve_path=True, allow_dash=True),
    default=None,
    help=("Output path. Defaults to gitignore-location from [tool.repomatic] config."),
)
@pass_context
def sync_gitignore(ctx: Context, output_path: Path | None) -> None:
    """Sync a .gitignore file from gitignore.io templates.

    Fetches templates for a base set of categories plus any extras from
    [tool.repomatic] config, then appends gitignore-extra-content.
    Writes to the path specified by gitignore-location (default
    ./.gitignore).

    \b
    Examples:
        # Generate .gitignore using config from pyproject.toml
        repomatic sync-gitignore

    \b
        # Write to custom location
        repomatic sync-gitignore --output ./custom/.gitignore

    \b
        # Preview on stdout
        repomatic sync-gitignore --output -
    """
    config = get_tool_config(ctx)
    exit_if_disabled(ctx, config.gitignore.sync, "gitignore.sync")

    content = build_gitignore(config)

    # Resolve output path.
    if output_path is None:
        output_path = Path(config.gitignore.location)

    if is_stdout(output_path):
        logging.info(f"Print to {sys.stdout.name}")
    else:
        logging.info(f"Write to {output_path}")

    echo(content.rstrip(), file=prep_path(output_path))


@repomatic.command(
    short_help="Sync GitHub release notes from changelog",
    section=_section_sync,
)
@option(
    "--dry-run/--live",
    default=True,
    help="Report what would be done without making changes.",
)
def sync_github_releases(dry_run: bool) -> None:
    """Sync GitHub release notes from changelog.md.

    Compares each GitHub release body against the corresponding
    changelog.md section and updates any that have drifted.

    \b
    Examples:
        # Dry run to preview what would be updated
        repomatic sync-github-releases --dry-run

    \b
        # Update drifted release notes
        repomatic sync-github-releases --live
    """
    changelog_path = Path(Config.changelog_location)
    if not changelog_path.exists():
        logging.warning(f"{changelog_path} not found.")
        return

    changelog = Changelog(changelog_path.read_text(encoding="UTF-8"))
    repo_url = changelog.extract_repo_url()
    if not repo_url:
        logging.warning("Could not extract repository URL from changelog.")
        return

    result = _sync_github_releases(repo_url, changelog_path, dry_run)
    echo(_render_sync_report(result))


@repomatic.command(
    short_help="Sync rolling dev pre-release on GitHub",
    section=_section_sync,
)
@option(
    "--dry-run/--live",
    default=True,
    help="Report what would be done without making changes.",
)
@option(
    "--delete/--no-delete",
    default=False,
    help="Delete-only mode: remove the dev pre-release without recreating.",
)
@option(
    "--upload-assets",
    type=dir_path(exists=True, resolve_path=True),
    default=None,
    help="Directory containing assets (binaries, packages) to upload.",
)
@pass_context
def sync_dev_release(
    ctx: Context,
    dry_run: bool,
    delete: bool,
    upload_assets: Path | None,
) -> None:
    """Sync a rolling dev pre-release on GitHub.

    Maintains a single pre-release that mirrors the unreleased changelog
    section. The dev tag is force-updated to point to the latest main
    commit.

    In --delete mode, removes the dev pre-release without recreating
    it. This is used during real releases to clean up.

    \b
    Examples:
        # Dry run to preview what would be synced
        repomatic sync-dev-release --dry-run

    \b
        # Create or update the dev pre-release
        repomatic sync-dev-release --live

    \b
        # Create or update with asset upload
        repomatic sync-dev-release --live --upload-assets release_assets/

    \b
        # Delete the dev pre-release (e.g. during a real release)
        repomatic sync-dev-release --live --delete
    """
    config = get_tool_config(ctx)
    exit_if_disabled(ctx, config.dev_release_sync, "dev-release.sync")

    if delete and upload_assets:
        raise UsageError("--delete and --upload-assets are mutually exclusive.")
    version = Metadata.get_current_version()
    if not version:
        logging.warning("Could not determine current version.")
        return

    changelog_path = Path(config.changelog_location)
    if not changelog_path.exists():
        logging.warning(f"{changelog_path} not found.")
        return

    changelog = Changelog(changelog_path.read_text(encoding="UTF-8"))
    repo_url = changelog.extract_repo_url()
    if not repo_url:
        logging.warning("Could not extract repository URL from changelog.")
        return

    # Parse owner/repo for gh CLI.
    parsed = owner_repo(repo_url)
    nwo = "/".join(parsed) if parsed else ""

    if delete:
        if dry_run:
            echo("[dry-run] Would delete all dev releases.")
            return
        _cleanup_dev_releases(nwo)
        echo("Deleted all dev releases.")
        return

    if _sync_dev_release(
        changelog_path, version, nwo, dry_run, asset_dir=upload_assets
    ):
        mode = "dry-run" if dry_run else "live"
        echo(f"[{mode}] Dev release v{version} synced.")


@repomatic.group(
    short_help="Lint downstream workflow caller files", section=_section_setup
)
def workflow():
    """Lint downstream workflow caller files.

    Check thin caller workflows that delegate to the canonical reusable
    workflows in kdeldycke/repomatic. Use repomatic init workflows
    to generate or sync workflow files.
    """


@workflow.command(short_help="Lint workflow files for common issues")
@option(
    "--workflow-dir",
    type=dir_path(exists=True, resolve_path=True),
    default=".github/workflows",
    help="Directory containing workflow YAML files.",
)
@option(
    "--repo",
    default=DEFAULT_REPO,
    help="Upstream repository to match thin callers against.",
)
@option(
    "--fatal/--warning",
    default=False,
    help="Exit with code 1 if issues are found (default: warning only).",
)
@pass_context
def lint(ctx, workflow_dir, repo, fatal):
    """Lint workflow files for common issues.

    Checks all YAML files in the workflow directory for:

    \b
    - Standalone workflows missing the workflow_dispatch trigger.
    - Thin callers using @main instead of a version tag.
    - Thin callers with triggers that diverge from the canonical workflow
      (missing or extra entries).
    - Thin callers missing required secrets.

    \b
    Examples:
        # Lint workflows in default location
        repomatic workflow lint

    \b
        # Lint with fatal mode (exit 1 on issues)
        repomatic workflow lint --fatal

    \b
        # Lint a custom directory
        repomatic workflow lint --workflow-dir ./my-workflows
    """
    exit_code = run_workflow_lint(
        workflow_dir=workflow_dir,
        repo=repo,
        fatal=fatal,
    )
    ctx.exit(exit_code)


@repomatic.command(
    short_help="Sync Git's .mailmap file with missing contributors",
    section=_section_sync,
)
@option(
    "--source",
    type=file_path(readable=True, resolve_path=True),
    default=".mailmap",
    help="Mailmap source file to use as reference for contributors identities that "
    "are already grouped.",
)
@option(
    "--create-if-missing/--skip-if-missing",
    is_flag=True,
    default=True,
    help="If not found, either create the missing destination mailmap file, or skip "
    "the update process entirely. This option is ignored if the destination is to print "
    f"the result to {sys.stdout.name}.",
)
@argument(
    "destination_mailmap",
    type=file_path(writable=True, resolve_path=True, allow_dash=True),
    default=None,
)
@pass_context
def sync_mailmap(ctx, source, create_if_missing, destination_mailmap):
    """Update .mailmap with missing contributors from Git history.

    Reads the existing .mailmap as a reference for grouped identities, then
    appends any contributors not already covered. Results are sorted but not
    regrouped: manual editing may be needed.

    The destination defaults to the source file (in-place update). Pass -
    to print to stdout instead.
    """
    config = get_tool_config(ctx)
    exit_if_disabled(ctx, config.mailmap_sync, "mailmap.sync")

    # Default destination to source path (in-place update).
    if destination_mailmap is None:
        destination_mailmap = source

    mailmap = Mailmap()

    if source.exists():
        logging.info(f"Read initial mapping from {source}")
        content = remove_header(source.read_text(encoding="UTF-8"))
        mailmap.parse(content)
    else:
        logging.debug(f"Mailmap source file {source} does not exist.")

    mailmap.update_from_git()
    new_content = mailmap.render()

    if is_stdout(destination_mailmap):
        logging.info(f"Print updated results to {sys.stdout.name}.")
        logging.debug(
            "Ignore the "
            + ("--create-if-missing" if create_if_missing else "--skip-if-missing")
            + " option."
        )
    else:
        logging.info(f"Save updated results to {destination_mailmap}")
        if not create_if_missing and not destination_mailmap.exists():
            logging.warning(
                f"{destination_mailmap} does not exist, stop the sync process."
            )
            ctx.exit()
        if content == new_content:
            logging.warning("Nothing to update, stop the sync process.")
            ctx.exit()

    echo(generate_header(ctx) + new_content, file=prep_path(destination_mailmap))


@repomatic.command(
    short_help="Label issues/PRs from GitHub sponsors", section=_section_github
)
@option(
    "--owner",
    envvar="GITHUB_REPOSITORY_OWNER",
    help="GitHub username or organization to check sponsorship for. "
    "Defaults to $GITHUB_REPOSITORY_OWNER.",
)
@option(
    "--author",
    help="GitHub username of the issue/PR author to check. "
    "Defaults to author from $GITHUB_EVENT_PATH.",
)
@option(
    "--repo",
    envvar="GITHUB_REPOSITORY",
    help="Repository in 'owner/repo' format. Defaults to $GITHUB_REPOSITORY.",
)
@option(
    "--number",
    type=IntRange(min=1),
    help="Issue or PR number. Defaults to number from $GITHUB_EVENT_PATH.",
)
@option(
    "--label",
    default="💖 sponsors",
    help="Label to add if author is a sponsor.",
)
@option(
    "--pr/--issue",
    "is_pr",
    default=None,
    help="Specify issue or pull request. Auto-detected from $GITHUB_EVENT_PATH.",
)
@_require_token(_token_mod, "validate_gh_token_env")
def sponsor_label(
    owner: str | None,
    author: str | None,
    repo: str | None,
    number: int | None,
    label: str,
    is_pr: bool | None,
) -> None:
    """Add a label to issues or PRs from GitHub sponsors.

    Checks if the author of an issue or PR is a sponsor of the repository owner.
    If they are, adds the specified label.

    This command requires the gh CLI to be installed and authenticated.

    When run in GitHub Actions, all parameters are auto-detected from environment
    variables ($GITHUB_REPOSITORY_OWNER, $GITHUB_REPOSITORY) and the event payload
    ($GITHUB_EVENT_PATH). You can override any auto-detected value by passing it
    explicitly.

    \b
    Examples:
        # In GitHub Actions (all defaults auto-detected)
        repomatic sponsor-label

    \b
        # Override specific values
        repomatic sponsor-label --label "sponsor"

    \b
        # Manual invocation with all values
        repomatic sponsor-label --owner kdeldycke --author some-user \\
            --repo kdeldycke/repomatic --number 123 --issue
    """
    # Apply defaults from GitHub Actions environment.
    if owner is None:
        owner = get_default_owner()
    if author is None:
        author = get_default_author()
    if number is None:
        number = get_default_number()
    if is_pr is None:
        is_pr = is_pull_request()

    # Validate required parameters.
    missing = []
    if not owner:
        missing.append("--owner")
    if not author:
        missing.append("--author")
    if not repo:
        missing.append("--repo")
    if not number:
        missing.append("--number")

    if missing:
        raise UsageError(
            f"Missing required parameters: {', '.join(missing)}. "
            "These could not be auto-detected from the environment."
        )

    # Type narrowing for mypy.
    assert owner and author and repo and number

    if is_sponsor(owner, author):
        if add_sponsor_label(repo, number, label, is_pr=is_pr):
            echo(f"Added {label!r} label to {'PR' if is_pr else 'issue'} #{number}")
        else:
            raise ClickException("Failed to add sponsor label")
    else:
        echo(f"Author {author!r} is not a sponsor of {owner!r}")


@repomatic.command(
    name="update-deps-graph",
    short_help="Generate dependency graph from uv lockfile",
    section=_section_setup,
)
@option(
    "-p",
    "--package",
    help="Focus on a specific package's dependency tree.",
)
@option_group(
    "Group filtering",
    option(
        "-g",
        "--group",
        "groups",
        multiple=True,
        help="Include dependencies from the specified group (e.g., test, typing). "
        "Can be repeated.",
    ),
    option(
        "--all-groups",
        is_flag=True,
        default=False,
        help="Include all dependency groups from pyproject.toml.",
    ),
    option(
        "--no-group",
        "excluded_groups",
        multiple=True,
        help="Exclude the specified group. Takes precedence over --all-groups "
        "and --group. Can be repeated.",
    ),
    option(
        "--only-group",
        "only_groups",
        multiple=True,
        help="Only include dependencies from the specified group, excluding main "
        "dependencies. Can be repeated.",
    ),
)
@option_group(
    "Extra filtering",
    option(
        "-e",
        "--extra",
        "extras",
        multiple=True,
        help="Include dependencies from the specified extra (e.g., xml, json5). "
        "Can be repeated.",
    ),
    option(
        "--all-extras",
        is_flag=True,
        default=False,
        help="Include all optional extras from pyproject.toml.",
    ),
    option(
        "--no-extra",
        "excluded_extras",
        multiple=True,
        help="Exclude the specified extra, if --all-extras is supplied. "
        "Can be repeated.",
    ),
    option(
        "--only-extra",
        "only_extras",
        multiple=True,
        help="Only include dependencies from the specified extra, excluding main "
        "dependencies. Can be repeated.",
    ),
)
@option(
    "--frozen/--no-frozen",
    default=True,
    help="Use --frozen to skip lock file updates.",
)
@option(
    "-l",
    "--level",
    type=IntRange(min=1),
    default=None,
    help="Maximum depth of the dependency graph. "
    "1 = directly-declared deps only, 2 = adds their deps, etc.",
)
@option(
    "-o",
    "--output",
    type=file_path(writable=True, resolve_path=True, allow_dash=True),
    default=None,
    help="Output file path. Defaults to [tool.repomatic] config or stdout.",
)
def deps_graph(
    package: str | None,
    groups: tuple[str, ...],
    all_groups: bool,
    excluded_groups: tuple[str, ...],
    only_groups: tuple[str, ...],
    extras: tuple[str, ...],
    all_extras: bool,
    excluded_extras: tuple[str, ...],
    only_extras: tuple[str, ...],
    frozen: bool,
    level: int | None,
    output: Path | None,
) -> None:
    """Generate a Mermaid dependency graph from the project's uv lockfile.

    Parses the CycloneDX SBOM export from uv and renders it as a Mermaid
    flowchart for documentation. Version specifiers from uv.lock are shown
    as edge labels.

    \b
    Examples:
        # Generate Mermaid graph
        repomatic update-deps-graph

    \b
        # Include test dependencies
        repomatic update-deps-graph --group test

    \b
        # Include all groups and extras
        repomatic update-deps-graph --all-groups --all-extras

    \b
        # Include all groups except typing
        repomatic update-deps-graph --all-groups --no-group typing

    \b
        # Include all extras except one
        repomatic update-deps-graph --all-extras --no-extra json5

    \b
        # Show only test group dependencies (no main deps)
        repomatic update-deps-graph --only-group test

    \b
        # Show only a specific extra's dependencies
        repomatic update-deps-graph --only-extra xml

    \b
        # Focus on a specific package
        repomatic update-deps-graph --package click-extra

    \b
        # Limit graph depth to 2 levels
        repomatic update-deps-graph --level 2

    \b
        # Save to file
        repomatic update-deps-graph --output docs/dependency-graph.md
    """
    config = get_tool_config()

    # Auto-detect package name from [project].name.
    if package is None:
        package = get_project_name()
        if package:
            logging.info(f"Auto-detected package from pyproject.toml: {package}")

    # Resolve output: CLI > config > stdout.
    if output is None:
        if config.dependency_graph.output:
            output = Path(config.dependency_graph.output).resolve()
        else:
            output = Path("-")

    if level is None:
        level = config.dependency_graph.level

    # --only-group/--only-extra select an exclusive mode: no main deps.
    exclude_base = bool(only_groups or only_extras)

    resolved_groups = resolve_subgraph_selection(
        SubgraphKind.GROUP,
        groups,
        all_groups,
        excluded_groups,
        only_groups,
        config.dependency_graph.all_groups,
        config.dependency_graph.no_groups,
    )
    resolved_extras = resolve_subgraph_selection(
        SubgraphKind.EXTRA,
        extras,
        all_extras,
        excluded_extras,
        only_extras,
        config.dependency_graph.all_extras,
        config.dependency_graph.no_extras,
    )

    graph = generate_dependency_graph(
        package=package,
        groups=resolved_groups,
        extras=resolved_extras,
        frozen=frozen,
        depth=level,
        exclude_base=exclude_base,
    )

    if is_stdout(output):
        logging.info(f"Print graph to {sys.stdout.name}")
    else:
        logging.info(f"Write graph to {output}")

    echo(graph, file=prep_path(output))


@repomatic.command(
    name="update-docs",
    short_help="Regenerate Sphinx API docs and dynamic content",
    section=_section_setup,
)
@option(
    "--check",
    is_flag=True,
    default=False,
    help="Report out-of-date self-updating content and exit non-zero, without "
    "writing anything. For CI drift detection.",
)
def update_docs(check: bool) -> None:
    """Regenerate Sphinx autodoc stubs and run the project's update script.

    Orchestrates four phases:

    1. Run `sphinx-apidoc` to generate RST stubs for all modules.
    2. If MyST-Parser is detected, convert the RST stubs to MyST markdown
       with ``{eval-rst}`` blocks.
    3. Run the project-specific `docs/docs_update.py` script (if present)
       to generate dynamic content.
    4. Refresh self-updating blocks (`{matrix}` compatibility tables and
       `python:render` `:mirror:` regions) found in `docs/` pages and
       `readme.md`, via `click-extra refresh-directives`.

    With ``--check``, phases 1-2 are skipped and phases 3-4 run in their own
    check modes to report drift without writing (the update script must accept
    a ``--check`` flag to participate).

    Configuration is read from `[tool.repomatic]` in `pyproject.toml`.
    """
    _update_docs(get_tool_config(), check=check)


@repomatic.command(
    short_help="Manage broken links issue lifecycle", section=_section_github
)
@option(
    "--lychee-exit-code",
    type=int,
    default=None,
    help="Exit code from lychee (0=no broken links, 2=broken links found).",
)
@option(
    "--body-file",
    type=file_path(exists=True, readable=True, resolve_path=True),
    default=None,
    help="Path to the issue body file (lychee output).",
)
@option(
    "--output-json",
    type=file_path(exists=True, readable=True, resolve_path=True),
    default=None,
    help="Path to Sphinx linkcheck output.json file.",
)
@option(
    "--source-url",
    default=None,
    help="Base URL for linking filenames and line numbers in the Sphinx report. "
    "Example: https://github.com/owner/repo/blob/<sha>/docs",
)
@option(
    "--repo-name",
    default=None,
    help="Repository name (for label selection)."
    " Defaults to $GITHUB_REPOSITORY name component.",
)
@_require_token(_token_mod, "validate_gh_token_env")
def broken_links(
    lychee_exit_code: int | None,
    body_file: Path | None,
    output_json: Path | None,
    source_url: str | None,
    repo_name: str | None,
) -> None:
    """Manage the broken links issue lifecycle.

    Combines Lychee and Sphinx linkcheck results into a single "Broken links"
    issue. Creates, updates, or closes the issue based on results.

    Requires the gh CLI to be installed and authenticated.

    \b
    In GitHub Actions, most options are auto-detected:
    - --repo-name defaults to $GITHUB_REPOSITORY name component.
    - --body-file defaults to ./lychee/out.md when --lychee-exit-code is set.
    - --output-json defaults to ./docs/_linkcheck/output.json if it exists.
    - --source-url is composed from $GITHUB_SERVER_URL, $GITHUB_REPOSITORY,
      and $GITHUB_SHA when --output-json is set.

    \b
    Examples:
        # In GitHub Actions (auto-detection)
        repomatic broken-links --lychee-exit-code 2

    \b
        # Explicit options
        repomatic broken-links \\
            --lychee-exit-code 2 \\
            --body-file ./lychee/out.md \\
            --repo-name "my-repo"
    """
    manage_combined_broken_links_issue(
        repo_name=repo_name,
        lychee_exit_code=lychee_exit_code,
        lychee_body_file=body_file,
        sphinx_output_json=output_json,
        sphinx_source_url=source_url,
    )


@repomatic.command(
    short_help="Manage setup guide issue lifecycle", section=_section_github
)
@option(
    "--has-notifications-pat",
    is_flag=True,
    default=False,
    envvar="HAS_REPOMATIC_NOTIFICATIONS_PAT",
    help="Whether REPOMATIC_NOTIFICATIONS_PAT is configured.",
)
@option(
    "--has-pat/--no-has-pat",
    default=None,
    help=(
        "Whether REPOMATIC_PAT is configured. "
        "Auto-detected from the REPOMATIC_PAT environment variable when omitted."
    ),
)
@option(
    "--has-virustotal-key",
    is_flag=True,
    default=False,
    envvar="HAS_VIRUSTOTAL_API_KEY",
    help="Whether VIRUSTOTAL_API_KEY is configured.",
)
@option(
    "--repo",
    default=None,
    envvar="GITHUB_REPOSITORY",
    help="Repository in 'owner/repo' format. Defaults to $GITHUB_REPOSITORY.",
)
@_require_token(_token_mod, "validate_gh_token_env")
@pass_context
def setup_guide(
    ctx: Context,
    has_notifications_pat: bool,
    has_pat: bool | None,
    has_virustotal_key: bool,
    repo: str | None,
) -> None:
    """Manage the setup guide issue lifecycle.

    Each setup step is shown as a collapsible section with a status
    indicator: incomplete steps are expanded with a warning emoji,
    completed steps are collapsed with a checkmark.

    PAT availability is auto-detected from the REPOMATIC_PAT environment
    variable when --has-pat/--no-has-pat is not specified.

    When a PAT is detected and --repo is provided, the command runs
    granular PAT permission checks and repository settings checks.
    The issue closes only when all verifiable steps pass.

    Requires the gh CLI to be installed and authenticated.

    \b
    Examples:
        # No secret: create or reopen the setup issue
        repomatic setup-guide

    \b
        # Secret configured: close the issue if all checks pass
        repomatic setup-guide --has-pat
    """
    # Auto-detect PAT from env when not explicitly specified on CLI.
    if has_pat is None:
        has_pat = bool(os.environ.get("REPOMATIC_PAT"))
    config = get_tool_config(ctx)
    exit_if_disabled(ctx, config.setup_guide, "setup-guide")

    manage_setup_guide(
        config,
        has_pat=has_pat,
        has_notifications_pat=has_notifications_pat,
        has_virustotal_key=has_virustotal_key,
        repo=repo,
    )


@repomatic.command(
    short_help="Unsubscribe from closed, inactive notification threads",
    section=_section_github,
)
@option(
    "--months",
    type=IntRange(min=1),
    default=3,
    help="Inactivity threshold in months. Threads updated more recently are kept.",
)
@option(
    "--batch-size",
    type=IntRange(min=1),
    default=200,
    help="Maximum number of threads/items to process per phase.",
)
@option(
    "--dry-run/--live",
    default=True,
    help="Report what would be done without making changes.",
)
@_require_token(_unsub_mod, "_validate_notifications_token")
def unsubscribe_threads(months: int, batch_size: int, dry_run: bool) -> None:
    """Unsubscribe from closed, inactive GitHub notification threads.

    Processes notifications in two phases:

    \b
    Phase 1 — REST notification threads:
      Fetches Issue/PullRequest notification threads, inspects each for
      closed + stale status, and unsubscribes via DELETE + PATCH.

    \b
    Phase 2 — GraphQL threadless subscriptions:
      Searches for closed issues/PRs the user is involved in and
      unsubscribes via the updateSubscription mutation.

    \b
    Examples:
        # Dry run to preview what would be unsubscribed
        repomatic unsubscribe-threads --dry-run

    \b
        # Unsubscribe from threads inactive for 6+ months
        repomatic unsubscribe-threads --months 6

    \b
        # Process at most 50 threads per phase
        repomatic unsubscribe-threads --batch-size 50
    """
    result = _unsubscribe_threads(months, batch_size, dry_run)
    echo(_render_report(result))


@repomatic.command(
    name="cancel-runs",
    short_help="Cancel in-progress workflow runs for a branch",
    section=_section_github,
)
@option(
    "--branch",
    required=True,
    help="Head branch whose in-progress and queued runs to cancel.",
)
@option(
    "--current-run-id",
    default="",
    envvar="GITHUB_RUN_ID",
    help="Run ID to spare (the cancelling run itself). Defaults to $GITHUB_RUN_ID.",
)
def cancel_runs(branch: str, current_run_id: str) -> None:
    """Cancel the in-progress and queued workflow runs of a branch.

    Fired when a pull request closes: GitHub does not cancel PR-triggered
    runs on close, so the branch's live runs would burn CI minutes to
    completion. The repository is resolved by the gh CLI from GH_REPO or
    the current checkout.

    \b
    Examples:
        # From the cancel-runs workflow, sparing its own run
        repomatic cancel-runs --branch "$BRANCH"
    """
    cancelled = cancel_superseded_runs(branch, current_run_id)
    echo(f"Cancelled {cancelled} run(s) for branch {branch!r}.")


@repomatic.command(
    short_help="Verify binary architecture and OS floor", section=_section_lint
)
@option(
    "--target",
    type=Choice(sorted(NUITKA_BUILD_TARGETS), case_sensitive=False),
    required=True,
    help="Target platform.",
)
@option(
    "--binary",
    "binary_path",
    type=file_path(exists=True, readable=True, resolve_path=True),
    required=True,
    help="Path to the binary file to verify.",
)
@option(
    "--dist-dir",
    "dist_dirs",
    type=dir_path(exists=True, resolve_path=True),
    multiple=True,
    help="Nuitka dist directory to include in the OS floor scan. Repeatable. "
    "Defaults to every *.dist directory in the working directory.",
)
def verify_binary(target: str, binary_path: Path, dist_dirs: tuple[Path, ...]) -> None:
    """Verify a compiled binary's architecture and minimum-OS floor.

    Parses the executable headers natively (ELF, Mach-O, PE), with no
    external tool: the architecture is checked on every platform, then the
    glibc floor on Linux and the deployment target on macOS are measured
    over the binary plus the Nuitka dist directories whose content its
    onefile payload repacks.

    \b
    Examples:
        # Verify a Linux ARM64 binary
        repomatic verify-binary --target linux-arm64 --binary ./mpm-linux-arm64.bin

    \b
        # Verify a Windows x64 binary
        repomatic verify-binary --target windows-x64 --binary ./mpm-windows-x64.exe
    """
    verify_binary_arch(target, binary_path)
    echo(f"Binary architecture verified for {target}: {binary_path}")
    scan_dirs = list(dist_dirs) or sorted(Path.cwd().glob("*.dist"))
    verify_binary_floor(target, binary_path, scan_dirs)
    echo(f"OS floor verified for {target}.")


AUDIT_HEADER_DEFS: tuple[tuple[str, str], ...] = (
    ("Package", "package"),
    ("Version", "version"),
    ("Advisory", "advisory"),
    ("Fixed", "fixed"),
    ("Sources", "sources"),
)
"""Column definitions for the `repomatic audit` table."""

_audit_sort = SortByOption(*AUDIT_HEADER_DEFS, default="package")


@repomatic.command(
    short_help="Report (and optionally fix) vulnerable dependencies",
    section=_section_lint,
    params=[_audit_sort],
)
@lockfile_option
@option(
    "--repo",
    "repo",
    type=str,
    default=lambda: os.environ.get("GITHUB_REPOSITORY", ""),
    help=(
        "Repository in OWNER/NAME format. Enables the GitHub Advisory"
        " Database source. Defaults to GITHUB_REPOSITORY when set."
    ),
)
@option(
    "--fix/--no-fix",
    default=False,
    help=(
        "Upgrade fixable packages and persist cooldown exemptions"
        " (mutates uv.lock and pyproject.toml). Default: report only."
    ),
)
@option(
    "--exit-zero/--no-exit-zero",
    default=False,
    help="In report mode, exit 0 even when vulnerabilities are found.",
)
@option(
    "--output",
    type=file_path(writable=True, resolve_path=True, allow_dash=True),
    default=None,
    help="Write a markdown report to this file.",
)
@output_format_option
@pass_context
def audit(
    ctx: Context,
    lockfile: Path,
    repo: str,
    fix: bool,
    exit_zero: bool,
    output: Path | None,
    output_format: str,
) -> None:
    """Scan locked dependencies for known security vulnerabilities.

    \b
    Read-only by default: queries every advisory database enabled in
    [tool.repomatic] vulnerable-deps.sources (default: uv-audit and
    github-advisories), unions and deduplicates the results, and prints
    them. The table respects the global --table-format option (github,
    json, csv, etc.), so --table-format json yields a machine-readable
    report. Report mode exits 1 when any vulnerability is found (use
    --exit-zero to override), so it can gate CI.

    \b
    With --fix, upgrades each fixable package with uv lock --upgrade-package
    and:
      - bypasses the exclude-newer cooldown for security fixes
      - persists exclude-newer-package entries in pyproject.toml
      - prints a markdown report of vulnerabilities and version changes

    \b
    Examples:
        # Report known vulnerabilities (read-only)
        repomatic audit

    \b
        # Machine-readable output
        repomatic --table-format json audit

    \b
        # Upgrade fixable packages (mutates uv.lock and pyproject.toml)
        repomatic audit --fix

    \b
        # CI autofix job: write the markdown report as a step output
        repomatic audit --fix --repo owner/name \\
            --output "$GITHUB_OUTPUT" --output-format github-actions
    """
    config = get_tool_config(ctx)

    # Parse the configured advisory sources, dropping the GitHub Advisory
    # Database when neither --repo nor GITHUB_REPOSITORY is available to
    # query it.
    sources: list[AdvisorySource] = []
    for raw in config.vulnerable_deps.sources:
        try:
            sources.append(AdvisorySource(raw))
        except ValueError:
            logging.warning(
                f"Unknown vulnerable-deps source {raw!r};"
                f" expected one of {[s.value for s in AdvisorySource]}."
            )
    if AdvisorySource.GITHUB_ADVISORIES in sources and not repo:
        logging.info(
            "GitHub Advisory Database source skipped:"
            " --repo not provided and GITHUB_REPOSITORY not set."
        )
        sources = [s for s in sources if s is not AdvisorySource.GITHUB_ADVISORIES]

    if fix:
        # vulnerable-deps.sync gates the autofix (mutation), not reporting.
        exit_if_disabled(ctx, config.vulnerable_deps.sync, "vulnerable-deps.sync")

        has_fixes, diff_table = _fix_vulnerable_deps(
            lockfile, repo=repo or None, sources=sources or None
        )
        if not has_fixes:
            echo("No fixable vulnerabilities found.")
            ctx.exit(0)

        echo("Upgraded vulnerable packages.")
        if diff_table:
            echo(diff_table)
        # Keep the github-actions key as `diff_table`: the autofix
        # workflow's pr-metadata step reads steps.fix.outputs.diff_table.
        emit_report(diff_table, output, output_format)
        ctx.exit(0)

    # Report mode (default, read-only).
    vulns = collect_vulnerable_packages(
        lockfile, repo=repo or None, sources=sources or None
    )
    if not vulns:
        echo("No known vulnerabilities found.")
        ctx.exit(0)

    rows = [
        (
            v.name,
            v.current_version,
            v.advisory_id,
            v.fixed_version or "unknown",
            ", ".join(sorted(s.value for s in v.sources)) or "—",
        )
        for v in vulns
    ]
    ctx.print_table(rows, AUDIT_HEADER_DEFS)

    if output:
        emit_report(
            format_vulnerability_table(vulns), output, output_format, key="vuln_table"
        )

    ctx.exit(0 if exit_zero else 1)


@repomatic.command(
    name="sync-dep-sources",
    short_help="Swap git-tracked dependencies to their released versions",
    section=_section_sync,
)
@lockfile_option
@option(
    "--table/--no-table",
    default=True,
    help="Print a summary table of updated packages.",
)
@option(
    "--release-notes/--no-release-notes",
    default=False,
    help="Fetch release notes from GitHub (markdown, appended after the table).",
)
@option(
    "--held-back/--no-held-back",
    default=True,
    help="Report newer releases withheld by the exclude-newer cooldown "
    "(runs a second uv resolution).",
)
@option(
    "--output",
    type=file_path(writable=True, resolve_path=True, allow_dash=True),
    default=None,
    help="Write a markdown report (table + release notes) to this file.",
)
@output_format_option
@pass_context
def sync_dep_sources(
    ctx: Context,
    lockfile: Path,
    table: bool,
    release_notes: bool,
    held_back: bool,
    output: Path | None,
    output_format: str,
) -> None:
    """Swap git-tracked dependencies back to their released versions.

    \b
    Manages one idiom: a [tool.uv.sources] entry tracking a git branch,
    paired with a .dev version floor naming the awaited release (like
    'mango>=2.1.0.dev0'). Once a stable release satisfying the floor ships
    on PyPI, the swap:
      - drops the [tool.uv.sources] override
      - tightens the .dev floor to its base release
      - freezes the adopted release through the exclude-newer cooldown
        (an exclude-newer-package entry the ordinary sync-uv-lock lifecycle
        prunes once it ages out)
      - re-locks and verifies the adopted version landed

    \b
    Overrides outside the idiom (path or workspace sources, rev/tag pins,
    floor-less branch tracks) are never touched. A resolution conflict or a
    lock landing on an unexpected version restores the project untouched.

    \b
    Examples:
        # Swap whatever is ready and show changes
        repomatic sync-dep-sources

    \b
        # CI: write markdown report as a GitHub Actions step output
        repomatic sync-dep-sources --no-table --release-notes \\
            --output "$GITHUB_OUTPUT" --output-format github-actions
    """
    config = get_tool_config(ctx)
    exit_if_disabled(ctx, config.dep_sources_sync, "dep-sources.sync")

    op = OPERATIONS_BY_NAME["sync-dep-sources"]
    rc = ResolveContext(
        config=config,
        today=datetime.now(timezone.utc).date(),
        release_notes=release_notes,
        # The held-back probe runs a second uv resolution, so gate it on a
        # consumer being present (terminal table or file output).
        held_back=held_back and (table or output is not None),
        lockfile=lockfile,
    )
    plan = op.resolve(rc)

    if not plan.has_changes:
        echo("No awaited release shipped.")
        ctx.exit(0)

    op.apply(plan)

    for swap in plan.source_swaps:
        echo(
            f"Swapped {swap.name} from git branch {swap.branch!r} to released"
            f" {swap.release}, frozen until it clears the cooldown."
        )
    if plan.changes:
        echo(f"{len(plan.changes)} package(s) updated.")

    _emit_lockfile_sync_report(
        ctx,
        plan,
        reference_date=rc.today,
        table=table,
        output=output,
        output_format=output_format,
    )


@repomatic.command(
    short_help="Re-lock dependencies and roll cooldown overrides forward",
    section=_section_sync,
)
@lockfile_option
@option(
    "--table/--no-table",
    default=True,
    help="Print a summary table of updated packages.",
)
@option(
    "--release-notes/--no-release-notes",
    default=False,
    help="Fetch release notes from GitHub (markdown, appended after the table).",
)
@option(
    "--held-back/--no-held-back",
    default=True,
    help="Report newer releases withheld by the exclude-newer cooldown "
    "(runs a second uv resolution).",
)
@option(
    "--output",
    type=file_path(writable=True, resolve_path=True, allow_dash=True),
    default=None,
    help="Write a markdown report (table + release notes) to this file.",
)
@output_format_option
@pass_context
def sync_uv_lock_cmd(
    ctx: Context,
    lockfile: Path,
    table: bool,
    release_notes: bool,
    held_back: bool,
    output: Path | None,
    output_format: str,
) -> None:
    """Upgrade all dependencies and clean up stale cooldown overrides.

    \b
    Wraps uv lock --upgrade and:
      - syncs the repomatic-owned [tool.uv] policy pins (required-version,
        exclude-newer) in pyproject.toml from the bundled template, so every
        machine resolves the lockfile with the same uv
      - prunes exclude-newer-package entries from pyproject.toml whose held
        version has aged past the exclude-newer cutoff, then freezes the
        survivors at their locked version (a fixed date) so the upgrade
        holds them instead of tracking newer releases
      - prints a table of updated packages with upload dates
      - reports the cooldown-bypass lifecycle: entries pruned or frozen by
        the run, and each active freeze with the date it expires
      - optionally fetches release notes from GitHub (markdown)
      - optionally reports newer releases held back by the cooldown, with the
        date each ages out of the exclude-newer window (--held-back)

    \b
    The table respects the global --table-format option (github, json,
    csv, etc.). Release notes are always rendered as markdown.

    \b
    Examples:
        # Upgrade and show changes
        repomatic sync-uv-lock

    \b
        # With release notes
        repomatic sync-uv-lock --release-notes

    \b
        # Machine-readable formats
        repomatic --table-format github sync-uv-lock
        repomatic --table-format json sync-uv-lock

    \b
        # CI: write markdown report as a GitHub Actions step output
        repomatic sync-uv-lock --no-table --release-notes \\
            --output "$GITHUB_OUTPUT" --output-format github-actions
    """
    config = get_tool_config(ctx)
    exit_if_disabled(ctx, config.uv_lock_sync, "uv-lock.sync")

    op = OPERATIONS_BY_NAME["sync-uv-lock"]
    rc = ResolveContext(
        config=config,
        today=datetime.now(timezone.utc).date(),
        release_notes=release_notes,
        # The held-back probe runs a second uv resolution, so gate it on a
        # consumer being present (terminal table or file output).
        held_back=held_back and (table or output is not None),
        lockfile=lockfile,
    )
    plan = op.resolve(rc)

    if plan.pins_synced:
        echo("Synced [tool.uv] policy pins from the bundled template.")

    if not plan.has_changes:
        echo("No dependency changes.")
        ctx.exit(0)

    op.apply(plan)

    if plan.changes:
        echo(f"{len(plan.changes)} package(s) updated.")
    if plan.pruned_bypasses:
        echo(
            "Expired cooldown bypass(es) cleared from pyproject.toml: "
            + ", ".join(forecast.name for forecast in plan.pruned_bypasses)
        )
    if plan.frozen_bypasses:
        echo(
            "Cooldown bypass(es) frozen at their locked version: "
            + ", ".join(plan.frozen_bypasses)
        )

    _emit_lockfile_sync_report(
        ctx,
        plan,
        reference_date=rc.today,
        table=table,
        output=output,
        output_format=output_format,
    )


def _emit_lockfile_sync_report(
    ctx: Context,
    plan: SyncPlan,
    *,
    reference_date: date,
    table: bool,
    output: Path | None,
    output_format: str,
) -> None:
    """Emit the shared terminal table and markdown report for a lockfile sync.

    Both sync-uv-lock and sync-dep-sources render the same package diff table,
    held-back and bypass tables, terminal release notes, and optional markdown
    file output from their resolved {class}`SyncPlan`.
    """
    # Terminal output: structured table via click-extra.
    if table:
        if plan.exclude_newer:
            echo(f"exclude-newer cutoff: {format_upload_date(plan.exclude_newer)}")
        _print_sync_table(
            ctx,
            plan.changes,
            plan.dates,
            subject="Package",
            reference_date=reference_date,
        )
        if plan.held_back:
            _print_held_back_table(ctx, plan.held_back)
        if plan.bypass_forecasts:
            _print_bypass_table(ctx, plan.bypass_forecasts)

    # Release notes echoed to the terminal (already fetched during resolve).
    if plan.notes_section:
        echo("")
        echo(plan.notes_section)

    # File output: markdown report for CI or downstream tooling.
    if output:
        emit_report(render_plan_markdown(plan), output, output_format)


def _print_sync_table(
    ctx: Context,
    changes: list[tuple[str, str, str]],
    dates: dict[str, str],
    *,
    subject: str,
    reference_date: date,
) -> None:
    """Print the shared terminal table for the dependency updaters.

    Columns are `{subject} | Old | New | Released`, the released date carrying a
    relative hint. Shared by `sync-uv-lock` and the three `sync-*` commands so
    their terminal output matches, and respects the global `--table-format`.
    Old/New stay separate columns (not the merged `Change` cell of the markdown
    PR body) so structured `--table-format json`/`csv` output stays parseable.
    """
    show_released = bool(dates)
    headers: tuple[str, ...] = (
        (subject, "Old", "New", "Released")
        if show_released
        else (subject, "Old", "New")
    )
    rows: list[tuple[str, ...]] = []
    for name, old, new in changes:
        row: tuple[str, ...] = (name, old or "(new)", new or "(removed)")
        if show_released:
            row = (*row, format_released(dates.get(name, ""), reference_date))
        rows.append(row)
    ctx.print_table(rows, headers)


def _print_held_back_table(
    ctx: Context,
    held_back: list[HeldBackPackage],
    *,
    subject: str = "Package",
) -> None:
    """Print the shared held-back terminal table for the cooldown-gated updaters.

    Columns are `{subject} | Locked | Available | Released | Eligible`. Shared by
    `sync-uv-lock` and the three `sync-*` commands, and respects the global
    `--table-format`.
    """
    echo("Held back by cooldown:")
    headers = (subject, "Locked", "Available", "Released", "Eligible")
    rows = [
        (
            pkg.name,
            pkg.locked_version,
            pkg.available_version,
            pkg.released,
            pkg.eligible,
        )
        for pkg in held_back
    ]
    ctx.print_table(rows, headers)


def _print_bypass_table(ctx: Context, forecasts: list[BypassForecast]) -> None:
    """Print the active cooldown-bypass freezes with their expiry forecasts.

    Columns are `Package | Held at | Held until`, mirroring the markdown
    section from {func}`~repomatic.dep_report.format_bypass_section`, and respects
    the global `--table-format`.
    """
    echo("Cooldown bypasses:")
    headers = ("Package", "Held at", "Held until")
    rows = [
        (forecast.name, forecast.held_version, forecast.expires)
        for forecast in forecasts
    ]
    ctx.print_table(rows, headers)


def _emit_version_sync_report(
    ctx: Context,
    plan: SyncPlan,
    output: Path | None,
    output_format: str,
) -> None:
    """Print a terminal report and optionally write a markdown PR-body report.

    Shared by the three `sync-*` version updaters. The terminal table and the
    markdown PR body (diff table, held-back section, release notes) route
    through the same shared renderers `sync-uv-lock` and `sync-deps` use
    ({func}`~repomatic.sync_ops.render_plan_markdown`), so every dependency
    updater's report matches.
    """
    echo(f"{len(plan.changes)} {plan.subject.lower()}(s) updated.")
    if plan.cutoff is not None:
        echo(f"minimum-release-age cutoff: {plan.cutoff:%Y-%m-%d}")
    _print_sync_table(
        ctx,
        plan.changes,
        plan.dates,
        subject=plan.subject,
        reference_date=plan.reference_date or datetime.now(timezone.utc).date(),
    )
    if plan.held_back:
        _print_held_back_table(ctx, plan.held_back, subject=plan.subject)
    if plan.notes_section:
        echo("")
        echo(plan.notes_section)
    if output:
        emit_report(render_plan_markdown(plan), output, output_format)


def _run_version_sync(
    ctx: Context,
    op_name: str,
    enabled: bool,
    key: str,
    output: Path | None,
    output_format: str,
    release_notes: bool,
    held_back: bool,
    up_to_date: str,
) -> None:
    """Shared body of the three version-sync commands.

    `sync-tool-versions`, `sync-action-pins`, and `sync-workflow-pins` differ
    only in their operation, feature flag, and messages: the guard, resolve,
    apply, and report sequence is identical.

    :param ctx: The Click context.
    :param op_name: The {data}`~repomatic.sync_ops.OPERATIONS_BY_NAME` key.
    :param enabled: The resolved feature flag value.
    :param key: The `[tool.repomatic]` flag key, for the disabled log line.
    :param output: The `--output` report path.
    :param output_format: The `--output-format` value.
    :param release_notes: Whether to fetch GitHub release notes.
    :param held_back: Whether to report cooldown-held releases.
    :param up_to_date: Message printed when nothing needs updating.
    """
    exit_if_disabled(ctx, enabled, key)

    op = OPERATIONS_BY_NAME[op_name]
    rc = ResolveContext(
        config=get_tool_config(ctx),
        today=datetime.now(timezone.utc).date(),
        release_notes=release_notes,
        held_back=held_back,
    )
    plan = op.resolve(rc)

    if not plan.has_changes:
        echo(up_to_date)
        ctx.exit(0)

    op.apply(plan)

    _emit_version_sync_report(ctx, plan, output, output_format)


@repomatic.command(
    short_help="Bump registry tool versions from upstream releases",
    section=_section_sync,
)
@option(
    "--output",
    type=file_path(writable=True, resolve_path=True, allow_dash=True),
    default=None,
    help="Write a markdown report (version table) to this file.",
)
@sync_release_notes_option
@sync_held_back_option
@output_format_option
@pass_context
def sync_tool_versions(
    ctx: Context,
    output: Path | None,
    output_format: str,
    release_notes: bool,
    held_back: bool,
) -> None:
    """Bump every `repomatic run` tool to its latest eligible release.

    \b
    For each registry tool, finds the highest version that has cleared the
    [tool.repomatic] minimum-release-age cooldown (GitHub releases for binary
    tools, PyPI otherwise), writes it into tool_registry.py, recomputes binary
    checksums in the same pass, and keeps the actionlint matcher URL in
    lint.yaml in lockstep.

    \b
    Upstream-only: it rewrites repomatic's own package source, so invoke it with
    `uv run` (editable), never `uvx` (whose isolated wheel is discarded).

    \b
    Examples:
        repomatic sync-tool-versions

    \b
        # CI: write a markdown report as a GitHub Actions step output
        repomatic sync-tool-versions \\
            --output "$GITHUB_OUTPUT" --output-format github-actions
    """
    _run_version_sync(
        ctx,
        "sync-tool-versions",
        get_tool_config(ctx).tool_versions_sync,
        "tool-versions.sync",
        output,
        output_format,
        release_notes,
        held_back,
        "All tools are up to date.",
    )


@repomatic.command(
    short_help="Bump SHA-pinned GitHub Actions to their latest release",
    section=_section_sync,
)
@option(
    "--output",
    type=file_path(writable=True, resolve_path=True, allow_dash=True),
    default=None,
    help="Write a markdown report (version table) to this file.",
)
@sync_release_notes_option
@sync_held_back_option
@output_format_option
@pass_context
def sync_action_pins(
    ctx: Context,
    output: Path | None,
    output_format: str,
    release_notes: bool,
    held_back: bool,
) -> None:
    """Bump SHA-pinned GitHub Actions across `.github/` to their latest release.

    \b
    Scans workflow and composite-action files for
    `uses: owner/repo@<sha> # vX.Y.Z` pins, resolves each action's latest
    release passing the [tool.repomatic] minimum-release-age cooldown to its
    commit SHA, and rewrites the SHA and version comment. repomatic's own
    reusable-workflow refs are left to `repomatic init`.

    \b
    Example:
        repomatic sync-action-pins
    """
    _run_version_sync(
        ctx,
        "sync-action-pins",
        get_tool_config(ctx).action_pins_sync,
        "action-pins.sync",
        output,
        output_format,
        release_notes,
        held_back,
        "All action pins are up to date.",
    )


@repomatic.command(
    short_help="Bump npm/PyPI version literals in workflow YAML",
    section=_section_sync,
)
@option(
    "--output",
    type=file_path(writable=True, resolve_path=True, allow_dash=True),
    default=None,
    help="Write a markdown report (version table) to this file.",
)
@sync_release_notes_option
@sync_held_back_option
@output_format_option
@pass_context
def sync_workflow_pins(
    ctx: Context,
    output: Path | None,
    output_format: str,
    release_notes: bool,
    held_back: bool,
) -> None:
    """Bump npm and PyPI version literals embedded in workflow YAML.

    \b
    Scans workflow and composite-action files for `npm install pkg@x` and
    `uvx '<pkg>==x'` pins, resolves each to its latest release passing the
    [tool.repomatic] minimum-release-age cooldown, and rewrites the literal.

    \b
    Example:
        repomatic sync-workflow-pins
    """
    _run_version_sync(
        ctx,
        "sync-workflow-pins",
        get_tool_config(ctx).workflow_pins_sync,
        "workflow-pins.sync",
        output,
        output_format,
        release_notes,
        held_back,
        "All workflow pins are up to date.",
    )


@repomatic.command(
    short_help="Update dependencies, all or a named subset",
    section=_section_sync,
)
@argument(
    "operations",
    nargs=-1,
    type=Choice(list(OPERATIONS_BY_NAME)),
)
@option(
    "--output",
    type=file_path(writable=True, resolve_path=True, allow_dash=True),
    default=None,
    help="Write a combined markdown report (one section per updater) to this file.",
)
@sync_release_notes_option
@sync_held_back_option
@option(
    "--dry-run/--no-dry-run",
    default=False,
    help="Resolve and preview every update without writing any change.",
)
@output_format_option
@pass_context
def sync_deps(
    ctx: Context,
    operations: tuple[str, ...],
    output: Path | None,
    release_notes: bool,
    held_back: bool,
    dry_run: bool,
    output_format: str,
) -> None:
    """Update project dependencies, the whole set or a named subset.

    \b
    The single entry point for dependency updates. It drives
    sync-dep-sources, sync-uv-lock, sync-action-pins, sync-workflow-pins, and
    sync-tool-versions: their network discovery runs concurrently (one shared
    HTTP cache, one spinner), then the file rewrites apply serially because
    three of them touch the same workflow files. Name one or more updaters to
    run just those; with none named, every enabled updater runs.

    \b
    The [tool.repomatic] feature flags are always authoritative: a disabled
    updater never runs. With no names, updaters not meaningful in the current
    working tree are skipped too (sync-tool-versions outside the repomatic
    checkout, the pin updaters without workflow files); naming one runs it
    regardless of the working tree.

    \b
    Each updater still opens its own PR in CI; this is the local one-shot, and
    the shared engine the consolidated autofix job drives.

    \b
    Examples:
        # Update everything enabled
        repomatic sync-deps

    \b
        # Only the lockfile and action pins
        repomatic sync-deps sync-uv-lock sync-action-pins

    \b
        # Preview without writing
        repomatic sync-deps --dry-run
    """
    config = get_tool_config(ctx)
    selected = selected_operations(config, names=list(operations) or None)
    if not selected:
        if operations:
            echo("None of the named updaters are enabled in [tool.repomatic].")
        else:
            echo("No dependency updaters are enabled for this repository.")
        ctx.exit(0)

    rc = ResolveContext(
        config=config,
        today=datetime.now(timezone.utc).date(),
        release_notes=release_notes,
        held_back=held_back,
        dry_run=dry_run,
    )
    results = run_sync_operations(
        selected, rc, spinner_label="Resolving dependency updates"
    )

    changed = [
        (op, plan) for op, plan in results if plan is not None and plan.has_changes
    ]
    failed = [op.name for op, plan in results if plan is None]

    if not changed:
        echo("All dependencies are up to date.")
    verb = "Would update" if dry_run else "Updated"
    for op, plan in changed:
        echo(f"{verb} {len(plan.changes)} via {op.name}:")
        _print_sync_table(
            ctx, plan.changes, plan.dates, subject=plan.subject, reference_date=rc.today
        )
        if plan.held_back:
            _print_held_back_table(ctx, plan.held_back, subject=plan.subject)
        if plan.bypass_forecasts:
            _print_bypass_table(ctx, plan.bypass_forecasts)

    if output:
        sections = [
            body for _op, plan in changed if (body := render_plan_markdown(plan))
        ]
        emit_report("\n\n".join(sections), output, output_format)

    if failed:
        echo(f"Failed to resolve: {', '.join(failed)}", err=True)
        ctx.exit(1)


@repomatic.command(
    short_help="Sync bumpversion config from bundled template", section=_section_sync
)
@pass_context
def sync_bumpversion(ctx: Context) -> None:
    """Sync [tool.bumpversion] config in pyproject.toml from the bundled
    template.

    Overwrites the [tool.bumpversion] section with the canonical template
    bundled in repomatic. Designed for the sync-bumpversion autofix job.
    The repomatic init bumpversion command remains available for interactive
    bootstrapping.
    """
    config = get_tool_config(ctx)
    exit_if_disabled(ctx, config.bumpversion_sync, "bumpversion.sync")

    result = run_init(
        output_dir=Path("."),
        components=("bumpversion",),
        config=config,
    )
    changed = [*result.created, *result.updated]
    if changed:
        for path in changed:
            echo(f"Updated: {path}")
    else:
        echo("bumpversion config is up to date.")


@repomatic.command(
    short_help="Remove config files that match bundled defaults",
    section=_section_sync,
)
def clean_unmodified_configs() -> None:
    """Remove config files identical to their bundled defaults.

    Scans both tool configs (yamllint, zizmor, etc.) and init-managed
    configs (like labels) and deletes any file whose content matches
    the bundled default after whitespace normalization.

    Designed for standalone use. The sync-repomatic autofix job uses
    repomatic init --delete-unmodified instead.
    """
    unmodified = find_all_unmodified_configs()
    if not unmodified:
        echo("No unmodified config files found.")
        return

    for label, rel_path in unmodified:
        Path(rel_path).unlink()
        echo(f"Removed: {rel_path} (unmodified {label} config)")


@repomatic.command(
    short_help="Sync repository labels via labelmaker", section=_section_sync
)
@option(
    "--repo",
    "repository",
    default=None,
    help="GitHub repository (owner/name). Auto-detected if omitted.",
)
@pass_context
def sync_labels(ctx: Context, repository: str | None) -> None:
    """Sync repository labels from bundled definitions using labelmaker.

    Exports label definitions via repomatic init labels, then applies them
    to the repository using labelmaker. Applies the default profile to
    all repositories, plus the awesome profile for awesome-* repos.

    Requires GITHUB_TOKEN in the environment. Downloads labelmaker
    automatically via the tool registry.
    """
    config = get_tool_config(ctx)
    exit_if_disabled(ctx, config.labels.sync, "labels.sync")

    # Auto-detect repository.
    meta = Metadata()
    if repository is None:
        repository = meta.repo_slug
    if not repository:
        raise ClickException("Cannot detect repository.")

    # Dump label files.
    result = run_init(output_dir=Path("."), components=("labels",), config=config)
    for path in [*result.created, *result.updated]:
        logging.info(f"Exported: {path}")

    try:
        apply_labels(config, repository, is_awesome=meta.is_awesome)
    except RuntimeError as e:
        raise ClickException(str(e))

    echo("Labels synced.")


def _parse_skill_frontmatter(content: str) -> dict[str, str]:
    """Extract YAML frontmatter fields from a skill definition file.

    Parses the `---`-delimited frontmatter block and returns a dict of
    key-value pairs. Only handles simple `key: value` lines (no nested
    structures).
    """
    parts = content.split("---", 2)
    if len(parts) < 3:
        return {}
    result = {}
    for line in parts[1].strip().splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            result[key.strip()] = value.strip()
    return result


@repomatic.command(
    short_help="List available Claude Code skills",
    section=_section_setup,
)
def list_skills() -> None:
    """List all bundled Claude Code skills grouped by lifecycle phase.

    Reads skill definitions from the bundled data files and displays them
    in a table grouped by phase: Setup, Development, Quality, and Release.
    """
    # Collect skill metadata from bundled files.
    skills_comp = COMPONENTS_BY_NAME["skills"]
    skills = []
    for entry in skills_comp.files:
        content = export_content(entry.source)
        meta = _parse_skill_frontmatter(content)
        name = meta.get("name", entry.file_id)
        description = meta.get("description", "")
        # Strip trailing period for table display.
        description = description.removesuffix(".")
        phase = SKILL_PHASES.get(name, "Other")
        skills.append((phase, name, description))

    # Group by phase in canonical order.
    for phase in SKILL_PHASE_ORDER:
        phase_skills = [(n, d) for p, n, d in skills if p == phase]
        if not phase_skills:
            continue
        echo(f"\n{phase}:")
        for name, description in phase_skills:
            echo(f"  /{name:<24s} {description}")

    echo("")


@repomatic.command(
    short_help="Run repository consistency checks", section=_section_lint
)
@option(
    "--repo-name",
    default=None,
    help="Repository name. Defaults to $GITHUB_REPOSITORY name component.",
)
@option(
    "--repo",
    default=None,
    envvar="GITHUB_REPOSITORY",
    help="Repository in 'owner/repo' format. Defaults to $GITHUB_REPOSITORY.",
)
@option(
    "--has-notifications-pat",
    is_flag=True,
    default=False,
    envvar="HAS_REPOMATIC_NOTIFICATIONS_PAT",
    help="Whether REPOMATIC_NOTIFICATIONS_PAT is configured.",
)
@option(
    "--has-pat/--no-has-pat",
    default=None,
    help=(
        "Whether REPOMATIC_PAT is configured. Enables PAT capability checks. "
        "Auto-detected from the REPOMATIC_PAT environment variable when omitted."
    ),
)
@option(
    "--has-virustotal-key",
    is_flag=True,
    default=False,
    envvar="HAS_VIRUSTOTAL_API_KEY",
    help="Whether VIRUSTOTAL_API_KEY is configured.",
)
@pass_context
def lint_repo(
    ctx: Context,
    repo_name: str | None,
    repo: str | None,
    has_notifications_pat: bool,
    has_pat: bool | None,
    has_virustotal_key: bool,
) -> None:
    """Run consistency checks on repository metadata.

    Reads package_name, is_sphinx, and project_description from
    pyproject.toml in the current directory.

    \b
    Checks:
      - Package name vs repository name (warning).
      - Website field set for Sphinx projects (warning).
      - Repository description matches project description (error).
      - GitHub topics subset of pyproject.toml keywords (warning).
      - Funding file present when owner has GitHub Sponsors (warning).
      - Stale draft releases (non-.dev0 drafts) (warning).
      - Fork PR workflow approval policy strict enough (warning).
      - VIRUSTOTAL_API_KEY secret missing when Nuitka is active (warning).
      - REPOMATIC_NOTIFICATIONS_PAT secret missing when the unsubscribe
        workflow is enabled (warning).

    \b
    When a PAT is detected, additional capability checks are run:
      - Contents permission (error).
      - Issues permission (error).
      - Pull requests permission (error).
      - Dependabot alerts permission and alerts enabled (error).
      - Workflows permission (error).

    \b
    Examples:
        # In GitHub Actions (reads pyproject.toml automatically)
        repomatic lint-repo --repo-name my-package

    \b
        # Local run (derives repo from $GITHUB_REPOSITORY or --repo)
        repomatic lint-repo --repo owner/repo

    \b
        # With PAT capability checks
        repomatic lint-repo --has-pat
    """
    # Auto-detect PAT from env when not explicitly specified on CLI.
    if has_pat is None:
        has_pat = bool(os.environ.get("REPOMATIC_PAT"))

    if repo_name is None and repo:
        # Extract repo name from owner/repo format.
        repo_name = repo.split("/")[-1] if "/" in repo else repo

    # Derive package_name, is_sphinx, project_description, keywords from pyproject.toml.
    metadata = Metadata()
    package_name = get_project_name()
    is_sphinx = metadata.is_sphinx
    project_description = metadata.project_description
    keywords = metadata.pyproject_toml.get("project", {}).get("keywords")

    config = get_tool_config(ctx)
    nuitka_active = config.nuitka_enabled and bool(metadata.script_entries)

    exit_code = run_repo_lint(
        package_name=package_name,
        repo_name=repo_name,
        is_sphinx=is_sphinx,
        project_description=project_description,
        keywords=keywords,
        repo=repo if repo else None,
        has_pat=has_pat,
        has_virustotal_key=has_virustotal_key,
        nuitka_active=nuitka_active,
        has_notifications_pat=has_notifications_pat,
        unsubscribe_active=config.notification_unsubscribe,
    )
    ctx.exit(exit_code)


@repomatic.command(
    short_help="Check changelog dates against release dates", section=_section_lint
)
@option(
    "--changelog",
    "changelog_path",
    type=file_path(exists=True, readable=True, resolve_path=True),
    default=None,
    help="Path to the changelog file. Defaults to the configured changelog.location.",
)
@option(
    "--package",
    default=None,
    help="PyPI package name for date lookups. Auto-detected from pyproject.toml.",
)
@option(
    "--fix",
    is_flag=True,
    default=False,
    help="Fix date mismatches and add PyPI admonitions to the changelog.",
)
@pass_context
def lint_changelog(
    ctx: Context,
    changelog_path: Path | None,
    package: str | None,
    fix: bool,
) -> None:
    """Verify that changelog release dates match canonical release dates.

    Uses PyPI upload dates as the canonical reference when the project is
    published to PyPI. Falls back to git tag dates for non-PyPI projects.

    PyPI timestamps are immutable and reflect the actual publication date,
    making them more reliable than git tags which can be recreated.

    Also detects orphaned versions: versions that exist as git tags,
    GitHub releases, or PyPI packages but have no corresponding changelog
    entry. Orphans cause a non-zero exit code.

    Reads pypi-package-history from [tool.repomatic] to fetch
    releases published under former package names (for renamed projects).

    Reads abandoned-versions from [tool.repomatic] to skip
    "not found on PyPI" warnings for releases that were frozen but
    never published (skip-and-move-forward releases).

    Reads changelog.bullet-word-threshold from [tool.repomatic] to warn,
    non-fatally, about unreleased changelog bullets longer than that many
    words. A changelog entry is a release note, not a commit message.

    \b
    Output symbols:
        ✓  Dates match
        ⚠  Version not found on reference source (warning, non-fatal)
        ✗  Date mismatch (error, fatal)

    \b
    With --fix, the command also:
        - Corrects mismatched dates to match the canonical source.
        - Adds a PyPI link admonition under each released version.
        - Adds a CAUTION admonition for yanked releases.
        - Adds a WARNING admonition for versions not on PyPI.
        - Inserts placeholder sections for orphaned versions.

    \b
    Exit codes:
        0  All dates match, or --fix corrected the file.
        1  Date mismatch or orphan detected without --fix.
        2  Sanity gate refused to rewrite: upstream lookup (PyPI or
           GitHub Releases) looks unhealthy and the existing changelog
           has substantial coverage that would be silently stripped.
           Re-run when the API is reachable.

    \b
    Examples:
        # Check the default changelog.md (auto-detects PyPI package)
        repomatic lint-changelog

    \b
        # Fix dates and add admonitions
        repomatic lint-changelog --fix

    \b
        # Explicit package name
        repomatic lint-changelog --package repomatic
    """
    config = get_tool_config(ctx)
    if changelog_path is None:
        changelog_path = Path(config.changelog_location)
    archive_location = config.changelog_archive_location
    exit_code = lint_changelog_dates(
        changelog_path,
        package=package,
        fix=fix,
        archive_path=Path(archive_location) if archive_location else None,
        pypi_package_history=config.pypi_package_history,
        abandoned_versions=config.abandoned_versions,
        bullet_word_threshold=config.changelog_bullet_word_threshold,
    )
    ctx.exit(exit_code)


TOOL_LIST_HEADER_DEFS: tuple[tuple[str, str], ...] = (
    ("Tool", "tool"),
    ("Version", "version"),
    ("Config source", "config-source"),
)
"""Column definitions for the `repomatic run --list` table."""

_run_sort = SortByOption(*TOOL_LIST_HEADER_DEFS, default="tool")


@repomatic.command(
    name="run",
    short_help="Run an external tool with managed config",
    section=_section_lint,
    context_settings={"ignore_unknown_options": True},
    params=[_run_sort],
)
@argument("tool_name", required=False, default=None)
@argument("extra_args", nargs=-1, type=UNPROCESSED)
@option("--list", "list_tools", is_flag=True, help="List all managed tools.")
@option(
    "--version",
    "tool_version",
    default=None,
    help="Override the pinned version of the tool.",
)
@option(
    "--checksum",
    default=None,
    help="Override the SHA-256 checksum for the current platform.",
)
@option(
    "--skip-checksum",
    is_flag=True,
    default=False,
    help="Skip SHA-256 verification of binary downloads.",
)
@option(
    "--no-cache",
    is_flag=True,
    default=False,
    help="Bypass the binary cache (download fresh every time).",
)
@pass_context
def run_cmd(
    ctx,
    tool_name,
    extra_args,
    list_tools,
    tool_version,
    checksum,
    skip_checksum,
    no_cache,
):
    """Run an external tool with managed configuration.

    Installs the tool at a pinned version, resolves config through a 4-level
    precedence chain (native config file, [tool.X] in pyproject.toml,
    bundled default, bare invocation), and invokes the tool.

    Binary tools are cached locally to avoid re-downloading on repeated runs.
    Use --no-cache to force a fresh download. See repomatic cache for cache
    management.

    \b
    Pass extra arguments to the tool after --:
        repomatic run yamllint -- --strict .
        repomatic run zizmor -- --offline .

    \b
    Override the pinned version:
        repomatic run shfmt --version 3.14.0 --skip-checksum -- .

    \b
    List all managed tools and their resolved config source:
        repomatic run --list
    """
    if list_tools:
        rows = [
            (spec.name, spec.version, resolve_config_source(spec))
            for spec in TOOL_REGISTRY.values()
        ]
        ctx.print_table(rows, TOOL_LIST_HEADER_DEFS)
        ctx.exit(0)

    if tool_name is None:
        raise UsageError(
            "Missing argument 'TOOL_NAME'. Use --list to see available tools."
        )

    exit_code = run_tool(
        tool_name,
        extra_args=extra_args,
        version=tool_version,
        checksum=checksum,
        skip_checksum=skip_checksum,
        no_cache=no_cache,
    )
    ctx.exit(exit_code)


CACHE_LIST_HEADER_DEFS: tuple[tuple[str, str], ...] = (
    ("Type", "type"),
    ("Name", "name"),
    ("Detail", "detail"),
    ("Size", "size"),
    ("Age", "age"),
)
"""Column definitions for the `repomatic cache show` table."""


@repomatic.group(short_help="Manage the download cache", section=_section_lint)
def cache():
    """Manage the local download cache.

    Binary tools and HTTP API responses are cached to avoid redundant
    downloads. This group provides subcommands to inspect, clean, and locate
    the cache.
    """


def _format_age(mtime: float) -> str:
    """Format a file mtime as a human-readable age string."""
    age_days = int((time.time() - mtime) / 86400)
    if age_days == 0:
        return "today"
    if age_days == 1:
        return "1 day"
    return f"{age_days} days"


_cache_show_sort = SortByOption(*CACHE_LIST_HEADER_DEFS, default="name")


@cache.command(short_help="List cached entries", params=[_cache_show_sort])
@pass_context
def show(ctx):
    """List all cached binaries and HTTP responses."""
    bin_entries = cache_info()
    http_entries = http_cache_info()
    cfg_entries = config_cache_info()
    if not bin_entries and not http_entries and not cfg_entries:
        echo("Cache is empty.")
        ctx.exit(0)

    rows = []
    total_size = 0
    for bin_entry in bin_entries:
        total_size += bin_entry.size
        rows.append((
            "binary",
            bin_entry.tool,
            f"{bin_entry.version} ({bin_entry.platform})",
            format_file_size(bin_entry.size),
            _format_age(bin_entry.mtime),
        ))
    for http_entry in http_entries:
        total_size += http_entry.size
        rows.append((
            "http",
            http_entry.namespace,
            http_entry.key,
            format_file_size(http_entry.size),
            _format_age(http_entry.mtime),
        ))
    for cfg_entry in cfg_entries:
        total_size += cfg_entry.size
        rows.append((
            "config",
            cfg_entry.tool,
            cfg_entry.filename,
            format_file_size(cfg_entry.size),
            _format_age(cfg_entry.mtime),
        ))

    ctx.print_table(rows, CACHE_LIST_HEADER_DEFS)
    total_count = len(bin_entries) + len(http_entries) + len(cfg_entries)
    echo(f"\nTotal: {total_count} file(s), {format_file_size(total_size)}")


@cache.command(short_help="Remove cached entries")
@option("--tool", default=None, help="Only remove binary entries for this tool.")
@option(
    "--namespace",
    default=None,
    help="Only remove HTTP entries in this namespace (e.g., pypi, github-releases).",
)
@option(
    "--max-age",
    type=int,
    default=None,
    help="Only remove entries older than this many days.",
)
@pass_context
def clean(ctx, tool, namespace, max_age):
    """Remove cached binaries and HTTP responses.

    Without options, removes everything. Use --tool to target a specific
    binary tool, --namespace for a specific HTTP namespace, or --max-age
    for entries older than a threshold.

    \b
    Examples:
        repomatic cache clean
        repomatic cache clean --tool ruff
        repomatic cache clean --namespace pypi
        repomatic cache clean --max-age 7
    """
    bin_deleted, bin_freed = clear_cache(tool=tool, max_age_days=max_age)
    http_deleted, http_freed = clear_http_cache(
        namespace=namespace,
        max_age_days=max_age,
    )
    cfg_deleted, cfg_freed = clear_config_cache(tool=tool)
    total_deleted = bin_deleted + http_deleted + cfg_deleted
    total_freed = bin_freed + http_freed + cfg_freed
    if total_deleted:
        echo(f"Removed {total_deleted} file(s), freed {format_file_size(total_freed)}.")
    else:
        echo("Nothing to remove.")


@cache.command(short_help="Print the cache directory path")
def path():
    """Print the absolute path to the cache directory.

    Useful for CI integration with actions/cache or similar tools.
    """
    echo(str(_cache_dir()))


@repomatic.command(short_help="Create and push a Git tag", section=_section_release)
@option(
    "--tag",
    required=True,
    help="Tag name to create (e.g., v1.2.3).",
)
@option(
    "--commit",
    default=None,
    help="Commit to tag. Defaults to HEAD.",
)
@option(
    "--push/--no-push",
    default=True,
    help="Push the tag to remote after creation.",
)
@option(
    "--skip-existing/--error-existing",
    default=True,
    help="Skip silently if tag exists, or fail with an error.",
)
@option(
    "-o",
    "--output",
    type=file_path(writable=True, resolve_path=True, allow_dash=True),
    default=None,
    help="Output file for created=true/false (e.g., $GITHUB_OUTPUT).",
)
def git_tag(
    tag: str,
    commit: str | None,
    push: bool,
    skip_existing: bool,
    output: Path | None,
) -> None:
    """Create and optionally push a Git tag.

    This command is idempotent: if the tag already exists and --skip-existing
    is used, it exits successfully without making changes. This allows safe
    re-runs of workflows interrupted after tag creation.

    \b
    Examples:
        # Create and push a tag
        repomatic git-tag --tag v1.2.3

    \b
        # Tag a specific commit
        repomatic git-tag --tag v1.2.3 --commit abc123def

    \b
        # Create tag without pushing
        repomatic git-tag --tag v1.2.3 --no-push

    \b
        # Fail if tag exists
        repomatic git-tag --tag v1.2.3 --error-existing

    \b
        # Output result for GitHub Actions
        repomatic git-tag --tag v1.2.3 --output "$GITHUB_OUTPUT"
    """
    try:
        created = create_and_push_tag(
            tag=tag,
            commit=commit,
            push=push,
            skip_existing=skip_existing,
        )
    except ValueError as e:
        raise ClickException(str(e))
    except subprocess.CalledProcessError as e:
        msg = f"Failed to create/push tag: {e}"
        if e.stderr:
            msg += f"\n{e.stderr.strip()}"
        raise ClickException(msg)

    if created:
        echo(f"Created{' and pushed' if push else ''} tag {tag!r}")
    else:
        echo(f"Tag {tag!r} already exists, skipped.")

    if output:
        echo(f"created={'true' if created else 'false'}", file=prep_path(output))


@repomatic.command(
    name="git-commit-push",
    short_help="Commit files and push, rebasing on rejection",
    section=_section_release,
)
@option(
    "--message",
    required=True,
    help="Commit message.",
)
@option(
    "--remote",
    default="origin",
    show_default=True,
    help="Remote to push to.",
)
@option(
    "--branch",
    default="main",
    show_default=True,
    help="Remote branch to push to.",
)
@argument(
    "paths",
    nargs=-1,
    required=True,
    type=file_path(exists=True, resolve_path=True),
)
def git_commit_push(
    message: str,
    remote: str,
    branch: str,
    paths: tuple[Path, ...],
) -> None:
    """Commit the given files and push them to a remote branch.

    Idempotent: exits successfully without creating a commit when the files
    are unchanged. A rejected push (something else pushed meanwhile) is
    retried after rebasing onto the fresh remote tip, so release jobs can
    publish generated files to the default branch without racing other
    pushes. Works from a detached HEAD.

    \b
    Examples:
        repomatic git-commit-push --message "Record v1.2.3 binaries" \\
            docs/binaries.md docs/assets/virustotal-scans.json
    """
    try:
        pushed = commit_and_push_files(paths, message, remote=remote, branch=branch)
    except RuntimeError as e:
        raise ClickException(str(e))
    except subprocess.CalledProcessError as e:
        msg = f"Git operation failed: {e}"
        if e.stderr:
            msg += f"\n{e.stderr.strip()}"
        raise ClickException(msg)

    if pushed:
        echo(f"Pushed to {remote}/{branch}.")
    else:
        echo("No changes to commit.")


@repomatic.command(
    name="scan-virustotal",
    short_help="Upload release binaries to VirusTotal",
    section=_section_release,
)
@option(
    "--tag",
    required=True,
    help="Release tag the binaries belong to (e.g., v1.2.3).",
)
@option(
    "--api-key",
    required=True,
    envvar="VIRUSTOTAL_API_KEY",
    help="VirusTotal API key.",
)
@option(
    "--binaries-dir",
    type=dir_path(exists=True, resolve_path=True),
    required=True,
    help="Directory containing binary files to upload.",
)
@option(
    "--rate-limit",
    type=IntRange(1, 60),
    default=4,
    show_default=True,
    help="Maximum VirusTotal API requests per minute.",
)
@option(
    "--poll/--no-poll",
    default=False,
    help="Poll for detection statistics after uploading.",
)
@option(
    "--poll-timeout",
    type=IntRange(60, 3600),
    default=600,
    show_default=True,
    help="Maximum seconds to wait for analysis completion when polling.",
)
@option(
    "--records",
    type=file_path(resolve_path=True),
    default=None,
    help="JSON scan history file to record detection snapshots in (requires --poll).",
)
def scan_virustotal(
    tag: str,
    api_key: str,
    binaries_dir: Path,
    rate_limit: int,
    poll: bool,
    poll_timeout: int,
    records: Path | None,
) -> None:
    """Upload release binaries to VirusTotal.

    Scans all .bin and .exe files in the given directory and uploads them to
    VirusTotal, seeding antivirus vendor databases with the signatures of the
    freshly built binaries.

    With --poll, waits for the analyses to complete and reports each binary's
    flagged / total verdict counts. With --records, the polled snapshots are
    merged into a JSON history file, which sync-binaries renders into the
    binaries catalog page.

    \b
    Examples:
        repomatic scan-virustotal --tag v1.2.3 --binaries-dir ./binaries

    \b
        repomatic scan-virustotal --tag v1.2.3 --binaries-dir ./binaries \\
            --poll --records docs/assets/virustotal-scans.json
    """
    if records and not poll:
        raise UsageError("--records requires --poll.")

    file_paths = sorted(
        p
        for p in binaries_dir.iterdir()
        if p.is_file() and p.suffix in BINARY_ASSET_SUFFIXES
    )
    if not file_paths:
        echo("No .bin or .exe files found, nothing to upload.")
        return

    echo(f"Uploading {len(file_paths)} file(s) to VirusTotal...")
    results = scan_files(api_key, file_paths, rate_limit)
    for result in results:
        echo(f"  {result.filename}: {result.analysis_url}")
    if not results:
        echo("All uploads failed.")
        return

    if poll:
        echo(
            f"Polling VirusTotal for {len(results)} file(s)"
            f" (timeout {poll_timeout}s)..."
        )
        results = poll_detection_stats(api_key, results, rate_limit, poll_timeout)
        for result in results:
            stats = str(result.detection_stats) if result.detection_stats else "pending"
            echo(f"  {result.filename}: {stats}")

    if records:
        new_records = records_from_results(results, tag)
        # Write even when no analysis completed: a normalized (possibly
        # empty) history file lets later pipeline steps rely on its presence.
        changed = upsert_scan_records(records, new_records)
        if changed:
            echo(f"Recorded {len(new_records)} scan(s) in {records}.")
        else:
            echo(f"Scan records in {records} already up to date.")


@repomatic.command(
    name="sync-binaries",
    short_help="Regenerate the binaries catalog page",
    section=_section_release,
)
@option(
    "--repo",
    required=True,
    envvar="GITHUB_REPOSITORY",
    help="Repository in owner/repo format.",
)
@option(
    "--page",
    type=file_path(resolve_path=True),
    default="docs/binaries.md",
    show_default=True,
    help="Markdown page to create or refresh.",
)
@option(
    "--records",
    type=file_path(resolve_path=True),
    default=None,
    help="JSON scan history file written by scan-virustotal.",
)
@option(
    "--backfill-records/--no-backfill-records",
    default=False,
    help="Recover detection snapshots from legacy release-notes tables into "
    "the records file (requires --records).",
)
@pass_context
def sync_binaries(
    ctx: Context,
    repo: str,
    page: Path,
    records: Path | None,
    backfill_records: bool,
) -> None:
    """Regenerate the binaries catalog from the GitHub Releases API.

    Writes the catalog data to assets/binaries.csv next to the page, one row
    per released binary: download link, size, SHA-256 checksum linking to the
    VirusTotal analysis, and the detection snapshot when a scan history file
    is given. The page renders the CSV through a csv-table directive and is
    created from a default template when missing; only its chart region is
    rewritten afterwards, so the prose can be edited per repository.

    With --backfill-records, detection snapshots are first recovered from the
    VirusTotal tables that release notes carried before the history file
    existed, and merged into the records file. Release notes are immutable,
    so the backfill converges and is safe to leave enabled.

    \b
    Examples:
        repomatic sync-binaries --repo owner/repo

    \b
        repomatic sync-binaries --repo owner/repo \\
            --records docs/assets/virustotal-scans.json --backfill-records
    """
    config = get_tool_config(ctx)
    exit_if_disabled(ctx, config.binaries_sync, "binaries.sync")

    if backfill_records and not records:
        raise UsageError("--backfill-records requires --records.")

    try:
        releases = get_releases_with_assets(f"https://github.com/{repo}")
    except GitHubReleasesUnavailable as e:
        raise ClickException(str(e))

    # Fixed location relative to the page, matching the `:file:` path in the
    # page template's csv-table directive.
    csv_path = page.parent / "assets" / "binaries.csv"

    try:
        if backfill_records and records:
            legacy = [
                record
                for release in releases
                if not release.draft and release.date
                for record in records_from_release_notes(
                    release.body, release.tag, release.date
                )
            ]
            if legacy and upsert_scan_records(records, legacy):
                echo(f"Backfilled {len(legacy)} snapshot(s) from release notes.")

        scan_records = load_scan_records(records) if records else []
        csv_changed = update_binaries_csv(
            csv_path, render_binaries_csv(repo, releases, scan_records)
        )
        page_changed = update_binaries_page(
            page, render_chart_section(scan_records), repo
        )
    except ValueError as e:
        raise ClickException(str(e))

    for path, changed in ((csv_path, csv_changed), (page, page_changed)):
        if changed:
            echo(f"Updated {path}.")
        else:
            echo(f"{path} already up to date.")


@repomatic.command(
    short_help="Generate PR body with workflow metadata", section=_section_github
)
@option(
    "--prefix",
    envvar="GHA_PR_BODY_PREFIX",
    default="",
    help="Content to prepend before the metadata details block. "
    "Can also be set via the GHA_PR_BODY_PREFIX environment variable.",
)
@option(
    "--template",
    type=Choice(get_template_names(), case_sensitive=False),
    default=None,
    help="Use a built-in prefix template instead of --prefix.",
)
@option(
    "--template-file",
    "template_file",
    type=file_path(exists=True, readable=True, resolve_path=True),
    default=None,
    help=(
        "Use an external template file (markdown with optional YAML"
        " frontmatter). Mutually exclusive with --template. Lets downstream"
        " repos ship project-specific PR templates without modifying"
        " repomatic. Templates should set 'footer: false' in their"
        " frontmatter to avoid duplicating the attribution footer that"
        " ships with the metadata block."
    ),
)
@option(
    "--template-arg",
    "template_args_cli",
    multiple=True,
    metavar="KEY=VALUE",
    help=(
        "Pass an arbitrary key/value pair to the template. Repeat to provide"
        " multiple. Use this to feed template variables not covered by the"
        " dedicated --version / --part / --pr-ref flags. Example:"
        " --template-arg channel=Nix."
    ),
)
@option(
    "--version",
    "version",
    default=None,
    help="Version string passed to the template (e.g. 1.2.0).",
)
@option(
    "--part",
    default=None,
    help="Version part passed to bump-version template (e.g. minor, major).",
)
@option(
    "--pr-ref",
    "pr_ref",
    default=None,
    help="PR reference passed to detect-squash-merge template (e.g. #2316).",
)
@option(
    "--output",
    type=file_path(writable=True, resolve_path=True, allow_dash=True),
    default="-",
    help="Output file path. Defaults to stdout.",
)
@option(
    "--output-format",
    type=Choice(["markdown", "github-actions"]),
    default="markdown",
    help=(
        "Format for --output."
        " 'github-actions' wraps body, title, and commit_message"
        " as step output variables."
    ),
)
def pr_body(
    prefix: str,
    template: str | None,
    template_file: Path | None,
    template_args_cli: tuple[str, ...],
    version: str | None,
    part: str | None,
    pr_ref: str | None,
    output: Path,
    output_format: str,
) -> None:
    """Generate a PR body with a collapsible workflow metadata block.

    Reads GITHUB_* environment variables to produce a <details> block
    listing the workflow metadata (documentation, trigger, actor, ref,
    commit, job, workflow, run).

    The prefix can be set via --template (built-in templates) or --prefix
    (arbitrary content, also via GHA_PR_BODY_PREFIX env var). If both are
    given, --prefix is prepended before the rendered template content.

    \b
    Examples:
        # Preview metadata block locally
        repomatic pr-body

    \b
        # CI: write as GitHub Actions step outputs
        repomatic pr-body --output "$GITHUB_OUTPUT" \\
            --output-format github-actions

    \b
        # Use a built-in template
        repomatic pr-body --template bump-version \\
            --version 1.2.0 --part minor

    \b
        # Use a downstream-shipped template with custom variables
        repomatic pr-body --template-file path/to/template.md \\
            --template-arg fruit=mango --template-arg crate=10

    \b
        # With a prefix via environment variable
        GHA_PR_BODY_PREFIX="Fix formatting" repomatic pr-body
    """

    if template and template_file:
        msg = "--template and --template-file are mutually exclusive."
        raise UsageError(msg)

    def _auto_version() -> str:
        """Read current_version from bumpversion config and strip .dev suffix."""
        ver = Metadata.get_current_version()
        if not ver:
            msg = "Cannot auto-detect version: no bumpversion config found."
            raise ClickException(msg)
        ver = re.sub(r"\.dev\d*$", "", ver)
        logging.info(f"Auto-detected version: {ver}")
        return ver

    cli_extra_args: dict[str, str | None] = {}
    for entry in template_args_cli:
        if "=" not in entry:
            msg = f"--template-arg expects KEY=VALUE, got {entry!r}."
            raise UsageError(msg)
        key, _, raw_value = entry.partition("=")
        cli_extra_args[key.strip()] = raw_value

    # Map argument names to their values or callables. CLI-provided extras
    # override built-in flag-driven sources so callers can pass any name.
    arg_sources: dict[str, str | None | Callable[[], str | None]] = {
        "diff_table": os.getenv("REPOMATIC_DIFF_TABLE", ""),
        "part": part,
        "pr_ref": pr_ref,
        # Callable, will be invoked if needed.
        "repo_url": lambda: Metadata().repo_url,
        "version": version if version is not None else _auto_version,
    }
    arg_sources.update(cli_extra_args)

    title_str = ""
    commit_msg_str = ""

    template_ref: str | Path | None = template or template_file

    if template_ref:
        kwargs: dict[str, str | None] = {}
        for arg in template_args(template_ref):
            value = arg_sources.get(arg)
            if value is None:
                msg = f"--{arg} is required for template {template_ref!r}"
                raise UsageError(msg)
            # Call if callable, otherwise use the value directly.
            kwargs[arg] = value() if callable(value) else value

        template_content = render_template(template_ref, **kwargs)
        # Combine prefix (e.g. from GHA_PR_BODY_PREFIX) with template content.
        if prefix:
            prefix = prefix + "\n\n" + template_content
        else:
            prefix = template_content
        title_str = render_title(template_ref, **kwargs)
        commit_msg_str = render_commit_message(template_ref, **kwargs)

    # Surface the template's job documentation deep link in the metadata
    # block, standing in for the description section PR bodies used to carry.
    docs_url = ""
    docs_name = ""
    if template_ref:
        docs_url = template_docs_url(template_ref)
        if template:
            docs_name = template
        elif template_file:
            docs_name = template_file.name.removesuffix(".noformat").removesuffix(".md")
    md = Metadata()
    metadata_block = generate_pr_metadata_block(
        md, docs_url=docs_url, docs_name=docs_name
    )
    body = build_pr_body(prefix, metadata_block, refresh_tip=generate_refresh_tip(md))

    if output_format == "github-actions":
        parts = [format_multiline_output("body", body)]
        if title_str:
            parts.append(f"title={title_str}")
        if commit_msg_str:
            parts.append(f"commit_message={commit_msg_str}")
        content = "\n".join(parts)
    else:
        content = body

    if is_stdout(output):
        logging.info(f"Print PR body to {sys.stdout.name}")
    else:
        logging.info(f"Write PR body to {output}")

    echo(content, file=prep_path(output))


@repomatic.command(
    short_help="Recompute SHA-256 checksums for the binary tool registry",
    section=_section_setup,
)
def update_checksums_cmd() -> None:
    """Recompute SHA-256 checksums for the binary tool registry.

    Downloads each binary-distributed tool in the `repomatic run` registry at
    its pinned version, computes the SHA-256, and rewrites any stale hash (and
    its version stamp) in tool_registry.py.

    \b
    A repair path for a manual version edit: sync-tool-versions already
    refreshes checksums when it bumps a version.

    \b
    Example:
        repomatic update-checksums
    """
    registry_path = Path(__file__).parent / "tool_registry.py"
    updated = update_registry_checksums(registry_path)

    for url, old_hash, new_hash in updated:
        echo(f"Updated: {url}")
        echo(f"  Old: {old_hash}")
        echo(f"  New: {new_hash}")
    if not updated:
        logging.info("All checksums are up to date.")


@repomatic.command(
    name="format-images",
    short_help="Format images with lossless optimization",
    section=_section_setup,
)
@option(
    "--min-savings",
    type=FloatRange(0, 100),
    default=DEFAULT_MIN_SAVINGS_PCT,
    show_default=True,
    help="Minimum percentage savings to keep an optimized file.",
)
@option(
    "--min-savings-bytes",
    type=IntRange(0),
    default=DEFAULT_MIN_SAVINGS_BYTES,
    show_default=True,
    help="Minimum absolute byte savings to keep an optimized file.",
)
@option(
    "--output",
    type=file_path(writable=True, resolve_path=True, allow_dash=True),
    default="-",
    help="Output file path. Defaults to stdout.",
)
@output_format_option
def format_images_cmd(
    min_savings: float,
    min_savings_bytes: int,
    output: Path,
    output_format: str,
) -> None:
    """Format images by losslessly optimizing them with external CLI tools.

    Discovers PNG and JPEG files and compresses them losslessly in-place
    using oxipng and jpegoptim. Produces a markdown summary table showing
    before/after sizes and savings.

    Only lossless optimizers are used so that results are idempotent:
    running the command twice produces no further changes.

    \b
    Required tools (install via apt):
        sudo apt-get install oxipng jpegoptim

    \b
    Examples:
        # Format images and print summary
        repomatic format-images

    \b
        # CI: write as a GitHub Actions step output
        repomatic format-images \\
            --output "$GITHUB_OUTPUT" --output-format github-actions

    \b
        # Use a 10% minimum savings threshold
        repomatic format-images --min-savings 10
    """
    image_files = Metadata().image_files
    if not image_files:
        echo("No image files found.")
        return

    logging.info(f"Found {len(image_files)} image file(s) to optimize.")
    results = optimize_images(
        image_files,
        min_savings_pct=min_savings,
        min_savings_bytes=min_savings_bytes,
    )
    markdown = generate_markdown_summary(results)

    if is_stdout(output):
        logging.info(f"Print image optimization summary to {sys.stdout.name}")
    else:
        logging.info(f"Write image optimization summary to {output}")

    emit_report(markdown, output, output_format, key="markdown")
