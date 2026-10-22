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

"""Tests for the global cache of binaries, HTTP responses and tool configs."""

from __future__ import annotations

import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from extra_platforms.pytest import skip_windows

from repomatic.cache import (
    BinaryCacheEntry,
    ConfigCacheEntry,
    HttpCacheEntry,
    _max_age_days,
    _prune_empty_dirs,
    auto_purge,
    cache_dir,
    cache_rows,
    cached_binary_path,
    clean,
    get_cached_binary,
    get_cached_response,
    lock_path,
    store_binary,
    store_config,
    store_response,
)
from repomatic.config import CacheConfig, Config

# ---------------------------------------------------------------------------
# cache_dir
# ---------------------------------------------------------------------------


def test_cache_dir_env_override(monkeypatch, tmp_path):
    """REPOMATIC_CACHE_DIR env var overrides platform default."""
    custom = tmp_path / "custom-cache"
    monkeypatch.setenv("REPOMATIC_CACHE_DIR", str(custom))
    assert cache_dir() == custom


def test_cache_dir_env_override_tilde(monkeypatch):
    """REPOMATIC_CACHE_DIR with ~ expands to home directory."""
    monkeypatch.setenv("REPOMATIC_CACHE_DIR", "~/my-cache")
    result = cache_dir()
    assert "~" not in str(result)
    assert result.name == "my-cache"


def test_cache_dir_darwin(monkeypatch):
    """macOS uses ~/Library/Caches/repomatic."""
    monkeypatch.delenv("REPOMATIC_CACHE_DIR", raising=False)
    monkeypatch.setattr("repomatic.cache.is_macos", lambda: True)
    monkeypatch.setattr("repomatic.cache.is_windows", lambda: False)
    result = cache_dir()
    assert result == Path.home() / "Library" / "Caches" / "repomatic"


def test_cache_dir_linux_xdg(monkeypatch):
    """Linux with XDG_CACHE_HOME uses $XDG_CACHE_HOME/repomatic."""
    monkeypatch.delenv("REPOMATIC_CACHE_DIR", raising=False)
    monkeypatch.setattr("repomatic.cache.is_macos", lambda: False)
    monkeypatch.setattr("repomatic.cache.is_windows", lambda: False)
    monkeypatch.setenv("XDG_CACHE_HOME", "/tmp/xdg-cache")
    assert cache_dir() == Path("/tmp/xdg-cache/repomatic")


def test_cache_dir_linux_default(monkeypatch):
    """Linux without XDG_CACHE_HOME uses ~/.cache/repomatic."""
    monkeypatch.delenv("REPOMATIC_CACHE_DIR", raising=False)
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.setattr("repomatic.cache.is_macos", lambda: False)
    monkeypatch.setattr("repomatic.cache.is_windows", lambda: False)
    result = cache_dir()
    assert result == Path.home() / ".cache" / "repomatic"


def test_cache_dir_windows_localappdata(monkeypatch):
    """Windows uses %LOCALAPPDATA%/repomatic/Cache."""
    monkeypatch.delenv("REPOMATIC_CACHE_DIR", raising=False)
    monkeypatch.setattr("repomatic.cache.is_macos", lambda: False)
    monkeypatch.setattr("repomatic.cache.is_windows", lambda: True)
    monkeypatch.setenv("LOCALAPPDATA", "/tmp/fake-localappdata")
    result = cache_dir()
    assert result == Path("/tmp/fake-localappdata/repomatic/Cache")


def test_cache_dir_from_config(monkeypatch, tmp_path):
    """cache.dir in [tool.repomatic] overrides platform default."""
    monkeypatch.delenv("REPOMATIC_CACHE_DIR", raising=False)
    config_dir = tmp_path / "config-cache"
    monkeypatch.setattr(
        "repomatic.cache.load_repomatic_config",
        lambda: Config(cache=CacheConfig(dir=str(config_dir))),
    )
    assert cache_dir() == config_dir


def test_cache_dir_env_beats_config(monkeypatch, tmp_path):
    """REPOMATIC_CACHE_DIR env var takes precedence over config."""
    env_dir = tmp_path / "env-cache"
    monkeypatch.setenv("REPOMATIC_CACHE_DIR", str(env_dir))
    monkeypatch.setattr(
        "repomatic.cache.load_repomatic_config",
        lambda: Config(cache=CacheConfig(dir=str(tmp_path / "config-cache"))),
    )
    assert cache_dir() == env_dir


