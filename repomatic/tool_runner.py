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

"""Unified tool runner with managed config resolution.

Provides `repomatic run <tool>` — a single entry point that installs an
external tool at a pinned version, resolves its configuration through a strict
4-level precedence chain, translates `[tool.X]` sections from
`pyproject.toml` into the tool's native format, and invokes the tool with
the resolved config. The tool catalog it drives (`ToolSpec` entries, pinned
versions, checksums) lives in `tool_registry.py`.

```{important}
Config resolution precedence (first match wins, no merging):

1. **Native config file** — tool's own config file in the repo.
2. **`[tool.X]` in `pyproject.toml`** — translated to native format.
3. **Bundled default** — from `repomatic/data/`.
4. **Bare invocation** — no config at all.
```
"""

from __future__ import annotations

import functools
import hashlib
import logging
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from contextlib import nullcontext
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.request import Request, urlopen

import tomlrt
from click_extra import ClickException, Spinner, config_table_to_flags, progressbar
from extra_platforms import is_github_ci
from packaging.version import Version

from .binary import compute_file_sha256
from .bundle import get_data_file_path
from .cache import (
    binary_sidecar_path,
    get_cached_binary,
    store_binary,
    store_config,
)
from .config import load_repomatic_config
from .metadata import Metadata
from .tool_registry import (
    NPM_MIN_VERSION_FOR_COOLDOWN,
    TOOL_REGISTRY,
    ArchiveFormat,
    BinarySpec,
    NativeFormat,
    ToolSpec,
    UnsupportedPlatformError,
)
from .uv import uv_cmd, uvx_cmd
from .version_sync import exclude_newer_cutoff, min_release_age_days

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Sequence
    from typing import Any


# ---------------------------------------------------------------------------
# Config resolution
# ---------------------------------------------------------------------------


def load_pyproject_tool_section(tool_name: str) -> dict[str, Any]:
    """Load `[tool.<tool_name>]` from `pyproject.toml` in the current directory.

    Returns the live `tomlrt.Table` (a `dict` subclass) rather than a plain-dict
    copy, so the section keeps its comment trivia for formats that can preserve
    it on materialization (see {meth}`NativeFormat.serialize`). Callers that only
    read values or test truthiness are unaffected.

    :return: The tool's config table, or empty dict if not found.
    """
    pyproject_path = Path("pyproject.toml")
    if not pyproject_path.exists():
        logging.debug("No pyproject.toml found in CWD.")
        return {}
    pyproject_data = tomlrt.loads(pyproject_path.read_text(encoding="UTF-8"))
    tool_section: dict[str, Any] = pyproject_data.get("tool", {}).get(tool_name, {})
    if tool_section:
        logging.debug("[tool.%s] found in pyproject.toml: %r", tool_name, tool_section)
    else:
        logging.debug("No [tool.%s] section in pyproject.toml.", tool_name)
    return tool_section


def _config_filename(spec: ToolSpec) -> str:
    """Derive the canonical config filename for the cache.

    Uses the first `native_config_files` entry (without leading dots or path
    components) if available, otherwise constructs from the native format
    extension.
    """
    if spec.native_config_files:
        return Path(spec.native_config_files[0]).name.lstrip(".")
    return f"{spec.name}.{spec.native_format.value}"


def _store_config_to_cache(
    spec: ToolSpec,
    content: str,
) -> tuple[list[str], Path | None]:
    """Write config content to the cache and return CLI args.

    :return: `([flag, cache_path], None)` on success, or falls back to a temp
        file returning `([flag, tmp_path], tmp_path)` if the cache is not
        writable.
    """
    assert spec.config_flag is not None
    filename = _config_filename(spec)
    cached = store_config(spec.name, filename, content)
    if cached is not None:
        logging.info(
            "%s: config cached at %s, passing via %s.",
            spec.name,
            cached,
            spec.config_flag,
        )
        return [spec.config_flag, str(cached)], None

    # Cache not writable — fall back to temp file.
    logging.warning(
        "%s: cache directory not writable, falling back to temp file.",
        spec.name,
    )
    with tempfile.NamedTemporaryFile(
        encoding="UTF-8",
        mode="w",
        suffix=f".{spec.native_format.value}",
        prefix=f"repomatic-{spec.name}-",
        delete=False,
    ) as tmp:
        tmp.write(content)
    tmp_path = Path(tmp.name)
    return [spec.config_flag, str(tmp_path)], tmp_path


def _write_cwd_config(spec: ToolSpec, content: str, level: int) -> Path:
    """Write config content to the first native config path for CWD-discovery.

    For tools without a `--config` flag. The caller must clean up the file
    after the tool exits.

    :param spec: Tool specification (must have `native_config_files`).
    :param content: Config file content to write.
    :param level: Precedence level (2 or 3) for the log message.
    :return: Path to the written file.
    """
    target = Path(spec.native_config_files[0])
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="UTF-8")
    logging.warning(
        "%s: wrote config to repo at %s (level %d). "
        "This tool has no --config flag; the file will be removed "
        "after the run.",
        spec.name,
        target.resolve(),
        level,
    )
    return target


