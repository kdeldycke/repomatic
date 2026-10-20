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
"""Repository commands of the `repomatic` CLI: setup, metadata and file syncs.

{data}`repomatic.cli.main.COMMANDS` registers each command here, and files it
under its help section.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from click.shell_completion import CompletionItem
from click_extra import (
    STDOUT_SENTINEL,
    ClickException,
    ColumnSpec,
    EnumChoice,
    FloatRange,
    IntRange,
    ParameterSource,
    ParamType,
    SortByOption,
    UsageError,
    argument,
    dir_path,
    echo,
    file_path,
    get_tool_config,
    is_stdout,
    option,
    pass_context,
    style,
    unstyle,
)
from extra_platforms import is_github_ci

from ..awesome_toc import fix_awesome_toc
from ..checks import report_result
from ..components import (
    COMPONENTS,
    COMPONENTS_BY_NAME,
    FILE_SELECTOR_COMPONENTS,
    parse_component_entries,
)
from ..config import (
    config_reference,
)
from ..file_ops import generated_header, strip_generated_header
from ..github.outputs import AnnotationLevel, emit_annotation, emit_report
from ..github.pr import carry_pr_branch_paths
from ..gitignore import build_gitignore, orphaned_rules
from ..images import (
    DEFAULT_MIN_SAVINGS_BYTES,
    DEFAULT_MIN_SAVINGS_PCT,
    generate_markdown_summary,
    optimize_images,
)
from ..init.run import DeletionPolicy, ReportSection, run_init
from ..init.upgrade import hand_off_upgrade, resolve_upgrade_target, upgrade_report
from ..lint_repo.context import LintContext
from ..lint_repo.roster import REPO_CHECK_HELP_LIST, run_repo_lint
from ..mailmap import MAILMAP_PATH, Mailmap
from ..metadata.core import (
    METADATA_KEYS_HEADER_DEFS,
    Metadata,
    all_metadata_keys,
    metadata_keys_reference,
)
from ..metadata.output import Dialect
from ..metrics.chart import render_charts
from ..metrics.store import (
    SAMPLE_HEADER_DEFS,
    MetricStore,
    collected_subjects,
    reconstruct_from_github,
    sample_subject,
)
from ..skills import SKILL_LIST_HEADER_DEFS, skill_catalog
from ..tooling.registry import BinaryInstallError
from ..upstream import DEFAULT_REPO
from .main import (
    exit_if_disabled,
    has_cloudflare_api_token_option,
    has_cloudflare_r2_keys_option,
    has_notifications_pat_option,
    has_pat_option,
    has_virustotal_key_option,
    log_output_target,
    output_option,
    repo_name_option,
    repo_option,
    report_options,
    subcommand,
    write_output,
)

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Sequence
    from typing import Any

    # Click's own context type, distinct from the click_extra subclass: the
    # `ParamType` overrides must accept any click context, or mypy flags the
    # narrowing as a Liskov violation.
    from click import Context as ClickContext, Parameter
    from click_extra import Context

    from ..config import Config
    from ..init.run import InitResult
    from ..init.upgrade import UpgradeTarget


class ComponentSelector(ParamType):
    """Accepts bare component names or qualified `component/file` selectors.

    Bare names (e.g., `skills`) select an entire component.  Qualified
    entries (e.g., `skills/repomatic-topics`) select a single file within
    a component.  Validation delegates to
    {func}`~repomatic.components.parse_component_entries`, the same code path
    the `exclude` and `include` config options go through, so the CLI and
    config agree on syntax and error messages.
    """

    name = "selector"

    def get_metavar(self, param: Parameter, ctx: ClickContext) -> str:
        return "[COMPONENT[/FILE]]"

    def convert(
        self, value: Any, param: Parameter | None, ctx: ClickContext | None
    ) -> str:
        """Check that *value* names a component, or one file of a component.

        :return: The selector, unchanged.
        :raises click.BadParameter: The selector names neither.
        """
        try:
            parse_component_entries([value], context="selection")
        except ValueError as e:
            self.fail(str(e), param, ctx)
        return str(value)

    def shell_complete(
        self, ctx: ClickContext, param: Parameter, incomplete: str
    ) -> list[CompletionItem]:
        completions: list[CompletionItem] = [
            CompletionItem(name)
            for name in sorted(COMPONENTS_BY_NAME)
            if name.startswith(incomplete)
        ]
        if "/" in incomplete:
            comp_part = incomplete.split("/", 1)[0]
            for key, comp in COMPONENTS_BY_NAME.items():
                if key.lower() == comp_part.lower():
                    for fid in sorted(comp.file_ids):
                        qualified = f"{key}/{fid}"
                        if qualified.startswith(incomplete):
                            completions.append(CompletionItem(qualified))
        return completions


def _report_paths(
    paths: Sequence[str] | Sequence[tuple[str, str]],
    heading: str,
    *,
    color: str = "",
    bold: bool = False,
    hint: str = "",
) -> None:
    """Echo one `init` report section: a styled heading, then its paths.

    Every section of the `init` summary has this shape, and an empty one prints
    nothing, so callers can hand over a list without guarding on it first.

    :param paths: Bare relative paths, or `(path, successor)` pairs for the
        removed-asset sections, whose successor note is appended dimmed.
    :param heading: The section heading, already carrying its own count.
    :param color: Colour for the heading and each path. Empty renders them
        dimmed instead, for a section that reports a non-event.
    :param bold: Embolden the heading, marking a section whose files were
        actually deleted rather than merely reported.
    :param hint: Dimmed suffix naming the flag that would act on the section.
    """
    if not paths:
        return
    if color:
        echo(style(heading, fg=color, bold=bold) + style(hint, dim=True))
    else:
        echo(style(heading, dim=True) + style(hint, dim=True))
    for entry in paths:
        path, successor = entry if isinstance(entry, tuple) else (entry, "")
        note = style(f"  ({successor})", dim=True) if successor else ""
        styled = style(path, fg=color) if color else style(path, dim=True)
        echo(f"  {styled}{note}")


_SECTION_HEADINGS: dict[tuple[ReportSection, bool], tuple[str, str, str]] = {
    (ReportSection.EXCLUDED, True): (
        "Deleted {count} excluded file(s) still on disk:",
        "red",
        "",
    ),
    (ReportSection.EXCLUDED, False): (
        "Excluded: {count} file(s) still on disk",
        "red",
        " (use --delete-excluded to remove):",
    ),
    (ReportSection.UNMODIFIED, True): (
        "Deleted {count} unmodified file(s) identical to bundled defaults:",
        "red",
        "",
    ),
    (ReportSection.UNMODIFIED, False): (
        "Unmodified: {count} file(s) identical to bundled defaults",
        "cyan",
        " (use --delete-unmodified to remove):",
    ),
    (ReportSection.REMOVED, True): (
        "Pruned {count} removed-upstream file(s) (unmodified orphans):",
        "red",
        "",
    ),
    (ReportSection.REMOVED, False): (
        "Removed upstream: {count} unmodified orphan(s) on disk",
        "red",
        " (--keep-removed set; delete manually):",
    ),
    (ReportSection.REMOVED_MODIFIED, True): (
        "Force-deleted {count} removed-upstream file(s) (locally modified):",
        "red",
        "",
    ),
    (ReportSection.REMOVED_MODIFIED, False): (
        (
            "Review manually: {count} removed-upstream file(s) modified since"
            " repomatic shipped them:"
        ),
        "yellow",
        "",
    ),
}
"""Heading, colour and hint of each deletable report section.

