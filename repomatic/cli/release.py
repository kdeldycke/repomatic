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
"""Release commands of the `repomatic` CLI: changelog, freeze, tag and assets.

{data}`repomatic.cli.main.COMMANDS` registers each command here, and files it
under its help section.
"""

from __future__ import annotations

import logging
import sys
import tempfile
from pathlib import Path

from click_extra import (
    Choice,
    ClickException,
    IntRange,
    UsageError,
    argument,
    dir_path,
    echo,
    file_path,
    get_tool_config,
    is_stdout,
    option,
    pass_context,
)

from ..changelog import Changelog, load_changelog_repo
from ..claude_plugin import ARCHIVE_NAME, pack_plugin
from ..file_ops import write_if_changed
from ..git_ops import GitError, create_and_push_tag
from ..github.dev_release import (
    cleanup_dev_releases,
    sync_dev_release,
)
from ..github.pr import carry_pr_branch_paths
from ..github.release_sync import (
    render_sync_report,
    sync_github_releases,
)
from ..github.releases import (
    GitHubReleasesUnavailable,
    get_releases_with_assets,
)
from ..github.urls import parse_github_repo
from ..github.workflow_files import WORKFLOW_DIR
from ..init.tool_config import init_config
from ..lint_changelog import lint_changelog_dates
from ..metadata.core import Metadata
from ..pyproject import get_current_version
from ..release.attestation import pack_attestation
from ..release.binaries_page import update_catalog
from ..release.binary import (
    BINARY_ASSET_SUFFIXES,
    NUITKA_BUILD_TARGETS,
    pack_binary_assets,
)
from ..release.prepare_release import CITATION_PATH, PrepareRelease
from ..release.verify import verify_binary_arch, verify_binary_floor
from ..release.virustotal import (
    FREE_TIER_RATE_LIMIT,
    RequestPacer,
    download_release_binaries,
    load_scan_records,
    poll_detection_stats,
    records_from_results,
    scan_files,
    upsert_scan_records,
)
from ..upstream import DEFAULT_REPO
from .main import (
    changelog_option,
    dry_run_option,
    exit_if_disabled,
    output_option,
    repo_option,
    subcommand,
    write_output,
)

TYPE_CHECKING = False
if TYPE_CHECKING:
    from click_extra import Context


@subcommand(
    short_help="Maintain a Markdown-formatted changelog",
    examples=(
        ("Open the next release's section, in place", "repomatic changelog"),
        ("Print the result instead", "repomatic changelog -"),
    ),
)
@changelog_option
@option(
    "--default-branch",
    default="main",
    show_default=True,
    help="Branch name the unreleased comparison URL points at.",
)
@argument(
    "output",
    type=file_path(writable=True, resolve_path=True, allow_dash=True),
    required=False,
)
@pass_context
def changelog(
    ctx: Context,
    changelog_path: Path,
    default_branch: str,
    output: Path | None,
) -> None:
    """Add an unreleased section for the current version to the changelog.

    Writes the result back to the changelog, unless OUTPUT names another
    file. Pass - as OUTPUT to print the result instead.
    """
    initial_content = None
    if changelog_path.exists():
        logging.info(f"Read initial changelog from {changelog_path}")
        initial_content = changelog_path.read_text(encoding="UTF-8")

    changelog = Changelog(initial_content, get_current_version())
    updated = changelog.update(default_branch=default_branch)
    if output is None:
        output = changelog_path
    if not updated and output == changelog_path and initial_content is not None:
        logging.warning("Changelog already up to date. Do nothing.")
        ctx.exit()

    # The write adds back the one trailing newline the file ends with.
    write_output("updated changelog", changelog.content.rstrip(), output)


