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
import os
import tempfile
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
from ..config import Config
from ..deps.sources import build_release_readiness
from ..git_ops import current_branch
from ..github import token as _token_mod, unsubscribe as _unsub_mod
from ..github.actions import (
    format_multiline_output,
    get_default_author,
    get_default_number,
    get_event_subject,
    get_github_event,
    is_pull_request,
    read_file_output,
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
from ..github.labels import (
    apply_labels,
    match_content_rules,
    match_file_rules,
    resolve_content_rules,
    resolve_file_rules,
)
from ..github.pr import (
    close_open_prs_on_branch,
    list_changed_files,
    upsert_pr,
)
from ..github.pr_body import (
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
from ..github.sponsor import (
    get_default_owner,
    is_sponsor,
)
from ..github.token import require_token
from ..github.unsubscribe import (
    render_report,
    unsubscribe_threads,
)
from ..init_project import run_init
from ..metadata.core import Metadata
from ..metadata.project import is_version_bump_allowed
from ..setup_guide import manage_setup_guide
from ..versions import BUMP_PARTS, strip_dev_suffix
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

    from click_extra import Context


# The template-feeding options shared verbatim by `pr-body` and `pr-sync`; their
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
    type=Choice(BUMP_PARTS, case_sensitive=False),
    default=None,
    help="Version part passed to the bump-version template.",
)


def _render_pr_content(
    config: Config,
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

    :param config: The resolved `[tool.repomatic]` configuration.
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
            repo_url=md.repo_url or None,
        )

    arg_sources: dict[str, str | Callable[[], str | None] | None] = {
        "changes_review": lambda: _review_step("changes_review"),
        "dev_release_review": lambda: _review_step("dev_release_review"),
        "diff_table": read_file_output("REPOMATIC_DIFF_TABLE"),
        "part": part,
        "pr_ref": pr_ref,
        "release_readiness": _release_readiness,
        # Callable, will be invoked if needed.
        "repo_url": lambda: md.repo_url,
        # Step outputs of `init --upgrade`, relayed by the `sync-repomatic` job.
        "upgrade_report": read_file_output("REPOMATIC_UPGRADE_REPORT"),
        "upgrade_version": os.getenv("REPOMATIC_UPGRADE_VERSION"),
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
        # Combine the --prefix content with the template content.
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
    config = get_tool_config(ctx)

    if number is None:
        number = get_default_number()
    if is_pr is None:
        is_pr = is_pull_request()
    if not number:
        raise UsageError(
            "Missing --number, and it could not be auto-detected from the environment."
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
    issue. Creates, updates, or closes the issue based on results. The
    repository name selects the labels the issue carries.

    Requires the gh CLI to be authenticated.

    \b
    In GitHub Actions, most options are auto-detected:
    - --repo-name defaults to $GITHUB_REPOSITORY name component.
    - --body-file defaults to ./lychee/out.md when --lychee-exit-code is set.
    - --output-json defaults to ./docs/_linkcheck/output.json if it exists.
    - --source-url is composed from $GITHUB_SERVER_URL, $GITHUB_REPOSITORY,
      and $GITHUB_SHA when --output-json is set.
    """
    manage_combined_broken_links_issue(
        repo_name=repo_name,
        lychee_exit_code=lychee_exit_code,
        lychee_body_file=body_file,
        sphinx_output_json=output_json,
        sphinx_source_url=source_url,
    )


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
    type=Choice(["off_topic", "resolved", "spam", "too_heated"]),
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

    if template and template_file:
        msg = "--template and --template-file are mutually exclusive."
        raise UsageError(msg)

    title_str, body, commit_msg_str = _render_pr_content(
        config=get_tool_config(ctx),
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

    write_output("PR body", content, output)


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
            config=get_tool_config(ctx),
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
        config,
        has_pat=has_pat,
        has_notifications_pat=has_notifications_pat,
        has_virustotal_key=has_virustotal_key,
        has_cloudflare_api_token=has_cloudflare_api_token,
        has_cloudflare_r2_keys=has_cloudflare_r2_keys,
        repo=repo,
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
    repo: str,
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
    if not number:
        missing.append("--number")

    if missing:
        raise UsageError(
            f"Missing required parameters: {', '.join(missing)}. "
            "These could not be auto-detected from the environment."
        )

    # Type narrowing for mypy.
    assert owner and author and number

    if is_sponsor(owner, author):
        if add_labels(repo, number, [label], is_pr=is_pr):
            echo(f"Added {label!r} label to {'PR' if is_pr else 'issue'} #{number}")
        else:
            raise ClickException("Failed to add sponsor label")
    else:
        echo(f"Author {author!r} is not a sponsor of {owner!r}")


@subcommand(short_help="Sync repository labels via labelmaker")
@repo_option()
@pass_context
def sync_labels(ctx: Context, repo: str) -> None:
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

    # Dump label files somewhere disposable: they are labelmaker inputs, not
    # repository content, so syncing labels leaves the working tree untouched.
    with tempfile.TemporaryDirectory(prefix="repomatic-labels-") as tmpdir:
        labels_dir = Path(tmpdir)
        result = run_init(output_dir=labels_dir, components=("labels",), config=config)
        for path in [*result.created, *result.updated]:
            logging.info(f"Exported: {path}")

        try:
            apply_labels(
                config, repo, is_awesome=Metadata().is_awesome, labels_dir=labels_dir
            )
        except RuntimeError as e:
            raise ClickException(str(e))

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
