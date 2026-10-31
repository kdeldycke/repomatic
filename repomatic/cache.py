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

"""Global cache for tool executables, API responses and generated tool configs.

Four subtrees under the user-level cache directory:

**Binary cache** (`bin/`): platform-specific tool executables, keyed by
``{tool}/{version}/{platform}/{executable}``. Each cached binary has a
`.sha256` sidecar written after a verified archive download. Cache hits
verify the binary against this sidecar to detect local tampering.

**HTTP response cache** (`http/`): JSON API responses from PyPI and GitHub,
keyed by ``{namespace}/{key}.json``. Freshness is controlled by a per-caller
TTL (seconds); stale entries remain on disk until auto-purge removes them.

**Config cache** (`config/`): generated tool configuration files, keyed by
``{tool}/{digest}/{filename}``, where the digest names the content. Two runs
that generate different configs for one tool (two checkouts, two branches)
write two files, so neither run reads the config of the other. Passed to tools
via explicit `--config` flags so repomatic never writes to the user's
repository.

**Locks** (`locks/`): the lock files {func}`lock_path` names, which the tool
runner holds while a config file sits in the working directory. Neither listed
nor cleaned: a lock file deleted while a run holds it lets the next run start
without waiting.

```{note}
The cache module is intentionally a pure storage layer. It does not know
about checksums, registries, API semantics, or tool specifications. All
trust and freshness decisions belong to the caller.
```
"""

from __future__ import annotations

import hashlib
import logging
import os
import posixpath
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from click_extra import ColumnSpec
from extra_platforms import is_macos, is_windows

from .config import load_repomatic_config
from .file_ops import atomic_write
from .humanize import SECONDS_PER_DAY, format_age, format_file_size

TYPE_CHECKING = False
if TYPE_CHECKING:
    from typing import Self

CACHE_LIST_HEADER_DEFS: tuple[ColumnSpec, ...] = (
    ColumnSpec("type", "Type"),
    ColumnSpec("name", "Name"),
    ColumnSpec("detail", "Detail"),
    ColumnSpec("size", "Size"),
    ColumnSpec("age", "Age"),
)
"""Column definitions for the `repomatic cache show` table.

Lives beside the entry dataclasses it renders; the CLI derives its
`--sort-by` choices from it.
"""

CONFIG_DIGEST_LENGTH = 16
"""Hex digits of the content digest that names a cached config directory.

Sixty-four bits: enough to keep apart every config one cache ever holds.
"""

SIDECAR_SUFFIX = ".sha256"
"""Suffix of the digest sidecar stored beside each cached binary.

Part of the binary cache's on-disk layout: the listing skips sidecars and the
clean removes them along with their entry. Computing, writing, and verifying
the digest itself stays with the caller (`tooling/binaries.py`), per the module
note above.
"""


def _platform_cache_dir() -> Path:
    """Return the platform-appropriate default cache directory.

    - Windows: `%LOCALAPPDATA%\\repomatic\\Cache`.
    - macOS: `$XDG_CACHE_HOME/repomatic`, else `~/Library/Caches/repomatic`.
    - Linux/POSIX: `$XDG_CACHE_HOME/repomatic`, else `~/.cache/repomatic`.

    A user who sets `XDG_CACHE_HOME` asks every tool to keep its cache there,
    macOS included. A relative value is ignored, as the XDG specification says.
    """
    home = Path.home()
    if is_windows():
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            return Path(local_app_data) / "repomatic" / "Cache"
        return home / "AppData" / "Local" / "repomatic" / "Cache"
    # XDG paths are POSIX paths, so posixpath checks them the same way on every
    # host.
    xdg_cache_home = os.environ.get("XDG_CACHE_HOME", "").strip()
    if posixpath.isabs(xdg_cache_home):
        return Path(xdg_cache_home) / "repomatic"
    if is_macos():
        return home / "Library" / "Caches" / "repomatic"
    return home / ".cache" / "repomatic"


