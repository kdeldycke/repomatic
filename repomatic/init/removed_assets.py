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

"""Find the orphans of assets repomatic no longer ships.

{data}`~repomatic.components.REMOVED_ASSETS` lists one tombstone per dropped
asset. `repomatic init` walks them against the target tree, and sorts each
orphan it finds into the ones safe to prune and the ones a person must review.
"""

from __future__ import annotations

from enum import Enum

from ..components import REMOVED_ASSETS, RemovedAsset
from ..github.workflow_files import load_workflow
from ..github.workflow_sync import canonical_workflow, extract_extra_jobs
from ..upstream import UPSTREAM_REPO_SLUGS

TYPE_CHECKING = False
if TYPE_CHECKING:
    from pathlib import Path

    from ..config import Config


class OrphanVerdict(Enum):
    """What `init` does with a file found at a tombstoned path."""

    PRUNE = "prune"
    """An untouched copy of a shipped asset: safe to delete."""

    REVIEW = "review"
    """A shipped asset edited since: reported, never deleted."""

    SKIP = "skip"
    """Not an asset repomatic shipped, only a file sharing its path."""


def detect_removed_assets(
    output_dir: Path, config: Config | None
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Find on-disk orphans of assets repomatic has dropped.

    Walks {data}`~repomatic.components.REMOVED_ASSETS`, resolves each tombstone's
    target through the same path overrides as live files, and classifies any
    that still exist on disk:

    - *prunable*: an untouched copy, safe to delete. For a content-gated asset
      the content matches one of the recorded shipped digests; for a workflow
      the file is a repomatic-lineage thin-caller for the removed workflow with
      no extra downstream jobs.
    - *review*: present but customized locally (content differs, or a
      thin-caller carries extra jobs). Reported, never deleted.

    A file that is not recognizable as a repomatic-shipped orphan (a user's own
    workflow that merely shares a name) is left untouched.

    A tombstoned file that is already gone is not the end of it: an asset that
    shipped as a folder leaves that folder standing, and an empty one is
    prunable on its own. See {func}`_empty_owned_dir`.

    :param output_dir: Root directory of the target repository.
    :param config: Repomatic config for `skills.location` / `subagents.location`
        path overrides.
    :return: `(prunable, review)`, each a list of `(relative_path,
        successor)` tuples sorted by path.
    """
    prunable: list[tuple[str, str]] = []
    review: list[tuple[str, str]] = []
    for asset in REMOVED_ASSETS:
        rel = asset.locate(asset.target, config)
        path = output_dir / rel
        if not path.exists():
            orphan_dir = _empty_owned_dir(asset, output_dir, config)
            if orphan_dir:
                prunable.append((orphan_dir, asset.successor))
            continue
        verdict = _classify(asset, path)
        if verdict is OrphanVerdict.PRUNE:
            prunable.append((rel, asset.successor))
        elif verdict is OrphanVerdict.REVIEW:
            review.append((rel, asset.successor))
    prunable.sort()
    review.sort()
    return prunable, review


def _classify(asset: RemovedAsset, path: Path) -> OrphanVerdict:
    """Judge the file at a tombstoned *path* through the gate of *asset*.

    A content-gated asset is matched against the digests of what repomatic
    shipped, in the form `init` writes to disk. A workflow is matched by its
    `uses:` fingerprint instead (see {func}`_classify_removed_workflow`).
    """
    if not asset.content_gated:
        return _classify_removed_workflow(path)
    if asset.is_untouched(path.read_text(encoding="UTF-8")):
        return OrphanVerdict.PRUNE
    return OrphanVerdict.REVIEW


def _empty_owned_dir(
    asset: RemovedAsset, output_dir: Path, config: Config | None
) -> str:
    """The folder a removed asset left behind empty, when there is one.

    A tombstone addresses a file, so it stops firing the moment that file goes
    by any route other than a full `init` prune: a hand `rm`, or a repomatic old
    enough to unlink the file without sweeping its parent. An asset that shipped
    as a folder then leaves that folder standing, empty, and invisible to every
    later run of a check keyed on the file. Pruning it needs no content gate: an
    empty directory holds nothing a repository could lose.

    A folder that still holds files is left alone. Whatever is in it, repomatic
    did not write it: its own entry point is the file already established as
    missing.

    :return: The folder's path relative to *output_dir*, or an empty string when
        the asset owned no folder, the folder is gone too, or it is not empty.
    """
    if not asset.owned_dir:
        return ""
    rel = asset.locate(asset.owned_dir, config)
    path = output_dir / rel
    if not path.is_dir() or any(path.iterdir()):
        return ""
    return rel


def _classify_removed_workflow(path: Path) -> OrphanVerdict:
    """Classify an on-disk workflow file as an orphan of a removed workflow.

    Thin-callers are parameterized per repo (version pin, `paths:` filters), so
    they carry no fixed content to hash. The file is matched instead by its
    `uses:` fingerprint against the upstream slugs in
    {data}`~repomatic.upstream.UPSTREAM_REPO_SLUGS`.

    :return: {attr}`OrphanVerdict.PRUNE` for a pure repomatic-lineage
        thin-caller for this workflow, {attr}`OrphanVerdict.REVIEW` when the
        user appended extra jobs, or {attr}`OrphanVerdict.SKIP` when the file
        is not a lineage thin-caller.
    """
    try:
        content = path.read_text(encoding="UTF-8")
    except OSError:
        return OrphanVerdict.SKIP
    data = load_workflow(content)
    if data is None:
        return OrphanVerdict.SKIP
    for slug in UPSTREAM_REPO_SLUGS:
        if canonical_workflow(data, slug) == path.name:
            extra = extract_extra_jobs(content, slug, data=data)
            return OrphanVerdict.REVIEW if extra.strip() else OrphanVerdict.PRUNE
    return OrphanVerdict.SKIP
