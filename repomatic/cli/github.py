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
"""GitHub issue and pull request commands of the `repomatic` CLI.

One module per help section: every command here registers onto the
`repomatic` group through the section object both import from
{mod}`repomatic.cli.main`, which pulls this module in at startup.
"""

from __future__ import annotations

from pathlib import Path

from click_extra import (
    Choice,
    ClickException,
    Context,
    IntRange,
    UsageError,
    echo,
    file_path,
    get_tool_config,
    option,
    pass_context,
    prep_path,
)

from ..broken_links import manage_combined_broken_links_issue
from ..config import (
    load_repomatic_config,
)
from ..git_ops import current_branch
from ..github import token as _token_mod, unsubscribe as _unsub_mod
from ..github.actions import (
    AnnotationLevel,
    cancel_superseded_runs,
    emit_annotation,
    format_multiline_output,
    get_default_author,
    get_default_number,
    get_event_subject,
    get_github_event,
    is_pull_request,
)
from ..github.ci_status import (
    CI_STATUS_HEADER_DEFS,
    monitored_workflows,
    read_ci_status,
)
from ..github.gh import gh_api_json
from ..github.issue import (
    BOT_ISSUE_LABEL,
    LOCK_INACTIVE_DAYS,
    LOCK_ISSUE_COMMENT,
    LOCK_PR_COMMENT,
    LOCK_REASON,
    LOCK_SEARCH_LIMIT,
    LOCK_THREADS_HEADER_DEFS,
    add_labels,
    lock_stale_threads,
)
from ..github.job_timings import (
    JOB_TIMINGS_HEADER_DEFS,
    fetch_job_timings,
    format_duration,
    render_markdown,
    summarize,
)
from ..github.pr import (
    list_changed_files,
    upsert_pr,
)
from ..github.pr_body import (
    get_template_names,
    template_draft,
    template_labels,
    template_stem,
)
from ..github.sponsor import (
    get_default_owner,
    is_sponsor,
)
from ..github.token import require_token
from ..github.unsubscribe import (
    render_report as _render_report,
    unsubscribe_threads as _unsubscribe_threads,
)
from ..init_project import is_source_repo
from ..labels import (
    match_content_rules,
    match_file_rules,
    resolve_content_rules,
    resolve_file_rules,
)
from ..lint_repo import (
    KNOWN_RUNNERS,
    WORKFLOW_DIR,
    literal_runners,
)
from ..metadata.core import (
    Metadata,
)
from ..runner_catalog import fetch_catalog
from ..runner_images import (
    apply_axes_retirement,
    apply_retirement,
    apply_upgrade,
    close_legacy_issue,
    plan_runner_changes,
    render_change_table,
)
from ..setup_guide import manage_setup_guide
from .main import (
    WorkflowFile,
    _ci_status_sort,
    _job_timings_sort,
    _render_pr_content,
    _section_github,
    dry_run_option,
    exit_if_disabled,
    has_cloudflare_api_token_option,
    has_notifications_pat_option,
    has_pat_option,
    has_virustotal_key_option,
    log_output_target,
    repo_slug_option,
    repomatic,
    stdout_output_option,
    template_arg_file_option,
    template_arg_option,
    template_part_option,
    template_version_option,
)

TYPE_CHECKING = False


