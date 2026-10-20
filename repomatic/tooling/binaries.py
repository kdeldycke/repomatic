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

"""Download, verify and cache the binary tools of the registry.

The binary half of `repomatic run`: each tool the registry ships as a
platform-specific executable is downloaded from its release page, checked
against the SHA-256 the registry records, extracted, and stored in the shared
cache. {func}`ensure_binary` is the entry point for code that shells out to one
of these tools without going through {func}`repomatic.tooling.runner.run_tool`.

The module imports neither the runner nor the metadata layer, so the `gh`
resolver of {mod}`repomatic.github.gh` can import it at the top of its module.
"""

from __future__ import annotations

import functools
import logging
import sys
import tarfile
import tempfile
import zipfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from click_extra import Spinner, progressbar

from ..cache import (
    binary_sidecar_path,
    get_cached_binary,
    store_binary,
)
from ..hashing import compute_file_sha256
from ..http import FetchError, download
from .registry import (
    ArchiveFormat,
    BinaryInstallError,
    lookup_tool,
)

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Callable, Generator

    from .registry import ResolvedBinary, ToolSpec


class ChecksumMismatchError(BinaryInstallError):
    """Raised when a download does not hash to the SHA-256 the registry records.

    Never retried: the same bytes fail the same way. A mismatch points at a
    stale pin, a moved release asset or a tampered artifact.
    """


_STAGING_DIRS: list[tempfile.TemporaryDirectory[str]] = []
"""Staging directories still backing a path {func}`install_binary` returned.

Only populated when the binary stays out of the cache (`--no-cache`, a Docker
overlay mount losing the cache write, an unwritable cache root). Keeping the
objects referenced pins the directories until interpreter exit, when their
finalizers reclaim them. Dropping them at function return would delete the
binary the caller is about to run.
"""


@contextmanager
def _progress(label: str, total: int) -> Generator[Callable[[int], object], None, None]:
    """Draw the feedback of one download.

    A determinate bar when the server sends a `Content-Length`, a spinner
    otherwise. Both stay silent off a terminal (CI logs, pipes, captured test
    output).

    :param label: Text beside the bar or the spinner.
    :param total: Advertised length of the download, `0` when unknown.
    :return: The callable that advances the bar by the size of a chunk.
    """
    if total:
        with progressbar(length=total, label=label, file=sys.stderr) as bar:
            yield bar.update
    else:
        # The spinner animates on a thread of its own: no chunk advances it.
        with Spinner(label):
            yield lambda size: None


def _download_and_verify(
    url: str,
    expected_sha256: str | None,
    dest_path: Path,
    *,
    label: str | None = None,
) -> None:
    """Download a file and verify its SHA-256 checksum.

    {func}`repomatic.http.download` retries a network failure. A checksum
    mismatch on a complete body is not retried, since identical bytes would
    fail identically.

    :param url: URL to download.
    :param expected_sha256: Expected lowercase hex SHA-256 digest.
        `None` skips verification (logs the computed digest for reference).
    :param dest_path: Where to write the downloaded file.
    :param label: Progress bar label. Defaults to the destination filename.
    :raises BinaryInstallError: If the download fails.
    :raises ChecksumMismatchError: If the checksum does not match.
    """
    try:
        actual = download(
            url,
            dest_path,
            progress=functools.partial(_progress, label or dest_path.name),
        )
    except FetchError as exc:
        msg = f"Cannot download {url}: {exc}"
        raise BinaryInstallError(msg) from exc
    if expected_sha256 is None:
        logging.info(f"SHA-256 of {url}: {actual} (not verified).")
        return
    if actual != expected_sha256:
        dest_path.unlink(missing_ok=True)
        msg = f"SHA-256 mismatch for {url}: expected {expected_sha256}, got {actual}"
        raise ChecksumMismatchError(msg)
    logging.debug(f"SHA-256 verified for {url}: {actual}")


def _check_member_safety(member_path: str) -> None:
    """Reject archive members with path traversal or absolute paths.

    :raises BinaryInstallError: If the member path is unsafe.
    """
    parts = PurePosixPath(member_path).parts
    if ".." in parts or member_path.startswith("/"):
        msg = f"Unsafe archive member path: {member_path}"
        raise BinaryInstallError(msg)


def _stripped(member_path: str, components: int) -> str | None:
    """Return *member_path* without its first *components* parts.

    :return: The shortened path, or `None` when no part is left.
    """
    parts = PurePosixPath(member_path).parts
    if len(parts) <= components:
        return None
    return str(PurePosixPath(*parts[components:]))


def _finalize_extracted(
    member_path: str,
    dest_dir: Path,
    target: str,
) -> Path:
    """Rename an extracted member to its final location and make executable.

    :param member_path: Archive member path as extracted.
    :param dest_dir: Directory the member was extracted into.
    :param target: Expected (stripped) filename.
    :return: Final path to the executable.
    """
    extracted = dest_dir / member_path
    final = dest_dir / PurePosixPath(target).name
    if extracted != final:
        extracted.rename(final)
    final.chmod(0o755)
    return final