Keyed by the section and whether the deletion policy deleted it. A deleted
section renders its heading in bold.
"""


def _print_init_report(result: InitResult, policy: DeletionPolicy) -> None:
    """Echo the summary of an `init` run, section after section."""
    # The exclude list is the one section that reads as a single inline
    # sentence rather than a heading over a path list.
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
    for section in ReportSection:
        deleted = policy.deletes(section)
        heading, color, hint = _SECTION_HEADINGS[section, deleted]
        entries = result.entries(section)
        _report_paths(
            entries,
            heading.format(count=len(entries)),
            color=color,
            bold=deleted,
            hint=hint,
        )
    for warning in result.warnings:
        echo(style("Warning: ", fg="yellow", bold=True) + warning)


def _print_next_steps(steps: Sequence[str]) -> None:
    """Echo the numbered next steps of an `init` run, if any."""
    if not steps:
        return
    echo("")
    echo(style("Next steps:", bold=True))
    for number, step in enumerate(steps, start=1):
        first, *rest = step.splitlines()
        echo(f"  {number}. {first}")
        for line in rest:
            echo(f"     {line}")


def _component_help_table() -> str:
    """Render the component table of the `init` help text."""
    width = max(len(comp.name) for comp in COMPONENTS) + 4
    return "\n".join(
        f"    {comp.name:<{width}s}{comp.description}" for comp in COMPONENTS
    )


def _emit_upgrade_report(
    target: UpgradeTarget,
    config: Config,
    *,
    repo: str,
    output_dir: Path,
    init_output: str,
    output: Path | None,
    output_format: str,
) -> None:
    """Write the upgrade report to `--output` when the run moved the pin.

    With `--output-format github-actions`, the adopted version travels beside
    the report as the `upgrade_version` step output, which picks the pull
    request template and fills its title.
    """
    if output is None:
        return
    report = upgrade_report(
        target, output_dir, config, init_output=init_output, repo=repo
    )
    if report is None:
        return
    adopted, body = report
    emit_report(
        body,
        output,
        output_format,
        key="upgrade_report",
        outputs={"upgrade_version": adopted},
    )


@subcommand(
    short_help="Bootstrap a repository to use reusable workflows",
    examples=(
        ("Full bootstrap (workflows + labels + changelog)", "repomatic init"),
        ("Pin to a specific version", "repomatic init --version v5.9.1"),
        (
            "Adopt the running version now, skipping the release-age cooldown",
            "repomatic init --no-cooldown",
        ),
        (
            "Upgrade to the newest release past the release-age cooldown",
            "repomatic init --upgrade",
        ),
        ("Install a single skill", "repomatic init skills/repomatic-topics"),
        ("One workflow + all labels", "repomatic init workflows/autofix.yaml labels"),
        ("Only merge ruff config into pyproject.toml", "repomatic init ruff"),
        ("Multiple components", "repomatic init ruff bumpversion"),
    ),
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
@option(
    "--upgrade",
    is_flag=True,
    default=False,
    help="Run as the newest repomatic release past the [tool.repomatic] "
    "minimum-release-age window, never below the release already pinned, so "
    "the upstream pin and every managed file move together. Hands off to that "
    "release through uvx when it is not the running one. Overrides --cooldown "
    "when it picks a release, and takes no COMPONENTS and no --version.",
)
@report_options(
    "Write the report of the move to this file, when --upgrade moves the pin."
)
@pass_context
def init(
    ctx: Context,
    components: tuple[str, ...],
    version_pin: str | None,
    cooldown: bool,
    repo: str,
    output_dir: Path,
    delete_excluded: bool,
    delete_unmodified: bool,
    keep_removed: bool,
    delete_removed_modified: bool,
    upgrade: bool,
    output: Path | None,
    output_format: str,
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

    --upgrade runs init as the newest release past that window instead, and
    hands off to it through uvx when it is not the running one: the upstream
    pin and every managed file then move in one run. With --output, it writes a
    report of the move for the upgrade pull request.

    \b
    Components:
    {component_table}

    \b
    File-level selectors ({file_selector_names}):
        workflows/autofix.yaml    A single workflow
        skills/repomatic-topics   A single skill
        labels/labels.toml        A single label config file
    """
    try:
        policy = DeletionPolicy(
            delete_excluded=delete_excluded,
            delete_unmodified=delete_unmodified,
            keep_removed=keep_removed,
            delete_removed_modified=delete_removed_modified,
        )
    except ValueError as error:
        raise UsageError(str(error)) from error
    if upgrade and (components or version_pin):
        raise UsageError(
            "--upgrade moves every managed file to one release: it takes no"
            " COMPONENTS and no --version."
        )

    config = get_tool_config(ctx)
    target = resolve_upgrade_target(config, output_dir, repo=repo) if upgrade else None
    if target is not None and not target.in_process:
        completed = hand_off_upgrade(
            target, config, repo=repo, output_dir=output_dir, flags=policy.flags()
        )
        # Echoed as it came, so the log reads like a direct run.
        echo(completed.stdout, nl=False)
        if completed.returncode:
            msg = (
                f"repomatic {target.version} init exited with code"
                f" {completed.returncode}."
            )
            raise ClickException(msg)
        _emit_upgrade_report(
            target,
            config,
            repo=repo,
            output_dir=output_dir,
            init_output=unstyle(completed.stdout),
            output=output,
            output_format=output_format,
        )
        return

    result = run_init(
        output_dir,
        components,
        config=config,
        version=version_pin,
        # An upgrade target already cleared the window. Judging it again could
        # hold the pin back while the content moves.
        cooldown=cooldown and target is None,
        repo=repo,
    )
    result.prune(output_dir, policy)
    _print_init_report(result, policy)
    _print_next_steps(result.next_steps(output_dir, config))

    if target is not None:
        _emit_upgrade_report(
            target,
            config,
            repo=repo,
            output_dir=output_dir,
            init_output="",
            output=output,
            output_format=output_format,
        )


