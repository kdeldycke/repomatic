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
"""CI commands of the `repomatic` CLI: run status, runners and the test matrix.

{data}`repomatic.cli.main.COMMANDS` registers each command here, and files it
under its help section.
"""

from __future__ import annotations

from pathlib import Path

from click_extra import (
    ClickException,
    EnumChoice,
    FloatRange,
    SortByOption,
    UsageError,
    argument,
    dir_path,
    echo,
    file_path,
    get_tool_config,
    option,
    pass_context,
)

from ..github.ci_status import (
    CI_STATUS_HEADER_DEFS,
    STABLE_GLYPH,
    UNSTABLE_GLYPH,
    WatchState,
    monitored_workflows,
    read_ci_status,
    watch_ci_status,
    workflow_files,
    write_failed_logs,
)
from ..github.job_timings import (
    JOB_TIMINGS_HEADER_DEFS,
    fetch_job_timings,
    render_markdown,
    summarize,
)
from ..github.outputs import AnnotationLevel, report_finding
from ..github.pr import list_automation_prs, release_pr as release_pr_status
from ..github.runs import cancel_superseded_runs
from ..github.workflow_files import WORKFLOW_DIR, literal_runners
from ..matrix.axes import (
    KNOWN_RUNNERS,
    MatrixName,
    python_version_sort_key,
)
from ..matrix.model import (
    JOB_STATE_KEY,
    OS_AXIS,
    PYTHON_VERSION_AXIS,
)
from ..matrix.runner_sync import (
    AXES_SOURCE_PATH,
    plan_runner_changes,
    render_change_table,
)
from ..matrix.runners import fetch_catalog
from ..metadata.core import Metadata
from ..release.protocol import RELEASE_PR_BRANCH
from ..upstream import is_source_repo
from .main import (
    LazyChoice,
    dry_run_option,
    exit_if_disabled,
    output_option,
    subcommand,
    write_output,
)

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections import Counter
    from collections.abc import Callable, Mapping, Sequence
    from typing import Any

    from click_extra import Context


AXIS_HEADER_LABELS = {
    OS_AXIS: "OS",
    PYTHON_VERSION_AXIS: "Python",
    JOB_STATE_KEY: "State",
}
"""Display names for the job keys `show-test-matrix` heads a row or column with.

A key absent from here heads its column under the raw name a matrix declares
it as, which is also how a caller names it on the command line.
"""

UNIVERSAL_AXIS_KEYS = (OS_AXIS, PYTHON_VERSION_AXIS, JOB_STATE_KEY)
"""The job keys every test matrix carries, whatever a project configures.

The base cross-product declares the first two and the state include stamps the
third, so no `[tool.repomatic.test-matrix]` directive can leave a matrix
without them. That makes them the safe answer when the real matrix cannot be
read at all, which is the only time {class}`MatrixAxis` needs one.
"""

CELL_STATE_SEPARATOR = ", "
"""Joins the labels of the distinct states one grid cell holds."""

EMPTY_CELL = "—"
"""What a grid cell shows where the matrix puts no job, like an excluded one."""

JOB_COUNT_MARK = "×"
"""Introduces the job count of a cell standing for more than one job.

A cell collapses every job at its intersection, so a matrix varying on a third
axis renders five jobs exactly like one. The mark is the East Asian Ambiguous
U+00D7, like the {data}`EMPTY_CELL` placeholder: a terminal drawing ambiguous
characters double-width misaligns both alike, and neither before the other.
"""

TEST_MATRIX_STATE_DISPLAY = {
    "stable": f"{STABLE_GLYPH} stable",
    "unstable": f"{UNSTABLE_GLYPH} unstable",
}
"""Emoji-decorated labels for job states in the `show-test-matrix` grid.

The same two glyphs the workflow templates stamp onto each matrix job's name,
and that {meth}`repomatic.github.ci_status.JobStatus.required` reads back off
it, so the grid and the CI verdict cannot come to disagree about which mark
means "allowed to fail".
"""