@subcommand(
    short_help="Check changelog dates against release dates",
    examples=(
        (
            "Check the default changelog.md (auto-detects PyPI package)",
            "repomatic lint-changelog",
        ),
        ("Fix dates and add admonitions", "repomatic lint-changelog --fix"),
        ("Explicit package name", "repomatic lint-changelog --package repomatic"),
    ),
)
@changelog_option
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
    changelog_path: Path,
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
    """
    config = get_tool_config(ctx)
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


@subcommand(
    short_help="Freeze version, changelog and pins for a release",
    examples=(
        ("Prepare release (changelog + citation)", "repomatic prepare-release"),
        (
            "Post-release: retarget workflows to main branch",
            "repomatic prepare-release --post-release",
        ),
    ),
)
@changelog_option
@option(
    "--citation",
    "citation_path",
    type=file_path(readable=True, writable=True, resolve_path=True),
    default=CITATION_PATH.as_posix(),
    help="Path to the citation file. Skipped when it does not exist.",
)
@option(
    "--workflow-dir",
    type=dir_path(resolve_path=True),
    default=WORKFLOW_DIR.as_posix(),
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
def prepare_release(
    changelog_path: Path,
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

    prep = PrepareRelease(
        changelog_path=changelog_path,
        citation_path=citation_path,
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


@subcommand(
    short_help="Create and push a Git tag",
    examples=(
        ("Create and push a tag", "repomatic git-tag --tag v1.2.3"),
        ("Tag a specific commit", "repomatic git-tag --tag v1.2.3 --commit abc123def"),
        ("Create tag without pushing", "repomatic git-tag --tag v1.2.3 --no-push"),
        ("Fail if tag exists", "repomatic git-tag --tag v1.2.3 --error-existing"),
        (
            "Output result for GitHub Actions",
            'repomatic git-tag --tag v1.2.3 --output "$GITHUB_OUTPUT"',
        ),
    ),
)
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
@output_option("Write created=true or created=false to this file, like $GITHUB_OUTPUT.")
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
    except GitError as e:
        raise ClickException(f"Failed to create/push tag: {e}")

    if created:
        echo(f"Created{' and pushed' if push else ''} tag {tag!r}")
    else:
        echo(f"Tag {tag!r} already exists, skipped.")

    if output:
        write_output("tag result", f"created={'true' if created else 'false'}", output)


@subcommand(
    short_help="Name an attestation bundle after the asset it attests",
    examples=(
        (
            "Single asset, named papaya.tar.gz.attestation.json",
            'repomatic pack-attestation --bundle "${BUNDLE_PATH}"',
        ),
        (
            "A glob's worth of assets, all covered by one bundle",
            "repomatic pack-attestation --bundle b.json --dir dist --name papaya-set",
        ),
    ),
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
    """
    for path in pack_attestation(bundle_path, asset_dir, set_name):
        echo(path)