assert init.help is not None
init.help = init.help.format(
    component_table=_component_help_table(),
    file_selector_names=", ".join(FILE_SELECTOR_COMPONENTS),
)


@subcommand(
    short_help="Check repository files against repomatic conventions",
    examples=(
        (
            "In GitHub Actions (reads pyproject.toml automatically)",
            "repomatic lint-repo --repo-name my-package",
        ),
        (
            "Local run (derives repo from $GITHUB_REPOSITORY or --repo)",
            "repomatic lint-repo --repo owner/repo",
        ),
        ("With PAT capability checks", "repomatic lint-repo --has-pat"),
    ),
)
@repo_name_option
@repo_option(detect=False)
@has_cloudflare_api_token_option
@has_cloudflare_r2_keys_option
@has_notifications_pat_option
@has_pat_option
@has_virustotal_key_option
@pass_context
def lint_repo(
    ctx: Context,
    repo_name: str | None,
    repo: str | None,
    has_cloudflare_api_token: bool,
    has_cloudflare_r2_keys: bool,
    has_notifications_pat: bool,
    has_pat: bool,
    has_virustotal_key: bool,
) -> None:
    """Run consistency checks on repository metadata.

    Reads package_name, is_sphinx, and project_description from
    pyproject.toml in the current directory.

    \b
    Checks, in report order. An error fails the command, a warning never does:
    {checks}
    """
    # Everything the checkout can answer is derived inside the context
    # constructor; the command hands over only what it alone knows.
    lint_ctx = LintContext.from_project(
        get_tool_config(ctx),
        repo=repo,
        repo_name=repo_name,
        has_pat=has_pat,
        has_virustotal_key=has_virustotal_key,
        has_cloudflare_api_token=has_cloudflare_api_token,
        has_cloudflare_r2_keys=has_cloudflare_r2_keys,
        has_notifications_pat=has_notifications_pat,
    )
    failed = False
    for check, result in run_repo_lint(lint_ctx):
        report_result(result, check.level)
        failed = failed or check.fails(result)
    ctx.exit(1 if failed else 0)


