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
"""Site commands of the `repomatic` CLI: Cloudflare hosting and Sphinx docs.

{data}`repomatic.cli.main.COMMANDS` registers each command here, and files it
under its help section.
"""

from __future__ import annotations

import logging

from click_extra import (
    ClickException,
    UsageError,
    dir_path,
    echo,
    get_tool_config,
    option,
    pass_context,
)

from ..cloudflare.api import CloudflareError, resolve_project
from ..cloudflare.pages import PagesMode, run_cloudflare_pages
from ..cloudflare.r2 import R2Mode, run_cloudflare_r2, run_offload
from ..docs import DocsPhase, PhaseStatus, update_docs
from ..github.actions import emit_report
from .main import report_options, subcommand

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Mapping
    from enum import StrEnum
    from pathlib import Path
    from typing import TypeVar

    from click_extra import Context

    ModeT = TypeVar("ModeT", bound=StrEnum)


def _one_mode(given: Mapping[ModeT, bool]) -> ModeT:
    """Return the one mode the flags of a command select.

    :param given: Whether the flag of each mode is on, keyed by the mode,
        which is named after its flag.
    :raises UsageError: When no flag, or more than one, is on.
    """
    selected = [mode for mode, active in given.items() if active]
    if len(selected) != 1:
        flags = ", ".join(f"--{mode}" for mode in given)
        got = ", ".join(f"--{mode}" for mode in selected) or "none"
        msg = f"Pick exactly one of {flags}. Got: {got}."
        raise UsageError(msg)
    return selected[0]


def _project(ctx: Context, project: str | None) -> str:
    """Name the Pages project a command acts on.

    :raises UsageError: When neither `--project`, the configuration nor the
        repository names one.
    """
    resolved = resolve_project(get_tool_config(ctx), project)
    if not resolved:
        msg = (
            "No project name: pass --project or set [tool.repomatic]"
            " site.cloudflare-project."
        )
        raise UsageError(msg)
    return resolved


def _update_docs_help() -> str:
    """Write the help of `update-docs`, listing the phases from their registry.

    A `\\b` line keeps Click from flowing the numbered list into one paragraph.
    """
    phases = list(enumerate(DocsPhase, start=1))
    checked = " and ".join(str(index) for index, phase in phases if phase.checks)
    listing = "\n".join(f"{index}. {phase.description}" for index, phase in phases)
    check_note = (
        f"With --check, only phases {checked} run, in their own check modes:"
        " they report out-of-date content and write nothing. The update script"
        " must accept a --check flag to take part."
    )
    return "\n\n".join((
        "Regenerate the Sphinx API pages and the dynamic content of the docs.",
        "Runs these phases in order, and stops at the first one that fails:",
        f"\b\n{listing}",
        check_note,
        "The phases read their settings from [tool.repomatic.docs].",
    ))


@subcommand(
    short_help="Create, check or update the Cloudflare Pages project",
    examples=(
        (
            "In CI: fail the job when the live project drifted",
            "repomatic cloudflare-pages --check",
        ),
        (
            "Write the declared compatibility date and placement back",
            "repomatic cloudflare-pages --apply",
        ),
        (
            "Create the project if missing; an existing one is reconciled, not re-created",
            "repomatic cloudflare-pages --create --project my-site",
        ),
        (
            "Serve the project at a custom domain, DNS record included",
            "repomatic cloudflare-pages --attach-domain example.com",
        ),
    ),
)
@option(
    "--project",
    default=None,
    help=(
        "Cloudflare Pages project to reconcile. Defaults to [tool.repomatic]"
        " site.cloudflare-project, then to the repository name."
    ),
)
@option(
    "--check",
    is_flag=True,
    help="Diff the live project against the declared state; exit 1 on drift.",
)
@option(
    "--apply",
    "apply_",
    is_flag=True,
    help="Write the declared values back to the live project.",
)
@option(
    "--dump",
    is_flag=True,
    help="Print the live project state as JSON, secrets redacted.",
)
@option(
    "--create",
    is_flag=True,
    help=(
        "Create the Direct Upload project when missing, then apply the"
        " declared values: the rebuild-from-nothing verb. An existing"
        " project is reused and reconciled, so re-running is safe."
    ),
)
@option(
    "--attach-domain",
    metavar="DOMAIN",
    default=None,
    help=(
        "Serve the project at DOMAIN, creating the proxied CNAME it needs."
        " The API attaches a hostname without any DNS, unlike the dashboard,"
        " leaving the domain pending forever; this does both."
    ),
)
@pass_context
def cloudflare_pages(
    ctx: Context,
    project: str | None,
    check: bool,
    apply_: bool,
    dump: bool,
    create: bool,
    attach_domain: str | None,
) -> None:
    """Reconcile the Cloudflare Pages project against the declared state.

    A Direct Upload project's live settings (compatibility date, Smart
    Placement, build image, attached source) exist only server-side, where
    they drift with nothing watching. This command diffs them against the
    `[tool.repomatic] site.*` declarations, enforces them, and warns when the
    API token is within a month of its expiry, which Cloudflare itself never
    signals.

    Credentials come from CLOUDFLARE_API_TOKEN, falling back to the OAuth
    token a local `wrangler login` stored. The account is derived from the
    credential at run time, never declared beside it.
    """
    mode = _one_mode({
        PagesMode.APPLY: apply_,
        PagesMode.ATTACH_DOMAIN: bool(attach_domain),
        PagesMode.CHECK: check,
        PagesMode.CREATE: create,
        PagesMode.DUMP: dump,
    })
    config = get_tool_config(ctx)
    name = _project(ctx, project)
    try:
        exit_code = run_cloudflare_pages(
            name,
            mode,
            domain=attach_domain or "",
            compatibility_date=config.site_cloudflare_compatibility_date,
            placement=config.site_cloudflare_placement,
        )
    except CloudflareError as error:
        raise ClickException(str(error)) from error
    ctx.exit(exit_code)


