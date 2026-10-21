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

The commands live in the domain modules beside this one. The group imports none
of them at startup: {data}`COMMANDS` names each command by the import path of
its command object, so a run imports this module and the one module that holds
the command it invokes. The help screen is the exception, since it lists the
summary of every command.

What stays here is what the domain modules share: the group, its help sections,
the options more than one module declares, the base parameter type and the
helpers command bodies call.

```{note}
A run that reads a configuration file still imports every command module.
click-extra builds its parameter template by walking the parameters of every
command, which loads each lazy one, even though `included_params=()` leaves the
template empty. So today the table speeds up only a run that reads no
configuration, like `repomatic --version`.
```

```{todo}
Remove the note above once click-extra skips that walk for an empty
`included_params`. The table then keeps every other command module unloaded.
```

```{caution}
Nuitka compiles only the modules it sees imported, and an import path is a
string. The `[tool.nuitka]` table of `pyproject.toml` includes the whole
`repomatic.cli` package for that reason, so the binary ships every command
module this table names.
```
"""

from __future__ import annotations

import logging
import os
import sys
from functools import partial

from click_extra import (
    Choice,
    LazySubcommand,
    Section,
    UsageError,
    command,
    file_path,
    get_tool_config,
    is_stdout,
    jobs_option,
    lazy_group,
    option,
    pass_context,
)

from ..changelog import resolved_changelog_path
from ..config import Config, publish_config
from ..github.outputs import write_text_output
from ..metadata.core import Metadata

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from pathlib import Path
    from typing import Any, TypeVar

    # Click's own context type, distinct from the click_extra subclass: the
    # `ParamType` overrides and the parameter callbacks must accept any click
    # context, or mypy flags the narrowing as a Liskov violation.
    from click import Context as ClickContext, Parameter
    from click_extra import Command, Context

    FC = TypeVar("FC", bound="Callable[..., Any]")


# Every section lists its commands alphabetically, whatever order the table
# below declares them in, matching the house ordering convention.
_section_setup = Section("Project setup", is_sorted=True)
_section_lint = Section("Linting & checks", is_sorted=True)
_section_sync = Section("Sync", is_sorted=True)
_section_release = Section("Release & versioning", is_sorted=True)
_section_github = Section("GitHub issues & PRs", is_sorted=True)
_section_ci = Section("CI & runners", is_sorted=True)
_section_site = Section("Site & docs", is_sorted=True)
_section_sample = Section("Forge sampling", is_sorted=True)
_section_tools = Section("Tools", is_sorted=True)


COMMANDS: dict[str, LazySubcommand] = {
    # Project setup.
    "init": LazySubcommand("repomatic.cli.repo.init", section=_section_setup),
    "list-skills": LazySubcommand(
        "repomatic.cli.repo.list_skills", section=_section_setup
    ),
    "show-config": LazySubcommand(
        "repomatic.cli.repo.show_config", section=_section_setup
    ),
    "show-metadata": LazySubcommand(
        "repomatic.cli.repo.show_metadata", section=_section_setup
    ),
    "show-test-matrix": LazySubcommand(
        "repomatic.cli.ci.show_test_matrix", section=_section_setup
    ),
    "update-dep-graph": LazySubcommand(
        "repomatic.cli.deps.update_dep_graph", section=_section_setup
    ),
    # Linting & checks.
    "audit": LazySubcommand("repomatic.cli.deps.audit", section=_section_lint),
    "fix-awesome-toc": LazySubcommand(
        "repomatic.cli.repo.fix_awesome_toc_cmd", section=_section_lint
    ),
    "format-images": LazySubcommand(
        "repomatic.cli.repo.format_images", section=_section_lint
    ),
    "lint-changelog": LazySubcommand(
        "repomatic.cli.release.lint_changelog", section=_section_lint
    ),
    "lint-deps": LazySubcommand("repomatic.cli.deps.lint_deps", section=_section_lint),
    "lint-repo": LazySubcommand("repomatic.cli.repo.lint_repo", section=_section_lint),
    # Sync.
    "sync-action-pins": LazySubcommand(
        "repomatic.cli.deps.sync_action_pins", section=_section_sync
    ),
    "sync-binaries": LazySubcommand(
        "repomatic.cli.release.sync_binaries", section=_section_sync
    ),
    "sync-bumpversion": LazySubcommand(
        "repomatic.cli.release.sync_bumpversion", section=_section_sync
    ),
    "sync-dep-sources": LazySubcommand(
        "repomatic.cli.deps.sync_dep_sources", section=_section_sync
    ),
    "sync-deps": LazySubcommand("repomatic.cli.deps.sync_deps", section=_section_sync),
    "sync-dev-release": LazySubcommand(
        "repomatic.cli.release.sync_dev_release_cmd", section=_section_sync
    ),
    "sync-github-releases": LazySubcommand(
        "repomatic.cli.release.sync_github_releases_cmd", section=_section_sync
    ),
    "sync-gitignore": LazySubcommand(
        "repomatic.cli.repo.sync_gitignore", section=_section_sync
    ),
    "sync-labels": LazySubcommand(
        "repomatic.cli.github.sync_labels", section=_section_sync
    ),
    "sync-mailmap": LazySubcommand(
        "repomatic.cli.repo.sync_mailmap", section=_section_sync
    ),
    "sync-runner-images": LazySubcommand(
        "repomatic.cli.ci.sync_runner_images", section=_section_sync
    ),
    "sync-tool-versions": LazySubcommand(
        "repomatic.cli.deps.sync_tool_versions", section=_section_sync
    ),
    "sync-uv-lock": LazySubcommand(
        "repomatic.cli.deps.sync_uv_lock", section=_section_sync
    ),
    "sync-workflow-pins": LazySubcommand(
        "repomatic.cli.deps.sync_workflow_pins", section=_section_sync
    ),
    # Release & versioning.
    "changelog": LazySubcommand(
        "repomatic.cli.release.changelog", section=_section_release
    ),
    "close-stale-bump-pr": LazySubcommand(
        "repomatic.cli.github.close_stale_bump_pr", section=_section_release
    ),
    "git-tag": LazySubcommand(
        "repomatic.cli.release.git_tag", section=_section_release
    ),
    "pack-attestation": LazySubcommand(
        "repomatic.cli.release.pack_attestation_cmd", section=_section_release
    ),
    "pack-binaries": LazySubcommand(
        "repomatic.cli.release.pack_binaries", section=_section_release
    ),
    "pack-plugin": LazySubcommand(
        "repomatic.cli.release.pack_plugin_cmd", section=_section_release
    ),
    "prepare-release": LazySubcommand(
        "repomatic.cli.release.prepare_release", section=_section_release
    ),
    "scan-virustotal": LazySubcommand(
        "repomatic.cli.release.scan_virustotal", section=_section_release
    ),
    "verify-binary": LazySubcommand(
        "repomatic.cli.release.verify_binary", section=_section_release
    ),
    # GitHub issues & PRs.
    "apply-labels": LazySubcommand(
        "repomatic.cli.github.apply_labels_cmd", section=_section_github
    ),
    "broken-links": LazySubcommand(
        "repomatic.cli.github.broken_links", section=_section_github
    ),
    "lock-threads": LazySubcommand(
        "repomatic.cli.github.lock_threads", section=_section_github
    ),
    "pr-body": LazySubcommand("repomatic.cli.github.pr_body", section=_section_github),
    "pr-sync": LazySubcommand("repomatic.cli.github.pr_sync", section=_section_github),
    "setup-guide": LazySubcommand(
        "repomatic.cli.github.setup_guide", section=_section_github
    ),
    "sponsor-label": LazySubcommand(
        "repomatic.cli.github.sponsor_label", section=_section_github
    ),
    "unsubscribe-threads": LazySubcommand(
        "repomatic.cli.github.unsubscribe_threads_cmd", section=_section_github
    ),
    # CI & runners.
    "cancel-runs": LazySubcommand("repomatic.cli.ci.cancel_runs", section=_section_ci),
    "ci-status": LazySubcommand("repomatic.cli.ci.ci_status", section=_section_ci),
    "job-timings": LazySubcommand("repomatic.cli.ci.job_timings", section=_section_ci),
    # Site & docs.
    "cloudflare-pages": LazySubcommand(
        "repomatic.cli.site.cloudflare_pages", section=_section_site
    ),
    "cloudflare-r2": LazySubcommand(
        "repomatic.cli.site.cloudflare_r2", section=_section_site
    ),
    "update-docs": LazySubcommand(
        "repomatic.cli.site.update_docs_cmd", section=_section_site
    ),
    # Forge sampling.
    "sample-metrics": LazySubcommand(
        "repomatic.cli.repo.sample_metrics", section=_section_sample
    ),
    # Tools.
    "cache": LazySubcommand("repomatic.cli.tools.cache", section=_section_tools),
    "run": LazySubcommand("repomatic.cli.tools.run", section=_section_tools),
    "update-checksums": LazySubcommand(
        "repomatic.cli.tools.update_checksums", section=_section_tools
    ),
}
"""Every `repomatic` command, keyed by name, with the import path of its object.

