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
import os
import platform
import re
import sys
import tarfile
import tempfile
import zipfile
from contextlib import contextmanager
from itertools import combinations
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import URLError

import pytest
import tomlrt
import yaml
from click_extra import Choice, ClickException
from click_extra.testing import CliRunner
from extra_platforms import (
    AARCH64,
    ALL_PLATFORMS,
    LINUX,
    MACOS,
    UBUNTU,
    UNKNOWN_PLATFORM,
    WINDOWS,
    X86_64,
    Architecture,
    Group,
    Platform,
)

from repomatic.cache import cached_binary_path
from repomatic.cli.main import repomatic
from repomatic.file_inventory import FileInventory
from repomatic.images import _check_tool
from repomatic.matrix_axes import (
    SINGLE_RUNNER_PYTHON_VERSIONS,
    TEST_RUNNERS_FULL,
    TEST_RUNNERS_PR,
)
from repomatic.metadata.core import Metadata
from repomatic.tooling.tool_registry import (
    _DIRECTIVE_YAML_OPTIONS_RE,
    _ESCAPED_COLON_FENCE_RE,
    CHECKSUMS,
    TOOL_REGISTRY,
    VERSIONS,
    ArchiveFormat,
    BinarySpec,
    NativeFormat,
    ToolBackend,
    ToolSpec,
    UnsupportedPlatformError,
    _fix_myst_directives,
    _reroot_section,
    _unescape_colon_fence,
    _yaml_block_to_field_list,
)
from repomatic.tooling.tool_runner import (
    TOOL_CRASH_EXIT_CODE,
    _build_install_args,
    _dereference_data_dir_symlinks,
    _download_and_verify,
    _extract_binary,
    _install_binary,
    _install_npm,
    _npm_supports_cooldown,
    _path_tools_env,
    ensure_binary,
    find_unmodified_configs,
    get_data_file_path,
    resolve_config,
    resolve_config_source,
    resolve_default_args,
    run_tool,
    verify_via_write_path,
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

    # pypi_name must be a bare, queryable PyPI project name: package may carry
    # an install extra (Nuitka's `nuitka[onefile]`), but the PyPI JSON API 404s
    # on a bracketed name, silently stalling sync-tool-versions.
    assert re.fullmatch(r"[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?", spec.pypi_name), (
        f"{name}: pypi_name {spec.pypi_name!r} is not a bare PyPI project name"
    )

    # config_flag uses the long-form double-dash convention.
    if spec.config_flag is not None:
        assert spec.config_flag.startswith("--"), (
            f"{name}: config_flag {spec.config_flag!r} must start with --"
        )

    # path_tools names another registry entry that ships a downloadable binary:
    # the runner installs each one through _install_binary to put it on PATH, so
    # a uvx- or npm-backed entry has no binary to expose and would fail at run
    # time rather than here.
    for companion in spec.path_tools:
        assert companion in TOOL_REGISTRY, (
            f"{name}: path_tools names unknown tool {companion!r}"
        )
        assert companion != name, f"{name}: path_tools must not list itself"
        assert TOOL_REGISTRY[companion].binary is not None, (
            f"{name}: path_tools entry {companion!r} has no binary spec, so the "
            "runner cannot place it on PATH"
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

    # npm is a distinct backend: mutually exclusive with binary and needs_venv.
    assert not (spec.npm is not None and spec.binary is not None), (
        f"{name}: npm and binary are mutually exclusive"
    )
    assert not (spec.npm is not None and spec.needs_venv), (
        f"{name}: npm and needs_venv are mutually exclusive"
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

    # Every with_packages entry must carry an `==` pin. An unpinned entry both
    # floats the environment between runs and is invisible to sync-tool-versions,
    # so it would silently age out the way mdformat's own `ruff==` pin once did.
    for entry in spec.with_packages:
        package, separator, version = entry.partition("==")
        assert separator and package and version, (
            f"{name}: with_packages entry {entry!r} must be pinned as `package==version`"
        )

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


def test_bundled_actionlint_labels_name_runners_the_matrix_emits():
    """Every declared label answers a runner the generated workflows can name.

    actionlint validates `runs-on:` against a list baked into its binary, and
    rejects a GitHub-hosted label its release predates. The bundled config
    declares those through `self-hosted-runner`, actionlint's escape hatch for
    an unrecognized label, which is why the entries carry no implication of
    being self-hosted.

    Pinning them to the matrix constants is what keeps the file from rotting: an
    entry surviving a move to a newer image would go on whitelisting a runner
    nothing uses, and read as still load-bearing.
    """
    with get_data_file_path("actionlint.yaml") as path:
        config = yaml.safe_load(path.read_text(encoding="UTF-8"))

    declared = set(config["self-hosted-runner"]["labels"])
    emitted = (
        set(TEST_RUNNERS_FULL)
        | set(TEST_RUNNERS_PR)
        | set(SINGLE_RUNNER_PYTHON_VERSIONS.values())
    )
    assert declared, "Bundled actionlint config declares no labels"
    assert declared <= emitted, (
        f"Declared but no longer emitted: {sorted(declared - emitted)}"
    )


def test_tool_backend_labels_unique():
    """Every backend resolves distinct display labels for the docs tables."""
    shorts = {backend.short_label for backend in ToolBackend}
    longs = {backend.long_label for backend in ToolBackend}
    assert len(shorts) == len(ToolBackend)
    assert len(longs) == len(ToolBackend)


@pytest.mark.parametrize(("name", "spec"), sorted(TOOL_REGISTRY.items()))
def test_tool_backend_matches_payload_fields(name, spec):
    """Each registry entry maps onto exactly one delivery backend."""
    backend = spec.backend
    assert (backend is ToolBackend.BINARY) == (spec.binary is not None)
    assert (backend is ToolBackend.NPM) == (spec.npm is not None)
    assert (backend is ToolBackend.VENV) == (
        spec.binary is None and spec.npm is None and spec.needs_venv
    )


@pytest.mark.parametrize(
    ("name", "spec"),
    [(n, s) for n, s in TOOL_REGISTRY.items() if s.binary is None],
)
def test_build_install_args_cooldown(name, spec, tmp_path, monkeypatch):
    """uvx tools carry `--exclude-newer`; needs_venv tools only when unlocked."""
    monkeypatch.chdir(tmp_path)
    cutoff = "2026-06-23"
    with_cutoff = _build_install_args(spec, exclude_newer=cutoff)

    if spec.needs_venv:
        # Without a uv.lock the project venv cannot freeze: the tool falls back
        # to an isolated `uv run --no-project`, cooldown-gated like uvx.
        assert "--no-project" in with_cutoff
        assert "--frozen" not in with_cutoff
        idx = with_cutoff.index("--exclude-newer")
        assert with_cutoff[idx + 1] == cutoff

        # `uv run --frozen` resolves from the lock, which already pins the
        # tree, so the cutoff is dropped.
        (tmp_path / "uv.lock").touch()
        locked = _build_install_args(spec, exclude_newer=cutoff)
        assert "--frozen" in locked
        assert "--no-project" not in locked
        assert "--exclude-newer" not in locked
    else:
        # uvx resolves fresh: the cutoff gates the transitive tree, before --from.
        idx = with_cutoff.index("--exclude-newer")
        assert idx < with_cutoff.index("--from")
        assert with_cutoff[idx + 1] == cutoff
        # No cutoff (0-days cooldown or unset) leaves the command unflagged.
        assert "--exclude-newer" not in _build_install_args(spec)


@pytest.mark.parametrize(
    ("name", "spec"),
    [(n, s) for n, s in TOOL_REGISTRY.items() if s.binary is None],
)
def test_uvx_tool_pins_registry_version(name, spec):
    """Every uvx-installed tool pins its `TOOL_REGISTRY` version in the command.

    Locks the invariant that keeps the per-tool command tests drift-proof: a
    version bump in `TOOL_REGISTRY` must flow into the `name==version` pin with
    no hand-edited literal. A hard-coded `name==X.Y.Z` in an individual test
    silently goes stale on the next bump (a missed `pyproject-fmt` literal once
    reddened every Tests cell), so version-pinning is asserted here once,
    generically, against the shared builder rather than restated per tool.
    """
    pin = f"{spec.package or spec.name}=={spec.version}"
    assert pin in _build_install_args(spec), f"{name}: missing registry pin {pin!r}"


def test_datasource_url_by_backend():
    """datasource_url points at npmjs for npm tools, GitHub/PyPI otherwise."""
    assert (
        TOOL_REGISTRY["awesome-lint"].datasource_url
        == "https://www.npmjs.com/package/awesome-lint"
    )
    assert TOOL_REGISTRY["ruff"].datasource_url == "https://github.com/astral-sh/ruff"
    # A tool with neither npm nor source_url falls back to its PyPI page.
    assert ToolSpec(name="widget").datasource_url == "https://pypi.org/project/widget/"


@patch("repomatic.tooling.tool_runner.subprocess.run")
@patch("repomatic.tooling.tool_runner.shutil.which", return_value="/usr/bin/npm")
def test_install_npm_command_shape(mock_which, mock_run, tmp_path):
    """The npm backend installs pkg@version into a prefix with the cooldown flag."""
    # Two subprocess calls when a cooldown is set: `npm --version`, then install.
    mock_run.side_effect = [
        MagicMock(returncode=0, stdout="11.10.0\n"),
        MagicMock(returncode=0),
    ]
    spec = TOOL_REGISTRY["awesome-lint"]
    # Pre-create the executable the (mocked) install would produce.
    bin_dir = tmp_path / "node_modules" / ".bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "awesome-lint").write_text("", encoding="UTF-8")

    bin_path = _install_npm(spec, tmp_path, cooldown_days=8)

    cmd = mock_run.call_args_list[-1][0][0]  # the install call, after `npm --version`
    assert cmd[0] == "/usr/bin/npm"
    assert cmd[1] == "install"
    assert f"awesome-lint@{spec.version}" in cmd
    assert "--prefix" in cmd and str(tmp_path) in cmd
    assert "--min-release-age=8" in cmd
    # Install-time scripts are blocked (the npm supply-chain worm vector); the
    # other flags mirror meta-package-manager's npm `pre_args`.
    assert "--ignore-scripts" in cmd
    assert "--no-audit" in cmd
    assert "--no-update-notifier" in cmd
    assert bin_path == bin_dir / "awesome-lint"


@patch("repomatic.tooling.tool_runner.subprocess.run")
@patch("repomatic.tooling.tool_runner.shutil.which", return_value="/usr/bin/npm")
def test_install_npm_zero_cooldown_omits_flag(mock_which, mock_run, tmp_path):
    """A 0-day cooldown drops the min-release-age flag entirely."""
    mock_run.return_value = MagicMock(returncode=0)
    bin_dir = tmp_path / "node_modules" / ".bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "awesome-lint").write_text("", encoding="UTF-8")

    _install_npm(TOOL_REGISTRY["awesome-lint"], tmp_path, cooldown_days=0)

    cmd = mock_run.call_args[0][0]
    assert not any(str(arg).startswith("--min-release-age") for arg in cmd)


@patch("repomatic.tooling.tool_runner.shutil.which", return_value=None)
def test_install_npm_without_npm_raises(mock_which, tmp_path):
    """A clear error surfaces when Node.js and npm are not on PATH."""
    with pytest.raises(RuntimeError, match="npm"):
        _install_npm(TOOL_REGISTRY["awesome-lint"], tmp_path, cooldown_days=0)


@pytest.mark.parametrize(
    ("version_output", "supported"),
    [
        ("10.8.2\n", False),
        ("11.9.0\n", False),
        ("11.10.0\n", True),
        ("11.12.1\n", True),
        # An unparsable version assumes support, so no spurious warning fires.
        ("garbage\n", True),
    ],
)
@patch("repomatic.tooling.tool_runner.subprocess.run")
def test_npm_supports_cooldown(mock_run, version_output, supported):
    """min-release-age support is gated on npm >= 11.10.0."""
    mock_run.return_value = MagicMock(returncode=0, stdout=version_output)
    assert _npm_supports_cooldown("/usr/bin/npm") is supported


@patch("repomatic.tooling.tool_runner.subprocess.run")
@patch("repomatic.tooling.tool_runner.shutil.which", return_value="/usr/bin/npm")
def test_install_npm_warns_when_npm_too_old(mock_which, mock_run, tmp_path, caplog):
    """Old npm cannot enforce the cooldown, so the runner warns but still installs."""
    mock_run.side_effect = [
        MagicMock(returncode=0, stdout="10.8.2\n"),  # npm --version
        MagicMock(returncode=0),  # npm install
    ]
    bin_dir = tmp_path / "node_modules" / ".bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "awesome-lint").write_text("", encoding="UTF-8")

    with caplog.at_level(logging.WARNING):
        _install_npm(TOOL_REGISTRY["awesome-lint"], tmp_path, cooldown_days=8)

    assert "minimum-release-age cooldown is not enforced" in caplog.text


def test_tool_registry_sorted_alphabetically():
    """Registry keys are sorted alphabetically."""
    keys = list(TOOL_REGISTRY.keys())
    assert keys == sorted(keys)


def test_pypi_name_strips_install_extra():
    """`pypi_name` yields the bare PyPI project name, dropping any install extra.

    Nuitka pins `package="nuitka[onefile]"` for installation, but the PyPI JSON
    API 404s on the bracketed name, so `pypi_name` must return `nuitka` for
    `sync-tool-versions` to find the real project.
    """
    assert TOOL_REGISTRY["nuitka"].pypi_name == "nuitka"
    # Extra stripped, and package=None falls back to the tool name.
    assert ToolSpec(name="papaya", package="papaya[juice]").pypi_name == "papaya"
    assert ToolSpec(name="papaya").pypi_name == "papaya"


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
        patch("repomatic.tooling.tool_registry.current_platform", return_value=MACOS),
        patch(
            "repomatic.tooling.tool_registry.current_architecture", return_value=AARCH64
        ),
    ):
        assert spec.resolve_platform() == (MACOS, AARCH64)


def test_resolve_platform_group_match():
    """Group membership matches when no exact Platform key exists."""
    spec = BinarySpec(
        urls={(LINUX, X86_64): "https://example.com/{version}/linux"},
        checksums={(LINUX, X86_64): "a" * 64},
        archive_format=ArchiveFormat.RAW,
    )
    # Simulate an Ubuntu system (member of LINUX group). Spelled out rather
    # than borrowed from `_on_linux_x86_64`, since the group-membership pass
    # is the behavior under test here, not a precondition of it.
    with (
        patch("repomatic.tooling.tool_registry.current_platform", return_value=UBUNTU),
        patch(
            "repomatic.tooling.tool_registry.current_architecture", return_value=X86_64
        ),
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
        patch("repomatic.tooling.tool_registry.current_platform", return_value=MACOS),
        patch(
            "repomatic.tooling.tool_registry.current_architecture", return_value=X86_64
        ),
        pytest.raises(RuntimeError, match="No binary"),
    ):
        spec.resolve_platform()


def test_resolve_platform_unknown_distro_falls_back_to_linux():
    """An unidentified distribution still reaches the family-wide `LINUX` key.

    extra-platforms returns `UNKNOWN_PLATFORM` for any distribution it has yet
    to learn, and that sentinel belongs to no group, so the group pass misses.
    The manylinux_2_28 build container (AlmaLinux) is the motivating case: it
    broke every `repomatic run` binary on a distro identity that never entered
    the choice of binary.
    """
    spec = BinarySpec(
        urls={
            (LINUX, X86_64): "https://example.com/{version}/linux",
            (MACOS, X86_64): "https://example.com/{version}/macos",
        },
        checksums={
            (LINUX, X86_64): "a" * 64,
            (MACOS, X86_64): "b" * 64,
        },
        archive_format=ArchiveFormat.RAW,
    )
    with (
        patch(
            "repomatic.tooling.tool_registry.current_platform",
            return_value=UNKNOWN_PLATFORM,
        ),
        patch(
            "repomatic.tooling.tool_registry.current_architecture", return_value=X86_64
        ),
        patch("repomatic.tooling.tool_registry.sys.platform", "linux"),
    ):
        assert spec.resolve_platform() == (LINUX, X86_64)


@pytest.mark.parametrize(
    ("urls", "sys_platform"),
    (
        # `sys.platform` proves the family, never the distribution, so a key
        # naming one distro must not absorb another's binary.
        ({(UBUNTU, X86_64): "https://example.com/deb"}, "linux"),
        # Only Linux gets a fallback: no other family can be unidentified.
        ({(LINUX, X86_64): "https://example.com/nix"}, "aix"),
        # The fallback never crosses architectures.
        ({(LINUX, AARCH64): "https://example.com/arm"}, "linux"),
    ),
)
def test_resolve_platform_unknown_distro_refuses_beyond_linux(
    urls: dict, sys_platform: str
):
    """The fallback widens to the Linux family and no further."""
    spec = BinarySpec(
        urls=urls,
        checksums=dict.fromkeys(urls, "a" * 64),
        archive_format=ArchiveFormat.RAW,
    )
    with (
        patch(
            "repomatic.tooling.tool_registry.current_platform",
            return_value=UNKNOWN_PLATFORM,
        ),
        patch(
            "repomatic.tooling.tool_registry.current_architecture", return_value=X86_64
        ),
        patch("repomatic.tooling.tool_registry.sys.platform", sys_platform),
        pytest.raises(UnsupportedPlatformError, match="No binary"),
    ):
        spec.resolve_platform()


def test_linux_binaries_are_keyed_on_the_family_group():
    """Every tool shipping a Linux binary keys it on the `LINUX` group.

    The pass-3 fallback only rescues a family-wide key, so a tool keyed on a
    specific distribution instead would still fail inside the manylinux build
    container.
    """
    offenders = {
        name
        for name, spec in TOOL_REGISTRY.items()
        if spec.binary
        for key in spec.binary.urls
        if isinstance(key[0], Platform) and key[0] in LINUX
    }
    assert not offenders, f"Linux binaries keyed on a distro, not LINUX: {offenders}"


def test_platform_cache_key():
    """Cache key is a filesystem-safe string derived from the PlatformKey."""
    assert BinarySpec.platform_cache_key((LINUX, AARCH64)) == "linux-aarch64"
    assert BinarySpec.platform_cache_key((MACOS, X86_64)) == "macos-x86_64"
    assert BinarySpec.platform_cache_key((WINDOWS, X86_64)) == "windows-x86_64"


def _urlopen_response(content: bytes, *, advertised_length: int | None = -1):
    """Build a `urlopen` double delivering *content* in one read.

    :param content: The body bytes, served before the terminating empty read.
    :param advertised_length: What `Content-Length` reports. The default
        derives it from *content*; `None` omits the header, which sends the
        download path down its indeterminate-progress branch; any other value
        is advertised verbatim, which is how a truncated transfer is staged.
    """
    response = MagicMock()
    length = len(content) if advertised_length == -1 else advertised_length
    response.headers.get = MagicMock(
        return_value=None if length is None else str(length)
    )
    response.read = MagicMock(side_effect=[content, b""])
    response.__enter__ = MagicMock(return_value=response)
    response.__exit__ = MagicMock(return_value=False)
    return response


def test_download_and_verify_success(tmp_path):
    """Successful download with matching checksum writes the file."""
    content = b"hello binary world"
    expected = hashlib.sha256(content).hexdigest()
    dest = tmp_path / "downloaded"

    with patch(
        "repomatic.tooling.tool_runner.urlopen", return_value=_urlopen_response(content)
    ):
        _download_and_verify("https://example.com/file", expected, dest)

    assert dest.exists()
    assert dest.read_bytes() == content


def test_download_and_verify_no_content_length(tmp_path):
    """A response without Content-Length downloads via the indeterminate Spinner."""
    content = b"hello binary world"
    expected = hashlib.sha256(content).hexdigest()
    dest = tmp_path / "downloaded"

    # No Content-Length: total stays 0, so the download path uses a Spinner
    # instead of a determinate progress bar.
    with patch(
        "repomatic.tooling.tool_runner.urlopen",
        return_value=_urlopen_response(content, advertised_length=None),
    ):
        _download_and_verify("https://example.com/file", expected, dest)

    assert dest.exists()
    assert dest.read_bytes() == content


def test_download_and_verify_mismatch(tmp_path):
    """Checksum mismatch raises ValueError and cleans up."""
    content = b"hello binary world"
    dest = tmp_path / "downloaded"

    with (
        patch(
            "repomatic.tooling.tool_runner.urlopen",
            return_value=_urlopen_response(content),
        ),
        pytest.raises(ValueError, match="SHA-256 mismatch"),
    ):
        _download_and_verify("https://example.com/file", "bad" * 16, dest)

    assert not dest.exists()


def test_download_and_verify_retries_transient_failure(tmp_path):
    """A transient network failure is retried and the next attempt succeeds."""
    content = b"hello binary world"
    expected = hashlib.sha256(content).hexdigest()
    dest = tmp_path / "downloaded"

    with (
        patch(
            "repomatic.tooling.tool_runner.urlopen",
            side_effect=[
                URLError("certificate verify failed: self-signed certificate"),
                _urlopen_response(content),
            ],
        ) as fake_urlopen,
        patch("repomatic.tooling.tool_runner.time.sleep") as fake_sleep,
    ):
        _download_and_verify("https://example.com/file", expected, dest)

    assert fake_urlopen.call_count == 2
    assert fake_sleep.call_count == 1
    assert dest.read_bytes() == content


def test_download_and_verify_gives_up_after_attempts(tmp_path):
    """A persistent network failure surfaces after the last attempt."""
    dest = tmp_path / "downloaded"

    with (
        patch(
            "repomatic.tooling.tool_runner.urlopen",
            side_effect=URLError("connection reset"),
        ) as fake_urlopen,
        patch("repomatic.tooling.tool_runner.time.sleep"),
        pytest.raises(URLError, match="connection reset"),
    ):
        _download_and_verify("https://example.com/file", "0" * 64, dest)

    assert fake_urlopen.call_count == 3
    assert not dest.exists()


def test_download_and_verify_truncated(tmp_path):
    """A body shorter than Content-Length raises OSError, not a mismatch.

    A truncated transfer (proxy hiccup, dropped connection) hashes to a wrong
    digest, so it used to be reported as a SHA-256 mismatch: that reads as a
    stale pin or a tampered artifact when the registry checksum is correct.
    Truncation is retried like any network failure, so the error only
    surfaces once every attempt came up short.
    """
    content = b"hello binary world"
    dest = tmp_path / "downloaded"

    with (
        patch(
            "repomatic.tooling.tool_runner.urlopen",
            # Advertise more bytes than the body delivers, on every attempt.
            side_effect=[
                _urlopen_response(content, advertised_length=len(content) + 7)
                for _ in range(3)
            ],
        ),
        patch("repomatic.tooling.tool_runner.time.sleep"),
        pytest.raises(OSError, match="Truncated download .* got 18 of 25 bytes"),
    ):
        _download_and_verify(
            "https://example.com/file",
            hashlib.sha256(content).hexdigest(),
            dest,
        )

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
    result = _extract_binary(archive, spec, tmp_path, "testtool", ArchiveFormat.RAW, 0)

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
    result = _extract_binary(
        archive, spec, tmp_path, "testtool", ArchiveFormat.TAR_GZ, 0
    )

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
    result = _extract_binary(
        archive, spec, tmp_path, "testtool", ArchiveFormat.TAR_GZ, 1
    )

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
    result = _extract_binary(
        archive_path, spec, tmp_path, "testtool", ArchiveFormat.TAR_XZ, 0
    )

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
        _extract_binary(archive, spec, tmp_path, "testtool", ArchiveFormat.TAR_GZ, 0)


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
        _extract_binary(archive, spec, tmp_path, "testtool", ArchiveFormat.TAR_GZ, 0)


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
    result = _extract_binary(
        archive, spec, tmp_path, "actionlint", ArchiveFormat.ZIP, 0
    )

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
    result = _extract_binary(archive, spec, tmp_path, "testtool", ArchiveFormat.ZIP, 1)

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
        _extract_binary(archive, spec, tmp_path, "nonexistent", ArchiveFormat.ZIP, 0)


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
        _extract_binary(archive, spec, tmp_path, "testtool", ArchiveFormat.ZIP, 0)


def test_extract_binary_format_override(tmp_path):
    """Per-platform archive format from dict is used when passed."""
    archive = _create_zip(tmp_path, "gitleaks.exe")

    spec = BinarySpec(
        urls={},
        checksums={},
        archive_format={
            ALL_PLATFORMS: ArchiveFormat.TAR_GZ,
            WINDOWS: ArchiveFormat.ZIP,
        },
    )
    # _install_binary resolves the format and passes it explicitly.
    result = _extract_binary(archive, spec, tmp_path, "gitleaks", ArchiveFormat.ZIP, 0)

    assert result == tmp_path / "gitleaks.exe"
    assert result.exists()


def test_get_archive_format_dict_resolution():
    """Dict archive_format resolves Platform > Group membership."""
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
        patch("repomatic.tooling.tool_registry.current_platform", return_value=MACOS),
        patch(
            "repomatic.tooling.tool_registry.current_architecture", return_value=X86_64
        ),
        pytest.raises(RuntimeError, match="No binary for"),
    ):
        _install_binary(spec, Path("/tmp"))


def test_path_tools_env_skips_platform_without_binary(caplog):
    """A companion publishing no binary here is skipped, not fatal.

    `shfmt` ships nothing for Windows ARM64, so provisioning it for mdformat
    can never succeed there. Aborting would make `repomatic run mdformat`
    unusable on the platform rather than merely leaving its shell blocks
    unformatted, so the run carries on with `PATH` untouched.
    """
    spec = TOOL_REGISTRY["mdformat"]
    assert "shfmt" in spec.path_tools
    path_dirs: list[tempfile.TemporaryDirectory[str]] = []
    with (
        patch("repomatic.tooling.tool_registry.current_platform", return_value=WINDOWS),
        patch(
            "repomatic.tooling.tool_registry.current_architecture", return_value=AARCH64
        ),
        caplog.at_level(logging.WARNING),
    ):
        env = _path_tools_env(spec, False, False, path_dirs)

    assert env is not None
    assert "shfmt" in caplog.text
    assert "No binary for" in caplog.text
    # Nothing was prepended, so the child inherits the ambient PATH verbatim.
    assert env["PATH"] == os.environ.get("PATH", "")


def test_dereference_data_dir_symlinks_stages_symlinked_source(tmp_path):
    """A symlink under an `--include-data-dir` source is resolved to real content.

    Reproduces the shape that crashes Nuitka's macOS codesign step: a
    directory holding a symlink whose target lies outside anything Nuitka
    copies. Staging must replace the link with the target's actual bytes,
    not merely a same-looking file.
    """
    target = tmp_path / "canonical.md"
    target.write_text("real content", encoding="UTF-8")

    src = tmp_path / "data" / "skills"
    src.mkdir(parents=True)
    (src / "SKILL.md").symlink_to(target)

    path_dirs: list[tempfile.TemporaryDirectory[str]] = []
    fixed = _dereference_data_dir_symlinks(
        [f"--include-data-dir={src}=repomatic/data/skills"], path_dirs
    )

    assert len(fixed) == 1
    assert len(path_dirs) == 1
    staged_src = Path(fixed[0].removeprefix("--include-data-dir=").rsplit("=", 1)[0])
    staged_file = staged_src / "SKILL.md"
    assert staged_file.read_text(encoding="UTF-8") == "real content"
    assert not staged_file.is_symlink()

    for path_dir in path_dirs:
        path_dir.cleanup()


def test_dereference_data_dir_symlinks_passes_through_plain_dirs(tmp_path):
    """A source with no symlinks, or a non-`--include-data-dir` flag, is untouched.

    Staging is wasted work (and a needless temp directory) when nothing
    would break Nuitka's copy, so only symlink-containing sources trigger it.
    """
    src = tmp_path / "data" / "awesome_template"
    src.mkdir(parents=True)
    (src / "license").write_text("license text", encoding="UTF-8")

    path_dirs: list[tempfile.TemporaryDirectory[str]] = []
    original = [
        f"--include-data-dir={src}=repomatic/data/awesome_template",
        "--include-package-data=click_extra",
    ]
    fixed = _dereference_data_dir_symlinks(original, path_dirs)

    assert fixed == original
    assert path_dirs == []


TARBALL_URL = "https://example.com/{version}/tool.tar.gz"
"""Download URL for the tests that go through the extract step."""


def _binary_spec(
    *,
    version: str = "1.0.0",
    checksum: str = "a" * 64,
    url: str = "https://example.com/{version}/tool",
    archive_executable: str | None = None,
    archive_format: ArchiveFormat = ArchiveFormat.RAW,
) -> ToolSpec:
    """A single-platform `ToolSpec` for the Linux x86_64 cache tests.

    Every caller pairs it with `_on_linux_x86_64`, so one URL key is enough:
    resolution never reaches a second.
    """
    return ToolSpec(
        name="testtool",
        version=version,
        binary=BinarySpec(
            urls={(LINUX, X86_64): url},
            checksums={(LINUX, X86_64): checksum},
            archive_format=archive_format,
            archive_executable=archive_executable,
        ),
    )


@contextmanager
def _on_linux_x86_64():
    """Pin platform detection to Linux x86_64 for the duration of the block.

    `UBUNTU` rather than `LINUX` on purpose: reporting a real distribution is
    what sends resolution through the group-membership pass, the way an actual
    runner does.
    """
    with (
        patch("repomatic.tooling.tool_registry.current_platform", return_value=UBUNTU),
        patch(
            "repomatic.tooling.tool_registry.current_architecture", return_value=X86_64
        ),
    ):
        yield


def test_install_binary_cache_hit(tmp_path, monkeypatch, cache_env):
    """_install_binary returns cached path when cache hit and sidecar matches."""
    # Pre-populate the cache with a fake binary and its .sha256 sidecar.
    fake_binary = b"cached-binary-content"
    binary_checksum = hashlib.sha256(fake_binary).hexdigest()

    spec = _binary_spec(checksum="archive-checksum-not-used-for-cache")

    cache_path = cached_binary_path("testtool", "1.0.0", "linux-x86_64", "testtool")
    cache_path.parent.mkdir(parents=True)
    cache_path.write_bytes(fake_binary)
    cache_path.chmod(0o755)
    # Write the sidecar with the binary's digest.
    sidecar = cache_path.with_suffix(cache_path.suffix + ".sha256")
    sidecar.write_text(binary_checksum, encoding="UTF-8")

    with _on_linux_x86_64():
        result = _install_binary(spec, tmp_path / "staging")

    assert result == cache_path


@pytest.mark.parametrize(
    "cached_name",
    (
        # What `store_binary` records on POSIX: the extracted file's own name,
        # with the archive's directory prefix already gone.
        "testtool",
        # What it records on Windows, where the archive carries the suffix.
        "testtool.exe",
    ),
)
def test_install_binary_cache_hit_on_a_nested_archive_executable(
    tmp_path, monkeypatch, cache_env, cached_name
):
    """A cached entry is found whatever path the executable held in the archive.

    `archive_executable` names a location *inside* the archive, while the cache
    keys an entry on the extracted file's own name. Probing with the nested path
    matched nothing, so a tool declaring one re-downloaded on every call despite
    a complete, sidecar-verified copy sitting in the cache. `gh` (`bin/gh`) was
    the only registry tool to declare one, and since every `gh` invocation
    routes through the pinned binary, it re-fetched 13 MB per run.
    """
    fake_binary = b"cached-binary-content"
    spec = _binary_spec(
        archive_executable="bin/testtool", archive_format=ArchiveFormat.ZIP
    )

    cache_path = cached_binary_path("testtool", "1.0.0", "linux-x86_64", cached_name)
    cache_path.parent.mkdir(parents=True)
    cache_path.write_bytes(fake_binary)
    cache_path.chmod(0o755)
    sidecar = cache_path.with_suffix(cache_path.suffix + ".sha256")
    sidecar.write_text(hashlib.sha256(fake_binary).hexdigest(), encoding="UTF-8")

    with _on_linux_x86_64():
        result = _install_binary(spec, tmp_path / "staging")

    assert result == cache_path


def test_install_binary_cache_miss_stores(tmp_path, monkeypatch, cache_env):
    """_install_binary stores the binary in cache after download on miss."""
    fake_binary = b"downloaded-binary"
    checksum = hashlib.sha256(fake_binary).hexdigest()

    spec = _binary_spec(version="2.0.0", checksum=checksum, url=TARBALL_URL)

    staging = tmp_path / "staging"
    staging.mkdir()

    with (
        _on_linux_x86_64(),
        patch("repomatic.tooling.tool_runner._download_and_verify"),
        patch("repomatic.tooling.tool_runner._extract_binary") as mock_extract,
    ):
        extracted = staging / "testtool"
        extracted.write_bytes(fake_binary)
        extracted.chmod(0o755)
        mock_extract.return_value = extracted

        result = _install_binary(spec, staging)

    expected_cache = cached_binary_path("testtool", "2.0.0", "linux-x86_64", "testtool")
    assert result == expected_cache
    assert expected_cache.read_bytes() == fake_binary
    # Sidecar must be written after cache store.
    sidecar = expected_cache.with_suffix(expected_cache.suffix + ".sha256")
    assert sidecar.is_file()
    assert (
        sidecar.read_text(encoding="UTF-8") == hashlib.sha256(fake_binary).hexdigest()
    )


def test_install_binary_no_cache_flag(tmp_path, monkeypatch, cache_env):
    """_install_binary with no_cache=True bypasses cache entirely."""
    spec = _binary_spec(url=TARBALL_URL)

    staging = tmp_path / "staging"
    staging.mkdir()

    with (
        _on_linux_x86_64(),
        patch("repomatic.tooling.tool_runner._download_and_verify"),
        patch("repomatic.tooling.tool_runner._extract_binary") as mock_extract,
        patch("repomatic.tooling.tool_runner.store_binary") as mock_store,
    ):
        extracted = staging / "testtool"
        extracted.write_bytes(b"binary")
        extracted.chmod(0o755)
        mock_extract.return_value = extracted

        result = _install_binary(spec, staging, no_cache=True)

    # Should NOT store in cache.
    mock_store.assert_not_called()
    assert result == extracted


def test_install_binary_cache_integrity_failure(tmp_path, monkeypatch, cache_env):
    """_install_binary re-downloads when cached binary fails sidecar check."""
    # Put a tampered binary in the cache with a sidecar for the original.
    cache_path = cached_binary_path("testtool", "1.0.0", "linux-x86_64", "testtool")
    cache_path.parent.mkdir(parents=True)
    cache_path.write_bytes(b"tampered-content")
    cache_path.chmod(0o755)
    # Sidecar records the digest of the original binary, not the tampered one.
    sidecar = cache_path.with_suffix(cache_path.suffix + ".sha256")
    sidecar.write_text(
        hashlib.sha256(b"original-content").hexdigest(), encoding="UTF-8"
    )

    spec = _binary_spec(checksum="archive-checksum", url=TARBALL_URL)

    staging = tmp_path / "staging"
    staging.mkdir()

    with (
        _on_linux_x86_64(),
        patch("repomatic.tooling.tool_runner._download_and_verify"),
        patch("repomatic.tooling.tool_runner._extract_binary") as mock_extract,
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


def test_install_binary_cache_store_fallback(tmp_path, monkeypatch, cache_env):
    """_install_binary falls back to temp path when cached file is missing."""
    fake_binary = b"downloaded-binary"
    checksum = hashlib.sha256(fake_binary).hexdigest()

    spec = _binary_spec(version="2.0.0", checksum=checksum, url=TARBALL_URL)

    staging = tmp_path / "staging"
    staging.mkdir()

    def fake_store(*args, **kwargs):
        """Return a cache path that doesn't exist on disk."""
        return cache_env / "bin" / "ghost" / "binary"

    with (
        _on_linux_x86_64(),
        patch("repomatic.tooling.tool_runner._download_and_verify"),
        patch("repomatic.tooling.tool_runner._extract_binary") as mock_extract,
        patch("repomatic.tooling.tool_runner.store_binary", side_effect=fake_store),
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


@patch("repomatic.tooling.tool_runner.subprocess.run")
@patch("repomatic.tooling.tool_runner._install_binary")
@patch("repomatic.tooling.tool_runner.is_github_ci", return_value=False)
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


@patch("repomatic.tooling.tool_runner.subprocess.run")
@patch("repomatic.tooling.tool_runner._install_binary")
@patch("repomatic.tooling.tool_runner.is_github_ci", return_value=False)
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


@patch("repomatic.tooling.tool_runner.subprocess.run")
@patch("repomatic.tooling.tool_runner._install_binary")
@patch("repomatic.tooling.tool_runner.is_github_ci", return_value=False)
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


@patch("repomatic.tooling.tool_runner.subprocess.run")
@patch("repomatic.tooling.tool_runner.is_github_ci", return_value=False)
def test_run_tool_ruff_bundled_default(mock_ci, mock_run, tmp_path, monkeypatch):
    """ruff uses bundled default config when no config exists."""
    monkeypatch.chdir(tmp_path)
    mock_run.return_value = MagicMock(returncode=0)

    run_tool("ruff", extra_args=("check", "--output-format", "github"))

    cmd = mock_run.call_args[0][0]
    assert cmd[0] == "uvx"
    assert f"ruff=={TOOL_REGISTRY['ruff'].version}" in " ".join(cmd)
    assert "--config" in cmd
    assert "check" in cmd
    assert "--output-format" in cmd
    assert "github" in cmd


@patch("repomatic.tooling.tool_runner.subprocess.run")
@patch("repomatic.tooling.tool_runner.is_github_ci", return_value=False)
def test_run_tool_ruff_reads_pyproject_natively(
    mock_ci, mock_run, tmp_path, monkeypatch
):
    """ruff gets no --config flag when [tool.ruff] exists in pyproject.toml."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "test"\n\n[tool.ruff]\npreview = true\n', encoding="UTF-8"
    )
    mock_run.return_value = MagicMock(returncode=0)

    run_tool("ruff", extra_args=("check",))

    cmd = mock_run.call_args[0][0]
    assert cmd[0] == "uvx"
    assert "--config" not in cmd
    assert "check" in cmd


@patch("repomatic.tooling.tool_runner.subprocess.run")
@patch("repomatic.tooling.tool_runner.is_github_ci", return_value=False)
def test_run_tool_bump_my_version_via_uvx(mock_ci, mock_run, tmp_path, monkeypatch):
    """bump-my-version runs via uvx with subcommand extra_args."""
    monkeypatch.chdir(tmp_path)
    mock_run.return_value = MagicMock(returncode=0)

    run_tool("bump-my-version", extra_args=("bump", "--verbose", "patch"))

    cmd = mock_run.call_args[0][0]
    assert cmd[0] == "uvx"
    assert f"bump-my-version=={TOOL_REGISTRY['bump-my-version'].version}" in " ".join(
        cmd
    )
    assert "bump" in cmd
    assert "--verbose" in cmd
    assert "patch" in cmd


# ---------------------------------------------------------------------------
# ensure_binary memoization and staging lifetime
# ---------------------------------------------------------------------------


@patch("repomatic.tooling.tool_runner._install_binary")
def test_ensure_binary_memoizes_per_tool(mock_install, tmp_path):
    """Repeated calls install once and return the same path.

    Callers in a loop (`format-images` optimizing one PNG per call) must not
    pay the install-and-verify path once per file.
    """
    cached = tmp_path / "labelmaker"
    cached.touch()
    mock_install.return_value = cached

    assert ensure_binary("labelmaker") == cached
    assert ensure_binary("labelmaker") == cached
    assert mock_install.call_count == 1


@patch("repomatic.tooling.tool_runner._install_binary")
def test_ensure_binary_cleans_staging_on_cache_hit(mock_install, tmp_path):
    """When the install lands in the cache, the staging directory is removed."""
    cached = tmp_path / "labelmaker"
    cached.touch()
    mock_install.return_value = cached

    ensure_binary("labelmaker")

    staging = mock_install.call_args[0][1]
    assert not staging.exists()


@patch("repomatic.tooling.tool_runner._install_binary")
def test_ensure_binary_keeps_staging_fallback_alive(mock_install):
    """A staging-path fallback survives the call instead of dangling.

    When the cache store fails (unwritable root, Docker overlay losing the
    write), `_install_binary` returns the staging copy: the staging directory
    must then outlive the call, or the caller would exec a just-deleted file.
    """

    def install(spec, staging_dir, **kwargs):
        path = staging_dir / "labelmaker"
        path.touch()
        return path

    mock_install.side_effect = install

    binary = ensure_binary("labelmaker")

    assert binary.is_file()


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


def test_resolve_config_reads_pyproject_falls_through_to_bundled(
    tmp_path, monkeypatch, cache_env
):
    """Tools with reads_pyproject=True use bundled default when no config exists."""
    monkeypatch.chdir(tmp_path)
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
    (tmp_path / ".yamllint.yaml").write_text("rules: {}", encoding="UTF-8")

    spec = TOOL_REGISTRY["yamllint"]
    args, tmp = resolve_config(
        spec, tool_config={"rules": {"line-length": {"max": 80}}}
    )
    assert args == []
    assert tmp is None


def test_resolve_config_pyproject_section(tmp_path, monkeypatch, cache_env):
    """[tool.X] in pyproject.toml produces a cached config file."""
    monkeypatch.chdir(tmp_path)

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


def test_resolve_config_toml_translation(tmp_path, monkeypatch, cache_env):
    """[tool.X] with native_format='toml' produces a valid TOML cached file."""
    monkeypatch.chdir(tmp_path)

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


def test_resolve_config_toml_nested_tables(tmp_path, monkeypatch, cache_env):
    """Nested dicts in [tool.X] produce TOML table sections."""
    monkeypatch.chdir(tmp_path)

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


def test_resolve_config_json_translation(tmp_path, monkeypatch, cache_env):
    """[tool.X] with native_format='json' produces a valid JSON cached file."""
    monkeypatch.chdir(tmp_path)

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


def test_resolve_config_bundled_default(tmp_path, monkeypatch, cache_env):
    """Bundled default is cached and passed via --config flag."""
    monkeypatch.chdir(tmp_path)

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


def test_resolve_config_empty_tool_config_is_not_match(
    tmp_path, monkeypatch, cache_env
):
    """An empty [tool.X] dict does not count as a config match."""
    monkeypatch.chdir(tmp_path)

    spec = TOOL_REGISTRY["zizmor"]
    args, tmp = resolve_config(spec, tool_config={})
    assert len(args) == 2
    assert args[0] == "--config"
    assert Path(args[1]).exists()
    assert tmp is None


# ---------------------------------------------------------------------------
# resolve_config_source
# ---------------------------------------------------------------------------


BARE_TOOL = ToolSpec(name="sometool", version="1.0.0", package="sometool")
"""A tool with no bundled default, no native file and no pyproject section."""


@pytest.mark.parametrize(
    ("tool", "files", "expected"),
    [
        # A shadowing config is labelled: it replaces the bundled default in
        # full rather than layering over it, and that loss is otherwise silent.
        pytest.param(
            "zizmor",
            {"zizmor.yaml": "rules: {}"},
            "zizmor.yaml (replaces bundled default)",
            id="native-file",
        ),
        pytest.param("yamllint", {}, "bundled default", id="bundled-default"),
        pytest.param(
            "yamllint",
            {"pyproject.toml": "[tool.yamllint]\nrules = {line-length = {max = 80}}\n"},
            "[tool.yamllint] in pyproject.toml (replaces bundled default)",
            id="pyproject-section",
        ),
        # A `reads_pyproject` tool resolves the same two ways, but reaches its
        # section natively rather than through a translated temp file.
        pytest.param("ruff", {}, "bundled default", id="reads-pyproject-fallback"),
        pytest.param(
            "ruff",
            {
                "pyproject.toml": '[project]\nname = "test"\n\n[tool.ruff]\npreview = true\n'
            },
            "[tool.ruff] in pyproject.toml (replaces bundled default)",
            id="reads-pyproject-native",
        ),
        pytest.param(BARE_TOOL, {}, "(bare)", id="bare"),
    ],
)
def test_resolve_config_source(tmp_path, monkeypatch, tool, files, expected):
    """The reported config source names where the tool's settings came from."""
    monkeypatch.chdir(tmp_path)
    for name, content in files.items():
        (tmp_path / name).write_text(content, encoding="UTF-8")

    spec = tool if isinstance(tool, ToolSpec) else TOOL_REGISTRY[tool]
    assert resolve_config_source(spec) == expected


# ---------------------------------------------------------------------------
# run_tool
# ---------------------------------------------------------------------------


def test_run_tool_unknown_tool():
    """Raise ValueError for unregistered tool names."""
    with pytest.raises(ValueError, match="Unknown tool"):
        run_tool("nonexistent-tool")


@patch("repomatic.tooling.tool_runner.subprocess.run")
@patch("repomatic.tooling.tool_runner.is_github_ci", return_value=False)
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
    assert f"yamllint=={TOOL_REGISTRY['yamllint'].version}" in " ".join(cmd)
    assert "--config-file" in cmd
    # default_flags are always present.
    assert "--strict" in cmd
    assert "." in cmd
    # Should not have CI flags.
    assert "--format" not in cmd


@patch("repomatic.tooling.tool_runner.subprocess.run")
@patch("repomatic.tooling.tool_runner.is_github_ci", return_value=True)
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


@patch("repomatic.tooling.tool_runner.subprocess.run")
@patch("repomatic.tooling.tool_runner.is_github_ci", return_value=False)
def test_run_tool_native_config_no_extra_flags(
    mock_ci, mock_run, tmp_path, monkeypatch
):
    """Tool with native config file gets no config flags from repomatic."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "zizmor.yaml").write_text("rules: {}", encoding="UTF-8")
    mock_run.return_value = MagicMock(returncode=0)

    run_tool("zizmor", extra_args=(".",))

    cmd = mock_run.call_args[0][0]
    assert "--config" not in cmd
    # default_flags are always present even with native config.
    assert "--offline" in cmd


@patch("repomatic.tooling.tool_runner.subprocess.run")
@patch("repomatic.tooling.tool_runner.is_github_ci", return_value=False)
def test_run_tool_pyproject_section_cached_config(
    mock_ci,
    mock_run,
    tmp_path,
    monkeypatch,
    cache_env,
):
    """[tool.X] translation writes config to cache and passes via --config."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        "[tool.zizmor]\n[tool.zizmor.rules.artipacked]\ndisable = true\n",
        encoding="UTF-8",
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


@patch("repomatic.tooling.tool_runner.subprocess.run")
@patch("repomatic.tooling.tool_runner.is_github_ci", return_value=False)
def test_run_tool_forwards_exit_code(mock_ci, mock_run, tmp_path, monkeypatch):
    """Tool's exit code is forwarded unchanged."""
    monkeypatch.chdir(tmp_path)
    mock_run.return_value = MagicMock(returncode=42)

    exit_code = run_tool("yamllint", extra_args=(".",))
    assert exit_code == 42


@patch("repomatic.tooling.tool_runner.subprocess.run")
@patch("repomatic.tooling.tool_runner.is_github_ci", return_value=False)
def test_run_tool_autopep8_default_flags(mock_ci, mock_run, tmp_path, monkeypatch):
    """autopep8 runs with all default flags via uvx."""
    monkeypatch.chdir(tmp_path)
    mock_run.return_value = MagicMock(returncode=0)

    run_tool("autopep8", extra_args=("file.py",))

    cmd = mock_run.call_args[0][0]
    assert cmd[0] == "uvx"
    assert f"autopep8=={TOOL_REGISTRY['autopep8'].version}" in " ".join(cmd)
    assert "--recursive" in cmd
    assert "--in-place" in cmd
    assert "--max-line-length" in cmd
    assert "88" in cmd
    assert "--select" in cmd
    assert "E501" in cmd
    assert "file.py" in cmd


@patch("repomatic.tooling.tool_runner.subprocess.run")
@patch("repomatic.tooling.tool_runner.is_github_ci", return_value=False)
def test_run_tool_pyproject_fmt(mock_ci, mock_run, tmp_path, monkeypatch):
    """pyproject-fmt runs bare via uvx, with no forced --expand-tables flag."""
    monkeypatch.chdir(tmp_path)
    mock_run.return_value = MagicMock(returncode=0)

    run_tool("pyproject-fmt", extra_args=("pyproject.toml",))

    cmd = mock_run.call_args[0][0]
    assert cmd[0] == "uvx"
    assert f"pyproject-fmt=={TOOL_REGISTRY['pyproject-fmt'].version}" in " ".join(cmd)
    assert "pyproject.toml" in cmd
    # No forced formatting preference: pyproject-fmt uses its own defaults.
    assert "--expand-tables" not in cmd
    assert "--config" not in cmd


@patch("repomatic.tooling.tool_runner.subprocess.run")
@patch("repomatic.tooling.tool_runner._install_binary")
@patch("repomatic.tooling.tool_runner.is_github_ci", return_value=False)
def test_run_tool_mdformat_with_packages(
    mock_ci,
    mock_install,
    mock_run,
    tmp_path,
    monkeypatch,
):
    """mdformat runs via uvx with all plugin packages."""
    monkeypatch.chdir(tmp_path)
    bin_path = tmp_path / "shfmt"
    bin_path.touch()
    mock_install.return_value = bin_path
    mock_run.return_value = MagicMock(returncode=0)

    run_tool("mdformat", extra_args=("readme.md",))

    cmd = mock_run.call_args[0][0]
    assert cmd[0] == "uvx"
    assert f"mdformat=={TOOL_REGISTRY['mdformat'].version}" in " ".join(cmd)
    assert "--number" not in cmd
    assert "--strict-front-matter" in cmd
    assert "readme.md" in cmd
    # Verify plugins are passed as --with flags.
    with_count = cmd.count("--with")
    spec = TOOL_REGISTRY["mdformat"]
    assert with_count == len(spec.with_packages)


@patch("repomatic.tooling.tool_runner.subprocess.run")
@patch("repomatic.tooling.tool_runner.Metadata")
@patch("repomatic.tooling.tool_runner.is_github_ci", return_value=False)
def test_run_tool_mypy_with_computed_params(
    mock_ci,
    mock_metadata_cls,
    mock_run,
    tmp_path,
    monkeypatch,
):
    """mypy runs via uv run with computed --python-version param."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "uv.lock").touch()
    mock_metadata_cls.return_value.mypy_params = ["--python-version", "3.10"]
    mock_run.return_value = MagicMock(returncode=0)

    run_tool("mypy", extra_args=("repomatic/",))

    cmd = mock_run.call_args[0][0]
    assert cmd[0] == "uv"
    assert "--no-progress" in cmd
    assert "run" in cmd
    assert "--frozen" in cmd
    assert f"mypy=={TOOL_REGISTRY['mypy'].version}" in " ".join(cmd)
    assert "--color-output" in cmd
    assert "--python-version" in cmd
    assert "3.10" in cmd
    assert "repomatic/" in cmd


@patch("repomatic.tooling.tool_runner.subprocess.run")
@patch("repomatic.tooling.tool_runner.Metadata")
@patch("repomatic.tooling.tool_runner.is_github_ci", return_value=False)
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


@patch("repomatic.tooling.tool_runner.subprocess.run")
@patch("repomatic.tooling.tool_runner.Metadata")
@patch("repomatic.tooling.tool_runner.is_github_ci", return_value=False)
def test_run_tool_mypy_no_lockfile_falls_back_isolated(
    mock_ci,
    mock_metadata_cls,
    mock_run,
    tmp_path,
    monkeypatch,
):
    """Without a uv.lock, mypy runs isolated instead of demanding `--frozen`.

    A repository holding a `pyproject.toml` with only `[tool.*]` tables (a
    dotfiles repo linting standalone scripts) has no lockfile, so
    `uv run --frozen` aborts with "Unable to find lockfile at `uv.lock`"
    before mypy ever starts.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text("[tool.mypy]\n", encoding="UTF-8")
    mock_metadata_cls.return_value.mypy_params = None
    mock_run.return_value = MagicMock(returncode=0)

    run_tool("mypy", extra_args=("script.py",))

    cmd = mock_run.call_args[0][0]
    assert cmd[0] == "uv"
    assert "run" in cmd
    assert "--no-project" in cmd
    assert "--frozen" not in cmd
    # The unlocked resolution still honors the minimum-release-age cooldown.
    assert "--exclude-newer" in cmd
    assert "script.py" in cmd


@patch("repomatic.tooling.tool_runner.subprocess.run")
@patch("repomatic.tooling.tool_runner.is_github_ci", return_value=False)
def test_run_tool_nuitka_uses_module_invocation(
    mock_ci, mock_run, tmp_path, monkeypatch
):
    """nuitka runs via `python -m nuitka` to avoid Windows script resolution issues."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "uv.lock").touch()
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


@patch("repomatic.tooling.tool_runner.subprocess.run")
@patch("repomatic.tooling.tool_runner.is_github_ci", return_value=False)
def test_run_tool_nuitka_nofollow_imports_default(
    mock_ci, mock_run, tmp_path, monkeypatch
):
    """nuitka excludes tkinter by default via nuitka.nofollow-imports."""
    monkeypatch.chdir(tmp_path)
    mock_run.return_value = MagicMock(returncode=0)

    run_tool("nuitka", extra_args=("repomatic",))

    cmd = mock_run.call_args[0][0]
    assert "--nofollow-import-to=tkinter" in cmd


@patch("repomatic.tooling.tool_runner.subprocess.run")
@patch("repomatic.tooling.tool_runner.is_github_ci", return_value=False)
def test_run_tool_nuitka_nofollow_imports_override(
    mock_ci, mock_run, tmp_path, monkeypatch
):
    """An explicit nuitka.nofollow-imports replaces the tkinter default."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        '[tool.repomatic]\nnuitka.nofollow-imports = ["test.*"]\n',
        encoding="UTF-8",
    )
    mock_run.return_value = MagicMock(returncode=0)

    run_tool("nuitka", extra_args=("repomatic",))

    cmd = mock_run.call_args[0][0]
    assert "--nofollow-import-to=test.*" in cmd
    assert "--nofollow-import-to=tkinter" not in cmd


# ---------------------------------------------------------------------------
# run_tool --output directory creation
# ---------------------------------------------------------------------------


@patch("repomatic.tooling.tool_runner.subprocess.run")
@patch("repomatic.tooling.tool_runner._install_binary")
@patch("repomatic.tooling.tool_runner.is_github_ci", return_value=False)
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


@patch("repomatic.tooling.tool_runner.subprocess.run")
@patch("repomatic.tooling.tool_runner._install_binary")
@patch("repomatic.tooling.tool_runner.is_github_ci", return_value=False)
def test_run_tool_creates_output_parent_equals_form(
    mock_ci,
    mock_install,
    mock_run,
    tmp_path,
    monkeypatch,
):
    """The `--output=path` spelling gets its parent directory created too."""
    monkeypatch.chdir(tmp_path)
    bin_path = tmp_path / "lychee"
    bin_path.touch()
    mock_install.return_value = bin_path
    mock_run.return_value = MagicMock(returncode=0)

    output_path = tmp_path / "scratch" / "report.md"
    run_tool("lychee", extra_args=(f"--output={output_path}", "readme.md"))

    assert output_path.parent.is_dir()


@patch("repomatic.tooling.tool_runner.subprocess.run")
@patch("repomatic.tooling.tool_runner._install_binary")
@patch("repomatic.tooling.tool_runner.is_github_ci", return_value=False)
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


@patch("repomatic.tooling.tool_runner.subprocess.run")
@patch("repomatic.tooling.tool_runner.is_github_ci", return_value=False)
def test_run_tool_no_output_flag_skips_mkdir(mock_ci, mock_run, tmp_path, monkeypatch):
    """run_tool without --output does not create any directories."""
    monkeypatch.chdir(tmp_path)
    mock_run.return_value = MagicMock(returncode=0)
    # Snapshot rather than assert emptiness: autouse fixtures (the isolated
    # app dir) may already have planted entries under this tmp tree, and the
    # invariant is only that `run_tool` itself adds nothing.
    before = set(tmp_path.iterdir())

    run_tool("yamllint", extra_args=(".",))

    assert set(tmp_path.iterdir()) == before


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


def test_find_unmodified_configs_honors_root(tmp_path, monkeypatch):
    """Detection reads under `root`, never the current directory.

    `run_init` joins the returned paths against its `output_dir`, so a scan
    anchored elsewhere would report on one tree and let `--delete-unmodified`
    delete from another.
    """
    with get_data_file_path("yamllint.yaml") as bundled:
        bundled_content = bundled.read_text(encoding="UTF-8")

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / ".yamllint.yaml").write_text(bundled_content, encoding="UTF-8")
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)

    assert find_unmodified_configs() == []
    assert ".yamllint.yaml" in [p for _, p in find_unmodified_configs(elsewhere)]


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
# MyST directive post-processing
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


