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
"""Release and versioning commands of the `repomatic` CLI.

One module per help section: every command here registers onto the
`repomatic` group through the section object both import from
{mod}`repomatic.cli`, which pulls this module in at startup.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from click_extra import (
    STDOUT_SENTINEL,
    Choice,
    ClickException,
    Context,
    IntRange,
    UsageError,
    argument,
    dir_path,
    echo,
    file_path,
    get_tool_config,
    option,
    pass_context,
    prep_path,
)

from .attestation import pack_attestation
from .binaries_page import (
    render_binaries_csv,
    render_chart_section,
    update_binaries_csv,
    update_binaries_page,
)
from .binary import (
    BINARY_ASSET_SUFFIXES,
    pack_binary_assets,
)
from .changelog import (
    Changelog,
    resolved_changelog_path,
)
from .cli import (
    _section_release,
    exit_if_disabled,
    log_output_target,
    repomatic,
)
from .git_ops import commit_and_push_files, create_and_push_tag
from .github.pr import (
    carry_pr_branch_paths,
    close_open_prs_on_branch,
)
from .github.releases import (
    GitHubReleasesUnavailable,
    get_releases_with_assets,
)
from .metadata import (
    Metadata,
)
from .metadata_project import is_version_bump_allowed
from .plugin import ARCHIVE_NAME, pack_plugin
from .prepare_release import PrepareRelease
from .registry import (
    DEFAULT_REPO,
    WORKFLOW_TARGET_ROOT,
)
from .virustotal import (
    FREE_TIER_RATE_LIMIT,
    load_scan_records,
    poll_detection_stats,
    records_from_release_notes,
    records_from_results,
    scan_files,
    upsert_scan_records,
)

TYPE_CHECKING = False


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
def changelog(
    ctx: Context,
    source: Path | None,
    default_branch: str,
    changelog_path: Path,
) -> None:
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
            docs/binaries.md docs/assets/virustotal-scans.csv

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
    ctx: Context,
    changelog_path: Path | None,
    citation_path: Path,
    workflow_dir: Path,
    default_branch: str,
    update_workflows: bool | None,
    post_release: bool,
) -> None:
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
    help="CSV scan history to record detection snapshots in (requires --poll).",
)
@option(
    "--carry-from",
    "carry_from",
    metavar="BRANCH",
    default=None,
    help="Restore --records from this remote branch before scanning, so "
    "snapshots still pending in an open pull request are appended to instead "
    "of dropped. Name the branch the job publishes to. Ignored when the "
    "branch does not exist, which is every run that follows a merge.",
)
def scan_virustotal(
    tag: str,
    api_key: str,
    binaries_dir: Path,
    rate_limit: int,
    poll: bool,
    poll_timeout: int,
    records: Path | None,
    carry_from: str | None,
) -> None:
    """Upload release binaries to VirusTotal.

    Scans all .bin and .exe files in the given directory and uploads them to
    VirusTotal, seeding antivirus vendor databases with the signatures of the
    freshly built binaries.

    With --poll, waits for the analyses to complete and reports each binary's
    flagged / total verdict counts. With --records, the polled snapshots are
    merged into a CSV history, which sync-binaries renders into the binaries
    catalog page.

    \b
    Examples:
        repomatic scan-virustotal --tag v1.2.3 --binaries-dir ./binaries

    \b
        repomatic scan-virustotal --tag v1.2.3 --binaries-dir ./binaries \\
            --poll --records docs/assets/virustotal-scans.csv
    """
    if records and not poll:
        raise UsageError("--records requires --poll.")
    if carry_from and not records:
        raise UsageError("--carry-from requires --records.")
    if carry_from and records:
        carry_pr_branch_paths(carry_from, (records,))

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
            --records docs/assets/virustotal-scans.csv --backfill-records
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
