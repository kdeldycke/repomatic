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

"""Tests for `repomatic.tooling.nuitka`: what `repomatic run nuitka` adds."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

import pytest

from repomatic.config import Config
from repomatic.release.binary import NUITKA_BUILD_TARGETS
from repomatic.tooling import nuitka
from repomatic.tooling.nuitka import (
    CCACHE_CONFIG,
    install_extras,
    nuitka_params,
    prepare_compile_cache,
    prepare_nuitka_build,
)
from repomatic.tooling.registry import TOOL_REGISTRY

TYPE_CHECKING = False
if TYPE_CHECKING:
    from repomatic.metadata.core import Metadata

MACOS_ARM = NUITKA_BUILD_TARGETS["macos-arm64"]
LINUX_ARM = NUITKA_BUILD_TARGETS["linux-arm64"]


def _metadata(**config: Any) -> Metadata:
    """A stand-in for `Metadata`, carrying a configuration and nothing else."""
    return cast("Metadata", SimpleNamespace(config=Config(**config)))


@pytest.fixture
def on_target(monkeypatch):
    """Pretend this machine compiles for the given build target."""

    def pretend(target):
        monkeypatch.setattr(nuitka, "current_build_target", lambda: target)

    return pretend


def test_registry_wires_the_hooks():
    """The `nuitka` entry runs both hooks of this module."""
    spec = TOOL_REGISTRY["nuitka"]
    assert spec.computed_params is nuitka_params
    assert spec.prepare is prepare_nuitka_build


@pytest.mark.parametrize(
    ("target", "expected"),
    (
        pytest.param(
            MACOS_ARM,
            ["--nofollow-import-to=tkinter", "--disable-cache=ccache"],
            id="macos",
        ),
        pytest.param(LINUX_ARM, ["--nofollow-import-to=tkinter"], id="linux"),
        pytest.param(None, ["--nofollow-import-to=tkinter"], id="no-target"),
    ),
)
def test_nuitka_params(on_target, target, expected):
    on_target(target)
    assert nuitka_params(_metadata()) == expected


def test_nuitka_params_follow_the_nofollow_setting(on_target):
    on_target(LINUX_ARM)
    metadata = _metadata(nuitka_nofollow_imports=["kiwi", "papaya"])
    assert nuitka_params(metadata) == [
        "--nofollow-import-to=kiwi",
        "--nofollow-import-to=papaya",
    ]


def test_install_extras_runs_one_inexact_sync():
    """Every extra rides one sync, which removes nothing the venv holds."""
    with patch.object(nuitka, "run_uv") as run_uv:
        install_extras(["sbom", "fruits"])
    run_uv.assert_called_once_with([
        "uv",
        "--no-progress",
        "sync",
        "--frozen",
        "--inexact",
        "--extra",
        "sbom",
        "--extra",
        "fruits",
    ])


def test_install_extras_without_extras_runs_nothing():
    with patch.object(nuitka, "run_uv") as run_uv:
        install_extras([])
    run_uv.assert_not_called()


def test_prepare_compile_cache_writes_the_ccache_settings(tmp_path):
    cache_dir = tmp_path / ".nuitka-cache"
    work_root = tmp_path / "work"
    prepare_compile_cache(cache_dir, cache_dir / "ccache", work_root)
    config = (cache_dir / "ccache" / CCACHE_CONFIG).read_text(encoding="UTF-8")
    assert config == f"compiler_check = content\nbase_dir = {work_root}\n"


def test_prepare_compile_cache_leaves_base_dir_unset_outside_a_runner(tmp_path):
    prepare_compile_cache(tmp_path / "cache", tmp_path / "cache" / "ccache", None)
    config = (tmp_path / "cache" / "ccache" / CCACHE_CONFIG).read_text(encoding="UTF-8")
    assert config == "compiler_check = content\n"


@pytest.mark.parametrize(("size", "kept"), ((100, True), (101, False)))
def test_prepare_compile_cache_resets_an_oversized_cache(
    tmp_path, monkeypatch, size, kept
):
    """A cache past the cap starts a fresh lineage, settings rewritten."""
    monkeypatch.setattr(nuitka, "COMPILE_CACHE_CAP", 100)
    cache_dir = tmp_path / ".nuitka-cache"
    (cache_dir / "clcache").mkdir(parents=True)
    (cache_dir / "clcache" / "object.o").write_bytes(b"\0" * size)

    prepare_compile_cache(cache_dir, cache_dir / "ccache", None)

    assert (cache_dir / "clcache" / "object.o").exists() is kept
    assert (cache_dir / "ccache" / CCACHE_CONFIG).is_file()


def test_prepare_nuitka_build_on_a_runner(tmp_path, monkeypatch, on_target):
    """A CI compile job gets its cache prepared and its target's environment."""
    on_target(MACOS_ARM)
    monkeypatch.setenv("NUITKA_CACHE_DIR", str(tmp_path / ".nuitka-cache"))
    monkeypatch.delenv("CCACHE_DIR", raising=False)
    monkeypatch.setenv("RUNNER_WORKSPACE", str(tmp_path / "work" / "papaya"))
    with patch.object(nuitka, "prepare_compile_cache") as prepare:
        env = prepare_nuitka_build(_metadata())
    # macOS keeps no compile cache.
    prepare.assert_not_called()
    assert env == {"MACOSX_DEPLOYMENT_TARGET": MACOS_ARM.floor}

    on_target(LINUX_ARM)
    monkeypatch.setenv("CCACHE_DIR", str(tmp_path / "ccache"))
    with patch.object(nuitka, "prepare_compile_cache") as prepare:
        env = prepare_nuitka_build(_metadata())
    prepare.assert_called_once_with(
        tmp_path / ".nuitka-cache", tmp_path / "ccache", tmp_path / "work"
    )
    assert env == {}


def test_prepare_nuitka_build_leaves_a_local_cache_alone(monkeypatch, on_target):
    """Outside CI, no cache was relocated, so none is touched."""
    on_target(LINUX_ARM)
    monkeypatch.delenv("NUITKA_CACHE_DIR", raising=False)
    with (
        patch.object(nuitka, "prepare_compile_cache") as prepare,
        patch.object(nuitka, "install_extras") as install,
    ):
        prepare_nuitka_build(_metadata(nuitka_extras=["sbom"]))
    prepare.assert_not_called()
    install.assert_called_once_with(["sbom"])