def cache_dir() -> Path:
    """Resolve the cache root directory.

    Precedence (highest to lowest):

    1. `REPOMATIC_CACHE_DIR` environment variable.
    2. `cache.dir` in `[tool.repomatic]`.
    3. Platform-specific default.

    ```{todo}
    Take the platform default from `get_cache_dir("repomatic")` of
    `click_extra.carapace`, and delete `_platform_cache_dir()`, once repomatic
    locks click-extra `10.0.0`: it ships that function, added in
    [kdeldycke/click-extra@b05771d](https://github.com/kdeldycke/click-extra/commit/b05771dc1d8f6c5228a3fa7211ff2ccfc49c89fa).
    ```

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


def lock_path(name: str) -> Path:
    """Return the path of the lock file called *name*.

    The file lives in the `locks/` subtree, which no listing or clean touches.

    :param name: Lock name, unique to what the lock guards.
    """
    return cache_dir() / "locks" / f"{name}.lock"


@dataclass(frozen=True, kw_only=True)
class CachedFile:
    """One file of a cache subtree, with the facts every entry carries.

    The three caches (binaries, HTTP responses, tool configs) differ only in
    how they *name* an entry. Each subclass reads its identity fields off the
    path of the entry, in {meth}`from_path`. The listing, the age filter and
    the clean are the same for all three, so they live here.
    """

    path: Path
    """Absolute path to the cached file."""

    size: int
    """File size in bytes."""

    mtime: float
    """File modification time (seconds since epoch)."""

    kind: ClassVar[str]
    """The cache this entry belongs to, as the `repomatic cache show` table
    spells it."""

    subdir: ClassVar[str]
    """Name of the subtree under the cache root."""

    scope_option: ClassVar[str]
    """The `repomatic cache clean` option that narrows a clean to one scope of
    this cache."""

    @classmethod
    def root(cls) -> Path:
        """Return the subtree of this cache under the cache root."""
        return cache_dir() / cls.subdir

    @classmethod
    def from_path(cls, path: Path, root: Path) -> Self | None:
        """Read an entry off the path of a file below *root*.

        :return: The entry, or `None` when the file is not an entry of this
            cache, like a digest sidecar.
        """
        raise NotImplementedError

    @classmethod
    def entries(cls) -> list[Self]:
        """List the entries of this cache, sorted by path."""
        root = cls.root()
        if not root.is_dir():
            return []
        found: list[Self] = []
        for path in sorted(root.rglob("*")):
            if path.is_file() and (entry := cls.from_path(path, root)) is not None:
                found.append(entry)
        return found

    @classmethod
    def clear(
        cls, scope: str | None = None, max_age_days: int | None = None
    ) -> tuple[int, int]:
        """Remove the entries of this cache that both filters select.

        Empty directories left behind are pruned afterwards.

        :param scope: Keep entries whose {attr}`scope` differs. `None` selects
            every entry.
        :param max_age_days: Keep entries younger than this. `None` selects
            every entry.
        :return: Tuple of (files_deleted, bytes_freed).
        """
        files_deleted = 0
        bytes_freed = 0
        for entry in cls.entries():
            if (scope is not None and entry.scope != scope) or entry.is_fresh(
                max_age_days
            ):
                continue
            logging.debug(f"Purging cache entry: {entry.path}")
            try:
                entry.remove()
            except OSError:
                logging.debug(f"Failed to remove {entry.path}.")
                continue
            files_deleted += 1
            bytes_freed += entry.size
        _prune_empty_dirs(cls.root())
        return files_deleted, bytes_freed

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

    def remove(self) -> None:
        """Delete the files of this entry."""
        self.path.unlink()

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


@dataclass(frozen=True, kw_only=True)
class BinaryCacheEntry(CachedFile):
    """A single cached binary with its metadata."""

    tool: str
    """Tool name (registry key)."""

    version: str
    """Pinned version string."""

    platform: str
    """Platform key, like `linux-x64` or `macos-arm64`."""

    executable: str
    """Executable filename."""

    kind: ClassVar[str] = "binary"
    subdir: ClassVar[str] = "bin"
    scope_option: ClassVar[str] = "tool"

    @classmethod
    def from_path(cls, path: Path, root: Path) -> Self | None:
        """Read the four levels of ``{tool}/{version}/{platform}/{executable}``.

        A digest sidecar is part of its binary's entry, not an entry itself.
        """
        parts = path.relative_to(root).parts
        if len(parts) != 4 or path.name.endswith(SIDECAR_SUFFIX):
            return None
        tool, version, platform, executable = parts
        stat = path.stat()
        return cls(
            path=path,
            size=stat.st_size,
            mtime=stat.st_mtime,
            tool=tool,
            version=version,
            platform=platform,
            executable=executable,
        )

    @property
    def scope(self) -> str:
        return self.tool

    @property
    def detail(self) -> str:
        return f"{self.version} ({self.platform})"

    def remove(self) -> None:
        """Delete the binary and its digest sidecar."""
        self.path.unlink()
        binary_sidecar_path(self.path).unlink(missing_ok=True)


@dataclass(frozen=True, kw_only=True)
class HttpCacheEntry(CachedFile):
    """A single cached HTTP response with its metadata."""

    namespace: str
    """Cache namespace, like `pypi` or `github-release-tags`."""

    key: str
    """Cache key within the namespace, like `requests` or `astral-sh/ruff`."""

    kind: ClassVar[str] = "http"
    subdir: ClassVar[str] = "http"
    scope_option: ClassVar[str] = "namespace"

    @classmethod
    def from_path(cls, path: Path, root: Path) -> Self | None:
        """Read ``{namespace}/{key}.json``, where the key can hold slashes."""
        parts = path.relative_to(root).parts
        if len(parts) < 2 or path.suffix != ".json":
            return None
        stat = path.stat()
        return cls(
            path=path,
            size=stat.st_size,
            mtime=stat.st_mtime,
            namespace=parts[0],
            key="/".join(parts[1:]).removesuffix(".json"),
        )

    @property
    def scope(self) -> str:
        return self.namespace

    @property
    def detail(self) -> str:
        return self.key


@dataclass(frozen=True, kw_only=True)
class ConfigCacheEntry(CachedFile):
    """A single cached tool configuration file with its metadata."""

    tool: str
    """Tool name (registry key)."""

    digest: str
    """Start of the SHA-256 of the content, which names the entry."""

    filename: str
    """Config filename, like `yamllint.yaml` or `biome.json`."""

    kind: ClassVar[str] = "config"
    subdir: ClassVar[str] = "config"
    scope_option: ClassVar[str] = "tool"

    @classmethod
    def from_path(cls, path: Path, root: Path) -> Self | None:
        """Read ``{tool}/{digest}/{filename}``.

        A file directly in the tool directory, with no digest level, is still
        an entry: the clean has to reach it.
        """
        parts = path.relative_to(root).parts
        if len(parts) not in (2, 3):
            return None
        stat = path.stat()
        return cls(
            path=path,
            size=stat.st_size,
            mtime=stat.st_mtime,
            tool=parts[0],
            digest=parts[1] if len(parts) == 3 else "",
            filename=parts[-1],
        )

    @property
    def scope(self) -> str:
        return self.tool

    @property
    def detail(self) -> str:
        return f"{self.filename} ({self.digest})" if self.digest else self.filename


CACHE_KINDS: tuple[type[CachedFile], ...] = (
    BinaryCacheEntry,
    HttpCacheEntry,
    ConfigCacheEntry,
)
"""Every cache, in the order `repomatic cache show` lists them."""


def cached_binary_path(
    name: str,
    version: str,
    platform_key: str,
    executable: str,
) -> Path:
    """Construct the cache path for a binary (does not check existence).

    :param name: Tool name.
    :param version: Pinned version.
    :param platform_key: Platform key, like `linux-x64`.
    :param executable: Executable filename.
    :return: Absolute path where the binary would be cached.
    """
    return BinaryCacheEntry.root() / name / version / platform_key / executable


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
        atomic_write(dest, f".{source.name}.", fill)
    except OSError as exc:
        logging.warning(f"Cannot cache {name} at {dest}: {exc}")
        return None
    logging.debug(f"Cached {name} {version} for {platform_key} at {dest}.")
    auto_purge()
    return dest


def get_cached_response(
    namespace: str,
    key: str,
    max_age_seconds: int,
) -> bytes | None:
    """Return a cached HTTP response if it exists and is fresh.

    :param namespace: Cache namespace, like `pypi` or `github-release-tags`.
    :param key: Cache key, may contain `/` for nested paths.
    :param max_age_seconds: Maximum age in seconds. Entries with mtime older
        than this are considered stale and ignored. `<= 0` disables the
        cache (always returns `None`).
    :return: Raw cached response bytes, or `None` if not cached or stale.
    """
    if max_age_seconds <= 0:
        return None
    path = HttpCacheEntry.root() / namespace / f"{key}.json"
    if not path.is_file():
        return None
    age = time.time() - path.stat().st_mtime
    if age > max_age_seconds:
        logging.debug(
            f"Stale HTTP cache entry: {path} (age {age:.0f}s > {max_age_seconds}s)."
        )
        return None
    logging.debug(f"HTTP cache hit: {path}.")
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
    dest = HttpCacheEntry.root() / namespace / f"{key}.json"
    try:
        atomic_write(dest, ".response.", lambda tmp_path: tmp_path.write_bytes(data))
    except OSError:
        logging.debug(f"Failed to cache HTTP response: {namespace}/{key}.")
        return None

    logging.debug(f"Cached HTTP response: {namespace}/{key} at {dest}.")
    auto_purge()
    return dest


def store_config(
    tool_name: str,
    filename: str,
    content: str,
) -> Path | None:
    """Store a generated tool config in the cache atomically.

    The digest of *content* names the directory of the file, so the same
    content always lands on the same path, and different content never
    overwrites it. A run storing a config refreshes its age, which keeps a
    config in use out of reach of {func}`auto_purge`.

    :param tool_name: Tool name (registry key).
    :param filename: Config filename, like `yamllint.yaml`.
    :param content: Config file content as text.
    :return: Path to the cached config file, or `None` if the write
        failed (permissions, read-only filesystem, sandbox restrictions).
    """
    digest = hashlib.sha256(content.encode("UTF-8")).hexdigest()
    dest = (
        ConfigCacheEntry.root() / tool_name / digest[:CONFIG_DIGEST_LENGTH] / filename
    )
    try:
        atomic_write(
            dest,
            f".{filename}.",
            lambda tmp_path: tmp_path.write_text(content, encoding="UTF-8"),
        )
    except OSError:
        logging.debug(f"Failed to cache config for {tool_name}.")
        return None

    logging.debug(f"Cached config for {tool_name} at {dest}.")
    auto_purge()
    return dest


def cache_rows() -> tuple[list[tuple[str, str, str, str, str]], int]:
    """List every cached file across the three caches, as table rows.

    Backs `repomatic cache show`: each entry renders itself
    ({meth}`CachedFile.as_row`), so the command stays a print call and a new
    cache kind shows up in the listing by existing.

    :return: `(rows, total_size)`, rows ordered as {data}`CACHE_KINDS`.
    """
    entries = [entry for kind in CACHE_KINDS for entry in kind.entries()]
    return [entry.as_row() for entry in entries], sum(e.size for e in entries)


def clean(
    *,
    tool: str | None = None,
    namespace: str | None = None,
    max_age_days: int | None = None,
) -> tuple[int, int]:
    """Remove cached entries, as `repomatic cache clean` selects them.

    A scope filter reaches only the caches it applies to: *tool* narrows the
    binary and config caches and leaves HTTP responses alone, *namespace* the
    reverse. With neither filter, every cache is cleaned.

    :param tool: Only remove the binary and config entries of this tool.
    :param namespace: Only remove the HTTP responses of this namespace.
    :param max_age_days: Only remove entries older than this many days.
    :return: Tuple of (files_deleted, bytes_freed) over every cache cleaned.
    """
    scopes = {"tool": tool, "namespace": namespace}
    scoped = any(scope is not None for scope in scopes.values())
    files_deleted = 0
    bytes_freed = 0
    for kind in CACHE_KINDS:
        scope = scopes[kind.scope_option]
        if scoped and scope is None:
            continue
        deleted, freed = kind.clear(scope=scope, max_age_days=max_age_days)
        files_deleted += deleted
        bytes_freed += freed
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
                f"Invalid REPOMATIC_CACHE_MAX_AGE={raw!r}, using config default."
            )

    # 2 + 3. Config from [tool.repomatic] (falls back to field default).
    config = load_repomatic_config()
    return config.cache.max_age


_PURGED_ROOTS: set[Path] = set()
"""Cache roots already swept by {func}`auto_purge` in this process.

Every store triggers a purge, and a dependency sweep stores hundreds of
responses per run: without the guard each store re-walks every subtree to
re-delete nothing. One sweep per root per process is enough, since entries
only age on the scale of days.
"""


def auto_purge() -> None:
    """Remove cached entries older than the configured TTL.

    Called automatically after every store, and runs at most once per cache
    root per process (see `_PURGED_ROOTS`). Purges every cache. Resolves the
    TTL from `REPOMATIC_CACHE_MAX_AGE` env var, then `cache.max-age` in
    `[tool.repomatic]`, then the `CacheConfig.max_age` field default. Set to
    `0` to disable.
    """
    root = cache_dir()
    if root in _PURGED_ROOTS:
        return
    _PURGED_ROOTS.add(root)
    days = _max_age_days()
    if days <= 0:
        return
    deleted, freed = clean(max_age_days=days)
    if deleted:
        logging.debug(f"Auto-purged {deleted} cached entry(ies), freed {freed} bytes.")
