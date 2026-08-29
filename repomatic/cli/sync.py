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
"""Sync commands of the `repomatic` CLI.

One module per help section: every command here registers onto the
`repomatic` group through the section object both import from
{mod}`repomatic.cli.main`, which pulls this module in at startup.
"""

from __future__ import annotations

import logging
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from click_extra import (
    Choice,
    ClickException,
    Context,
    UsageError,
    argument,
    dir_path,
    echo,
    file_path,
    get_tool_config,
    is_stdout,
    option,
    pass_context,
    prep_path,
)

from ..changelog import (
    load_changelog_repo,
)
from ..github.actions import (
    AnnotationLevel,
    emit_annotation,
    emit_report,
)
from ..github.dev_release import (
    cleanup_dev_releases as _cleanup_dev_releases,
    sync_dev_release as _sync_dev_release,
)
from ..github.release_sync import (
    render_sync_report as _render_sync_report,
    sync_github_releases as _sync_github_releases,
)
from ..github.releases import (
    owner_repo,
)
from ..gitignore import build_gitignore, orphaned_rules
from ..init_project import run_init
from ..labels import (
    apply_labels,
)
from ..mailmap import Mailmap, remove_header
from ..metadata.core import (
    Metadata,
)
from ..sync_ops import (
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
from ..tooling.tool_registry import (
    generated_header,
)
from .main import (
    _section_sync,
    dry_run_option,
    exit_if_disabled,
    lock_held_back_option,
    lockfile_option,
    log_output_target,
    output_format_option,
    repo_slug_option,
    repomatic,
    report_output_option,
    sync_held_back_option,
    sync_release_notes_option,
    sync_table_option,
    version_report_output_option,
)

TYPE_CHECKING = False


@repomatic.command(
    short_help="Bump SHA-pinned GitHub Actions to their latest release",
    section=_section_sync,
    examples=(
        (
            "Bump every SHA-pinned action past the cooldown",
            "repomatic sync-action-pins",
        ),
    ),
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
    examples=(
        ("Swap whatever is ready and show changes", "repomatic sync-dep-sources"),
        (
            "CI: write markdown report as a GitHub Actions step output",
            'repomatic sync-dep-sources --no-table --release-notes --output "$GITHUB_OUTPUT" --output-format github-actions',
        ),
    ),
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
    examples=(
        ("Update everything enabled", "repomatic sync-deps"),
        (
            "Only the lockfile and action pins",
            "repomatic sync-deps sync-uv-lock sync-action-pins",
        ),
        ("Preview without writing", "repomatic sync-deps --dry-run"),
    ),
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
    examples=(
        (
            "Dry run to preview what would be synced",
            "repomatic sync-dev-release --dry-run",
        ),
        ("Create or update the dev pre-release", "repomatic sync-dev-release --live"),
        (
            "Create or update with asset upload",
            "repomatic sync-dev-release --live --upload-assets release_assets/",
        ),
        (
            "Delete the dev pre-release, as a real release does",
            "repomatic sync-dev-release --live --delete",
        ),
    ),
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
    examples=(
        (
            "Dry run to preview what would be updated",
            "repomatic sync-github-releases --dry-run",
        ),
        ("Update drifted release notes", "repomatic sync-github-releases --live"),
    ),
)
@dry_run_option
def sync_github_releases(dry_run: bool) -> None:
    """Sync GitHub release notes from changelog.md.

    Compares each GitHub release body against the corresponding
    changelog.md section and updates any that have drifted.
    """
    loaded = load_changelog_repo(get_tool_config())
    if loaded is None:
        return
    changelog_path, repo_url = loaded

    result = _sync_github_releases(repo_url, changelog_path, dry_run)
    echo(_render_sync_report(result))


@repomatic.command(
    short_help="Sync .gitignore from gitignore.io templates",
    section=_section_sync,
    examples=(
        (
            "Generate .gitignore using config from pyproject.toml",
            "repomatic sync-gitignore",
        ),
        (
            "Write to custom location",
            "repomatic sync-gitignore --output ./custom/.gitignore",
        ),
        ("Preview on stdout", "repomatic sync-gitignore --output -"),
    ),
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
    if not drop_orphans and not is_stdout(output_path) and output_path.is_file():
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
def sync_mailmap(
    ctx: Context,
    source: Path,
    create_if_missing: bool,
    destination_mailmap: Path | None,
) -> None:
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
    examples=(
        ("Bump every registry tool past the cooldown", "repomatic sync-tool-versions"),
        (
            "CI: write a markdown report as a GitHub Actions step output",
            'repomatic sync-tool-versions --output "$GITHUB_OUTPUT" --output-format github-actions',
        ),
    ),
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
    examples=(
        ("Upgrade and show changes", "repomatic sync-uv-lock"),
        ("With release notes", "repomatic sync-uv-lock --release-notes"),
        (
            "Render the report as a GitHub-flavored table",
            "repomatic --table-format github sync-uv-lock",
        ),
        ("Render the report as JSON", "repomatic --table-format json sync-uv-lock"),
        (
            "CI: write markdown report as a GitHub Actions step output",
            'repomatic sync-uv-lock --no-table --release-notes --output "$GITHUB_OUTPUT" --output-format github-actions',
        ),
    ),
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
    examples=(
        (
            "Bump every workflow version literal past the cooldown",
            "repomatic sync-workflow-pins",
        ),
    ),
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
