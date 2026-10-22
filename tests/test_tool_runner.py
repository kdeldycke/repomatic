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

import json
import logging
import os
import re
import threading
from dataclasses import replace
from itertools import combinations
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import tomlrt
import yaml
from click_extra import Choice
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
from packaging.version import Version

from repomatic.bundle import get_data_content
from repomatic.cli.main import repomatic
from repomatic.cli.tools import run as run_command
from repomatic.deps.uv import UvError
from repomatic.file_inventory import FileInventory
from repomatic.github.urls import parse_github_repo
from repomatic.matrix.axes import (
    SINGLE_RUNNER_PYTHON_VERSIONS,
    TEST_RUNNERS_FULL,
    TEST_RUNNERS_PR,
)
from repomatic.metadata.core import Metadata
from repomatic.metadata.project import MYPY_VERSION_MIN
from repomatic.release.binary import NUITKA_BUILD_TARGETS
from repomatic.tooling.registry import (
    _DIRECTIVE_YAML_OPTIONS_RE,
    _ESCAPED_COLON_FENCE_RE,
    CHECKSUMS,
    NPM_BOOTSTRAP_TOOL,
    NPM_MIN_VERSION_FOR_COOLDOWN,
    TOOL_REGISTRY,
    VERSIONS,
    ArchiveFormat,
    BinaryInstallError,
    BinarySpec,
    NativeFormat,
    ToolBackend,
    ToolInstallError,
    ToolSpec,
    UnsupportedPlatformError,
    _fix_myst_directives,
    _unescape_colon_fence,
    _yaml_block_to_field_list,
    lookup_tool,
    tool_reference,
    tool_summary,
)
from repomatic.tooling.runner import (
    NPM_INSTALL_FLAGS,
    TOOL_CRASH_EXIT_CODE,
    ConfigLevel,
    ConfigSource,
    _build_install_args,
    _digest_targets,
    _install_into_venv,
    _install_npm,
    _installs_into_venv,
    _npm_supports_cooldown,
    _path_tools_env,
    _prepare,
    _venv_install_args,
    find_unmodified_configs,
    resolve_config,
    resolve_default_args,
    run_tool,
    verify_via_write_path,
)
from tests.conftest import skip_unless_tool_runs

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

    # pyproject_table, when set, must differ from name (otherwise use None).
    if spec.pyproject_table is not None:
        assert spec.pyproject_table != spec.name, (
            f"{name}: pyproject_table equals name, set pyproject_table=None instead"
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
    # the runner installs each one through install_binary to put it on PATH, so
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
                        f"{name}: short flag {flag!r} from computed_params: "
                        "use long form"
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

    # install_into_venv installs into the venv that needs_venv provides.
    if spec.install_into_venv:
        assert spec.needs_venv, f"{name}: install_into_venv requires needs_venv=True"

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

    # An attestation is verified against the GitHub release that serves the
    # download, which must be a release of the tool's own repository.
    if spec.binary is not None and spec.binary.attestation is not None:
        own_repo = "/".join(parse_github_repo(spec.source_url or "") or ())
        for key in spec.binary.urls:
            repo, tag = spec.resolve_binary(key).release_source
            assert repo == own_repo, (
                f"{name}: {key} downloads from {repo}, not {own_repo}"
            )
            assert spec.version in tag, f"{name}: tag {tag!r} names no version"

    # with_packages is only meaningful for uvx-invoked tools.
    if spec.binary is not None:
        assert not spec.with_packages, f"{name}: binary tools cannot use with_packages"

    # Every with_packages entry must carry an `==` pin. An unpinned entry both
    # floats the environment between runs and is invisible to sync-tool-versions,
    # so it would silently age out the way mdformat's own `ruff==` pin once did.
    for entry in spec.with_packages:
        package, separator, version = entry.partition("==")
        assert separator and package and version, (
            f"{name}: with_packages entry {entry!r} must be pinned as "
            "`package==version`"
        )

    if spec.default_config:
        content = get_data_content(spec.default_config)
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
            assert "{version}" in url, (  # noqa: RUF027  # A literal placeholder.
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
    config = yaml.safe_load(get_data_content("actionlint.yaml"))

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


@pytest.mark.parametrize("locked", (False, True), ids=("no-lock", "uv-lock"))
@pytest.mark.parametrize(
    ("name", "spec"),
    [(n, s) for n, s in TOOL_REGISTRY.items() if s.binary is None and s.npm is None],
)
def test_uv_exemptions_follow_the_install_command(
    name, spec, locked, tmp_path, monkeypatch
):
    """A PyPI tool reads `[tool.uv]` exemptions exactly when `uvx` does not install it.

    `lint-repo` asks `ToolBackend.reads_uv_exemptions` whether a project entry
    can admit a pin inside the window. `uvx` reads no project configuration,
    while `uv run` and `uv pip install` read it, so the answer must follow the
    command the runner builds.
    """
    monkeypatch.chdir(tmp_path)
    if locked:
        (tmp_path / "uv.lock").touch()
    command = (
        _venv_install_args(spec, ".venv")
        if _installs_into_venv(spec)
        else _build_install_args(spec)
    )
    assert spec.backend.reads_uv_exemptions == (command[0] != "uvx"), name


@pytest.mark.parametrize(
    ("name", "spec"),
    [(n, s) for n, s in TOOL_REGISTRY.items() if s.binary is None],
)
def test_uvx_tool_pins_registry_version(name, spec, tmp_path, monkeypatch):
    """Every uvx-installed tool pins its `TOOL_REGISTRY` version in the command.

    Locks the invariant that keeps the per-tool command tests drift-proof: a
    version bump in `TOOL_REGISTRY` must flow into the `name==version` pin with
    no hand-edited literal. A hard-coded `name==X.Y.Z` in an individual test
    silently goes stale on the next bump (a missed `pyproject-fmt` literal once
    reddened every Tests cell), so version-pinning is asserted here once,
    generically, against the shared builder rather than restated per tool.

    The pin sits in the command that installs the tool. With a `uv.lock`, a
    tool declaring `install_into_venv` is installed by its own command.
    """
    pin = spec.pypi_pin
    monkeypatch.chdir(tmp_path)
    for locked in (False, True):
        if locked:
            (tmp_path / "uv.lock").touch()
        command = (
            _venv_install_args(spec, ".venv")
            if _installs_into_venv(spec)
            else _build_install_args(spec)
        )
        assert pin in command, (
            f"{name}: missing registry pin {pin!r} (uv.lock: {locked})"
        )


@pytest.mark.parametrize(
    ("name", "spec"),
    [(n, s) for n, s in TOOL_REGISTRY.items() if s.install_into_venv],
)
def test_build_install_args_install_into_venv(name, spec, tmp_path, monkeypatch):
    """A tool installed into the project venv runs from it, with no layer."""
    monkeypatch.chdir(tmp_path)
    pin = spec.pypi_pin

    # Without a uv.lock there is no project venv to install into: the tool is
    # layered over an isolated environment, like any other needs_venv tool.
    assert not _installs_into_venv(spec)
    unlocked = _build_install_args(spec)
    assert unlocked[unlocked.index("--with") + 1] == pin

    (tmp_path / "uv.lock").touch()
    assert _installs_into_venv(spec)
    locked = _build_install_args(spec)
    assert "--with" not in locked
    assert pin not in locked
    # The install has synced the venv already.
    assert locked[: locked.index("--")] == [
        "uv",
        "--no-progress",
        "run",
        "--frozen",
        "--no-sync",
    ]


@pytest.mark.parametrize(
    ("name", "spec"),
    [(n, s) for n, s in TOOL_REGISTRY.items() if s.install_into_venv],
)
@patch("repomatic.tooling.runner.subprocess.run")
def test_install_into_venv_installs_the_pin(mock_run, name, spec):
    """The pinned tool goes into the venv that `uv run --frozen` reports."""
    mock_run.side_effect = [
        MagicMock(returncode=0, stdout="/orchard/.venv\n"),
        MagicMock(returncode=0),
    ]

    _install_into_venv(spec)

    locate, install = (call.args[0] for call in mock_run.call_args_list)
    assert locate[:4] == ["uv", "--no-progress", "run", "--frozen"]
    assert install == [
        "uv",
        "--no-progress",
        "pip",
        "install",
        "--python",
        "/orchard/.venv",
        spec.pypi_pin,
    ]


@pytest.mark.parametrize(
    ("results", "message"),
    [
        # uv fails before it can report the venv.
        ([(2, "")], "Cannot locate the project virtualenv"),
        # uv succeeds and reports nothing.
        ([(0, "\n")], "Cannot locate the project virtualenv"),
        # The venv is found, and the install into it fails.
        ([(0, "/orchard/.venv\n"), (1, "")], "Cannot install nuitka"),
    ],
)
@patch("repomatic.tooling.runner.subprocess.run")
def test_install_into_venv_reports_a_uv_failure(mock_run, results, message):
    """A uv failure stops the run before the tool starts."""
    mock_run.side_effect = [
        MagicMock(returncode=code, stdout=stdout) for code, stdout in results
    ]

    with pytest.raises(ToolInstallError, match=message):
        _install_into_venv(TOOL_REGISTRY["nuitka"])

    assert mock_run.call_count == len(results)


def test_datasource_url_by_backend():
    """datasource_url points at npmjs for npm tools, GitHub/PyPI otherwise."""
    assert (
        TOOL_REGISTRY["awesome-lint"].datasource_url
        == "https://www.npmjs.com/package/awesome-lint"
    )
    assert TOOL_REGISTRY["ruff"].datasource_url == "https://github.com/astral-sh/ruff"
    # A tool with neither npm nor source_url falls back to its PyPI page.
    assert ToolSpec(name="widget").datasource_url == "https://pypi.org/project/widget/"


def _fake_npm_prefixes(dest: Path, *tools: str) -> None:
    """Pre-create the executables the (mocked) npm installs would produce.

    Every tool but the pinned npm lands in *dest*. The pinned npm lands in the
    `npm` folder below it.
    """
    for tool in tools:
        prefix = dest / tool if tool == NPM_BOOTSTRAP_TOOL else dest
        bin_dir = prefix / "node_modules" / ".bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        (bin_dir / tool).write_text("", encoding="UTF-8")


@patch("repomatic.tooling.runner.subprocess.run")
@patch("repomatic.tooling.runner.shutil.which", return_value="/usr/bin/npm")
def test_install_npm_command_shape(mock_which, mock_run, tmp_path):
    """The npm on PATH installs the pinned npm, which installs the tool.

    Both installs land in a prefix of their own, block install scripts and
    carry the cooldown.
    """
    # The pinned npm install, its `npm --version`, the tool install, then the
    # signature audit of each prefix.
    mock_run.side_effect = [
        MagicMock(returncode=0),
        MagicMock(returncode=0, stdout=f"{NPM_MIN_VERSION_FOR_COOLDOWN}\n"),
        MagicMock(returncode=0),
        MagicMock(returncode=0),
        MagicMock(returncode=0),
    ]
    spec = TOOL_REGISTRY["awesome-lint"]
    pinned = TOOL_REGISTRY[NPM_BOOTSTRAP_TOOL]
    _fake_npm_prefixes(tmp_path, NPM_BOOTSTRAP_TOOL, "awesome-lint")

    bin_path = _install_npm(spec, tmp_path, cooldown_days=8)

    bootstrap = mock_run.call_args_list[0][0][0]
    install = mock_run.call_args_list[2][0][0]
    assert bootstrap[:2] == ["/usr/bin/npm", "install"]
    assert f"npm@{pinned.version}" in bootstrap
    assert str(tmp_path / NPM_BOOTSTRAP_TOOL) in bootstrap
    pinned_npm = tmp_path / NPM_BOOTSTRAP_TOOL / "node_modules" / ".bin" / "npm"
    assert install[:2] == [str(pinned_npm), "install"]
    assert f"awesome-lint@{spec.version}" in install
    assert "--prefix" in install and str(tmp_path) in install
    for cmd in (bootstrap, install):
        assert "--min-release-age=8" in cmd
        # Install-time scripts are blocked (the npm supply-chain worm vector);
        # the other flags mirror meta-package-manager's npm `pre_args`.
        for flag in NPM_INSTALL_FLAGS:
            assert flag in cmd
    assert bin_path == tmp_path / "node_modules" / ".bin" / "awesome-lint"
    # The pinned npm audits its own prefix, then the tool's.
    audits = [call[0][0] for call in mock_run.call_args_list[3:]]
    assert [cmd[:3] for cmd in audits] == [
        [str(pinned_npm), "audit", "signatures"],
        [str(pinned_npm), "audit", "signatures"],
    ]
    assert [cmd[-1] for cmd in audits] == [
        str(tmp_path / NPM_BOOTSTRAP_TOOL),
        str(tmp_path),
    ]


@patch("repomatic.tooling.runner.subprocess.run")
@patch("repomatic.tooling.runner.shutil.which", return_value="/usr/bin/npm")
def test_install_npm_rejects_an_unverified_signature(mock_which, mock_run, tmp_path):
    """A failed signature audit fails the install, after the tool landed."""
    # Both installs succeed; the first audit fails.
    mock_run.side_effect = [
        MagicMock(returncode=0),
        MagicMock(returncode=0),
        MagicMock(returncode=1),
    ]
    _fake_npm_prefixes(tmp_path, NPM_BOOTSTRAP_TOOL, "awesome-lint")

    with pytest.raises(ToolInstallError, match="audit signatures"):
        _install_npm(TOOL_REGISTRY["awesome-lint"], tmp_path, cooldown_days=0)

    assert mock_run.call_count == 3


@patch("repomatic.tooling.runner.subprocess.run")
@patch("repomatic.tooling.runner.shutil.which", return_value="/usr/bin/npm")
def test_install_npm_zero_cooldown_omits_flag(mock_which, mock_run, tmp_path):
    """A 0-day cooldown drops the min-release-age flag from both installs."""
    mock_run.return_value = MagicMock(returncode=0)
    _fake_npm_prefixes(tmp_path, NPM_BOOTSTRAP_TOOL, "awesome-lint")

    _install_npm(TOOL_REGISTRY["awesome-lint"], tmp_path, cooldown_days=0)

    # Two installs, then one signature audit per prefix.
    assert mock_run.call_count == 4
    for call in mock_run.call_args_list:
        assert not any(str(arg).startswith("--min-release-age") for arg in call[0][0])


@patch("repomatic.tooling.runner.shutil.which", return_value=None)
def test_install_npm_without_npm_raises(mock_which, tmp_path):
    """A clear error surfaces when Node.js and npm are not on PATH."""
    with pytest.raises(ToolInstallError, match="npm"):
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
@patch("repomatic.tooling.runner.subprocess.run")
def test_npm_supports_cooldown(mock_run, version_output, supported):
    """min-release-age support is gated on npm >= 11.10.0."""
    mock_run.return_value = MagicMock(returncode=0, stdout=version_output)
    assert _npm_supports_cooldown("/usr/bin/npm") is supported


@patch("repomatic.tooling.runner.subprocess.run")
@patch("repomatic.tooling.runner.shutil.which", return_value="/usr/bin/npm")
def test_install_npm_warns_when_npm_too_old(mock_which, mock_run, tmp_path, caplog):
    """An npm that cannot enforce the cooldown warns, but still installs.

    Running the pinned npm itself installs it with the npm on PATH, which is
    then the one the floor check reads.
    """
    mock_run.side_effect = [
        MagicMock(returncode=0, stdout="10.8.2\n"),  # npm --version
        MagicMock(returncode=0),  # npm install
        MagicMock(returncode=0),  # npm audit signatures
    ]
    bin_dir = tmp_path / "node_modules" / ".bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / NPM_BOOTSTRAP_TOOL).write_text("", encoding="UTF-8")

    with caplog.at_level(logging.WARNING):
        _install_npm(TOOL_REGISTRY[NPM_BOOTSTRAP_TOOL], tmp_path, cooldown_days=8)

    # The version probe, the install, then the signature audit.
    assert mock_run.call_count == 3
    assert "minimum-release-age cooldown is not enforced" in caplog.text


def test_pinned_npm_enforces_the_cooldown():
    """The npm that installs every npm-backed tool honors `min-release-age`.

    A pin moved below the floor would install every tool with the cooldown
    ignored, and the bootstrap would hide it.
    """
    pinned = TOOL_REGISTRY[NPM_BOOTSTRAP_TOOL]
    assert pinned.npm is not None
    assert Version(pinned.version) >= Version(NPM_MIN_VERSION_FOR_COOLDOWN)


@pytest.mark.parametrize(
    "spec",
    [spec for spec in TOOL_REGISTRY.values() if spec.problem_matcher],
    ids=lambda spec: spec.name,
)
def test_problem_matcher_reads_the_output_of_its_tool(spec):
    """A bundled matcher is a valid matcher file, keyed on a named owner.

    actionlint's own line format, with and without the color codes of its
    `-color` flag, is what the pattern has to read.
    """
    matchers = json.loads(get_data_content(spec.problem_matcher))["problemMatcher"]
    assert [matcher["owner"] for matcher in matchers] == [spec.name]
    (pattern,) = matchers[0]["pattern"]
    finding = '.github/workflows/fig.yaml:3:5: unexpected key "fig" [syntax-check]'
    # The kind closes the line in gray: its reset code opens the next line.
    colored = (
        "\x1b[33m.github/workflows/fig.yaml\x1b[0m\x1b[90m:\x1b[0m3\x1b[90m:\x1b[0m5"
        '\x1b[90m: \x1b[0m\x1b[1munexpected key "fig"\x1b[0m\x1b[90m [syntax-check]'
    )
    for line in (finding, colored):
        match = re.fullmatch(pattern["regexp"], line)
        assert match, line
        assert match.group(pattern["file"]) == ".github/workflows/fig.yaml"
        assert match.group(pattern["line"]) == "3"
        assert match.group(pattern["column"]) == "5"
        assert match.group(pattern["code"]) == "syntax-check"


@pytest.mark.parametrize("in_ci", (True, False))
@patch("repomatic.tooling.runner.subprocess.run")
@patch("repomatic.tooling.binaries.install_binary")
def test_run_tool_wraps_the_run_in_its_problem_matcher(
    mock_install_binary, mock_run, tmp_path, monkeypatch, in_ci
):
    """In GitHub Actions, the matcher is on for the length of the run, and only then.

    The runner reads the matcher file when it processes the registration, so the
    file lands in the job's temporary directory, which outlives the process.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setattr("repomatic.tooling.runner.is_github_ci", lambda: in_ci)
    mock_install_binary.return_value = tmp_path / "actionlint"
    events: list[str] = []
    monkeypatch.setattr(
        "repomatic.tooling.runner.add_problem_matcher",
        lambda path: events.append(f"add {path.name}"),
    )
    monkeypatch.setattr(
        "repomatic.tooling.runner.remove_problem_matcher",
        lambda owner: events.append(f"remove {owner}"),
    )

    def run(*args, **kwargs):
        events.append("run")
        return MagicMock(returncode=0)

    mock_run.side_effect = run

    assert run_tool("actionlint") == 0

    matcher = TOOL_REGISTRY["actionlint"].problem_matcher
    if in_ci:
        assert events == [f"add {matcher}", "run", "remove actionlint"]
        written = (tmp_path / matcher).read_text(encoding="UTF-8")
        assert written == get_data_content(matcher)
    else:
        assert events == ["run"]


def test_actionlint_lints_run_scripts_with_the_pinned_shellcheck():
    """actionlint finds shellcheck on PATH, so the registry puts its own there."""
    assert TOOL_REGISTRY["actionlint"].path_tools == ("shellcheck",)
    assert TOOL_REGISTRY["shellcheck"].binary is not None


def test_lookup_tool_rejects_an_unknown_name():
    """One lookup, one error, naming the tools that do exist."""
    assert lookup_tool("ruff") is TOOL_REGISTRY["ruff"]
    with pytest.raises(ValueError, match=r"Unknown tool: 'papaya'\. Available tools: "):
        lookup_tool("papaya")


def test_resolve_binary_fills_in_the_platform_download():
    """A resolved binary carries what one platform's download needs."""
    spec = ToolSpec(
        name="papaya",
        version="1.2.3",
        binary=BinarySpec(
            urls={(WINDOWS, X86_64): "https://example.com/{version}/papaya.zip"},
            checksums={(WINDOWS, X86_64): "a" * 64},
            archive_format=ArchiveFormat.ZIP,
            archive_executable="bin/papaya",
            strip_components=1,
        ),
    )

    resolved = spec.resolve_binary((WINDOWS, X86_64))

    assert resolved.url == "https://example.com/1.2.3/papaya.zip"
    assert resolved.archive_name == "papaya.zip"
    assert resolved.checksum == "a" * 64
    assert resolved.archive_format is ArchiveFormat.ZIP
    assert resolved.strip_components == 1
    assert resolved.cache_key == "windows-x86_64"
    assert resolved.archive_members == ("bin/papaya", "bin/papaya.exe")
    assert resolved.cached_names == ("papaya", "papaya.exe")


def test_resolve_binary_refuses_a_tool_without_binary():
    """Only a binary tool has a download to resolve."""
    with pytest.raises(ValueError, match="ships no binary"):
        TOOL_REGISTRY["ruff"].resolve_binary()


def test_with_overrides_pins_another_version_and_checksum():
    """The overrides reach the download URL and the checksum on record."""
    spec = TOOL_REGISTRY["shfmt"]
    with (
        patch("repomatic.tooling.registry.current_platform", return_value=UBUNTU),
        patch("repomatic.tooling.registry.current_architecture", return_value=X86_64),
    ):
        overridden = spec.with_overrides(version="9.9.9", checksum="b" * 64)
        resolved = overridden.resolve_binary()

    assert "9.9.9" in resolved.url
    assert resolved.checksum == "b" * 64
    # The registry entry itself is left alone.
    assert spec.binary is not None
    assert spec.binary.checksums[LINUX, X86_64] != "b" * 64
    assert spec.with_overrides() is spec


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        pytest.param(("--output", "lychee/out.md"), [Path("lychee/out.md")], id="pair"),
        pytest.param(("--output=lychee/out.md",), [Path("lychee/out.md")], id="equals"),
        pytest.param(("--output",), [], id="dangling-flag"),
        pytest.param((".",), [], id="no-flag"),
    ],
)
def test_output_paths_reads_both_flag_forms(args, expected):
    """The report destination is read off either form of the output flag."""
    assert TOOL_REGISTRY["lychee"].output_paths(args) == expected
    assert TOOL_REGISTRY["ruff"].output_paths(args) == []


