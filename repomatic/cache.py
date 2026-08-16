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

"""Global cache for downloaded tool executables, HTTP API responses, and
generated tool configurations.

Three cache subtrees under the user-level cache directory:

**Binary cache** (`bin/`): platform-specific tool executables, keyed by
``{tool}/{version}/{platform}/{executable}``. Each cached binary has a
`.sha256` sidecar written after a verified archive download. Cache hits
verify the binary against this sidecar to detect local tampering.

**HTTP response cache** (`http/`): JSON API responses from PyPI and GitHub,
keyed by ``{namespace}/{key}.json``. Freshness is controlled by a per-caller
TTL (seconds); stale entries remain on disk until auto-purge removes them.

**Config cache** (`config/`): generated tool configuration files, keyed by
``{tool}/{filename}``. Overwritten on every invocation from the current
`[tool.X]` section in `pyproject.toml` or bundled defaults. Passed to
tools via explicit `--config` flags so repomatic never writes to the
user's repository.

```{note}
The cache module is intentionally a pure storage layer. It does not know
about checksums, registries, API semantics, or tool specifications. All
trust and freshness decisions belong to the caller.
```
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from extra_platforms import is_macos, is_windows

from .config import load_repomatic_config
from .humanize import SECONDS_PER_DAY, format_age, format_file_size

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

CACHE_LIST_HEADER_DEFS: tuple[tuple[str, str], ...] = (
    ("Type", "type"),
    ("Name", "name"),
    ("Detail", "detail"),
    ("Size", "size"),
    ("Age", "age"),
)
"""Column definitions for the `repomatic cache show` table.

Lives beside the entry dataclasses it renders; the CLI derives its
`--sort-by` choices from it.
"""


def _atomic_write(dest: Path, prefix: str, write: Callable[[Path], object]) -> None:
    """Write *dest* atomically: temp file in the target directory, then rename.

    The rename is atomic on POSIX (same-filesystem rename) and safe on Windows
    (`Path.replace` overwrites atomically). *write* receives the temp path and
    fills it (its return value is ignored, so `write_text`/`write_bytes` pass
    straight through); partial writes are cleaned up on any failure.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=prefix, suffix=".tmp")
    try:
        os.close(fd)
        write(Path(tmp))
        Path(tmp).replace(dest)
    except BaseException:
        # Clean up partial writes on any failure.
        Path(tmp).unlink(missing_ok=True)
        raise


@dataclass(frozen=True)
class CachedFile:
    """The filesystem facts every cached entry carries, whatever it holds.

    The three caches (binaries, HTTP responses, tool configs) differ only in
    how they *name* an entry; everything the listing, the age filter and the
    purge loop need is here, so those all take a `CachedFile` and never care
    which subtree it came from.

    Subclasses supply their own identity fields plus {attr}`kind` and
    {attr}`scope`.
    """

    size: int
    """File size in bytes."""

    path: Path
    """Absolute path to the cached file."""

    mtime: float
    """File modification time (seconds since epoch)."""

    kind: ClassVar[str] = ""
    """The cache this entry belongs to, as the `repomatic cache show` table
    spells it."""

    @property
    def scope(self) -> str:
        """The name a `cache clean` filter matches this entry on.

        Doubles as the table's subject column: the thing a reader identifies
        the entry by (`--tool ruff`, `--namespace pypi`) is the same thing the
        listing shows them, so one property serves both.
        """
        raise NotImplementedError

    @property
    def detail(self) -> str:
        """What distinguishes this entry from its siblings in the same scope."""
        raise NotImplementedError

    def is_fresh(self, max_age_days: int | None) -> bool:
        """Whether this entry is younger than the age cutoff.

        A `None` cutoff keeps nothing: age-unfiltered clears delete every
        entry the caller's other filters matched.
        """
        if max_age_days is None:
            return False
        return self.mtime >= time.time() - max_age_days * SECONDS_PER_DAY

    def as_row(self) -> tuple[str, str, str, str, str]:
        """Render this entry as one `repomatic cache show` table row."""
        return (
            self.kind,
            self.scope,
            self.detail,
            format_file_size(self.size),
            format_age(self.mtime),
        )


