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
"""Dependency commands of the `repomatic` CLI: audit, lint, graph and updates.

{data}`repomatic.cli.main.COMMANDS` registers each command here, and files it
under its help section. The terminal output of the dependency updaters lives
here too: {mod}`repomatic.deps.sync` resolves and applies, and renders only the
markdown report the CI jobs read.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from click_extra import (
    STDOUT_SENTINEL,
    Choice,
    IntRange,
    SortByOption,
    UsageError,
    argument,
    echo,
    file_path,
    get_tool_config,
    option,
    option_group,
    pass_context,
    resolve_jobs,
)

from .. import git_ops
from ..deps.audit import (
    AUDIT_HEADER_DEFS,
    AdvisorySource,
    collect_vulnerable_packages,
    fix_vulnerable_deps,
    format_vulnerability_table,
)
from ..deps.graph import (
    SubgraphKind,
    generate_dependency_graph,
    resolve_subgraph_selection,
)
from ..deps.policy import scan_policy
from ..deps.report import (
    BYPASS_COLUMNS,
    HELD_BACK_COLUMNS,
    format_released,
    format_upload_date,
)
from ..deps.shippable import (
    LINT_DEPS_HEADER_DEFS,
    format_blocker_section,
    scan_project,
)
from ..deps.sync import (
    OPERATIONS_BY_NAME,
    ResolveContext,
    render_plan_markdown,
    run_sync_operations,
    selected_operations,
)
from ..github.outputs import AnnotationLevel, emit_annotation, emit_report
from ..github.pr import upsert_pr
from ..github.pr_body import render_pr
from ..humanize import today_utc
from ..metadata.core import Metadata
from ..pyproject import (
    dependency_group_names,
    extra_names,
    get_project_name,
)
from ..templating import PrTemplate
from .main import (
    LazyChoice,
    dry_run_option,
    exit_if_disabled,
    log_output_target,
    output_option,
    repo_option,
    report_options,
    subcommand,
    write_output,
)

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Sequence

    from click_extra import Context

    from ..config import Config
    from ..deps.sync import SyncOperation, SyncPlan


class DependencyGroup(LazyChoice):
    """A group declared in the project's `[dependency-groups]`.

    `uv` refuses an undeclared group, so the set is closed and naming one it
    does not hold is always a typo.
    """

    name = "group"

    def resolve(self) -> tuple[str, ...]:
        return dependency_group_names()


class ProjectExtra(LazyChoice):
    """An extra declared in the project's `[project.optional-dependencies]`."""

    name = "extra"

    def resolve(self) -> tuple[str, ...]:
        return extra_names()