def test_max_age_days_from_config(monkeypatch):
    """cache.max-age in [tool.repomatic] overrides field default."""
    monkeypatch.delenv("REPOMATIC_CACHE_MAX_AGE", raising=False)
    monkeypatch.setattr(
        "repomatic.cache.load_repomatic_config",
        lambda: Config(cache=CacheConfig(max_age=7)),
    )
    assert _max_age_days() == 7


def test_max_age_days_env_beats_config(monkeypatch):
    """REPOMATIC_CACHE_MAX_AGE env var takes precedence over config."""
    monkeypatch.setenv("REPOMATIC_CACHE_MAX_AGE", "3")
    monkeypatch.setattr(
        "repomatic.cache.load_repomatic_config",
        lambda: Config(cache=CacheConfig(max_age=90)),
    )
    assert _max_age_days() == 3


# ---------------------------------------------------------------------------
# cached_binary_path / get_cached_binary
# ---------------------------------------------------------------------------


def test_cached_binary_path_structure(cache_env):
    """Cache path follows bin/{tool}/{version}/{platform}/{executable}."""
    result = cached_binary_path("ruff", "0.11.0", "linux-x64", "ruff")
    assert result == cache_env / "bin" / "ruff" / "0.11.0" / "linux-x64" / "ruff"


def test_get_cached_binary_not_cached(cache_env):
    """Returns None when the binary is not in the cache."""
    assert get_cached_binary("ruff", "0.11.0", "linux-x64", "ruff") is None


def test_get_cached_binary_exists(cache_env):
    """Returns the path when the binary exists and is executable."""
    path = cached_binary_path("ruff", "0.11.0", "linux-x64", "ruff")
    path.parent.mkdir(parents=True)
    path.write_bytes(b"fake-binary")
    path.chmod(0o755)
    assert get_cached_binary("ruff", "0.11.0", "linux-x64", "ruff") == path


@skip_windows(
    reason=(
        "Windows does not use Unix execute bits; os.access(..., X_OK) always "
        "returns True."
    ),
)
def test_get_cached_binary_not_executable(cache_env):
    """Returns None when the file exists but is not executable."""
    path = cached_binary_path("ruff", "0.11.0", "linux-x64", "ruff")
    path.parent.mkdir(parents=True)
    path.write_bytes(b"fake-binary")
    path.chmod(0o644)
    assert get_cached_binary("ruff", "0.11.0", "linux-x64", "ruff") is None


# ---------------------------------------------------------------------------
# store_binary
# ---------------------------------------------------------------------------


def test_store_binary_creates_cache_entry(tmp_path, cache_env):
    """store_binary copies the source file into the cache."""
    source = tmp_path / "staging" / "ruff"
    source.parent.mkdir()
    source.write_bytes(b"binary-content")
    source.chmod(0o755)

    result = store_binary("ruff", "0.11.0", "linux-x64", source)

    expected = cached_binary_path("ruff", "0.11.0", "linux-x64", "ruff")
    assert result == expected
    assert result.read_bytes() == b"binary-content"
    assert os.access(result, os.X_OK)


def test_store_binary_overwrites_existing(tmp_path, cache_env):
    """store_binary replaces an existing cached binary atomically."""
    source_v1 = tmp_path / "staging" / "ruff"
    source_v1.parent.mkdir()
    source_v1.write_bytes(b"version-1")
    store_binary("ruff", "0.11.0", "linux-x64", source_v1)

    source_v1.write_bytes(b"version-2")
    result = store_binary("ruff", "0.11.0", "linux-x64", source_v1)
    assert result is not None
    assert result.read_bytes() == b"version-2"


def test_store_binary_triggers_auto_purge(tmp_path, cache_env):
    """store_binary calls auto_purge after storing."""
    source = tmp_path / "staging" / "ruff"
    source.parent.mkdir()
    source.write_bytes(b"data")

    with patch("repomatic.cache.auto_purge") as mock_purge:
        store_binary("ruff", "0.11.0", "linux-x64", source)
        mock_purge.assert_called_once()


