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
"""GitHub commands of the `repomatic` CLI: issues, pull requests and labels.

{data}`repomatic.cli.main.COMMANDS` registers each command here, and files it
under its help section.
"""

from __future__ import annotations

import logging
from pathlib import Path

from click_extra import (
    STDOUT_SENTINEL,
    Choice,
    ClickException,
    IntRange,
    UsageError,
    echo,
    file_path,
    get_tool_config,
    option,
    pass_context,
)

from ..broken_links import manage_combined_broken_links_issue
from ..components import is_awesome_repo
from ..config import Config
from ..github import token as _token_mod, unsubscribe as _unsub_mod
from ..github.event import (
    get_default_author,
    get_default_number,
    is_pull_request,
)
from ..github.labels import apply_labels, labels_for_thread
from ..github.outputs import format_multiline_output
from ..github.pr import close_open_prs_on_branch, upsert_pr
from ..github.pr_body import TemplateArgumentError, render_pr
from ..github.sponsor import is_sponsor
from ..github.threads import (
    BOT_ISSUE_LABEL,
    LOCK_INACTIVE_DAYS,
    LOCK_ISSUE_COMMENT,
    LOCK_PR_COMMENT,
    LOCK_REASON,
    LOCK_REASONS,
    LOCK_SEARCH_LIMIT,
    LOCK_THREADS_HEADER_DEFS,
    ThreadKind,
    add_labels,
    lock_stale_threads,
)
from ..github.token import require_token
from ..github.unsubscribe import (
    render_report,
    unsubscribe_threads,
)
from ..github.urls import repo_name
from ..lint_repo.context import LintContext
from ..metadata.core import Metadata
from ..metadata.project import is_version_bump_allowed
from ..setup_guide import manage_setup_guide
from ..templating import PrTemplate, get_template_names
from ..versions import BUMP_PARTS
from .main import (
    dry_run_option,
    exit_if_disabled,
    has_cloudflare_api_token_option,
    has_cloudflare_r2_keys_option,
    has_notifications_pat_option,
    has_pat_option,
    has_virustotal_key_option,
    repo_name_option,
    repo_option,
    report_options,
    subcommand,
    write_output,
)

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Any, TypeVar

    from click_extra import Context

    FC = TypeVar("FC", bound="Callable[..., Any]")


SPONSOR_LABEL = "💖 sponsor"
"""The label `sponsor-label` applies, as `repomatic/data/labels.toml` names it."""


def thread_options(func: FC) -> FC:
    """`--number` and `--pr/--issue`, read from the event payload when absent.

    The thread a command acts on: {func}`resolve_thread` applies the defaults.
    """
    func = option(
        "--pr/--issue",
        "is_pr",
        default=None,
        help="Specify issue or pull request. Auto-detected from $GITHUB_EVENT_PATH.",
    )(func)
    return option(
        "--number",
        type=IntRange(min=1),
        help="Issue or PR number. Defaults to the number in $GITHUB_EVENT_PATH.",
    )(func)


def resolve_thread(number: int | None, is_pr: bool | None) -> tuple[int, ThreadKind]:
    """Complete the {func}`thread_options` values from the event payload.

    :param number: The `--number` value, read from the event when `None`.
    :param is_pr: The `--pr/--issue` value, read from the event when `None`.
    :return: The thread number and kind.
    :raises UsageError: When neither the option nor the event names a thread.
    """
    if number is None:
        number = get_default_number()
    if not number:
        msg = (
            "Missing --number, and it could not be auto-detected from the environment."
        )
        raise UsageError(msg)
    return number, ThreadKind.of(is_pull_request() if is_pr is None else is_pr)


