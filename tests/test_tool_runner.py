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

"""Tests for the unified tool runner."""

from __future__ import annotations

import hashlib
import io
import json
import logging
import re
import tarfile
import zipfile
from itertools import combinations
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import tomlrt
import yaml
from extra_platforms import (
    AARCH64,
    LINUX,
    MACOS,
    UBUNTU,
    WINDOWS,
    X86_64,
    Architecture,
    Group,
    Platform,
)

from repomatic.tool_runner import (
    _DIRECTIVE_YAML_OPTIONS_RE,
    CHECKSUMS,
    TOOL_REGISTRY,
    VERSIONS,
    ArchiveFormat,
    BinarySpec,
    NativeFormat,
    ToolSpec,
    _download_and_verify,
    _extract_binary,
    _fix_myst_directive_options,
    _install_binary,
    _reroot_section,
    _yaml_block_to_field_list,
    binary_tool_context,
    find_unmodified_configs,
    get_data_file_path,
    pyproject_table_to_flags,
    resolve_config,
    resolve_config_source,
    run_tool,
)

# ---------------------------------------------------------------------------
# ToolSpec and registry validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("name", "spec"), TOOL_REGISTRY.items())
def test_tool_spec_integrity(name, spec):
    """Each registry entry passes structural integrity checks."""
    # name matches registry key, is ASCII lowercase alphanumeric + hyphens.
    assert spec.name == name
    assert re.fullmatch(r"[a-z][a-z0-9-]*", name), f"{name}: invalid name format"

    # version is a pinned 2- or 3-component release. Most tools use 3-component
    # semver; Nuitka ships 2-component releases (like `4.1`).
    assert re.fullmatch(r"\d+\.\d+(\.\d+)?", spec.version), (
        f"{name}: version {spec.version!r} is not a pinned release"
    )

    # package, when set, must differ from name (otherwise use None).
    if spec.package is not None:
        assert spec.package != spec.name, (
            f"{name}: package equals name — set package=None instead"
        )

    # config_flag uses the long-form double-dash convention.
    if spec.config_flag is not None:
        assert spec.config_flag.startswith("--"), (
            f"{name}: config_flag {spec.config_flag!r} must start with --"
        )

    # Flags in default_flags and ci_flags that begin with "-" must use long form:
    # either POSIX "--foo" or Go-style "-foo" (more than one char after the dash).
    # Bare values like "88" or "github" interspersed with flags are left unchecked.
    for flag in spec.default_flags:
        if flag.startswith("-"):
            assert flag.startswith("--") or len(flag) > 2, (
                f"{name}: short flag {flag!r} in default_flags — use long form"
            )
    for flag in spec.ci_flags:
        if flag.startswith("-"):
            assert flag.startswith("--") or len(flag) > 2, (
                f"{name}: short flag {flag!r} in ci_flags — use long form"
            )

    # check_flags use the long-form double-dash convention, and only drive the
    # post_process warning — so they are meaningless without a post_process.
    for flag in spec.check_flags:
        assert flag.startswith("--") or len(flag) > 2, (
            f"{name}: short flag {flag!r} in check_flags — use long form"
        )
    if spec.check_flags:
        assert spec.post_process is not None, (
            f"{name}: check_flags set without a post_process callback"
        )

    # computed_params must also produce long-form flags.
    if spec.computed_params:
        from repomatic.metadata import Metadata

        with patch.object(Metadata, "__init__", lambda self: None):
            m = Metadata()
            # Provide minimal stubs for mypy_params.
            m.pyproject = None
            params = spec.computed_params(m) or []
            for flag in params:
                if flag.startswith("-"):
                    assert flag.startswith("--") or len(flag) > 2, (
                        f"{name}: short flag {flag!r} from computed_params — use long form"
                    )

    # No flag should appear in more than one of the flag-carrying fields.
    flag_fields = {
        "config_flag": {spec.config_flag} if spec.config_flag else set(),
        "default_flags": {f for f in spec.default_flags if f.startswith("-")},
        "ci_flags": {f for f in spec.ci_flags if f.startswith("-")},
    }
    for (field_a, flags_a), (field_b, flags_b) in combinations(flag_fields.items(), 2):
        overlap = flags_a & flags_b
        assert not overlap, f"{name}: {field_a} and {field_b} share flags: {overlap}"

    # needs_venv and binary are mutually exclusive.
    assert not (spec.needs_venv and spec.binary is not None), (
        f"{name}: needs_venv and binary are mutually exclusive"
    )

    # module requires needs_venv (module invocation uses the venv's Python).
    if spec.module is not None:
        assert spec.needs_venv, f"{name}: module requires needs_venv=True"

    # NativeFormat.FLAGS is mutually exclusive with file-based config: such tools
    # accept no config file and do not read [tool.X] natively.
    if spec.native_format is NativeFormat.FLAGS:
        assert not spec.reads_pyproject, (
            f"{name}: FLAGS format conflicts with reads_pyproject"
        )
        assert not spec.config_flag, f"{name}: FLAGS format conflicts with config_flag"
        assert not spec.native_config_files, (
            f"{name}: FLAGS format conflicts with native_config_files"
        )

    # with_packages is only meaningful for uvx-invoked tools.
    if spec.binary is not None:
        assert not spec.with_packages, f"{name}: binary tools cannot use with_packages"

    if spec.default_config:
        with get_data_file_path(spec.default_config) as path:
            assert path.exists(), f"{spec.default_config} not found in data/"
            content = path.read_text(encoding="UTF-8")
            if spec.native_format == NativeFormat.YAML:
                yaml.safe_load(content)
            elif spec.native_format == NativeFormat.TOML:
                tomlrt.loads(content)
            elif spec.native_format == NativeFormat.JSON:
                json.loads(content)
        assert spec.config_flag or spec.native_config_files, (
            f"{name} has default_config but no config_flag or native_config_files"
        )

    if spec.binary is not None:
        # Must have at least a Linux x86_64 binary (CI baseline).
        assert (LINUX, X86_64) in spec.binary.urls, (
            f"{name} binary missing (LINUX, X86_64) URL"
        )
        assert set(spec.binary.checksums.keys()) == set(spec.binary.urls.keys()), (
            f"{name}: checksum keys must match URL keys exactly"
        )

        # Every key must be a (Platform|Group, Architecture) tuple.
        for key in spec.binary.urls:
            assert isinstance(key, tuple) and len(key) == 2, (
                f"{name}: key {key!r} must be a (platform, architecture) tuple"
            )
            plat, arch = key
            assert isinstance(plat, (Platform, Group)), (
                f"{name}: {key!r} platform element must be Platform or Group"
            )
            assert isinstance(arch, Architecture), (
                f"{name}: {key!r} architecture element must be Architecture"
            )

        # Every URL must contain a {version} placeholder.
        for key, url in spec.binary.urls.items():
            assert "{version}" in url, (
                f"{name}/{key}: URL missing {{version}} placeholder"
            )

        # Checksums must be valid SHA-256 hex digests (64 lowercase hex chars).
        for key, checksum in spec.binary.checksums.items():
            assert re.fullmatch(r"[0-9a-f]{64}", checksum), (
                f"{name}/{key}: checksum is not a 64-char lowercase hex digest"
            )

        # URLs must be HTTPS.
        for key, url in spec.binary.urls.items():
            assert url.startswith("https://"), f"{name}/{key}: URL must use HTTPS"

        # archive_format: when a dict, all values must be ArchiveFormat
        # and all keys must be valid specifiers.
        if isinstance(spec.binary.archive_format, dict):
            for pk, fmt in spec.binary.archive_format.items():
                assert isinstance(fmt, ArchiveFormat), (
                    f"{name}: archive_format value for {pk!r} is not ArchiveFormat"
                )
                assert isinstance(pk, (tuple, Platform, Group)), (
                    f"{name}: archive_format key {pk!r} must be a "
                    f"PlatformKey tuple, Platform, or Group"
                )
        else:
            assert isinstance(spec.binary.archive_format, ArchiveFormat), (
                f"{name}: archive_format must be ArchiveFormat or dict"
            )

        # RAW archives should not have path separators in archive_executable.
        if (
            spec.binary.archive_format == ArchiveFormat.RAW
            and spec.binary.archive_executable is not None
        ):
            assert "/" not in spec.binary.archive_executable, (
                f"{name}: RAW archive_executable must not contain path separators"
            )


def test_tool_registry_sorted_alphabetically():
    """Registry keys are sorted alphabetically."""
    keys = list(TOOL_REGISTRY.keys())
    assert keys == sorted(keys)


def test_checksum_sidecar_covers_exactly_binary_tools():
    """`CHECKSUMS` and `VERSIONS` are keyed by exactly the binary tool names."""
    binary_tools = {n for n, s in TOOL_REGISTRY.items() if s.binary is not None}
    assert set(CHECKSUMS) == binary_tools
    assert set(VERSIONS) == binary_tools


