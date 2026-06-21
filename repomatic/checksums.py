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

"""Update SHA-256 checksums for binary downloads.

Two update modes:

1. **Workflow files** — scans for GitHub release download URLs paired with
   `sha256sum --check` verification lines. Replaces stale hashes in-place.
2. **Tool registry** — iterates `TOOL_REGISTRY` entries with `binary`
   specs, downloads each URL, and replaces stale hashes in `tool_runner.py`.

Designed to be called by Renovate `postUpgradeTasks` after version bumps,
but also works standalone for manual checksum updates.
"""

from __future__ import annotations

import hashlib
import logging
import re
import sys
from contextlib import nullcontext
from pathlib import Path
from urllib.request import Request, urlopen

from click_extra import progressbar

from .tool_runner import TOOL_REGISTRY

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Iterator

# Matches GitHub release download URLs in quoted strings.
_URL_PATTERN = re.compile(
    r"https://github\.com/[^\"]+/releases/download/[^\"]+",
)

# Matches a 64-character lowercase hex SHA-256 hash.
_HASH_PATTERN = re.compile(r"[a-f0-9]{64}")

# How many lines after a URL to search for the sha256sum check line.
_HASH_SEARCH_WINDOW = 5


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


def _find_checksum_pairs(lines: list[str]) -> Iterator[tuple[str, int, str]]:
    """Find (url, hash_line_index, old_hash) triples in workflow file lines.

    For each GitHub release URL found, searches the next few lines for a
    `sha256sum --check` line. The hash may be on that same line (single-line
    pattern) or on a preceding line (multi-line `echo ... | sha256sum`).

    :param lines: Lines of the workflow file.
    :return: Iterator of (url, hash_line_index, old_hash) triples.
    """
    for i, line in enumerate(lines):
        for url_match in _URL_PATTERN.finditer(line):
            url = url_match.group()
            # Search subsequent lines for a sha256sum check.
            for j in range(i + 1, min(i + 1 + _HASH_SEARCH_WINDOW, len(lines))):
                if "sha256sum" in lines[j]:
                    hash_match = _HASH_PATTERN.search(lines[j])
                    if hash_match:
                        yield url, j, hash_match.group()
                        break
                    # Hash may be on a preceding line (multi-line echo|sha256sum).
                    for k in range(j - 1, i, -1):
                        hash_match = _HASH_PATTERN.search(lines[k])
                        if hash_match:
                            yield url, k, hash_match.group()
                            break
                    break


def update_checksums(file_path: Path) -> list[tuple[str, str, str]]:
    """Update SHA-256 checksums in a workflow file.

    :param file_path: Path to the workflow YAML file.
    :return: List of (url, old_hash, new_hash) for each updated checksum.
        Empty if all checksums are already correct.
    """
    content = file_path.read_text(encoding="UTF-8")
    lines = content.splitlines()
    updated: list[tuple[str, str, str]] = []
    pairs = list(_find_checksum_pairs(lines))

    progress = (
        progressbar(
            pairs,
            label="Verifying checksums",
            file=sys.stderr,
        )
        if sys.stderr.isatty()
        else nullcontext(pairs)
    )
    with progress as items:
        for url, hash_line_idx, old_hash in items:
            logging.info(f"Verifying checksum for {url}")
            new_hash = _download_sha256(url)

            if old_hash != new_hash:
                lines[hash_line_idx] = lines[hash_line_idx].replace(
                    old_hash,
                    new_hash,
                )
                updated.append((url, old_hash, new_hash))
                logging.info(f"Updated checksum: {old_hash} -> {new_hash}")
            else:
                logging.info("Checksum unchanged.")

    if updated:
        file_path.write_text("\n".join(lines) + "\n", encoding="UTF-8")

    return updated


def update_registry_checksums(registry_path: Path) -> list[tuple[str, str, str]]:
    """Update SHA-256 checksums for binary tools in the tool runner registry.

    Iterates all `TOOL_REGISTRY` entries with `binary` specs, downloads
    each URL, computes the SHA-256, and replaces stale hashes in-place in the
    Python source file.

    :param registry_path: Path to `tool_runner.py`.
    :return: List of (url, old_hash, new_hash) for each updated checksum.
        Empty if all checksums are already correct.
    """
    content = registry_path.read_text(encoding="UTF-8")
    updated: list[tuple[str, str, str]] = []

    # Flatten all binary platform entries for progress tracking.
    entries = [
        (spec, pk, tmpl.format(version=spec.version), spec.binary.checksums[pk])
        for spec in TOOL_REGISTRY.values()
        if spec.binary is not None
        for pk, tmpl in spec.binary.urls.items()
    ]

    progress = (
        progressbar(
            entries,
            label="Verifying checksums",
            file=sys.stderr,
        )
        if sys.stderr.isatty()
        else nullcontext(entries)
    )
    with progress as items:
        for spec, platform_key, url, old_hash in items:
            logging.info(
                f"Verifying registry checksum for {spec.name} ({platform_key})"
            )
            new_hash = _download_sha256(url)

            if old_hash != new_hash:
                content = content.replace(old_hash, new_hash)
                updated.append((url, old_hash, new_hash))
                logging.info(f"Updated checksum: {old_hash} -> {new_hash}")
            else:
                logging.info("Checksum unchanged.")

    if updated:
        registry_path.write_text(content, encoding="UTF-8")

    return updated