def test_with_package_pins_keep_the_spelling_of_each_entry():
    """A plugin pin splits into the package name as written and its version."""
    spec = ToolSpec(
        name="papaya", with_packages=("papaya_seeds==1.0.0", "papaya-skin==2.1")
    )
    assert spec.with_package_pins == (("papaya_seeds", "1.0.0"), ("papaya-skin", "2.1"))


def test_tool_docs_name_the_table_each_tool_reads():
    """bump-my-version is documented under the table it reads."""
    assert "`[tool.bumpversion]`" in tool_summary()
    reference = tool_reference()
    assert "`[tool.bumpversion]` in `pyproject.toml` (native)" in reference
    assert "`[tool.bump-my-version]` in `pyproject.toml`" not in reference


@pytest.mark.parametrize(("name", "spec"), sorted(TOOL_REGISTRY.items()))
def test_release_badge_follows_the_install_source(name, spec):
    """The release badge reads the registry each tool is installed from."""
    reference = tool_reference()
    section = reference.split(f"### [{spec.label}]")[1].split("\n### ")[0]
    expected = {
        ToolBackend.BINARY: "img.shields.io/github/release-date/",
        ToolBackend.NPM: "img.shields.io/npm/v/",
        ToolBackend.VENV: "img.shields.io/pypi/v/",
        ToolBackend.UVX: "img.shields.io/pypi/v/",
    }[spec.backend]
    assert expected in section, name


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


