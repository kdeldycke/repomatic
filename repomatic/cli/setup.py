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
"""Project setup and reporting commands of the `repomatic` CLI.

One module per help section: every command here registers onto the
`repomatic` group through the section object both import from
{mod}`repomatic.cli.main`, which pulls this module in at startup.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from click_extra import (
    STDOUT_SENTINEL,
    Choice,
    Context,
    EnumChoice,
    FloatRange,
    IntRange,
    ParameterSource,
    UsageError,
    argument,
    dir_path,
    echo,
    file_path,
    get_tool_config,
    is_stdout,
    option,
    option_group,
    pass_context,
    prep_path,
    style,
)
from extra_platforms import is_github_ci

from ..config import (
    CONFIG_REFERENCE_HEADER_DEFS,
    config_reference,
    escape_type_for_gfm_table,
)
from ..deps.dep_graph import (
    SubgraphKind,
    generate_dependency_graph,
    resolve_subgraph_selection,
)
from ..docs import update_docs as _update_docs
from ..github.actions import (
    emit_report,
)
from ..github.matrix import (
    OS_AXIS,
    PYTHON_VERSION_AXIS,
)
from ..images import (
    DEFAULT_MIN_SAVINGS_BYTES,
    DEFAULT_MIN_SAVINGS_PCT,
    generate_markdown_summary,
    optimize_images,
)
from ..init_project import prune_paths, run_init
from ..metadata.core import (
    METADATA_KEYS_HEADER_DEFS,
    Dialect,
    Metadata,
    all_metadata_keys,
    metadata_keys_reference,
)
from ..pyproject import get_project_name
from ..registry import (
    COMPONENT_HELP_TABLE,
    DEFAULT_REPO,
    EPHEMERAL_TARGETS,
    FILE_SELECTOR_COMPONENTS,
    SKILL_LIST_HEADER_DEFS,
    SKILL_PHASE_ORDER,
    skill_catalog,
)
from ..release.checksums import update_registry_checksums
from ..tooling import tool_registry
from .main import (
    AXIS_HEADER_LABELS,
    ComponentSelector,
    DependencyGroup,
    MatrixAxis,
    ProjectExtra,
    _metadata_sort,
    _report_paths,
    _section_setup,
    _show_config_sort,
    flat_matrix_table,
    format_matrix_cell,
    log_output_target,
    matrix_axis_keys,
    matrix_axis_sort_key,
    output_format_option,
    repomatic,
    stdout_output_option,
)

TYPE_CHECKING = False


@repomatic.command(
    name="update-dep-graph",
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
        type=DependencyGroup(),
        multiple=True,
        help="Include dependencies from the specified group. Can be repeated.",
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
        type=DependencyGroup(),
        multiple=True,
        help="Exclude the specified group. Takes precedence over --all-groups "
        "and --group. Can be repeated.",
    ),
    option(
        "--only-group",
        "only_groups",
        type=DependencyGroup(),
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
        type=ProjectExtra(),
        multiple=True,
        help="Include dependencies from the specified extra. Can be repeated.",
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
        type=ProjectExtra(),
        multiple=True,
        help="Exclude the specified extra, if --all-extras is supplied. "
        "Can be repeated.",
    ),
    option(
        "--only-extra",
        "only_extras",
        type=ProjectExtra(),
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
def dep_graph(
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
        repomatic update-dep-graph

    \b
        # Include test dependencies
        repomatic update-dep-graph --group test

    \b
        # Include all groups and extras
        repomatic update-dep-graph --all-groups --all-extras

    \b
        # Include all groups except typing
        repomatic update-dep-graph --all-groups --no-group typing

    \b
        # Include all extras except one
        repomatic update-dep-graph --all-extras --no-extra json5

    \b
        # Show only test group dependencies (no main deps)
        repomatic update-dep-graph --only-group test

    \b
        # Show only a specific extra's dependencies
        repomatic update-dep-graph --only-extra xml

    \b
        # Focus on a specific package
        repomatic update-dep-graph --package click-extra

    \b
        # Limit graph depth to 2 levels
        repomatic update-dep-graph --level 2

    \b
        # Save to file
        repomatic update-dep-graph --output ./docs/assets/dependencies.mmd
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
            output = Path(STDOUT_SENTINEL)

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

    log_output_target("graph", output)

    echo(graph, file=prep_path(output))


@repomatic.command(
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
@stdout_output_option
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
    Required tools:
        oxipng is downloaded and checksum-verified from the pinned tool
        registry, once per run. jpegoptim has to be on $PATH:
        sudo apt-get install jpegoptim

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

    log_output_target("image optimization summary", output)

    emit_report(markdown, output, output_format, key="markdown")


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
    "--cooldown/--no-cooldown",
    default=True,
    help="When adopting a repomatic release newer than the one this repository "
    "already pins, hold the upstream pin back to the newest release past the "
    "[tool.repomatic] minimum-release-age window. Never moves a pin at or above "
    "the running version. --no-cooldown pins the running repomatic version "
    "immediately. Ignored when --version is set.",
)
@option(
    "--upstream-repo",
    "repo",
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
    components: tuple[str, ...],
    version_pin: str | None,
    cooldown: bool,
    repo: str,
    output_dir: Path,
    delete_excluded: bool,
    delete_unmodified: bool,
    keep_removed: bool,
    delete_removed_modified: bool,
) -> None:
    """Bootstrap a repository to use reusable workflows from kdeldycke/repomatic.

    With no arguments, generates thin-caller workflow files, exports
    configuration files (labels), and creates a minimal changelog. Specify
    COMPONENTS to initialize only selected parts.

    Scope restrictions (awesome-only, non-awesome) and [tool.repomatic]
    exclude entries only apply during bare init (no arguments). Explicitly
    naming a component bypasses scope, allowing workflows to materialize
    out-of-scope configs at runtime.

    Selectors use the same syntax as the exclude config in
    [tool.repomatic]: bare names select an entire component, qualified
    component/file entries select a single file.

    The derived upstream pin honors the [tool.repomatic] minimum-release-age
    cooldown when it would adopt a release newer than the one already pinned in
    this repository: if the running repomatic version is still inside that
    window, the workflow pin steps back to the newest release that has cleared
    it, and never below the pin already on disk. Re-running init at the pinned
    version leaves it untouched. Pass --no-cooldown to pin the running version
    immediately, or --version to pin an exact tag.

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
        # Adopt the running version now, skipping the release-age cooldown
        repomatic init --no-cooldown

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
        cooldown=cooldown,
        repo=repo,
        config=get_tool_config(),
    )

    # Print summary. The exclude list is the one section that reads as a single
    # inline sentence rather than a heading over a path list.
    if result.excluded:
        echo(
            style("Excluded by config: ", dim=True)
            + ", ".join(style(e, fg="yellow") for e in result.excluded)
        )

    _report_paths(
        result.created,
        f"Created {len(result.created)} file(s):",
        color="green",
        bold=True,
    )
    _report_paths(
        result.updated,
        f"Updated {len(result.updated)} existing file(s):",
        color="yellow",
        bold=True,
    )
    _report_paths(
        result.skipped,
        f"Skipped {len(result.skipped)} existing file(s) (never overwritten):",
    )

    # Each remaining section pairs a delete flag with the two reports it picks
    # between: acted-on when the flag says so, left-on-disk otherwise.
    if delete_excluded:
        prune_paths(result.excluded_existing, output_dir)
        _report_paths(
            result.excluded_existing,
            f"Deleted {len(result.excluded_existing)} excluded file(s) still on disk:",
            color="red",
            bold=True,
        )
    else:
        _report_paths(
            result.excluded_existing,
            f"Excluded: {len(result.excluded_existing)} file(s) still on disk",
            color="red",
            hint=" (use --delete-excluded to remove):",
        )

    if delete_unmodified:
        prune_paths(result.unmodified_configs, output_dir, prune_parents=False)
        _report_paths(
            result.unmodified_configs,
            f"Deleted {len(result.unmodified_configs)} unmodified file(s) identical"
            " to bundled defaults:",
            color="red",
            bold=True,
        )
    else:
        _report_paths(
            result.unmodified_configs,
            f"Unmodified: {len(result.unmodified_configs)} file(s) identical to"
            " bundled defaults",
            color="cyan",
            hint=" (use --delete-unmodified to remove):",
        )

    if keep_removed:
        _report_paths(
            result.removed_prunable,
            f"Removed upstream: {len(result.removed_prunable)} unmodified orphan(s)"
            " on disk",
            color="red",
            hint=" (--keep-removed set; delete manually):",
        )
    else:
        prune_paths(result.removed_prunable, output_dir)
        _report_paths(
            result.removed_prunable,
            f"Pruned {len(result.removed_prunable)} removed-upstream file(s)"
            " (unmodified orphans):",
            color="red",
            bold=True,
        )

    if delete_removed_modified:
        prune_paths(result.removed_review, output_dir)
        _report_paths(
            result.removed_review,
            f"Force-deleted {len(result.removed_review)} removed-upstream file(s)"
            " (locally modified):",
            color="red",
            bold=True,
        )
    else:
        _report_paths(
            result.removed_review,
            f"Review manually: {len(result.removed_review)} removed-upstream file(s)"
            " modified since repomatic shipped them:",
            color="yellow",
        )

    if result.warnings:
        for warning in result.warnings:
            echo(style("Warning: ", fg="yellow", bold=True) + warning)

    touched = [*result.created, *result.updated]
    # A run that only staged ephemeral files (`init labels`) produced scratch
    # input for the command about to read it, so there is nothing to commit.
    has_changes = bool(set(touched) - EPHEMERAL_TARGETS)
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


