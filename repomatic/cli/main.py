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

"""The `repomatic` command-line group and its shared plumbing.

Commands live in the per-section `cli_*` sibling modules and register onto
the group defined here; the bottom of this module imports them for that side
effect. What stays here is everything they share: the group, its help
sections, the reusable option declarations, the parameter types, and the
small helpers command bodies lean on.
"""

from __future__ import annotations

import copy
import logging
import os
import sys
from pathlib import Path

from click import Command
from click.shell_completion import CompletionItem
from click_extra import (
    STDOUT_SENTINEL,
    Choice,
    ClickException,
    Context,
    ParamType,
    Section,
    SortByOption,
    UsageError,
    dir_path,
    echo,
    file_path,
    get_tool_config,
    group,
    is_stdout,
    jobs_option,
    option,
    pass_context,
    style,
)

from ..cache import (
    CACHE_LIST_HEADER_DEFS,
    cache_dir as _cache_dir,
    cache_rows,
    clear_cache,
    clear_config_cache,
    clear_http_cache,
)
from ..config import (
    CONFIG_REFERENCE_HEADER_DEFS,
    Config,
)
from ..deps.dep_sources import (
    LINT_DEPS_HEADER_DEFS,
    build_release_readiness,
)
from ..deps.vulnerable_deps import (
    AUDIT_HEADER_DEFS,
)
from ..github.actions import (
    read_file_output,
)
from ..github.ci_status import (
    CI_STATUS_HEADER_DEFS,
    STABLE_GLYPH,
    UNSTABLE_GLYPH,
    workflow_files,
)
from ..github.job_timings import (
    JOB_TIMINGS_HEADER_DEFS,
)
from ..github.matrix import (
    JOB_STATE_KEY,
    OS_AXIS,
    PIVOT_CELL_SEPARATOR,
    PYTHON_VERSION_AXIS,
)
from ..github.pr_body import (
    build_pr_body,
    build_release_review_steps,
    generate_pr_metadata_block,
    generate_refresh_tip,
    render_commit_message,
    render_template,
    render_title,
    template_args,
    template_docs_url,
    template_stem,
)
from ..github.workflow_sync import run_workflow_lint
from ..humanize import format_file_size
from ..matrix_axes import (
    TEST_RUNNERS_FULL,
    TEST_RUNNERS_PR,
    python_version_sort_key,
)
from ..metadata.core import (
    METADATA_KEYS_HEADER_DEFS,
    Metadata,
)
from ..pyproject import (
    dependency_group_names,
    extra_names,
)
from ..registry import (
    ALL_COMPONENTS,
    DEFAULT_REPO,
    WORKFLOW_TARGET_ROOT,
    parse_component_entries,
    valid_file_ids,
)
from ..tooling.tool_registry import (
    TOOL_LIST_HEADER_DEFS,
)
from ..versions import BUMP_PARTS, strip_dev_suffix

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections import Counter
    from collections.abc import Callable, Mapping, Sequence
    from typing import Any

    # Click's own context type, distinct from the click_extra subclass imported
    # above: the `ParamType` overrides must accept any click context, or mypy
    # flags the narrowing as a Liskov violation.
    from click import Context as ClickContext, Parameter

    from ..github.matrix import Matrix


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
    type=Choice(BUMP_PARTS, case_sensitive=False),
    default=None,
    help="Version part passed to the bump-version template.",
)


def deprecated_alias(
    name: str, command: Command, *, replacement: str, removed_in: str
) -> Command:
    """Build a hidden alias for a renamed command, warning on each run.

    The alias shares the renamed command's parameters, so both spellings parse
    the same command line; it only swaps the callback for one that logs the
    deprecation first. The copy is shallow, which is safe here: a Click command
    holds no state between invocations.

    :param name: The old command name to keep answering to.
    :param command: The renamed command to delegate to.
    :param replacement: The new command name, spelled for the log line.
    :param removed_in: The release the alias will be removed in.
    :return: The alias, ready to register on the group.
    """
    alias = copy.copy(command)
    alias.name = name
    alias.hidden = True
    alias.short_help = f"Deprecated: use {replacement}"
    if command.callback is None:
        msg = f"Cannot alias {command.name!r} without a callback."
        raise ValueError(msg)
    original_callback = command.callback

    def warn_and_delegate(**kwargs: Any) -> None:
        logging.warning(
            f"repomatic {name} is deprecated and will be removed in {removed_in}."
            f" Use repomatic {replacement}."
        )
        original_callback(**kwargs)

    alias.callback = warn_and_delegate
    return alias


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
def repomatic() -> None:
    pass


