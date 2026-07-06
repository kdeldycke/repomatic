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
`tool_runner.py` (alongside the `VERSIONS` stamps). Driven by
`repomatic update-checksums` and, with a version override, by
`sync-tool-versions` so a version bump and its matching checksums land in one
pass.
"""

from __future__ import annotations

import hashlib
import logging
import re
import sys
from pathlib import Path
from urllib.request import Request, urlopen

from click_extra import progressbar, run_jobs

from .tool_runner import TOOL_REGISTRY, PlatformKey, ToolSpec


def _download_sha256(url: str) -> str:
    """Download a URL and return its SHA-256 hex digest.

    :param url: The URL to download.
    :return: Lowercase hex SHA-256 digest of the response body.
    """
    request = Request(url)
    with urlopen(request) as response:
        digest = hashlib.sha256(response.read()).hexdigest()
    logging.debug(f"SHA-256 of {url}: {digest}")
    return digest


def update_registry_checksums(
    tool_runner_path: Path,
    version_overrides: dict[str, str] | None = None,
) -> list[tuple[str, str, str]]:
    """Recompute binary checksums and version stamps in `tool_runner.py`.

    Iterates every `TOOL_REGISTRY` entry with a `binary` spec, downloads each
    platform URL (concurrently, sized by the global `--jobs` option and
    sequential at `DEBUG` verbosity or without an active CLI context), computes
    its SHA-256, and replaces stale hashes in-place. Also reconciles each tool's
    `VERSIONS` stamp with the version the checksums were computed for, the basis
    of the offline staleness test.

    :param tool_runner_path: Path to `tool_runner.py`.
    :param version_overrides: Optional mapping of tool name to a version to
        download instead of the in-memory `ToolSpec.version`. `sync-tool-versions`
        passes this so it can bump the version in the source and refresh the
        checksums in a single process: the in-memory registry still holds the
        pre-bump version because the file was edited, not reimported.
    :return: List of `(url, old_hash, new_hash)` for each updated checksum.
        Empty if all checksums are already correct.
    """
    overrides = version_overrides or {}
    original = tool_runner_path.read_text(encoding="UTF-8")
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

    # run_jobs yields digests in submission order, so the rewrite loop below
    # stays deterministic while the downloads overlap.
    with progressbar(
        zip(entries, run_jobs(entry_sha256, entries, serial_at_debug=True)),
        length=len(entries),
        label="Verifying checksums",
        file=sys.stderr,
    ) as items:
        for (spec, platform_key, url, old_hash), new_hash in items:
            if old_hash != new_hash:
                content = content.replace(old_hash, new_hash)
                updated.append((url, old_hash, new_hash))
                logging.info(f"Updated checksum: {old_hash} -> {new_hash}")
            else:
                logging.info("Checksum unchanged.")

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
        tool_runner_path.write_text(content, encoding="UTF-8")

    return updated