assert lint_repo.help is not None
lint_repo.help = lint_repo.help.format(checks=REPO_CHECK_HELP_LIST)


@subcommand(short_help="List available Claude Code skills")
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
        (phase, f"/{name}", description) for phase, name, description in skill_catalog()
    ]

    # A skill description is written for a model deciding whether to invoke it,
    # so it runs to a few sentences and would otherwise set the table's width on
    # its own. A format unable to render a wrapped cell drops the cap itself.
    ctx.print_table(rows, SKILL_LIST_HEADER_DEFS)


@subcommand(
    short_help="Print repository and CI state as key/value pairs for workflows",
    params=[SortByOption(*METADATA_KEYS_HEADER_DEFS, default="key")],
    examples=(
        ("Print two keys", "repomatic show-metadata current_version is_python_project"),
        ("List every key", "repomatic show-metadata --list-keys"),
        (
            "CI: write as GitHub Actions step outputs",
            'repomatic show-metadata --output-format github-json --output "$GITHUB_OUTPUT" current_version is_python_project',
        ),
    ),
)
@option(
    "--output-format",
    type=EnumChoice(Dialect),
    default=Dialect.github,
    help="Rendering format of the metadata.",
)
@option(
    "--overwrite/--no-overwrite",
    default=True,
    help="Overwrite the output file if it already holds content.",
)
@output_option("Write the metadata to this file.", default=STDOUT_SENTINEL)
@option(
    "--list-keys",
    is_flag=True,
    default=False,
    help="List all available metadata keys with descriptions and exit.",
)
@argument("keys", nargs=-1)
@pass_context
def show_metadata(
    ctx: Context,
    output_format: Dialect,
    overwrite: bool,
    output: Path,
    list_keys: bool,
    keys: tuple[str, ...],
) -> None:
    """Dump project metadata to a file.

    Prints all metadata keys to stdout by default. Use --output to write to
    a file. Pass key names as arguments to filter output.
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
            logging.warning("Ignore the --overwrite option.")
    # CI creates $GITHUB_OUTPUT before the step runs, so an empty target is the
    # normal case: only a file holding content has something to lose.
    elif output.exists() and output.stat().st_size:
        msg = "Target file exists and will be overwritten."
        if overwrite:
            logging.warning(msg)
        else:
            logging.critical(msg)
            ctx.exit(2)

    meta = Metadata()

    # Output a warning in GitHub runners if metadata are not saved to $GITHUB_OUTPUT.
    # Skip the warning for stdout: writing to stdout is a legitimate use case
    # (human inspection, --output-format json piping, Sphinx docs rendering) even
    # in CI.
    if is_github_ci() and not is_stdout(output):
        env_file = os.getenv("GITHUB_OUTPUT")
        if env_file and Path(env_file) != output:
            logging.warning(
                "Output path is not the same as $GITHUB_OUTPUT environment variable,"
                " which is generally what we're looking to do in GitHub CI runners for"
                " other jobs to consume the produced metadata."
            )

    content = meta.dump(dialect=output_format, keys=keys)

    # When writing to a file, copy the content to stderr so the computed
    # metadata is visible in CI logs without an extra debug step.
    if not is_stdout(output):
        echo(content, err=True)

    write_output("metadata", content, output)


CONFIG_REFERENCE_HEADER_DEFS: tuple[ColumnSpec, ...] = (
    ColumnSpec("option", "Option"),
    ColumnSpec("type", "Type", max_width=24),
    ColumnSpec("default", "Default", max_width=24),
    ColumnSpec("description", "Description", max_width=60),
)
"""Column definitions for the `[tool.repomatic]` configuration reference table.