def test_store_binary_stamps_store_time(monkeypatch, tmp_path, cache_env):
    """A source carrying an old archive mtime is not self-purged on store.

    shutil.copy2 preserves the source mtime, which for a binary extracted
    from a release archive is the upstream build date. auto_purge ages
    entries by mtime, so without a fresh timestamp the auto_purge call
    inside store_binary would delete the entry it just stored.
    """
    monkeypatch.setenv("REPOMATIC_CACHE_MAX_AGE", "30")  # Purge enabled.

    source = tmp_path / "staging" / "ruff"
    source.parent.mkdir()
    source.write_bytes(b"binary-content")
    source.chmod(0o755)
    ancient = time.time() - 90 * 86400
    os.utime(source, (ancient, ancient))

    result = store_binary("ruff", "0.11.0", "linux-x64", source)

    assert result is not None
    assert result.is_file()
    assert abs(result.stat().st_mtime - time.time()) < 60


def test_store_binary_unwritable_cache_returns_none(tmp_path, cache_env):
    """An unwritable cache root degrades to `None` instead of raising.

    Matches the fail-soft contract of `store_response` and `store_config`:
    the caller falls back to its staging copy.
    """
    source = tmp_path / "staging" / "ruff"
    source.parent.mkdir()
    source.write_bytes(b"data")

    with patch("repomatic.cache.atomic_write", side_effect=OSError("read-only")):
        assert store_binary("ruff", "0.11.0", "linux-x64", source) is None


# ---------------------------------------------------------------------------
# BinaryCacheEntry.entries
# ---------------------------------------------------------------------------


def test_cache_info_empty(cache_env):
    """Returns empty list when cache is empty."""
    assert BinaryCacheEntry.entries() == []


def test_cache_info_lists_entries(tmp_path, cache_env):
    """Returns all cached entries with correct metadata."""
    source = tmp_path / "staging" / "ruff"
    source.parent.mkdir()
    source.write_bytes(b"binary-content-here")
    store_binary("ruff", "0.11.0", "linux-x64", source)

    source2 = tmp_path / "staging" / "biome"
    source2.write_bytes(b"biome-bin")
    store_binary("biome", "2.0.0", "macos-arm64", source2)

    entries = BinaryCacheEntry.entries()
    assert len(entries) == 2
    tools = {e.tool for e in entries}
    assert tools == {"biome", "ruff"}

    ruff_entry = next(e for e in entries if e.tool == "ruff")
    assert ruff_entry.version == "0.11.0"
    assert ruff_entry.platform == "linux-x64"
    assert ruff_entry.size == len(b"binary-content-here")


# ---------------------------------------------------------------------------
# BinaryCacheEntry.clear
# ---------------------------------------------------------------------------


def test_clear_cache_all(tmp_path, cache_env):
    """A clear with no filter removes every binary."""
    source = tmp_path / "staging" / "tool"
    source.parent.mkdir()
    source.write_bytes(b"data")
    store_binary("ruff", "0.11.0", "linux-x64", source)
    store_binary("biome", "2.0.0", "linux-x64", source)

    deleted, freed = BinaryCacheEntry.clear()
    assert deleted == 2
    assert freed > 0
    assert BinaryCacheEntry.entries() == []


def test_clear_cache_specific_tool(tmp_path, cache_env):
    """A scoped clear removes only the binaries of that tool."""
    source = tmp_path / "staging" / "tool"
    source.parent.mkdir()
    source.write_bytes(b"data")
    store_binary("ruff", "0.11.0", "linux-x64", source)
    store_binary("biome", "2.0.0", "linux-x64", source)

    deleted, _ = BinaryCacheEntry.clear(scope="ruff")
    assert deleted == 1
    entries = BinaryCacheEntry.entries()
    assert len(entries) == 1
    assert entries[0].tool == "biome"


def test_clear_cache_max_age(tmp_path, cache_env):
    """An age-limited clear removes only old binaries."""
    source = tmp_path / "staging" / "tool"
    source.parent.mkdir()
    source.write_bytes(b"data")
    store_binary("ruff", "0.11.0", "linux-x64", source)

    # Make the cached file look 60 days old.
    cached = cached_binary_path("ruff", "0.11.0", "linux-x64", "tool")
    old_time = time.time() - 60 * 86400
    os.utime(cached, (old_time, old_time))

    # Store a fresh one.
    store_binary("biome", "2.0.0", "linux-x64", source)

    deleted, _ = BinaryCacheEntry.clear(max_age_days=30)
    assert deleted == 1
    entries = BinaryCacheEntry.entries()
    assert len(entries) == 1
    assert entries[0].tool == "biome"