@pytest.mark.parametrize(
    ("package", "pin"),
    [
        pytest.param(None, "papaya==1.2.3", id="tool-name"),
        pytest.param("papaya[juice]", "papaya[juice]==1.2.3", id="install-extra"),
    ],
)
def test_pypi_pin_names_the_package_and_its_version(package, pin):
    """`pypi_pin` is the exact requirement an installer takes, extra included.

    The command tests above read the pin from this property, so its shape is
    stated here once.
    """
    assert ToolSpec(name="papaya", version="1.2.3", package=package).pypi_pin == pin


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
# Platform resolution and companion binaries
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
        patch("repomatic.tooling.registry.current_platform", return_value=MACOS),
        patch("repomatic.tooling.registry.current_architecture", return_value=AARCH64),
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
        patch("repomatic.tooling.registry.current_platform", return_value=UBUNTU),
        patch("repomatic.tooling.registry.current_architecture", return_value=X86_64),
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
        patch("repomatic.tooling.registry.current_platform", return_value=MACOS),
        patch("repomatic.tooling.registry.current_architecture", return_value=X86_64),
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
            "repomatic.tooling.registry.current_platform",
            return_value=UNKNOWN_PLATFORM,
        ),
        patch("repomatic.tooling.registry.current_architecture", return_value=X86_64),
        patch("repomatic.tooling.registry.sys.platform", "linux"),
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
            "repomatic.tooling.registry.current_platform",
            return_value=UNKNOWN_PLATFORM,
        ),
        patch("repomatic.tooling.registry.current_architecture", return_value=X86_64),
        patch("repomatic.tooling.registry.sys.platform", sys_platform),
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