assert init_project.help is not None
init_project.help = init_project.help.format(
    component_table=COMPONENT_HELP_TABLE,
    file_selector_names=", ".join(FILE_SELECTOR_COMPONENTS),
)


@repomatic.command(
    short_help="List available Claude Code skills",
    section=_section_setup,
)
@pass_context
def list_skills(ctx: Context) -> None:
    """List all bundled Claude Code skills, in lifecycle-phase order.

    Reads skill definitions from the bundled data files and renders them as a
    table. Respects the global --table-format option.

    The rows keep the canonical lifecycle order, Setup first and Release
    last. No --sort-by is offered against it: sorting the phase column
    alphabetically would spell that lifecycle Development first.
    """
    rows = [
        (phase, f"/{name}", description)
        for phase, name, description in sorted(
            skill_catalog(), key=lambda skill: SKILL_PHASE_ORDER.index(skill[0])
        )
    ]

    # A skill description is written for a model deciding whether to invoke it,
    # so it runs to a few sentences and would otherwise set the table's width on
    # its own. A format unable to render a wrapped cell drops the cap itself.
    ctx.print_table(rows, SKILL_LIST_HEADER_DEFS)


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
    default=STDOUT_SENTINEL,
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
def metadata(
    ctx: Context,
    format: Dialect,
    overwrite: bool,
    output: Path,
    list_keys: bool,
    keys: tuple[str, ...],
) -> None:
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

    log_output_target("metadata", output)
    if is_stdout(output):
        # The overwrite flag is moot for stdout. Warn only when the user set it
        # explicitly: firing on the default value warns on every bare stdout run.
        if (
            overwrite
            and ctx.get_parameter_source("overwrite") is not ParameterSource.DEFAULT
        ):
            logging.warning("Ignore the --overwrite/--force/--replace option.")
    elif output.exists():
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