def test_clear_cache_prunes_empty_dirs(tmp_path, cache_env):
    """A clear removes the directories it empties."""
    source = tmp_path / "staging" / "tool"
    source.parent.mkdir()
    source.write_bytes(b"data")
    store_binary("ruff", "0.11.0", "linux-x64", source)

    BinaryCacheEntry.clear()
    # The bin/ directory tree should be fully pruned.
    bin_dir = cache_env / "bin"
    if bin_dir.exists():
        remaining = list(bin_dir.rglob("*"))
        assert remaining == []


# ---------------------------------------------------------------------------
# auto_purge
# ---------------------------------------------------------------------------


def test_auto_purge_removes_old_entries(monkeypatch, tmp_path, cache_env):
    """auto_purge removes entries older than REPOMATIC_CACHE_MAX_AGE."""
    monkeypatch.setenv("REPOMATIC_CACHE_MAX_AGE", "7")

    source = tmp_path / "staging" / "tool"
    source.parent.mkdir()
    source.write_bytes(b"data")

    # Bypass auto_purge during store so we can set up state manually.
    with patch("repomatic.cache.auto_purge"):
        store_binary("ruff", "0.11.0", "linux-x64", source)

    # Age the cached binary to 10 days.
    cached = cached_binary_path("ruff", "0.11.0", "linux-x64", "tool")
    old_time = time.time() - 10 * 86400
    os.utime(cached, (old_time, old_time))

    auto_purge()
    assert BinaryCacheEntry.entries() == []


def test_auto_purge_disabled_when_zero(tmp_path, cache_env):
    """auto_purge does nothing when REPOMATIC_CACHE_MAX_AGE=0."""
    source = tmp_path / "staging" / "tool"
    source.parent.mkdir()
    source.write_bytes(b"data")

    with patch("repomatic.cache.auto_purge"):
        store_binary("ruff", "0.11.0", "linux-x64", source)

    # Age the cached binary.
    cached = cached_binary_path("ruff", "0.11.0", "linux-x64", "tool")
    old_time = time.time() - 365 * 86400
    os.utime(cached, (old_time, old_time))

    auto_purge()
    assert len(BinaryCacheEntry.entries()) == 1


def test_auto_purge_keeps_fresh_entries(monkeypatch, tmp_path, cache_env):
    """auto_purge keeps entries newer than the threshold."""
    monkeypatch.setenv("REPOMATIC_CACHE_MAX_AGE", "30")

    source = tmp_path / "staging" / "tool"
    source.parent.mkdir()
    source.write_bytes(b"data")

    with patch("repomatic.cache.auto_purge"):
        store_binary("ruff", "0.11.0", "linux-x64", source)

    auto_purge()
    assert len(BinaryCacheEntry.entries()) == 1


# ---------------------------------------------------------------------------
# _max_age_days
# ---------------------------------------------------------------------------


def test_max_age_days_default(monkeypatch):
    """Returns CacheConfig.max_age when env var is not set."""
    monkeypatch.delenv("REPOMATIC_CACHE_MAX_AGE", raising=False)
    assert _max_age_days() == CacheConfig.max_age


def test_max_age_days_custom(monkeypatch):
    """Reads integer from REPOMATIC_CACHE_MAX_AGE."""
    monkeypatch.setenv("REPOMATIC_CACHE_MAX_AGE", "7")
    assert _max_age_days() == 7


def test_max_age_days_zero_disables(monkeypatch):
    """REPOMATIC_CACHE_MAX_AGE=0 disables auto-purge."""
    monkeypatch.setenv("REPOMATIC_CACHE_MAX_AGE", "0")
    assert _max_age_days() == 0


def test_max_age_days_invalid_fallback(monkeypatch):
    """Invalid value falls back to default with a warning."""
    monkeypatch.setenv("REPOMATIC_CACHE_MAX_AGE", "not-a-number")
    assert _max_age_days() == CacheConfig.max_age


# ---------------------------------------------------------------------------
# _prune_empty_dirs
# ---------------------------------------------------------------------------


def test_prune_empty_dirs(tmp_path):
    """Removes empty nested directories but preserves root."""
    nested = tmp_path / "a" / "b" / "c"
    nested.mkdir(parents=True)
    _prune_empty_dirs(tmp_path)
    assert tmp_path.exists()
    assert not (tmp_path / "a").exists()


def test_prune_empty_dirs_preserves_nonempty(tmp_path):
    """Preserves directories that contain files."""
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "file.txt").write_text("keep", encoding="UTF-8")
    (tmp_path / "a" / "b").mkdir()
    _prune_empty_dirs(tmp_path)
    assert (tmp_path / "a").exists()
    assert not (tmp_path / "a" / "b").exists()