The three columns whose outliers set the whole table's width are capped: a
`list[dict[str, str | bool | list[str]]]` type, a list default, and a
description running past a hundred characters. `Option` stays uncapped, because
wrapping a dotted key splits an identifier mid-token. A format unable to render
a wrapped cell drops the caps on its own.
"""


def format_config_default(value: object) -> str:
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


def format_config_type(annotation: str) -> str:
    """Format a type annotation for a raw GFM cell of the reference table.

    Strips `| None` suffixes, since the default column already shows whether
    `None` is the default.

    Then escapes the outer brackets of a nested generic: mdformat reads
    `list[dict[str, str]]` as a Markdown link reference, and would escape it
    again on every run. A simple generic like `list[str]` has no nested
    bracket and stays as is. The cell is never wrapped in backticks, where a
    backslash would render as itself.
    """
    ftype = annotation.replace(" | None", "")
    if "[" in ftype:
        first = ftype.index("[")
        last = ftype.rindex("]")
        inner = ftype[first + 1 : last]
        if "[" in inner:
            return ftype[:first] + "\\[" + inner + "\\]" + ftype[last + 1 :]
    return ftype


def config_reference_rows() -> list[tuple[str, str, str, str]]:
    """The `show-config` table: one formatted row per `[tool.repomatic]` key.

    :return: `(option, type, default, description)` rows, the cells of
        {data}`CONFIG_REFERENCE_HEADER_DEFS`.
    """
    return [
        (
            f"`{key}`",
            format_config_type(type_hint),
            format_config_default(default),
            summary,
        )
        for key, type_hint, default, summary in config_reference()
    ]


@subcommand(
    short_help="Print [tool.repomatic] configuration reference",
    params=[SortByOption(*CONFIG_REFERENCE_HEADER_DEFS, default="option")],
)
@pass_context
def show_config(ctx: Context) -> None:
    """Print the [tool.repomatic] configuration reference table.

    Renders a table of all available options, their types, defaults, and
    descriptions, generated from the Config dataclass docstrings.
    Respects the global --table-format and --sort-by options.
    """
    ctx.print_table(config_reference_rows(), CONFIG_REFERENCE_HEADER_DEFS)


@subcommand(
    short_help="Format images with lossless optimization",
    examples=(
        ("Format images and print summary", "repomatic format-images"),
        (
            "CI: write as a GitHub Actions step output",
            'repomatic format-images --output "$GITHUB_OUTPUT" --output-format github-actions',
        ),
        (
            "Use a 10% minimum savings threshold",
            "repomatic format-images --min-savings 10",
        ),
    ),
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
@report_options("Write the summary table to this file.", default=STDOUT_SENTINEL)
def format_images(
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
    """
    image_files = Metadata().files.image_files
    if not image_files:
        echo("No image files found.")
        return

    logging.info(f"Found {len(image_files)} image file(s) to optimize.")
    try:
        results = optimize_images(
            image_files,
            min_savings_pct=min_savings,
            min_savings_bytes=min_savings_bytes,
        )
    except BinaryInstallError as exc:
        raise ClickException(str(exc)) from exc
    markdown = generate_markdown_summary(results)

    log_output_target("image optimization summary", output)

    emit_report(markdown, output, output_format, key="markdown")