@dataclass(frozen=True)
class CacheEntry(CachedFile):
    """A single cached binary with its metadata."""

    tool: str = ""
    """Tool name (registry key)."""

    version: str = ""
    """Pinned version string."""

    platform: str = ""
    """Platform key (e.g., `linux-x64`, `macos-arm64`)."""

    executable: str = ""
    """Executable filename."""

    kind: ClassVar[str] = "binary"

    @property
    def scope(self) -> str:
        return self.tool

    @property
    def detail(self) -> str:
        return f"{self.version} ({self.platform})"


@dataclass(frozen=True)
class HttpCacheEntry(CachedFile):
    """A single cached HTTP response with its metadata."""

    namespace: str = ""
    """Cache namespace (e.g., `pypi`, `github-releases`)."""

    key: str = ""
    """Cache key within the namespace (e.g., `requests`, `astral-sh/ruff`)."""

    kind: ClassVar[str] = "http"

    @property
    def scope(self) -> str:
        return self.namespace

    @property
    def detail(self) -> str:
        return self.key


@dataclass(frozen=True)
class ConfigCacheEntry(CachedFile):
    """A single cached tool configuration file with its metadata."""

    tool: str = ""
    """Tool name (registry key)."""

    filename: str = ""
    """Config filename (e.g., `yamllint.yaml`, `biome.json`)."""

    kind: ClassVar[str] = "config"

    @property
    def scope(self) -> str:
        return self.tool

    @property
    def detail(self) -> str:
        return self.filename


def _platform_cache_dir() -> Path:
    """Return the platform-appropriate default cache directory.

    - macOS: `~/Library/Caches/repomatic`.
    - Windows: `%LOCALAPPDATA%\\repomatic\\Cache`.
    - Linux/POSIX: `$XDG_CACHE_HOME/repomatic` or `~/.cache/repomatic`.
    """
    home = Path.home()
    if is_macos():
        return home / "Library" / "Caches" / "repomatic"
    if is_windows():
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            return Path(local_app_data) / "repomatic" / "Cache"
        return home / "AppData" / "Local" / "repomatic" / "Cache"
    # Linux and other POSIX.
    xdg = os.environ.get("XDG_CACHE_HOME")
    if xdg:
        return Path(xdg) / "repomatic"
    return home / ".cache" / "repomatic"


def cache_dir() -> Path:
    """Resolve the cache root directory.

    Precedence (highest to lowest):

    1. `REPOMATIC_CACHE_DIR` environment variable.
    2. `cache.dir` in `[tool.repomatic]`.
    3. Platform-specific default.

    :return: Absolute path to the cache root (may not exist yet).
    """
    # 1. Environment variable (highest priority).
    env_override = os.environ.get("REPOMATIC_CACHE_DIR")
    if env_override:
        return Path(env_override).expanduser().resolve()

    # 2. Config from [tool.repomatic].
    config = load_repomatic_config()
    if config.cache.dir:
        return Path(config.cache.dir).expanduser().resolve()

    # 3. Platform default.
    return _platform_cache_dir()


def _bin_dir() -> Path:
    """Return the `bin/` subdirectory under the cache root."""
    return cache_dir() / "bin"


def cached_binary_path(
    name: str,
    version: str,
    platform_key: str,
    executable: str,
) -> Path:
    """Construct the cache path for a binary (does not check existence).

    :param name: Tool name.
    :param version: Pinned version.
    :param platform_key: Platform key (e.g., `linux-x64`).
    :param executable: Executable filename.
    :return: Absolute path where the binary would be cached.
    """
    return _bin_dir() / name / version / platform_key / executable


SIDECAR_SUFFIX = ".sha256"
"""Suffix of the digest sidecar stored beside each cached binary.

Part of the binary cache's on-disk layout: the listers skip sidecars and the
purger removes them along with their entry. Computing, writing, and verifying
the digest itself stays with the caller (`tool_runner`), per the module note
above.
"""