@pytest.mark.parametrize(
    "name", [n for n, s in TOOL_REGISTRY.items() if s.binary is not None]
)
def test_checksum_version_matches_registry(name):
    """Each tool's sidecar version stamp matches its `ToolSpec.version`.

    The offline tripwire: a version bump that did not refresh the checksums
    leaves `VERSIONS` stale, failing here before the release is cut. Fix by
    running `repomatic update-checksums`.
    """
    spec = TOOL_REGISTRY[name]
    assert VERSIONS[name] == spec.version, (
        f"{name}: VERSIONS stamp ({VERSIONS[name]!r}) is stale vs "
        f"registry version ({spec.version!r})"
    )
    # Runtime checksums are sourced from the shared CHECKSUMS map.
    assert spec.binary is not None
    assert spec.binary.checksums is CHECKSUMS[name]


# ---------------------------------------------------------------------------
# Binary download infrastructure
# ---------------------------------------------------------------------------


def test_resolve_platform_exact_match():
    """Exact Platform match takes priority over Group membership."""
    spec = BinarySpec(
        urls={
            (LINUX, X86_64): "https://example.com/{version}/linux",
            (MACOS, AARCH64): "https://example.com/{version}/macos",
        },
        checksums={
            (LINUX, X86_64): "a" * 64,
            (MACOS, AARCH64): "b" * 64,
        },
        archive_format=ArchiveFormat.RAW,
    )
    with (
        patch("repomatic.tool_runner.current_platform", return_value=MACOS),
        patch("repomatic.tool_runner.current_architecture", return_value=AARCH64),
    ):
        assert spec.resolve_platform() == (MACOS, AARCH64)


def test_resolve_platform_group_match():
    """Group membership matches when no exact Platform key exists."""
    spec = BinarySpec(
        urls={(LINUX, X86_64): "https://example.com/{version}/linux"},
        checksums={(LINUX, X86_64): "a" * 64},
        archive_format=ArchiveFormat.RAW,
    )
    # Simulate an Ubuntu system (member of LINUX group).
    with (
        patch("repomatic.tool_runner.current_platform", return_value=UBUNTU),
        patch("repomatic.tool_runner.current_architecture", return_value=X86_64),
    ):
        assert spec.resolve_platform() == (LINUX, X86_64)


def test_resolve_platform_no_match():
    """No matching key raises RuntimeError."""
    spec = BinarySpec(
        urls={(LINUX, AARCH64): "https://example.com/{version}/tool"},
        checksums={(LINUX, AARCH64): "a" * 64},
        archive_format=ArchiveFormat.RAW,
    )
    with (
        patch("repomatic.tool_runner.current_platform", return_value=MACOS),
        patch("repomatic.tool_runner.current_architecture", return_value=X86_64),
        pytest.raises(RuntimeError, match="No binary"),
    ):
        spec.resolve_platform()


def test_platform_cache_key():
    """Cache key is a filesystem-safe string derived from the PlatformKey."""
    assert BinarySpec.platform_cache_key((LINUX, AARCH64)) == "linux-aarch64"
    assert BinarySpec.platform_cache_key((MACOS, X86_64)) == "macos-x86_64"
    assert BinarySpec.platform_cache_key((WINDOWS, X86_64)) == "windows-x86_64"


def test_download_and_verify_success(tmp_path):
    """Successful download with matching checksum writes the file."""
    content = b"hello binary world"
    expected = hashlib.sha256(content).hexdigest()
    dest = tmp_path / "downloaded"

    mock_response = MagicMock()
    mock_response.read = MagicMock(side_effect=[content, b""])
    mock_response.__enter__ = MagicMock(return_value=mock_response)
    mock_response.__exit__ = MagicMock(return_value=False)

    with patch("repomatic.tool_runner.urlopen", return_value=mock_response):
        _download_and_verify("https://example.com/file", expected, dest)

    assert dest.exists()
    assert dest.read_bytes() == content


def test_download_and_verify_no_content_length(tmp_path):
    """A response without Content-Length downloads via the indeterminate Spinner."""
    content = b"hello binary world"
    expected = hashlib.sha256(content).hexdigest()
    dest = tmp_path / "downloaded"

    mock_response = MagicMock()
    # No Content-Length: total stays 0, so the download path uses a Spinner
    # instead of a determinate progress bar.
    mock_response.headers.get = MagicMock(return_value=None)
    mock_response.read = MagicMock(side_effect=[content, b""])
    mock_response.__enter__ = MagicMock(return_value=mock_response)
    mock_response.__exit__ = MagicMock(return_value=False)

    with patch("repomatic.tool_runner.urlopen", return_value=mock_response):
        _download_and_verify("https://example.com/file", expected, dest)

    assert dest.exists()
    assert dest.read_bytes() == content


def test_download_and_verify_mismatch(tmp_path):
    """Checksum mismatch raises ValueError and cleans up."""
    content = b"hello binary world"
    dest = tmp_path / "downloaded"

    mock_response = MagicMock()
    mock_response.read = MagicMock(side_effect=[content, b""])
    mock_response.__enter__ = MagicMock(return_value=mock_response)
    mock_response.__exit__ = MagicMock(return_value=False)

    with (
        patch("repomatic.tool_runner.urlopen", return_value=mock_response),
        pytest.raises(ValueError, match="SHA-256 mismatch"),
    ):
        _download_and_verify("https://example.com/file", "bad" * 16, dest)

    assert not dest.exists()


def test_extract_binary_raw(tmp_path):
    """RAW format renames the archive to the executable name."""
    archive = tmp_path / "biome-linux-x64"
    archive.write_bytes(b"\x7fELF fake binary")

    spec = BinarySpec(
        urls={},
        checksums={},
        archive_format=ArchiveFormat.RAW,
        archive_executable="biome",
    )
    result = _extract_binary(archive, spec, tmp_path, "testtool")

    assert result == tmp_path / "biome"
    assert result.exists()
    assert result.stat().st_mode & 0o755


def _create_tar_gz(tmp_path, member_name, content=b"#!/bin/sh\necho hi"):
    """Create a tar.gz archive with a single member."""
    archive_path = tmp_path / "tool.tar.gz"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(name=member_name)
        info.size = len(content)
        tar.addfile(info, io.BytesIO(content))
    archive_path.write_bytes(buf.getvalue())
    return archive_path


def test_extract_binary_tar_gz(tmp_path):
    """TAR_GZ extracts the named executable and makes it executable."""
    archive = _create_tar_gz(tmp_path, "actionlint")

    spec = BinarySpec(
        urls={},
        checksums={},
        archive_format=ArchiveFormat.TAR_GZ,
        archive_executable="actionlint",
    )
    result = _extract_binary(archive, spec, tmp_path, "testtool")

    assert result == tmp_path / "actionlint"
    assert result.exists()
    assert result.stat().st_mode & 0o755


def test_extract_binary_tar_gz_with_strip_components(tmp_path):
    """TAR_GZ with strip_components strips leading path components."""
    archive = _create_tar_gz(tmp_path, "subdir/bin/mytool")

    spec = BinarySpec(
        urls={},
        checksums={},
        archive_format=ArchiveFormat.TAR_GZ,
        archive_executable="bin/mytool",
        strip_components=1,
    )
    result = _extract_binary(archive, spec, tmp_path, "testtool")

    assert result.name == "mytool"
    assert result.exists()
    assert result.stat().st_mode & 0o755


def test_extract_binary_tar_xz(tmp_path):
    """TAR_XZ extracts the named executable."""
    archive_path = tmp_path / "tool.tar.xz"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:xz") as tar:
        content = b"#!/bin/sh\necho hi"
        info = tarfile.TarInfo(name="lychee")
        info.size = len(content)
        tar.addfile(info, io.BytesIO(content))
    archive_path.write_bytes(buf.getvalue())

    spec = BinarySpec(
        urls={},
        checksums={},
        archive_format=ArchiveFormat.TAR_XZ,
        archive_executable="lychee",
    )
    result = _extract_binary(archive_path, spec, tmp_path, "testtool")

    assert result == tmp_path / "lychee"
    assert result.exists()


def test_extract_binary_tar_missing_executable(tmp_path):
    """Missing executable in tar archive raises FileNotFoundError."""
    archive = _create_tar_gz(tmp_path, "other_binary")

    spec = BinarySpec(
        urls={},
        checksums={},
        archive_format=ArchiveFormat.TAR_GZ,
        archive_executable="nonexistent",
    )
    with pytest.raises(FileNotFoundError, match="not found in archive"):
        _extract_binary(archive, spec, tmp_path, "testtool")