@subcommand(
    short_help="Remove the table-of-contents entries awesome-lint rejects",
    examples=(
        ("Fix the readme and every translation beside it", "repomatic fix-awesome-toc"),
    ),
)
def fix_awesome_toc_cmd() -> None:
    """Remove the table-of-contents entries awesome-lint forbids.

    Deletes the Contents, Contributing, Footnotes and Related Lists entries
    from the mdformat-toc block of readme.md and of every readme.{lang}.md
    translation beside it.

    \b
    A translation names those sections in its own language, so they are
    matched by their position in the heading sequence of readme.md rather
    than by name.

    \b
    Run it right after mdformat regenerates the ToC: mdformat-toc lists every
    heading it finds and has no way to leave one out.
    """
    report = fix_awesome_toc()
    if not report:
        echo("No forbidden ToC entry found.")
        return

    for readme, removed in report.items():
        echo(f"{readme}: removed {', '.join(removed)}")


@subcommand(
    short_help="Sync .gitignore from gitignore.io templates",
    examples=(
        (
            "Generate .gitignore using config from pyproject.toml",
            "repomatic sync-gitignore",
        ),
        (
            "Write to custom location",
            "repomatic sync-gitignore --output ./custom/.gitignore",
        ),
        ("Preview on stdout", "repomatic sync-gitignore --output -"),
    ),
)
@output_option(
    "Write the .gitignore to this file. Defaults to [tool.repomatic]"
    " gitignore.location."
)
@option(
    "--drop-orphans/--no-drop-orphans",
    default=False,
    help=(
        "Overwrite rules found on disk but absent from the generated file, "
        "instead of refusing to drop them."
    ),
)
@pass_context
def sync_gitignore(ctx: Context, output: Path | None, drop_orphans: bool) -> None:
    """Sync a .gitignore file from gitignore.io templates.

    Fetches templates for a base set of categories plus any extras from
    [tool.repomatic] config, then appends gitignore-extra-content.
    Writes to the path specified by gitignore-location (default
    ./.gitignore).

    The generated file is the whole file: nothing is read back from the copy on
    disk. A rule added there by hand is therefore dropped on the next sync, so
    the write aborts when it would lose one. Move the rule into
    gitignore-extra-content to keep it, or pass --drop-orphans to let it go.
    """
    config = get_tool_config(ctx)
    exit_if_disabled(ctx, config.gitignore.sync, "gitignore.sync")

    content = build_gitignore(config)

    # Resolve output path.
    if output is None:
        output = Path(config.gitignore.location)

    # Compare against what is already there before overwriting it. Skipped for
    # stdout, which destroys nothing, and for a first run, which has nothing to
    # destroy.
    if not drop_orphans and not is_stdout(output) and output.is_file():
        orphans = orphaned_rules(output.read_text(encoding="UTF-8"), content)
        if orphans:
            emit_annotation(
                AnnotationLevel.ERROR,
                f"{output}: sync would drop {len(orphans)} hand-added "
                f"rule(s): {', '.join(orphans)}",
            )
            echo(
                f"Refusing to overwrite {output}: "
                f"{len(orphans)} rule(s) on disk are absent from the generated "
                "file and would be lost.",
                err=True,
            )
            for rule in orphans:
                echo(f"  {rule}", err=True)
            echo(
                "\nMove them into [tool.repomatic.gitignore] extra-content to "
                "keep them, or re-run with --drop-orphans to discard them.",
                err=True,
            )
            ctx.exit(1)

    write_output(".gitignore", content.rstrip(), output)


