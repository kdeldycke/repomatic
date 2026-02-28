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
and Python package. The dev tag (e.g. ``v6.1.1.dev0``) is force-updated
to point to the latest ``main`` commit — no tag proliferation.

.. note::
    Dev releases are created as **drafts** so they remain mutable even
    when GitHub's immutable releases setting is enabled. Drafts can always
    be deleted and have their assets replaced. The workflow uploads
    binaries and packages after creation, then leaves the release as a
    draft — it is never published.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from .gh import run_gh_command
from .release_sync import build_expected_body

TYPE_CHECKING = False
if TYPE_CHECKING:
    pass


def sync_dev_release(
    changelog_path: Path,
    version: str,
    nwo: str,
    dry_run: bool = True,
) -> bool:
    """Create or update the dev pre-release on GitHub.

    Reads the changelog, renders the release body for the given version
    via :func:`build_expected_body`, then deletes any existing dev
    release and creates a new pre-release.

    :param changelog_path: Path to ``changelog.md``.
    :param version: Current version string (e.g. ``6.1.1.dev0``).
    :param nwo: Repository name-with-owner (e.g. ``user/repo``).
    :param dry_run: If ``True``, report without making changes.
    :return: ``True`` if the release was synced (or would be in
        dry-run), ``False`` if the changelog section is empty.
    """
    from ..changelog import Changelog

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
        return True

    # Delete all existing dev releases (current + stale from previous versions).
    cleanup_dev_releases(nwo)

    # Create new draft pre-release targeting main. Draft stays mutable so
    # the workflow can upload binaries and packages after creation, and so
    # immutable releases don't block asset uploads or future deletions.
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
        nwo,
    ])
    logging.info(f"Created dev draft pre-release {tag}.")

    return True


def cleanup_dev_releases(nwo: str) -> None:
    """Delete all dev pre-releases from GitHub.

    Lists all releases and deletes any whose tag ends with ``.dev0``.
    This handles both the current version's dev release and stale ones
    left behind after version bumps. Silently succeeds if no dev
    releases exist or if individual deletions fail (e.g., immutable
    published releases from before the draft migration).

    :param nwo: Repository name-with-owner (e.g. ``user/repo``).
    """
    try:
        output = run_gh_command([
            "release",
            "list",
            "--json",
            "tagName",
            "--repo",
            nwo,
        ])
    except RuntimeError:
        logging.debug("Could not list releases.")
        return

    releases = json.loads(output)
    for release in releases:
        tag = release["tagName"]
        if tag.endswith(".dev0"):
            delete_release_by_tag(tag, nwo)


def delete_dev_release(version: str, nwo: str) -> None:
    """Delete the dev pre-release and its tag from GitHub.

    Silently succeeds if no dev release exists. This is used during
    real releases to clean up the dev pre-release for the version
    being released.

    :param version: Dev version string (e.g. ``6.1.1.dev0``).
    :param nwo: Repository name-with-owner (e.g. ``user/repo``).
    """
    delete_release_by_tag(f"v{version}", nwo)


def delete_release_by_tag(tag: str, nwo: str) -> None:
    """Delete a release and its tag from GitHub.

    Silently succeeds if the release does not exist or cannot be
    deleted (e.g., immutable published releases).

    :param tag: Git tag name (e.g. ``v6.1.1.dev0``).
    :param nwo: Repository name-with-owner (e.g. ``user/repo``).
    """
    try:
        run_gh_command([
            "release",
            "delete",
            tag,
            "--cleanup-tag",
            "--yes",
            "--repo",
            nwo,
        ])
        logging.info(f"Deleted dev release {tag}.")
    except RuntimeError:
        logging.debug(f"Could not delete release {tag}.")