# Every section lists its subcommands alphabetically, whatever order the
# modules register them in, matching the house ordering convention.
_section_ci = Section("CI & runners", is_sorted=True)
_section_github = Section("GitHub issues & PRs", is_sorted=True)
_section_lint = Section("Linting & checks", is_sorted=True)
_section_release = Section("Release & versioning", is_sorted=True)
_section_sample = Section("Forge sampling", is_sorted=True)
_section_setup = Section("Project setup", is_sorted=True)
_section_sync = Section("Sync", is_sorted=True)


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

    def get_metavar(self, param: Parameter, ctx: ClickContext) -> str:
        return "[COMPONENT[/FILE]]"

    def convert(
        self, value: Any, param: Parameter | None, ctx: ClickContext | None
    ) -> str:
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


_ci_status_sort = SortByOption(*CI_STATUS_HEADER_DEFS, default="workflow")


_job_timings_sort = SortByOption(*JOB_TIMINGS_HEADER_DEFS, default="median")


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


_audit_sort = SortByOption(*AUDIT_HEADER_DEFS, default="package")


@repomatic.group(short_help="Manage the download cache")
def cache() -> None:
    """Manage the local download cache.

    Binary tools and HTTP API responses are cached to avoid redundant
    downloads. This group provides subcommands to inspect, clean, and locate
    the cache.
    """


_cache_show_sort = SortByOption(*CACHE_LIST_HEADER_DEFS, default="name")


@cache.command(short_help="List cached entries", params=[_cache_show_sort])
@pass_context
def show(ctx: Context) -> None:
    """List all cached binaries and HTTP responses."""
    rows, total_size = cache_rows()
    if not rows:
        echo("Cache is empty.")
        ctx.exit(0)

    ctx.print_table(rows, CACHE_LIST_HEADER_DEFS)
    echo(f"\nTotal: {len(rows)} file(s), {format_file_size(total_size)}")


@cache.command(
    short_help="Remove cached entries",
    examples=(
        ("Drop every cached download", "repomatic cache clean"),
        ("Only one tool's entries", "repomatic cache clean --tool ruff"),
        ("Only one namespace", "repomatic cache clean --namespace pypi"),
        ("Only entries older than a week", "repomatic cache clean --max-age 7"),
    ),
)
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
def clean(
    ctx: Context,
    tool: str | None,
    namespace: str | None,
    max_age: int | None,
) -> None:
    """Remove cached binaries, tool configs and HTTP responses.

    Without options, removes everything. Use --tool to target a specific
    binary tool and its cached config, --namespace for a specific HTTP
    namespace, or --max-age for entries older than a threshold.
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
def path() -> None:
    """Print the absolute path to the cache directory.

    Useful for CI integration with actions/cache or similar tools.
    """
    echo(str(_cache_dir()))


_lint_deps_sort = SortByOption(*LINT_DEPS_HEADER_DEFS, default="package")


_run_sort = SortByOption(*TOOL_LIST_HEADER_DEFS, default="tool")


_metadata_sort = SortByOption(*METADATA_KEYS_HEADER_DEFS, default="key")


_show_config_sort = SortByOption(*CONFIG_REFERENCE_HEADER_DEFS, default="option")


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

JOB_COUNT_MARK = "×"
"""Introduces the job count of a cell standing for more than one job.

A cell collapses every job at its intersection, so a matrix varying on a third
axis renders five jobs exactly like one. The mark is the East Asian Ambiguous
U+00D7, like the `—` placeholder the grid already uses: a terminal drawing
ambiguous characters double-width misaligns both alike, and neither before the
other.
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

