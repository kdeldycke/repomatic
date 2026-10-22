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

Downloads the release artifact of each platform of the binary tools, and
rewrites stale hashes in place in `tooling/registry.py` (alongside the
`VERSIONS` stamps). `repomatic update-checksums` verifies every binary tool.
`sync-tool-versions` passes the tools it bumped, with their new versions, so a
version bump and its matching checksums land in one pass.

Before a digest is recorded, the download of a tool whose upstream attests its
releases is verified against that attestation: see
{class}`~repomatic.tooling.registry.Attestation`. A failed verification aborts
the run like a failed download, and nothing is rewritten.
"""

from __future__ import annotations

import logging
import re
import tempfile
from functools import partial
from pathlib import Path

from click_extra import OperationTrail, run_jobs

from ..file_ops import write_if_changed
from ..github.gh import GhCommandError, run_gh_command
from ..http import download
from .registry import TOOL_REGISTRY

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from .registry import ResolvedBinary, ToolSpec


class AttestationError(RuntimeError):
    """A release asset whose upstream attestation did not verify."""


def verify_attestation(resolved: ResolvedBinary, path: Path) -> None:
    """Verify the upstream attestation of the download of *resolved* at *path*.

    Runs the pinned `gh`, which checks the Sigstore signature of the
    attestation and that its subject is the digest of *path*.

    :raises AttestationError: The attestation is missing, or does not cover
        the bytes at *path*.
    """
    attestation = resolved.attestation
    assert attestation is not None
    try:
        run_gh_command(attestation.gh_args(resolved, path))
    except GhCommandError as error:
        repo, tag = resolved.release_source
        msg = (
            f"{resolved.archive_name} of {repo} {tag}: the {attestation.value} did"
            f" not verify, so its digest is not recorded. {error}"
        )
        raise AttestationError(msg) from error
    logging.info(f"Verified the {attestation.value} of {resolved.archive_name}.")


def _download_sha256(url: str, verify: Callable[[Path], None] | None = None) -> str:
    """Download a URL and return its SHA-256 hex digest.

    Streams through {func}`repomatic.http.download` into a scratch file,
    inheriting its retries, its stall timeout and its `Content-Length`
    truncation guard. The retries keep one transient failure from ending a
    batch over every binary of the registry. A short body must fail loudly
    here: its digest would otherwise be written into `tooling/registry.py` as
    the new canonical checksum, poisoning every later verified install. No
    progress feedback: the caller fans downloads out under its own
    `OperationTrail`.

    :param url: The URL to download.
    :param verify: Called on the downloaded file before it is deleted, to
        check it against an upstream attestation. The file keeps the name of
        the asset, as a verifier may read it.
    :return: Lowercase hex SHA-256 digest of the response body.
    :raises repomatic.http.FetchError: If the download fails.
    :raises AttestationError: If *verify* rejects the download.
    """
    with tempfile.TemporaryDirectory(prefix="repomatic-checksum-") as scratch:
        artifact = Path(scratch) / (url.rsplit("/", 1)[-1] or "artifact")
        digest = download(url, artifact)
        if verify is not None:
            verify(artifact)
    logging.debug(f"SHA-256 of {url}: {digest}")
    return digest


def checksum_targets(
    version_overrides: Mapping[str, str] | None = None,
) -> list[tuple[ToolSpec, ResolvedBinary]]:
    """List the release artifacts whose checksums the registry records.

    :param version_overrides: Tool name to the version to download instead of
        the pinned one. When given, only these tools are listed: the other
        tools keep the checksums of their pinned version.
    :return: `(spec, download)` for each platform of each selected tool, where
        the download names the artifact URL and the checksum on record.
    """
    targets: list[tuple[ToolSpec, ResolvedBinary]] = []
    for spec in TOOL_REGISTRY.values():
        binary = spec.binary
        if binary is None:
            continue
        target = spec
        if version_overrides is not None:
            if spec.name not in version_overrides:
                continue
            target = spec.with_overrides(version=version_overrides[spec.name])
        targets.extend((target, target.resolve_binary(key)) for key in binary.urls)
    return targets


def update_registry_checksums(
    registry_path: Path,
    version_overrides: Mapping[str, str] | None = None,
    *,
    jobs: int = 1,
) -> list[tuple[str, str, str]]:
    """Recompute binary checksums and version stamps in `tooling/registry.py`.

    Downloads each artifact {func}`checksum_targets` lists, computes its
    SHA-256, and replaces stale hashes in-place. Also reconciles the `VERSIONS`
    stamp of each tool it verified with the version the checksums were
    computed for, the basis of the offline staleness test.

    :param registry_path: Path to `tooling/registry.py`.
    :param version_overrides: Optional mapping of tool name to a version to
        download instead of the in-memory `ToolSpec.version`, which limits the
        run to these tools. `sync-tool-versions` passes this so it can bump the
        version in the source and refresh the checksums in a single process:
        the in-memory registry still holds the pre-bump version because the
        file was edited, not reimported.
    :param jobs: How many downloads run at once.
    :return: List of `(url, old_hash, new_hash)` for each updated checksum.
        Empty if all checksums are already correct.
    """
    targets = checksum_targets(version_overrides)
    original = registry_path.read_text(encoding="UTF-8")
    content = original
    updated: list[tuple[str, str, str]] = []

    def target_sha256(target: tuple[ToolSpec, ResolvedBinary]) -> str:
        spec, resolved = target
        logging.info(
            f"Verifying registry checksum for {spec.name} ({resolved.cache_key})"
        )
        verify = partial(verify_attestation, resolved) if resolved.attestation else None
        return _download_sha256(resolved.url, verify)

    # run_jobs yields digests in submission order, so the rewrite loop below
    # stays deterministic while the downloads overlap. A download that raises
    # still aborts the batch (no partial rewrite), leaving the trail's completed
    # rows on screen.
    with OperationTrail(
        label="Verifying", unit="checksums", total=len(targets), jobs=jobs
    ) as trail:
        for (spec, resolved), new_hash in zip(
            targets, run_jobs(target_sha256, targets, jobs=jobs)
        ):
            trail.mark(True, f"{spec.name} ({resolved.cache_key})")
            old_hash = resolved.checksum
            if old_hash is None or old_hash == new_hash:
                logging.info("Checksum unchanged.")
                continue
            content = content.replace(old_hash, new_hash)
            updated.append((resolved.url, old_hash, new_hash))
            logging.info(f"Updated checksum: {old_hash} -> {new_hash}")
        trail.finish(True, f"Verified {trail.ok_count}/{len(targets)} checksums")

    # Reconcile the VERSIONS stamp of each verified tool with its version
    # (idempotent). The key pattern only matches the quoted-string values in
    # `VERSIONS`, never the `ToolSpec(` registry entries or the tuple-keyed
    # `CHECKSUMS` entries.
    for spec in {spec.name: spec for spec, _ in targets}.values():
        content = re.sub(
            rf'("{re.escape(spec.name)}":\s*)"[^"]*"',
            rf'\g<1>"{spec.version}"',
            content,
        )

    write_if_changed(registry_path, content, previous=original)
    return updated