def binary_sidecar_path(binary_path: Path) -> Path:
    """Return the digest sidecar path for a cached binary.

    :param binary_path: Path to the cached binary.
    :return: Path of the sidecar file next to it.
    """
    return binary_path.with_suffix(binary_path.suffix + SIDECAR_SUFFIX)


def get_cached_binary(
    name: str,
    version: str,
    platform_key: str,
    executable: str,
) -> Path | None:
    """Return the cached binary path if it exists and is executable.

    Does **not** verify the checksum. The caller is responsible for integrity
    checks since it owns the checksum value and the `skip_checksum` flag.

    :param name: Tool name.
    :param version: Pinned version.
    :param platform_key: Platform key.
    :param executable: Executable filename.
    :return: Path to the cached binary, or `None` if not cached.
    """
    path = cached_binary_path(name, version, platform_key, executable)
    if path.is_file() and os.access(path, os.X_OK):
        return path
    return None


def store_binary(
    name: str,
    version: str,
    platform_key: str,
    source: Path,
) -> Path | None:
    """Copy an extracted binary into the cache atomically.

    Writes to a temporary file in the target directory, then renames to the
    final name. This is atomic on POSIX (same-filesystem rename) and safe on
    Windows (`Path.replace` overwrites atomically).

    Triggers {func}`auto_purge` after a successful store.

    :param name: Tool name.
    :param version: Pinned version.
    :param platform_key: Platform key.
    :param source: Path to the extracted binary to cache.
    :return: Path to the cached binary, or `None` when the cache is unwritable
        (a read-only cache root, a restricted CI mount): callers fall back to
        their staging copy, matching {func}`store_response` and
        {func}`store_config`.
    """
    dest = cached_binary_path(name, version, platform_key, source.name)

    def fill(tmp_path: Path) -> None:
        shutil.copy2(source, tmp_path)
        tmp_path.chmod(0o755)
        # copy2 preserves the source mtime, which for a binary extracted from
        # a release archive is the upstream build date. Purging ages entries
        # by mtime, so an old build date would get the entry deleted by the
        # auto_purge() call below, before this store even returns. Stamp the
        # store time instead.
        os.utime(tmp_path)

    try:
        _atomic_write(dest, f".{source.name}.", fill)
    except OSError as exc:
        logging.warning("Cannot cache %s at %s: %s", name, dest, exc)
        return None
    logging.debug("Cached %s %s for %s at %s.", name, version, platform_key, dest)
    auto_purge()
    return dest


def cache_info() -> list[CacheEntry]:
    """List all cached binaries.

    The `bin/` layout is fixed at four levels
    (``{tool}/{version}/{platform}/{executable}``), so one glob walks it and
    the identity fields read straight off each path's ancestry.

    :return: List of {class}`CacheEntry` instances, sorted by tool name then
        version.
    """
    bin_root = _bin_dir()
    if not bin_root.is_dir():
        return []

    entries: list[CacheEntry] = []
    for binary in sorted(bin_root.glob("*/*/*/*")):
        # Skip stray directories and digest sidecar files.
        if not binary.is_file() or binary.name.endswith(SIDECAR_SUFFIX):
            continue
        stat = binary.stat()
        entries.append(
            CacheEntry(
                tool=binary.parents[2].name,
                version=binary.parents[1].name,
                platform=binary.parent.name,
                executable=binary.name,
                size=stat.st_size,
                path=binary,
                mtime=stat.st_mtime,
            )
        )
    return entries


def clear_cache(
    tool: str | None = None,
    max_age_days: int | None = None,
) -> tuple[int, int]:
    """Remove cached binaries.

    :param tool: If set, only remove entries for this tool. Otherwise remove
        all cached binaries.
    :param max_age_days: If set, only remove entries with mtime older than
        this many days. Otherwise remove all matching entries.
    :return: Tuple of (files_deleted, bytes_freed).
    """
    return _clear_subtree(
        cache_info(),
        _bin_dir(),
        scope=tool,
        max_age_days=max_age_days,
        sidecars=True,
    )