@subcommand(short_help="Sync Git's .mailmap file with missing contributors")
@option(
    "--source",
    type=file_path(readable=True, resolve_path=True),
    default=MAILMAP_PATH,
    help="Mailmap source file to use as reference for contributors identities that "
    "are already grouped.",
)
@option(
    "--create-if-missing/--skip-if-missing",
    is_flag=True,
    default=True,
    help="If not found, either create the missing destination mailmap file, or "
    "skip the update process entirely. This option is ignored if the destination "
    "is stdout.",
)
@argument(
    "destination_mailmap",
    type=file_path(writable=True, resolve_path=True, allow_dash=True),
    default=None,
)
@pass_context
def sync_mailmap(
    ctx: Context,
    source: Path,
    create_if_missing: bool,
    destination_mailmap: Path | None,
) -> None:
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

    # An absent source starts from empty content, so the first run on a fresh
    # repository (the --create-if-missing bootstrap) has a baseline to diff
    # against instead of an unbound name.
    content = ""
    if source.exists():
        logging.info(f"Read initial mapping from {source}")
        content = strip_generated_header(source.read_text(encoding="UTF-8"))
        mailmap.parse(content)
    else:
        logging.debug(f"Mailmap source file {source} does not exist.")

    try:
        mailmap.update_from_git()
    except RuntimeError as error:
        raise ClickException(str(error)) from error
    new_content = mailmap.render()

    if is_stdout(destination_mailmap):
        logging.debug(
            "Ignore the "
            + ("--create-if-missing" if create_if_missing else "--skip-if-missing")
            + " option."
        )
    else:
        if not create_if_missing and not destination_mailmap.exists():
            logging.warning(
                f"{destination_mailmap} does not exist, stop the sync process."
            )
            ctx.exit()
        if content == new_content:
            logging.warning("Nothing to update, stop the sync process.")
            ctx.exit()

    write_output(
        "updated mailmap",
        generated_header(ctx.command_path) + new_content,
        destination_mailmap,
    )