def test_fix_myst_directives_in_place(tmp_path):
    """Post-processor rewrites files in-place and skips unchanged files."""
    affected = tmp_path / "affected.md"
    affected.write_text(
        "# Title\n\n```{py:module} mymod\n---\nno-typesetting:\n---\n```\n",
        encoding="UTF-8",
    )

    untouched = tmp_path / "untouched.md"
    original = "# Plain markdown\n\nNo directives here.\n"
    untouched.write_text(original, encoding="UTF-8")

    _fix_myst_directives([str(affected), str(untouched), "/nonexistent/path"])

    assert affected.read_text(encoding="UTF-8") == (
        "# Title\n\n```{py:module} mymod\n:no-typesetting:\n```\n"
    )
    assert untouched.read_text(encoding="UTF-8") == original


def test_fix_myst_directives_multiple_directives(tmp_path):
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
        encoding="UTF-8",
    )

    _fix_myst_directives([str(md)])

    assert md.read_text(encoding="UTF-8") == (
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


@pytest.mark.parametrize(
    ("before", "after"),
    [
        pytest.param(
            "\\:::\\{admonition} Coming from `pacapt`?\n\\:class: tip\nBody.\n\\:::\n",
            ":::{admonition} Coming from `pacapt`?\n:class: tip\nBody.\n:::\n",
            id="title-and-class",
        ),
        pytest.param(
            "\\:::\\{note}\nBody.\n\\:::\n",
            ":::{note}\nBody.\n:::\n",
            id="note-no-title",
        ),
        pytest.param(
            "\\:::\\{admonition} T\n\\:class: tip\n\\:name: ref\nBody.\n\\:::\n",
            ":::{admonition} T\n:class: tip\n:name: ref\nBody.\n:::\n",
            id="multiple-options",
        ),
        pytest.param(
            "\\::::\\{admonition} Outer\n\\:::\\{tip} Inner\nx\n\\:::\n\\::::\n",
            "::::{admonition} Outer\n:::{tip} Inner\nx\n:::\n::::\n",
            id="nested-fences",
        ),
    ],
)
def test_escaped_colon_fence_regex(before, after):
    """Escaped colon-fence directives are un-escaped, nesting included."""
    assert _ESCAPED_COLON_FENCE_RE.sub(_unescape_colon_fence, before) == after


@pytest.mark.parametrize(
    "content",
    [
        pytest.param(":::{note}\nBody.\n:::\n", id="unescaped-colon-fence"),
        pytest.param("Term\n: definition\n", id="deflist-single-colon"),
        pytest.param("A line with \\:-) smiley.\n", id="stray-escaped-colon"),
        pytest.param("```python\nx = 1\n```\n", id="plain-code-fence"),
    ],
)
def test_escaped_colon_fence_regex_no_false_positives(content):
    """Unescaped fences, deflist syntax and stray escapes are left untouched."""
    assert _ESCAPED_COLON_FENCE_RE.sub(_unescape_colon_fence, content) == content


def test_fix_myst_directives_colon_fence_in_place(tmp_path):
    """Both fixups run together: YAML options and escaped colon fences."""
    md = tmp_path / "both.md"
    md.write_text(
        "```{python:render}\n"
        "---\n"
        "mirror:\n"
        "---\n"
        "print(table())\n"
        "```\n"
        "\n"
        "\\:::\\{admonition} Coming from `pacapt`?\n"
        "\\:class: tip\n"
        "Body.\n"
        "\\:::\n",
        encoding="UTF-8",
    )

    _fix_myst_directives([str(md)])

    assert md.read_text(encoding="UTF-8") == (
        "```{python:render}\n"
        ":mirror:\n"
        "print(table())\n"
        "```\n"
        "\n"
        ":::{admonition} Coming from `pacapt`?\n"
        ":class: tip\n"
        "Body.\n"
        ":::\n"
    )


def test_check_bypasses_post_process():
    """check_bypasses_post_process is True only for a post_process tool in check mode."""
    mdformat = TOOL_REGISTRY["mdformat"]
    assert mdformat.check_bypasses_post_process(("--check", "readme.md"))
    assert not mdformat.check_bypasses_post_process(("readme.md",))
    # Tools without a post_process callback are authoritative in check mode.
    ruff = TOOL_REGISTRY["ruff"]
    assert not ruff.check_bypasses_post_process(("format", "--check"))


@patch("repomatic.tooling.tool_runner.subprocess.run")
@patch("repomatic.tooling.tool_runner._install_binary")
@patch("repomatic.tooling.tool_runner.is_github_ci", return_value=False)
def test_run_tool_warns_when_check_bypasses_post_process(
    mock_ci,
    mock_install,
    mock_run,
    tmp_path,
    monkeypatch,
    caplog,
):
    """mdformat --check warns that the post-process fixup is skipped."""
    monkeypatch.chdir(tmp_path)
    bin_path = tmp_path / "shfmt"
    bin_path.touch()
    mock_install.return_value = bin_path
    mock_run.return_value = MagicMock(returncode=1)

    with caplog.at_level(logging.WARNING):
        run_tool("mdformat", extra_args=("--check", "readme.md"))

    assert "check mode" in caplog.text


@patch("repomatic.tooling.tool_runner.subprocess.run")
@patch("repomatic.tooling.tool_runner._install_binary")
@patch("repomatic.tooling.tool_runner.is_github_ci", return_value=False)
def test_run_tool_no_check_warning_in_write_mode(
    mock_ci,
    mock_install,
    mock_run,
    tmp_path,
    monkeypatch,
    caplog,
):
    """mdformat in write mode (no check flag) emits no check-mode warning."""
    monkeypatch.chdir(tmp_path)
    bin_path = tmp_path / "shfmt"
    bin_path.touch()
    mock_install.return_value = bin_path
    mock_run.return_value = MagicMock(returncode=0)

    with caplog.at_level(logging.WARNING):
        run_tool("mdformat", extra_args=("readme.md",))

    assert "check mode" not in caplog.text


# ---------------------------------------------------------------------------
# [tool.X] to CLI flags translation
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Per-platform strip_components
# ---------------------------------------------------------------------------


def test_strip_components_resolves_per_platform():
    """A dict `strip_components` resolves like `archive_format` does.

    `gh` is the reason both exist: its Linux and macOS archives nest under a
    versioned directory while the Windows zip does not, so one count cannot
    serve every platform.
    """
    spec = BinarySpec(
        urls={},
        checksums={},
        archive_format=ArchiveFormat.ZIP,
        strip_components={ALL_PLATFORMS: 1, WINDOWS: 0},
    )
    assert spec.get_strip_components((LINUX, X86_64)) == 1
    assert spec.get_strip_components((MACOS, AARCH64)) == 1
    assert spec.get_strip_components((WINDOWS, X86_64)) == 0
    assert spec.get_strip_components((WINDOWS, AARCH64)) == 0


def test_strip_components_plain_int_applies_everywhere():
    """A bare `int` keeps applying to every platform, as before."""
    spec = BinarySpec(urls={}, checksums={}, archive_format=ArchiveFormat.ZIP)
    assert spec.get_strip_components((LINUX, X86_64)) == 0
    assert spec.get_strip_components((WINDOWS, X86_64)) == 0


def test_gh_spec_matches_upstream_archive_layout():
    """`gh`'s declared layout matches what cli/cli actually publishes.

    Locks in the asymmetry that motivated per-platform `strip_components`: a
    future release that flattened the Linux tarball, or nested the Windows zip,
    would extract the wrong path and only fail during a release.
    """
    binary = TOOL_REGISTRY["gh"].binary
    assert binary is not None
    assert binary.archive_executable == "bin/gh"
    for key, expected_fmt, expected_strip in (
        ((LINUX, AARCH64), ArchiveFormat.TAR_GZ, 1),
        ((LINUX, X86_64), ArchiveFormat.TAR_GZ, 1),
        ((MACOS, AARCH64), ArchiveFormat.ZIP, 1),
        ((MACOS, X86_64), ArchiveFormat.ZIP, 1),
        ((WINDOWS, AARCH64), ArchiveFormat.ZIP, 0),
        ((WINDOWS, X86_64), ArchiveFormat.ZIP, 0),
    ):
        assert binary.get_archive_format(key) is expected_fmt, key
        assert binary.get_strip_components(key) == expected_strip, key


# ---------------------------------------------------------------------------
# ensure_binary
# ---------------------------------------------------------------------------


def test_ensure_binary_rejects_unknown_tool():
    """An unregistered name fails loudly rather than falling back to $PATH."""
    with pytest.raises(ClickException, match="not a binary tool"):
        ensure_binary("definitely-not-a-registered-tool")


def test_ensure_binary_rejects_non_binary_tool():
    """A uvx- or npm-backed tool has no binary to place, so it is refused.

    `ruff` installs through uvx; asking for its executable path would silently
    return nothing useful, so the seam rejects it instead.
    """
    assert TOOL_REGISTRY["ruff"].binary is None
    with pytest.raises(ClickException, match="not a binary tool"):
        ensure_binary("ruff")


def test_image_optimizers_resolve_through_registry_or_path():
    """`oxipng` comes from the registry; `jpegoptim` still needs `$PATH`.

    Guards the split documented in `_check_tool`: oxipng ships prebuilt
    binaries and is pinned and checksummed, while jpegoptim publishes source
    only and stays a distro package.
    """
    assert TOOL_REGISTRY["oxipng"].binary is not None
    assert "jpegoptim" not in TOOL_REGISTRY
    # Registry-backed tools report available without consulting $PATH.
    with patch("repomatic.images.shutil.which", return_value=None):
        assert _check_tool("oxipng") is True
        assert _check_tool("jpegoptim") is False


# ---------------------------------------------------------------------------
# Default argument scope
# ---------------------------------------------------------------------------


def test_resolve_default_args_without_defaults():
    """A tool declaring none runs bare, exactly as before."""
    assert resolve_default_args(TOOL_REGISTRY["ruff"]) == [[]]


def test_resolve_default_args_flags_only():
    """A tool whose CI shape is a literal argument needs no inventory."""
    assert resolve_default_args(TOOL_REGISTRY["zizmor"]) == [["."]]


def test_resolve_default_args_appends_the_inventory(tmp_path, monkeypatch):
    """Declared paths resolve to the repository's own matching files."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "harvest.py").write_text("mango = 1\n", encoding="UTF-8")
    (tmp_path / "orchard.py").write_text("papaya = 2\n", encoding="UTF-8")

    assert resolve_default_args(TOOL_REGISTRY["mypy"]) == [["harvest.py", "orchard.py"]]


def test_resolve_default_args_keeps_default_args_ahead_of_paths(tmp_path, monkeypatch):
    """A subcommand stays the first token, where the tool's parser wants it."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "harvest.json").write_text("{}\n", encoding="UTF-8")

    batches = resolve_default_args(TOOL_REGISTRY["biome"])
    assert batches is not None
    (batch,) = batches
    assert batch[0] == "format"
    assert batch[-1] == "harvest.json"


def test_resolve_default_args_splits_per_file(tmp_path, monkeypatch):
    """`per_file` mirrors `xargs -n1`: one invocation per target."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "harvest.md").write_text("# Mango\n", encoding="UTF-8")
    (tmp_path / "orchard.md").write_text("# Papaya\n", encoding="UTF-8")

    assert resolve_default_args(TOOL_REGISTRY["mdformat"]) == [
        ["harvest.md"],
        ["orchard.md"],
    ]


def test_resolve_default_args_empty_inventory_means_skip(tmp_path, monkeypatch):
    """No matching file resolves to `None`, never to a pathless invocation.

    This is the guard the whole feature exists for: a write-mode formatter
    handed zero paths walks the entire tree instead of doing nothing.
    """
    monkeypatch.chdir(tmp_path)
    assert resolve_default_args(TOOL_REGISTRY["mdformat"]) is None


@patch("repomatic.tooling.tool_runner._install_binary")
@patch("repomatic.tooling.tool_runner.subprocess.run")
@patch("repomatic.tooling.tool_runner.is_github_ci", return_value=False)
def test_run_tool_skips_a_tool_with_no_targets(
    mock_ci, mock_run, mock_install, tmp_path, monkeypatch
):
    """An empty inventory means the tool is never spawned."""
    monkeypatch.chdir(tmp_path)

    assert run_tool("mdformat") == 0
    mock_run.assert_not_called()


@patch("repomatic.tooling.tool_runner._install_binary")
@patch("repomatic.tooling.tool_runner.subprocess.run")
@patch("repomatic.tooling.tool_runner.is_github_ci", return_value=False)
def test_run_tool_runs_once_per_file(
    mock_ci, mock_run, mock_install, tmp_path, monkeypatch
):
    """A per-file tool is invoked once per target, installed once."""
    monkeypatch.chdir(tmp_path)
    mock_run.return_value = MagicMock(returncode=0)
    (tmp_path / "harvest.md").write_text("# Mango\n", encoding="UTF-8")
    (tmp_path / "orchard.md").write_text("# Papaya\n", encoding="UTF-8")

    assert run_tool("mdformat") == 0
    assert mock_run.call_count == 2
    assert [call[0][0][-1] for call in mock_run.call_args_list] == [
        "harvest.md",
        "orchard.md",
    ]


@patch("repomatic.tooling.tool_runner._install_binary")
@patch("repomatic.tooling.tool_runner.subprocess.run")
@patch("repomatic.tooling.tool_runner.is_github_ci", return_value=False)
def test_run_tool_reports_the_first_failure_across_batches(
    mock_ci, mock_run, mock_install, tmp_path, monkeypatch
):
    """Every target still runs, and no later success masks an earlier failure."""
    monkeypatch.chdir(tmp_path)
    mock_run.side_effect = [MagicMock(returncode=2), MagicMock(returncode=0)]
    (tmp_path / "harvest.md").write_text("# Mango\n", encoding="UTF-8")
    (tmp_path / "orchard.md").write_text("# Papaya\n", encoding="UTF-8")

    assert run_tool("mdformat") == 2
    assert mock_run.call_count == 2


@patch("repomatic.tooling.tool_runner._install_binary")
@patch("repomatic.tooling.tool_runner.subprocess.run")
@patch("repomatic.tooling.tool_runner.is_github_ci", return_value=False)
def test_run_tool_explicit_args_suppress_defaults(
    mock_ci, mock_run, mock_install, tmp_path, monkeypatch
):
    """A caller-supplied argument means the caller drives the whole argv.

    Without the all-or-nothing rule, biome's `format` default would splice
    ahead of an explicit `check`, building a command neither one asked for.
    """
    monkeypatch.chdir(tmp_path)
    mock_run.return_value = MagicMock(returncode=0)
    (tmp_path / "harvest.json").write_text("{}\n", encoding="UTF-8")

    assert run_tool("biome", extra_args=("check", "harvest.json")) == 0
    cmd = mock_run.call_args[0][0]
    assert "format" not in cmd
    assert cmd[-2:] == ["check", "harvest.json"]


@pytest.mark.parametrize(
    "spec",
    [spec for spec in TOOL_REGISTRY.values() if spec.default_paths],
    ids=lambda spec: spec.name,
)
def test_default_paths_names_a_real_inventory_group(spec):
    """A tool's declared target group exists and yields paths.

    The field is a string because the inventory resolves against the working
    directory at call time, so a typo would otherwise surface as an
    `AttributeError` mid-run, in CI, after the tool was already installed.
    """
    group = getattr(FileInventory(), spec.default_paths, None)
    assert group is not None, (
        f"{spec.name} declares default_paths={spec.default_paths!r}, which is "
        "not a FileInventory attribute"
    )
    assert all(isinstance(path, Path) for path in group)


@pytest.mark.parametrize(
    "spec",
    [spec for spec in TOOL_REGISTRY.values() if spec.per_file],
    ids=lambda spec: spec.name,
)
def test_per_file_requires_default_paths(spec):
    """`per_file` splits a target list, so it is meaningless without one."""
    assert spec.default_paths, (
        f"{spec.name} sets per_file with no default_paths to split"
    )


# ---------------------------------------------------------------------------
# Rewrite-status crash detection
# ---------------------------------------------------------------------------

MESSY_PYPROJECT = '[project]\nname="orchard"\nversion="1.0"\n'
"""An unformatted `pyproject.toml`, the input a formatter is expected to rewrite."""


@patch("repomatic.tooling.tool_runner._install_binary")
@patch("repomatic.tooling.tool_runner.subprocess.run")
@patch("repomatic.tooling.tool_runner.is_github_ci", return_value=False)
def test_run_tool_reports_a_rewrite_status_that_rewrote_nothing(
    mock_ci, mock_run, mock_install, tmp_path, monkeypatch, caplog
):
    """A formatter claiming a rewrite it did not perform is reported as a crash.

    This is how pyproject-fmt dies on a `PanicException`: exit code 1, the same
    one it uses for a successful reformat, and an untouched file. Callers have
    to tolerate that code, so without the file check the crash reads as a
    normal run and the autofix job goes green having formatted nothing.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text(MESSY_PYPROJECT, encoding="UTF-8")
    mock_run.return_value = MagicMock(returncode=1)

    with caplog.at_level(logging.ERROR):
        assert run_tool("pyproject-fmt") == TOOL_CRASH_EXIT_CODE

    assert "left every target unchanged" in caplog.text
    assert (tmp_path / "pyproject.toml").read_text(encoding="UTF-8") == MESSY_PYPROJECT


@patch("repomatic.tooling.tool_runner._install_binary")
@patch("repomatic.tooling.tool_runner.subprocess.run")
@patch("repomatic.tooling.tool_runner.is_github_ci", return_value=False)
def test_run_tool_passes_a_rewrite_status_that_rewrote_a_file(
    mock_ci, mock_run, mock_install, tmp_path, monkeypatch
):
    """The same exit code is passed through untouched when a file did change."""
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "pyproject.toml"
    target.write_text(MESSY_PYPROJECT, encoding="UTF-8")

    def reformat(*args, **kwargs):
        target.write_text('[project]\nname = "orchard"\n', encoding="UTF-8")
        return MagicMock(returncode=1)

    mock_run.side_effect = reformat

    assert run_tool("pyproject-fmt") == 1


@patch("repomatic.tooling.tool_runner._install_binary")
@patch("repomatic.tooling.tool_runner.subprocess.run")
@patch("repomatic.tooling.tool_runner.is_github_ci", return_value=False)
def test_run_tool_leaves_other_tools_exit_codes_alone(
    mock_ci, mock_run, mock_install, tmp_path, monkeypatch
):
    """A tool declaring no rewrite status keeps reporting its own failures.

    mdformat exits 1 on a genuine error, never to announce a rewrite, so the
    contradiction check must not reinterpret it.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "harvest.md").write_text("# Mango\n", encoding="UTF-8")
    mock_run.return_value = MagicMock(returncode=1)

    assert TOOL_REGISTRY["mdformat"].rewrite_exit_code is None
    assert run_tool("mdformat") == 1


@patch("repomatic.tooling.tool_runner.run_tool", return_value=TOOL_CRASH_EXIT_CODE)
def test_verify_via_write_path_propagates_a_crash(mock_run_tool, tmp_path, monkeypatch):
    """A tool that dies on the copies is a failure, not a clean bill of health.

    The copies come back unformatted, so comparing them to the originals finds
    no difference. Reporting that as "already formatted" would turn every
    crash into a passing verification.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text(MESSY_PYPROJECT, encoding="UTF-8")

    exit_code, drifted = verify_via_write_path("pyproject-fmt")

    assert exit_code == TOOL_CRASH_EXIT_CODE
    assert drifted == []


@pytest.mark.skipif(
    not sys.platform.startswith("darwin")
    and platform.machine().lower() in ("aarch64", "arm64"),
    reason=(
        "mdformat-config pulls taplo, which has no prebuilt wheel and a broken "
        "0.9.3 sdist on Linux and Windows ARM64; only macOS ARM64 ships one"
    ),
)
def test_verify_via_write_path_accepts_the_working_directory(tmp_path, monkeypatch):
    """A path resolving to the working directory verifies the default set.

    The scratch root is created inside the working directory, so mirroring `.`
    under it makes the scratch its own copy destination and `shutil.copytree`
    dies on it as an existing path. It reached users as a bare
    `FileExistsError: .repomatic-verify-<rand>`, which reads as a repository
    problem rather than a rejected argument.
    """
    monkeypatch.chdir(tmp_path)
    # Explicit LF: `write_text`'s default newline translation would otherwise
    # write CRLF on Windows, and mdformat normalizes to LF, so the untouched
    # original would drift from its own formatted copy on line endings alone.
    (tmp_path / "harvest.md").write_text(
        "# Mango\n\nRipe.\n", encoding="UTF-8", newline="\n"
    )

    exit_code, drifted = verify_via_write_path("mdformat", extra_args=(".",))

    assert exit_code == 0
    assert drifted == []
    # The scratch directory is the thing that used to collide: nothing of it
    # may outlive the call, or the next one collides with the leftover.
    assert not list(tmp_path.glob(".repomatic-verify-*"))


def test_run_rejects_an_unknown_tool_as_a_usage_error():
    """An unknown tool name is refused at parse time, naming the registry.

    It used to reach `run_tool` and raise a bare `ValueError`, so a typo
    printed a Python traceback instead of a usage error.
    """
    result = CliRunner().invoke(repomatic, ["run", "papaya"])
    assert result.exit_code == 2, result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "papaya" in result.output
    for tool in ("ruff", "mypy"):
        assert tool in result.output


def test_run_offers_every_registered_tool():
    """The argument's choices are the registry, so neither can drift."""
    command = repomatic.commands["run"]
    param = next(p for p in command.params if p.name == "tool_name")
    assert isinstance(param.type, Choice)
    assert tuple(param.type.choices) == tuple(sorted(TOOL_REGISTRY))