def test_path_tools_env_skips_platform_without_binary(caplog):
    """A companion publishing no binary here is skipped, not fatal.

    `shfmt` ships nothing for Windows ARM64, so provisioning it for mdformat
    can never succeed there. Aborting would make `repomatic run mdformat`
    unusable on the platform rather than merely leaving its shell blocks
    unformatted, so the run carries on with `PATH` untouched.
    """
    spec = TOOL_REGISTRY["mdformat"]
    assert "shfmt" in spec.path_tools
    with (
        patch("repomatic.tooling.registry.current_platform", return_value=WINDOWS),
        patch("repomatic.tooling.registry.current_architecture", return_value=AARCH64),
        caplog.at_level(logging.WARNING),
    ):
        env = _path_tools_env(spec, skip_checksum=False, no_cache=False)

    assert env is not None
    assert "shfmt" in caplog.text
    assert "No binary for" in caplog.text
    # Nothing was prepended, so the child inherits the ambient PATH verbatim.
    assert env["PATH"] == os.environ.get("PATH", "")


# ---------------------------------------------------------------------------
# run_tool with binary tools
# ---------------------------------------------------------------------------


@patch("repomatic.tooling.runner.subprocess.run")
@patch("repomatic.tooling.binaries.install_binary")
@patch("repomatic.tooling.runner.is_github_ci", return_value=False)
def test_run_tool_binary_uses_direct_path(
    mock_ci,
    mock_install_binary,
    mock_run,
    tmp_path,
    monkeypatch,
):
    """Binary tools use the downloaded binary path, not uvx."""
    monkeypatch.chdir(tmp_path)
    bin_path = tmp_path / "typos"
    bin_path.touch()
    mock_install_binary.return_value = bin_path
    mock_run.return_value = MagicMock(returncode=0)

    exit_code = run_tool("typos")

    assert exit_code == 0
    cmd = mock_run.call_args[0][0]
    assert cmd[0] == str(bin_path)
    assert "uvx" not in cmd
    assert "--write-changes" in cmd


@pytest.mark.parametrize(
    ("name", "flag", "extra_args"),
    [
        pytest.param(
            "ruff",
            "--config=force-exclude=true",
            ("check", "recipes/tarte.py"),
            id="ruff",
        ),
        pytest.param("typos", "--force-exclude", ("recipes/tarte.md",), id="typos"),
    ],
)
@patch("repomatic.tooling.runner.subprocess.run")
@patch("repomatic.tooling.binaries.install_binary")
@patch("repomatic.tooling.runner.is_github_ci", return_value=False)
def test_run_tool_holds_excludes_for_explicit_paths(
    mock_ci,
    mock_install_binary,
    mock_run,
    tmp_path,
    monkeypatch,
    name,
    flag,
    extra_args,
):
    """A path named on the command line stays subject to the tool's exclude list.

    ruff and typos apply their exclude list only while they walk a directory, so a
    file passed by name is processed whatever the list says, unless the flag that
    holds the list comes with it.
    """
    monkeypatch.chdir(tmp_path)
    bin_path = tmp_path / name
    bin_path.touch()
    mock_install_binary.return_value = bin_path
    mock_run.return_value = MagicMock(returncode=0)

    run_tool(name, extra_args=extra_args)

    cmd = mock_run.call_args[0][0]
    assert flag in cmd
    assert cmd.index(flag) < cmd.index(extra_args[0])


@patch("repomatic.tooling.runner.subprocess.run")
@patch("repomatic.tooling.binaries.install_binary")
@patch("repomatic.tooling.runner.is_github_ci", return_value=False)
def test_run_tool_binary_forwards_extra_args(
    mock_ci,
    mock_install_binary,
    mock_run,
    tmp_path,
    monkeypatch,
):
    """Extra args are appended after default flags for binary tools."""
    monkeypatch.chdir(tmp_path)
    bin_path = tmp_path / "biome"
    bin_path.touch()
    mock_install_binary.return_value = bin_path
    mock_run.return_value = MagicMock(returncode=0)

    run_tool("biome", extra_args=("format", "--write", "file.json"))

    cmd = mock_run.call_args[0][0]
    assert cmd[0] == str(bin_path)
    assert "format" in cmd
    assert "--write" in cmd
    assert "file.json" in cmd


@patch("repomatic.tooling.runner.subprocess.run")
@patch("repomatic.tooling.binaries.install_binary")
@patch("repomatic.tooling.runner.is_github_ci", return_value=False)
def test_run_tool_binary_default_flags(
    mock_ci,
    mock_install_binary,
    mock_run,
    tmp_path,
    monkeypatch,
):
    """Binary tools include default_flags in the command."""
    monkeypatch.chdir(tmp_path)
    bin_path = tmp_path / "actionlint"
    bin_path.touch()
    mock_install_binary.return_value = bin_path
    mock_run.return_value = MagicMock(returncode=0)

    run_tool("actionlint")

    cmd = mock_run.call_args[0][0]
    assert "-color" in cmd


@patch("repomatic.tooling.runner.subprocess.run")
@patch("repomatic.tooling.runner.is_github_ci", return_value=False)
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


@patch("repomatic.tooling.runner.subprocess.run")
@patch("repomatic.tooling.runner.is_github_ci", return_value=False)
def test_run_tool_ruff_reads_pyproject_natively(
    mock_ci, mock_run, tmp_path, monkeypatch
):
    """ruff gets no config file when [tool.ruff] exists in pyproject.toml."""
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


@patch("repomatic.tooling.runner.subprocess.run")
@patch("repomatic.tooling.runner.is_github_ci", return_value=False)
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
# Config resolution
# ---------------------------------------------------------------------------


def _declare_tool_table(directory: Path, name: str, table: dict) -> None:
    """Write a `pyproject.toml` in *directory* that holds `[tool.<name>]`."""
    (directory / "pyproject.toml").write_text(
        tomlrt.Document({"tool": {name: table}}).render(), encoding="UTF-8"
    )


def test_resolve_config_reads_pyproject_with_section(tmp_path, monkeypatch):
    """Tools with reads_pyproject=True skip translation when [tool.X] exists."""
    monkeypatch.chdir(tmp_path)
    _declare_tool_table(tmp_path, "testool", {"preview": True})
    spec = ToolSpec(
        name="testool",
        version="1.0.0",
        config_flag="--config",
        default_config="testool.toml",
        reads_pyproject=True,
    )
    args, tmp = resolve_config(spec)
    assert args == []
    assert tmp is None


def test_resolve_config_finds_a_table_named_apart_from_the_tool(tmp_path, monkeypatch):
    """bump-my-version reads `[tool.bumpversion]`, not `[tool.bump-my-version]`."""
    monkeypatch.chdir(tmp_path)
    _declare_tool_table(tmp_path, "bumpversion", {"current_version": "1.2.3"})
    spec = TOOL_REGISTRY["bump-my-version"]

    source = ConfigSource.find(spec)

    assert source.level is ConfigLevel.PYPROJECT
    assert source.label == "[tool.bumpversion] in pyproject.toml"
    assert resolve_config(spec) == ([], None)


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
    args, tmp = resolve_config(spec)
    assert len(args) == 2
    assert args[0] == "--config"
    assert Path(args[1]).exists()
    assert tmp is None