# ---------------------------------------------------------------------------
# HTTP response cache
# ---------------------------------------------------------------------------


def _http_dir() -> Path:
    """Return the `http/` subdirectory under the cache root."""
    return cache_dir() / "http"


def get_cached_response(
    namespace: str,
    key: str,
    max_age_seconds: int,
) -> bytes | None:
    """Return a cached HTTP response if it exists and is fresh.

    :param namespace: Cache namespace (e.g., `pypi`, `github-releases`).
    :param key: Cache key, may contain `/` for nested paths.
    :param max_age_seconds: Maximum age in seconds. Entries with mtime older
        than this are considered stale and ignored. `<= 0` disables the
        cache (always returns `None`).
    :return: Raw cached response bytes, or `None` if not cached or stale.
    """
    if max_age_seconds <= 0:
        return None
    path = _http_dir() / namespace / f"{key}.json"
    if not path.is_file():
        return None
    age = time.time() - path.stat().st_mtime
    if age > max_age_seconds:
        logging.debug(
            "Stale HTTP cache entry: %s (age %.0fs > %ds).", path, age, max_age_seconds
        )
        return None
    logging.debug("HTTP cache hit: %s.", path)
    return path.read_bytes()


def store_response(
    namespace: str,
    key: str,
    data: bytes,
) -> Path | None:
    """Store an HTTP response in the cache atomically.

    Uses the same write-to-temp-then-rename pattern as {func}`store_binary`.
    Triggers {func}`auto_purge` after a successful store.

    :param namespace: Cache namespace.
    :param key: Cache key, may contain `/` for nested paths.
    :param data: Raw response bytes to cache.
    :return: Path to the cached response file, or `None` if the write
        failed (permissions, read-only filesystem, sandbox restrictions).
    """
    dest = _http_dir() / namespace / f"{key}.json"
    try:
        _atomic_write(dest, ".response.", lambda tmp_path: tmp_path.write_bytes(data))
    except OSError:
        logging.debug("Failed to cache HTTP response: %s/%s.", namespace, key)
        return None

    logging.debug("Cached HTTP response: %s/%s at %s.", namespace, key, dest)
    auto_purge()
    return dest


def http_cache_info() -> list[HttpCacheEntry]:
    """List all cached HTTP responses.

    :return: List of {class}`HttpCacheEntry` instances, sorted by namespace
        then key.
    """
    http_root = _http_dir()
    if not http_root.is_dir():
        return []

    entries: list[HttpCacheEntry] = []
    for ns_dir in sorted(http_root.iterdir()):
        if not ns_dir.is_dir():
            continue
        namespace = ns_dir.name
        for json_file in sorted(ns_dir.rglob("*.json")):
            if not json_file.is_file():
                continue
            # Derive key from relative path minus .json extension.
            rel = json_file.relative_to(ns_dir)
            key = str(rel.with_suffix(""))
            stat = json_file.stat()
            entries.append(
                HttpCacheEntry(
                    namespace=namespace,
                    key=key,
                    size=stat.st_size,
                    path=json_file,
                    mtime=stat.st_mtime,
                )
            )
    return entries


def clear_http_cache(
    namespace: str | None = None,
    max_age_days: int | None = None,
) -> tuple[int, int]:
    """Remove cached HTTP responses.

    :param namespace: If set, only remove entries in this namespace. Otherwise
        remove all cached responses.
    :param max_age_days: If set, only remove entries with mtime older than
        this many days. Otherwise remove all matching entries.
    :return: Tuple of (files_deleted, bytes_freed).
    """
    return _clear_subtree(
        http_cache_info(),
        _http_dir(),
        scope=namespace,
        max_age_days=max_age_days,
    )


# ---------------------------------------------------------------------------
# Config cache
# ---------------------------------------------------------------------------


def _config_dir() -> Path:
    """Return the `config/` subdirectory under the cache root."""
    return cache_dir() / "config"