def template_options(func: FC) -> FC:
    """The options that pick a template and feed its arguments.

    {func}`resolve_template` reads `--template` and `--template-file` into one
    template, and {func}`parse_template_args` reads `--template-arg` and
    `--template-arg-file` into one mapping.
    """
    decorators = (
        option(
            "--template",
            type=Choice(get_template_names(), case_sensitive=False),
            default=None,
            help="Built-in template to render the content from.",
        ),
        option(
            "--template-file",
            "template_file",
            type=file_path(exists=True, readable=True, resolve_path=True),
            default=None,
            help=(
                "Template file to render the content from, instead of --template:"
                " markdown with optional YAML frontmatter, for project-specific"
                " content. Set 'footer: false' in its frontmatter: the metadata"
                " block already carries the attribution footer."
            ),
        ),
        option(
            "--template-arg",
            "template_args_cli",
            multiple=True,
            metavar="KEY=VALUE",
            help=(
                "Pass an arbitrary key/value pair to the template. Repeat to provide"
                " multiple. Use this to feed template variables not covered by the"
                " dedicated flags. Example: --template-arg channel=Nix."
            ),
        ),
        option(
            "--template-arg-file",
            "template_arg_files",
            multiple=True,
            metavar="KEY=PATH",
            help=(
                "Read a template value from a file. Repeat to provide multiple. Use"
                " this for a value with no ceiling on its size, like a generated"
                " table: a report travels as a path rather than inline. Example:"
                " --template-arg-file summary=proposal.md."
            ),
        ),
        option(
            "--version",
            "version",
            default=None,
            help="Version string passed to the template (like 1.2.0).",
        ),
        option(
            "--part",
            type=Choice(BUMP_PARTS, case_sensitive=False),
            default=None,
            help="Version part passed to the bump-version template.",
        ),
    )
    for decorator in reversed(decorators):
        func = decorator(func)
    return func


def resolve_template(
    template: str | None, template_file: Path | None
) -> PrTemplate | None:
    """The template `--template` or `--template-file` names, if either does.

    :raises UsageError: When both are given.
    """
    if template and template_file:
        msg = "--template and --template-file are mutually exclusive."
        raise UsageError(msg)
    ref = template or template_file
    return PrTemplate.load(ref) if ref else None


def parse_template_args(
    template_args_cli: tuple[str, ...], template_arg_files: tuple[str, ...]
) -> dict[str, str]:
    """Read `--template-arg` and `--template-arg-file` into one mapping.

    The files are read after the inline pairs, so a file wins on a key given
    both ways.

    :raises UsageError: On a malformed pair, or a file that cannot be read.
    """
    args: dict[str, str] = {}
    for entry in template_args_cli:
        key, sep, value = entry.partition("=")
        if not sep:
            msg = f"--template-arg expects KEY=VALUE, got {entry!r}."
            raise UsageError(msg)
        args[key.strip()] = value
    for entry in template_arg_files:
        key, sep, raw_path = entry.partition("=")
        if not sep:
            msg = f"--template-arg-file expects KEY=PATH, got {entry!r}."
            raise UsageError(msg)
        path = Path(raw_path)
        if not path.is_file():
            msg = f"--template-arg-file cannot read {raw_path!r}."
            raise UsageError(msg)
        args[key.strip()] = path.read_text(encoding="UTF-8").strip()
    return args