```{caution}
{data}`~repomatic.github.ci_status.UNSTABLE_GLYPH` is an emoji-presentation
sequence (U+2049 followed by the U+FE0F selector), and that is the one class
of glyph terminals measure differently: `wcwidth` counts it as two columns and
the table renderer pads to that, while a terminal allocating a single cell for
it (Apple Terminal does, painting the glyph over the space that follows) draws
the row a column short of its own separators. Carrying the mark CI stamps is
worth that, by decision: do not "fix" the alignment by dropping the selector
here, which would leave the grid and the job names spelling the mark
differently. `--no-emoji` sidesteps the whole question for a reader who wants
a square grid, and a terminal on Unicode 9 widths never sees it.
```
"""


def matrix_axis_keys(matrix: Matrix) -> tuple[str, ...]:
    """Every job key one matrix carries, sorted.

    Read off the solved job stream rather than off the declared axes, so a key
    only an `include` directive contributes is listed and one whose directive
    every `exclude` cancelled is not. That makes the listing exactly the set an
    axis can pivot on: anything outside it yields an empty grid.
    """
    return tuple(sorted({key for job in matrix.solve() for key in job}))


class LazyChoice(Choice):
    """A `Choice` whose accepted values are the project's, read at render time.

    A `Choice` declared at import can only carry what every repository has in
    common, which for most of these options is nothing: the dependency groups,
    the workflow files and the matrix axes are all the project's own. Resolving
    them when Click renders the help (or a completion list) is what lets the
    option name them, and keeps the lookup out of every other command's
    startup.

    Two rules make that safe to do mid-render:

    - **Nothing may fail or speak.** A traceback replaces the text the reader
      asked for, and a warning prepends noise to it, so `resolve` runs with
      logging off and any exception degrades to no values at all.
    - **No values means "cannot tell", not "nothing is valid".** An empty
      `Choice` refuses every value including the option's own default, so a
      project this type cannot read would take the whole command down with it.
      The type accepts anything instead, and renders the plain metavar its
      `name` gives rather than an empty `[]`.

    A subclass that knows a floor its project cannot go below declares it as
    `fallback`, and never reaches the pass-through above.
    """

    fallback: tuple[str, ...] = ()
    """Values to offer when `resolve` finds none. Empty means accept anything."""

    def __init__(self) -> None:
        # `Choice.__init__` assigns to `choices`, read-only here. Set the rest
        # of the state it owns directly rather than working around that.
        self.case_sensitive = True

    def resolve(self) -> tuple[str, ...]:
        """The values this type accepts, read from the project."""
        raise NotImplementedError

    @property
    def choices(self) -> tuple[str, ...]:  # type: ignore[override]
        """Resolve the values, degrading to `fallback` on any failure."""
        logging.disable(logging.CRITICAL)
        try:
            resolved = self.resolve()
        except Exception:  # noqa: BLE001
            # Deliberately not logged: this runs mid-render, so a report would
            # land in the middle of the help text it is apologising for.
            resolved = ()
        finally:
            logging.disable(logging.NOTSET)
        return resolved or self.fallback

    def convert(self, value: Any, param: Parameter | None, ctx: ClickContext | None):
        if not self.choices:
            return value
        return super().convert(value, param, ctx)

    def get_metavar(self, param: Parameter, ctx: ClickContext) -> str | None:
        # `None` falls back to Click's plain rendering of `name`, which beats
        # the empty `[]` an unresolved Choice would otherwise print.
        if not self.choices:
            return None
        return super().get_metavar(param, ctx)


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
    converts, so {func}`show_test_matrix` re-checks the axis against the
    matrix they did name, and refuses the few keys `pr` drops.
    """

    name = "axis"

    fallback = UNIVERSAL_AXIS_KEYS
    """Every matrix carries these three, so an unreadable one still pivots."""

    def resolve(self) -> tuple[str, ...]:
        return matrix_axis_keys(Metadata().test_matrix)