# ---------------------------------------------------------------------------
# HTTP response cache
# ---------------------------------------------------------------------------


def test_store_and_get_cached_response(cache_env):
    """Round-trip: store a response, then retrieve it."""
    data = b'{"name": "requests", "version": "2.31.0"}'
    store_response("pypi", "requests", data)

    result = get_cached_response("pypi", "requests", max_age_seconds=3600)
    assert result == data


def test_get_cached_response_zero_ttl(cache_env):
    """TTL=0 bypasses cache entirely."""
    store_response("pypi", "requests", b'{"cached": true}')
    assert get_cached_response("pypi", "requests", max_age_seconds=0) is None


def test_get_cached_response_negative_ttl(cache_env):
    """Negative TTL bypasses cache entirely."""
    store_response("pypi", "requests", b'{"cached": true}')
    assert get_cached_response("pypi", "requests", max_age_seconds=-1) is None


def test_get_cached_response_missing(cache_env):
    """Returns None when no cached response exists."""
    assert get_cached_response("pypi", "nonexistent", 3600) is None


def test_get_cached_response_stale(cache_env):
    """Returns None when cached response is older than max_age_seconds."""
    store_response("pypi", "requests", b'{"stale": true}')

    # Age the file.
    cached = cache_env / "http" / "pypi" / "requests.json"
    old_time = time.time() - 7200
    os.utime(cached, (old_time, old_time))

    assert get_cached_response("pypi", "requests", max_age_seconds=3600) is None


def test_store_response_nested_key(cache_env):
    """Keys with slashes create nested directories."""
    data = b'{"tag": "v1.0.0", "body": "release notes"}'
    store_response("github-release", "astral-sh/ruff/1.0.0", data)

    result = get_cached_response("github-release", "astral-sh/ruff/1.0.0", 86400)
    assert result == data

    expected_path = (
        cache_env / "http" / "github-release" / "astral-sh" / "ruff" / "1.0.0.json"
    )
    assert expected_path.exists()


def test_http_cache_info_lists_entries(cache_env):
    """Every cached response is listed."""
    store_response("pypi", "requests", b'{"a": 1}')
    store_response("pypi", "flask", b'{"b": 2}')
    store_response("github-releases", "astral-sh/ruff", b'{"c": 3}')

    entries = HttpCacheEntry.entries()
    assert len(entries) == 3
    namespaces = {e.namespace for e in entries}
    assert namespaces == {"github-releases", "pypi"}

    pypi_keys = {e.key for e in entries if e.namespace == "pypi"}
    assert pypi_keys == {"flask", "requests"}


def test_http_cache_info_empty(cache_env):
    """Returns empty list when HTTP cache is empty."""
    assert HttpCacheEntry.entries() == []


def test_clear_http_cache_all(cache_env):
    """A clear with no filter removes every HTTP entry."""
    store_response("pypi", "requests", b"data1")
    store_response("github-releases", "astral-sh/ruff", b"data2")

    deleted, freed = HttpCacheEntry.clear()
    assert deleted == 2
    assert freed > 0
    assert HttpCacheEntry.entries() == []


def test_clear_http_cache_by_namespace(cache_env):
    """A scoped clear removes only the entries of that namespace."""
    store_response("pypi", "requests", b"data1")
    store_response("github-releases", "astral-sh/ruff", b"data2")

    deleted, _ = HttpCacheEntry.clear(scope="pypi")
    assert deleted == 1
    entries = HttpCacheEntry.entries()
    assert len(entries) == 1
    assert entries[0].namespace == "github-releases"


def test_clear_http_cache_by_age(cache_env):
    """An age-limited clear removes only old HTTP entries."""
    store_response("pypi", "old-pkg", b"old")
    cached = cache_env / "http" / "pypi" / "old-pkg.json"
    old_time = time.time() - 10 * 86400
    os.utime(cached, (old_time, old_time))

    store_response("pypi", "new-pkg", b"new")

    deleted, _ = HttpCacheEntry.clear(max_age_days=5)
    assert deleted == 1
    entries = HttpCacheEntry.entries()
    assert len(entries) == 1
    assert entries[0].key == "new-pkg"


# ---------------------------------------------------------------------------
# Config cache
# ---------------------------------------------------------------------------