def test_resolve_config_native_file_wins(tmp_path, monkeypatch):
    """Native config file takes precedence over everything else."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".yamllint.yaml").write_text("rules: {}", encoding="UTF-8")
    _declare_tool_table(tmp_path, "yamllint", {"rules": {"line-length": {"max": 80}}})

    spec = TOOL_REGISTRY["yamllint"]
    args, tmp = resolve_config(spec)
    assert args == []
    assert tmp is None


def test_resolve_config_pyproject_section(tmp_path, monkeypatch, cache_env):
    """[tool.X] in pyproject.toml produces a cached config file."""
    monkeypatch.chdir(tmp_path)

    spec = TOOL_REGISTRY["yamllint"]
    tool_config = {"rules": {"line-length": {"max": 80}}}
    _declare_tool_table(tmp_path, "yamllint", tool_config)
    args, tmp = resolve_config(spec)

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
    _declare_tool_table(
        tmp_path, "lychee", {"max_redirects": 5, "exclude": ["example.com"]}
    )
    args, tmp = resolve_config(spec)

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
    _declare_tool_table(
        tmp_path, "lychee", {"cache": {"enable": True, "max_age": 3600}}
    )
    args, tmp = resolve_config(spec)

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


def test_native_format_toml_serialize_reroots_section_and_keeps_comments():
    """A live [tool.X] section loses its prefix and keeps every comment."""
    section = tomlrt.loads(
        "[project]\n"
        'name = "orchard"\n'
        "\n"
        "# above header\n"
        "[tool.x]  # on header\n"
        "# leading\n"
        "a = 1  # eol\n"
        "# above inline table\n"
        "held = { apple = 1 }\n"
        "[tool.x.sub]\n"
        "# nested\n"
        "b = 2\n"
        "[[tool.x.items]]\n"
        "# aot\n"
        "c = 3\n"
    )["tool"]["x"]

    out = NativeFormat.TOML.serialize(section)

    assert "[sub]" in out and "[tool.x.sub]" not in out
    assert "[[items]]" in out and "[[tool.x.items]]" not in out
    for comment in (
        "# above header",
        "# on header",
        "# leading",
        "# eol",
        "# above inline table",
        "# nested",
        "# aot",
    ):
        assert comment in out
    assert tomlrt.loads(out).to_dict() == section.to_dict()


def test_native_format_toml_serialize_inline_section():
    """An inline `tool.x = {...}` table serializes to the same data."""
    section = tomlrt.loads("[tool]\nx = { a = 1, sub = { b = 2 } }\n")["tool"]["x"]

    out = NativeFormat.TOML.serialize(section)

    assert tomlrt.loads(out).to_dict() == {"a": 1, "sub": {"b": 2}}


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
    _declare_tool_table(tmp_path, "biome", tool_config)
    args, tmp = resolve_config(spec)

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
    _declare_tool_table(tmp_path, "mdformat", {"number": True})
    args, tmp = resolve_config(spec)

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
    """No config_flag and no native_config_files raises NotImplementedError."""
    monkeypatch.chdir(tmp_path)

    spec = ToolSpec(
        name="sometool",
        version="1.0.0",
        package="sometool",
    )
    _declare_tool_table(tmp_path, "sometool", {"key": "value"})
    with pytest.raises(NotImplementedError, match="no config_flag"):
        resolve_config(spec)


def test_resolve_config_bundled_default(tmp_path, monkeypatch, cache_env):
    """Bundled default is cached and passed via --config flag."""
    monkeypatch.chdir(tmp_path)

    spec = TOOL_REGISTRY["yamllint"]
    args, tmp = resolve_config(spec)
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
    args, tmp = resolve_config(spec)
    assert args == []
    assert tmp is None


def test_resolve_config_empty_tool_config_is_not_match(
    tmp_path, monkeypatch, cache_env
):
    """An empty [tool.X] table does not count as a config match."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text("[tool.zizmor]\n", encoding="UTF-8")

    spec = TOOL_REGISTRY["zizmor"]
    args, tmp = resolve_config(spec)
    assert len(args) == 2
    assert args[0] == "--config"
    assert Path(args[1]).exists()
    assert tmp is None


# ---------------------------------------------------------------------------
# Config source
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
        # The path keeps its forward slashes on every platform.
        pytest.param(
            "zizmor",
            {".github/zizmor.yml": "rules: {}"},
            ".github/zizmor.yml (replaces bundled default)",
            id="native-file-in-a-directory",
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
                "pyproject.toml": (
                    '[project]\nname = "test"\n\n[tool.ruff]\npreview = true\n'
                )
            },
            "[tool.ruff] in pyproject.toml (replaces bundled default)",
            id="reads-pyproject-native",
        ),
        pytest.param(BARE_TOOL, {}, "(bare)", id="bare"),
    ],
)
def test_config_source_label(tmp_path, monkeypatch, tool, files, expected):
    """The reported config source names where the tool's settings came from."""
    monkeypatch.chdir(tmp_path)
    for name, content in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="UTF-8")

    spec = tool if isinstance(tool, ToolSpec) else TOOL_REGISTRY[tool]
    assert ConfigSource.find(spec).label == expected


# ---------------------------------------------------------------------------
# run_tool
# ---------------------------------------------------------------------------


def test_run_tool_unknown_tool():
    """Raise ValueError for unregistered tool names."""
    with pytest.raises(ValueError, match="Unknown tool"):
        run_tool("nonexistent-tool")


@patch("repomatic.tooling.runner.subprocess.run")
@patch("repomatic.tooling.runner.is_github_ci", return_value=False)
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


@patch("repomatic.tooling.runner.subprocess.run")
@patch("repomatic.tooling.runner.is_github_ci", return_value=True)
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


@patch("repomatic.tooling.runner.subprocess.run")
@patch("repomatic.tooling.runner.is_github_ci", return_value=False)
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


@patch("repomatic.tooling.runner.subprocess.run")
@patch("repomatic.tooling.runner.is_github_ci", return_value=False)
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


@patch("repomatic.tooling.runner.subprocess.run")
@patch("repomatic.tooling.runner.is_github_ci", return_value=False)
def test_run_tool_forwards_exit_code(mock_ci, mock_run, tmp_path, monkeypatch):
    """Tool's exit code is forwarded unchanged."""
    monkeypatch.chdir(tmp_path)
    mock_run.return_value = MagicMock(returncode=42)

    exit_code = run_tool("yamllint", extra_args=(".",))
    assert exit_code == 42


@patch("repomatic.tooling.runner.subprocess.run")
@patch("repomatic.tooling.runner.is_github_ci", return_value=False)
def test_run_tool_reports_a_launcher_missing_from_path(
    mock_ci, mock_run, tmp_path, monkeypatch
):
    """No `uvx` on `PATH` raises the install error callers turn into a message."""
    monkeypatch.chdir(tmp_path)
    mock_run.side_effect = FileNotFoundError(2, "No such file or directory", "uvx")

    with pytest.raises(ToolInstallError, match="Cannot start yamllint"):
        run_tool("yamllint", extra_args=(".",))


@patch("repomatic.tooling.runner.subprocess.run")
@patch("repomatic.tooling.runner.is_github_ci", return_value=False)
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


@patch("repomatic.tooling.runner.subprocess.run")
@patch("repomatic.tooling.runner.is_github_ci", return_value=False)
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


@patch("repomatic.tooling.runner.subprocess.run")
@patch("repomatic.tooling.binaries.install_binary")
@patch("repomatic.tooling.runner.is_github_ci", return_value=False)
def test_run_tool_mdformat_with_packages(
    mock_ci,
    mock_install_binary,
    mock_run,
    tmp_path,
    monkeypatch,
):
    """mdformat runs via uvx with all plugin packages."""
    monkeypatch.chdir(tmp_path)
    bin_path = tmp_path / "shfmt"
    bin_path.touch()
    mock_install_binary.return_value = bin_path
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


@patch("repomatic.tooling.binaries.install_binary")
@patch("repomatic.tooling.runner.is_github_ci", return_value=False)
def test_run_tool_concurrent_runs_keep_their_cwd_config(
    mock_ci, mock_install_binary, cache_env, tmp_path, monkeypatch
):
    """Two runs sharing a directory each keep a written config for their whole run.

    Without the lock, the second run took the first run's temporary
    `.mdformat.toml` for the repository's own, then lost it halfway when the
    first run deleted it on the way out.
    """
    monkeypatch.chdir(tmp_path)
    bin_path = tmp_path / "shfmt"
    bin_path.touch()
    mock_install_binary.return_value = bin_path
    for name in ("apple.md", "banana.md"):
        (tmp_path / name).write_text("# Orchard\n", encoding="UTF-8")
    config = tmp_path / ".mdformat.toml"
    first_running = threading.Event()
    second_running = threading.Event()
    seen: dict[str, tuple[bool, bool]] = {}

    def fake_run(cmd, **kwargs):
        target = "apple.md" if "apple.md" in cmd else "banana.md"
        at_start = config.exists()
        if target == "apple.md":
            first_running.set()
            # Leave the second run time to start inside this one, if it can.
            second_running.wait(timeout=0.5)
        else:
            second_running.set()
            # Outlive the first run, which removes its config on exit.
            first.join(timeout=5)
        seen[target] = (at_start, config.exists())
        return MagicMock(returncode=0)

    first = threading.Thread(
        target=run_tool, args=("mdformat",), kwargs={"extra_args": ("apple.md",)}
    )
    with patch("repomatic.tooling.runner.subprocess.run", side_effect=fake_run):
        first.start()
        assert first_running.wait(timeout=5)
        run_tool("mdformat", extra_args=("banana.md",))
        first.join(timeout=5)

    assert seen == {"apple.md": (True, True), "banana.md": (True, True)}
    assert not config.exists()


