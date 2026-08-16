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

import logging
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from click.shell_completion import CompletionItem
from click_extra import (
    STDOUT_SENTINEL,
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
    is_stdout,
    jobs_option,
    option,
    option_group,
    pass_context,
    prep_path,
    style,
)
from extra_platforms import is_github_ci

from .attestation import pack_attestation
from .awesome_toc import fix_awesome_toc
from .binaries_page import (
    render_binaries_csv,
    render_chart_section,
    update_binaries_csv,
    update_binaries_page,
)
from .binary import (
    BINARY_ASSET_SUFFIXES,
    NUITKA_BUILD_TARGETS,
    pack_binary_assets,
    verify_binary_arch,
    verify_binary_floor,
)
from .broken_links import manage_combined_broken_links_issue
from .cache import (
    CACHE_LIST_HEADER_DEFS,
    cache_dir as _cache_dir,
    cache_rows,
    clear_cache,
    clear_config_cache,
    clear_http_cache,
)
from .changelog import (
    Changelog,
    lint_changelog_dates,
    load_changelog_repo,
    resolved_changelog_path,
)
from .checksums import update_registry_checksums
from .config import (
    CONFIG_REFERENCE_HEADER_DEFS,
    Config,
    config_reference,
    escape_type_for_gfm_table,
    load_repomatic_config,
)
from .dep_graph import (
    SubgraphKind,
    generate_dependency_graph,
    resolve_subgraph_selection,
)
from .dep_policy import scan_policy
from .dep_sources import (
    LINT_DEPS_HEADER_DEFS,
    build_release_readiness,
    format_blocker_section,
    scan_project,
)
from .docs import update_docs as _update_docs
from .forge import (
    PROJECT_SAMPLE_HEADER_DEFS,
    sample_projects as _sample_projects,
)
from .git_ops import commit_and_push_files, create_and_push_tag, current_branch
from .github import token as _token_mod, unsubscribe as _unsub_mod
from .github.actions import (
    AnnotationLevel,
    cancel_superseded_runs,
    emit_annotation,
    emit_report,
    format_multiline_output,
    get_default_author,
    get_default_number,
    get_event_subject,
    get_github_event,
    is_pull_request,
    read_file_output,
)
from .github.ci_status import (
    CI_STATUS_HEADER_DEFS,
    STABLE_GLYPH,
    UNSTABLE_GLYPH,
    monitored_workflows,
    read_ci_status,
)
from .github.dev_release import (
    cleanup_dev_releases as _cleanup_dev_releases,
    sync_dev_release as _sync_dev_release,
)
from .github.gh import gh_api_json
from .github.issue import (
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
from .github.job_timings import (
    JOB_TIMINGS_HEADER_DEFS,
    fetch_job_timings,
    format_duration,
    render_markdown,
    summarize,
)
from .github.pr import close_open_prs_on_branch, list_changed_files, upsert_pr
from .github.pr_body import (
    build_pr_body,
    build_release_review_steps,
    generate_pr_metadata_block,
    generate_refresh_tip,
    get_template_names,
    render_commit_message,
    render_template,
    render_title,
    template_args,
    template_docs_url,
    template_draft,
    template_labels,
    template_stem,
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
    get_default_owner,
    is_sponsor,
)
from .github.token import require_token
from .github.unsubscribe import (
    render_report as _render_report,
    unsubscribe_threads as _unsubscribe_threads,
)
from .github.workflow_sync import run_workflow_lint
from .gitignore import build_gitignore, orphaned_rules
from .humanize import format_file_size
from .images import (
    DEFAULT_MIN_SAVINGS_BYTES,
    DEFAULT_MIN_SAVINGS_PCT,
    generate_markdown_summary,
    optimize_images,
)
from .init_project import is_source_repo, prune_paths, run_init
from .labels import (
    apply_labels,
    match_content_rules,
    match_file_rules,
    resolve_content_rules,
    resolve_file_rules,
)
from .lint_repo import (
    KNOWN_RUNNERS,
    WORKFLOW_DIR,
    documentation_url,
    literal_runners,
    run_repo_lint,
)
from .mailmap import Mailmap, remove_header
from .metadata import (
    METADATA_KEYS_HEADER_DEFS,
    Dialect,
    Metadata,
    all_metadata_keys,
    is_version_bump_allowed,
    metadata_keys_reference,
)
from .plugin import ARCHIVE_NAME, pack_plugin
from .prepare_release import PrepareRelease
from .pyproject import get_project_name
from .registry import (
    ALL_COMPONENTS,
    COMPONENT_HELP_TABLE,
    DEFAULT_REPO,
    EPHEMERAL_TARGETS,
    FILE_SELECTOR_COMPONENTS,
    SKILL_PHASE_ORDER,
    WORKFLOW_TARGET_ROOT,
    parse_component_entries,
    skill_catalog,
    valid_file_ids,
)
from .runner_catalog import fetch_catalog
from .runner_images import (
    apply_arrival,
    apply_axes_retirement,
    apply_retirement,
    close_legacy_issue,
    plan_runner_changes,
    render_change_table,
)
from .setup_guide import manage_setup_guide
from .star_chart import ChartSpec, write_chart
from .stars import (
    STAR_SAMPLE_HEADER_DEFS,
    backfill_wayback as _backfill_wayback,
    collected_repos,
    import_star_history_csv,
    load_star_records,
    reconstruct_from_github,
    sample_current,
    save_star_records,
    series as star_series,
)
from .sync_ops import (
    OPERATIONS_BY_NAME,
    ResolveContext,
    emit_lockfile_sync_report,
    print_plan_tables,
    render_plan_markdown,
    resolve_lockfile_plan,
    run_sync_operations,
    run_version_sync,
    selected_operations,
)
from .tool_registry import (
    TOOL_LIST_HEADER_DEFS,
    TOOL_REGISTRY,
    generated_header,
)
from .tool_runner import (
    resolve_config_source,
    run_tool,
    verify_via_write_path,
)
from .version_sync import strip_dev_suffix
from .virustotal import (
    FREE_TIER_RATE_LIMIT,
    load_scan_records,
    poll_detection_stats,
    records_from_release_notes,
    records_from_results,
    scan_files,
    upsert_scan_records,
)
from .vulnerable_deps import (
    AUDIT_HEADER_DEFS,
    AdvisorySource,
    collect_vulnerable_packages,
    fix_vulnerable_deps as _fix_vulnerable_deps,
    format_vulnerability_table,
)

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Callable, Sequence


# ---------------------------------------------------------------------------
# Shared options.
#
# An option used by more than one command is declared once here and applied as a
# decorator, so the flag name, type, default and help text cannot drift between
# the commands that expose it.
# ---------------------------------------------------------------------------

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
# The lockfile pair (sync-uv-lock, sync-dep-sources) probes held-back releases
# with a second uv resolution rather than from an already-fetched candidate list,
# so its help text names that cost where the version-sync trio's does not.
lock_held_back_option = option(
    "--held-back/--no-held-back",
    default=True,
    help="Report newer releases withheld by the exclude-newer cooldown "
    "(runs a second uv resolution).",
)
sync_table_option = option(
    "--table/--no-table",
    default=True,
    help="Print a summary table of updated packages.",
)
report_output_option = option(
    "--output",
    type=file_path(writable=True, resolve_path=True, allow_dash=True),
    default=None,
    help="Write a markdown report (table + release notes) to this file.",
)
version_report_output_option = option(
    "--output",
    type=file_path(writable=True, resolve_path=True, allow_dash=True),
    default=None,
    help="Write a markdown report (version table) to this file.",
)
stdout_output_option = option(
    "--output",
    type=file_path(writable=True, resolve_path=True, allow_dash=True),
    default=STDOUT_SENTINEL,
    help="Output file path. Defaults to stdout.",
)
dry_run_option = option(
    "--dry-run/--live",
    default=True,
    help="Report what would be done without making changes.",
)
repo_slug_option = option(
    "--repo",
    default=None,
    envvar="GITHUB_REPOSITORY",
    help="Repository in 'owner/repo' format. Defaults to $GITHUB_REPOSITORY.",
)
repo_name_option = option(
    "--repo-name",
    default=None,
    help="Repository name. Defaults to $GITHUB_REPOSITORY name component.",
)
has_cloudflare_account_id_option = option(
    "--has-cloudflare-account-id",
    is_flag=True,
    default=False,
    envvar="HAS_CLOUDFLARE_ACCOUNT_ID",
    help="Whether CLOUDFLARE_ACCOUNT_ID is configured.",
)
has_cloudflare_api_token_option = option(
    "--has-cloudflare-api-token",
    is_flag=True,
    default=False,
    envvar="HAS_CLOUDFLARE_API_TOKEN",
    help="Whether CLOUDFLARE_API_TOKEN is configured.",
)
has_notifications_pat_option = option(
    "--has-notifications-pat",
    is_flag=True,
    default=False,
    envvar="HAS_REPOMATIC_NOTIFICATIONS_PAT",
    help="Whether REPOMATIC_NOTIFICATIONS_PAT is configured.",
)
# Auto-detected from the environment rather than declared `envvar`: the flag is
# tri-state (`None` means "not specified on the CLI"), and only the *presence* of
# a token matters, not its value, which no envvar mapping can express.
has_pat_option = option(
    "--has-pat/--no-has-pat",
    default=lambda: bool(os.environ.get("REPOMATIC_PAT")),
    help=(
        "Whether REPOMATIC_PAT is configured, enabling the PAT capability checks."
        " Auto-detected from the REPOMATIC_PAT environment variable when omitted."
    ),
)
has_virustotal_key_option = option(
    "--has-virustotal-key",
    is_flag=True,
    default=False,
    envvar="HAS_VIRUSTOTAL_API_KEY",
    help="Whether VIRUSTOTAL_API_KEY is configured.",
)
# The template-feeding trio shared verbatim by `pr-body` and `pr-sync`; their
# `--template`/`--template-file` options stay per-command, whose help texts
# document command-specific derivations.
template_arg_option = option(
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
template_arg_file_option = option(
    "--template-arg-file",
    "template_arg_files",
    multiple=True,
    metavar="KEY=PATH",
    help=(
        "Read a template value from a file. Repeat to provide multiple. Use"
        " this for a value with no ceiling on its size, like a generated table:"
        " a report travels as a path rather than inline. Example:"
        " --template-arg-file summary=proposal.md."
    ),
)
template_version_option = option(
    "--version",
    "version",
    default=None,
    help="Version string passed to the template (e.g. 1.2.0).",
)
template_part_option = option(
    "--part",
    default=None,
    help="Version part passed to the bump-version template (e.g. minor, major).",
)


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


def log_output_target(subject: str, output: Path) -> None:
    """Log where a command is about to write *subject*.

    Every command that honors an `--output` path narrates the destination the
    same way, distinguishing the stdout case (`-`) so the log names the stream
    instead of a literal dash.

    :param subject: What is being written, as a noun phrase (`"metadata"`,
        `"PR body"`).
    :param output: The resolved `--output` path.
    """
    if is_stdout(output):
        logging.info(f"Print {subject} to {sys.stdout.name}")
    else:
        logging.info(f"Write {subject} to {output}")


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
_section_sample = Section("Forge sampling")
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


_ci_status_sort = SortByOption(*CI_STATUS_HEADER_DEFS, default="workflow")


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
    "--dry-run/--no-dry-run",
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
        elif apply_arrival(change, Path("pyproject.toml")):
            echo("  added a continue-on-error probe to pyproject.toml")

    if output:
        output.write_text(render_change_table(changes), encoding="UTF-8")
        echo(f"Wrote {output}")


_job_timings_sort = SortByOption(*JOB_TIMINGS_HEADER_DEFS, default="median")


@repomatic.command(
    name="job-timings",
    short_help="Measure how long each runner image takes, from finished runs",
    section=_section_github,
    params=[_job_timings_sort],
)
@option(
    "--workflow",
    default="tests.yaml",
    show_default=True,
    help="Workflow file whose runs to sample.",
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


def _render_pr_content(
    prefix: str = "",
    prefix_file: Path | None = None,
    template: str | None = None,
    template_file: Path | None = None,
    template_args_cli: tuple[str, ...] = (),
    template_arg_files: tuple[str, ...] = (),
    version: str | None = None,
    part: str | None = None,
    pr_ref: str | None = None,
) -> tuple[str, str, str]:
    """Render a pull request's title, body and commit message.

    The rendering core shared by `pr-body` (which emits the three as step
    outputs for external consumption) and `pr-sync` (which feeds them straight
    to {func}`~repomatic.github.pr.upsert_pr`, skipping the `$GITHUB_OUTPUT`
    round-trip and its 32 KiB step-output ceiling entirely).

    :return: A `(title, body, commit_message)` tuple; title and commit message
        are empty when no template supplies them.
    :raises UsageError: When a template argument cannot be resolved.
    """
    if prefix_file:
        prefix = prefix_file.read_text(encoding="UTF-8")

    # One Metadata for the whole render: the review steps, the `repo_url`
    # template arg and the metadata block must all read the same CI state.
    md = Metadata()

    def _auto_version() -> str:
        """Read current_version from bumpversion config and strip .dev suffix."""
        ver = Metadata.get_current_version()
        if not ver:
            msg = "Cannot auto-detect version: no bumpversion config found."
            raise ClickException(msg)
        ver = strip_dev_suffix(ver)
        logging.info(f"Auto-detected version: {ver}")
        return ver

    def _resolve_version() -> str:
        """Resolve the release version from --version, else the bumpversion config."""
        return version if version is not None else _auto_version()

    # The prepare-release checklist's two review steps share one GitHub
    # releases lookup; memoize it so both template args reuse a single fetch,
    # and so no other template pays for it (only prepare-release declares them).
    review_steps: dict[str, str] = {}

    def _review_step(key: str) -> str:
        if not review_steps:
            dev_review, changes_review = build_release_review_steps(
                md, _resolve_version()
            )
            review_steps["dev_release_review"] = dev_review
            review_steps["changes_review"] = changes_review
        return review_steps[key]

    cli_extra_args: dict[str, str | None] = {}
    for entry in template_args_cli:
        if "=" not in entry:
            msg = f"--template-arg expects KEY=VALUE, got {entry!r}."
            raise UsageError(msg)
        key, _, raw_value = entry.partition("=")
        cli_extra_args[key.strip()] = raw_value
    # Read after the inline pairs, so a file wins on a key given both ways.
    for entry in template_arg_files:
        if "=" not in entry:
            msg = f"--template-arg-file expects KEY=PATH, got {entry!r}."
            raise UsageError(msg)
        key, _, raw_path = entry.partition("=")
        path = Path(raw_path)
        if not path.is_file():
            msg = f"--template-arg-file cannot read {raw_path!r}."
            raise UsageError(msg)
        cli_extra_args[key.strip()] = path.read_text(encoding="UTF-8").strip()

    # Map argument names to their values or callables. CLI-provided extras
    # override built-in flag-driven sources so callers can pass any name.
    def _release_readiness() -> str:
        config = get_tool_config()
        # Pinned to the commit the body was rendered from, so the line the
        # banner points at is the line that was read. A branch ref would drift
        # onto whatever `main` holds when the maintainer clicks it.
        blob_url = f"{md.repo_url}/blob/{md.sha}" if md.repo_url and md.sha else None
        return build_release_readiness(
            Path("pyproject.toml"),
            Path("uv.lock"),
            config.minimum_release_age,
            allow=config.lint_deps.allow,
            source_url=blob_url,
        )

    arg_sources: dict[str, str | None | Callable[[], str | None]] = {
        "changes_review": lambda: _review_step("changes_review"),
        "dev_release_review": lambda: _review_step("dev_release_review"),
        "diff_table": read_file_output("REPOMATIC_DIFF_TABLE"),
        "part": part,
        "pr_ref": pr_ref,
        "release_readiness": _release_readiness,
        # Callable, will be invoked if needed.
        "repo_url": lambda: md.repo_url,
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
            docs_name = template_stem(template_file.name)
    metadata_block = generate_pr_metadata_block(
        md, docs_url=docs_url, docs_name=docs_name
    )
    body = build_pr_body(prefix, metadata_block, refresh_tip=generate_refresh_tip(md))
    return title_str, body, commit_msg_str


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
@has_cloudflare_account_id_option
@has_cloudflare_api_token_option
@has_notifications_pat_option
@has_pat_option
@has_virustotal_key_option
@repo_slug_option
@require_token(_token_mod, "validate_gh_token_env")
@pass_context
def setup_guide(
    ctx: Context,
    has_cloudflare_account_id: bool,
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
        has_cloudflare_account_id=has_cloudflare_account_id,
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
        # workflow's pr-metadata step reads steps.fix.outputs.diff_table_file.
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


@repomatic.group(short_help="Manage the download cache", section=_section_lint)
def cache():
    """Manage the local download cache.

    Binary tools and HTTP API responses are cached to avoid redundant
    downloads. This group provides subcommands to inspect, clean, and locate
    the cache.
    """


_cache_show_sort = SortByOption(*CACHE_LIST_HEADER_DEFS, default="name")


@cache.command(short_help="List cached entries", params=[_cache_show_sort])
@pass_context
def show(ctx):
    """List all cached binaries and HTTP responses."""
    rows, total_size = cache_rows()
    if not rows:
        echo("Cache is empty.")
        ctx.exit(0)

    ctx.print_table(rows, CACHE_LIST_HEADER_DEFS)
    echo(f"\nTotal: {len(rows)} file(s), {format_file_size(total_size)}")


@cache.command(short_help="Remove cached entries")
@option(
    "--tool",
    default=None,
    help="Only remove the binary and config entries for this tool.",
)
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
    """Remove cached binaries, tool configs and HTTP responses.

    Without options, removes everything. Use --tool to target a specific
    binary tool and its cached config, --namespace for a specific HTTP
    namespace, or --max-age for entries older than a threshold.

    \b
    Examples:
        repomatic cache clean
        repomatic cache clean --tool ruff
        repomatic cache clean --namespace pypi
        repomatic cache clean --max-age 7
    """
    # Each scoping option only reaches the cache kinds it applies to: a
    # --namespace clean leaves binaries and configs alone, a --tool clean
    # leaves HTTP responses alone. Bare and age-only cleans cover everything.
    scoped = tool is not None or namespace is not None
    bin_deleted = bin_freed = cfg_deleted = cfg_freed = 0
    http_deleted = http_freed = 0
    if tool is not None or not scoped:
        bin_deleted, bin_freed = clear_cache(tool=tool, max_age_days=max_age)
        cfg_deleted, cfg_freed = clear_config_cache(tool=tool, max_age_days=max_age)
    if namespace is not None or not scoped:
        http_deleted, http_freed = clear_http_cache(
            namespace=namespace,
            max_age_days=max_age,
        )
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


@repomatic.command(
    short_help="Remove the ToC entries awesome-lint forbids",
    section=_section_lint,
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

    \b
    Example:
        repomatic fix-awesome-toc
    """
    report = fix_awesome_toc()
    if not report:
        echo("No forbidden ToC entry found.")
        return

    for readme, removed in report.items():
        echo(f"{readme}: removed {', '.join(removed)}")


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

    Warns, non-fatally, about a released section holding no entry: a
    published release heading with nothing under it reads as broken to
    anyone scanning the notes for that version.

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
        changelog_path = resolved_changelog_path(config)
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


_lint_deps_sort = SortByOption(*LINT_DEPS_HEADER_DEFS, default="package")


@repomatic.command(
    name="lint-deps",
    short_help="Check dependencies resolve from the public index",
    section=_section_lint,
    params=[_lint_deps_sort],
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
@option(
    "--output",
    type=file_path(writable=True, resolve_path=True, allow_dash=True),
    default=None,
    help="Write a markdown report of the blocking findings to this file.",
)
@output_format_option
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

    \b
    Examples:
        # Check the current project
        repomatic lint-deps

    \b
        # Report without failing, for an ordinary CI push
        repomatic lint-deps --no-fatal

    \b
        # Emit a markdown report for a PR body
        repomatic lint-deps --no-fatal --output report.md
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
        rows = [
            (
                finding.package,
                str(finding.kind),
                finding.location,
                finding.verdict,
            )
            for finding in findings
        ]
        ctx.print_table(rows, LINT_DEPS_HEADER_DEFS)

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


@repomatic.command(
    short_help="Run repository consistency checks", section=_section_lint
)
@repo_name_option
@repo_slug_option
@has_cloudflare_account_id_option
@has_cloudflare_api_token_option
@has_notifications_pat_option
@has_pat_option
@has_virustotal_key_option
@pass_context
def lint_repo(
    ctx: Context,
    repo_name: str | None,
    repo: str | None,
    has_cloudflare_account_id: bool,
    has_cloudflare_api_token: bool,
    has_notifications_pat: bool,
    has_pat: bool,
    has_virustotal_key: bool,
) -> None:
    """Run consistency checks on repository metadata.

    Reads package_name, is_sphinx, and project_description from
    pyproject.toml in the current directory.

    \b
    Checks:
      - Package name vs repository name (warning).
      - Website field set for Sphinx projects, and matching the documentation
        URL declared in [project.urls] (warning).
      - Repository description matches project description (error).
      - Inline upstream pins match the version the uses: refs name (error).
      - Inline upstream pins resolving under a cooldown carry their
        --exclude-newer-package exemption (error).
      - Workflows only ask repomatic metadata for keys it still emits (error).
      - Every astral-sh/setup-uv step pins one uv version (warning).
      - GitHub topics subset of pyproject.toml keywords (warning).
      - Funding file present when owner has GitHub Sponsors (warning).
      - Stale draft releases (non-.dev0 drafts) (warning).
      - Install guide download URLs resolve to real release assets (warning).
      - Repository-local PR body templates sit in .github/pr-templates/
        and carry valid frontmatter (warning).
      - Fork PR workflow approval policy strict enough (warning).
      - VIRUSTOTAL_API_KEY secret missing when Nuitka is active (warning).
      - REPOMATIC_NOTIFICATIONS_PAT secret missing when the unsubscribe
        workflow is enabled (warning).
      - CLOUDFLARE_API_TOKEN or CLOUDFLARE_ACCOUNT_ID secret missing when
        sphinx.deploy targets Cloudflare Pages (warning).

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

    if repo_name is None and repo:
        # Extract repo name from owner/repo format.
        repo_name = repo.split("/")[-1] if "/" in repo else repo

    # Derive package_name, is_sphinx, project_description, docs_url and keywords
    # from pyproject.toml.
    metadata = Metadata()
    package_name = get_project_name()
    is_sphinx = metadata.is_sphinx
    project_description = metadata.project_description
    project_table = metadata.pyproject_toml.get("project", {})
    docs_url = documentation_url(project_table.get("urls"))
    keywords = project_table.get("keywords")

    config = get_tool_config(ctx)
    nuitka_active = config.nuitka_enabled and bool(metadata.script_entries)

    exit_code = run_repo_lint(
        package_name=package_name,
        repo_name=repo_name,
        is_package=metadata.is_python_package,
        is_sphinx=is_sphinx,
        sphinx_deploy=config.sphinx_deploy,
        project_description=project_description,
        docs_url=docs_url,
        keywords=keywords,
        repo=repo if repo else None,
        has_pat=has_pat,
        has_virustotal_key=has_virustotal_key,
        has_cloudflare_api_token=has_cloudflare_api_token,
        has_cloudflare_account_id=has_cloudflare_account_id,
        nuitka_active=nuitka_active,
        has_notifications_pat=has_notifications_pat,
        unsubscribe_active=config.notification_unsubscribe,
    )
    ctx.exit(exit_code)


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
    "--verify",
    is_flag=True,
    default=False,
    help=(
        "Report which targets the tool would rewrite, without touching them."
        " Runs the write path against throwaway copies, so the answer holds"
        " even for a tool whose own --check mode is unreliable."
    ),
)
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
    verify,
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
    Report what a formatter would rewrite, leaving the tree alone:
        repomatic run mdformat --verify -- changelog.md

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

    if verify:
        exit_code, drifted = verify_via_write_path(
            tool_name,
            extra_args=extra_args,
            version=tool_version,
            checksum=checksum,
            skip_checksum=skip_checksum,
            no_cache=no_cache,
        )
        for path in drifted:
            echo(f"Would rewrite: {path}")
        ctx.exit(exit_code)

    exit_code = run_tool(
        tool_name,
        extra_args=extra_args,
        version=tool_version,
        checksum=checksum,
        skip_checksum=skip_checksum,
        no_cache=no_cache,
    )
    ctx.exit(exit_code)


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


@repomatic.command(
    short_help="Maintain a Markdown-formatted changelog", section=_section_release
)
@option(
    "--source",
    type=file_path(exists=True, readable=True, resolve_path=True),
    default=None,
    help="Changelog source file. Defaults to the configured changelog.location.",
)
@option(
    "--default-branch",
    default="main",
    show_default=True,
    help="Branch name the unreleased comparison URL points at.",
)
@argument(
    "changelog_path",
    type=file_path(writable=True, resolve_path=True, allow_dash=True),
    default=STDOUT_SENTINEL,
)
@pass_context
def changelog(ctx, source, default_branch, changelog_path):
    """Stamp the changelog with the current version's release header."""
    if source is None:
        source = resolved_changelog_path(get_tool_config(ctx))
    initial_content = None
    if source.exists():
        logging.info(f"Read initial changelog from {source}")
        initial_content = source.read_text(encoding="UTF-8")

    changelog = Changelog(initial_content, Metadata.get_current_version())
    content = changelog.update(default_branch=default_branch)
    if content == initial_content:
        logging.warning("Changelog already up to date. Do nothing.")
        ctx.exit()

    log_output_target("updated changelog", changelog_path)
    echo(content, file=prep_path(changelog_path))


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
@option(
    "--all-changes/--no-all-changes",
    default=False,
    help="Stage every change in the working tree instead of named files, for a "
    "job whose output paths come from configuration.",
)
@argument(
    "paths",
    nargs=-1,
    required=False,
    type=file_path(exists=True, resolve_path=True),
)
def git_commit_push(
    message: str,
    remote: str,
    branch: str,
    all_changes: bool,
    paths: tuple[Path, ...],
) -> None:
    """Commit the given files and push them to a remote branch.

    Idempotent: exits successfully without creating a commit when the files
    are unchanged. A rejected push (something else pushed meanwhile) is
    retried after rebasing onto the fresh remote tip, so release jobs can
    publish generated files to the default branch without racing other
    pushes. Works from a detached HEAD.

    With --all-changes, everything the working tree carries is staged rather
    than a named list. Reserved for a job whose writers are the steps right
    before it and whose output paths only its configuration knows.

    \b
    Examples:
        repomatic git-commit-push --message "Record v1.2.3 binaries" \\
            docs/binaries.md docs/assets/virustotal-scans.json

    \b
        repomatic git-commit-push --message "Sample star counts" --all-changes
    """
    if bool(paths) == all_changes:
        raise UsageError("Pass either PATHS or --all-changes, not both or neither.")
    try:
        pushed = commit_and_push_files(
            paths, message, remote=remote, branch=branch, all_changes=all_changes
        )
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
    short_help="Name an attestation bundle after the asset it attests",
    section=_section_release,
)
@option(
    "--bundle",
    "bundle_path",
    type=file_path(exists=True, readable=True, resolve_path=True),
    required=True,
    help="Attestation bundle written by actions/attest.",
)
@option(
    "--dir",
    "asset_dir",
    type=dir_path(exists=True, resolve_path=True),
    default=".",
    show_default=True,
    help="Directory holding the attested assets, and where the bundle lands.",
)
@option(
    "--name",
    "set_name",
    help="Stem naming the set, for a bundle attesting more than one asset.",
)
def pack_attestation_cmd(
    bundle_path: Path, asset_dir: Path, set_name: str | None
) -> None:
    """Rename an attestation bundle after its subject, print the upload list.

    actions/attest writes every bundle to the same `attestation.json`
    basename, so a release attaching several would keep only the last. This
    reads back the subjects the bundle actually attests, names it after them,
    and prints every file the release upload step should attach: the assets
    and their bundle, one path per line.

    A bundle covering several assets has no single name to take, so pass
    --name to name the set instead.

    Idempotent: re-running copies the same bytes over the same name.

    \b
    Examples:
        # Single asset, named papaya.tar.gz.attestation.json
        repomatic pack-attestation --bundle "${BUNDLE_PATH}"

    \b
        # A glob's worth of assets, all covered by one bundle
        repomatic pack-attestation --bundle b.json --dir dist --name papaya-set
    """
    for path in pack_attestation(bundle_path, asset_dir, set_name):
        echo(path)


@repomatic.command(
    short_help="Pack compiled binaries and their versionless aliases",
    section=_section_release,
)
@option(
    "--version",
    "version_str",
    required=True,
    help="Release version whose binaries earn versionless aliases.",
)
@option(
    "--dir",
    "dist_dir",
    type=dir_path(exists=True, resolve_path=True),
    required=True,
    help="Directory holding the compiled binaries and attestation bundles.",
)
def pack_binaries(version_str: str, dist_dir: Path) -> None:
    """Materialize versionless binary aliases and print the upload list.

    Copies each versioned binary (`repomatic-1.2.3-linux-arm64.bin`) to its
    versionless alias (`repomatic-linux-arm64.bin`) so the stable
    releases/latest/download URLs always resolve, then prints every file the
    release upload step should attach, one path per line. Python
    distributions (.tar.gz, .whl) are skipped: create-release already
    uploaded them.

    Idempotent: re-running overwrites the same aliases with the same bytes.

    \b
    Examples:
        repomatic pack-binaries --version 1.2.3 --dir ./compile-assets
    """
    for path in pack_binary_assets(dist_dir, version_str):
        echo(path)


@repomatic.command(
    short_help="Pack the skills and agents as a Claude Code plugin",
    section=_section_release,
)
@option(
    "--output",
    type=file_path(writable=True, resolve_path=True),
    default=f"./{ARCHIVE_NAME}",
    show_default=True,
    help="Destination path of the plugin archive.",
)
def pack_plugin_cmd(output: Path) -> None:
    """Pack the bundled skills and agents into a Claude Code plugin archive.

    Assembles `.claude-plugin/plugin.json` and every skill and agent the
    component registry declares into a zip holding a single top-level folder,
    which the release lane attaches to each GitHub release. The archive is
    byte-deterministic, so re-packing an unchanged tree produces an identical
    file.

    \b
    Examples:
        # Pack into the default ./repomatic-claude-plugin.zip
        repomatic pack-plugin

    \b
        # Install the packed plugin locally, without a marketplace
        repomatic pack-plugin --output /tmp/repomatic-claude-plugin.zip
        unzip /tmp/repomatic-claude-plugin.zip -d /tmp/plugin
        claude --plugin-dir /tmp/plugin/repomatic
    """
    members = pack_plugin(Path.cwd(), output)
    echo(f"Packed {len(members)} files into {output}")


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
    default=WORKFLOW_TARGET_ROOT,
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
        changelog_path = resolved_changelog_path(get_tool_config(ctx))
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
    name="sample-projects",
    short_help="Snapshot tracked projects' popularity and activity",
    section=_section_sample,
)
@option(
    "--store",
    type=file_path(resolve_path=True),
    default=None,
    help="JSON readings file. Defaults to [tool.repomatic.projects] store.",
)
@pass_context
def sample_projects(ctx: Context, store: Path | None) -> None:
    """Read every tracked project's stars, newest release and newest commit.

    Reads each project through whichever API its host speaks (GitHub, GitLab or
    Forgejo) and records one row per project, rewritten only when a reading
    moves. A project that fails to answer keeps its previous reading rather
    than losing it, so one flaky instance blanks no column.

    \b
    Examples:
        repomatic sample-projects

    \b
        repomatic sample-projects --store docs/assets/project-metrics.json
    """
    config = get_tool_config(ctx)
    exit_if_disabled(ctx, config.projects.sync, "projects.sync")

    if not config.projects.repos:
        echo("No project declared in [tool.repomatic.projects] repos, nothing to read.")
        return

    readings = store or Path(config.projects.store)
    try:
        outcomes = _sample_projects(
            readings, config.projects.repos, config.projects.forges
        )
    except ValueError as error:
        raise ClickException(str(error))

    rows = [
        (
            outcome.project_id,
            str(outcome.metrics.stars) if outcome.metrics else "—",
            (outcome.metrics.release or "—") if outcome.metrics else "—",
            (outcome.metrics.commit or "—") if outcome.metrics else "—",
            outcome.error or ("updated" if outcome.changed else "unchanged"),
        )
        for outcome in outcomes
    ]
    ctx.print_table(rows, PROJECT_SAMPLE_HEADER_DEFS)

    moved = sum(1 for outcome in outcomes if outcome.changed)
    if moved:
        echo(f"Recorded {moved} reading(s) in {readings}.")
    else:
        echo(f"{readings} already up to date.")


@repomatic.command(
    name="sample-stars",
    short_help="Accumulate the star history of tracked repositories",
    section=_section_sample,
)
@option(
    "--store",
    type=file_path(resolve_path=True),
    default=None,
    help="JSON history file. Defaults to [tool.repomatic.stars] store.",
)
@option(
    "--forward/--no-forward",
    default=True,
    help="Snapshot today's aggregate star count of every tracked repository.",
)
@option(
    "--reconstruct/--no-reconstruct",
    default=True,
    help="Rebuild exact curves from per-star timestamps, for repositories the "
    "token administers.",
)
@option(
    "--backfill-wayback",
    is_flag=True,
    default=False,
    help="Mine contemporaneous counts from archived GitHub pages. Slow, and a "
    "one-off: the scheduled job never runs it.",
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
def sample_stars(
    ctx: Context,
    store: Path | None,
    forward: bool,
    reconstruct: bool,
    backfill_wayback: bool,
    import_csv: tuple[Path, ...],
    render: bool,
) -> None:
    """Accumulate the star history of every tracked repository, and chart it.

    GitHub restricted its stargazer endpoints to a repository's own admins in
    2026, which left every third-party star chart on the web rendering an error
    card. The aggregate count stayed public, so this snapshots it on a schedule
    and commits the result: a history that accrues locally cannot be revoked.

    Each point records where it came from, since the curves are not all
    measured the same way. A repository the token administers is reconstructed
    exactly from the timestamp of every star it still holds; the rest are
    sampled forward, and backfilled from archived pages or from a
    star-history.com export.

    \b
    Examples:
        repomatic sample-stars

    \b
        repomatic sample-stars --no-reconstruct --backfill-wayback

    \b
        repomatic sample-stars --import-csv star-history-export.csv
    """
    config = get_tool_config(ctx)
    exit_if_disabled(ctx, config.stars.sync, "stars.sync")

    if not config.stars.series:
        echo("No repository declared in [tool.repomatic.stars] series, nothing to do.")
        return

    history = store or Path(config.stars.store)
    try:
        records = load_star_records(history)
    except ValueError as error:
        raise ClickException(str(error))
    tracked = collected_repos(config.stars.series, config.stars.predecessors)

    outcomes = []
    if forward:
        for name, repo in tracked.items():
            outcomes.append(sample_current(records, name, repo))
    if reconstruct:
        for name, repo in tracked.items():
            outcomes.append(reconstruct_from_github(records, name, repo))
    for export in import_csv:
        try:
            outcomes.extend(import_star_history_csv(records, export, tracked.values()))
        except (OSError, ValueError) as error:
            raise ClickException(str(error))
    if backfill_wayback:
        for name, repo in tracked.items():
            outcomes.append(_backfill_wayback(records, name, repo, history))

    ctx.print_table(
        [
            (
                outcome.name,
                outcome.repo,
                str(outcome.stars) if outcome.stars is not None else "—",
                str(outcome.points),
                outcome.note or "—",
            )
            for outcome in outcomes
        ],
        STAR_SAMPLE_HEADER_DEFS,
    )

    if save_star_records(history, records):
        echo(f"Recorded {len(records)} point(s) in {history}.")
    else:
        echo(f"{history} already up to date.")

    if not render:
        return
    if not records:
        echo("No point recorded yet, so no chart to draw.")
        return
    # Stamped with the newest reading rather than with today, so re-rendering
    # an unmoved history rewrites nothing. A caption naming the run date would
    # churn the committed SVGs on every scheduled pass that found no new star.
    stamp = max(record.day for record in records.values())
    grouped = star_series(records, config.stars.series, config.stars.predecessors)
    for entry in config.stars.charts:
        try:
            spec = ChartSpec.from_mapping(entry)
            changed = write_chart(grouped, spec, config.stars.colors, stamp)
        except ValueError as error:
            raise ClickException(str(error))
        echo(f"{'Redrew' if changed else 'Unchanged'} {spec.output}.")


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
    default=FREE_TIER_RATE_LIMIT,
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
    components,
    version_pin,
    cooldown,
    repo,
    output_dir,
    delete_excluded,
    delete_unmodified,
    keep_removed,
    delete_removed_modified,
):
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
def list_skills() -> None:
    """List all bundled Claude Code skills grouped by lifecycle phase.

    Reads skill definitions from the bundled data files and displays them
    in a table grouped by phase: Setup, Development, Quality, and Release.
    """
    skills = skill_catalog()

    # Group by phase in canonical order.
    for phase in SKILL_PHASE_ORDER:
        phase_skills = [(n, d) for p, n, d in skills if p == phase]
        if not phase_skills:
            continue
        echo(f"\n{phase}:")
        for name, description in phase_skills:
            echo(f"  /{name:<24s} {description}")

    echo("")


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

    Renders a table of all available options, their types, defaults, and
    descriptions, generated from the Config dataclass docstrings.
    Respects the global --table-format and --sort-by options.
    """
    rows = [
        (option, escape_type_for_gfm_table(ftype), default, desc)
        for option, ftype, default, desc in config_reference()
    ]
    ctx.print_table(rows, CONFIG_REFERENCE_HEADER_DEFS)


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
    default=WORKFLOW_TARGET_ROOT,
    help="Directory containing workflow YAML files.",
)
@option(
    "--upstream-repo",
    "repo",
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
    short_help="Bump SHA-pinned GitHub Actions to their latest release",
    section=_section_sync,
)
@version_report_output_option
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
    exit_if_disabled(ctx, get_tool_config(ctx).action_pins_sync, "action-pins.sync")
    run_version_sync(
        ctx,
        "sync-action-pins",
        output,
        output_format,
        release_notes,
        held_back,
        "All action pins are up to date.",
    )


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
    name="sync-dep-sources",
    short_help="Swap git-tracked dependencies to their released versions",
    section=_section_sync,
)
@lockfile_option
@sync_table_option
@sync_release_notes_option
@lock_held_back_option
@report_output_option
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

    op, rc, plan = resolve_lockfile_plan(
        "sync-dep-sources",
        config,
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

    emit_lockfile_sync_report(
        ctx,
        plan,
        reference_date=rc.today,
        table=table,
        output=output,
        output_format=output_format,
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
    # Same spelling as the shared `dry_run_option`, so `--live` works on every
    # command that has a dry-run mode; only the default differs (`sync-deps`
    # is the "do it" aggregate, so it runs live unless asked not to).
    "--dry-run/--live",
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
        print_plan_tables(ctx, plan, rc.today)

    if output:
        sections = [
            body for _op, plan in changed if (body := render_plan_markdown(plan))
        ]
        emit_report("\n\n".join(sections), output, output_format)

    if failed:
        echo(f"Failed to resolve: {', '.join(failed)}", err=True)
        ctx.exit(1)


@repomatic.command(
    short_help="Sync rolling dev pre-release on GitHub",
    section=_section_sync,
)
@dry_run_option
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

    loaded = load_changelog_repo(config)
    if loaded is None:
        return
    changelog_path, repo_url = loaded

    # Parse owner/repo for gh CLI.
    parsed = owner_repo(repo_url)
    repository = "/".join(parsed) if parsed else ""

    if delete:
        if dry_run:
            echo("[dry-run] Would delete all dev releases.")
            return
        _cleanup_dev_releases(repository)
        echo("Deleted all dev releases.")
        return

    if _sync_dev_release(
        changelog_path, version, repository, dry_run, asset_dir=upload_assets
    ):
        mode = "dry-run" if dry_run else "live"
        echo(f"[{mode}] Dev release v{version} synced.")


@repomatic.command(
    short_help="Sync GitHub release notes from changelog",
    section=_section_sync,
)
@dry_run_option
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
    loaded = load_changelog_repo(get_tool_config())
    if loaded is None:
        return
    changelog_path, repo_url = loaded

    result = _sync_github_releases(repo_url, changelog_path, dry_run)
    echo(_render_sync_report(result))


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
@option(
    "--drop-orphans/--no-drop-orphans",
    default=False,
    help=(
        "Overwrite rules found on disk but absent from the generated file, "
        "instead of refusing to drop them."
    ),
)
@pass_context
def sync_gitignore(ctx: Context, output_path: Path | None, drop_orphans: bool) -> None:
    """Sync a .gitignore file from gitignore.io templates.

    Fetches templates for a base set of categories plus any extras from
    [tool.repomatic] config, then appends gitignore-extra-content.
    Writes to the path specified by gitignore-location (default
    ./.gitignore).

    The generated file is the whole file: nothing is read back from the copy on
    disk. A rule added there by hand is therefore dropped on the next sync, so
    the write aborts when it would lose one. Move the rule into
    gitignore-extra-content to keep it, or pass --drop-orphans to let it go.

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

    # Compare against what is already there before overwriting it. Skipped for
    # stdout, which destroys nothing, and for a first run, which has nothing to
    # destroy.
    if not drop_orphans and str(output_path) != "-" and output_path.is_file():
        orphans = orphaned_rules(output_path.read_text(encoding="UTF-8"), content)
        if orphans:
            emit_annotation(
                AnnotationLevel.ERROR,
                f"{output_path}: sync would drop {len(orphans)} hand-added "
                f"rule(s): {', '.join(orphans)}",
            )
            echo(
                f"Refusing to overwrite {output_path}: "
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

    log_output_target(".gitignore", output_path)

    echo(content.rstrip(), file=prep_path(output_path))


@repomatic.command(
    short_help="Sync repository labels via labelmaker", section=_section_sync
)
@repo_slug_option
@pass_context
def sync_labels(ctx: Context, repo: str | None) -> None:
    """Sync repository labels from bundled definitions using labelmaker.

    Exports label definitions to a scratch directory, then applies them to the
    repository using labelmaker. Applies the default profile to all
    repositories, plus the awesome profile for awesome-* repos.

    Authentication follows the canonical token resolution (REPOMATIC_PAT,
    then GH_TOKEN, then GITHUB_TOKEN). Downloads labelmaker automatically via
    the tool registry.
    """
    config = get_tool_config(ctx)
    exit_if_disabled(ctx, config.labels.sync, "labels.sync")

    # Auto-detect repository.
    meta = Metadata()
    if repo is None:
        repo = meta.repo_slug
    if not repo:
        raise ClickException("Cannot detect repository.")

    # Dump label files somewhere disposable: they are labelmaker inputs, not
    # repository content, so syncing labels leaves the working tree untouched.
    with tempfile.TemporaryDirectory(prefix="repomatic-labels-") as tmpdir:
        labels_dir = Path(tmpdir)
        result = run_init(output_dir=labels_dir, components=("labels",), config=config)
        for path in [*result.created, *result.updated]:
            logging.info(f"Exported: {path}")

        try:
            apply_labels(
                config, repo, is_awesome=meta.is_awesome, labels_dir=labels_dir
            )
        except RuntimeError as e:
            raise ClickException(str(e))

    echo("Labels synced.")


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
    help="If not found, either create the missing destination mailmap file, or "
    "skip the update process entirely. This option is ignored if the destination "
    f"is to print the result to {sys.stdout.name}.",
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

    # An absent source starts from empty content, so the first run on a fresh
    # repository (the --create-if-missing bootstrap) has a baseline to diff
    # against instead of an unbound name.
    content = ""
    if source.exists():
        logging.info(f"Read initial mapping from {source}")
        content = remove_header(source.read_text(encoding="UTF-8"))
        mailmap.parse(content)
    else:
        logging.debug(f"Mailmap source file {source} does not exist.")

    mailmap.update_from_git()
    new_content = mailmap.render()

    log_output_target("updated mailmap", destination_mailmap)
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

    echo(
        generated_header(ctx.command_path) + new_content,
        file=prep_path(destination_mailmap),
    )


@repomatic.command(
    short_help="Bump registry tool versions from upstream releases",
    section=_section_sync,
)
@version_report_output_option
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
    exit_if_disabled(ctx, get_tool_config(ctx).tool_versions_sync, "tool-versions.sync")
    run_version_sync(
        ctx,
        "sync-tool-versions",
        output,
        output_format,
        release_notes,
        held_back,
        "All tools are up to date.",
    )


@repomatic.command(
    short_help="Re-lock dependencies and roll cooldown overrides forward",
    section=_section_sync,
)
@lockfile_option
@sync_table_option
@sync_release_notes_option
@lock_held_back_option
@report_output_option
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
        machine resolves the lockfile against the same uv floor and cooldown
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

    op, rc, plan = resolve_lockfile_plan(
        "sync-uv-lock",
        config,
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

    emit_lockfile_sync_report(
        ctx,
        plan,
        reference_date=rc.today,
        table=table,
        output=output,
        output_format=output_format,
    )


@repomatic.command(
    short_help="Bump npm/PyPI version literals in workflow YAML",
    section=_section_sync,
)
@version_report_output_option
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
    exit_if_disabled(ctx, get_tool_config(ctx).workflow_pins_sync, "workflow-pins.sync")
    run_version_sync(
        ctx,
        "sync-workflow-pins",
        output,
        output_format,
        release_notes,
        held_back,
        "All workflow pins are up to date.",
    )