def _deliver_config(
    spec: ToolSpec,
    content: str,
    level: int,
) -> tuple[list[str], Path | None]:
    """Deliver resolved config content via the appropriate mechanism.

    Tools with `config_flag` get a cached file passed via CLI. Tools with
    only `native_config_files` get a CWD file that must be cleaned up.

    :param spec: Tool specification.
    :param content: Config file content.
    :param level: Precedence level for logging.
    :return: Tuple of (extra CLI args, cleanup path or None).
    """
    if spec.config_flag:
        return _store_config_to_cache(spec, content)

    if spec.native_config_files:
        return [], _write_cwd_config(spec, content, level)

    msg = (
        f"{spec.name} has config content at level {level} but no config_flag "
        f"and no native_config_files to write to."
    )
    raise NotImplementedError(msg)


def resolve_config(
    spec: ToolSpec,
    tool_config: dict[str, Any] | None = None,
) -> tuple[list[str], Path | None]:
    """Resolve config for a tool using the 4-level precedence chain.

    :param spec: Tool specification.
    :param tool_config: Pre-loaded `[tool.X]` config dict. If `None`,
        reads from `pyproject.toml` in the current directory.
    :return: Tuple of (extra CLI args for config, path to clean up).
        The path is `None` when no cleanup is needed (cache-based configs
        persist across runs). Non-`None` paths are CWD files written for
        tools that have no `--config` flag.
    """
    # Level 1: Native config file exists in the repo.
    for config_file in spec.native_config_files:
        if Path(config_file).exists():
            logging.info(
                "%s: using native config file: %s (level 1).", spec.name, config_file
            )
            return [], None

    # Level 2: [tool.X] in pyproject.toml.
    if tool_config is None:
        tool_config = load_pyproject_tool_section(spec.name)

    if tool_config:
        if spec.reads_pyproject:
            logging.info(
                "%s: using [tool.%s] in pyproject.toml, read natively (level 2).",
                spec.name,
                spec.name,
            )
            return [], None

        if spec.native_format is NativeFormat.FLAGS:
            flags = config_table_to_flags(tool_config)
            logging.info(
                "%s: translated [tool.%s] to %d CLI flag(s) (level 2).",
                spec.name,
                spec.name,
                len(flags),
            )
            return flags, None

        content = spec.native_format.serialize(tool_config, tool_name=spec.name)
        logging.debug(
            "Translated [tool.%s] to %s:\n%s",
            spec.name,
            spec.native_format.value,
            content,
        )
        return _deliver_config(spec, content, level=2)

    # Level 3: Bundled default from repomatic/data/.
    if spec.default_config:
        with get_data_file_path(spec.default_config) as bundled_path:
            content = bundled_path.read_text(encoding="UTF-8")
        return _deliver_config(spec, content, level=3)

    # Level 4: Bare invocation.
    logging.info("%s: no config found, bare invocation (level 4).", spec.name)
    return [], None


# ---------------------------------------------------------------------------
# Binary download infrastructure
# ---------------------------------------------------------------------------


DOWNLOAD_TIMEOUT = 30
"""Socket-level timeout for artifact downloads, in seconds.

A stall guard, not a transfer budget: `urlopen` applies it to each blocking
socket operation, so a healthy multi-minute download is unaffected while a dead
connection fails in seconds instead of hanging a CI job to the runner ceiling.
Deliberately larger than {data}`repomatic.http.DEFAULT_TIMEOUT`, which is sized
for small JSON API responses.
"""

_DOWNLOAD_CHUNK_SIZE = 65536
"""Read size for streaming downloads and incremental hashing."""


def download_to(
    url: str,
    dest_path: Path,
    *,
    label: str | None = None,
    progress: bool = True,
) -> str:
    """Stream *url* into *dest_path* and return its SHA-256 hex digest.

    Chunked download with incremental hash computation, so large binaries never
    load fully into memory. Shows a progress bar on interactive terminals when
    the server provides a `Content-Length` header; pass `progress=False` from
    concurrent callers whose fan-out draws its own progress display.

    The single download seam for every artifact repomatic fetches by hand:
    whatever consumes the digest (verification in {func}`_download_and_verify`,
    checksum harvesting in `checksums.py`) builds on this so the truncation
    guard below applies to all of them. A short body (proxy hiccup, dropped
    connection) hashes to a wrong digest, so without the guard it would surface
    later as a checksum mismatch: that reads as a stale pin or a tampered
    artifact when nothing is wrong upstream. Name the real failure instead.

    :param url: URL to download.
    :param dest_path: Where to write the downloaded file.
    :param label: Progress bar label. Defaults to the destination filename.
    :param progress: Draw per-download feedback on interactive terminals.
    :return: Lowercase hex SHA-256 digest of the downloaded bytes.
    :raises OSError: If the body is shorter than the advertised `Content-Length`.
    """
    request = Request(url)
    sha256 = hashlib.sha256()
    bytes_read = 0
    with (
        urlopen(request, timeout=DOWNLOAD_TIMEOUT) as response,
        dest_path.open("wb") as f,
    ):
        content_length = response.headers.get("Content-Length")
        total = int(content_length) if content_length else 0
        # Determinate vs indeterminate feedback, both silent off a TTY (CI logs,
        # pipes, captured test output): with a Content-Length, click_extra's
        # progressbar draws a percentage bar; without one there is nothing to
        # measure, so a Spinner just signals the download is still alive.
        feedback: Any
        if not progress:
            feedback = nullcontext()
        elif total:
            feedback = progressbar(
                length=total, label=label or dest_path.name, file=sys.stderr
            )
        else:
            feedback = Spinner(label or dest_path.name)
        with feedback as bar:
            while chunk := response.read(_DOWNLOAD_CHUNK_SIZE):
                f.write(chunk)
                sha256.update(chunk)
                bytes_read += len(chunk)
                # Only the determinate progressbar advances per chunk; the
                # Spinner animates on its own background thread and the
                # nullcontext yields None, neither exposing update().
                if bar is not None and not isinstance(bar, Spinner):
                    bar.update(len(chunk))
    if total and bytes_read != total:
        dest_path.unlink(missing_ok=True)
        msg = f"Truncated download for {url}: got {bytes_read} of {total} bytes."
        raise OSError(msg)
    return sha256.hexdigest()