def test_extract_binary_tar_unsafe_path(tmp_path):
    """Archive member with path traversal raises ValueError."""
    archive = _create_tar_gz(tmp_path, "../../../etc/passwd")

    spec = BinarySpec(
        urls={},
        checksums={},
        archive_format=ArchiveFormat.TAR_GZ,
        archive_executable="../../../etc/passwd",
    )
    with pytest.raises(ValueError, match="Unsafe archive member"):
        _extract_binary(archive, spec, tmp_path, "testtool")


def _create_zip(tmp_path, member_name, content=b"MZ fake exe"):
    """Create a ZIP archive with a single member."""
    archive_path = tmp_path / "tool.zip"
    with zipfile.ZipFile(archive_path, "w") as zf:
        zf.writestr(member_name, content)
    return archive_path


def test_extract_binary_zip(tmp_path):
    """ZIP format extracts the named executable and makes it executable."""
    archive = _create_zip(tmp_path, "actionlint.exe")

    spec = BinarySpec(
        urls={},
        checksums={},
        archive_format=ArchiveFormat.ZIP,
    )
    result = _extract_binary(archive, spec, tmp_path, "actionlint")

    assert result == tmp_path / "actionlint.exe"
    assert result.exists()
    assert result.stat().st_mode & 0o755


def test_extract_binary_zip_with_strip_components(tmp_path):
    """ZIP with strip_components strips leading path components."""
    archive = _create_zip(tmp_path, "subdir/bin/mytool.exe")

    spec = BinarySpec(
        urls={},
        checksums={},
        archive_format=ArchiveFormat.ZIP,
        archive_executable="bin/mytool.exe",
        strip_components=1,
    )
    result = _extract_binary(archive, spec, tmp_path, "testtool")

    assert result.name == "mytool.exe"
    assert result.exists()
    assert result.stat().st_mode & 0o755


def test_extract_binary_zip_missing_executable(tmp_path):
    """Missing executable in ZIP archive raises FileNotFoundError."""
    archive = _create_zip(tmp_path, "other.exe")

    spec = BinarySpec(
        urls={},
        checksums={},
        archive_format=ArchiveFormat.ZIP,
    )
    with pytest.raises(FileNotFoundError, match="not found in archive"):
        _extract_binary(archive, spec, tmp_path, "nonexistent")


def test_extract_binary_zip_unsafe_path(tmp_path):
    """ZIP member with path traversal raises ValueError."""
    archive = _create_zip(tmp_path, "../../../etc/passwd")

    spec = BinarySpec(
        urls={},
        checksums={},
        archive_format=ArchiveFormat.ZIP,
        archive_executable="../../../etc/passwd",
    )
    with pytest.raises(ValueError, match="Unsafe archive member"):
        _extract_binary(archive, spec, tmp_path, "testtool")


def test_extract_binary_format_override(tmp_path):
    """Per-platform archive format from dict is used when passed."""
    archive = _create_zip(tmp_path, "gitleaks.exe")

    from extra_platforms import ALL_PLATFORMS

    spec = BinarySpec(
        urls={},
        checksums={},
        archive_format={
            ALL_PLATFORMS: ArchiveFormat.TAR_GZ,
            WINDOWS: ArchiveFormat.ZIP,
        },
    )
    # _install_binary resolves the format and passes it explicitly.
    result = _extract_binary(archive, spec, tmp_path, "gitleaks", ArchiveFormat.ZIP)

    assert result == tmp_path / "gitleaks.exe"
    assert result.exists()


def test_get_archive_format_dict_resolution():
    """Dict archive_format resolves Platform > Group membership."""
    from extra_platforms import ALL_PLATFORMS

    spec = BinarySpec(
        urls={},
        checksums={},
        archive_format={
            ALL_PLATFORMS: ArchiveFormat.TAR_GZ,
            WINDOWS: ArchiveFormat.ZIP,
        },
    )
    assert spec.get_archive_format((LINUX, X86_64)) == ArchiveFormat.TAR_GZ
    assert spec.get_archive_format((MACOS, AARCH64)) == ArchiveFormat.TAR_GZ
    assert spec.get_archive_format((WINDOWS, X86_64)) == ArchiveFormat.ZIP


def test_install_binary_missing_platform():
    """Missing platform key in binary spec raises RuntimeError."""
    spec = ToolSpec(
        name="testtool",
        version="1.0.0",
        package="testtool",
        binary=BinarySpec(
            urls={(LINUX, AARCH64): "https://example.com/{version}/tool"},
            checksums={(LINUX, AARCH64): "a" * 64},
            archive_format=ArchiveFormat.RAW,
            archive_executable="testtool",
        ),
    )
    with (
        patch("repomatic.tool_runner.current_platform", return_value=MACOS),
        patch("repomatic.tool_runner.current_architecture", return_value=X86_64),
        pytest.raises(RuntimeError, match="No binary for"),
    ):
        _install_binary(spec, Path("/tmp"))