@subcommand(
    short_help="Record what forges say about the repositories this project tracks",
    examples=(
        ("Record today's readings", "repomatic sample-metrics"),
        (
            "Take today's readings without rebuilding the star curves",
            "repomatic sample-metrics --no-reconstruct",
        ),
    ),
)
@option(
    "--store",
    type=file_path(resolve_path=True),
    default=None,
    help="CSV store to accumulate into. Defaults to [tool.repomatic.metrics] store.",
)
@option(
    "--carry-from",
    "carry_from",
    metavar="BRANCH",
    default=None,
    help="Restore the store from this remote branch before sampling, so "
    "readings still pending in an open pull request are appended to instead "
    "of dropped. Name the branch the job publishes to. Ignored when the "
    "branch does not exist, which is every run that follows a merge.",
)
@option(
    "--forward/--no-forward",
    default=True,
    help="Read every subject's current metrics from its own forge.",
)
@option(
    "--reconstruct/--no-reconstruct",
    default=True,
    help="Rebuild each GitHub subject's star curve from its star history.",
)
@option(
    "--render/--no-render",
    default=True,
    help="Redraw the configured charts from the stored history.",
)
@pass_context
def sample_metrics(
    ctx: Context,
    store: Path | None,
    carry_from: str | None,
    forward: bool,
    reconstruct: bool,
    render: bool,
) -> None:
    """Record what forges say about the repositories this project tracks.

    Reads every subject through whichever API its host speaks (GitHub, GitLab
    or Forgejo) and appends one row per subject, metric and date. A counter
    like the star count accrues, so its curve can be charted; an attribute like
    the date of the newest commit keeps a single row, restamped only when it
    moves.

    GitHub restricted its stargazer endpoints to a repository's own admins in
    2026, which left every third-party star chart on the web rendering an error
    card. It reopened an anonymous star history later that year, and this reads
    it on a schedule and commits the result: a history that accrues locally
    cannot be revoked.

    Each reading records where it came from, since the curves are not all
    measured the same way. A GitHub repository has its whole curve rebuilt from
    that star history, which counts the stars it still holds; every other forge
    is sampled forward, one reading per run.
    """
    config = get_tool_config(ctx)
    exit_if_disabled(ctx, config.metrics.sync, "metrics.sync")

    if not config.metrics.subjects:
        echo("No repository in [tool.repomatic.metrics] subjects, nothing to sample.")
        return

    store_path = store or Path(config.metrics.store)
    if carry_from:
        carry_pr_branch_paths(carry_from, (store_path,))
    try:
        readings = MetricStore.load(store_path)
        tracked = collected_subjects(
            config.metrics.subjects, config.metrics.predecessors
        )
    except ValueError as error:
        raise ClickException(str(error))

    outcomes = []
    if forward:
        for name, repo in tracked.items():
            outcomes.append(sample_subject(readings, name, repo, config.metrics.forges))
    if reconstruct:
        for name, repo in tracked.items():
            outcomes.append(reconstruct_from_github(readings, name, repo))

    ctx.print_table([outcome.row() for outcome in outcomes], SAMPLE_HEADER_DEFS)

    if readings.save(store_path):
        echo(f"Recorded {len(readings)} reading(s) in {store_path}.")
    else:
        echo(f"{store_path} already up to date.")

    if not render:
        return
    if not readings:
        echo("No reading recorded yet, so no chart to draw.")
        return
    try:
        for output, changed in render_charts(readings, config.metrics):
            echo(f"{'Redrew' if changed else 'Unchanged'} {output}.")
    except ValueError as error:
        raise ClickException(str(error))