def _download_and_verify(
    url: str,
    expected_sha256: str | None,
    dest_path: Path,
    *,
    label: str | None = None,
) -> None:
    """Download a file and verify its SHA-256 checksum.

    :param url: URL to download.
    :param expected_sha256: Expected lowercase hex SHA-256 digest.
        `None` skips verification (logs the computed digest for reference).
    :param dest_path: Where to write the downloaded file.
    :param label: Progress bar label. Defaults to the destination filename.
    :raises OSError: If the body is shorter than the advertised `Content-Length`.
    :raises ValueError: If the checksum does not match.
    """
    actual = download_to(url, dest_path, label=label)
    if expected_sha256 is None:
        logging.info("SHA-256 of %s: %s (not verified).", url, actual)
        return
    if actual != expected_sha256:
        dest_path.unlink(missing_ok=True)
        msg = f"SHA-256 mismatch for {url}: expected {expected_sha256}, got {actual}"
        raise ValueError(msg)
    logging.debug("SHA-256 verified for %s: %s", url, actual)


def _check_member_safety(member_path: str) -> None:
    """Reject archive members with path traversal or absolute paths.

    :raises ValueError: If the member path is unsafe.
    """
    parts = PurePosixPath(member_path).parts
    if ".." in parts or member_path.startswith("/"):
        msg = f"Unsafe archive member path: {member_path}"
        raise ValueError(msg)


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


def _extract_binary(
    archive_path: Path,
    spec: BinarySpec,
    dest_dir: Path,
    tool_name: str,
    archive_format: ArchiveFormat,
    strip_components: int,
) -> Path:
    """Extract the tool executable from a downloaded archive.

    :param archive_path: Path to the downloaded archive file.
    :param spec: Binary specification with format and executable info.
    :param dest_dir: Directory to extract into.
    :param tool_name: Tool name, used as default for `archive_executable`.
    :param archive_format: Per-platform archive format, from
        `BinarySpec.get_archive_format`.
    :param strip_components: Per-platform strip count, from
        `BinarySpec.get_strip_components`.
    :return: Path to the extracted executable.
    :raises FileNotFoundError: If the executable is not found in the archive.
    """
    executable = spec.archive_executable or tool_name
    # Dispatch by archive format. A RAW download is the executable itself,
    # renamed into place; the archive formats delegate to their extractors.
    if archive_format is ArchiveFormat.RAW:
        dest = dest_dir / executable
        archive_path.rename(dest)
        dest.chmod(0o755)
        return dest
    if archive_format is ArchiveFormat.ZIP:
        return _extract_from_zip(archive_path, dest_dir, executable, strip_components)
    return _extract_from_tar(
        archive_path, archive_format, dest_dir, executable, strip_components
    )


def _extract_from_tar(
    archive_path: Path,
    fmt: ArchiveFormat,
    dest_dir: Path,
    executable: str,
    strip_components: int,
) -> Path:
    """Extract a tool executable from a tar archive."""
    with tarfile.open(str(archive_path), fmt.tarfile_mode()) as tar:
        for member in tar.getmembers():
            parts = PurePosixPath(member.name).parts
            if len(parts) <= strip_components:
                continue
            stripped = str(PurePosixPath(*parts[strip_components:]))
            if stripped == executable:
                _check_member_safety(member.name)
                if sys.version_info >= (3, 12):
                    tar.extract(member, dest_dir, filter="data")
                else:
                    tar.extract(member, dest_dir)
                return _finalize_extracted(member.name, dest_dir, executable)

    msg = f"Executable {executable!r} not found in archive"
    raise FileNotFoundError(msg)