@repomatic.command(
    name="show-config",
    short_help="Print [tool.repomatic] configuration reference",
    section=_section_setup,
    params=[_show_config_sort],
)
@pass_context
def show_config(ctx: Context) -> None:
    """Print the [tool.repomatic] configuration reference table.

    Renders a table of all available options, their types, defaults, and
    descriptions, generated from the Config dataclass docstrings.
    Respects the global --table-format and --sort-by options.
    """
    rows = [
        (option, escape_type_for_gfm_table(ftype), default, desc)
        for option, ftype, default, desc in config_reference()
    ]
    # Cap the three columns whose outliers set the whole table's width: a
    # `list[dict[str, str | bool | list[str]]]` type, a list default, and a
    # description running past a hundred characters. `Option` stays uncapped,
    # because wrapping a dotted key splits an identifier mid-token. A format
    # unable to render a wrapped cell drops the caps on its own.
    ctx.print_table(rows, CONFIG_REFERENCE_HEADER_DEFS)


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
@option(
    "--grid/--no-grid",
    default=False,
    help="Collapse the listing into a two-axis grid, one cell per intersection.",
)
@option(
    "--row-axis",
    type=MatrixAxis(),
    default=PYTHON_VERSION_AXIS,
    help="Job key sorted on first, heading the grid's rows.",
)
@option(
    "--col-axis",
    type=MatrixAxis(),
    default=OS_AXIS,
    help="Job key sorted on next, heading the grid's columns.",
)
@argument(
    "matrix_name",
    metavar="[full|pr]",
    type=Choice(["full", "pr"]),
    default="full",
    required=False,
)
@pass_context
def show_test_matrix(
    ctx: Context,
    emoji: bool,
    grid: bool,
    row_axis: str,
    col_axis: str,
    matrix_name: str,
) -> None:
    """List every job of the computed CI test matrix, one row per job.

    Each row carries one column per axis and states whether that job runs
    stable or unstable (continue-on-error), which is the job list CI
    schedules. Sort it on the axes you care about with --row-axis and
    --col-axis. Pass "full" for the push and schedule matrix (the default), or
    "pr" for the reduced pull-request matrix. Respects the global
    --table-format option.

    --grid trades that listing for a two-axis grid: the two axes head the
    sides and every other axis collapses into the cells. It is the compact
    read of the same jobs, and it is lossy, so a cell standing for several
    jobs states how many.

    \b
    Examples:
        repomatic show-test-matrix
        repomatic show-test-matrix pr --no-emoji
        repomatic show-test-matrix --col-axis click-version
        repomatic show-test-matrix --grid
        repomatic show-test-matrix --grid --row-axis os --col-axis python-version
        repomatic --table-format github show-test-matrix full
    """
    meta = Metadata()
    matrix = meta.test_matrix if matrix_name == "full" else meta.test_matrix_pr
    # An axis no job carries pivots into an empty grid, which reads as a matrix
    # with nothing in it rather than as the typo it is. The `--help` listing
    # offers the full matrix's keys, so a `pr` grid lands here on the few the
    # reduced matrix drops.
    job_keys = matrix_axis_keys(matrix)
    for hint, axis in (("--row-axis", row_axis), ("--col-axis", col_axis)):
        if axis not in job_keys:
            msg = (
                f"{axis!r} is not a key of the {matrix_name} matrix. "
                f"Pass {hint} one of: {', '.join(job_keys)}."
            )
            raise UsageError(msg)
    # Both layouts order on the same two axes, so the same matrix reads in one
    # order either way. Neither keeps the order the solved job stream presents
    # them in, which only matches the canonical one for a cross-product matrix.
    row_key = matrix_axis_sort_key(row_axis, matrix_name)
    col_key = matrix_axis_sort_key(col_axis, matrix_name)
    if not grid:
        # Stable sorts compose: the secondary key first, the primary over it.
        jobs = list(matrix.solve())
        if col_key:
            jobs.sort(key=lambda job: col_key(job.get(col_axis, "")))
        if row_key:
            jobs.sort(key=lambda job: row_key(job.get(row_axis, "")))
        flat_headers, flat_rows = flat_matrix_table(
            jobs, (row_axis, col_axis), emoji=emoji
        )
        ctx.print_table(flat_rows, flat_headers)
        return
    col_values, rows = matrix.pivot(row_axis, col_axis)
    tallies = matrix.pivot_counts(row_axis, col_axis)
    if row_key:
        rows = tuple(sorted(rows, key=lambda row: row_key(row[0])))
    if col_key:
        permutation = sorted(
            range(len(col_values)), key=lambda index: col_key(col_values[index])
        )
        col_values = tuple(col_values[index] for index in permutation)
        rows = tuple(
            (row[0], *(row[1 + index] for index in permutation)) for row in rows
        )
    headers = (AXIS_HEADER_LABELS.get(row_axis, row_axis), *col_values)
    rows = tuple(
        (
            row[0],
            *(
                format_matrix_cell(cell, tallies.get((row[0], col)), emoji=emoji)
                for col, cell in zip(col_values, row[1:], strict=True)
            ),
        )
        for row in rows
    )
    ctx.print_table(rows, headers)


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
    registry_path = Path(tool_registry.__file__)
    updated = update_registry_checksums(registry_path)

    for url, old_hash, new_hash in updated:
        echo(f"Updated: {url}")
        echo(f"  Old: {old_hash}")
        echo(f"  New: {new_hash}")
    if not updated:
        logging.info("All checksums are up to date.")


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
    4. Refresh self-updating blocks (``{matrix}`` compatibility tables and
       `python:render` `:mirror:` regions) found in `docs/` pages and
       `readme.md`, via `click-extra refresh-directives`.

    With ``--check``, phases 1-2 are skipped and phases 3-4 run in their own
    check modes to report drift without writing (the update script must accept
    a ``--check`` flag to participate).

    Configuration is read from `[tool.repomatic]` in `pyproject.toml`.
    """
    _update_docs(get_tool_config(), check=check)
