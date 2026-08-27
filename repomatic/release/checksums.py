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

"""Recompute SHA-256 checksums for the binary tool registry.

Iterates every `TOOL_REGISTRY` entry with a `binary` spec, downloads each
platform's release artifact, and rewrites stale hashes in-place in
`tool_registry.py` (alongside the `VERSIONS` stamps). Driven by
`repomatic update-checksums` and, with a version override, by
`sync-tool-versions` so a version bump and its matching checksums land in one
pass.
"""

from __future__ import annotations

import logging
import re
import tempfile
from pathlib import Path

from click_extra import (
    OperationTrail,
    get_current_context,
    resolve_jobs,
    run_jobs,
)

from ..tooling.tool_registry import TOOL_REGISTRY
from ..tooling.tool_runner import download_to

TYPE_CHECKING = False
if TYPE_CHECKING:
    from ..tooling.tool_registry import PlatformKey, ToolSpec


def _download_sha256(url: str) -> str:
    """Download a URL and return its SHA-256 hex digest.

    Streams through {func}`~repomatic.tooling.tool_runner.download_to` into a scratch
    file, inheriting its stall timeout and Content-Length truncation guard. A
    short body must fail loudly here: its digest would otherwise be written
    into `tool_registry.py` as the new canonical checksum, poisoning every
    later verified install. Feedback is suppressed because the caller fans
    downloads out under its own `OperationTrail`.

    :param url: The URL to download.
    :return: Lowercase hex SHA-256 digest of the response body.
    """
    with tempfile.TemporaryDirectory(prefix="repomatic-checksum-") as scratch:
        digest = download_to(url, Path(scratch) / "artifact", progress=False)
    logging.debug(f"SHA-256 of {url}: {digest}")
    return digest


def update_registry_checksums(
    registry_path: Path,
    version_overrides: dict[str, str] | None = None,
) -> list[tuple[str, str, str]]:
    """Recompute binary checksums and version stamps in `tool_registry.py`.

    Iterates every `TOOL_REGISTRY` entry with a `binary` spec, downloads each
    platform URL (concurrently, sized by the global `--jobs` option and
    sequential at `DEBUG` verbosity or without an active CLI context), computes
    its SHA-256, and replaces stale hashes in-place. Also reconciles each tool's
    `VERSIONS` stamp with the version the checksums were computed for, the basis
    of the offline staleness test.

    :param registry_path: Path to `tool_registry.py`.
    :param version_overrides: Optional mapping of tool name to a version to
        download instead of the in-memory `ToolSpec.version`. `sync-tool-versions`
        passes this so it can bump the version in the source and refresh the
        checksums in a single process: the in-memory registry still holds the
        pre-bump version because the file was edited, not reimported.
    :return: List of `(url, old_hash, new_hash)` for each updated checksum.
        Empty if all checksums are already correct.
    """
    overrides = version_overrides or {}
    original = registry_path.read_text(encoding="UTF-8")
    content = original
    updated: list[tuple[str, str, str]] = []

    # Flatten all binary platform entries for progress tracking, resolving each
    # download URL against the override version when one is supplied.
    entries = [
        (
            spec,
            pk,
            tmpl.format(version=overrides.get(spec.name, spec.version)),
            spec.binary.checksums[pk],
        )
        for spec in TOOL_REGISTRY.values()
        if spec.binary is not None
        for pk, tmpl in spec.binary.urls.items()
    ]

    def entry_sha256(entry: tuple[ToolSpec, PlatformKey, str, str]) -> str:
        spec, platform_key, url, _ = entry
        logging.info(f"Verifying registry checksum for {spec.name} ({platform_key})")
        return _download_sha256(url)

    # Size the fan-out once, up front, so the trail's rendering mode matches
    # the width run_jobs fans out to below.
    ctx = get_current_context(silent=True)
    jobs = resolve_jobs(ctx, len(entries), serial_at_debug=True)

    # run_jobs yields digests in submission order, so the rewrite loop below
    # stays deterministic while the downloads overlap. A download that raises
    # still aborts the batch (no partial rewrite), leaving the trail's completed
    # rows on screen.
    with OperationTrail(
        label="Verifying", unit="checksums", total=len(entries), jobs=jobs
    ) as trail:
        for (spec, platform_key, url, old_hash), new_hash in zip(
            entries, run_jobs(entry_sha256, entries, jobs=jobs)
        ):
            trail.mark(True, f"{spec.name} ({platform_key})")
            if old_hash != new_hash:
                content = content.replace(old_hash, new_hash)
                updated.append((url, old_hash, new_hash))
                logging.info(f"Updated checksum: {old_hash} -> {new_hash}")
            else:
                logging.info("Checksum unchanged.")
        trail.finish(True, f"Verified {trail.ok_count}/{len(entries)} checksums")

    # Reconcile each tool's VERSIONS stamp with its target version (idempotent).
    # The key pattern only matches the quoted-string values in `VERSIONS`, never
    # the `ToolSpec(` registry entries or the tuple-keyed `CHECKSUMS` entries.
    for spec in TOOL_REGISTRY.values():
        if spec.binary is None:
            continue
        target = overrides.get(spec.name, spec.version)
        content = re.sub(
            rf'("{re.escape(spec.name)}":\s*)"[^"]*"',
            rf'\g<1>"{target}"',
            content,
        )

    if content != original:
        registry_path.write_text(content, encoding="UTF-8")

    return updated