@patch("repomatic.tooling.runner.subprocess.run")
@patch("repomatic.tooling.binaries.install_binary")
@patch("repomatic.tooling.runner.is_github_ci", return_value=False)
def test_run_tool_runs_unlocked_when_the_cache_is_unwritable(
    mock_ci, mock_install_binary, mock_run, tmp_path, monkeypatch
):
    """A lock file that cannot be created does not stop the run."""
    monkeypatch.chdir(tmp_path)
    bin_path = tmp_path / "shfmt"
    bin_path.touch()
    mock_install_binary.return_value = bin_path
    # A regular file where the cache root should be: no directory fits under it.
    blocker = tmp_path / "cache"
    blocker.write_text("", encoding="UTF-8")
    monkeypatch.setenv("REPOMATIC_CACHE_DIR", str(blocker))
    (tmp_path / "apple.md").write_text("# Orchard\n", encoding="UTF-8")
    mock_run.return_value = MagicMock(returncode=0)

    assert run_tool("mdformat", extra_args=("apple.md",)) == 0

    mock_run.assert_called_once()
    assert not (tmp_path / ".mdformat.toml").exists()


@patch("repomatic.tooling.runner.subprocess.run")
@patch("repomatic.tooling.runner.Metadata")
@patch("repomatic.tooling.runner.is_github_ci", return_value=False)
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


@patch("repomatic.tooling.runner.subprocess.run")
@patch("repomatic.tooling.runner.Metadata")
@patch("repomatic.tooling.runner.is_github_ci", return_value=False)
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


@patch("repomatic.tooling.runner.subprocess.run")
@patch("repomatic.tooling.runner.Metadata")
@patch("repomatic.tooling.runner.is_github_ci", return_value=False)
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


@pytest.mark.once
@pytest.mark.real_http
def test_mypy_version_floor_matches_the_pinned_mypy(tmp_path, monkeypatch):
    """The pinned mypy accepts `MYPY_VERSION_MIN` and refuses the release before.

    `mypy_params` floors `--python-version` at this value, so a project that
    declares an older `requires-python` still hands mypy a dialect it runs.
    """
    monkeypatch.chdir(tmp_path)
    skip_unless_tool_runs("mypy")
    (tmp_path / "orchard.py").write_text("apples = 3\n", encoding="UTF-8")
    major, minor = MYPY_VERSION_MIN

    floor = run_tool(
        "mypy", extra_args=("--python-version", f"{major}.{minor}", "orchard.py")
    )
    below = run_tool(
        "mypy", extra_args=("--python-version", f"{major}.{minor - 1}", "orchard.py")
    )

    assert floor == 0
    # Exit code 2 is how mypy refuses an argument.
    assert below == 2


@patch("repomatic.tooling.runner.subprocess.run")
@patch("repomatic.tooling.runner.is_github_ci", return_value=False)
def test_run_tool_nuitka_uses_module_invocation(
    mock_ci, mock_run, tmp_path, monkeypatch
):
    """nuitka runs via `python -m nuitka` to avoid Windows script resolution issues."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "uv.lock").touch()
    mock_run.return_value = MagicMock(returncode=0, stdout="/orchard/.venv\n")

    run_tool("nuitka", extra_args=("repomatic",))

    # Nuitka is installed into the project venv first, then runs from it.
    locate, install, cmd = (call.args[0] for call in mock_run.call_args_list)
    assert locate[-1] == "import sys; print(sys.prefix)"
    assert install[2:6] == ["pip", "install", "--python", "/orchard/.venv"]
    assert cmd[0] == "uv"
    assert "run" in cmd
    assert "--frozen" in cmd
    assert "--no-sync" in cmd
    assert "--with" not in cmd
    python_idx = cmd.index("python")
    assert cmd[python_idx + 1] == "-m"
    assert cmd[python_idx + 2] == "nuitka"
    assert "repomatic" in cmd


@patch("repomatic.tooling.runner.subprocess.run")
@patch("repomatic.tooling.runner.is_github_ci", return_value=False)
def test_run_tool_nuitka_nofollow_imports_default(
    mock_ci, mock_run, tmp_path, monkeypatch
):
    """nuitka excludes tkinter by default via nuitka.nofollow-imports."""
    monkeypatch.chdir(tmp_path)
    mock_run.return_value = MagicMock(returncode=0)

    run_tool("nuitka", extra_args=("repomatic",))

    cmd = mock_run.call_args[0][0]
    assert "--nofollow-import-to=tkinter" in cmd


@patch("repomatic.tooling.runner.subprocess.run")
@patch("repomatic.tooling.runner.is_github_ci", return_value=False)
def test_run_tool_nuitka_gets_its_target_environment(
    mock_ci, mock_run, tmp_path, monkeypatch
):
    """The variables the prepare hook returns reach Nuitka, over the inherited ones."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("NUITKA_CACHE_DIR", raising=False)
    monkeypatch.setattr(
        "repomatic.tooling.nuitka.current_build_target",
        lambda: NUITKA_BUILD_TARGETS["macos-x64"],
    )
    mock_run.return_value = MagicMock(returncode=0)

    run_tool("nuitka", extra_args=("repomatic",))

    cmd = mock_run.call_args.args[0]
    env = mock_run.call_args.kwargs["env"]
    assert "--disable-cache=ccache" in cmd
    assert env["MACOSX_DEPLOYMENT_TARGET"] == "10.15"
    assert env["PATH"] == os.environ["PATH"]


def test_prepare_failure_is_an_install_error():
    """A preparation uv refuses ends the run like a failed install."""

    def refuse(metadata):
        raise UvError(2, ["uv", "sync"], "", "No such extra: kiwi")

    spec = replace(TOOL_REGISTRY["nuitka"], prepare=refuse)
    with pytest.raises(ToolInstallError, match="No such extra: kiwi"):
        _prepare(spec)


@patch("repomatic.tooling.runner.subprocess.run")
@patch("repomatic.tooling.runner.is_github_ci", return_value=False)
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


@patch("repomatic.tooling.runner.subprocess.run")
@patch("repomatic.tooling.binaries.install_binary")
@patch("repomatic.tooling.runner.is_github_ci", return_value=False)
def test_run_tool_creates_output_parent_directory(
    mock_ci,
    mock_install_binary,
    mock_run,
    tmp_path,
    monkeypatch,
):
    """run_tool creates parent directories for --output file paths."""
    monkeypatch.chdir(tmp_path)
    bin_path = tmp_path / "lychee"
    bin_path.touch()
    mock_install_binary.return_value = bin_path
    mock_run.return_value = MagicMock(returncode=0)

    output_path = tmp_path / "subdir" / "nested" / "out.md"
    run_tool("lychee", extra_args=("--output", str(output_path), "readme.md"))

    assert output_path.parent.is_dir()


@patch("repomatic.tooling.runner.subprocess.run")
@patch("repomatic.tooling.binaries.install_binary")
@patch("repomatic.tooling.runner.is_github_ci", return_value=False)
def test_run_tool_creates_output_parent_equals_form(
    mock_ci,
    mock_install_binary,
    mock_run,
    tmp_path,
    monkeypatch,
):
    """The `--output=path` spelling gets its parent directory created too."""
    monkeypatch.chdir(tmp_path)
    bin_path = tmp_path / "lychee"
    bin_path.touch()
    mock_install_binary.return_value = bin_path
    mock_run.return_value = MagicMock(returncode=0)

    output_path = tmp_path / "scratch" / "report.md"
    run_tool("lychee", extra_args=(f"--output={output_path}", "readme.md"))

    assert output_path.parent.is_dir()


@patch("repomatic.tooling.runner.subprocess.run")
@patch("repomatic.tooling.binaries.install_binary")
@patch("repomatic.tooling.runner.is_github_ci", return_value=False)
def test_run_tool_output_existing_directory_is_noop(
    mock_ci,
    mock_install_binary,
    mock_run,
    tmp_path,
    monkeypatch,
):
    """run_tool does not fail when --output parent directory already exists."""
    monkeypatch.chdir(tmp_path)
    bin_path = tmp_path / "lychee"
    bin_path.touch()
    mock_install_binary.return_value = bin_path
    mock_run.return_value = MagicMock(returncode=0)

    output_dir = tmp_path / "existing"
    output_dir.mkdir()
    run_tool("lychee", extra_args=("--output", str(output_dir / "out.md")))

    assert output_dir.is_dir()