@subcommand(
    short_help="Pack compiled binaries and their versionless aliases",
    examples=(
        (
            "Pack the binaries a build run downloaded",
            "repomatic pack-binaries --version 1.2.3 --dir ./compile-assets",
        ),
    ),
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
    """
    for path in pack_binary_assets(dist_dir, version_str):
        echo(path)


@subcommand(
    short_help="Pack the skills and agents as a Claude Code plugin",
    examples=(
        (
            "Pack into the default ./repomatic-claude-plugin.zip",
            "repomatic pack-plugin",
        ),
        (
            "Pack for a local install, without a marketplace",
            "repomatic pack-plugin --output /tmp/repomatic-claude-plugin.zip",
        ),
    ),
)
@output_option("Write the plugin archive to this file.", default=f"./{ARCHIVE_NAME}")
def pack_plugin_cmd(output: Path) -> None:
    """Pack the bundled skills and agents into a Claude Code plugin archive.

    Assembles `.claude/.claude-plugin/plugin.json` and every skill and agent the
    component registry declares into a zip holding a single top-level folder,
    which the release lane attaches to each GitHub release. The archive is
    byte-deterministic, so re-packing an unchanged tree produces an identical
    file.

    To load a packed plugin without a marketplace, unzip the archive and point
    `claude --plugin-dir` at the extracted folder.
    """
    if is_stdout(output):
        # The packer writes to a path, so the archive goes through a scratch
        # file on its way to stdout.
        with tempfile.TemporaryDirectory(prefix="repomatic-plugin-") as scratch:
            archive = Path(scratch) / ARCHIVE_NAME
            pack_plugin(Path.cwd(), archive)
            sys.stdout.buffer.write(archive.read_bytes())
        return
    members = pack_plugin(Path.cwd(), output)
    echo(f"Packed {len(members)} files into {output}")


@subcommand(
    short_help="Upload release binaries to VirusTotal",
    examples=(
        (
            "Scan the binaries attached to a tag",
            "repomatic scan-virustotal --tag v1.2.3 --binaries-dir ./binaries",
        ),
        (
            "Wait for verdicts and record them",
            "repomatic scan-virustotal --tag v1.2.3 --binaries-dir ./binaries --poll --records docs/assets/virustotal-scans.csv",
        ),
        (
            "Download the release's binaries first",
            "repomatic scan-virustotal --tag v1.2.3 --download --repo owner/repo --binaries-dir ./binaries",
        ),
    ),
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
    type=dir_path(resolve_path=True),
    required=True,
    help="Directory containing binary files to upload.",
)
@option(
    "--download/--no-download",
    default=False,
    help="Download the tag's versioned binaries from its GitHub release into "
    "--binaries-dir first, waiting for the release to list its assets.",
)
@repo_option(
    "Repository in owner/repo format that --download reads the release from.",
    detect=False,
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
    download: bool,
    repo: str | None,
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

    With --download, first fetches the tag's versioned binaries from its GitHub
    release into the directory, waiting for a freshly published release to list
    its assets.

    With --poll, waits for the analyses to complete and reports each binary's
    flagged / total verdict counts. With --records, the polled snapshots are
    merged into a CSV history, which sync-binaries renders into the binaries
    catalog page.
    """
    if records and not poll:
        raise UsageError("--records requires --poll.")
    if carry_from and not records:
        raise UsageError("--carry-from requires --records.")
    if download and not repo:
        raise UsageError("--download requires --repo.")
    if not download and not binaries_dir.is_dir():
        raise UsageError(f"Directory {binaries_dir} does not exist.")
    if carry_from and records:
        carry_pr_branch_paths(carry_from, (records,))

    if download and repo:
        try:
            download_release_binaries(repo, tag, binaries_dir)
        except RuntimeError as error:
            raise ClickException(f"Could not download {tag} binaries: {error}")

    file_paths = sorted(
        p
        for p in binaries_dir.iterdir()
        if p.is_file() and p.suffix in BINARY_ASSET_SUFFIXES
    )
    if not file_paths:
        echo("No .bin or .exe files found, nothing to upload.")
        return

    # One pacer for the uploads and the polls: they draw on the same budget.
    pacer = RequestPacer(rate_limit)
    echo(f"Uploading {len(file_paths)} file(s) to VirusTotal...")
    results = scan_files(api_key, file_paths, pacer)
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
        results = poll_detection_stats(api_key, results, pacer, poll_timeout)
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


@subcommand(
    short_help="Verify binary architecture and OS floor",
    examples=(
        (
            "Verify a Linux ARM64 binary",
            "repomatic verify-binary --target linux-arm64 --binary ./mpm-linux-arm64.bin",
        ),
        (
            "Verify a Windows x64 binary",
            "repomatic verify-binary --target windows-x64 --binary ./mpm-windows-x64.exe",
        ),
    ),
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
    """
    # The choice of `--target` already rejected an unknown target.
    build_target = NUITKA_BUILD_TARGETS[target]
    verify_binary_arch(build_target, binary_path)
    echo(f"Binary architecture verified for {target}: {binary_path}")
    scan_dirs = list(dist_dirs) or sorted(Path.cwd().glob("*.dist"))
    verify_binary_floor(build_target, binary_path, scan_dirs)
    echo(f"OS floor verified for {target}.")


@subcommand(
    short_help="Regenerate the binaries catalog page",
    examples=(
        (
            "Regenerate the binaries catalog page",
            "repomatic sync-binaries --repo owner/repo",
        ),
        (
            "With the detection snapshots of a scan history",
            "repomatic sync-binaries --records docs/assets/virustotal-scans.csv",
        ),
    ),
)
@repo_option()
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
    help="CSV scan history written by scan-virustotal.",
)
@pass_context
def sync_binaries(
    ctx: Context,
    repo: str,
    page: Path,
    records: Path | None,
) -> None:
    """Regenerate the binaries catalog from the GitHub Releases API.

    Writes the catalog data to assets/binaries.csv next to the page, one row
    per released binary: its version linking to the release, its platform
    linking to the download, its release date, and the VirusTotal detection
    snapshot when a scan history file is given. The page renders the CSV
    through a csv-table directive and is created from a default template when
    missing; only its chart region is rewritten afterwards, so the prose can
    be edited per repository.
    """
    exit_if_disabled(ctx, "binaries.sync")

    try:
        releases = get_releases_with_assets(f"https://github.com/{repo}")
    except GitHubReleasesUnavailable as e:
        raise ClickException(str(e))

    try:
        scan_records = load_scan_records(records) if records else []
        changes = update_catalog(page, repo, releases, scan_records)
    except ValueError as e:
        raise ClickException(str(e))

    for path, changed in changes.items():
        if changed:
            echo(f"Updated {path}.")
        else:
            echo(f"{path} already up to date.")


@subcommand(short_help="Sync bumpversion config from bundled template")
@pass_context
def sync_bumpversion(ctx: Context) -> None:
    """Sync [tool.bumpversion] config in pyproject.toml from the bundled
    template.

    Re-derives the [tool.bumpversion] section from the canonical template
    bundled in repomatic, keeping the local additions. Designed for the
    sync-bumpversion autofix job. The repomatic init bumpversion command
    remains available for interactive bootstrapping.
    """
    exit_if_disabled(ctx, "bumpversion.sync")

    pyproject_path = Path("pyproject.toml")
    merged = init_config("bumpversion", pyproject_path)
    if merged is not None and write_if_changed(pyproject_path, merged):
        echo(f"Updated: {pyproject_path}")
    else:
        echo("bumpversion config is up to date.")


@subcommand(
    short_help="Sync rolling dev pre-release on GitHub",
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
@dry_run_option(default=True)
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
def sync_dev_release_cmd(
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
    exit_if_disabled(ctx, "dev-release.sync")

    if delete and upload_assets:
        raise UsageError("--delete and --upload-assets are mutually exclusive.")
    version = get_current_version()
    if not version:
        logging.warning("Could not determine current version.")
        return

    loaded = load_changelog_repo(config)
    if loaded is None:
        return
    changelog_path, repo_url = loaded

    # Parse owner/repo for gh CLI.
    parsed = parse_github_repo(repo_url)
    repository = "/".join(parsed) if parsed else ""

    if delete:
        if dry_run:
            echo("[dry-run] Would delete all dev releases.")
            return
        cleanup_dev_releases(repository)
        echo("Deleted all dev releases.")
        return

    try:
        synced = sync_dev_release(
            changelog_path, version, repository, dry_run, asset_dir=upload_assets
        )
    except RuntimeError as error:
        raise ClickException(str(error))
    if synced:
        mode = "dry-run" if dry_run else "live"
        echo(f"[{mode}] Dev release v{version} synced.")


@subcommand(
    short_help="Sync GitHub release notes from changelog",
    examples=(
        (
            "Dry run to preview what would be updated",
            "repomatic sync-github-releases --dry-run",
        ),
        ("Update drifted release notes", "repomatic sync-github-releases --live"),
    ),
)
@dry_run_option(default=True)
@pass_context
def sync_github_releases_cmd(ctx: Context, dry_run: bool) -> None:
    """Sync GitHub release notes from changelog.md.

    Compares each GitHub release body against the corresponding
    changelog.md section and updates any that have drifted.
    """
    exit_if_disabled(ctx, "github-releases.sync")
    loaded = load_changelog_repo(get_tool_config(ctx))
    if loaded is None:
        return
    changelog_path, repo_url = loaded

    result = sync_github_releases(repo_url, changelog_path, dry_run)
    echo(render_sync_report(result))