def _extract_from_tar(
    archive_path: Path, resolved: ResolvedBinary, dest_dir: Path
) -> Path:
    """Extract a tool executable from a tar archive."""
    with tarfile.open(archive_path, resolved.archive_format.tarfile_mode()) as tar:
        for member in tar.getmembers():
            stripped = _stripped(member.name, resolved.strip_components)
            if stripped == resolved.executable:
                _check_member_safety(member.name)
                # The extraction filter of PEP 706 reached 3.11 in a patch
                # release, so only a feature test tells whether it is there.
                if hasattr(tarfile, "data_filter"):
                    tar.extract(member, dest_dir, filter="data")
                else:
                    tar.extract(member, dest_dir)
                return _finalize_extracted(member.name, dest_dir, stripped)

    msg = f"Executable {resolved.executable!r} not found in archive"
    raise BinaryInstallError(msg)


def _extract_from_zip(
    archive_path: Path, resolved: ResolvedBinary, dest_dir: Path
) -> Path:
    """Extract a tool executable from a ZIP archive."""
    with zipfile.ZipFile(archive_path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            stripped = _stripped(info.filename, resolved.strip_components)
            if stripped in resolved.archive_members:
                _check_member_safety(info.filename)
                zf.extract(info, dest_dir)
                return _finalize_extracted(info.filename, dest_dir, stripped)

    msg = f"Executable {resolved.executable!r} not found in archive"
    raise BinaryInstallError(msg)


def _extract_binary(
    archive_path: Path, resolved: ResolvedBinary, dest_dir: Path
) -> Path:
    """Extract the tool executable from a downloaded archive.

    A `RAW` download is the executable itself, renamed into place.

    :param archive_path: Path to the downloaded archive file.
    :param resolved: The download, which names the executable and the format.
    :param dest_dir: Directory to extract into.
    :return: Path to the extracted executable.
    :raises BinaryInstallError: If the archive cannot be read, or holds no
        executable of that name, or holds it at an unsafe path.
    """
    try:
        if resolved.archive_format is ArchiveFormat.RAW:
            dest = dest_dir / resolved.executable
            archive_path.rename(dest)
            dest.chmod(0o755)
            return dest
        if resolved.archive_format is ArchiveFormat.ZIP:
            return _extract_from_zip(archive_path, resolved, dest_dir)
        return _extract_from_tar(archive_path, resolved, dest_dir)
    except (OSError, tarfile.TarError, zipfile.BadZipFile) as exc:
        msg = f"Cannot extract {resolved.executable} from {archive_path.name}: {exc}"
        raise BinaryInstallError(msg) from exc


def _write_binary_sidecar(binary_path: Path) -> None:
    """Compute and write the SHA-256 sidecar for a cached binary.

    Called after a verified archive download + extraction + cache store.
    The sidecar is the trust anchor for subsequent cache hits: it stores the
    digest of the *extracted* binary, distinct from the archive checksum in
    the registry. The archive checksum defends against supply-chain tampering
    at download time; the sidecar defends against local cache tampering
    between runs. The sidecar's location is cache layout, so
    {func}`~repomatic.cache.binary_sidecar_path` owns it.
    """
    digest = compute_file_sha256(binary_path)
    sidecar = binary_sidecar_path(binary_path)
    sidecar.write_text(digest, encoding="UTF-8")
    logging.debug(f"Wrote binary sidecar: {sidecar} ({digest}).")


def _verify_cached_binary(path: Path) -> bool:
    """Verify a cached binary against its `.sha256` sidecar.

    :param path: Path to the cached binary.
    :return: `True` if the sidecar exists and the digest matches.
    """
    sidecar = binary_sidecar_path(path)
    if not sidecar.is_file():
        return False
    expected = sidecar.read_text(encoding="UTF-8").strip()
    return compute_file_sha256(path) == expected


def _cached_binary(
    spec: ToolSpec, resolved: ResolvedBinary, *, verify: bool
) -> Path | None:
    """Return the cached executable of *spec*, or `None` to download it.

    :param verify: Check the cached binary against its digest sidecar. A
        binary that fails the check is deleted, sidecar included.
    """
    cached = next(
        (
            path
            for name in resolved.cached_names
            if (
                path := get_cached_binary(
                    spec.name, spec.version, resolved.cache_key, name
                )
            )
            is not None
        ),
        None,
    )
    if cached is None:
        return None
    if not verify:
        logging.info(
            f"Using cached {spec.name} {spec.version} for {resolved.cache_key}"
            " (checksum skipped)."
        )
        return cached
    if _verify_cached_binary(cached):
        logging.info(
            f"Using cached {spec.name} {spec.version} for {resolved.cache_key}"
            " (sidecar verified)."
        )
        return cached
    logging.warning(
        f"Cached {spec.name} {spec.version} failed integrity check, re-downloading."
    )
    cached.unlink(missing_ok=True)
    binary_sidecar_path(cached).unlink(missing_ok=True)
    return None


def _store(spec: ToolSpec, resolved: ResolvedBinary, extracted: Path) -> Path | None:
    """Copy the extracted binary into the cache, with its digest sidecar.

    A containerized CI runner can lose a cached file to an overlay filesystem
    or a mount restriction, so the stored copy has to exist before it is used.

    :return: The cached executable, or `None` when the cache cannot hold it.
    """
    cached = store_binary(spec.name, spec.version, resolved.cache_key, extracted)
    if cached is None or not cached.is_file():
        logging.warning(
            "Cached binary unavailable after store at "
            f"{cached if cached is not None else 'cache'}, using temp path."
        )
        return None
    try:
        _write_binary_sidecar(cached)
    except OSError as exc:
        # The next run finds no sidecar and downloads the binary again.
        logging.warning(f"Cannot write the digest sidecar of {cached}: {exc}")
    return cached


def install_binary(
    spec: ToolSpec, *, skip_checksum: bool = False, no_cache: bool = False
) -> Path:
    """Download, verify and extract a binary tool, and return its executable.

    Two-layer integrity model:

    - **Archive checksum** (download time): the registry checksum is verified
      against the downloaded archive. This defends against supply-chain
      tampering and is auditable against the upstream release page.
    - **Binary sidecar** (cache hit): after a verified download, extraction,
      and cache store, a `.sha256` sidecar is written next to the cached
      binary. Subsequent cache hits verify the binary against this sidecar,
      defending against local cache tampering between runs.

    The download goes through a staging directory. When the binary stays out
    of the cache, the staging copy is returned, and its directory lasts as long
    as the process: see {data}`_STAGING_DIRS`.

    :param spec: Tool specification with `binary` set.
    :param skip_checksum: Skip SHA-256 verification when `True`.
    :param no_cache: Bypass the cache entirely when `True`.
    :return: Path to the ready-to-run executable.
    :raises BinaryInstallError: On any failure to install the binary. The
        subclasses {class}`~repomatic.tooling.registry.UnsupportedPlatformError`
        and {class}`ChecksumMismatchError` name the two failures a caller may
        handle apart.
    """
    resolved = spec.resolve_binary()
    checksum = None if skip_checksum else resolved.checksum
    if not skip_checksum and checksum is None:
        # Fail closed on a spec that maps the platform to a URL but records no
        # digest for it. Only a hand-built spec gets here: the registry enforces
        # URL and checksum key parity.
        msg = (
            f"No SHA-256 recorded for {spec.name} {spec.version}"
            f" on {resolved.cache_key}."
        )
        raise BinaryInstallError(msg)

    if not no_cache:
        cached = _cached_binary(spec, resolved, verify=not skip_checksum)
        if cached is not None:
            return cached

    logging.info(f"Downloading {spec.name} {spec.version} for {resolved.cache_key}...")
    if skip_checksum:
        logging.warning(f"Checksum verification skipped for {spec.name}.")
    staging = tempfile.TemporaryDirectory(prefix=f"repomatic-{spec.name}-bin-")
    staging_dir = Path(staging.name)
    try:
        archive = staging_dir / resolved.archive_name
        _download_and_verify(
            resolved.url, checksum, archive, label=f"{spec.name} {spec.version}"
        )
        extracted = _extract_binary(archive, resolved, staging_dir)
    except BinaryInstallError:
        staging.cleanup()
        raise

    if not no_cache:
        stored = _store(spec, resolved, extracted)
        if stored is not None:
            staging.cleanup()
            return stored
    _STAGING_DIRS.append(staging)
    return extracted


@functools.cache
def ensure_binary(name: str) -> Path:
    """Install a registry binary tool and return the path to its executable.

    The seam for repomatic code that shells out to a third-party binary but is
    not itself a {func}`~repomatic.tooling.runner.run_tool` invocation. It
    buys the same guarantees every `repomatic run` binary gets: the
    registry-pinned version, its archive verified against the recorded
    SHA-256, and a shared cache so repeated calls in one run download once.

    Prefer this over looking the tool up on `PATH`. Whatever `PATH` offers is
    whichever version the machine or CI image happens to carry, unpinned and
    unverified, and it differs between a developer's laptop and every runner.

    Memoized per tool name: callers in a loop (`format-images` optimizing one
    PNG per call) hit the install-and-verify path once per process, not once
    per file. The memo ignores the cache root, which a process does not change.
    Failures are not memoized, so a transient download error can be retried.

    :param name: Registry key of a tool whose
        {class}`~repomatic.tooling.registry.ToolSpec` declares a `binary`.
    :return: Absolute path to the ready-to-run executable.
    :raises ValueError: If the tool is unknown or ships no binary.
    :raises BinaryInstallError: If the binary cannot be installed.
    """
    spec = lookup_tool(name)
    if spec.binary is None:
        msg = f"{name!r} is not a binary tool in the repomatic registry."
        raise ValueError(msg)
    return install_binary(spec)
