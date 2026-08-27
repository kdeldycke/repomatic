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
"""Forge sampling commands of the `repomatic` CLI.

One module per help section: every command here registers onto the
`repomatic` group through the section object both import from
{mod}`repomatic.cli`, which pulls this module in at startup.
"""

from __future__ import annotations

from pathlib import Path

from click_extra import (
    ClickException,
    Context,
    Spinner,
    echo,
    file_path,
    get_tool_config,
    option,
    pass_context,
)

from .cli import (
    _section_sample,
    exit_if_disabled,
    repomatic,
)
from .github.pr import (
    carry_pr_branch_paths,
)
from .metric_chart import ChartSpec, write_chart
from .metrics import (
    SAMPLE_HEADER_DEFS,
    backfill_wayback as _backfill_wayback,
    collected_subjects,
    import_star_history_csv,
    load_metrics,
    reconstruct_from_github,
    sample_subject,
    save_metrics,
    series as metric_series,
)

TYPE_CHECKING = False


@repomatic.command(
    name="sample-metrics",
    short_help="Record what forges say about the repositories this project tracks",
    section=_section_sample,
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
    help="Rebuild exact star curves from per-star timestamps, for GitHub "
    "repositories the token administers.",
)
@option(
    "--backfill-wayback",
    is_flag=True,
    default=False,
    help="Mine contemporaneous star counts from archived GitHub pages. Slow, "
    "and a one-off: the scheduled job never runs it.",
)
@option(
    "--import-csv",
    type=file_path(exists=True, resolve_path=True),
    multiple=True,
    help="Import a star-history.com calendar export. Repeatable.",
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
    backfill_wayback: bool,
    import_csv: tuple[Path, ...],
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
    card. The aggregate count stayed public, so this snapshots it on a schedule
    and commits the result: a history that accrues locally cannot be revoked.

    Each reading records where it came from, since the curves are not all
    measured the same way. A GitHub repository the token administers is
    reconstructed exactly from the timestamp of every star it still holds; the
    rest are sampled forward, and backfilled from archived pages or from a
    star-history.com export.

    \b
    Examples:
        repomatic sample-metrics

    \b
        repomatic sample-metrics --no-reconstruct --backfill-wayback

    \b
        repomatic sample-metrics --import-csv star-history-export.csv
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
        records = load_metrics(store_path)
        tracked = collected_subjects(
            config.metrics.subjects, config.metrics.predecessors
        )
    except ValueError as error:
        raise ClickException(str(error))

    outcomes = []
    if forward:
        for name, repo in tracked.items():
            outcomes.append(sample_subject(records, name, repo, config.metrics.forges))
    if reconstruct:
        for name, repo in tracked.items():
            outcomes.append(reconstruct_from_github(records, name, repo))
    for export in import_csv:
        try:
            outcomes.extend(import_star_history_csv(records, export, tracked.values()))
        except (OSError, ValueError) as error:
            raise ClickException(str(error))
    if backfill_wayback:
        # Animated from its own thread, so the line keeps moving while a single
        # archived page blocks through its retries, and silent off an
        # interactive stream, which leaves a piped or scheduled run's log clean.
        # The backfill is the one command here whose subject is unreachable and
        # slow enough that a watcher cannot tell work from a hang.
        with Spinner("Mining the archives", timer=True) as progress:

            def announce(status: str) -> None:
                progress.label = status

            for name, repo in tracked.items():
                outcomes.append(
                    _backfill_wayback(
                        records,
                        name,
                        repo,
                        store_path,
                        on_status=announce,
                        on_row=progress.echo,
                    )
                )

    ctx.print_table(
        [
            (
                outcome.subject,
                outcome.phase,
                outcome.repo,
                str(outcome.stars) if outcome.stars is not None else "—",
                str(outcome.rows),
                outcome.note or "—",
            )
            for outcome in outcomes
        ],
        SAMPLE_HEADER_DEFS,
    )

    if save_metrics(store_path, records):
        echo(f"Recorded {len(records)} reading(s) in {store_path}.")
    else:
        echo(f"{store_path} already up to date.")

    if not render:
        return
    if not records:
        echo("No reading recorded yet, so no chart to draw.")
        return
    for entry in config.metrics.charts:
        try:
            spec = ChartSpec.from_mapping(entry)
            grouped = metric_series(
                records,
                config.metrics.subjects,
                spec.metric,
                config.metrics.predecessors,
            )
            # Stamped with the newest reading of the plotted metric rather than
            # with today, so re-rendering an unmoved history rewrites nothing. A
            # caption naming the run date would churn the committed SVGs on
            # every scheduled pass that found nothing new.
            stamp = max(
                (day for points in grouped.values() for day, _value in points),
                default=None,
            )
            changed = write_chart(
                grouped,
                spec,
                config.metrics.colors,
                stamp.isoformat() if stamp else None,
            )
        except ValueError as error:
            raise ClickException(str(error))
        echo(f"{'Redrew' if changed else 'Unchanged'} {spec.output}.")