class MatrixAxis(LazyChoice):
    """A job key naming one axis of the `show-test-matrix` grid.

    A `Choice` rather than a bare type, so the accepted keys reach the reader
    through Click's own `[a|b|c]` metavar and click-extra's choice styling,
    and the shell completes them: a hand-written help line would render none
    of that. Its values are read at render time rather than declared at
    import, because they are the project's own. A repository varying its
    matrix on a `click-version` can pivot on it, and that is exactly the axis
    a fixed list would omit.

    The listing is the full matrix's, the superset: `variations` and
    `full-include` rows reach it alone, while every other directive reaches
    both matrices. Which one the caller names is not parsed yet when a type
    converts, so `show-test-matrix` re-checks the axis against the
    matrix they did name, and refuses the few keys `pr` drops.
    """

    name = "axis"

    fallback = UNIVERSAL_AXIS_KEYS
    """Every matrix carries these three, so an unreadable one still pivots."""

    def resolve(self) -> tuple[str, ...]:
        return Metadata().test_matrix.job_keys()


class WorkflowFile(LazyChoice):
    """A workflow file name, as `.github/workflows/` spells it.

    Both commands taking one read the current repository's own CI, so the
    checkout is the authority on what may be named. Every file counts, not
    just the ones {func}`~repomatic.github.ci_status.monitored_workflows`
    watches by default: a schedule-only workflow has runs worth reading too.
    """

    name = "workflow"

    def resolve(self) -> tuple[str, ...]:
        return workflow_files(WORKFLOW_DIR)


def matrix_axis_sort_key(
    axis: str, matrix_name: MatrixName
) -> Callable[[str], Any] | None:
    """Canonical ordering for one grid axis, or `None` to keep job order.

    Keyed on which axis it is rather than on which side of the grid it landed:
    a transposed grid earns the runner order its columns get by default, and
    reads as arbitrarily shuffled without it. An axis the test matrix does not
    define an order for (a `click-version`) keeps the order the job stream
    presents it in, which is the matrix author's own.
    """
    if axis == PYTHON_VERSION_AXIS:
        # Job order appends single-runner build flavors (like 3.14t) after
        # every base version, rather than beside the version they build.
        return python_version_sort_key
    if axis == OS_AXIS:
        # An include directive or a full-include flattening can perturb
        # first-seen runner order. Runners outside the canonical tuple keep
        # their first-seen order after it.
        canonical = matrix_name.runners
        rank = {runner: index for index, runner in enumerate(canonical)}
        return lambda runner: rank.get(runner, len(canonical))
    return None


def state_label(state: str, emoji: bool = True) -> str:
    """Label one job state, glyph-decorated unless `emoji` says otherwise.

    A state the matrix carries but this CLI has no label for renders as
    itself, so a new one shows up in the grid rather than vanishing from it.
    """
    return TEST_MATRIX_STATE_DISPLAY.get(state, state) if emoji else state


def flat_matrix_table(
    jobs: Sequence[Mapping[str, str]],
    leading: Sequence[str] = (),
    emoji: bool = True,
) -> tuple[tuple[str, ...], tuple[tuple[str, ...], ...]]:
    """Lay a solved job stream out as one row per job, one column per key.

    A grid has two axes and collapses every other one into its cells, which is
    what a matrix varying on a third loses. Here each key is a column instead,
    so nothing collapses and the table is the job list CI will run.

    Columns are collected across the whole stream, since a job may carry a key
    its neighbour does not (the `python-label` only a prerelease needs), and
    the state lands last as the outcome the other columns explain. A job
    missing a key renders empty there rather than shifting its row.

    :param jobs: The solved job stream, already in the order to render.
    :param leading: Keys to column first, in this order, before the ones the
        stream contributes. A listing sorted on an axis reads as unsorted with
        that axis buried in the middle, so the caller leads with the axes it
        ordered by. A key no job carries is skipped rather than columned empty.
    :param emoji: Label the state with its glyph rather than its bare word.
    :return: A `(headers, rows)` pair, in the order `print_table` takes them
        the other way round.
    """
    present = list(dict.fromkeys(key for job in jobs for key in job))
    keys = [key for key in dict.fromkeys(leading) if key in present]
    keys += [key for key in present if key not in keys]
    if JOB_STATE_KEY in keys:
        keys.append(keys.pop(keys.index(JOB_STATE_KEY)))
    headers = tuple(AXIS_HEADER_LABELS.get(key, key) for key in keys)
    rows = tuple(
        tuple(
            state_label(job.get(key, ""), emoji)
            if key == JOB_STATE_KEY
            else job.get(key, "")
            for key in keys
        )
        for job in jobs
    )
    return headers, rows