def test_install_binary_cache_hit(tmp_path, monkeypatch):
    """_install_binary returns cached path when cache hit and sidecar matches."""
    monkeypatch.setenv("REPOMATIC_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("REPOMATIC_CACHE_MAX_AGE", "0")

    # Pre-populate the cache with a fake binary and its .sha256 sidecar.
    fake_binary = b"cached-binary-content"
    binary_checksum = hashlib.sha256(fake_binary).hexdigest()

    spec = ToolSpec(
        name="testtool",
        version="1.0.0",
        binary=BinarySpec(
            urls={(LINUX, X86_64): "https://example.com/{version}/tool"},
            checksums={(LINUX, X86_64): "archive-checksum-not-used-for-cache"},
            archive_format=ArchiveFormat.RAW,
        ),
    )

    from repomatic.cache import cached_binary_path

    cache_path = cached_binary_path("testtool", "1.0.0", "linux-x86_64", "testtool")
    cache_path.parent.mkdir(parents=True)
    cache_path.write_bytes(fake_binary)
    cache_path.chmod(0o755)
    # Write the sidecar with the binary's digest.
    sidecar = cache_path.with_suffix(cache_path.suffix + ".sha256")
    sidecar.write_text(binary_checksum, encoding="UTF-8")

    with (
        patch("repomatic.tool_runner.current_platform", return_value=UBUNTU),
        patch("repomatic.tool_runner.current_architecture", return_value=X86_64),
    ):
        result = _install_binary(spec, tmp_path / "staging")

    assert result == cache_path


def test_install_binary_cache_miss_stores(tmp_path, monkeypatch):
    """_install_binary stores the binary in cache after download on miss."""
    monkeypatch.setenv("REPOMATIC_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("REPOMATIC_CACHE_MAX_AGE", "0")

    fake_binary = b"downloaded-binary"
    checksum = hashlib.sha256(fake_binary).hexdigest()

    spec = ToolSpec(
        name="testtool",
        version="2.0.0",
        binary=BinarySpec(
            urls={(LINUX, X86_64): "https://example.com/{version}/tool.tar.gz"},
            checksums={(LINUX, X86_64): checksum},
            archive_format=ArchiveFormat.RAW,
        ),
    )

    staging = tmp_path / "staging"
    staging.mkdir()

    with (
        patch("repomatic.tool_runner.current_platform", return_value=UBUNTU),
        patch("repomatic.tool_runner.current_architecture", return_value=X86_64),
        patch("repomatic.tool_runner._download_and_verify"),
        patch("repomatic.tool_runner._extract_binary") as mock_extract,
    ):
        extracted = staging / "testtool"
        extracted.write_bytes(fake_binary)
        extracted.chmod(0o755)
        mock_extract.return_value = extracted

        result = _install_binary(spec, staging)

    from repomatic.cache import cached_binary_path

    expected_cache = cached_binary_path("testtool", "2.0.0", "linux-x86_64", "testtool")
    assert result == expected_cache
    assert expected_cache.read_bytes() == fake_binary
    # Sidecar must be written after cache store.
    sidecar = expected_cache.with_suffix(expected_cache.suffix + ".sha256")
    assert sidecar.is_file()
    assert (
        sidecar.read_text(encoding="UTF-8") == hashlib.sha256(fake_binary).hexdigest()
    )


def test_install_binary_no_cache_flag(tmp_path, monkeypatch):
    """_install_binary with no_cache=True bypasses cache entirely."""
    monkeypatch.setenv("REPOMATIC_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("REPOMATIC_CACHE_MAX_AGE", "0")

    spec = ToolSpec(
        name="testtool",
        version="1.0.0",
        binary=BinarySpec(
            urls={(LINUX, X86_64): "https://example.com/{version}/tool.tar.gz"},
            checksums={(LINUX, X86_64): "a" * 64},
            archive_format=ArchiveFormat.RAW,
        ),
    )

    staging = tmp_path / "staging"
    staging.mkdir()

    with (
        patch("repomatic.tool_runner.current_platform", return_value=UBUNTU),
        patch("repomatic.tool_runner.current_architecture", return_value=X86_64),
        patch("repomatic.tool_runner._download_and_verify"),
        patch("repomatic.tool_runner._extract_binary") as mock_extract,
        patch("repomatic.tool_runner.store_binary") as mock_store,
    ):
        extracted = staging / "testtool"
        extracted.write_bytes(b"binary")
        extracted.chmod(0o755)
        mock_extract.return_value = extracted

        result = _install_binary(spec, staging, no_cache=True)

    # Should NOT store in cache.
    mock_store.assert_not_called()
    assert result == extracted


def test_install_binary_cache_integrity_failure(tmp_path, monkeypatch):
    """_install_binary re-downloads when cached binary fails sidecar check."""
    monkeypatch.setenv("REPOMATIC_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("REPOMATIC_CACHE_MAX_AGE", "0")

    # Put a tampered binary in the cache with a sidecar for the original.
    from repomatic.cache import cached_binary_path

    cache_path = cached_binary_path("testtool", "1.0.0", "linux-x86_64", "testtool")
    cache_path.parent.mkdir(parents=True)
    cache_path.write_bytes(b"tampered-content")
    cache_path.chmod(0o755)
    # Sidecar records the digest of the original binary, not the tampered one.
    sidecar = cache_path.with_suffix(cache_path.suffix + ".sha256")
    sidecar.write_text(
        hashlib.sha256(b"original-content").hexdigest(), encoding="UTF-8"
    )

    spec = ToolSpec(
        name="testtool",
        version="1.0.0",
        binary=BinarySpec(
            urls={(LINUX, X86_64): "https://example.com/{version}/tool.tar.gz"},
            checksums={(LINUX, X86_64): "archive-checksum"},
            archive_format=ArchiveFormat.RAW,
        ),
    )

    staging = tmp_path / "staging"
    staging.mkdir()

    with (
        patch("repomatic.tool_runner.current_platform", return_value=UBUNTU),
        patch("repomatic.tool_runner.current_architecture", return_value=X86_64),
        patch("repomatic.tool_runner._download_and_verify"),
        patch("repomatic.tool_runner._extract_binary") as mock_extract,
    ):
        extracted = staging / "testtool"
        extracted.write_bytes(b"real-binary")
        extracted.chmod(0o755)
        mock_extract.return_value = extracted

        result = _install_binary(spec, staging)

    # Should have re-downloaded and re-cached.
    new_cached = cached_binary_path("testtool", "1.0.0", "linux-x86_64", "testtool")
    assert result == new_cached
    assert new_cached.read_bytes() == b"real-binary"


def test_install_binary_cache_store_fallback(tmp_path, monkeypatch):
    """_install_binary falls back to temp path when cached file is missing."""
    monkeypatch.setenv("REPOMATIC_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("REPOMATIC_CACHE_MAX_AGE", "0")

    fake_binary = b"downloaded-binary"
    checksum = hashlib.sha256(fake_binary).hexdigest()

    spec = ToolSpec(
        name="testtool",
        version="2.0.0",
        binary=BinarySpec(
            urls={(LINUX, X86_64): "https://example.com/{version}/tool.tar.gz"},
            checksums={(LINUX, X86_64): checksum},
            archive_format=ArchiveFormat.RAW,
        ),
    )

    staging = tmp_path / "staging"
    staging.mkdir()

    def fake_store(*args, **kwargs):
        """Return a cache path that doesn't exist on disk."""
        return tmp_path / "cache" / "bin" / "ghost" / "binary"

    with (
        patch("repomatic.tool_runner.current_platform", return_value=UBUNTU),
        patch("repomatic.tool_runner.current_architecture", return_value=X86_64),
        patch("repomatic.tool_runner._download_and_verify"),
        patch("repomatic.tool_runner._extract_binary") as mock_extract,
        patch("repomatic.tool_runner.store_binary", side_effect=fake_store),
    ):
        extracted = staging / "testtool"
        extracted.write_bytes(fake_binary)
        extracted.chmod(0o755)
        mock_extract.return_value = extracted

        result = _install_binary(spec, staging)

    # Should fall back to the temp directory copy.
    assert result == extracted
    assert result.read_bytes() == fake_binary


# ---------------------------------------------------------------------------
# run_tool with binary tools
# ---------------------------------------------------------------------------


@patch("repomatic.tool_runner.subprocess.run")
@patch("repomatic.tool_runner._install_binary")
@patch("repomatic.tool_runner.is_github_ci", return_value=False)
def test_run_tool_binary_uses_direct_path(
    mock_ci,
    mock_install,
    mock_run,
    tmp_path,
    monkeypatch,
):
    """Binary tools use the downloaded binary path, not uvx."""
    monkeypatch.chdir(tmp_path)
    bin_path = tmp_path / "typos"
    bin_path.touch()
    mock_install.return_value = bin_path
    mock_run.return_value = MagicMock(returncode=0)

    exit_code = run_tool("typos")

    assert exit_code == 0
    cmd = mock_run.call_args[0][0]
    assert cmd[0] == str(bin_path)
    assert "uvx" not in cmd
    assert "--write-changes" in cmd


@patch("repomatic.tool_runner.subprocess.run")
@patch("repomatic.tool_runner._install_binary")
@patch("repomatic.tool_runner.is_github_ci", return_value=False)
def test_run_tool_binary_forwards_extra_args(
    mock_ci,
    mock_install,
    mock_run,
    tmp_path,
    monkeypatch,
):
    """Extra args are appended after default flags for binary tools."""
    monkeypatch.chdir(tmp_path)
    bin_path = tmp_path / "biome"
    bin_path.touch()
    mock_install.return_value = bin_path
    mock_run.return_value = MagicMock(returncode=0)

    run_tool("biome", extra_args=("format", "--write", "file.json"))

    cmd = mock_run.call_args[0][0]
    assert cmd[0] == str(bin_path)
    assert "format" in cmd
    assert "--write" in cmd
    assert "file.json" in cmd


@patch("repomatic.tool_runner.subprocess.run")
@patch("repomatic.tool_runner._install_binary")
@patch("repomatic.tool_runner.is_github_ci", return_value=False)
def test_run_tool_binary_default_flags(
    mock_ci,
    mock_install,
    mock_run,
    tmp_path,
    monkeypatch,
):
    """Binary tools include default_flags in the command."""
    monkeypatch.chdir(tmp_path)
    bin_path = tmp_path / "actionlint"
    bin_path.touch()
    mock_install.return_value = bin_path
    mock_run.return_value = MagicMock(returncode=0)

    run_tool("actionlint")

    cmd = mock_run.call_args[0][0]
    assert "-color" in cmd


@patch("repomatic.tool_runner.subprocess.run")
@patch("repomatic.tool_runner.is_github_ci", return_value=False)
def test_run_tool_ruff_bundled_default(mock_ci, mock_run, tmp_path, monkeypatch):
    """ruff uses bundled default config when no config exists."""
    monkeypatch.chdir(tmp_path)
    mock_run.return_value = MagicMock(returncode=0)

    run_tool("ruff", extra_args=("check", "--output-format", "github"))

    cmd = mock_run.call_args[0][0]
    assert cmd[0] == "uvx"
    assert "ruff==0.15.5" in " ".join(cmd)
    assert "--config" in cmd
    assert "check" in cmd
    assert "--output-format" in cmd
    assert "github" in cmd


@patch("repomatic.tool_runner.subprocess.run")
@patch("repomatic.tool_runner.is_github_ci", return_value=False)
def test_run_tool_ruff_reads_pyproject_natively(
    mock_ci, mock_run, tmp_path, monkeypatch
):
    """ruff gets no --config flag when [tool.ruff] exists in pyproject.toml."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "test"\n\n[tool.ruff]\npreview = true\n'
    )
    mock_run.return_value = MagicMock(returncode=0)

    run_tool("ruff", extra_args=("check",))

    cmd = mock_run.call_args[0][0]
    assert cmd[0] == "uvx"
    assert "--config" not in cmd
    assert "check" in cmd


@patch("repomatic.tool_runner.subprocess.run")
@patch("repomatic.tool_runner.is_github_ci", return_value=False)
def test_run_tool_bump_my_version_via_uvx(mock_ci, mock_run, tmp_path, monkeypatch):
    """bump-my-version runs via uvx with subcommand extra_args."""
    monkeypatch.chdir(tmp_path)
    mock_run.return_value = MagicMock(returncode=0)

    run_tool("bump-my-version", extra_args=("bump", "--verbose", "patch"))

    cmd = mock_run.call_args[0][0]
    assert cmd[0] == "uvx"
    assert "bump-my-version==1.2.7" in " ".join(cmd)
    assert "bump" in cmd
    assert "--verbose" in cmd
    assert "patch" in cmd


# ---------------------------------------------------------------------------
# binary_tool_context
# ---------------------------------------------------------------------------


@patch("repomatic.tool_runner._install_binary")
def test_binary_tool_context_yields_path(mock_install, tmp_path):
    """binary_tool_context yields the installed binary path."""
    bin_path = tmp_path / "labelmaker"
    bin_path.touch()
    mock_install.return_value = bin_path

    with binary_tool_context("labelmaker") as lm:
        assert lm == bin_path
        assert lm.exists()


def test_binary_tool_context_no_binary_spec():
    """binary_tool_context raises for tools without a binary spec."""
    with (
        pytest.raises(AssertionError, match="no binary spec"),
        binary_tool_context("ruff"),
    ):
        pass


# ---------------------------------------------------------------------------
# Config resolution
# ---------------------------------------------------------------------------


def test_resolve_config_reads_pyproject_with_section():
    """Tools with reads_pyproject=True skip translation when [tool.X] exists."""
    spec = ToolSpec(
        name="testool",
        version="1.0.0",
        config_flag="--config",
        default_config="testool.toml",
        reads_pyproject=True,
    )
    args, tmp = resolve_config(spec, tool_config={"preview": True})
    assert args == []
    assert tmp is None


def test_resolve_config_reads_pyproject_falls_through_to_bundled(tmp_path, monkeypatch):
    """Tools with reads_pyproject=True use bundled default when no config exists."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("REPOMATIC_CACHE_DIR", str(tmp_path / "cache"))
    spec = ToolSpec(
        name="testool",
        version="1.0.0",
        config_flag="--config",
        native_format=NativeFormat.TOML,
        default_config="ruff.toml",
        reads_pyproject=True,
    )
    args, tmp = resolve_config(spec, tool_config={})
    assert len(args) == 2
    assert args[0] == "--config"
    assert Path(args[1]).exists()
    assert tmp is None


def test_resolve_config_native_file_wins(tmp_path, monkeypatch):
    """Native config file takes precedence over everything else."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".yamllint.yaml").write_text("rules: {}")

    spec = TOOL_REGISTRY["yamllint"]
    args, tmp = resolve_config(
        spec, tool_config={"rules": {"line-length": {"max": 80}}}
    )
    assert args == []
    assert tmp is None


def test_resolve_config_pyproject_section(tmp_path, monkeypatch):
    """[tool.X] in pyproject.toml produces a cached config file."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("REPOMATIC_CACHE_DIR", str(tmp_path / "cache"))

    spec = TOOL_REGISTRY["yamllint"]
    tool_config = {"rules": {"line-length": {"max": 80}}}
    args, tmp = resolve_config(spec, tool_config=tool_config)

    assert len(args) == 2
    assert args[0] == "--config-file"
    config_path = Path(args[1])
    assert config_path.exists()
    assert "cache" in str(config_path)

    # Verify content.
    content = config_path.read_text(encoding="UTF-8")
    parsed = yaml.safe_load(content)
    assert parsed == tool_config

    # Cache-based: no cleanup needed.
    assert tmp is None


def test_resolve_config_toml_translation(tmp_path, monkeypatch):
    """[tool.X] with native_format='toml' produces a valid TOML cached file."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("REPOMATIC_CACHE_DIR", str(tmp_path / "cache"))

    spec = ToolSpec(
        name="lychee",
        version="0.23.0",
        package="lychee",
        config_flag="--config",
        native_format=NativeFormat.TOML,
    )
    tool_config = {"max_redirects": 5, "exclude": ["example.com"]}
    args, tmp = resolve_config(spec, tool_config=tool_config)

    assert len(args) == 2
    assert args[0] == "--config"
    content = Path(args[1]).read_text(encoding="UTF-8")
    assert "max_redirects = 5" in content
    assert '"example.com"' in content
    assert tmp is None


def test_resolve_config_toml_nested_tables(tmp_path, monkeypatch):
    """Nested dicts in [tool.X] produce TOML table sections."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("REPOMATIC_CACHE_DIR", str(tmp_path / "cache"))

    spec = ToolSpec(
        name="lychee",
        version="0.23.0",
        package="lychee",
        config_flag="--config",
        native_format=NativeFormat.TOML,
    )
    tool_config = {"cache": {"enable": True, "max_age": 3600}}
    args, tmp = resolve_config(spec, tool_config=tool_config)

    content = Path(args[1]).read_text(encoding="UTF-8")
    assert "[cache]" in content
    assert "enable = true" in content
    assert "max_age = 3600" in content
    assert tmp is None


def test_resolve_config_toml_preserves_pyproject_comments(tmp_path, monkeypatch):
    """A [tool.X] TOML section materialised to a native file keeps its comments.

    Reading the section live (not via a plain-dict copy) lets the generated
    standalone file carry the user's comments while dropping the `[tool.X]`
    prefix.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("REPOMATIC_CACHE_DIR", str(tmp_path / "cache"))

    (tmp_path / "pyproject.toml").write_text(
        "[tool.gitleaks]\n"
        "# Recognisable title.\n"
        'title = "scan"  # shown in reports\n'
        "\n"
        "# Skip vendored trees.\n"
        "[tool.gitleaks.allowlist]\n"
        'paths = ["vendor/"]\n',
        encoding="UTF-8",
    )

    spec = ToolSpec(
        name="gitleaks",
        version="8.0.0",
        package="gitleaks",
        config_flag="--config",
        native_format=NativeFormat.TOML,
    )
    args, tmp = resolve_config(spec)

    content = Path(args[1]).read_text(encoding="UTF-8")
    # Standalone format: the [tool.gitleaks] prefix is reparented to the root.
    assert "[tool.gitleaks]" not in content
    assert "[allowlist]" in content
    # Comments survive the round-trip.
    assert "# Recognisable title." in content
    assert "# shown in reports" in content
    assert "# Skip vendored trees." in content
    assert tmp is None


def test_reroot_section_strips_prefix_and_keeps_comments():
    """_reroot_section reparents a [tool.X] section and preserves every comment."""
    section = tomlrt.loads(
        "[tool.x]\n"
        "# leading\n"
        "a = 1  # eol\n"
        "[tool.x.sub]\n"
        "# nested\n"
        "b = 2\n"
        "[[tool.x.items]]\n"
        "# aot\n"
        "c = 3\n"
    )["tool"]["x"]

    out = tomlrt.dumps(_reroot_section(section))

    assert "[sub]" in out and "[tool.x.sub]" not in out
    assert "[[items]]" in out and "[[tool.x.items]]" not in out
    for comment in ("# leading", "# eol", "# nested", "# aot"):
        assert comment in out


def test_reroot_section_inline_expands_without_raising():
    """An inline `tool.x = {...}` table expands instead of hitting the comment API."""
    section = tomlrt.loads("[tool]\nx = { a = 1, sub = { b = 2 } }\n")["tool"]["x"]

    out = tomlrt.dumps(_reroot_section(section))

    assert "a = 1" in out
    assert "[sub]" in out
    assert "b = 2" in out


def test_resolve_config_json_translation(tmp_path, monkeypatch):
    """[tool.X] with native_format='json' produces a valid JSON cached file."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("REPOMATIC_CACHE_DIR", str(tmp_path / "cache"))

    spec = ToolSpec(
        name="biome",
        version="2.4.5",
        package="biome",
        config_flag="--config-path",
        native_format=NativeFormat.JSON,
    )
    tool_config = {"formatter": {"indentStyle": "space", "indentWidth": 2}}
    args, tmp = resolve_config(spec, tool_config=tool_config)

    assert len(args) == 2
    assert args[0] == "--config-path"
    content = Path(args[1]).read_text(encoding="UTF-8")
    parsed = json.loads(content)
    assert parsed == tool_config
    assert tmp is None


def test_resolve_config_cwd_write_no_config_flag(tmp_path, monkeypatch):
    """CWD-discovery tools write translated [tool.X] to the native config path."""
    monkeypatch.chdir(tmp_path)

    spec = ToolSpec(
        name="mdformat",
        version="1.0.0",
        package="mdformat",
        native_config_files=(".mdformat.toml",),
        native_format=NativeFormat.TOML,
    )
    args, tmp = resolve_config(spec, tool_config={"number": True})

    try:
        assert args == []
        assert tmp is not None
        assert tmp == Path(".mdformat.toml")
        content = tmp.read_text(encoding="UTF-8")
        assert "number = true" in content
    finally:
        if tmp:
            tmp.unlink(missing_ok=True)


def test_resolve_config_no_config_flag_no_native_files_raises(tmp_path, monkeypatch):
    """Tools with no config_flag and no native_config_files raise NotImplementedError."""
    monkeypatch.chdir(tmp_path)

    spec = ToolSpec(
        name="sometool",
        version="1.0.0",
        package="sometool",
    )
    with pytest.raises(NotImplementedError, match="no config_flag"):
        resolve_config(spec, tool_config={"key": "value"})


def test_resolve_config_bundled_default(tmp_path, monkeypatch):
    """Bundled default is cached and passed via --config flag."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("REPOMATIC_CACHE_DIR", str(tmp_path / "cache"))

    spec = TOOL_REGISTRY["yamllint"]
    args, tmp = resolve_config(spec, tool_config={})
    assert len(args) == 2
    assert args[0] == "--config-file"
    assert Path(args[1]).exists()
    assert "cache" in str(args[1])
    assert tmp is None


def test_resolve_config_bare_invocation(tmp_path, monkeypatch):
    """Tool with no config at all gets bare invocation."""
    monkeypatch.chdir(tmp_path)

    spec = ToolSpec(
        name="sometool",
        version="1.0.0",
        package="sometool",
        native_format=NativeFormat.YAML,
    )
    args, tmp = resolve_config(spec, tool_config={})
    assert args == []
    assert tmp is None


def test_resolve_config_empty_tool_config_is_not_match(tmp_path, monkeypatch):
    """An empty [tool.X] dict does not count as a config match."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("REPOMATIC_CACHE_DIR", str(tmp_path / "cache"))

    spec = TOOL_REGISTRY["zizmor"]
    args, tmp = resolve_config(spec, tool_config={})
    assert len(args) == 2
    assert args[0] == "--config"
    assert Path(args[1]).exists()
    assert tmp is None


# ---------------------------------------------------------------------------
# resolve_config_source
# ---------------------------------------------------------------------------


def test_resolve_config_source_native_file(tmp_path, monkeypatch):
    """Reports native config file when it exists."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "zizmor.yaml").write_text("rules: {}")

    result = resolve_config_source(TOOL_REGISTRY["zizmor"])
    assert result == "zizmor.yaml"


def test_resolve_config_source_bundled_default(tmp_path, monkeypatch):
    """Reports bundled default when no other config exists."""
    monkeypatch.chdir(tmp_path)

    result = resolve_config_source(TOOL_REGISTRY["yamllint"])
    assert result == "bundled default"


def test_resolve_config_source_pyproject_section(tmp_path, monkeypatch):
    """Reports [tool.X] when pyproject.toml has the section."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        "[tool.yamllint]\nrules = {line-length = {max = 80}}\n"
    )

    result = resolve_config_source(TOOL_REGISTRY["yamllint"])
    assert result == "[tool.yamllint] in pyproject.toml"


def test_resolve_config_source_reads_pyproject_bundled_fallback(tmp_path, monkeypatch):
    """Reports bundled default for reads_pyproject tool when no config exists."""
    monkeypatch.chdir(tmp_path)

    result = resolve_config_source(TOOL_REGISTRY["ruff"])
    assert result == "bundled default"


def test_resolve_config_source_reads_pyproject_native(tmp_path, monkeypatch):
    """Reports [tool.X] for reads_pyproject tool when pyproject section exists."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "test"\n\n[tool.ruff]\npreview = true\n'
    )

    result = resolve_config_source(TOOL_REGISTRY["ruff"])
    assert result == "[tool.ruff] in pyproject.toml"


def test_resolve_config_source_bare(tmp_path, monkeypatch):
    """Reports (bare) for tool with no config anywhere."""
    monkeypatch.chdir(tmp_path)

    spec = ToolSpec(
        name="sometool",
        version="1.0.0",
        package="sometool",
    )
    result = resolve_config_source(spec)
    assert result == "(bare)"


# ---------------------------------------------------------------------------
# run_tool
# ---------------------------------------------------------------------------


def test_run_tool_unknown_tool():
    """Raise ValueError for unregistered tool names."""
    with pytest.raises(ValueError, match="Unknown tool"):
        run_tool("nonexistent-tool")


@patch("repomatic.tool_runner.subprocess.run")
@patch("repomatic.tool_runner.is_github_ci", return_value=False)
def test_run_tool_yamllint_bundled_default(mock_ci, mock_run, tmp_path, monkeypatch):
    """yamllint with bundled default builds the correct command."""
    monkeypatch.chdir(tmp_path)
    mock_run.return_value = MagicMock(returncode=0)

    exit_code = run_tool("yamllint", extra_args=(".",))

    assert exit_code == 0
    mock_run.assert_called_once()
    cmd = mock_run.call_args[0][0]
    assert cmd[0] == "uvx"
    assert "--no-progress" in cmd
    assert "yamllint==1.38.0" in " ".join(cmd)
    assert "--config-file" in cmd
    # default_flags are always present.
    assert "--strict" in cmd
    assert "." in cmd
    # Should not have CI flags.
    assert "--format" not in cmd


@patch("repomatic.tool_runner.subprocess.run")
@patch("repomatic.tool_runner.is_github_ci", return_value=True)
def test_run_tool_ci_flags(mock_ci, mock_run, tmp_path, monkeypatch):
    """CI flags are appended when GITHUB_ACTIONS is set."""
    monkeypatch.chdir(tmp_path)
    mock_run.return_value = MagicMock(returncode=0)

    run_tool("yamllint", extra_args=(".",))

    cmd = mock_run.call_args[0][0]
    # default_flags always present.
    assert "--strict" in cmd
    # CI flags should be present.
    assert "--format" in cmd
    idx = cmd.index("--format")
    assert cmd[idx + 1] == "github"


@patch("repomatic.tool_runner.subprocess.run")
@patch("repomatic.tool_runner.is_github_ci", return_value=False)
def test_run_tool_native_config_no_extra_flags(
    mock_ci, mock_run, tmp_path, monkeypatch
):
    """Tool with native config file gets no config flags from repomatic."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "zizmor.yaml").write_text("rules: {}")
    mock_run.return_value = MagicMock(returncode=0)

    run_tool("zizmor", extra_args=(".",))

    cmd = mock_run.call_args[0][0]
    assert "--config" not in cmd
    # default_flags are always present even with native config.
    assert "--offline" in cmd


@patch("repomatic.tool_runner.subprocess.run")
@patch("repomatic.tool_runner.is_github_ci", return_value=False)
def test_run_tool_pyproject_section_cached_config(
    mock_ci,
    mock_run,
    tmp_path,
    monkeypatch,
):
    """[tool.X] translation writes config to cache and passes via --config."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("REPOMATIC_CACHE_DIR", str(tmp_path / "cache"))
    (tmp_path / "pyproject.toml").write_text(
        "[tool.zizmor]\n[tool.zizmor.rules.artipacked]\ndisable = true\n"
    )
    mock_run.return_value = MagicMock(returncode=0)

    run_tool("zizmor", extra_args=(".",))

    cmd = mock_run.call_args[0][0]
    assert "--config" in cmd
    config_idx = cmd.index("--config")
    config_file = Path(cmd[config_idx + 1])
    # Cache-based config persists after run.
    assert config_file.exists()
    assert "cache" in str(config_file)


@patch("repomatic.tool_runner.subprocess.run")
@patch("repomatic.tool_runner.is_github_ci", return_value=False)
def test_run_tool_forwards_exit_code(mock_ci, mock_run, tmp_path, monkeypatch):
    """Tool's exit code is forwarded unchanged."""
    monkeypatch.chdir(tmp_path)
    mock_run.return_value = MagicMock(returncode=42)

    exit_code = run_tool("yamllint", extra_args=(".",))
    assert exit_code == 42


@patch("repomatic.tool_runner.subprocess.run")
@patch("repomatic.tool_runner.is_github_ci", return_value=False)
def test_run_tool_autopep8_default_flags(mock_ci, mock_run, tmp_path, monkeypatch):
    """autopep8 runs with all default flags via uvx."""
    monkeypatch.chdir(tmp_path)
    mock_run.return_value = MagicMock(returncode=0)

    run_tool("autopep8", extra_args=("file.py",))

    cmd = mock_run.call_args[0][0]
    assert cmd[0] == "uvx"
    assert "autopep8==2.3.2" in " ".join(cmd)
    assert "--recursive" in cmd
    assert "--in-place" in cmd
    assert "--max-line-length" in cmd
    assert "88" in cmd
    assert "--select" in cmd
    assert "E501" in cmd
    assert "file.py" in cmd


@patch("repomatic.tool_runner.subprocess.run")
@patch("repomatic.tool_runner.is_github_ci", return_value=False)
def test_run_tool_pyproject_fmt(mock_ci, mock_run, tmp_path, monkeypatch):
    """pyproject-fmt runs bare via uvx, with no forced --expand-tables flag."""
    monkeypatch.chdir(tmp_path)
    mock_run.return_value = MagicMock(returncode=0)

    run_tool("pyproject-fmt", extra_args=("pyproject.toml",))

    cmd = mock_run.call_args[0][0]
    assert cmd[0] == "uvx"
    assert "pyproject-fmt==2.25.1" in " ".join(cmd)
    assert "pyproject.toml" in cmd
    # No forced formatting preference: pyproject-fmt uses its own defaults.
    assert "--expand-tables" not in cmd
    assert "--config" not in cmd


@patch("repomatic.tool_runner.subprocess.run")
@patch("repomatic.tool_runner.is_github_ci", return_value=False)
def test_run_tool_mdformat_with_packages(mock_ci, mock_run, tmp_path, monkeypatch):
    """mdformat runs via uvx with all plugin packages."""
    monkeypatch.chdir(tmp_path)
    mock_run.return_value = MagicMock(returncode=0)

    run_tool("mdformat", extra_args=("readme.md",))

    cmd = mock_run.call_args[0][0]
    assert cmd[0] == "uvx"
    assert "mdformat==1.0.0" in " ".join(cmd)
    assert "--number" not in cmd
    assert "--strict-front-matter" in cmd
    assert "readme.md" in cmd
    # Verify plugins are passed as --with flags.
    with_count = cmd.count("--with")
    spec = TOOL_REGISTRY["mdformat"]
    assert with_count == len(spec.with_packages)


@patch("repomatic.tool_runner.subprocess.run")
@patch("repomatic.metadata.Metadata")
@patch("repomatic.tool_runner.is_github_ci", return_value=False)
def test_run_tool_mypy_with_computed_params(
    mock_ci,
    mock_metadata_cls,
    mock_run,
    tmp_path,
    monkeypatch,
):
    """mypy runs via uv run with computed --python-version param."""
    monkeypatch.chdir(tmp_path)
    mock_metadata_cls.return_value.mypy_params = ["--python-version", "3.10"]
    mock_run.return_value = MagicMock(returncode=0)

    run_tool("mypy", extra_args=("repomatic/",))

    cmd = mock_run.call_args[0][0]
    assert cmd[0] == "uv"
    assert "--no-progress" in cmd
    assert "run" in cmd
    assert "--frozen" in cmd
    assert "mypy==1.19.1" in " ".join(cmd)
    assert "--color-output" in cmd
    assert "--python-version" in cmd
    assert "3.10" in cmd
    assert "repomatic/" in cmd


@patch("repomatic.tool_runner.subprocess.run")
@patch("repomatic.metadata.Metadata")
@patch("repomatic.tool_runner.is_github_ci", return_value=False)
def test_run_tool_mypy_without_computed_params(
    mock_ci,
    mock_metadata_cls,
    mock_run,
    tmp_path,
    monkeypatch,
):
    """mypy runs without computed params when Metadata returns None."""
    monkeypatch.chdir(tmp_path)
    mock_metadata_cls.return_value.mypy_params = None
    mock_run.return_value = MagicMock(returncode=0)

    run_tool("mypy", extra_args=("repomatic/",))

    cmd = mock_run.call_args[0][0]
    assert "--color-output" in cmd
    assert "--python-version" not in cmd


@patch("repomatic.tool_runner.subprocess.run")
@patch("repomatic.tool_runner.is_github_ci", return_value=False)
def test_run_tool_nuitka_uses_module_invocation(
    mock_ci, mock_run, tmp_path, monkeypatch
):
    """nuitka runs via `python -m nuitka` to avoid Windows script resolution issues."""
    monkeypatch.chdir(tmp_path)
    mock_run.return_value = MagicMock(returncode=0)

    run_tool("nuitka", extra_args=("repomatic",))

    cmd = mock_run.call_args[0][0]
    assert cmd[0] == "uv"
    assert "run" in cmd
    assert "--frozen" in cmd
    python_idx = cmd.index("python")
    assert cmd[python_idx + 1] == "-m"
    assert cmd[python_idx + 2] == "nuitka"
    assert "repomatic" in cmd


# ---------------------------------------------------------------------------
# run_tool --output directory creation
# ---------------------------------------------------------------------------


@patch("repomatic.tool_runner.subprocess.run")
@patch("repomatic.tool_runner._install_binary")
@patch("repomatic.tool_runner.is_github_ci", return_value=False)
def test_run_tool_creates_output_parent_directory(
    mock_ci,
    mock_install,
    mock_run,
    tmp_path,
    monkeypatch,
):
    """run_tool creates parent directories for --output file paths."""
    monkeypatch.chdir(tmp_path)
    bin_path = tmp_path / "lychee"
    bin_path.touch()
    mock_install.return_value = bin_path
    mock_run.return_value = MagicMock(returncode=0)

    output_path = tmp_path / "subdir" / "nested" / "out.md"
    run_tool("lychee", extra_args=("--output", str(output_path), "readme.md"))

    assert output_path.parent.is_dir()


@patch("repomatic.tool_runner.subprocess.run")
@patch("repomatic.tool_runner._install_binary")
@patch("repomatic.tool_runner.is_github_ci", return_value=False)
def test_run_tool_output_existing_directory_is_noop(
    mock_ci,
    mock_install,
    mock_run,
    tmp_path,
    monkeypatch,
):
    """run_tool does not fail when --output parent directory already exists."""
    monkeypatch.chdir(tmp_path)
    bin_path = tmp_path / "lychee"
    bin_path.touch()
    mock_install.return_value = bin_path
    mock_run.return_value = MagicMock(returncode=0)

    output_dir = tmp_path / "existing"
    output_dir.mkdir()
    run_tool("lychee", extra_args=("--output", str(output_dir / "out.md")))

    assert output_dir.is_dir()


@patch("repomatic.tool_runner.subprocess.run")
@patch("repomatic.tool_runner.is_github_ci", return_value=False)
def test_run_tool_no_output_flag_skips_mkdir(mock_ci, mock_run, tmp_path, monkeypatch):
    """run_tool without --output does not create any directories."""
    monkeypatch.chdir(tmp_path)
    mock_run.return_value = MagicMock(returncode=0)

    run_tool("yamllint", extra_args=(".",))

    # Only the tmp_path itself should exist; no new subdirectories.
    assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------
# get_data_file_path
# ---------------------------------------------------------------------------


def test_get_data_file_path_existing():
    """Bundled data files are accessible via get_data_file_path."""
    with get_data_file_path("zizmor.yaml") as path:
        assert path.exists()
        content = path.read_text(encoding="UTF-8")
        assert "rules:" in content


def test_get_data_file_path_missing():
    """Missing data files raise FileNotFoundError."""
    with (
        pytest.raises(FileNotFoundError, match="not found"),
        get_data_file_path("nonexistent.yaml"),
    ):
        pass


# ---------------------------------------------------------------------------
# find_unmodified_configs
# ---------------------------------------------------------------------------


def test_find_unmodified_configs_exact_match(tmp_path, monkeypatch):
    """Native config file matching bundled default is flagged as unmodified."""
    monkeypatch.chdir(tmp_path)

    with get_data_file_path("yamllint.yaml") as bundled:
        bundled_content = bundled.read_text(encoding="UTF-8")

    (tmp_path / ".yamllint.yaml").write_text(bundled_content, encoding="UTF-8")

    result = find_unmodified_configs()
    paths = [p for _, p in result]
    assert ".yamllint.yaml" in paths


def test_find_unmodified_configs_trailing_whitespace(tmp_path, monkeypatch):
    """Trailing whitespace differences are normalized away."""
    monkeypatch.chdir(tmp_path)

    with get_data_file_path("yamllint.yaml") as bundled:
        bundled_content = bundled.read_text(encoding="UTF-8")

    (tmp_path / ".yamllint.yaml").write_text(
        bundled_content.rstrip() + "\n\n\n", encoding="UTF-8"
    )

    result = find_unmodified_configs()
    paths = [p for _, p in result]
    assert ".yamllint.yaml" in paths


def test_find_unmodified_configs_modified_content(tmp_path, monkeypatch):
    """Native config with different content is not flagged."""
    monkeypatch.chdir(tmp_path)

    (tmp_path / ".yamllint.yaml").write_text(
        "rules:\n  line-length:\n    max: 80\n", encoding="UTF-8"
    )

    result = find_unmodified_configs()
    paths = [p for _, p in result]
    assert ".yamllint.yaml" not in paths


def test_find_unmodified_configs_no_file(tmp_path, monkeypatch):
    """No native config on disk returns empty list."""
    monkeypatch.chdir(tmp_path)

    result = find_unmodified_configs()
    assert result == []


def test_find_unmodified_configs_multiple_tools(tmp_path, monkeypatch):
    """Redundant files for multiple tools are all detected."""
    monkeypatch.chdir(tmp_path)

    for data_name, native_name in (
        ("yamllint.yaml", ".yamllint.yaml"),
        ("zizmor.yaml", "zizmor.yaml"),
    ):
        with get_data_file_path(data_name) as bundled:
            content = bundled.read_text(encoding="UTF-8")
        (tmp_path / native_name).write_text(content, encoding="UTF-8")

    result = find_unmodified_configs()
    tools = {t for t, _ in result}
    assert "yamllint" in tools
    assert "zizmor" in tools


def test_find_unmodified_configs_alternative_filename(tmp_path, monkeypatch):
    """Alternative native config filename (.yamllint.yml) is also checked."""
    monkeypatch.chdir(tmp_path)

    with get_data_file_path("yamllint.yaml") as bundled:
        bundled_content = bundled.read_text(encoding="UTF-8")

    (tmp_path / ".yamllint.yml").write_text(bundled_content, encoding="UTF-8")

    result = find_unmodified_configs()
    paths = [p for _, p in result]
    assert ".yamllint.yml" in paths


# ---------------------------------------------------------------------------
# MyST directive options post-processing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("before", "after"),
    [
        pytest.param(
            "```{py:module} extra_platforms.detection\n"
            "---\n"
            "no-typesetting:\n"
            "no-contents-entry:\n"
            "---\n"
            "```\n",
            "```{py:module} extra_platforms.detection\n"
            ":no-typesetting:\n"
            ":no-contents-entry:\n"
            "```\n",
            id="backtick-fence-flags",
        ),
        pytest.param(
            "```{directive} arg\n---\nclass: my-class\nname: my-name\n---\n```\n",
            "```{directive} arg\n:class: my-class\n:name: my-name\n```\n",
            id="backtick-fence-key-value",
        ),
        pytest.param(
            ":::{note}\n---\nclass: special\n---\n:::\n",
            ":::{note}\n:class: special\n:::\n",
            id="colon-fence",
        ),
        pytest.param(
            "````{directive} arg\n---\nkey: value\n---\n````\n",
            "````{directive} arg\n:key: value\n````\n",
            id="four-backtick-fence",
        ),
    ],
)
def test_directive_yaml_options_regex(before, after):
    """YAML-block directive options are converted to field-list syntax."""
    assert _DIRECTIVE_YAML_OPTIONS_RE.sub(_yaml_block_to_field_list, before) == after


@pytest.mark.parametrize(
    "content",
    [
        pytest.param(
            "---\ntitle: My Doc\n---\n\n# Hello\n",
            id="yaml-frontmatter",
        ),
        pytest.param(
            "Some text\n\n---\n\nMore text\n",
            id="horizontal-rule",
        ),
        pytest.param(
            "```python\nprint('hello')\n```\n",
            id="plain-code-fence",
        ),
    ],
)
def test_directive_yaml_options_regex_no_false_positives(content):
    """Non-directive YAML blocks and horizontal rules are left untouched."""
    assert _DIRECTIVE_YAML_OPTIONS_RE.sub(_yaml_block_to_field_list, content) == content


def test_fix_myst_directive_options_in_place(tmp_path):
    """Post-processor rewrites files in-place and skips unchanged files."""
    affected = tmp_path / "affected.md"
    affected.write_text(
        "# Title\n\n```{py:module} mymod\n---\nno-typesetting:\n---\n```\n",
        encoding="utf-8",
    )

    untouched = tmp_path / "untouched.md"
    original = "# Plain markdown\n\nNo directives here.\n"
    untouched.write_text(original, encoding="utf-8")

    _fix_myst_directive_options([str(affected), str(untouched), "/nonexistent/path"])

    assert affected.read_text(encoding="utf-8") == (
        "# Title\n\n```{py:module} mymod\n:no-typesetting:\n```\n"
    )
    assert untouched.read_text(encoding="utf-8") == original


def test_fix_myst_directive_options_multiple_directives(tmp_path):
    """Multiple directive blocks in the same file are all fixed."""
    md = tmp_path / "multi.md"
    md.write_text(
        "```{py:module} mod_a\n"
        "---\n"
        "no-typesetting:\n"
        "no-contents-entry:\n"
        "---\n"
        "```\n"
        "\n"
        "Some text.\n"
        "\n"
        "```{py:module} mod_b\n"
        "---\n"
        "no-typesetting:\n"
        "---\n"
        "```\n",
        encoding="utf-8",
    )

    _fix_myst_directive_options([str(md)])

    assert md.read_text(encoding="utf-8") == (
        "```{py:module} mod_a\n"
        ":no-typesetting:\n"
        ":no-contents-entry:\n"
        "```\n"
        "\n"
        "Some text.\n"
        "\n"
        "```{py:module} mod_b\n"
        ":no-typesetting:\n"
        "```\n"
    )


def test_check_bypasses_post_process():
    """check_bypasses_post_process is True only for a post_process tool in check mode."""
    mdformat = TOOL_REGISTRY["mdformat"]
    assert mdformat.check_bypasses_post_process(("--check", "readme.md"))
    assert not mdformat.check_bypasses_post_process(("readme.md",))
    # Tools without a post_process callback are authoritative in check mode.
    ruff = TOOL_REGISTRY["ruff"]
    assert not ruff.check_bypasses_post_process(("format", "--check"))


@patch("repomatic.tool_runner.subprocess.run")
@patch("repomatic.tool_runner.is_github_ci", return_value=False)
def test_run_tool_warns_when_check_bypasses_post_process(
    mock_ci,
    mock_run,
    tmp_path,
    monkeypatch,
    caplog,
):
    """mdformat --check warns that the post-process fixup is skipped."""
    monkeypatch.chdir(tmp_path)
    mock_run.return_value = MagicMock(returncode=1)

    with caplog.at_level(logging.WARNING):
        run_tool("mdformat", extra_args=("--check", "readme.md"))

    assert "check mode" in caplog.text


@patch("repomatic.tool_runner.subprocess.run")
@patch("repomatic.tool_runner.is_github_ci", return_value=False)
def test_run_tool_no_check_warning_in_write_mode(
    mock_ci,
    mock_run,
    tmp_path,
    monkeypatch,
    caplog,
):
    """mdformat in write mode (no check flag) emits no check-mode warning."""
    monkeypatch.chdir(tmp_path)
    mock_run.return_value = MagicMock(returncode=0)

    with caplog.at_level(logging.WARNING):
        run_tool("mdformat", extra_args=("readme.md",))

    assert "check mode" not in caplog.text


# ---------------------------------------------------------------------------
# [tool.X] to CLI flags translation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("table", "expected"),
    [
        ({}, []),
        ({"onefile": True}, ["--onefile"]),
        ({"standalone": False}, []),
        (
            {"product-name": "Meta Package Manager"},
            ["--product-name=Meta Package Manager"],
        ),
        ({"jobs": 4}, ["--jobs=4"]),
        (
            {"include-package-data": ["click_extra", "foo"]},
            ["--include-package-data=click_extra", "--include-package-data=foo"],
        ),
        (
            {
                "product-name": "Meta Package Manager",
                "show-scons": True,
                "nofollow-import-to": ["*.tests", "*.distutils"],
            },
            [
                "--product-name=Meta Package Manager",
                "--show-scons",
                "--nofollow-import-to=*.tests",
                "--nofollow-import-to=*.distutils",
            ],
        ),
    ],
)
def test_pyproject_table_to_flags(table, expected):
    """A `[tool.X]` table translates to long-form CLI flags.

    `bool` true becomes a bare flag, `false` is skipped, scalars become
    `--key=value`, lists repeat the flag, and declaration order is preserved.
    """
    assert pyproject_table_to_flags(table) == expected


def test_resolve_config_flags_format():
    """A FLAGS-format spec turns `[tool.X]` into CLI args, not a file."""
    spec = ToolSpec(name="nuitka", native_format=NativeFormat.FLAGS)
    config_args, cleanup = resolve_config(
        spec,
        tool_config={"onefile": True, "include-package-data": ["foo", "bar"]},
    )
    assert config_args == [
        "--onefile",
        "--include-package-data=foo",
        "--include-package-data=bar",
    ]
    assert cleanup is None


def test_resolve_config_flags_format_empty():
    """No `[tool.X]` section yields no flags and no cleanup file."""
    spec = ToolSpec(name="nuitka", native_format=NativeFormat.FLAGS)
    assert resolve_config(spec, tool_config={}) == ([], None)


def test_native_format_flags_serialize_raises():
    """FLAGS is not a file format, so serialize() rejects it."""
    with pytest.raises(ValueError, match="not a file format"):
        NativeFormat.FLAGS.serialize({"onefile": True})