@patch("repomatic.tooling.runner.subprocess.run")
@patch("repomatic.tooling.runner.is_github_ci", return_value=False)
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
# find_unmodified_configs
# ---------------------------------------------------------------------------


def test_find_unmodified_configs_exact_match(tmp_path, monkeypatch):
    """Native config file matching bundled default is flagged as unmodified."""
    monkeypatch.chdir(tmp_path)

    bundled_content = get_data_content("yamllint.yaml")

    (tmp_path / ".yamllint.yaml").write_text(bundled_content, encoding="UTF-8")

    result = find_unmodified_configs()
    paths = [p for _, p in result]
    assert ".yamllint.yaml" in paths


def test_find_unmodified_configs_trailing_whitespace(tmp_path, monkeypatch):
    """Trailing whitespace differences are normalized away."""
    monkeypatch.chdir(tmp_path)

    bundled_content = get_data_content("yamllint.yaml")

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
    bundled_content = get_data_content("yamllint.yaml")

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
        content = get_data_content(data_name)
        (tmp_path / native_name).write_text(content, encoding="UTF-8")

    result = find_unmodified_configs()
    tools = {t for t, _ in result}
    assert "yamllint" in tools
    assert "zizmor" in tools


def test_find_unmodified_configs_alternative_filename(tmp_path, monkeypatch):
    """Alternative native config filename (.yamllint.yml) is also checked."""
    monkeypatch.chdir(tmp_path)

    bundled_content = get_data_content("yamllint.yaml")

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


def test_fix_myst_directives_skips_a_file_that_is_not_text(tmp_path):
    """A file that does not decode as UTF-8 is not one mdformat wrote.

    `run_tool` hands over every file whose content changed below a directory
    argument, whoever wrote it: an editor that refreshes `.git/index` during
    the run is enough. The page listed after the photo proves the loop goes on.
    """
    photo = tmp_path / "tart.jpeg"
    photo.write_bytes(b"\xff\xd8\xff\xe0")
    page = tmp_path / "pear-tart.md"
    page.write_text(
        "```{figure} tart.jpeg\n---\nalt: A pear tart\n---\n```\n", encoding="UTF-8"
    )

    _fix_myst_directives([str(photo), str(page)])

    assert photo.read_bytes() == b"\xff\xd8\xff\xe0"
    assert page.read_text(encoding="UTF-8") == (
        "```{figure} tart.jpeg\n:alt: A pear tart\n```\n"
    )


def test_check_bypasses_post_process():
    """Only a post_process tool in check mode bypasses post-processing."""
    mdformat = TOOL_REGISTRY["mdformat"]
    assert mdformat.check_bypasses_post_process(("--check", "readme.md"))
    assert not mdformat.check_bypasses_post_process(("readme.md",))
    # Tools without a post_process callback are authoritative in check mode.
    ruff = TOOL_REGISTRY["ruff"]
    assert not ruff.check_bypasses_post_process(("format", "--check"))


@patch("repomatic.tooling.runner.subprocess.run")
@patch("repomatic.tooling.binaries.install_binary")
@patch("repomatic.tooling.runner.is_github_ci", return_value=False)
def test_run_tool_warns_when_check_bypasses_post_process(
    mock_ci,
    mock_install_binary,
    mock_run,
    tmp_path,
    monkeypatch,
    caplog,
):
    """mdformat --check warns that the post-process fixup is skipped."""
    monkeypatch.chdir(tmp_path)
    bin_path = tmp_path / "shfmt"
    bin_path.touch()
    mock_install_binary.return_value = bin_path
    mock_run.return_value = MagicMock(returncode=1)

    with caplog.at_level(logging.WARNING):
        run_tool("mdformat", extra_args=("--check", "readme.md"))

    assert "check mode" in caplog.text


@patch("repomatic.tooling.runner.subprocess.run")
@patch("repomatic.tooling.binaries.install_binary")
@patch("repomatic.tooling.runner.is_github_ci", return_value=False)
def test_run_tool_no_check_warning_in_write_mode(
    mock_ci,
    mock_install_binary,
    mock_run,
    tmp_path,
    monkeypatch,
    caplog,
):
    """mdformat in write mode (no check flag) emits no check-mode warning."""
    monkeypatch.chdir(tmp_path)
    bin_path = tmp_path / "shfmt"
    bin_path.touch()
    mock_install_binary.return_value = bin_path
    mock_run.return_value = MagicMock(returncode=0)

    with caplog.at_level(logging.WARNING):
        run_tool("mdformat", extra_args=("readme.md",))

    assert "check mode" not in caplog.text


@patch("repomatic.tooling.runner.subprocess.run")
@patch("repomatic.tooling.binaries.install_binary")
@patch("repomatic.tooling.runner.is_github_ci", return_value=False)
def test_run_tool_post_processes_files_below_a_directory(
    mock_ci,
    mock_install_binary,
    mock_run,
    tmp_path,
    monkeypatch,
):
    """mdformat pointed at a directory gets its fixup on each file it rewrote.

    A file the run left alone keeps its content. It stands for a file that the
    exclude list of the tool skipped, which the fixup must not rewrite either.
    """
    monkeypatch.chdir(tmp_path)
    bin_path = tmp_path / "shfmt"
    bin_path.touch()
    mock_install_binary.return_value = bin_path

    field_list = "```{figure} tart.jpeg\n:alt: A pear tart\n```\n"
    yaml_block = "```{figure} tart.jpeg\n---\nalt: A pear tart\n---\n```\n"
    recipes = tmp_path / "recipes"
    recipes.mkdir()
    rewritten = recipes / "pear-tart.md"
    rewritten.write_text("* Three pears\n\n" + field_list, encoding="UTF-8")
    skipped = recipes / "plum-jam.md"
    skipped.write_text("* Two plums\n\n" + yaml_block, encoding="UTF-8")

    def mdformat(cmd, **kwargs):
        """Rewrite one file the way `mdformat-myst` does, and skip the other."""
        rewritten.write_text("- Three pears\n\n" + yaml_block, encoding="UTF-8")
        return MagicMock(returncode=0)

    mock_run.side_effect = mdformat

    run_tool("mdformat", extra_args=("recipes",))

    # The new list marker proves the tool ran, and the options the fixup.
    assert rewritten.read_text(encoding="UTF-8") == "- Three pears\n\n" + field_list
    assert skipped.read_text(encoding="UTF-8") == "* Two plums\n\n" + yaml_block


# ---------------------------------------------------------------------------
# [tool.X] to CLI flags translation
# ---------------------------------------------------------------------------


def test_resolve_config_flags_format(tmp_path, monkeypatch):
    """A FLAGS-format spec turns `[tool.X]` into CLI args, not a file."""
    monkeypatch.chdir(tmp_path)
    _declare_tool_table(
        tmp_path, "nuitka", {"onefile": True, "include-package-data": ["foo", "bar"]}
    )
    spec = ToolSpec(name="nuitka", native_format=NativeFormat.FLAGS)
    config_args, cleanup = resolve_config(spec)
    assert config_args == [
        "--onefile",
        "--include-package-data=foo",
        "--include-package-data=bar",
    ]
    assert cleanup is None


def test_resolve_config_flags_format_empty(tmp_path, monkeypatch):
    """No `[tool.X]` section yields no flags and no cleanup file."""
    monkeypatch.chdir(tmp_path)
    spec = ToolSpec(name="nuitka", native_format=NativeFormat.FLAGS)
    assert resolve_config(spec) == ([], None)


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


@patch("repomatic.tooling.binaries.install_binary")
@patch("repomatic.tooling.runner.subprocess.run")
@patch("repomatic.tooling.runner.is_github_ci", return_value=False)
def test_run_tool_skips_a_tool_with_no_targets(
    mock_ci, mock_run, mock_install_binary, tmp_path, monkeypatch
):
    """An empty inventory means the tool is never spawned."""
    monkeypatch.chdir(tmp_path)

    assert run_tool("mdformat") == 0
    mock_run.assert_not_called()