class WorkflowFile(LazyChoice):
    """A workflow file name, as `.github/workflows/` spells it.

    Both commands taking one read the current repository's own CI, so the
    checkout is the authority on what may be named. Every file counts, not
    just the ones {func}`~repomatic.github.ci_status.monitored_workflows`
    watches by default: a schedule-only workflow has runs worth reading too.
    """

    name = "workflow"

    def resolve(self) -> tuple[str, ...]:
        return workflow_files(Path(WORKFLOW_TARGET_ROOT))


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


def matrix_axis_sort_key(axis: str, matrix_name: str) -> Callable[[str], Any] | None:
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
        canonical = TEST_RUNNERS_FULL if matrix_name == "full" else TEST_RUNNERS_PR
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


def format_matrix_cell(
    cell: str, tally: Counter[str] | None, emoji: bool = True
) -> str:
    """Render one `show-test-matrix` cell from the jobs that landed in it.

    Every state is labelled on its own. A cell holds more than one when the
    matrix carries an axis beyond its two (a `click-version` variation, say):
    both jobs land on the same intersection, and a label looked up for the
    joined string would match nothing and leave that cell the only bare one in
    its column.

    A state carrying several jobs also states how many, since the grid gives a
    reader no other way to tell five stable jobs from one.

    :param cell: The cell {meth}`~repomatic.github.matrix.Matrix.pivot`
        rendered, returned untouched when no job occupies the intersection:
        the placeholder for an empty one is that method's to choose.
    :param tally: That intersection's job count per state, or `None` where the
        matrix puts no job at all.
    :param emoji: Label each state with its glyph rather than its bare word. A
        count is not decoration, and shows either way.
    """
    if tally is None:
        return cell
    labels = []
    for state, count in tally.items():
        label = state_label(state, emoji)
        labels.append(f"{label} {JOB_COUNT_MARK}{count}" if count > 1 else label)
    return PIVOT_CELL_SEPARATOR.join(labels)


@repomatic.command(
    name="lint-workflows",
    short_help="Lint downstream workflow caller files",
    section=_section_lint,
    examples=(
        ("Lint workflows in default location", "repomatic lint-workflows"),
        ("Lint with fatal mode (exit 1 on issues)", "repomatic lint-workflows --fatal"),
        (
            "Lint a custom directory",
            "repomatic lint-workflows --workflow-dir ./my-workflows",
        ),
    ),
)
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
def lint_workflows(ctx: Context, workflow_dir: Path, repo: str, fatal: bool) -> None:
    """Lint workflow files for common issues.

    Check thin caller workflows that delegate to the canonical reusable
    workflows in kdeldycke/repomatic. Use repomatic init workflows
    to generate or sync workflow files.

    \b
    - Standalone workflows missing the workflow_dispatch trigger.
    - Thin callers using @main instead of a version tag.
    - Thin callers with triggers that diverge from the canonical workflow
      (missing or extra entries).
    - Thin callers missing required secrets.
    """
    exit_code = run_workflow_lint(
        workflow_dir=workflow_dir,
        repo=repo,
        fatal=fatal,
    )
    ctx.exit(exit_code)


# Deprecated spelling of `lint-workflows`, kept invocable for one release cycle.
@repomatic.group(
    name="workflow",
    hidden=True,
    short_help="Deprecated: use lint-workflows",
)
def workflow() -> None:
    """Deprecated entry point for `repomatic lint-workflows`.

    `workflow lint` still runs the lint, printing a deprecation warning.
    It will be removed in `8.0.0`.
    """


workflow.add_command(
    deprecated_alias(
        "lint",
        lint_workflows,
        replacement="lint-workflows",
        removed_in="8.0.0",
    )
)


# Populate the group: each module registers its commands onto
# `repomatic` at import time, so they must be imported for their side
# effect, and only after the group and its sections exist above.
from . import (
    github,  # noqa: F401
    lint as _lint_commands,  # noqa: F401
    release,  # noqa: F401
    sample,  # noqa: F401
    setup,  # noqa: F401
    sync,  # noqa: F401
)