def store_config(
    tool_name: str,
    filename: str,
    content: str,
) -> Path | None:
    """Store a generated tool config in the cache atomically.

    Uses the same write-to-temp-then-rename pattern as {func}`store_response`.
    Does **not** trigger {func}`auto_purge`: config files are tiny and
    overwritten on every invocation, so age-based pruning is unnecessary.

    :param tool_name: Tool name (registry key).
    :param filename: Config filename (e.g., `yamllint.yaml`).
    :param content: Config file content as text.
    :return: Path to the cached config file, or `None` if the write
        failed (permissions, read-only filesystem, sandbox restrictions).
    """
    dest = _config_dir() / tool_name / filename
    try:
        _atomic_write(
            dest,
            f".{filename}.",
            lambda tmp_path: tmp_path.write_text(content, encoding="UTF-8"),
        )
    except OSError:
        logging.debug("Failed to cache config for %s.", tool_name)
        return None

    logging.debug("Cached config for %s at %s.", tool_name, dest)
    return dest


def config_cache_info() -> list[ConfigCacheEntry]:
    """List all cached tool configurations.

    :return: List of {class}`ConfigCacheEntry` instances, sorted by tool name.
    """
    config_root = _config_dir()
    if not config_root.is_dir():
        return []

    entries: list[ConfigCacheEntry] = []
    for tool_dir in sorted(config_root.iterdir()):
        if not tool_dir.is_dir():
            continue
        for config_file in sorted(tool_dir.iterdir()):
            if not config_file.is_file():
                continue
            stat = config_file.stat()
            entries.append(
                ConfigCacheEntry(
                    tool=tool_dir.name,
                    filename=config_file.name,
                    size=stat.st_size,
                    path=config_file,
                    mtime=stat.st_mtime,
                )
            )
    return entries


def clear_config_cache(
    tool: str | None = None,
    max_age_days: int | None = None,
) -> tuple[int, int]:
    """Remove cached tool configurations.

    :param tool: If set, only remove entries for this tool. Otherwise remove
        all cached configurations.
    :param max_age_days: If set, only remove entries older than this many
        days, matching {func}`clear_cache` and {func}`clear_http_cache`.
    :return: Tuple of (files_deleted, bytes_freed).
    """
    return _clear_subtree(
        config_cache_info(),
        _config_dir(),
        scope=tool,
        max_age_days=max_age_days,
    )


def cache_rows() -> tuple[list[tuple[str, str, str, str, str]], int]:
    """List every cached file across the three caches, as table rows.

    Backs `repomatic cache show`: each entry renders itself
    ({meth}`CachedFile.as_row`), so the command stays a print call and a new
    cache kind shows up in the listing by existing.

    :return: `(rows, total_size)`, rows ordered binaries, then HTTP responses,
        then tool configs.
    """
    entries: list[CachedFile] = [
        *cache_info(),
        *http_cache_info(),
        *config_cache_info(),
    ]
    return [entry.as_row() for entry in entries], sum(e.size for e in entries)


def _clear_subtree(
    entries: Sequence[CachedFile],
    root: Path,
    *,
    scope: str | None,
    max_age_days: int | None,
    sidecars: bool = False,
) -> tuple[int, int]:
    """Remove the entries of one cache subtree, honoring both filters.

    The shared body of {func}`clear_cache`, {func}`clear_http_cache` and
    {func}`clear_config_cache`, which differ only in the subtree they walk and
    the name their scope filter goes by on the command line.

    :param entries: Candidate entries, from that cache's `*_cache_info()`.
    :param root: The subtree they live in. A missing one clears nothing.
    :param scope: Keep entries whose {attr}`~CachedFile.scope` differs. `None`
        selects every entry.
    :param max_age_days: Keep entries younger than this. `None` selects every
        entry.
    :param sidecars: Also remove each entry's `.sha256` sidecar.
    :return: Tuple of (files_deleted, bytes_freed).
    """
    if not root.is_dir():
        return 0, 0
    return _purge(
        entries,
        root,
        keep=lambda entry: (
            (scope is not None and entry.scope != scope) or entry.is_fresh(max_age_days)
        ),
        sidecars=sidecars,
    )