Grouped by help section, in the order the help screen lists the sections: the
group registers each section the first time an entry here names it.
"""


# included_params=() disables merge_default_map: every [tool.repomatic] key is
# config-only (not a CLI parameter), so merging the keys would collide with
# command names (`setup-guide` is both a config key and a command). Config
# access goes through config_schema and get_tool_config() alone.
@lazy_group(
    config_schema=Config,
    schema_strict=False,
    included_params=(),
    lazy_subcommands=COMMANDS,
)
@jobs_option()
@pass_context
def repomatic(ctx: Context) -> None:
    # Library helpers read settings through `load_repomatic_config()`. Publishing
    # the config the CLI resolved makes them honor `--config` and the upward
    # search too, so one command sees one configuration.
    publish_config(get_tool_config(ctx))
    ctx.call_on_close(partial(publish_config, None))


def subcommand(
    *, params: Iterable[Parameter] = (), **kwargs: Any
) -> Callable[[Callable[..., Any]], Command]:
    """Declare a `repomatic` command, carrying only the options it declares.

    The `command` decorator of click-extra adds the options of a root command
    (`--config`, `--verbosity`, `--version` and the rest) to every command it
    builds. A command of the group gets those from `repomatic` instead.

    :param params: Options built ahead of the command, like a `--sort-by`
        option. Click extends the list it gets, so each call copies it.
    :param kwargs: Passed on to the `command` decorator of click-extra.
    """
    return command(params=list(params), **kwargs)


def exit_if_disabled(ctx: Context, key: str) -> None:
    """Exit successfully when a `[tool.repomatic]` feature flag is off.

    The shared guard of every sync command: a disabled feature is a normal,
    configured state, so the command logs the flag and exits `0` instead of
    failing the workflow that invoked it.

    Reads the flag through {meth}`~repomatic.config.Config.is_enabled`, so a
    call site names the key once, as the user writes it.

    :param ctx: The Click context to read the configuration from and exit
        through.
    :param key: The dotted `[tool.repomatic]` key, like `labels.sync`.
    :raises ValueError: When *key* names no setting.
    """
    if not get_tool_config(ctx).is_enabled(key):
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


def write_output(subject: str, content: str, output: Path) -> None:
    """Write *content* to an `--output` path, then close the file.

    Logs the destination first, through {func}`log_output_target`, then hands
    the write to {func}`~repomatic.github.outputs.write_text_output`, the one
    function that owns how an `--output` stream is opened and closed.

    :param subject: What is being written, as a noun phrase, for the log line.
    :param content: The text to write. A trailing newline is added.
    :param output: The destination path, or `-` for stdout.
    """
    log_output_target(subject, output)
    write_text_output(content, output)


# ---------------------------------------------------------------------------
# Shared options.
#
# An option that more than one domain module declares is built here, so its
# flag name, type, default and help text cannot drift between the commands that
# expose it. An option of a single module lives in that module.
# ---------------------------------------------------------------------------


def output_option(help_text: str, *, default: str | None = None) -> Callable[[FC], FC]:
    """`-o`/`--output`: the file a command writes, or `-` for stdout.

    :param help_text: What the command writes there.
    :param default: The destination when the option is absent. `None` lets the
        command decide, from its configuration or by writing nothing.
    """
    return option(
        "-o",
        "--output",
        type=file_path(writable=True, resolve_path=True, allow_dash=True),
        default=default,
        help=help_text,
    )


_output_format_option = option(
    "--output-format",
    type=Choice(["markdown", "github-actions"]),
    default="markdown",
    help=(
        "Format of the --output file. github-actions writes step outputs, for"
        " $GITHUB_OUTPUT."
    ),
)


def report_options(help_text: str, *, default: str | None = None) -> Callable[[FC], FC]:
    """`--output` with the `--output-format` it is written in.

    :param help_text: What the command writes to `--output`.
    :param default: The `--output` destination when the option is absent.
    """

    def decorator(func: FC) -> FC:
        return output_option(help_text, default=default)(_output_format_option(func))

    return decorator


def dry_run_option(*, default: bool) -> Callable[[FC], FC]:
    """`--dry-run/--live`.

    :param default: `True` for a command that changes nothing unless asked to,
        `False` for one that CI runs to make the change.
    """
    return option(
        "--dry-run/--live",
        default=default,
        help="Report what would be done without making changes.",
    )


def _resolve_repo(ctx: ClickContext, param: Parameter, value: str | None) -> str:
    """Fall back to the repository of the current checkout.

    :raises click.UsageError: Neither `--repo`, `$GITHUB_REPOSITORY` nor the checkout
        names a repository.
    """
    repo = value or Metadata().repo_slug
    if not repo:
        msg = (
            "No repository found: pass --repo, set $GITHUB_REPOSITORY, or run"
            " from a clone with a GitHub remote."
        )
        raise UsageError(msg, ctx=ctx)
    return repo


def repo_option(
    help_text: str = "Repository in owner/repo format.", *, detect: bool = True
) -> Callable[[FC], FC]:
    """`--repo`, read from `$GITHUB_REPOSITORY` when absent.

    :param help_text: What the command uses the repository for.
    :param detect: Also fall back to the repository of the current checkout,
        and refuse to run when nothing names one. Off for a command that also
        works without a repository.
    """
    if not detect:
        return option(
            "--repo",
            envvar="GITHUB_REPOSITORY",
            default=None,
            help=f"{help_text} Defaults to $GITHUB_REPOSITORY.",
        )
    return option(
        "--repo",
        envvar="GITHUB_REPOSITORY",
        default=None,
        callback=_resolve_repo,
        help=(
            f"{help_text} Defaults to $GITHUB_REPOSITORY, then to the GitHub"
            " remote of the current clone."
        ),
    )


repo_name_option = option(
    "--repo-name",
    default=None,
    help="Repository name. Defaults to the name component of $GITHUB_REPOSITORY.",
)


def _resolve_changelog(ctx: ClickContext, param: Parameter, value: Path | None) -> Path:
    """Fall back to the `[tool.repomatic] changelog.location` path."""
    if value is not None:
        return value
    return resolved_changelog_path(get_tool_config(ctx))


changelog_option = option(
    "--changelog",
    "changelog_path",
    type=file_path(exists=True, readable=True, resolve_path=True),
    default=None,
    callback=_resolve_changelog,
    help="Path to the changelog file. Defaults to [tool.repomatic] changelog.location.",
)

has_cloudflare_api_token_option = option(
    "--has-cloudflare-api-token",
    is_flag=True,
    default=False,
    envvar="HAS_CLOUDFLARE_API_TOKEN",
    help="Whether CLOUDFLARE_API_TOKEN is configured.",
)
has_cloudflare_r2_keys_option = option(
    "--has-cloudflare-r2-keys",
    is_flag=True,
    default=False,
    envvar="HAS_CLOUDFLARE_R2_KEYS",
    help=(
        "Whether CLOUDFLARE_R2_ACCESS_KEY_ID and CLOUDFLARE_R2_SECRET_ACCESS_KEY"
        " are both configured."
    ),
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

    Every access resolves again, on purpose: a type instance lives as long as
    its command, which outlives the working directory a run started in.
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