def _extract_from_zip(
    archive_path: Path,
    dest_dir: Path,
    executable: str,
    strip_components: int,
) -> Path:
    """Extract a tool executable from a ZIP archive."""
    # Windows executables may have a .exe suffix inside the archive.
    targets = {executable, f"{executable}.exe"}

    with zipfile.ZipFile(archive_path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            parts = PurePosixPath(info.filename).parts
            if len(parts) <= strip_components:
                continue
            stripped = str(PurePosixPath(*parts[strip_components:]))
            if stripped in targets:
                _check_member_safety(info.filename)
                zf.extract(info, dest_dir)
                return _finalize_extracted(info.filename, dest_dir, stripped)

    msg = f"Executable {executable!r} not found in archive"
    raise FileNotFoundError(msg)


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
    logging.debug("Wrote binary sidecar: %s (%s).", sidecar, digest)


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


def _install_binary(
    spec: ToolSpec,
    tmp_dir: Path,
    skip_checksum: bool = False,
    no_cache: bool = False,
) -> Path:
    """Download, verify, and extract a binary tool.

    Two-layer integrity model:

    - **Archive checksum** (download time): the registry checksum is verified
      against the downloaded archive. This defends against supply-chain
      tampering and is auditable against the upstream release page.
    - **Binary sidecar** (cache hit): after a verified download, extraction,
      and cache store, a `.sha256` sidecar is written next to the cached
      binary. Subsequent cache hits verify the binary against this sidecar,
      defending against local cache tampering between runs.

    :param spec: Tool specification with `binary` set.
    :param tmp_dir: Temporary directory for download and extraction.
    :param skip_checksum: Skip SHA-256 verification when `True`.
    :param no_cache: Bypass the cache entirely when `True`.
    :return: Path to the ready-to-run executable.
    :raises RuntimeError: If no binary is available for the current platform.
    """
    binary = spec.binary
    assert binary is not None

    key = binary.resolve_platform()
    cache_key = BinarySpec.platform_cache_key(key)

    executable = binary.archive_executable or spec.name
    # Fail closed on a spec that maps the platform to a URL but records no
    # digest for it (only reachable through hand-built specs: the registry
    # enforces URL/checksum key parity). Falling through with an empty digest
    # would report a nonsensical "expected , got abc…" mismatch instead.
    checksum = None if skip_checksum else binary.checksums.get(key)
    if not skip_checksum and checksum is None:
        msg = f"No SHA-256 recorded for {spec.name} {spec.version} on {cache_key}."
        raise RuntimeError(msg)

    # Check cache (unless --no-cache).
    if not no_cache:
        cached = get_cached_binary(spec.name, spec.version, cache_key, executable)
        if cached is not None:
            if skip_checksum:
                logging.info(
                    "Using cached %s %s for %s (checksum skipped).",
                    spec.name,
                    spec.version,
                    cache_key,
                )
                return cached
            if _verify_cached_binary(cached):
                logging.info(
                    "Using cached %s %s for %s (sidecar verified).",
                    spec.name,
                    spec.version,
                    cache_key,
                )
                return cached
            # Sidecar missing or digest mismatch: re-download from source.
            logging.warning(
                "Cached %s %s failed integrity check, re-downloading.",
                spec.name,
                spec.version,
            )
            cached.unlink(missing_ok=True)
            binary_sidecar_path(cached).unlink(missing_ok=True)

    url = binary.urls[key].format(version=spec.version)

    # Derive archive filename from URL.
    archive_name = url.rsplit("/", 1)[-1]
    archive_path = tmp_dir / archive_name

    logging.info("Downloading %s %s for %s...", spec.name, spec.version, cache_key)
    if skip_checksum:
        logging.warning("Checksum verification skipped for %s.", spec.name)
    _download_and_verify(
        url,
        checksum,
        archive_path,
        label=f"{spec.name} {spec.version}",
    )

    fmt = binary.get_archive_format(key)
    extracted = _extract_binary(
        archive_path, binary, tmp_dir, spec.name, fmt, binary.get_strip_components(key)
    )

    # Store in cache for future use. Verify the cached copy is accessible
    # before returning it; fall back to the temp directory copy otherwise.
    # Docker-based CI runners (e.g., ubuntu-slim) can silently lose cached
    # files due to overlay filesystem or mount restrictions, and an unwritable
    # cache root makes store_binary return None.
    if not no_cache:
        cached = store_binary(spec.name, spec.version, cache_key, extracted)
        if cached is not None and cached.is_file():
            _write_binary_sidecar(cached)
            return cached
        logging.warning(
            "Cached binary unavailable after store at %s, using temp path.",
            cached if cached is not None else "cache",
        )
    return extracted


def _npm_supports_cooldown(npm: str) -> bool:
    """Whether the npm at *npm* honors `min-release-age` (npm 11.10.0+).

    Runs `npm --version` and compares against
    {data}`NPM_MIN_VERSION_FOR_COOLDOWN`. Assumes support when the version cannot
    be determined, so a parse or exec failure never emits a spurious warning.
    """
    try:
        raw = subprocess.run(
            [npm, "--version"],
            capture_output=True,
            text=True,
            encoding="UTF-8",
            check=False,
        ).stdout.strip()
        return Version(raw) >= Version(NPM_MIN_VERSION_FOR_COOLDOWN)
    except (ValueError, OSError):
        return True


def _install_npm(spec: ToolSpec, dest: Path, cooldown_days: int) -> Path:
    """Install an npm tool into *dest* and return its executable path.

    The npm counterpart to {func}`_install_binary`: installs
    `<package>@<version>` into a throwaway prefix, relying on npm's own
    per-tarball integrity checks (no repomatic-pinned checksum), and applies the
    `minimum-release-age` cooldown through npm's `min-release-age` when
    *cooldown_days* is non-zero. On npm older than
    {data}`NPM_MIN_VERSION_FOR_COOLDOWN` the gate is a no-op and the runner warns
    (via {func}`_npm_supports_cooldown`); the `lint-awesome` job provisions a
    new-enough npm so CI enforces it.

    ```{note}
    `--ignore-scripts` skips `preinstall`/`install`/`postinstall` scripts for
    the package and its whole transitive tree: the install-time-script vector
    behind most npm supply-chain worms. Safe here because `_install_npm` only
    ever installs a repomatic-pinned, cooldown-gated tool (unlike a
    general-purpose installer taking arbitrary package names, where a
    postinstall step can be load-bearing); see the sibling `meta-package-manager`
    project's `NPM` manager for that broader case, which cannot assume the same.
    The other flags match that project's npm `pre_args` for consistency,
    including `--no-audit` (disables sending the resolved dependency tree to
    the registry's audit endpoint on every install, not just log noise).
    ```

    :param dest: Directory to install into (`dest/node_modules/.bin/<exe>`).
    :param cooldown_days: `minimum-release-age` in whole days; `0` omits the gate.
    :return: Path to the installed executable.
    :raises RuntimeError: npm is absent, the install fails, or the executable is
        missing afterward.
    """
    npm = shutil.which("npm")
    if npm is None:
        raise RuntimeError(
            f"{spec.name} needs Node.js and npm on PATH, but npm was not found."
        )
    package_pin = f"{spec.package or spec.name}@{spec.version}"
    cmd = [
        npm,
        "install",
        "--ignore-scripts",
        "--no-progress",
        "--no-update-notifier",
        "--no-fund",
        "--no-audit",
        "--prefix",
        str(dest),
        package_pin,
    ]
    if cooldown_days:
        cmd.append(f"--min-release-age={cooldown_days}")
        if not _npm_supports_cooldown(npm):
            logging.warning(
                "npm on PATH is older than %s, so the minimum-release-age cooldown "
                "is not enforced; %s's transitive dependencies resolve without it.",
                NPM_MIN_VERSION_FOR_COOLDOWN,
                spec.name,
            )
    logging.info("Installing %s via npm: %s", spec.name, " ".join(cmd))
    result = subprocess.run(cmd, check=False)
    if result.returncode != 0:
        raise RuntimeError(
            f"npm install of {package_pin} failed with exit code {result.returncode}."
        )
    executable = spec.executable or spec.name
    bin_path = dest / "node_modules" / ".bin" / executable
    if not bin_path.exists():
        raise RuntimeError(
            f"{executable!r} not found under {dest} after installing {package_pin}."
        )
    return bin_path


# ---------------------------------------------------------------------------
# Tool invocation
# ---------------------------------------------------------------------------


def _build_install_args(spec: ToolSpec, exclude_newer: str | None = None) -> list[str]:
    """Build the command prefix for installing and running a tool.

    *exclude_newer* (a `YYYY-MM-DD` date) gates the isolated resolution by
    upload date, applying the `minimum-release-age` cooldown to the tool's
    transitive dependencies. It is ignored for `needs_venv` tools running from a
    lockfile, whose tree the frozen `uv.lock` already pins.

    ```{note}
    A `needs_venv` tool gets the project virtualenv only when the working
    directory holds a `uv.lock` to freeze it from. Without one, `uv run
    --frozen` aborts before the tool starts, and an unfrozen run would resolve
    the project live and write a brand-new `uv.lock` as a side effect. So the
    tool falls back to an isolated, cooldown-gated environment
    (`--no-project`), the same guarantee the `uvx` path provides: the tool
    still runs, it just cannot import the project's dependencies.
    ```
    """
    package_pin = f"{spec.package or spec.name}=={spec.version}"
    executable = spec.executable or spec.name

    if spec.needs_venv:
        if Path("uv.lock").is_file():
            cmd = uv_cmd("run", frozen=True)
        else:
            cmd = uv_cmd("run", no_project=True, exclude_newer=exclude_newer)
        if spec.module:
            cmd.extend(["--with", package_pin, "--", "python", "-m", spec.module])
        else:
            cmd.extend(["--with", package_pin, "--", executable])
    else:
        cmd = uvx_cmd(exclude_newer=exclude_newer)
        for pkg in spec.with_packages:
            cmd.extend(["--with", pkg])
        cmd.extend(["--from", package_pin, executable])

    return cmd


def _splice_config_args(
    config_args: list[str],
    extra_args: Sequence[str],
    spec: ToolSpec,
) -> list[str]:
    """Combine config and extra args in the order the tool expects.

    When `spec.config_after_subcommand` is `True` and `extra_args`
    starts with a subcommand name, config args are inserted after it::

        [subcommand] + config_args + [remaining extra_args]

    Otherwise config args come first (the default).
    """
    if spec.config_after_subcommand and config_args and extra_args:
        return [extra_args[0], *config_args, *extra_args[1:]]
    return [*config_args, *extra_args]


_STAGING_DIRS: list[tempfile.TemporaryDirectory[str]] = []
"""Staging directories still backing a path {func}`ensure_binary` handed out.

Only populated when the cache store fell back to the staging copy (a Docker
overlay mount losing the cache write, an unwritable cache root). Keeping the
objects referenced pins the directories until interpreter exit, when their
finalizers reclaim them; dropping them at function return would delete the
binary the caller is about to exec.
"""


@functools.cache
def ensure_binary(name: str) -> Path:
    """Install a registry binary tool and return the path to its executable.

    The seam for repomatic code that shells out to a third-party binary but is
    not itself a {func}`run_tool` invocation. It buys the same guarantees every
    `repomatic run` binary gets: the registry-pinned version, its archive
    verified against the recorded SHA-256, and a shared cache so repeated calls
    in one run download once.

    Prefer this over looking the tool up on `PATH`. Whatever `PATH` offers is
    whichever version the machine or CI image happens to carry, unpinned and
    unverified, and it differs between a developer's laptop and every runner.

    Memoized per tool name: callers in a loop (`format-images` optimizing one
    PNG per call) hit the install-and-verify path once per process, not once
    per file. Failures are not memoized, so a transient download error can be
    retried.

    :param name: Registry key of a tool whose
        {class}`~repomatic.tool_registry.ToolSpec` declares a `binary`.
    :return: Absolute path to the ready-to-run executable.
    :raises ClickException: If the tool is unknown, ships no binary, or cannot
        be downloaded and verified.
    """
    spec = TOOL_REGISTRY.get(name)
    if spec is None or spec.binary is None:
        msg = f"{name!r} is not a binary tool in the repomatic registry."
        raise ClickException(msg)
    # The staging dir is scratch for download and extraction. On the happy path
    # _install_binary returns the path inside the persistent cache and staging
    # is deleted right away; on the cache-store fallback it returns the staging
    # copy itself, which must then live as long as the process.
    staging = tempfile.TemporaryDirectory(prefix=f"repomatic-{name}-bin-")
    try:
        binary_path = _install_binary(spec, Path(staging.name))
    except RuntimeError as exc:
        staging.cleanup()
        raise ClickException(str(exc)) from exc
    if Path(staging.name) in binary_path.parents:
        _STAGING_DIRS.append(staging)
    else:
        staging.cleanup()
    return binary_path


def _path_tools_env(
    spec: ToolSpec,
    skip_checksum: bool,
    no_cache: bool,
    path_dirs: list[tempfile.TemporaryDirectory[str]],
) -> dict[str, str] | None:
    """Build the child environment exposing *spec*'s companion binaries.

    Installs each {attr}`~repomatic.tool_registry.ToolSpec.path_tools` entry
    through {func}`_install_binary`, so it arrives at the registry-pinned version
    with its checksum verified, then prepends the directories holding them to
    `PATH`. Prepending (rather than appending) is
    what makes the pin authoritative: a system-installed build of the same tool
    further down `PATH` is shadowed instead of winning by accident.

    Temporary directories are appended to *path_dirs* for the caller to clean up,
    since they must outlive this function and stay alive for the tool's run.

    A companion publishing no binary for the running platform is skipped with a
    warning rather than failing the run, since no retry can make it exist:
    `shfmt` ships nothing for Windows ARM64, and hard-failing there would leave
    `repomatic run mdformat` unusable on the platform instead of merely leaving
    its shell blocks unformatted. Every other install failure still aborts.

    :param spec: Specification of the tool about to run.
    :param skip_checksum: Skip SHA-256 verification, as for the primary tool.
    :param no_cache: Bypass the binary cache when `True`.
    :param path_dirs: Accumulator the caller cleans up in its `finally` block.
    :return: The environment to hand the child, or `None` to inherit unchanged
        when the tool declares no companions.
    :raises ClickException: If a companion fails to install for any reason other
        than the platform having no binary at all.
    """
    if not spec.path_tools:
        return None

    prefixes = []
    for tool_name in spec.path_tools:
        companion = TOOL_REGISTRY[tool_name]
        tmp_dir = tempfile.TemporaryDirectory(prefix=f"repomatic-{tool_name}-path-")
        path_dirs.append(tmp_dir)
        try:
            bin_path = _install_binary(
                companion,
                Path(tmp_dir.name),
                skip_checksum,
                no_cache=no_cache,
            )
        except UnsupportedPlatformError as exc:
            # The companion publishes nothing for this platform, so no run can
            # ever provision it here. Carrying on without it keeps the primary
            # tool working, degraded, instead of making it unusable on the
            # platform: mdformat still formats Markdown, leaving only its shell
            # blocks untouched.
            logging.warning("Skipping %s on PATH for %s: %s", tool_name, spec.name, exc)
            continue
        except RuntimeError as exc:
            msg = f"Cannot provision {tool_name} on PATH for {spec.name}: {exc}"
            raise ClickException(msg) from exc
        logging.info(
            "Exposing %s %s on PATH for %s.",
            tool_name,
            companion.version,
            spec.name,
        )
        prefixes.append(str(bin_path.parent))

    env = dict(os.environ)
    env["PATH"] = os.pathsep.join([*prefixes, env.get("PATH", "")])
    return env


def _dereference_data_dir_symlinks(
    config_args: list[str],
    path_dirs: list[tempfile.TemporaryDirectory[str]],
) -> list[str]:
    """Stage a symlink-free copy for any `--include-data-dir=SRC=DEST` flag.

    Nuitka's data-file copier preserves a symlink whose relative target still
    textually resolves "below" the distribution root, even when that subtree
    was never populated by any `--include-data-dir`/`--include-data-files`
    flag: `isFilenameBelowPath` in `nuitka.utils.FileOperations` only checks
    that the recomputed target does not climb above the root via `..`, never
    that the target actually exists there. The macOS codesign step then
    crashes: `signDistributionMacOS` calls `withMadeWritableFileMode` to make
    every distribution file writable, which runs a plain `os.stat` on each
    one; `os.stat` follows the dangling link, and the target was never
    copied.

    Staging a plain copy sidesteps the bug regardless of its root cause:
    `shutil.copytree(..., symlinks=False)` resolves every symlink to its
    target's real content, so Nuitka never sees a link to preserve. Applies
    to any `--include-data-dir` flag from any tool; only Nuitka emits this
    flag shape today, so it is a no-op for everything else.

    :param config_args: Flags resolved from `[tool.X]`, to scan and rewrite.
    :param path_dirs: Accumulator the caller cleans up in its `finally` block.
    :return: *config_args* with symlink-containing sources replaced by staged
        copies; entries with no symlinks pass through unchanged.
    """
    prefix = "--include-data-dir="
    fixed = []
    for arg in config_args:
        if arg.startswith(prefix):
            src, _, dest = arg[len(prefix) :].partition("=")
            src_path = Path(src)
            if src_path.is_dir() and any(
                member.is_symlink() for member in src_path.rglob("*")
            ):
                staging = tempfile.TemporaryDirectory(prefix="repomatic-data-dir-")
                path_dirs.append(staging)
                staged_src = Path(staging.name) / src_path.name
                shutil.copytree(src_path, staged_src, symlinks=False)
                fixed.append(f"{prefix}{staged_src}={dest}")
                continue
        fixed.append(arg)
    return fixed


def run_tool(
    name: str,
    extra_args: Sequence[str] = (),
    version: str | None = None,
    checksum: str | None = None,
    skip_checksum: bool = False,
    no_cache: bool = False,
) -> int:
    """Run an external tool with managed config resolution.

    :param name: Tool name (must be in `TOOL_REGISTRY`).
    :param extra_args: Extra arguments passed through to the tool.
    :param version: Override the pinned version.
    :param checksum: Override the SHA-256 checksum for the current platform.
    :param skip_checksum: Skip SHA-256 verification entirely.
    :param no_cache: Bypass the binary cache when `True`.
    :return: The tool's exit code.
    """
    if name not in TOOL_REGISTRY:
        msg = (
            f"Unknown tool: {name!r}. "
            f"Available tools: {', '.join(sorted(TOOL_REGISTRY))}"
        )
        raise ValueError(msg)

    spec = TOOL_REGISTRY[name]

    # Apply CLI overrides to the frozen spec.
    if version:
        spec = replace(spec, version=version)
    if checksum and spec.binary:
        key = spec.binary.resolve_platform()
        new_checksums = {**spec.binary.checksums, key: checksum}
        spec = replace(spec, binary=replace(spec.binary, checksums=new_checksums))

    logging.info("Resolving config for %s %s...", spec.name, spec.version)
    config_args, tmp_path = resolve_config(spec)

    bin_dir = None
    path_dirs: list[tempfile.TemporaryDirectory[str]] = []
    try:
        config_args = _dereference_data_dir_symlinks(config_args, path_dirs)

        # Build command prefix: binary download or uvx/uv-run.
        if spec.binary is not None:
            bin_dir = tempfile.TemporaryDirectory(
                prefix=f"repomatic-{name}-bin-",
            )
            try:
                bin_path = _install_binary(
                    spec,
                    Path(bin_dir.name),
                    skip_checksum,
                    no_cache=no_cache,
                )
            except RuntimeError as exc:
                raise ClickException(str(exc)) from exc
            cmd = [str(bin_path)]
        elif spec.npm is not None:
            bin_dir = tempfile.TemporaryDirectory(prefix=f"repomatic-{name}-npm-")
            # Gate awesome-lint's transitive tree by the same minimum-release-age
            # window the sync jobs apply to pins, via npm's min-release-age.
            cooldown = min_release_age_days(load_repomatic_config().minimum_release_age)
            try:
                bin_path = _install_npm(spec, Path(bin_dir.name), cooldown)
            except RuntimeError as exc:
                raise ClickException(str(exc)) from exc
            cmd = [str(bin_path)]
        else:
            # Gate the isolated uvx resolution (tool + transitive tree) by the
            # same minimum-release-age window the sync jobs apply to pins.
            cutoff = exclude_newer_cutoff(
                load_repomatic_config().minimum_release_age,
                datetime.now(tz=timezone.utc).date(),
            )
            cmd = _build_install_args(spec, exclude_newer=cutoff)

        # Default flags (always applied).
        if spec.default_flags:
            cmd.extend(spec.default_flags)

        # CI output format flags.
        if spec.ci_flags and is_github_ci():
            cmd.extend(spec.ci_flags)

        # Computed parameters derived from project metadata.
        if spec.computed_params:
            cmd.extend(spec.computed_params(Metadata()))

        # Config args from resolution (cache path or empty).
        cmd.extend(_splice_config_args(config_args, extra_args, spec))

        # Pre-create parent directories for the tool's declared report
        # destination; see `ToolSpec.output_flag` for why.
        if spec.output_flag:
            for i, arg in enumerate(extra_args):
                if arg == spec.output_flag and i + 1 < len(extra_args):
                    destination = Path(extra_args[i + 1])
                elif arg.startswith(f"{spec.output_flag}="):
                    destination = Path(arg.split("=", 1)[1])
                else:
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)

        env = _path_tools_env(spec, skip_checksum, no_cache, path_dirs)

        logging.info("Running: %s", " ".join(cmd))
        result = subprocess.run(cmd, check=False, env=env)

        logging.info("%s exited with code %d.", spec.name, result.returncode)

        # post_process is a write-mode-only fixup: it rewrites files on disk,
        # so it runs only after a successful write (return code 0), never in
        # check/dry-run mode, which writes nothing. The warning below covers
        # that gap; see ToolSpec.check_bypasses_post_process.
        if result.returncode == 0 and spec.post_process:
            spec.post_process(extra_args)

        # A check/dry-run invocation of a post_process tool cannot be trusted:
        # the fixup never ran, so warn rather than report a misleading status.
        if spec.check_bypasses_post_process(extra_args):
            logging.warning(
                "%s ran in check mode, but it relies on a repomatic "
                "post-processing step that only runs when files are written. "
                "Its exit status is unreliable here: it can flag drift the "
                "write path would reconcile, or miss drift the write path "
                "would introduce. Re-run in write mode for an authoritative "
                "result.",
                spec.name,
            )

        return result.returncode

    finally:
        if bin_dir is not None:
            bin_dir.cleanup()
        for path_dir in path_dirs:
            path_dir.cleanup()
        if tmp_path is not None:
            logging.debug("Cleaning up temp config: %s", tmp_path.resolve())
            tmp_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