def test_store_config_keys_the_file_on_its_content(cache_env):
    """Different content lands on different files; the same content on one.

    Two checkouts that generate different configs for one tool get one file
    each, so no run reads the config of the other.
    """
    pears = store_config("yamllint", "yamllint.yaml", "fruit: pear\n")
    plums = store_config("yamllint", "yamllint.yaml", "fruit: plum\n")
    again = store_config("yamllint", "yamllint.yaml", "fruit: pear\n")

    assert pears is not None
    assert plums is not None
    assert pears != plums
    assert again == pears
    assert pears.read_text(encoding="UTF-8") == "fruit: pear\n"
    assert plums.read_text(encoding="UTF-8") == "fruit: plum\n"
    assert pears.name == plums.name == "yamllint.yaml"
    assert pears.parent.parent == cache_env / "config" / "yamllint"


def test_config_entries_list_both_layouts(cache_env):
    """A config with no digest directory is listed too, so a clean reaches it."""
    stored = store_config("biome", "biome.json", "{}\n")
    assert stored is not None
    flat = cache_env / "config" / "biome" / "biome.json"
    flat.write_text("{}\n", encoding="UTF-8")

    entries = ConfigCacheEntry.entries()

    assert {entry.path for entry in entries} == {stored, flat}
    assert {entry.digest for entry in entries} == {stored.parent.name, ""}
    assert all(entry.tool == "biome" for entry in entries)


def test_store_config_triggers_auto_purge(cache_env):
    """A stored config ages like every other entry, so a store sweeps the cache."""
    with patch("repomatic.cache.auto_purge") as mock_purge:
        store_config("yamllint", "yamllint.yaml", "fruit: pear\n")
    mock_purge.assert_called_once()


# ---------------------------------------------------------------------------
# clean
# ---------------------------------------------------------------------------


def _fill_every_cache(tmp_path: Path) -> None:
    """Store one binary and one config of `ruff`, and one PyPI response."""
    source = tmp_path / "staging" / "ruff"
    source.parent.mkdir()
    source.write_bytes(b"binary")
    store_binary("ruff", "0.11.0", "linux-x64", source)
    store_config("ruff", "ruff.toml", "line-length = 88\n")
    store_response("pypi", "requests", b"{}")


@pytest.mark.parametrize(
    ("options", "left"),
    [
        pytest.param({}, set(), id="everything"),
        pytest.param({"tool": "ruff"}, {"http"}, id="tool-spares-http"),
        pytest.param({"namespace": "pypi"}, {"binary", "config"}, id="namespace"),
        pytest.param({"tool": "biome"}, {"binary", "config", "http"}, id="other-tool"),
        pytest.param({"max_age_days": 30}, {"binary", "config", "http"}, id="fresh"),
    ],
)
def test_clean_reaches_the_caches_its_filters_apply_to(
    tmp_path, cache_env, options, left
):
    """A scope filter narrows only the caches it names, and spares the rest."""
    _fill_every_cache(tmp_path)

    deleted, _ = clean(**options)

    remaining = {row[0] for row in cache_rows()[0]}
    assert remaining == left
    assert deleted == 3 - len(left)


def test_auto_purge_reaches_configs(monkeypatch, tmp_path, cache_env):
    """A stale config is purged with the binaries and the HTTP responses."""
    monkeypatch.setenv("REPOMATIC_CACHE_MAX_AGE", "7")
    with patch("repomatic.cache.auto_purge"):
        stored = store_config("ruff", "ruff.toml", "line-length = 88\n")
    assert stored is not None
    old_time = time.time() - 10 * 86400
    os.utime(stored, (old_time, old_time))

    auto_purge()

    assert ConfigCacheEntry.entries() == []


def test_cache_rows_lists_every_cache_in_order(tmp_path, cache_env):
    """Binaries come first, then HTTP responses, then tool configs."""
    _fill_every_cache(tmp_path)

    rows, total = cache_rows()

    assert [row[0] for row in rows] == ["binary", "http", "config"]
    assert [row[1] for row in rows] == ["ruff", "pypi", "ruff"]
    assert total > 0


# ---------------------------------------------------------------------------
# lock_path
# ---------------------------------------------------------------------------


def test_lock_files_stay_out_of_listings_and_cleans(cache_env):
    """A lock file another run may hold is never listed nor deleted."""
    lock = lock_path("mdformat-0123456789abcdef")
    assert lock == cache_env / "locks" / "mdformat-0123456789abcdef.lock"
    lock.parent.mkdir(parents=True)
    lock.touch()

    clean()

    assert lock.is_file()
    assert cache_rows() == ([], 0)