@subcommand(
    short_help="Serve site files over the Pages size limit from Cloudflare R2",
    examples=(
        (
            "In CI, between the build and `wrangler pages deploy`",
            "repomatic cloudflare-r2 --offload ./docs/_build",
        ),
        (
            "Write the offload table to the step summary of a GitHub Actions run",
            (
                "repomatic cloudflare-r2 --offload ./docs/_build"
                ' --output "$GITHUB_STEP_SUMMARY"'
            ),
        ),
        (
            "Create the bucket and attach its domain, or converge an existing one",
            "repomatic cloudflare-r2 --create",
        ),
        (
            "Compare the live bucket against the declared state",
            "repomatic cloudflare-r2 --check",
        ),
    ),
)
@option(
    "--project",
    default=None,
    help=(
        "Cloudflare Pages project of the site, which resolves the account."
        " Defaults to [tool.repomatic] site.cloudflare-project, then to the"
        " repository name."
    ),
)
@option(
    "--offload",
    metavar="DIRECTORY",
    type=dir_path(exists=True, readable=True, resolve_path=True),
    default=None,
    help=(
        "Move every file over 25 MiB out of the built site in DIRECTORY, to the"
        " bucket, and redirect its path there in DIRECTORY/_redirects. Without"
        " a bucket, drop the file with an error. Exits 1 when it drops a file,"
        " with the rest of DIRECTORY still ready to deploy."
    ),
)
@option(
    "--create",
    is_flag=True,
    help=(
        "Create the bucket when missing, attach the domain with TLS 1.2 at"
        " least, and turn the r2.dev URL off. Re-running is safe."
    ),
)
@option(
    "--check",
    is_flag=True,
    help="Compare the live bucket against the declared state; exit 1 on drift.",
)
@report_options(
    "Write the --offload table, one row per file over the limit, to this file."
)
@pass_context
def cloudflare_r2(
    ctx: Context,
    project: str | None,
    offload: Path | None,
    create: bool,
    check: bool,
    output: Path | None,
    output_format: str,
) -> None:
    """Serve a static site's files over the Pages size limit from Cloudflare R2.

    Cloudflare Pages Direct Upload rejects any file over 25 MiB. --offload
    moves each such file from the built site to the bucket that [tool.repomatic]
    site.cloudflare-r2-bucket declares, and redirects its path to the copy.
    Without a bucket, or when a file cannot move, the file is dropped with an
    error annotation and the command exits 1. The rest of the tree stays ready
    to deploy, so everything else can still publish.

    Uploads use a key pair limited to the bucket, from
    CLOUDFLARE_R2_ACCESS_KEY_ID and CLOUDFLARE_R2_SECRET_ACCESS_KEY. --create
    and --check manage the bucket through the REST API, with
    CLOUDFLARE_API_TOKEN or a local `wrangler login`. The account resolves
    from the credential, never from a declared identifier.
    """
    mode = _one_mode({
        R2Mode.CHECK: check,
        R2Mode.CREATE: create,
        R2Mode.OFFLOAD: offload is not None,
    })
    config = get_tool_config(ctx)
    bucket = config.site_cloudflare_r2_bucket
    domain = config.site_cloudflare_r2_domain

    if offload is not None:
        report = run_offload(
            offload, bucket=bucket, domain=domain, project=_project(ctx, project)
        )
        emit_report(report.markdown(), output, output_format, key="offload_report")
        ctx.exit(report.exit_code)

    if not bucket:
        msg = (
            "No bucket declared: set [tool.repomatic] site.cloudflare-r2-bucket"
            " and site.cloudflare-r2-domain."
        )
        raise UsageError(msg)
    name = _project(ctx, project)
    try:
        exit_code = run_cloudflare_r2(name, bucket, domain, mode)
    except CloudflareError as error:
        raise ClickException(str(error)) from error
    ctx.exit(exit_code)


@subcommand(
    short_help="Regenerate Sphinx API docs and dynamic content",
    help=_update_docs_help(),
)
@option(
    "--check",
    is_flag=True,
    default=False,
    help="Report out-of-date self-updating content and exit non-zero, without "
    "writing anything. For CI drift detection.",
)
@pass_context
def update_docs_cmd(ctx: Context, check: bool) -> None:
    """Regenerate the Sphinx API pages and the dynamic content of the docs.

    The command's help comes from _update_docs_help(), which lists the phases.
    """
    try:
        results = update_docs(get_tool_config(ctx), check=check)
    except ValueError as error:
        raise ClickException(str(error)) from error

    drifted: list[str] = []
    for result in results:
        if result.status is PhaseStatus.FAILED:
            raise ClickException(result.message)
        if result.status is PhaseStatus.DRIFT:
            drifted.append(result.message)
        elif result.status is PhaseStatus.SKIPPED:
            logging.info(result.message)
        elif result.message:
            echo(result.message)

    if not check or not results:
        return
    if drifted:
        msg = (
            f"Documentation is out of date ({', '.join(drifted)})."
            " Run `repomatic update-docs`."
        )
        raise ClickException(msg)
    echo("Documentation is up to date.")
