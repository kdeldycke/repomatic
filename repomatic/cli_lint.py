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
"""Linting and verification commands of the `repomatic` CLI.

One module per help section: every command here registers onto the
`repomatic` group through the section object both import from
{mod}`repomatic.cli`, which pulls this module in at startup.
"""

from __future__ import annotations

import logging
from pathlib import Path

from click_extra import (
    UNPROCESSED,
    Choice,
    ClickException,
    Context,
    UsageError,
    argument,
    dir_path,
    echo,
    file_path,
    get_tool_config,
    option,
    pass_context,
)

from .awesome_toc import fix_awesome_toc
from .binary import (
    NUITKA_BUILD_TARGETS,
    verify_binary_arch,
    verify_binary_floor,
)
from .changelog import (
    lint_changelog_dates,
    resolved_changelog_path,
)
from .cli import (
    _audit_sort,
    _lint_deps_sort,
    _run_sort,
    _section_lint,
    exit_if_disabled,
    has_cloudflare_api_token_option,
    has_notifications_pat_option,
    has_pat_option,
    has_virustotal_key_option,
    lockfile_option,
    log_output_target,
    output_format_option,
    repo_name_option,
    repo_slug_option,
    repomatic,
)
from .cloudflare import CloudflareError, run_cloudflare_pages
from .dep_policy import scan_policy
from .dep_sources import (
    LINT_DEPS_HEADER_DEFS,
    format_blocker_section,
    scan_project,
)
from .github.actions import (
    AnnotationLevel,
    emit_annotation,
    emit_report,
)
from .lint_repo import (
    LintContext,
    run_repo_lint,
)
from .metadata import (
    Metadata,
)
from .site_anchors import (
    DEFAULT_BUILD_DIR,
    DEFAULT_DOCS_DIR,
    check_anchors,
)
from .tool_registry import (
    TOOL_LIST_HEADER_DEFS,
    TOOL_REGISTRY,
)
from .tool_runner import (
    resolve_config_source,
    run_tool,
    verify_via_write_path,
)
from .vulnerable_deps import (
    AUDIT_HEADER_DEFS,
    AdvisorySource,
    collect_vulnerable_packages,
    fix_vulnerable_deps as _fix_vulnerable_deps,
    format_vulnerability_table,
)

TYPE_CHECKING = False


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
    default="",
    envvar="GITHUB_REPOSITORY",
    help=(
        "Repository in OWNER/NAME format. Enables the GitHub Advisory"
        " Database source. Defaults to $GITHUB_REPOSITORY."
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
    short_help="Check same-page doc links against the built site",
    section=_section_lint,
)
@option(
    "--docs-dir",
    type=dir_path(exists=True, readable=True, resolve_path=True),
    default=str(DEFAULT_DOCS_DIR),
    show_default=True,
    help="Root of the documentation sources to read links from.",
)
@option(
    "--build-dir",
    type=dir_path(exists=True, readable=True, resolve_path=True),
    default=str(DEFAULT_BUILD_DIR),
    show_default=True,
    help="Root of the rendered site to resolve those links against.",
)
@pass_context
def lint_anchors(ctx: Context, docs_dir: Path, build_dir: Path) -> None:
    """Check every same-page link resolves to an anchor the build produced.

    A literal (#fragment) link is copied into the HTML untouched, so a
    fragment naming a slug that was never generated ships as a link that
    looks fine and lands nowhere. Sphinx cannot catch it, having nothing to
    resolve, and a Markdown link checker has to guess the slug rather than
    read it, which is a different answer often enough to be useless.

    Only fragments written in the Markdown sources are checked, so a theme's
    own footnote backrefs and header permalinks are never mistaken for
    something an author asked for.

    Run it against a site that has just been built: an out-of-date build
    directory answers for the pages it was made from.

    \b
    Examples:
        # Defaults, straight after a Sphinx build
        repomatic lint-anchors

    \b
        # A tree built elsewhere
        repomatic lint-anchors --docs-dir ./docs --build-dir /tmp/site
    """
    report = check_anchors(docs_dir, build_dir)

    for source in report.unbuilt:
        echo(f"ℹ {source}: no built page found, skipped.")
    for finding in report.missing:
        emit_annotation(AnnotationLevel.ERROR, finding.message)
        echo(f"✗ {finding.message}")

    if report.missing:
        echo(
            f"{len(report.missing)} of {report.checked} same-page link(s) resolve"
            " to nothing."
        )
        ctx.exit(1)
    echo(f"✓ {report.checked} same-page link(s) resolve against {build_dir}.")


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
@has_cloudflare_api_token_option
@has_notifications_pat_option
@has_pat_option
@has_virustotal_key_option
@pass_context
def lint_repo(
    ctx: Context,
    repo_name: str | None,
    repo: str | None,
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
      - The pinned uv carries a checksum in the pinned astral-sh/setup-uv
        (warning).
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
      - CLOUDFLARE_API_TOKEN secret missing when site.deploy targets
        Cloudflare Pages (warning).
      - Legacy github.io URLs still redirect, for a project that moved its
        site to Cloudflare Pages (warning).
      - Committed _redirects files survive the Cloudflare Pages engine:
        no dropped rules, no silent budget abort (error).
      - wrangler.toml agrees with the declared Cloudflare project name and
        compatibility date (warning).

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

    # Everything the checkout can answer is derived inside the context
    # constructor; the command hands over only what it alone knows.
    exit_code = run_repo_lint(
        LintContext.from_project(
            get_tool_config(ctx),
            repo=repo or None,
            repo_name=repo_name,
            has_pat=has_pat,
            has_virustotal_key=has_virustotal_key,
            has_cloudflare_api_token=has_cloudflare_api_token,
            has_notifications_pat=has_notifications_pat,
        )
    )
    ctx.exit(exit_code)


@repomatic.command(
    short_help="Reconcile the Cloudflare Pages project", section=_section_lint
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

    \b
    Examples:
        # In CI: fail the job when the live project drifted
        repomatic cloudflare-pages --check

    \b
        # Write the declared compatibility date and placement back
        repomatic cloudflare-pages --apply

    \b
        # Rebuild from nothing: create the project if missing, then configure
        # it. An existing project is reconciled, not re-created.
        repomatic cloudflare-pages --create --project my-site

    \b
        # Serve the project at a custom domain, DNS record included
        repomatic cloudflare-pages --attach-domain example.com
    """
    modes = {
        "--apply": apply_,
        "--attach-domain": bool(attach_domain),
        "--check": check,
        "--create": create,
        "--dump": dump,
    }
    selected = [flag for flag, active in modes.items() if active]
    if len(selected) != 1:
        msg = f"Pick exactly one of {', '.join(modes)}. Got: {selected or 'none'}."
        raise UsageError(msg)

    config = get_tool_config(ctx)
    resolved = project or config.site_cloudflare_project or Metadata().repo_name
    if not resolved:
        msg = (
            "No project name: pass --project or set [tool.repomatic]"
            " site.cloudflare-project."
        )
        raise UsageError(msg)

    try:
        exit_code = run_cloudflare_pages(
            resolved or "",
            check=check,
            apply=apply_,
            dump=dump,
            create=create,
            attach_domain=attach_domain or "",
            compatibility_date=config.site_cloudflare_compatibility_date,
            placement=config.site_cloudflare_placement,
        )
    except CloudflareError as error:
        raise ClickException(str(error)) from error
    ctx.exit(exit_code)


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
