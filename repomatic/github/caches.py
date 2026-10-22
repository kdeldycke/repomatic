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

"""GitHub Actions caches of a repository, and the pruning of superseded ones.

A cache step keyed on the run or the commit, with a prefix in its
`restore-keys`, saves a fresh entry on every run and restores the newest
previous one. Each older entry is never read again, yet it stays until GitHub
evicts it a week after its last access, and counts against the repository's
cache quota of 10 GB. Past the quota GitHub evicts the least recently used
entry, live caches included, so an accumulation of dead snapshots is what
evicts the tool and compile caches a job depends on.

{func}`prune_caches` keeps the newest entries of a prefix and deletes the rest.

```{note}
`actions/cache` saves at the end of the job, once every step ran, so a prune
step inside the job sees the entries of the previous runs only: keeping one
leaves two once the job saves its own.
```
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import quote

from click_extra import ColumnSpec, format_size

from .gh import GhCommandError, gh_api_pages, run_gh_command

TYPE_CHECKING = False
if TYPE_CHECKING:
    from typing import Any

CACHES_ENDPOINT = "repos/{repo}/actions/caches"
"""REST endpoint listing the caches of a repository, and deleting one by ID."""

PRUNE_CACHES_HEADER_DEFS: tuple[ColumnSpec, ...] = (
    ColumnSpec("key", "Key"),
    ColumnSpec("ref", "Ref"),
    ColumnSpec("size", "Size"),
    ColumnSpec("created", "Created"),
    ColumnSpec("outcome", "Outcome"),
)
"""Column definitions for the `repomatic prune-caches` table."""


@dataclass(frozen=True)
class ActionsCache:
    """One cache entry of a repository, as the REST API lists it."""

    id: int
    """Identifier the delete endpoint takes."""

    key: str
    """The key the saving step wrote."""

    ref: str
    """The git ref the entry is scoped to, like `refs/heads/main`."""

    size: int
    """Size in bytes."""

    created_at: str
    """ISO 8601 timestamp of the save, which sorts as text."""

    @classmethod
    def from_api(cls, item: dict[str, Any]) -> ActionsCache:
        """Build an entry from one item of the listing endpoint."""
        return cls(
            id=int(item["id"]),
            key=str(item["key"]),
            ref=str(item.get("ref") or ""),
            size=int(item.get("size_in_bytes") or 0),
            created_at=str(item.get("created_at") or ""),
        )


@dataclass(frozen=True)
class PruneReport:
    """What a prune of one prefix kept, deleted and could not delete."""

    prefix: str
    """The key prefix pruned."""

    kept: tuple[ActionsCache, ...]
    """The newest entries, left in place."""

    deleted: tuple[ActionsCache, ...]
    """The superseded entries deleted, or that a dry run would delete."""

    refused: tuple[ActionsCache, ...]
    """The superseded entries the API refused to delete."""

    @property
    def freed(self) -> int:
        """Bytes the deleted entries held."""
        return sum(cache.size for cache in self.deleted)

    def rows(self, *, dry_run: bool = False) -> list[list[str]]:
        """One table row per entry, in the order of the listing."""
        outcomes = [
            *((cache, "kept") for cache in self.kept),
            *(
                (cache, "would delete" if dry_run else "deleted")
                for cache in self.deleted
            ),
            *((cache, "refused") for cache in self.refused),
        ]
        return [
            [cache.key, cache.ref, format_size(cache.size), cache.created_at, outcome]
            for cache, outcome in outcomes
        ]


def list_caches(repo: str, prefix: str) -> list[ActionsCache]:
    """Caches of *repo* whose key starts with *prefix*, newest first.

    The API filters on the prefix itself, and the result is filtered again
    here, so a prefix the query string decodes differently matches no more
    than it names.

    :param repo: Repository in `owner/name` form.
    :param prefix: Key prefix, as a cache step's `restore-keys` names it.
    :raises GhCommandError: The listing failed, as when the token lacks
        `actions: read`.
    :raises GhOutputError: The listing was not JSON.
    """
    endpoint = (
        f"{CACHES_ENDPOINT.format(repo=repo)}?key={quote(prefix, safe='')}"
        "&per_page=100&sort=created_at&direction=desc"
    )
    items = gh_api_pages(endpoint, key="actions_caches")
    caches = [
        ActionsCache.from_api(item)
        for item in items
        if isinstance(item, dict) and str(item.get("key", "")).startswith(prefix)
    ]
    return sorted(caches, key=lambda cache: cache.created_at, reverse=True)


def delete_cache(repo: str, cache: ActionsCache) -> None:
    """Delete one cache entry of *repo*.

    :raises GhCommandError: The deletion failed, as when the token lacks
        `actions: write`.
    """
    endpoint = f"{CACHES_ENDPOINT.format(repo=repo)}/{cache.id}"
    run_gh_command(["api", "--method", "DELETE", endpoint])


def prune_caches(
    repo: str, prefix: str, *, keep: int = 1, dry_run: bool = False
) -> PruneReport:
    """Delete every cache of *prefix* but the newest *keep*.

    A deletion the API refuses is logged and reported, never raised:
    housekeeping must not fail the job it runs in. The listing itself raises,
    and the caller decides.

    :param repo: Repository in `owner/name` form.
    :param prefix: Key prefix whose entries to prune.
    :param keep: How many of the newest entries to leave in place.
    :param dry_run: Report the entries to delete without deleting them.
    :raises GhCommandError: The listing failed.
    :raises GhOutputError: The listing was not JSON.
    """
    caches = list_caches(repo, prefix)
    kept, stale = caches[:keep], caches[keep:]
    deleted: list[ActionsCache] = []
    refused: list[ActionsCache] = []
    for cache in stale:
        if dry_run:
            deleted.append(cache)
            continue
        try:
            delete_cache(repo, cache)
        except GhCommandError as error:
            logging.warning(f"Could not delete cache {cache.key!r}: {error}")
            refused.append(cache)
        else:
            logging.info(f"Deleted cache {cache.key!r} ({format_size(cache.size)}).")
            deleted.append(cache)
    return PruneReport(
        prefix=prefix,
        kept=tuple(kept),
        deleted=tuple(deleted),
        refused=tuple(refused),
    )