def _detect_config_level(spec: ToolSpec) -> tuple[int, str]:
    """Detect which precedence level is active for a tool's config.

    Performs the same 4-level walk as {func}`resolve_config` but only
    detects the level without producing config content or CLI args.

    :return: `(level, description)` where level is 1-4 and description
        is a human-readable source label.
    """
    for config_file in spec.native_config_files:
        if Path(config_file).exists():
            return 1, config_file

    tool_config = load_pyproject_tool_section(spec.name)
    if tool_config:
        return 2, f"[tool.{spec.name}] in pyproject.toml"

    if spec.default_config:
        return 3, "bundled default"

    return 4, "(bare)"


def resolve_config_source(spec: ToolSpec) -> str:
    """Return a human-readable description of the active config source.

    Used by `repomatic run --list` to show which precedence level is active
    for each tool in the current repo.
    """
    return _detect_config_level(spec)[1]


def find_unmodified_configs(root: Path | None = None) -> list[tuple[str, str]]:
    """Find native config files identical to their bundled defaults.

    Iterates over every tool in {data}`TOOL_REGISTRY` that has a
    `default_config`.  For each, checks whether any of its
    `native_config_files` exists on disk and is content-identical
    to the bundled default after trailing-whitespace normalization.

    The normalization (`rstrip() + "\\n"`) matches the convention
    used by `_init_config_files` when writing files during `init`.

    :param root: Directory the relative config paths resolve against.
        Defaults to the working directory; `run_init` passes its
        `output_dir` so the scan and the deletion the CLI derives from it
        (`--delete-unmodified`) agree on one tree.
    :return: List of `(tool_name, relative_path)` tuples for each
        unmodified file found.
    """
    base = root or Path()
    unmodified: list[tuple[str, str]] = []

    for name, spec in sorted(TOOL_REGISTRY.items()):
        if not spec.default_config:
            continue

        with get_data_file_path(spec.default_config) as bundled_path:
            bundled = bundled_path.read_text(encoding="UTF-8").rstrip() + "\n"

        for config_file in spec.native_config_files:
            path = base / config_file
            if not path.exists():
                continue
            native = path.read_text(encoding="UTF-8").rstrip() + "\n"
            if native == bundled:
                unmodified.append((name, config_file))

    return unmodified
