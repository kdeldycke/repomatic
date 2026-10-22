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
"""Tool commands of the `repomatic` CLI: the pinned tool runner and its cache.

{data}`repomatic.cli.main.COMMANDS` registers each command here, and files it
under its help section.
"""

from __future__ import annotations

import logging
from pathlib import Path

from click_extra import (
    UNPROCESSED,
    Choice,
    ClickException,
    SortByOption,
    UsageError,
    argument,
    echo,
    group,
    option,
    pass_context,
    resolve_jobs,
)

from ..cache import (
    CACHE_LIST_HEADER_DEFS,
    cache_dir,
    cache_rows,
    clean as clean_cache,
)
from ..http import FetchError
from ..humanize import format_file_size
from ..tooling import registry as tool_registry
from ..tooling.checksums import (
    AttestationError,
    checksum_targets,
    update_registry_checksums,
)
from ..tooling.registry import (
    TOOL_LIST_HEADER_DEFS,
    TOOL_REGISTRY,
    ToolInstallError,
)
from ..tooling.runner import (
    ConfigSource,
    run_tool,
    verify_via_write_path,
)
from .main import subcommand

TYPE_CHECKING = False
if TYPE_CHECKING:
    from click_extra import Context


@subcommand(
    short_help="Run a registered external tool, pinned and checksum-verified",
    context_settings={"ignore_unknown_options": True},
    params=[SortByOption(*TOOL_LIST_HEADER_DEFS, default="tool")],
)
@argument(
    "tool_name",
    required=False,
    default=None,
    type=Choice(sorted(TOOL_REGISTRY)),
    # The whole roster spelled into the usage line wraps it over several lines,
    # and Click names the offending parameter by its metavar, so a bare Choice
    # prints the roster twice in one error. `--list` is the roster.
    metavar="[TOOL]",
)
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
def run(
    ctx: Context,
    tool_name: str | None,
    extra_args: tuple[str, ...],
    list_tools: bool,
    verify: bool,
    tool_version: str | None,
    checksum: str | None,
    skip_checksum: bool,
    no_cache: bool,
) -> None:
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
        repomatic run zizmor -- --persona auditor .

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
            (spec.name, spec.version, ConfigSource.find(spec).label)
            for spec in TOOL_REGISTRY.values()
        ]
        ctx.print_table(rows, TOOL_LIST_HEADER_DEFS)
        ctx.exit(0)

    if tool_name is None:
        raise UsageError("Missing argument 'TOOL'. Use --list to see available tools.")

    try:
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
        else:
            exit_code = run_tool(
                tool_name,
                extra_args=extra_args,
                version=tool_version,
                checksum=checksum,
                skip_checksum=skip_checksum,
                no_cache=no_cache,
            )
    except ToolInstallError as exc:
        raise ClickException(str(exc)) from exc
    ctx.exit(exit_code)


# Built with no option of its own, like every `subcommand`: the root options
# come from the `repomatic` group.
@group(params=[], short_help="Manage the download cache")
def cache() -> None:
    """Manage the local download cache.

    Binary tools, HTTP API responses and generated tool configs are cached to
    avoid redundant downloads. This group provides subcommands to inspect,
    clean, and locate the cache.
    """


@cache.command(
    short_help="List cached entries",
    params=[SortByOption(*CACHE_LIST_HEADER_DEFS, default="name")],
)
@pass_context
def show(ctx: Context) -> None:
    """List all cached binaries, HTTP responses and tool configs."""
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
    help="Only remove HTTP entries in this namespace, like pypi or"
    " github-release-tags.",
)
@option(
    "--max-age",
    type=int,
    default=None,
    help="Only remove entries older than this many days.",
)
def clean(
    tool: str | None,
    namespace: str | None,
    max_age: int | None,
) -> None:
    """Remove cached binaries, tool configs and HTTP responses.

    Without options, removes everything. Use --tool to target a specific
    binary tool and its cached config, --namespace for a specific HTTP
    namespace, or --max-age for entries older than a threshold.
    """
    deleted, freed = clean_cache(tool=tool, namespace=namespace, max_age_days=max_age)
    if deleted:
        echo(f"Removed {deleted} file(s), freed {format_file_size(freed)}.")
    else:
        echo("Nothing to remove.")


@cache.command(short_help="Print the cache directory path")
def path() -> None:
    """Print the absolute path to the cache directory.

    Useful for CI integration with actions/cache or similar tools.
    """
    echo(str(cache_dir()))


@subcommand(
    short_help="Recompute SHA-256 checksums for the binary tool registry",
    examples=(
        ("Recompute every checksum in the tool registry", "repomatic update-checksums"),
    ),
)
@pass_context
def update_checksums(ctx: Context) -> None:
    """Recompute SHA-256 checksums for the binary tool registry.

    Downloads each binary-distributed tool in the `repomatic run` registry at
    its pinned version, computes the SHA-256, and rewrites any stale hash (and
    its version stamp) in tooling/registry.py. A tool whose upstream attests
    its releases has each download verified against the attestation first,
    with the pinned gh: one that does not verify stops the run, and nothing
    is rewritten.

    \b
    A repair path for a manual version edit: sync-tool-versions already
    refreshes checksums when it bumps a version.
    """
    registry_path = Path(tool_registry.__file__)
    jobs = resolve_jobs(ctx, len(checksum_targets()), serial_at_debug=True)
    try:
        updated = update_registry_checksums(registry_path, jobs=jobs)
    except FetchError as exc:
        raise ClickException(f"Cannot download a release artifact: {exc}") from exc
    except AttestationError as exc:
        raise ClickException(str(exc)) from exc

    for url, old_hash, new_hash in updated:
        echo(f"Updated: {url}")
        echo(f"  Old: {old_hash}")
        echo(f"  New: {new_hash}")
    if not updated:
        logging.info("All checksums are up to date.")