def format_matrix_cell(tally: Counter[str] | None, emoji: bool = True) -> str:
    """Render one `show-test-matrix` grid cell from the jobs that landed in it.

    Every state is labelled on its own. A cell holds more than one when the
    matrix carries an axis beyond its two (a `click-version` variation, say):
    both jobs land on the same intersection, and each keeps its own label.

    A state carrying several jobs also states how many, since the grid gives a
    reader no other way to tell five stable jobs from one.

    :param tally: That intersection's job count per state, as
        {meth}`~repomatic.matrix.model.Matrix.pivot_counts` reports it, or
        `None` where the matrix puts no job at all.
    :param emoji: Label each state with its glyph rather than its bare word. A
        count is not decoration, and shows either way.
    :return: The cell text, or {data}`EMPTY_CELL` for an empty intersection.
    """
    if tally is None:
        return EMPTY_CELL
    labels = []
    for state, count in tally.items():
        label = state_label(state, emoji)
        labels.append(f"{label} {JOB_COUNT_MARK}{count}" if count > 1 else label)
    return CELL_STATE_SEPARATOR.join(labels)


@subcommand(
    short_help="Cancel in-progress workflow runs for a branch",
    examples=(
        (
            "From the cancel-runs workflow, sparing its own run",
            'repomatic cancel-runs --branch "$BRANCH"',
        ),
    ),
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
    """
    try:
        cancelled = cancel_superseded_runs(branch, current_run_id)
    except RuntimeError as error:
        msg = f"Cannot list the live runs of {branch!r}: {error}"
        raise ClickException(msg) from error
    echo(f"Cancelled {cancelled} run(s) for branch {branch!r}.")


@subcommand(
    short_help="Report which CI jobs are red, and which of them gate a merge",
    params=[SortByOption(*CI_STATUS_HEADER_DEFS, default="workflow")],
    examples=(
        ("What is red on main right now", "repomatic ci-status"),
        (
            "One workflow, without failing the shell",
            "repomatic ci-status --workflow tests.yaml --no-fatal",
        ),
    ),
)
@option(
    "--branch",
    default="main",
    show_default=True,
    help="Branch whose latest run of each workflow to read.",
)
@option(
    "--workflow",
    "workflows",
    type=WorkflowFile(),
    # A dozen file names spelled into the choice metavar overflow the help
    # column. The help text below points at the directory holding them, the
    # shell completes them, and a rejected value lists every one.
    metavar="WORKFLOW",
    multiple=True,
    help=(
        "Workflow file to read, repeatable. Defaults to every workflow a push"
        " can start, derived from .github/workflows/."
    ),
)
@option(
    "--fatal/--no-fatal",
    default=True,
    help="Exit non-zero when a required job failed.",
)
@option(
    "--wait/--no-wait",
    default=False,
    help=(
        "Poll until a required job fails, or until every run settled and the tip"
        " has a run of its own. Each poll prints the runs whose verdict changed."
    ),
)
@option(
    "--interval",
    type=FloatRange(min=5),
    default=60,
    show_default=True,
    help="Seconds between two polls under --wait.",
)
@option(
    "--until-change",
    is_flag=True,
    help=(
        "Under --wait, stop after the first poll reporting a verdict the state"
        " file has not seen, so a re-armed watch wakes once per event."
    ),
)
@option(
    "--state-file",
    type=file_path(writable=True, resolve_path=True),
    help=(
        "JSON file of the verdicts already reported: read before the first poll,"
        " written on every change."
    ),
)
@option(
    "--timeout",
    type=FloatRange(min=0),
    default=0,
    show_default=True,
    help="Seconds after which --wait gives up, 0 for no limit.",
)
@option(
    "--failed-logs",
    type=dir_path(resolve_path=True),
    help=(
        "Write the log of every failed required job into this directory, control"
        " sequences stripped. A settled job's log is readable while its run goes."
    ),
)
@option(
    "--automation-prs",
    is_flag=True,
    help=(
        "Also list the open pull requests of the automation, with how many"
        " commits each one lags its base."
    ),
)
@option(
    "--release-pr",
    is_flag=True,
    help=(
        "Also report the open release pull request: its draft state, head and"
        " how many commits it lags the branch."
    ),
)
@pass_context
def ci_status(
    ctx: Context,
    branch: str,
    workflows: tuple[str, ...],
    fatal: bool,
    wait: bool,
    interval: float,
    until_change: bool,
    state_file: Path | None,
    timeout: float,
    failed_logs: Path | None,
    automation_prs: bool,
    release_pr: bool,
) -> None:
    """Report the latest CI run of each workflow, and what is actually broken.

    Reads jobs rather than runs, so a crashed allowed-failure probe cannot
    hide inside a green run conclusion and a run still reading "queued"
    cannot hide the dozen jobs of it that already finished.

    A job whose name carries the unstable glyph anywhere is allowed to fail
    and never affects the exit code. Every other job is required, including
    the non-matrix ones that carry no glyph at all. A matrix job whose name
    still holds its template never evaluated: no cell of it ran.

    A run that was cancelled, or that ran no job, is reported as such and never
    as green: it has no result for its commit. A workflow that ran no job on
    the tip is read on the last commit whose run of it was not skipped.

    --wait polls in-process, at --interval, and ends on the first failed
    required job rather than at the end of the slowest matrix: that is the
    moment to act. --until-change and --state-file turn it into a watch that
    a scheduler re-arms, waking once per event. --failed-logs then fetches
    the logs to read, one file per failed job.
    """
    names = list(workflows) or monitored_workflows(WORKFLOW_DIR)
    if not names:
        echo("No workflow to read.")
        ctx.exit(0)

    try:
        if wait:
            state = WatchState.load(state_file)
            polls = watch_ci_status(
                names,
                branch,
                state=state,
                interval=interval,
                until_change=until_change,
                timeout=timeout,
            )
            for status, changed in polls:
                for run in changed:
                    echo(f"⏱ {run.workflow} ({run.head_sha[:8]}): {run.verdict}")
        else:
            status = read_ci_status(names, branch)
    except RuntimeError as error:
        msg = f"Cannot read the CI runs of {branch!r}: {error}"
        raise ClickException(msg) from error
    if not status.runs:
        echo(f"No run found on {branch!r} for: {', '.join(names)}.")
        ctx.exit(0)

    ctx.print_table([run.cells() for run in status.runs], CI_STATUS_HEADER_DEFS)

    # Only what gates a merge becomes an annotation: a warning names a run
    # that holds nothing up.
    for run in status.runs:
        for level, message in run.findings():
            if level is AnnotationLevel.ERROR:
                report_finding(level, message)
            else:
                echo(f"{level.glyph} {message}")

    if status.skipped_on_tip:
        echo(
            f"… no job ran on the tip of {branch!r} ({status.tip_sha[:8]}) for:"
            f" {', '.join(status.skipped_on_tip)}. Each one is read on an earlier"
            " commit."
        )
    elif status.tip_sha and not status.runs_on_tip:
        echo(
            f"… no run on the tip of {branch!r} ({status.tip_sha[:8]}) yet: every"
            " run above belongs to an earlier commit."
        )
    if not status.settled:
        echo("… some jobs have not settled yet.")

    if failed_logs is not None:
        for path in write_failed_logs(status, failed_logs):
            echo(f"📄 {path}")

    if automation_prs:
        rows = list_automation_prs()
        if not rows:
            echo("No automation pull request is open.")
        for pr in rows:
            lag = pr.get("behindBy")
            behind = "lag unknown" if lag is None else f"{lag} commit(s) behind"
            counts = f"+{pr['additions']}/-{pr['deletions']}"
            echo(
                f"🔀 #{pr['number']} {pr['headRefName']}: {pr['title']}"
                f" ({pr['changedFiles']} file(s), {counts}, {behind}"
                f" {pr['baseRefName']}) {pr['url']}"
            )

    if release_pr:
        release = release_pr_status(branch)
        if release is None:
            echo(f"No release pull request is open on {RELEASE_PR_BRANCH!r}.")
        else:
            lag = release.get("behindBy")
            behind = "lag unknown" if lag is None else f"{lag} commit(s) behind"
            draft = "draft" if release.get("isDraft") else "ready for review"
            head = str(release.get("headRefOid") or "")[:8]
            echo(
                f"🚀 Release PR #{release['number']} ({draft}): {release['title']},"
                f" head {head}, {behind} {branch}. {release['url']}"
            )

    ctx.exit(1 if status.blocking and fatal else 0)


@subcommand(
    short_help="Measure how long each runner image takes, from finished runs",
    params=[SortByOption(*JOB_TIMINGS_HEADER_DEFS, default="median")],
    examples=(
        ("Which image is holding the matrix up", "repomatic job-timings"),
        (
            "A wider sample, written out for documentation",
            "repomatic job-timings --limit 10 --output timings.md",
        ),
    ),
)
@option(
    "--workflow",
    type=WorkflowFile(),
    metavar="WORKFLOW",
    default="tests.yaml",
    show_default=True,
    help="Workflow file whose runs to sample, from .github/workflows/.",
)
@option(
    "--branch",
    default="main",
    show_default=True,
    help="Branch whose runs to sample.",
)
@option(
    "--limit",
    default=5,
    show_default=True,
    type=int,
    help="How many recent successful runs to sample.",
)
@output_option("Write the report as a Markdown table to this file, for documentation.")
@pass_context
def job_timings(
    ctx: Context, workflow: str, branch: str, limit: int, output: Path | None
) -> None:
    """Report median whole-job wall-clock per runner image.

    Runner choice rests on measurement, and the measurement that matters is
    the whole job: a benchmark that times only the tool pass misses checkout
    and install. The jobs API reports start and end timestamps, so what this
    reads is whole-job by construction.

    Only successful runs are sampled, because a failed run's jobs stop early
    and time where the failure landed rather than what the image costs. The
    figure is a median across several runs, which is what turns a queue stall
    into noise instead of a verdict.
    """
    timings = fetch_job_timings(workflow, branch, limit)
    if not timings:
        echo(
            f"No finished job found in the last {limit} successful "
            f"{workflow!r} run(s) on {branch!r}."
        )
        ctx.exit(0)

    reports = summarize(timings)
    ctx.print_table([report.cells() for report in reports], JOB_TIMINGS_HEADER_DEFS)

    if output:
        write_output(
            "job timings report", render_markdown(reports, workflow, limit), output
        )


@subcommand(
    short_help="Render the CI test matrix as a grid",
    examples=(
        ("One row per job", "repomatic show-test-matrix"),
        ("The PR subset, in plain words", "repomatic show-test-matrix pr --no-emoji"),
        (
            "Pivot columns on a custom axis",
            "repomatic show-test-matrix --col-axis click-version",
        ),
        ("The compact two-axis grid", "repomatic show-test-matrix --grid"),
        (
            "Choose both grid axes",
            "repomatic show-test-matrix --grid --row-axis os --col-axis python-version",
        ),
        (
            "The full matrix as a GitHub-flavored table",
            "repomatic --table-format github show-test-matrix full",
        ),
    ),
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
    metavar=f"[{'|'.join(MatrixName)}]",
    type=EnumChoice(MatrixName),
    default=MatrixName.FULL,
    required=False,
)
@pass_context
def show_test_matrix(
    ctx: Context,
    emoji: bool,
    grid: bool,
    row_axis: str,
    col_axis: str,
    matrix_name: MatrixName,
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
    """
    matrix = Metadata().test_matrix_named(matrix_name)
    # An axis no job carries pivots into an empty grid, which reads as a matrix
    # with nothing in it rather than as the typo it is. The `--help` listing
    # offers the full matrix's keys, so a `pr` grid lands here on the few the
    # reduced matrix drops.
    job_keys = matrix.job_keys()
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
    tallies = matrix.pivot_counts(row_axis, col_axis)
    # First-seen order, then the canonical one where the axis has one.
    row_values = list(dict.fromkeys(row for row, _ in tallies))
    col_values = list(dict.fromkeys(col for _, col in tallies))
    if row_key:
        row_values.sort(key=row_key)
    if col_key:
        col_values.sort(key=col_key)
    headers = (AXIS_HEADER_LABELS.get(row_axis, row_axis), *col_values)
    rows = [
        (
            row,
            *(
                format_matrix_cell(tallies.get((row, col)), emoji=emoji)
                for col in col_values
            ),
        )
        for row in row_values
    ]
    ctx.print_table(rows, headers)


@subcommand(
    short_help="Move runner images forward as GitHub retires and supersedes them",
    examples=(
        (
            "What would change, without touching the tree",
            "repomatic sync-runner-images --dry-run",
        ),
    ),
)
# Runs live by default: CI calls it to write the proposal.
@dry_run_option(default=False)
@output_option(
    "Write the proposal as a Markdown table to this file, for feeding a pull"
    " request body through `pr-sync --template-arg-file`."
)
@pass_context
def sync_runner_images(ctx: Context, dry_run: bool, output: Path | None) -> None:
    """Move retiring runner images forward, and probe superseding ones.

    Every label this repository runs is looked up in GitHub's available-images
    table. A **retirement** rewrites each literal `runs-on:` naming a deprecated
    image onto its successor, because those jobs carry an end date. An
    **upgrade** adds a strictly newer *version* to the full test matrix as a
    `continue-on-error` probe rather than migrating onto it: nothing is bet on
    it, but the suite starts exercising it at once, which surfaces a dependency
    breaking there while there is runway to report it upstream.

    Strictly newer by version is what separates an upgrade from a flavour. A
    same-version variant carrying a different toolchain is not a newer image and
    is never proposed as one.

    Merging is the decision; the CI run this triggers is the evidence for it.
    To decline a proposal for good, name the label in
    `[tool.repomatic.sync-runner-images] ignore`: closing the pull request
    alone brings it back on the next run.
    """
    if output:
        # An empty proposal until one is planned: the workflow step feeding it
        # to `pr-sync` reads it whatever this run decides, a switched-off sync
        # included, and should not have to branch on whether it exists.
        write_output("runner image proposal", "", output)
    exit_if_disabled(ctx, "sync-runner-images.sync")

    config = get_tool_config(ctx)
    literal = literal_runners()
    changes = plan_runner_changes(
        literal,
        set(KNOWN_RUNNERS) | set(literal),
        fetch_catalog(),
        config.sync_runner_images.ignore,
    )
    if not changes:
        echo("No runner image change to propose.")
        ctx.exit(0)

    # Only a checkout of repomatic itself holds the curated axes. Downstream
    # they arrive through the pin, so a repository consuming repomatic picks
    # the new matrix up when it adopts a release.
    axes_path = AXES_SOURCE_PATH if is_source_repo(Path.cwd()) else None
    for change in changes:
        echo(f"{change.kind}: {change.summary}")
        echo(f"  because {change.reason}")
        if change.alternative:
            echo(f"  passed over: {change.alternative} (preview)")
        if dry_run:
            continue
        for path in change.apply(WORKFLOW_DIR, Path("pyproject.toml"), axes_path):
            echo(f"  rewrote {path}")

    if output:
        write_output("runner image proposal", render_change_table(changes), output)
