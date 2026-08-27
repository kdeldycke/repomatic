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

"""Sync a rolling dev pre-release on GitHub.

Maintains a single **draft** pre-release that mirrors the unreleased
changelog section and always carries the latest successful dev binaries
and Python package. The dev tag (e.g. `v6.1.1.dev0`) is force-updated
to point to the latest `main` commit — no tag proliferation.

When the current version's dev release already exists, it is **edited**
(not deleted and recreated) so that previously uploaded assets — especially
compiled binaries — survive pushes that skip binary compilation (e.g.
documentation-only changes). The `upload_release_assets()` function deletes
all existing assets before uploading new ones, preventing stale files from
accumulating when the naming scheme changes. Stale dev releases from previous
versions are always deleted.

```{note}
Dev releases are created as **drafts** so they remain mutable even
when GitHub's immutable releases setting is enabled. Immutability
only blocks **asset uploads** on published releases — deletion still
works. But because the workflow needs to upload binaries *after*
creation, the release must stay as a draft throughout its lifetime
to allow asset uploads. See `CLAUDE.md` § Immutable releases.
```
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..changelog import Changelog
from ..release.binary import BINARY_ASSET_SUFFIXES, PYTHON_DIST_SUFFIXES
from .gh import gh_api_json, run_gh_command
from .release_sync import build_expected_body
from .releases import edit_release_notes

DEV_ASSET_PATTERNS = tuple(
    f"*{suffix}" for suffix in BINARY_ASSET_SUFFIXES + PYTHON_DIST_SUFFIXES
)
"""Glob patterns for dev release assets.

Both halves are spelled as globs from the sets that define them:
{data}`~repomatic.release.binary.BINARY_ASSET_SUFFIXES` for the compiled binaries, so a
dev pre-release carries exactly the artifacts the release workflow downloads and
`scan-virustotal` submits, and {data}`~repomatic.release.binary.PYTHON_DIST_SUFFIXES`
for what a dev pre-release adds on top. Derived rather than re-listed, because a
dev release advertising a different set of assets than the real one is the bug
this pairing exists to prevent.