@patch("repomatic.tooling.binaries.install_binary")
@patch("repomatic.tooling.runner.subprocess.run")
@patch("repomatic.tooling.runner.is_github_ci", return_value=False)
def test_run_tool_runs_once_per_file(
    mock_ci, mock_run, mock_install_binary, tmp_path, monkeypatch
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


@patch("repomatic.tooling.binaries.install_binary")
@patch("repomatic.tooling.runner.subprocess.run")
@patch("repomatic.tooling.runner.is_github_ci", return_value=False)
def test_run_tool_reports_the_first_failure_across_batches(
    mock_ci, mock_run, mock_install_binary, tmp_path, monkeypatch
):
    """Every target still runs, and no later success masks an earlier failure."""
    monkeypatch.chdir(tmp_path)
    mock_run.side_effect = [MagicMock(returncode=2), MagicMock(returncode=0)]
    (tmp_path / "harvest.md").write_text("# Mango\n", encoding="UTF-8")
    (tmp_path / "orchard.md").write_text("# Papaya\n", encoding="UTF-8")

    assert run_tool("mdformat") == 2
    assert mock_run.call_count == 2


@patch("repomatic.tooling.binaries.install_binary")
@patch("repomatic.tooling.runner.subprocess.run")
@patch("repomatic.tooling.runner.is_github_ci", return_value=False)
def test_run_tool_explicit_args_suppress_defaults(
    mock_ci, mock_run, mock_install_binary, tmp_path, monkeypatch
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


@patch("repomatic.tooling.binaries.install_binary")
@patch("repomatic.tooling.runner.subprocess.run")
@patch("repomatic.tooling.runner.is_github_ci", return_value=False)
def test_run_tool_reports_a_rewrite_status_that_rewrote_nothing(
    mock_ci, mock_run, mock_install_binary, tmp_path, monkeypatch, caplog
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


@patch("repomatic.tooling.binaries.install_binary")
@patch("repomatic.tooling.runner.subprocess.run")
@patch("repomatic.tooling.runner.is_github_ci", return_value=False)
def test_run_tool_reports_success_for_a_rewrite_it_verified(
    mock_ci, mock_run, mock_install_binary, tmp_path, monkeypatch
):
    """The same exit code reports success when a file did change.

    The rewrite is what the caller asked for, so no workflow step has to
    tolerate the code the formatter announces it with.
    """
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "pyproject.toml"
    target.write_text(MESSY_PYPROJECT, encoding="UTF-8")

    def reformat(*args, **kwargs):
        target.write_text('[project]\nname = "orchard"\n', encoding="UTF-8")
        return MagicMock(returncode=1)

    mock_run.side_effect = reformat

    assert run_tool("pyproject-fmt") == 0


@patch("repomatic.tooling.binaries.install_binary")
@patch("repomatic.tooling.runner.subprocess.run")
@patch("repomatic.tooling.runner.is_github_ci", return_value=False)
def test_run_tool_leaves_other_tools_exit_codes_alone(
    mock_ci, mock_run, mock_install_binary, tmp_path, monkeypatch
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


def test_digest_targets_stays_out_of_git_directories(tmp_path, monkeypatch):
    """A walk below a directory argument never enters a `.git` directory.

    No tool rewrites a file there, and another process can. An index that an
    editor refreshes during the run would read as a file the tool rewrote. A
    `.gitignore` file is not in that directory, and stays in the walk.
    """
    monkeypatch.chdir(tmp_path)
    for name, content in (
        (".git/index", "DIRC"),
        (".git/objects/pack", "PACK"),
        (".gitignore", "*.pyc\n"),
        ("orchard/.git/HEAD", "ref: refs/heads/main\n"),
        ("orchard/plum.md", "Two plums\n"),
        ("recipes/pear-tart.md", "Three pears\n"),
    ):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="UTF-8")

    assert set(_digest_targets(["."])) == {
        ".gitignore",
        str(Path("orchard", "plum.md")),
        str(Path("recipes", "pear-tart.md")),
    }


@patch("repomatic.tooling.runner.run_tool", return_value=TOOL_CRASH_EXIT_CODE)
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


@patch("repomatic.tooling.runner.run_tool", return_value=0)
def test_verify_via_write_path_accepts_the_working_directory(
    mock_run_tool, tmp_path, monkeypatch
):
    """A path resolving to the working directory verifies the default set.

    The scratch root is created inside the working directory, so mirroring `.`
    under it makes the scratch its own copy destination and `shutil.copytree`
    dies on it as an existing path. It reached users as a bare
    `FileExistsError: .repomatic-verify-<rand>`, which reads as a repository
    problem rather than a rejected argument.

    The collision happens while copying, before the tool runs, so a stubbed
    `run_tool` keeps the test off the network.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "harvest.md").write_text("# Mango\n\nRipe.\n", encoding="UTF-8")

    exit_code, drifted = verify_via_write_path("mdformat", extra_args=(".",))

    assert exit_code == 0
    assert drifted == []
    # The tool ran on a copy of the default set, not on `.` or on nothing.
    copies = [Path(arg) for arg in mock_run_tool.call_args.kwargs["extra_args"]]
    assert [copy.name for copy in copies] == ["harvest.md"]
    assert copies[0].parent.name.startswith(".repomatic-verify-")
    # The scratch directory is the thing that used to collide: nothing of it
    # may outlive the call, or the next one collides with the leftover.
    assert not list(tmp_path.glob(".repomatic-verify-*"))


@pytest.mark.parametrize(
    ("tool", "extra_args"),
    (
        # typos walks the tree itself, so it declares no default path to copy.
        ("typos", ()),
        ("mdformat", ("papaya.md",)),
    ),
)
@patch("repomatic.tooling.runner.run_tool", return_value=0)
def test_verify_via_write_path_fails_when_nothing_is_copied(
    mock_run_tool, tool, extra_args, tmp_path, monkeypatch
):
    """A verification that copied no path measured nothing, so it cannot pass.

    An exit code of `0` would read as a clean tree to any gate running it.
    """
    monkeypatch.chdir(tmp_path)

    exit_code, drifted = verify_via_write_path(tool, extra_args=extra_args)

    assert exit_code == 2
    assert drifted == []
    mock_run_tool.assert_not_called()
    assert not list(tmp_path.glob(".repomatic-verify-*"))


@patch("repomatic.tooling.runner.run_tool", return_value=0)
def test_verify_via_write_path_reports_a_path_it_cannot_copy(
    mock_run_tool, tmp_path, monkeypatch, caplog
):
    """A copy that fails is one logged error that names the path, not a traceback.

    Nothing was measured, so the answer is the `2` of a call that copied no
    path, and the scratch directory does not outlive the call.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "harvest.md").write_text("# Mango\n", encoding="UTF-8")

    with (
        patch(
            "repomatic.tooling.runner.shutil.copy2",
            side_effect=PermissionError(13, "Permission denied"),
        ),
        caplog.at_level(logging.ERROR),
    ):
        outcome = verify_via_write_path("mdformat", extra_args=("harvest.md",))

    assert outcome == (2, [])
    assert "cannot copy harvest.md" in caplog.text
    assert "Permission denied" in caplog.text
    mock_run_tool.assert_not_called()
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


@patch("repomatic.tooling.binaries.install_binary")
def test_run_reports_an_install_failure_as_an_error(
    mock_install_binary, tmp_path, monkeypatch
):
    """A tool that cannot be installed ends the run with a message, not a traceback."""
    monkeypatch.chdir(tmp_path)
    mock_install_binary.side_effect = BinaryInstallError(
        "SHA-256 mismatch for https://example.com/typos.tar.gz"
    )
    result = CliRunner().invoke(repomatic, ["run", "typos", "--", "pear.md"])
    assert result.exit_code == 1, result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "SHA-256 mismatch" in result.output


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        # The bare form scans the history, as the lint-secrets job does.
        pytest.param((), ["--verbose", "--redact", "git", "."], id="bare"),
        pytest.param(
            ("dir", "."), ["--verbose", "--redact", "dir", "."], id="caller-driven"
        ),
    ],
)
@patch("repomatic.tooling.runner.subprocess.run")
@patch("repomatic.tooling.binaries.install_binary")
@patch("repomatic.tooling.runner.is_github_ci", return_value=False)
def test_run_tool_gitleaks_scans_the_history_by_default(
    mock_ci, mock_install_binary, mock_run, tmp_path, monkeypatch, args, expected
):
    """A bare gitleaks run scans what CI scans, with no deprecated `detect`."""
    monkeypatch.chdir(tmp_path)
    mock_install_binary.return_value = tmp_path / "gitleaks"
    mock_run.return_value = MagicMock(returncode=0)

    run_tool("gitleaks", extra_args=args)

    cmd = mock_run.call_args[0][0]
    assert cmd[1:] == expected
    assert "detect" not in cmd


def test_run_offers_every_registered_tool():
    """The argument's choices are the registry, so neither can drift."""
    command = run_command
    param = next(p for p in command.params if p.name == "tool_name")
    assert isinstance(param.type, Choice)
    assert tuple(param.type.choices) == tuple(sorted(TOOL_REGISTRY))