lockfile_option = option(
    "--lockfile",
    type=file_path(resolve_path=True),
    default="uv.lock",
    help="Path to the uv.lock file.",
)
# The lockfile pair (sync-uv-lock, sync-dep-sources) probes held-back releases
# with a second uv resolution rather than from an already-fetched candidate list,
# so its help text names that cost where the version-sync trio's does not.
lock_held_back_option = option(
    "--held-back/--no-held-back",
    default=True,
    help="Report newer releases withheld by the exclude-newer cooldown "
    "(runs a second uv resolution).",
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
sync_table_option = option(
    "--table/--no-table",
    default=True,
    help="Print a summary table of updated packages.",
)


def _resolve_context(
    ctx: Context,
    config: Config,
    *,
    release_notes: bool,
    held_back: bool,
    dry_run: bool = False,
    lockfile: Path | None = None,
) -> ResolveContext:
    """Build the inputs every resolve of one run shares.

    The `--jobs` width is resolved here, on the command's own thread: a worker
    thread running a resolve has no Click context to read it from. The count
    passed is uncapped, since each fan-out caps it at its own number of items.
    """
    rc = ResolveContext(
        config=config,
        today=today_utc(),
        release_notes=release_notes,
        held_back=held_back,
        dry_run=dry_run,
        jobs=resolve_jobs(ctx, sys.maxsize, serial_at_debug=True),
    )
    if lockfile is not None:
        rc.lockfile = lockfile
    return rc


def print_plan_tables(ctx: Context, plan: SyncPlan) -> None:
    """Print a resolved plan's diff, bypass and held-back tables.

    The terminal counterpart of {func}`~repomatic.deps.sync.render_plan_markdown`,
    in the same order, so a run's terminal output and its PR body read the same
    way. Every dependency updater goes through here: the two lockfile commands,
    the three version-sync commands, and the aggregate `sync-deps`.

    The diff table keeps Old and New as two columns, not the merged `Change`
    cell of the markdown PR body, so `--table-format json` or `csv` output
    stays parseable. Each table respects the global `--table-format`.
    """
    show_released = bool(plan.dates)
    headers: tuple[str, ...] = (plan.subject, "Old", "New")
    if show_released:
        headers = (*headers, "Released")
    rows: list[tuple[str, ...]] = []
    for name, old, new in plan.changes:
        row: tuple[str, ...] = (name, old or "(new)", new or "(removed)")
        if show_released:
            released = format_released(plan.dates.get(name, ""), plan.reference_date)
            row = (*row, released)
        rows.append(row)
    ctx.print_table(rows, headers)

    if plan.uv_project.bypass_forecasts:
        echo("Cooldown bypasses:")
        ctx.print_table(
            [forecast.row for forecast in plan.uv_project.bypass_forecasts],
            BYPASS_COLUMNS,
        )
    if plan.held_back:
        echo("Held back by cooldown:")
        ctx.print_table(
            [pkg.row for pkg in plan.held_back], (plan.subject, *HELD_BACK_COLUMNS)
        )


def emit_plan_report(
    ctx: Context,
    plan: SyncPlan,
    *,
    output: Path | None,
    output_format: str,
    table: bool = True,
) -> None:
    """Print a plan's cutoff, tables and release notes, then write its report.

    The shared tail of every updater command. A plan names one cooldown: uv's
    `exclude-newer` cutoff for the lockfile pair, the `minimum-release-age`
    cutoff for the version-sync trio.

    :param table: Print the cutoff and the tables. The lockfile pair turns
        them off in CI, where only the markdown report is read.
    """
    if table:
        if plan.uv_project.exclude_newer:
            cutoff = format_upload_date(plan.uv_project.exclude_newer)
            echo(f"exclude-newer cutoff: {cutoff}")
        if plan.cutoff is not None:
            echo(f"minimum-release-age cutoff: {plan.cutoff:%Y-%m-%d}")
        print_plan_tables(ctx, plan)
    if plan.notes_section:
        echo("")
        echo(plan.notes_section)
    if output:
        emit_report(render_plan_markdown(plan), output, output_format)


def _enabled_operation(ctx: Context, name: str) -> tuple[SyncOperation, Config]:
    """Look an updater up, exiting when its `[tool.repomatic]` flag is off."""
    op = OPERATIONS_BY_NAME[name]
    exit_if_disabled(ctx, op.config_key)
    return op, get_tool_config(ctx)


def _run_version_sync(
    ctx: Context,
    name: str,
    *,
    output: Path | None,
    output_format: str,
    release_notes: bool,
    held_back: bool,
    up_to_date: str,
) -> None:
    """Shared body of the three version-sync commands.

    `sync-tool-versions`, `sync-action-pins`, and `sync-workflow-pins` differ
    only in their operation and messages: the resolve, apply, and report
    sequence is identical.

    :param up_to_date: Message printed when nothing needs updating.
    """
    op, config = _enabled_operation(ctx, name)
    rc = _resolve_context(ctx, config, release_notes=release_notes, held_back=held_back)
    plan = op.resolve(rc)
    if not plan.has_changes:
        echo(up_to_date)
        ctx.exit(0)

    op.apply(plan)

    echo(f"{len(plan.changes)} {plan.subject.lower()}(s) updated.")
    if plan.self_pin_exemptions:
        echo(
            "Backfilled the cooldown exemption on the self-pin in: "
            + ", ".join(sorted(plan.self_pin_exemptions))
        )
    emit_plan_report(ctx, plan, output=output, output_format=output_format)


def _resolve_lockfile_plan(
    ctx: Context,
    name: str,
    *,
    lockfile: Path,
    table: bool,
    output: Path | None,
    release_notes: bool,
    held_back: bool,
) -> tuple[SyncOperation, SyncPlan]:
    """Resolve one of the two lockfile-mutating operations.

    `sync-uv-lock` and `sync-dep-sources` gate the held-back probe on a consumer
    being present, unlike the version-sync trio: that probe costs a second full
    uv resolution, so a run that prints no table and writes no report must not
    pay for it.

    The apply and the narration stay with each command, whose "what happened"
    lines differ (adopted releases for one, bypass lifecycle for the other).
    """
    op, config = _enabled_operation(ctx, name)
    rc = _resolve_context(
        ctx,
        config,
        release_notes=release_notes,
        held_back=held_back and (table or output is not None),
        lockfile=lockfile,
    )
    return op, op.resolve(rc)


@subcommand(
    short_help="Report (and optionally fix) vulnerable dependencies",
    params=[SortByOption(*AUDIT_HEADER_DEFS, default="package")],
    examples=(
        ("Report known vulnerabilities (read-only)", "repomatic audit"),
        ("Machine-readable output", "repomatic --table-format json audit"),
        (
            "Upgrade fixable packages (mutates uv.lock and pyproject.toml)",
            "repomatic audit --fix",
        ),
        (
            "CI autofix job: write the markdown report as a step output",
            'repomatic audit --fix --repo owner/name --output "$GITHUB_OUTPUT" --output-format github-actions',
        ),
    ),
)
@lockfile_option
@repo_option(
    "Repository in owner/repo format, for the GitHub Advisory Database source.",
    detect=False,
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
@report_options("Write a markdown report to this file.")
@pass_context
def audit(
    ctx: Context,
    lockfile: Path,
    repo: str | None,
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
    them. The GitHub Advisory Database needs a repository, so it is skipped
    without --repo. The table respects the global --table-format option
    (github, json, csv, etc.), so --table-format json yields a
    machine-readable report. Report mode exits 1 when any vulnerability is
    found (use --exit-zero to override), so it can gate CI.

    \b
    With --fix, upgrades each fixable package with uv lock --upgrade-package
    and:
      - bypasses the exclude-newer cooldown for security fixes
      - persists exclude-newer-package entries in pyproject.toml
      - prints a markdown report of vulnerabilities and version changes
    """
    config = get_tool_config(ctx)

    # The parsed list goes to the library as is, even when empty: the library
    # skips a source it cannot query, and only `None` means "every source".
    sources = AdvisorySource.from_config(config.vulnerable_deps.sources)

    if fix:
        # vulnerable-deps.sync gates the autofix (mutation), not reporting.
        exit_if_disabled(ctx, "vulnerable-deps.sync")

        result = fix_vulnerable_deps(lockfile, repo=repo, sources=sources)
        if not result.has_fixes:
            echo("No fixable vulnerabilities found.")
            ctx.exit(0)

        echo("Upgraded vulnerable packages.")
        report = result.render(today_utc())
        echo(report)
        # Keep the github-actions key as `diff_table`: the autofix
        # workflow's pr-metadata step reads steps.fix.outputs.diff_table_file.
        emit_report(report, output, output_format)
        ctx.exit(0)

    # Report mode (default, read-only).
    vulns = collect_vulnerable_packages(lockfile, repo=repo, sources=sources)
    if not vulns:
        echo("No known vulnerabilities found.")
        ctx.exit(0)

    ctx.print_table([v.row for v in vulns], AUDIT_HEADER_DEFS)

    if output:
        emit_report(
            format_vulnerability_table(vulns), output, output_format, key="vuln_table"
        )

    ctx.exit(0 if exit_zero else 1)


@subcommand(
    short_help="Check dependencies resolve from the public index",
    params=[SortByOption(*LINT_DEPS_HEADER_DEFS, default="package")],
    examples=(
        ("Check the current project", "repomatic lint-deps"),
        (
            "Report without failing, for an ordinary CI push",
            "repomatic lint-deps --no-fatal",
        ),
        (
            "Emit a markdown report for a PR body",
            "repomatic lint-deps --no-fatal --output report.md",
        ),
    ),
)
@option(
    "--pyproject",
    "pyproject_path",
    type=file_path(resolve_path=True),
    default="pyproject.toml",
    help="Path to the pyproject.toml file.",
)
@lockfile_option
@option(
    "--fatal/--no-fatal",
    default=True,
    help=(
        "Exit non-zero when a finding blocks a release. Pass --no-fatal to"
        " report without failing, for continuous visibility on ordinary"
        " pushes."
    ),
)
@option(
    "--policy/--no-policy",
    default=True,
    help=(
        "Also report declarations departing from the project's version"
        " policy: upper bounds, missing floors, unsorted lists, misplaced"
        " type stubs, uncommented and over-long floor comments. Never blocks"
        " a release."
    ),
)
@report_options("Write a markdown report of the blocking findings to this file.")
@pass_context
def lint_deps(
    ctx: Context,
    pyproject_path: Path,
    lockfile: Path,
    fatal: bool,
    policy: bool,
    output: Path | None,
    output_format: str,
) -> None:
    """Check that every dependency resolves from the index users install from.

    A dependency is shippable when whoever installs the published artifact
    gets the same code the release was tested against. A git branch, a local
    path, a fork, a direct URL or a private index all break that, and most of
    them break it silently: a `[tool.uv.sources]` override never reaches the
    published metadata, so the wheel builds and uploads exactly as it would
    have, and only the install fails.

    Runs entirely offline, reading pyproject.toml and uv.lock. The lockfile
    pass is what catches a source override on a package no table names, since
    it records the resolved origin of the whole tree.

    Reads lint-deps.allow from [tool.repomatic] for packages exempted by
    name, each mapped to the reason it is safe, and
    lint-deps.comment-word-threshold for the length a floor comment may run to.

    \b
    Output symbols:
        ✗  Blocks a release
        ⚠  Warning, does not block
        ℹ  Covered by lint-deps.allow

    \b
    Exit codes:
        0  Nothing blocks a release, or --no-fatal was passed.
        1  At least one finding blocks a release.
    """
    config = get_tool_config(ctx)
    findings = scan_project(
        pyproject_path,
        lockfile,
        config.minimum_release_age,
        allow=config.lint_deps.allow,
    )

    policy_findings = (
        scan_policy(pyproject_path, config.lint_deps.comment_word_threshold)
        if policy
        else []
    )

    # The clean bill of health covers shippability alone, so it prints whenever
    # there is no source finding, style findings or not: a policy warning is
    # not a claim about where a dependency resolves from.
    if not findings:
        echo("✓ Every dependency resolves from the public package index.")
        if not policy_findings:
            ctx.exit(0)
    else:
        ctx.print_table([finding.row for finding in findings], LINT_DEPS_HEADER_DEFS)

    blocking = [finding for finding in findings if finding.blocking]
    for finding in findings:
        if finding.allowed:
            echo(f"ℹ {finding.message}")
        elif finding.blocking:
            emit_annotation(AnnotationLevel.ERROR, finding.message)
            echo(f"✗ {finding.message}")
        else:
            emit_annotation(AnnotationLevel.WARNING, finding.message)
            echo(f"⚠ {finding.message}")

    # Style findings render after the shippability ones and never reach the
    # exit code: a release is not held for an uncommented floor.
    for policy_finding in policy_findings:
        emit_annotation(AnnotationLevel.WARNING, policy_finding.message)
        echo(f"⚠ {policy_finding.message}")

    if output:
        log_output_target("dependency blockers", output)
        emit_report(
            format_blocker_section(findings),
            output,
            output_format,
            key="release_blockers",
        )

    ctx.exit(1 if blocking and fatal else 0)


@subcommand(
    short_help="Generate dependency graph from uv lockfile",
    examples=(
        ("Generate Mermaid graph", "repomatic update-dep-graph"),
        ("Include test dependencies", "repomatic update-dep-graph --group test"),
        (
            "Include all groups and extras",
            "repomatic update-dep-graph --all-groups --all-extras",
        ),
        (
            "Include all groups except typing",
            "repomatic update-dep-graph --all-groups --no-group typing",
        ),
        (
            "Include all extras except one",
            "repomatic update-dep-graph --all-extras --no-extra json5",
        ),
        (
            "Show only test group dependencies (no main deps)",
            "repomatic update-dep-graph --only-group test",
        ),
        (
            "Show only a specific extra's dependencies",
            "repomatic update-dep-graph --only-extra xml",
        ),
        (
            "Focus on a specific package",
            "repomatic update-dep-graph --package click-extra",
        ),
        ("Limit graph depth to 2 levels", "repomatic update-dep-graph --level 2"),
        (
            "Save to file",
            "repomatic update-dep-graph --output ./docs/assets/dependencies.mmd",
        ),
    ),
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
@output_option(
    "Write the graph to this file. Defaults to [tool.repomatic]"
    " dependency-graph.output, then to stdout."
)
@pass_context
def update_dep_graph(
    ctx: Context,
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
    """
    config = get_tool_config(ctx)
    exit_if_disabled(ctx, "dependency-graph.update")

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

    write_output("graph", graph, output)


def _sync_pull_requests(
    ctx: Context, operations: Sequence[SyncOperation], rc: ResolveContext
) -> list[str]:
    """Run each updater from a clean tree, and converge its pull request.

    Each updater owns a branch and a pull request named after it, so a reviewer
    merges or reverts one without the others, and the tree goes back to `HEAD`
    before each one, so no diff bleeds into the next. An updater with nothing
    to act on in this tree (no `uv.lock`, no workflow file, no git-tracked
    source) resolves nothing, but its pull request is still converged: a stale
    one closes.

    A failed updater leaves its pull request as it is, and never stops the
    updaters after it.

    :return: The name of each updater that failed.
    """
    md = Metadata()
    assignees = (md.event_actor,) if md.event_actor else ()
    failed: list[str] = []
    for op in operations:
        git_ops.discard_changes()
        report = ""
        try:
            if op.applies_here():
                plan = op.resolve(rc)
                if plan.has_changes:
                    op.apply(plan)
                    echo(f"{op.name} updated {len(plan.changes)} item(s):")
                    print_plan_tables(ctx, plan)
                    report = render_plan_markdown(plan)
            template = PrTemplate.load(op.name)
            content = render_pr(
                md, rc.config, template=template, extra_args={"diff_table": report}
            )
            result = upsert_pr(
                branch=op.name,
                title=content.title,
                body=content.body,
                commit_message=content.commit_message,
                labels=template.labels,
                assignees=assignees,
                draft=template.draft,
            )
        except Exception:
            logging.exception(f"{op.name} failed.")
            emit_annotation(
                AnnotationLevel.ERROR,
                f"{op.name} failed, so its pull request is left as it is.",
            )
            failed.append(op.name)
            continue
        number = f" PR #{result.number}" if result.number else ""
        echo(f"{op.name}: {result.operation}{number} on {result.branch}")
    return failed


@subcommand(
    short_help="Update dependencies, all or a named subset",
    examples=(
        ("Update everything enabled", "repomatic sync-deps"),
        (
            "Only the lockfile and action pins",
            "repomatic sync-deps sync-uv-lock sync-action-pins",
        ),
        ("Preview without writing", "repomatic sync-deps --dry-run"),
        (
            "CI: one pull request per updater",
            "repomatic sync-deps --pr-sync --release-notes sync-uv-lock",
        ),
    ),
)
@argument(
    "operations",
    nargs=-1,
    type=Choice(list(OPERATIONS_BY_NAME)),
)
@report_options(
    "Write a combined markdown report (one section per updater) to this file."
)
@sync_release_notes_option
@sync_held_back_option
# `sync-deps` is the "do it" aggregate, so it runs live unless asked not to.
@dry_run_option(default=False)
@option(
    "--pr-sync",
    is_flag=True,
    help=(
        "Run the updaters one after the other, each from a clean tree, and open,"
        " refresh or close the pull request of each, as `pr-sync` does. The mode"
        " the autofix workflow runs."
    ),
)
@pass_context
def sync_deps(
    ctx: Context,
    operations: tuple[str, ...],
    output: Path | None,
    output_format: str,
    release_notes: bool,
    held_back: bool,
    dry_run: bool,
    pr_sync: bool,
) -> None:
    """Update project dependencies, the whole set or a named subset.

    \b
    The single entry point for dependency updates. It drives
    sync-dep-sources, sync-uv-lock, sync-action-pins, sync-workflow-pins, and
    sync-tool-versions: their network discovery runs concurrently (one shared
    HTTP cache, one spinner), then the file rewrites apply serially because
    two of them touch the same workflow files. Name one or more updaters to
    run just those; with none named, every enabled updater runs.

    \b
    The [tool.repomatic] feature flags are always authoritative: a disabled
    updater never runs. With no names, updaters not meaningful in the current
    working tree are skipped too (sync-tool-versions outside the repomatic
    checkout, the pin updaters without workflow files); naming one runs it
    regardless of the working tree.

    \b
    With --pr-sync, each updater instead runs on its own, from a tree put back
    to HEAD, and opens, refreshes or closes the pull request on the branch
    named after it. A named updater with nothing to act on in this tree runs
    nothing, and only closes a stale pull request. A failed updater leaves its
    pull request alone, the others still run, and the command exits 1.
    """
    if pr_sync and (dry_run or output):
        msg = (
            "--pr-sync writes each report into its pull request: it takes no"
            " --dry-run and no --output."
        )
        raise UsageError(msg)

    config = get_tool_config(ctx)
    selected = selected_operations(config, names=list(operations) or None)
    if not selected:
        if operations:
            echo("None of the named updaters are enabled in [tool.repomatic].")
        else:
            echo("No dependency updaters are enabled for this repository.")
        ctx.exit(0)

    rc = _resolve_context(
        ctx, config, release_notes=release_notes, held_back=held_back, dry_run=dry_run
    )
    if pr_sync:
        pr_failures = _sync_pull_requests(ctx, selected, rc)
        if pr_failures:
            echo(f"Failed: {', '.join(pr_failures)}", err=True)
            ctx.exit(1)
        return

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
        print_plan_tables(ctx, plan)

    if output:
        sections = [
            body for _op, plan in changed if (body := render_plan_markdown(plan))
        ]
        emit_report("\n\n".join(sections), output, output_format)

    if failed:
        echo(f"Failed to resolve: {', '.join(failed)}", err=True)
        ctx.exit(1)


@subcommand(
    short_help="Re-lock dependencies and roll cooldown overrides forward",
    examples=(
        ("Upgrade and show changes", "repomatic sync-uv-lock"),
        ("With release notes", "repomatic sync-uv-lock --release-notes"),
        (
            "Render the report as a GitHub-flavored table",
            "repomatic --table-format github sync-uv-lock",
        ),
        ("Render the report as JSON", "repomatic --table-format json sync-uv-lock"),
        (
            "CI: write markdown report as a GitHub Actions step output",
            'repomatic sync-uv-lock --no-table --release-notes --output "$GITHUB_OUTPUT" --output-format github-actions',
        ),
    ),
)
@lockfile_option
@sync_table_option
@sync_release_notes_option
@lock_held_back_option
@report_options("Write a markdown report (table and release notes) to this file.")
@pass_context
def sync_uv_lock(
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
        machine resolves the lockfile against the same uv floor and cooldown
      - prunes exclude-newer-package entries from pyproject.toml whose held
        version has aged past the exclude-newer cutoff, or whose own cutoff
        has when the lock holds no version, then freezes the survivors at
        their locked version (a fixed date) so the upgrade holds them
        instead of tracking newer releases
      - prints a table of updated packages with upload dates
      - reports the cooldown-bypass lifecycle: entries pruned or frozen by
        the run, and each active freeze with the date it expires
      - optionally fetches release notes from GitHub (markdown)
      - optionally reports newer releases held back by the cooldown, with the
        date each ages out of the exclude-newer window (--held-back)

    \b
    The table respects the global --table-format option (github, json,
    csv, etc.). Release notes are always rendered as markdown.
    """
    op, plan = _resolve_lockfile_plan(
        ctx,
        "sync-uv-lock",
        lockfile=lockfile,
        table=table,
        output=output,
        release_notes=release_notes,
        held_back=held_back,
    )

    if plan.uv_project.pins_synced:
        echo("Synced [tool.uv] policy pins from the bundled template.")

    if not plan.has_changes:
        echo("No dependency changes.")
        ctx.exit(0)

    op.apply(plan)

    if plan.changes:
        echo(f"{len(plan.changes)} package(s) updated.")
    if plan.uv_project.pruned_bypasses:
        echo(
            "Expired cooldown bypass(es) cleared from pyproject.toml: "
            + ", ".join(forecast.name for forecast in plan.uv_project.pruned_bypasses)
        )
    if plan.uv_project.frozen_bypasses:
        echo(
            "Cooldown bypass(es) frozen at their locked version: "
            + ", ".join(plan.uv_project.frozen_bypasses)
        )

    emit_plan_report(ctx, plan, output=output, output_format=output_format, table=table)


@subcommand(
    short_help="Swap git-tracked dependencies to their released versions",
    examples=(
        ("Swap whatever is ready and show changes", "repomatic sync-dep-sources"),
        (
            "CI: write markdown report as a GitHub Actions step output",
            'repomatic sync-dep-sources --no-table --release-notes --output "$GITHUB_OUTPUT" --output-format github-actions',
        ),
    ),
)
@lockfile_option
@sync_table_option
@sync_release_notes_option
@lock_held_back_option
@report_options("Write a markdown report (table and release notes) to this file.")
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
    """
    op, plan = _resolve_lockfile_plan(
        ctx,
        "sync-dep-sources",
        lockfile=lockfile,
        table=table,
        output=output,
        release_notes=release_notes,
        held_back=held_back,
    )

    if not plan.has_changes:
        echo("No awaited release shipped.")
        ctx.exit(0)

    op.apply(plan)

    for swap in plan.uv_project.source_swaps:
        echo(
            f"Swapped {swap.name} from git branch {swap.branch!r} to released"
            f" {swap.release}, frozen until it clears the cooldown."
        )
    if plan.changes:
        echo(f"{len(plan.changes)} package(s) updated.")

    emit_plan_report(ctx, plan, output=output, output_format=output_format, table=table)


@subcommand(
    short_help="Bump registry tool versions from upstream releases",
    examples=(
        ("Bump every registry tool past the cooldown", "repomatic sync-tool-versions"),
        (
            "CI: write a markdown report as a GitHub Actions step output",
            'repomatic sync-tool-versions --output "$GITHUB_OUTPUT" --output-format github-actions',
        ),
    ),
)
@report_options("Write a markdown report (version table) to this file.")
@sync_release_notes_option
@sync_held_back_option
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
    tools, PyPI otherwise), writes it into tooling/registry.py, recomputes binary
    checksums in the same pass, and refreshes the bundled actionlint problem
    matcher from the tag of the actionlint it pins.

    \b
    Upstream-only: it rewrites repomatic's own package source, so invoke it with
    `uv run` (editable), never `uvx` (whose isolated wheel is discarded).
    """
    _run_version_sync(
        ctx,
        "sync-tool-versions",
        output=output,
        output_format=output_format,
        release_notes=release_notes,
        held_back=held_back,
        up_to_date="All tools are up to date.",
    )


@subcommand(
    short_help="Bump SHA-pinned GitHub Actions to their latest release",
    examples=(
        (
            "Bump every SHA-pinned action past the cooldown",
            "repomatic sync-action-pins",
        ),
    ),
)
@report_options("Write a markdown report (version table) to this file.")
@sync_release_notes_option
@sync_held_back_option
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
    """
    _run_version_sync(
        ctx,
        "sync-action-pins",
        output=output,
        output_format=output_format,
        release_notes=release_notes,
        held_back=held_back,
        up_to_date="All action pins are up to date.",
    )


@subcommand(
    short_help="Bump npm/PyPI version literals in workflow YAML",
    examples=(
        (
            "Bump every workflow version literal past the cooldown",
            "repomatic sync-workflow-pins",
        ),
    ),
)
@report_options("Write a markdown report (version table) to this file.")
@sync_release_notes_option
@sync_held_back_option
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
    """
    _run_version_sync(
        ctx,
        "sync-workflow-pins",
        output=output,
        output_format=output_format,
        release_notes=release_notes,
        held_back=held_back,
        up_to_date="All workflow pins are up to date.",
    )