```{note}
Bare extensions (no `repomatic-` prefix) keep patterns generic so
downstream repositories can reuse the same logic regardless of their
package name.
```
"""


def sync_dev_release(
    changelog_path: Path,
    version: str,
    repository: str,
    dry_run: bool = True,
    asset_dir: Path | None = None,
) -> bool:
    """Create or update the dev pre-release on GitHub.

    Reads the changelog, renders the release body for the given version
    via {func}`build_expected_body`, then either edits the existing dev
    release or creates a new one. Stale dev releases from previous
    versions are always cleaned up.

    Existing releases are edited (not deleted and recreated) to preserve
    assets like compiled binaries from previous successful builds.
    When `asset_dir` is provided, existing assets are deleted and new
    ones uploaded via {func}`upload_release_assets`.

    :param changelog_path: Path to `changelog.md`.
    :param version: Current version string (e.g. `6.1.1.dev0`).
    :param repository: GitHub repository in `owner/name` form.
    :param dry_run: If `True`, report without making changes.
    :param asset_dir: Directory containing assets to upload. If `None`,
        no asset upload is performed.
    :return: `True` if the release was synced (or would be in
        dry-run), `False` if the changelog section is empty.
    """
    content = changelog_path.read_text(encoding="UTF-8")
    changelog = Changelog(content)

    body = build_expected_body(changelog, version)
    if not body:
        logging.warning(f"Changelog section for {version} produced an empty body.")
        return False

    tag = f"v{version}"

    if dry_run:
        logging.info(f"[dry-run] Would sync dev release {tag}.")
        logging.info(f"[dry-run] Release body:\n{body}")
        if asset_dir is not None:
            files = _collect_asset_files(asset_dir)
            if files:
                names = ", ".join(f.name for f in files)
                logging.info(f"[dry-run] Would upload {len(files)} assets: {names}")
            else:
                logging.info(f"[dry-run] No matching assets found in {asset_dir}.")
        return True

    # Delete stale dev releases from previous versions, preserving the current one.
    cleanup_dev_releases(repository, keep_tag=tag)

    # Try to edit the existing release first. This preserves assets (binaries)
    # from previous successful builds when the current push skips compilation.
    if edit_release_notes(tag, repository, body, title=version):
        logging.info(f"Updated existing dev draft pre-release {tag}.")
    else:
        # No existing release to edit; create a new draft pre-release targeting
        # main. Draft stays mutable so the workflow can upload binaries and
        # packages after creation, and so immutable releases don't block asset
        # uploads or future deletions.
        run_gh_command([
            "release",
            "create",
            tag,
            "--draft",
            "--prerelease",
            "--target",
            "main",
            "--title",
            version,
            "--notes",
            body,
            "--repo",
            repository,
        ])
        logging.info(f"Created dev draft pre-release {tag}.")

    if asset_dir is not None:
        uploaded = upload_release_assets(tag, repository, asset_dir)
        if uploaded:
            names = ", ".join(f.name for f in uploaded)
            logging.info(f"Uploaded {len(uploaded)} assets: {names}")

    return True


def _collect_asset_files(asset_dir: Path) -> list[Path]:
    """Collect files matching {data}`DEV_ASSET_PATTERNS` from a directory.

    :param asset_dir: Directory to scan.
    :return: Sorted list of matching file paths.
    """
    files: list[Path] = []
    for pattern in DEV_ASSET_PATTERNS:
        files.extend(asset_dir.glob(pattern))
    return sorted(set(files))


def _delete_release_assets(tag: str, repository: str) -> int:
    """Delete all assets from an existing release.

    Fetches the asset list via `gh release view` and deletes each asset
    individually via the GitHub API. Continues on per-asset failures,
    mirroring the shell script's `|| true` behavior.

    :param tag: Git tag name (e.g. `v6.1.1.dev0`).
    :param repository: GitHub repository in `owner/name` form.
    :return: Number of assets successfully deleted.
    """
    payload = gh_api_json([
        "release",
        "view",
        tag,
        "--repo",
        repository,
        "--json",
        "assets",
    ])
    if payload is None:
        logging.debug(f"Could not view release {tag} for asset deletion.")
        return 0

    assets = payload.get("assets", [])
    deleted = 0
    for asset in assets:
        # Extract numeric asset ID from the apiUrl field.
        asset_id = asset["apiUrl"].split("/")[-1]
        try:
            run_gh_command([
                "api",
                "--method",
                "DELETE",
                f"repos/{repository}/releases/assets/{asset_id}",
            ])
            deleted += 1
        except RuntimeError:
            logging.debug(f"Could not delete asset {asset_id}.")
    return deleted


def upload_release_assets(
    tag: str,
    repository: str,
    asset_dir: Path,
) -> list[Path]:
    """Upload assets to a GitHub release.

    Scans `asset_dir` for files matching {data}`DEV_ASSET_PATTERNS`. If
    no matching files are found, returns immediately without modifying the
    release — this preserves existing assets for documentation-only pushes.
    When files are found, all existing assets are deleted first to prevent
    stale files from accumulating when the naming scheme changes.

    :param tag: Git tag name (e.g. `v6.1.1.dev0`).
    :param repository: GitHub repository in `owner/name` form.
    :param asset_dir: Directory containing assets to upload.
    :return: List of uploaded file paths.
    """
    files = _collect_asset_files(asset_dir)
    if not files:
        logging.info("No matching assets found; preserving existing release assets.")
        return []

    deleted = _delete_release_assets(tag, repository)
    logging.debug(f"Deleted {deleted} existing assets before upload.")

    file_args = [str(f) for f in files]
    run_gh_command([
        "release",
        "upload",
        tag,
        *file_args,
        "--repo",
        repository,
        "--clobber",
    ])
    return files


def cleanup_dev_releases(repository: str, *, keep_tag: str | None = None) -> None:
    """Delete stale dev pre-releases from GitHub.

    Lists all releases and deletes any whose tag ends with `.dev0`,
    except `keep_tag` which is preserved so its assets (e.g. compiled
    binaries) survive. This handles stale dev releases left behind after
    version bumps. Silently succeeds if no dev releases exist or if
    individual deletions fail.

    :param repository: GitHub repository in `owner/name` form.
    :param keep_tag: Tag to preserve (e.g. `v6.2.0.dev0`). If `None`,
        all dev releases are deleted.
    """
    releases = gh_api_json([
        "release",
        "list",
        "--json",
        "tagName",
        "--repo",
        repository,
    ])
    if releases is None:
        logging.debug("Could not list releases.")
        return

    for release in releases:
        tag = release["tagName"]
        if (
            tag.endswith(".dev0")
            and tag != keep_tag
            and delete_release_by_tag(tag, repository)
        ):
            logging.info(f"Deleted stale dev release {tag}.")


def delete_release_by_tag(tag: str, repository: str) -> bool:
    """Delete a release and its tag from GitHub.

    Silently succeeds if the release does not exist or cannot be deleted. The
    outcome is returned rather than announced: this helper deletes any release,
    so only the caller knows what kind of release it just removed and can name
    it accurately.

    :param tag: Git tag name (e.g. `v6.1.1.dev0`).
    :param repository: GitHub repository in `owner/name` form.
    :return: `True` when the release was deleted, `False` when it did not exist
        or could not be removed.
    """
    try:
        run_gh_command([
            "release",
            "delete",
            tag,
            "--cleanup-tag",
            "--yes",
            "--repo",
            repository,
        ])
    except RuntimeError:
        logging.debug(f"Could not delete release {tag}.")
        return False
    return True