@subcommand(
    short_help="Label an issue or PR from its content and changed files",
    examples=(
        (
            "Preview what the current event's issue or PR would earn",
            "repomatic apply-labels",
        ),
        ("Apply them, as the labeller job does", "repomatic apply-labels --live"),
        (
            "Manual invocation against one pull request",
            "repomatic apply-labels --live --pr --number 42",
        ),
    ),
)
@thread_options
@repo_option()
@dry_run_option(default=True)
@require_token(_token_mod, "validate_gh_token_env")
@pass_context
def apply_labels_cmd(
    ctx: Context,
    number: int | None,
    is_pr: bool | None,
    repo: str,
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
    """
    number, kind = resolve_thread(number, is_pr)
    labels = labels_for_thread(get_tool_config(ctx), number, kind=kind, repository=repo)

    if not labels:
        echo(f"No rule matched {kind.label} #{number}.")
        return
    if dry_run:
        echo(f"Would label {kind.label} #{number} with: {', '.join(labels)}")
        return
    if not add_labels(number, labels, kind=kind, repository=repo):
        raise ClickException(f"Failed to label {kind.label} #{number}")
    echo(f"Labelled {kind.label} #{number} with: {', '.join(labels)}")


@subcommand(
    short_help="Manage broken links issue lifecycle",
    examples=(
        (
            "In GitHub Actions, with every default auto-detected",
            "repomatic broken-links --lychee-exit-code 2",
        ),
        (
            "Explicit options",
            'repomatic broken-links --lychee-exit-code 2 --body-file ./lychee/out.md --repo-name "my-repo"',
        ),
    ),
)
@option(
    "--lychee-exit-code",
    type=int,
    default=None,
    help=(
        "Exit code from lychee: 0 for no broken links, 2 for broken links found."
        " Any other code is a run that did not complete, which leaves the issue"
        " as it is."
    ),
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
@repo_name_option
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
    issue. Creates or updates the issue when a checker found broken links, and
    closes it when every checker that ran completed clean. A checker that did
    not complete leaves the issue as it is. The repository name selects the
    labels the issue carries.

    Requires the gh CLI to be authenticated.

    \b
    In GitHub Actions, most options are auto-detected:
    - --repo-name defaults to $GITHUB_REPOSITORY name component.
    - --body-file defaults to ./lychee/out.md when --lychee-exit-code is set.
    - --output-json defaults to ./docs/_linkcheck/output.json if it exists.
    - --source-url is composed from $GITHUB_SERVER_URL, $GITHUB_REPOSITORY,
      and $GITHUB_SHA when --output-json is set.
    """
    try:
        manage_combined_broken_links_issue(
            repo_name=repo_name,
            lychee_exit_code=lychee_exit_code,
            lychee_body_file=body_file,
            sphinx_output_json=output_json,
            sphinx_source_url=source_url,
        )
    except ValueError as error:
        raise UsageError(str(error)) from error


@subcommand(
    short_help="Close a stale version-bump PR",
    examples=(
        (
            "Close a stale minor-bump pull request",
            "repomatic close-stale-bump-pr --part minor",
        ),
        (
            "Close a stale major-bump pull request",
            "repomatic close-stale-bump-pr --part major",
        ),
    ),
)
@option(
    "--part",
    type=Choice(BUMP_PARTS, case_sensitive=False),
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


@subcommand(
    short_help="Lock closed, inactive issues and PRs",
    examples=(
        ("Preview what a run would lock", "repomatic lock-threads"),
        ("Lock them, as the autolock workflow does", "repomatic lock-threads --live"),
        (
            "Lock silently after six months, sparing triaged threads",
            'repomatic lock-threads --live --inactive-days 180 --issue-comment "" --exclude-label "🚧 needs triage"',
        ),
    ),
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
    type=Choice(LOCK_REASONS),
    default=LOCK_REASON,
    help="Reason recorded with the lock.",
)
@repo_option()
@dry_run_option(default=True)
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
    repo: str,
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
    """
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


@subcommand(
    short_help="Generate PR body with workflow metadata",
    examples=(
        ("Preview metadata block locally", "repomatic pr-body"),
        (
            "CI: write as GitHub Actions step outputs",
            'repomatic pr-body --output "$GITHUB_OUTPUT" --output-format github-actions',
        ),
        (
            "Use a built-in template",
            "repomatic pr-body --template bump-version --version 1.2.0 --part minor",
        ),
        (
            "Use a downstream-shipped template with custom variables",
            "repomatic pr-body --template-file path/to/template.md --template-arg fruit=mango --template-arg crate=10",
        ),
        (
            "With a prefix above the metadata block",
            'repomatic pr-body --prefix "Fix formatting"',
        ),
    ),
)
@option(
    "--prefix",
    default="",
    help="Content to prepend before the metadata details block.",
)
@option(
    "--prefix-file",
    "prefix_file",
    type=file_path(exists=True, readable=True, resolve_path=True),
    default=None,
    help="Read the prefix from a file instead of --prefix, for a report too "
    "large to pass as an argument. Wins over --prefix when both are given.",
)
@template_options
@option(
    "--pr-ref",
    "pr_ref",
    default=None,
    help="PR reference passed to detect-squash-merge template (like #2316).",
)
@report_options(
    "Write the PR body to this file. github-actions writes the body, title and"
    " commit message as step outputs.",
    default=STDOUT_SENTINEL,
)
@pass_context
def pr_body(
    ctx: Context,
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
    (arbitrary content) or --prefix-file (the same content read from a file).
    If a template and a prefix are both given, the prefix is prepended before
    the rendered template content.
    """
    pr_template = resolve_template(template, template_file)
    if prefix_file:
        prefix = prefix_file.read_text(encoding="UTF-8")
    try:
        content = render_pr(
            Metadata(),
            get_tool_config(ctx),
            template=pr_template,
            prefix=prefix,
            extra_args=parse_template_args(template_args_cli, template_arg_files),
            version=version,
            part=part,
            pr_ref=pr_ref,
        )
    except TemplateArgumentError as error:
        raise UsageError(str(error)) from error

    if output_format == "github-actions":
        parts = [format_multiline_output("body", content.body)]
        if content.title:
            parts.append(f"title={content.title}")
        if content.commit_message:
            parts.append(f"commit_message={content.commit_message}")
        write_output("PR body", "\n".join(parts), output)
    else:
        write_output("PR body", content.body, output)


@subcommand(
    short_help="Create, refresh or retire an automation PR",
    examples=(
        (
            "Refresh an autofix job's pull request",
            "repomatic pr-sync --template format-python",
        ),
        (
            "Open a version-bump PR on its conventional branch",
            "repomatic pr-sync --template bump-version --part minor --branch minor-version-increment",
        ),
        (
            "A one-off PR with explicit content",
            'repomatic pr-sync --branch my-fix --title "Fix" --body "…" --commit-message "Fix the thing"',
        ),
    ),
)
@template_options
@option(
    "--branch",
    default=None,
    help="Head branch to create, update or retire. By convention the job ID; "
    "defaults to the template name, or to the stem of the template file.",
)
@option(
    "--title",
    default=None,
    help="Pull-request title, when not using a template.",
)
@option(
    "--body",
    default=None,
    help="Rendered markdown body, when not using a template.",
)
@option(
    "--commit-message",
    "commit_message",
    default=None,
    help="Commit message, when not using a template.",
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
    envvar="GITHUB_ACTOR",
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
@pass_context
def pr_sync(
    ctx: Context,
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
    """
    pr_template = resolve_template(template, template_file)

    if pr_template:
        if title or body or commit_message:
            msg = (
                "--title, --body and --commit-message are rendered from the "
                "template; pass either a template or the explicit trio, not "
                "both."
            )
            raise UsageError(msg)
        try:
            content = render_pr(
                Metadata(),
                get_tool_config(ctx),
                template=pr_template,
                extra_args=parse_template_args(template_args_cli, template_arg_files),
                version=version,
                part=part,
            )
        except TemplateArgumentError as error:
            raise UsageError(str(error)) from error
        title, body, commit_message = (
            content.title,
            content.body,
            content.commit_message,
        )
        branch = branch or pr_template.name
        labels = labels or pr_template.labels
        if draft is None:
            draft = pr_template.draft
    elif not (title and body and commit_message):
        msg = "Without --template, --title, --body and --commit-message are required."
        raise UsageError(msg)
    elif branch is None:
        msg = "Without --template, --branch is required."
        raise UsageError(msg)

    result = upsert_pr(
        branch=branch,
        title=title,
        body=body,
        commit_message=commit_message,
        base=base,
        labels=labels,
        assignees=assignees,
        draft=bool(draft),
        add_paths=add_paths,
    )
    if result.number:
        echo(f"{result.operation}: PR #{result.number} on {result.branch}")
    else:
        echo(f"{result.operation}: {result.branch}")


@subcommand(
    short_help="Manage setup guide issue lifecycle",
    examples=(
        ("No secret: create or reopen the setup issue", "repomatic setup-guide"),
        (
            "Secret configured: close the issue if all checks pass",
            "repomatic setup-guide --has-pat",
        ),
    ),
)
@has_cloudflare_api_token_option
@has_cloudflare_r2_keys_option
@has_notifications_pat_option
@has_pat_option
@has_virustotal_key_option
@repo_option(detect=False)
@require_token(_token_mod, "validate_gh_token_env")
@pass_context
def setup_guide(
    ctx: Context,
    has_cloudflare_api_token: bool,
    has_cloudflare_r2_keys: bool,
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
    """
    config = get_tool_config(ctx)
    exit_if_disabled(ctx, config.setup_guide, "setup-guide")

    manage_setup_guide(
        LintContext.from_project(
            config,
            repo=repo,
            has_pat=has_pat,
            has_notifications_pat=has_notifications_pat,
            has_virustotal_key=has_virustotal_key,
            has_cloudflare_api_token=has_cloudflare_api_token,
            has_cloudflare_r2_keys=has_cloudflare_r2_keys,
        )
    )


@subcommand(
    short_help="Label issues/PRs from GitHub sponsors",
    examples=(
        (
            "In GitHub Actions, with every default auto-detected",
            "repomatic sponsor-label",
        ),
        ("Override specific values", 'repomatic sponsor-label --label "sponsor"'),
        (
            "Manual invocation with all values",
            "repomatic sponsor-label --owner kdeldycke --author some-user --repo kdeldycke/repomatic --number 123 --issue",
        ),
    ),
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
@repo_option()
@thread_options
@option(
    "--label",
    default=SPONSOR_LABEL,
    help="Label to add if author is a sponsor.",
)
@require_token(_token_mod, "validate_gh_token_env")
def sponsor_label(
    owner: str | None,
    author: str | None,
    repo: str,
    number: int | None,
    is_pr: bool | None,
    label: str,
) -> None:
    """Add a label to issues or PRs from GitHub sponsors.

    Checks if the author of an issue or PR is a sponsor of the repository owner.
    If they are, adds the specified label.

    This command requires the gh CLI to be authenticated.

    When run in GitHub Actions, all parameters are auto-detected from environment
    variables ($GITHUB_REPOSITORY_OWNER, $GITHUB_REPOSITORY) and the event payload
    ($GITHUB_EVENT_PATH). You can override any auto-detected value by passing it
    explicitly.
    """
    owner = owner or Metadata().repo_owner
    author = author or get_default_author()
    if not owner or not author:
        missing = [
            name
            for name, value in (("--owner", owner), ("--author", author))
            if not value
        ]
        raise UsageError(
            f"Missing required parameters: {', '.join(missing)}. "
            "These could not be auto-detected from the environment."
        )
    number, kind = resolve_thread(number, is_pr)

    if not is_sponsor(owner, author):
        echo(f"Author {author!r} is not a sponsor of {owner!r}")
        return
    if not add_labels(number, [label], kind=kind, repository=repo):
        raise ClickException("Failed to add sponsor label")
    echo(f"Added {label!r} label to {kind.label} #{number}")


@subcommand(short_help="Sync repository labels via labelmaker")
@repo_option()
@pass_context
def sync_labels(ctx: Context, repo: str) -> None:
    """Sync repository labels from bundled definitions using labelmaker.

    Applies the bundled label definitions to the repository using labelmaker:
    the default profile to all repositories, plus the awesome profile for
    awesome-* repos, then the definitions the project adds. Each one is staged
    in a scratch directory, so syncing labels leaves the working tree
    untouched.

    Authentication follows the canonical token resolution (REPOMATIC_PAT,
    then GH_TOKEN, then GITHUB_TOKEN). Downloads labelmaker automatically via
    the tool registry.
    """
    config = get_tool_config(ctx)
    exit_if_disabled(ctx, config.labels.sync, "labels.sync")
    try:
        apply_labels(config, repo, is_awesome=is_awesome_repo(repo_name(repo)))
    except RuntimeError as error:
        raise ClickException(str(error)) from error
    echo("Labels synced.")


@subcommand(
    short_help="Unsubscribe from closed, inactive notification threads",
    examples=(
        (
            "Dry run to preview what would be unsubscribed",
            "repomatic unsubscribe-threads --dry-run",
        ),
        (
            "Unsubscribe from threads inactive for 6+ months",
            "repomatic unsubscribe-threads --months 6",
        ),
        (
            "Unsubscribe from at most 50 threads per phase",
            "repomatic unsubscribe-threads --max-unsubscribes 50",
        ),
    ),
)
@option(
    "--months",
    type=IntRange(min=1),
    default=Config.notification_months,
    help="Inactivity threshold in months. Threads updated more recently are kept.",
)
@option(
    "--max-unsubscribes",
    type=IntRange(min=1),
    default=Config.notification_max_unsubscribes,
    help="Maximum unsubscribes per phase. Inspection is not capped.",
)
@dry_run_option(default=True)
@require_token(_unsub_mod, "_validate_notifications_token")
def unsubscribe_threads_cmd(months: int, max_unsubscribes: int, dry_run: bool) -> None:
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
    """
    result = unsubscribe_threads(months, max_unsubscribes, dry_run)
    echo(render_report(result, Metadata().repo_url))