@repomatic.command(
    name="apply-labels",
    short_help="Label an issue or PR from its content and changed files",
    section=_section_github,
)
@option(
    "--number",
    type=IntRange(min=1),
    help="Issue or PR number. Defaults to the number in $GITHUB_EVENT_PATH.",
)
@option(
    "--pr/--issue",
    "is_pr",
    default=None,
    help="Specify issue or pull request. Auto-detected from $GITHUB_EVENT_PATH.",
)
@repo_slug_option
@dry_run_option
@require_token(_token_mod, "validate_gh_token_env")
@pass_context
def apply_labels_cmd(
    ctx: Context,
    number: int | None,
    is_pr: bool | None,
    repo: str | None,
    dry_run: bool,
) -> None:
    """Label a freshly opened issue or pull request from the project's rules.

    Two rule families, both tables under `[tool.repomatic.labels]` mapping a
    label to the patterns that apply it, overlaid on the bundled defaults:

    \b
      content-rules  keywords or /regex/flags matched against the title and
                     body, of an issue or a pull request alike
      file-rules     globs matched against the paths a pull request changes

    Additive only. A label already on the thread stays, and none is ever
    removed, so a classification made by hand survives every later run.

    This pre-labels for the maintainer's first pass and never replaces it, so
    the rules are tuned for precision: a missing label costs one click, a wrong
    one is noise on every issue that trips it.

    Requires the gh CLI to be authenticated.

    \b
    Examples:
        # Preview what the current event's issue or PR would earn
        repomatic apply-labels

    \b
        # Apply them, as the labeller job does
        repomatic apply-labels --live

    \b
        # Manual invocation against one pull request
        repomatic apply-labels --live --pr --number 42
    """
    config = get_tool_config(ctx)

    if repo is None:
        repo = Metadata().repo_slug
    if number is None:
        number = get_default_number()
    if is_pr is None:
        is_pr = is_pull_request()
    if not repo or not number:
        raise UsageError(
            "Missing --repo or --number, and neither could be auto-detected"
            " from the environment."
        )

    kind = "pr" if is_pr else "issue"

    # In the labeller job the event payload carries the thread. A manual
    # invocation naming another thread reads its text from the API instead, so
    # the content rules see the real title and body rather than an empty
    # string.
    subject = get_event_subject()
    if subject.get("number") != number:
        subject = (
            gh_api_json([
                kind,
                "view",
                str(number),
                "--repo",
                repo,
                "--json",
                "title,body",
            ])
            or {}
        )
    text = f"{subject.get('title') or ''}\n{subject.get('body') or ''}"
    matched = match_content_rules(resolve_content_rules(config), text)

    # File rules need a diff, which only a pull request has.
    if is_pr:
        matched |= match_file_rules(
            resolve_file_rules(config),
            list_changed_files(number, repo),
        )

    thread = "PR" if is_pr else "issue"
    if not matched:
        echo(f"No rule matched {thread} #{number}.")
        return

    labels = sorted(matched)
    if dry_run:
        echo(f"Would label {thread} #{number} with: {', '.join(labels)}")
        return
    if not add_labels(repo, number, labels, is_pr=is_pr):
        raise ClickException(f"Failed to label {thread} #{number}")
    echo(f"Labelled {thread} #{number} with: {', '.join(labels)}")


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
@require_token(_token_mod, "validate_gh_token_env")
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

    Requires the gh CLI to be authenticated.

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
    name="ci-status",
    short_help="Report which CI jobs are red, and which of them gate a merge",
    section=_section_github,
    params=[_ci_status_sort],
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
@pass_context
def ci_status(
    ctx: Context, branch: str, workflows: tuple[str, ...], fatal: bool
) -> None:
    """Report the latest CI run of each workflow, and what is actually broken.

    Reads jobs rather than runs, so a crashed allowed-failure probe cannot
    hide inside a green run conclusion and a run still reading "queued"
    cannot hide the dozen jobs of it that already finished.

    A job whose name carries the unstable glyph anywhere is allowed to fail
    and never affects the exit code. Every other job is required, including
    the non-matrix ones that carry no glyph at all.

    \b
    Examples:
        # What is red on main right now
        repomatic ci-status

    \b
        # One workflow, without failing the shell
        repomatic ci-status --workflow tests.yaml --no-fatal
    """
    names = list(workflows) or monitored_workflows(WORKFLOW_DIR)
    if not names:
        echo("No workflow to read.")
        ctx.exit(0)

    status = read_ci_status(names, branch)
    if not status.runs:
        echo(f"No run found on {branch!r} for: {', '.join(names)}.")
        ctx.exit(0)

    rows = [
        (
            run.workflow,
            run.head_sha[:8],
            run.status,
            run.verdict,
        )
        for run in status.runs
    ]
    ctx.print_table(rows, CI_STATUS_HEADER_DEFS)

    for run in status.runs:
        if run.workflow_level_failure:
            message = (
                f"{run.workflow}: run {run.run_id} failed with no failed job. "
                "This is a workflow-level error (an invalid matrix expression, "
                "malformed YAML, a missing secret): read the run's error "
                "annotations, there is no job log."
            )
            emit_annotation(AnnotationLevel.ERROR, message)
            echo(f"✗ {message}")
        for job in run.failed_required:
            message = f"{run.workflow}: required job failed: {job.name}"
            emit_annotation(AnnotationLevel.ERROR, message)
            echo(f"✗ {message}")
        for job in run.failed_probes:
            echo(f"⚠ {run.workflow}: allowed-failure probe failed: {job.name}")

    if not status.settled:
        echo("… some jobs have not settled yet.")

    ctx.exit(1 if status.blocking and fatal else 0)