def _purge(
    entries: Sequence[CachedFile],
    root: Path,
    *,
    keep: Callable[[CachedFile], bool],
    sidecars: bool = False,
) -> tuple[int, int]:
    """Delete the cache *entries* not spared by the *keep* predicate.

    Shared delete loop behind {func}`clear_cache`, {func}`clear_http_cache`,
    and {func}`clear_config_cache`. Empty parent directories under *root* are
    pruned afterwards.

    :param entries: Candidate entries from one of the `*_cache_info()` listers.
    :param root: The cache subtree the entries live in.
    :param keep: Entries for which this returns `True` are left untouched.
    :param sidecars: Also remove each entry's `.sha256` sidecar (binary cache).
    :return: Tuple of (files_deleted, bytes_freed).
    """
    files_deleted = 0
    bytes_freed = 0
    for entry in entries:
        if keep(entry):
            continue
        logging.debug("Purging cache entry: %s", entry.path)
        try:
            bytes_freed += entry.size
            entry.path.unlink()
            if sidecars:
                sidecar = binary_sidecar_path(entry.path)
                if sidecar.is_file():
                    sidecar.unlink()
            files_deleted += 1
        except OSError:
            logging.debug("Failed to remove %s.", entry.path)

    _prune_empty_dirs(root)
    return files_deleted, bytes_freed


def _prune_empty_dirs(root: Path) -> None:
    """Remove empty directories under *root*, bottom-up."""
    if not root.is_dir():
        return
    # Walk bottom-up and attempt to remove each directory. rmdir() fails
    # (OSError) if the directory is non-empty, which is the desired behavior.
    for dirpath, _dirnames, _filenames in os.walk(root, topdown=False):
        path = Path(dirpath)
        if path == root:
            continue
        try:
            path.rmdir()
        except OSError:
            pass


def _max_age_days() -> int:
    """Resolve the auto-purge TTL.

    Precedence (highest to lowest):

    1. `REPOMATIC_CACHE_MAX_AGE` environment variable.
    2. `cache.max-age` in `[tool.repomatic]`.
    3. `CacheConfig.max_age` field default.

    :return: TTL in days. `0` means auto-purge is disabled.
    """
    # 1. Environment variable (highest priority).
    raw = os.environ.get("REPOMATIC_CACHE_MAX_AGE", "")
    if raw.strip():
        try:
            return int(raw)
        except ValueError:
            logging.warning(
                "Invalid REPOMATIC_CACHE_MAX_AGE=%r, using config default.",
                raw,
            )

    # 2 + 3. Config from [tool.repomatic] (falls back to field default).
    config = load_repomatic_config()
    return config.cache.max_age


_PURGED_ROOTS: set[Path] = set()
"""Cache roots already swept by {func}`auto_purge` in this process.

Every {func}`store_response` triggers a purge, and a dependency sweep stores
hundreds of responses per run: without the guard each store re-walks the whole
binary and HTTP subtrees to re-delete nothing. One sweep per root per process
is enough, since entries only age on the scale of days.
"""


def auto_purge() -> None:
    """Remove cached entries older than the configured TTL.

    Called automatically after {func}`store_binary` and
    {func}`store_response`, and runs at most once per cache root per process
    (see {data}`_PURGED_ROOTS`). Purges both binary and HTTP cache entries.
    Resolves the TTL from `REPOMATIC_CACHE_MAX_AGE` env var, then
    `cache.max-age` in `[tool.repomatic]`, then the
    `CacheConfig.max_age` field default. Set to `0` to disable.
    """
    root = cache_dir()
    if root in _PURGED_ROOTS:
        return
    _PURGED_ROOTS.add(root)
    days = _max_age_days()
    if days <= 0:
        return
    bin_deleted, bin_freed = clear_cache(max_age_days=days)
    http_deleted, http_freed = clear_http_cache(max_age_days=days)
    total_deleted = bin_deleted + http_deleted
    total_freed = bin_freed + http_freed
    if total_deleted:
        logging.debug(
            "Auto-purged %d cached entry(ies), freed %d bytes.",
            total_deleted,
            total_freed,
        )