@repomatic.command(
    name="sync-runner-images",
    short_help="Move runner images forward as GitHub retires and supersedes them",
    section=_section_github,
)
@option(
    # Same spelling as the shared `dry_run_option`, so `--live` works on every
    # command that has a dry-run mode; only the default differs (this one runs
    # live, since CI calls it to write the proposal).
    "--dry-run/--live",
    default=False,
    help="Report the changes without writing them.",
)
@option(
    "--output",
    type=file_path(writable=True, resolve_path=True),
    help=(
        "Write the proposal as a Markdown table to this file, for feeding a"
        " pull request body through `pr-sync --template-arg-file`."
    ),
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

    \b
    Examples:
        # What would change, without touching the tree
        repomatic sync-runner-images --dry-run
    """
    config = load_repomatic_config()
    # Retires the issue the announcement feed used to maintain. Harmless where
    # there never was one, and the only moment a repository adopting this
    # release can be noticed.
    if not dry_run:
        close_legacy_issue()

    literal = literal_runners()
    changes = plan_runner_changes(
        literal,
        set(KNOWN_RUNNERS) | set(literal),
        fetch_catalog(),
        config.sync_runner_images.ignore,
    )
    if not changes:
        echo("No runner image change to propose.")
        if output:
            # An empty file rather than none: the workflow step feeding it to
            # `pr-sync` should not have to branch on whether it exists.
            output.write_text("", encoding="UTF-8")
        ctx.exit(0)

    for change in changes:
        echo(f"{change.kind}: {change.summary}")
        echo(f"  because {change.reason}")
        if change.alternative:
            echo(f"  passed over: {change.alternative} (preview)")
        if dry_run:
            continue
        if change.kind == "retirement":
            for path in apply_retirement(change, WORKFLOW_DIR):
                echo(f"  rewrote {path.name}")
            # Only here do the curated axes exist to move. Downstream they
            # arrive through the pin, so a repo consuming repomatic picks the
            # new matrix up when it adopts a release.
            axes = Path("repomatic/matrix_axes.py")
            if is_source_repo(Path.cwd()) and apply_axes_retirement(change, axes):
                echo(f"  rewrote {axes.name} (every downstream matrix follows)")
        elif apply_upgrade(change, Path("pyproject.toml")):
            echo("  added a continue-on-error probe to pyproject.toml")

    if output:
        output.write_text(render_change_table(changes), encoding="UTF-8")
        echo(f"Wrote {output}")


@repomatic.command(
    name="job-timings",
    short_help="Measure how long each runner image takes, from finished runs",
    section=_section_github,
    params=[_job_timings_sort],
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
@option(
    "--output",
    type=file_path(writable=True, resolve_path=True),
    help="Write the report as a Markdown table to this file, for documentation.",
)
@pass_context
def job_timings(
    ctx: Context, workflow: str, branch: str, limit: int, output: Path | None
) -> None:
    """Report median whole-job wall-clock per runner image.

    Runner choice is supposed to rest on measurement, and the measurement that
    matters is whole-job: this repository once kept a lean image for years on a
    benchmark that timed only the tool pass, where it looked near-parity, while
    end to end it was 20-56% slower. The jobs API reports start and end
    timestamps, so what this reads is whole-job by construction.

    Only successful runs are sampled, because a failed run's jobs stop early
    and time where the failure landed rather than what the image costs. The
    figure is a median across several runs, which is what turns a queue stall
    into noise instead of a verdict.

    \b
    Examples:
        # Which image is holding the matrix up
        repomatic job-timings

    \b
        # A wider sample, written out for documentation
        repomatic job-timings --limit 10 --output timings.md
    """
    timings = fetch_job_timings(workflow, branch, limit)
    if not timings:
        echo(
            f"No finished job found in the last {limit} successful "
            f"{workflow!r} run(s) on {branch!r}."
        )
        ctx.exit(0)

    reports = summarize(timings)
    ctx.print_table(
        [
            (
                report.runner,
                str(report.job_count),
                format_duration(report.median_seconds),
                report.slowest_job,
                format_duration(report.slowest_seconds),
            )
            for report in reports
        ],
        JOB_TIMINGS_HEADER_DEFS,
    )

    if output:
        output.write_text(render_markdown(reports, workflow, limit), encoding="UTF-8")
        echo(f"Wrote {output}")


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
    short_help="Lock closed, inactive issues and PRs",
    section=_section_github,
)
@option(
    "--inactive-days",
    type=IntRange(min=1),
    default=LOCK_INACTIVE_DAYS,
    help="Days without activity before a closed thread is locked.",
)
@option(
    "--issue-comment",
    default=LOCK_ISSUE_COMMENT,
    help="Comment posted on an issue before locking it. Empty to post none.",
)
@option(
    "--pr-comment",
    default=LOCK_PR_COMMENT,
    help="Comment posted on a pull request before locking it. Empty to post none.",
)
@option(
    "--exclude-label",
    "exclude_labels",
    multiple=True,
    default=(BOT_ISSUE_LABEL,),
    help="Leave threads carrying this label unlocked. Repeatable.",
)
@option(
    "--limit",
    type=IntRange(min=1, max=1000),
    default=LOCK_SEARCH_LIMIT,
    help="Maximum number of threads to examine in one run.",
)
@option(
    "--reason",
    type=Choice(["off_topic", "resolved", "spam", "too_heated"]),
    default=LOCK_REASON,
    help="Reason recorded with the lock.",
)
@repo_slug_option
@dry_run_option
@require_token(_token_mod, "validate_gh_token_env")
@pass_context
def lock_threads(
    ctx: Context,
    inactive_days: int,
    issue_comment: str,
    pr_comment: str,
    exclude_labels: tuple[str, ...],
    limit: int,
    reason: str,
    repo: str | None,
    dry_run: bool,
) -> None:
    """Lock closed issues and pull requests left inactive too long.

    Keeps spam and necro-posting off threads whose discussion is over, without
    touching anything still moving: the clock counts from a thread's last
    update, so a closed issue people are still replying to keeps resetting it.

    Locking is one-way here. Nothing unlocks a thread on a schedule, and a
    thread already locked never reappears in the search, so re-running the
    command right after a run finds nothing left to do.

    Issues repomatic maintains itself are excluded by default: they are meant
    to reopen when their condition recurs, which a lock would block.

    Requires the gh CLI to be authenticated.

    \b
    Examples:
        # Preview what a run would lock
        repomatic lock-threads

    \b
        # Lock them, as the autolock workflow does
        repomatic lock-threads --live

    \b
        # Lock silently after six months, sparing triaged threads
        repomatic lock-threads --live --inactive-days 180 \\
            --issue-comment "" --exclude-label "🚧 needs triage"
    """
    if repo is None:
        repo = Metadata().repo_slug
    if not repo:
        raise ClickException("Cannot detect repository.")

    rows = lock_stale_threads(
        repo,
        inactive_days=inactive_days,
        issue_comment=issue_comment,
        pr_comment=pr_comment,
        exclude_labels=exclude_labels,
        limit=limit,
        reason=reason,
        dry_run=dry_run,
    )
    if not rows:
        echo(f"No closed thread in {repo} has been inactive for {inactive_days} days.")
        return
    ctx.print_table(rows, LOCK_THREADS_HEADER_DEFS)


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
    "--prefix-file",
    "prefix_file",
    type=file_path(exists=True, readable=True, resolve_path=True),
    envvar="GHA_PR_BODY_PREFIX_FILE",
    default=None,
    help="Read the prefix from a file instead of --prefix, for a report too "
    "large to travel in an environment variable. "
    "Can also be set via the GHA_PR_BODY_PREFIX_FILE environment variable. "
    "Wins over --prefix when both are given.",
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
@template_arg_option
@template_arg_file_option
@template_version_option
@template_part_option
@option(
    "--pr-ref",
    "pr_ref",
    default=None,
    help="PR reference passed to detect-squash-merge template (e.g. #2316).",
)
@stdout_output_option
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
    prefix_file: Path | None,
    template: str | None,
    template_file: Path | None,
    template_args_cli: tuple[str, ...],
    template_arg_files: tuple[str, ...],
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

    The prefix can be set via --template (built-in templates), --prefix
    (arbitrary content, also via GHA_PR_BODY_PREFIX env var) or --prefix-file
    (the same content read from a file, also via GHA_PR_BODY_PREFIX_FILE). If
    a template and a prefix are both given, the prefix is prepended before the
    rendered template content.

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

    title_str, body, commit_msg_str = _render_pr_content(
        prefix=prefix,
        prefix_file=prefix_file,
        template=template,
        template_file=template_file,
        template_args_cli=template_args_cli,
        template_arg_files=template_arg_files,
        version=version,
        part=part,
        pr_ref=pr_ref,
    )

    if output_format == "github-actions":
        parts = [format_multiline_output("body", body)]
        if title_str:
            parts.append(f"title={title_str}")
        if commit_msg_str:
            parts.append(f"commit_message={commit_msg_str}")
        content = "\n".join(parts)
    else:
        content = body

    log_output_target("PR body", output)

    echo(content, file=prep_path(output))


@repomatic.command(
    short_help="Create, refresh or retire an automation PR", section=_section_github
)
@option(
    "--template",
    type=Choice(get_template_names(), case_sensitive=False),
    default=None,
    help="Render title, body and commit message from a built-in template, "
    "and derive the branch, labels and draft state from it: the branch "
    "defaults to the template name, labels and draft to its frontmatter.",
)
@option(
    "--template-file",
    "template_file",
    type=file_path(exists=True, readable=True, resolve_path=True),
    default=None,
    help="Use an external template file instead of --template, for "
    "project-specific PRs. Same derivations, with the branch defaulting "
    "to the file's stem.",
)
@template_arg_option
@template_arg_file_option
@template_version_option
@template_part_option
@option(
    "--branch",
    default=None,
    help="Head branch to create, update or retire. By convention the job ID; "
    "defaults to the template name when a template is given.",
)
@option(
    "--title",
    envvar="GHA_PR_TITLE",
    default=None,
    help="Pull-request title, when not using a template. "
    "Can also be set via the GHA_PR_TITLE environment variable.",
)
@option(
    "--body",
    envvar="GHA_PR_BODY",
    default=None,
    help="Rendered markdown body, when not using a template. "
    "Can also be set via the GHA_PR_BODY environment variable.",
)
@option(
    "--commit-message",
    "commit_message",
    envvar="GHA_PR_COMMIT_MESSAGE",
    default=None,
    help="Commit message, when not using a template. "
    "Can also be set via the GHA_PR_COMMIT_MESSAGE environment variable.",
)
@option(
    "--base",
    default=None,
    help="Base branch. Defaults to the currently checked-out branch, or to "
    "the repository default branch when the checkout is detached in CI.",
)
@option(
    "--label",
    "labels",
    multiple=True,
    help="Label to attach, overriding the template's frontmatter labels. "
    "Repeat to provide multiple. Best-effort: a label GitHub refuses "
    "warns instead of failing the command.",
)
@option(
    "--assignee",
    "assignees",
    multiple=True,
    envvar=["GHA_PR_ASSIGNEE", "GITHUB_ACTOR"],
    help="Assignee to attach. Repeat to provide multiple. Defaults to the "
    "workflow actor via the ambient GITHUB_ACTOR variable. Best-effort: "
    "github-actions[bot] cannot be assigned and only warns.",
)
@option(
    "--draft/--no-draft",
    default=None,
    help="Hold the pull request in draft, overriding the template's "
    "frontmatter. Re-applied on every update, not just at creation, so a "
    "PR marked ready-for-review goes back to draft on the next sync.",
)
@option(
    "--add-path",
    "add_paths",
    multiple=True,
    help="Git pathspec limiting what the pull request commits. Repeat to "
    "provide multiple. Defaults to the whole tree, which is right when a "
    "job's only writes are the ones it means to publish; narrow it when the "
    "job also provisions tooling into the checkout, so an installed package "
    "or a rewritten lock file cannot ride along.",
)
def pr_sync(
    template: str | None,
    template_file: Path | None,
    template_args_cli: tuple[str, ...],
    template_arg_files: tuple[str, ...],
    version: str | None,
    part: str | None,
    branch: str | None,
    title: str | None,
    body: str | None,
    commit_message: str | None,
    base: str | None,
    labels: tuple[str, ...],
    assignees: tuple[str, ...],
    draft: bool | None,
    add_paths: tuple[str, ...],
) -> None:
    """Converge a branch and its PR onto whatever the working tree holds.

    Opens the pull request when the tree carries changes, refreshes it when
    those changes moved, does nothing at all when the branch already matches,
    and closes the PR (deleting the branch) once the changes are gone.

    Idempotent: re-running with an unchanged tree performs no write, so a
    workflow re-run never churns the PR or re-triggers its checks.

    With --template, the title, body and commit message are rendered
    internally and the branch, labels and draft state come from the template
    and its frontmatter, so the whole operation is one flag. Without one,
    pass --title, --body and --commit-message explicitly.

    Works from a detached HEAD: the base falls back to the repository default
    branch read from the CI event payload, and any commits the job made
    itself are carried through.

    Commits the whole tree unless --add-path narrows it. A job that installs
    its own linter into the checkout, or runs a package manager that rewrites
    a lock file on the way past, wants the narrow form: those writes are not
    what the pull request is for.

    \b
    Examples:
        repomatic pr-sync --template format-python
        repomatic pr-sync --template bump-version --part minor \\
            --branch minor-version-increment
        repomatic pr-sync --branch my-fix --title "Fix" --body "…" \\
            --commit-message "Fix the thing"
    """
    if template and template_file:
        msg = "--template and --template-file are mutually exclusive."
        raise UsageError(msg)
    template_ref: str | Path | None = template or template_file

    if template_ref:
        if title or body or commit_message:
            msg = (
                "--title, --body and --commit-message are rendered from the "
                "template; pass either a template or the explicit trio, not "
                "both."
            )
            raise UsageError(msg)
        title, body, commit_message = _render_pr_content(
            template=template,
            template_file=template_file,
            template_args_cli=template_args_cli,
            template_arg_files=template_arg_files,
            version=version,
            part=part,
        )
    elif not (title and body and commit_message):
        msg = "Without --template, --title, --body and --commit-message are required."
        raise UsageError(msg)

    if branch is None:
        if template:
            branch = template
        elif template_file:
            branch = template_stem(template_file.name)
        else:
            msg = "Without --template, --branch is required."
            raise UsageError(msg)

    if not labels and template_ref:
        labels = tuple(template_labels(template_ref))
    if draft is None:
        draft = template_draft(template_ref) if template_ref else False

    # A checkout pinned to a raw SHA has no branch to infer the base from; in
    # CI the event payload knows the repository default branch, which is what
    # every automation PR here opens against.
    if base is None and current_branch() is None:
        base = (get_github_event().get("repository") or {}).get("default_branch")

    result = upsert_pr(
        branch=branch,
        title=title,
        body=body,
        commit_message=commit_message,
        base=base,
        labels=labels,
        assignees=assignees,
        draft=draft,
        add_paths=add_paths,
    )
    if result.number:
        echo(f"{result.operation}: PR #{result.number} on {result.branch}")
    else:
        echo(f"{result.operation}: {result.branch}")


@repomatic.command(
    short_help="Manage setup guide issue lifecycle", section=_section_github
)
@has_cloudflare_api_token_option
@has_notifications_pat_option
@has_pat_option
@has_virustotal_key_option
@repo_slug_option
@require_token(_token_mod, "validate_gh_token_env")
@pass_context
def setup_guide(
    ctx: Context,
    has_cloudflare_api_token: bool,
    has_notifications_pat: bool,
    has_pat: bool,
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

    Requires the gh CLI to be authenticated.

    \b
    Examples:
        # No secret: create or reopen the setup issue
        repomatic setup-guide

    \b
        # Secret configured: close the issue if all checks pass
        repomatic setup-guide --has-pat
    """
    config = get_tool_config(ctx)
    exit_if_disabled(ctx, config.setup_guide, "setup-guide")

    manage_setup_guide(
        config,
        has_pat=has_pat,
        has_notifications_pat=has_notifications_pat,
        has_virustotal_key=has_virustotal_key,
        has_cloudflare_api_token=has_cloudflare_api_token,
        repo=repo,
    )


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
@repo_slug_option
@option(
    "--number",
    type=IntRange(min=1),
    help="Issue or PR number. Defaults to number from $GITHUB_EVENT_PATH.",
)
@option(
    "--label",
    # The label `repomatic/data/labels.toml` defines, singular: the plural
    # this option used to default to exists in no synced repository, so every
    # sponsor-labelling attempt died on an unknown label.
    default="💖 sponsor",
    help="Label to add if author is a sponsor.",
)
@option(
    "--pr/--issue",
    "is_pr",
    default=None,
    help="Specify issue or pull request. Auto-detected from $GITHUB_EVENT_PATH.",
)
@require_token(_token_mod, "validate_gh_token_env")
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

    This command requires the gh CLI to be authenticated.

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
        if add_labels(repo, number, [label], is_pr=is_pr):
            echo(f"Added {label!r} label to {'PR' if is_pr else 'issue'} #{number}")
        else:
            raise ClickException("Failed to add sponsor label")
    else:
        echo(f"Author {author!r} is not a sponsor of {owner!r}")


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
@dry_run_option
@require_token(_unsub_mod, "_validate_notifications_token")
def unsubscribe_threads(months: int, batch_size: int, dry_run: bool) -> None:
    """Unsubscribe from closed, inactive GitHub notification threads.

    Processes notifications in two phases:

    \b
    Phase 1, REST notification threads:
      Fetches Issue/PullRequest notification threads, inspects each for
      closed + stale status, and unsubscribes via DELETE + PATCH.

    \b
    Phase 2, GraphQL threadless subscriptions:
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
