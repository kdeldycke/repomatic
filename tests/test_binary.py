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

"""Tests for binary verification."""

from __future__ import annotations

from string import ascii_lowercase, digits
from unittest.mock import patch

import pytest
from extra_platforms import ALL_IDS

from repomatic.binary import (
    BINARY_ARCH_MAPPINGS,
    NUITKA_BUILD_TARGETS,
    SKIP_BINARY_BUILD_BRANCHES,
    VERSION_BUMP_BRANCHES,
    run_exiftool,
    verify_binary_arch,
)
from repomatic.metadata import Metadata


@pytest.mark.parametrize(
    "target",
    [
        "linux-arm64",
        "linux-x64",
        "macos-arm64",
        "macos-x64",
        "windows-arm64",
        "windows-x64",
    ],
)
def test_all_targets_present(target):
    """All expected targets are in the mapping."""
    assert target in BINARY_ARCH_MAPPINGS


@pytest.mark.parametrize(
    ("target", "expected_field", "expected_substring"),
    [
        ("linux-arm64", "CPUType", "Arm 64-bits"),
        ("linux-x64", "CPUType", "AMD x86-64"),
        ("macos-arm64", "CPUType", "ARM 64-bit"),
        ("macos-x64", "CPUType", "x86 64-bit"),
        ("windows-arm64", "MachineType", "ARM64"),
        ("windows-x64", "MachineType", "AMD64"),
    ],
)
def test_mapping_values(target, expected_field, expected_substring):
    """Each target maps to correct field and substring."""
    field, substring = BINARY_ARCH_MAPPINGS[target]
    assert field == expected_field
    assert substring == expected_substring


def test_run_exiftool_missing_command(tmp_path):
    """run_exiftool raises FileNotFoundError when exiftool is not on PATH."""
    binary = tmp_path / "test.bin"
    binary.touch()
    with (
        patch("repomatic.binary.shutil.which", return_value=None),
        pytest.raises(FileNotFoundError, match="not found on PATH"),
    ):
        run_exiftool(binary)


def test_unknown_target(tmp_path):
    """Unknown target raises ValueError."""
    binary = tmp_path / "test.bin"
    binary.touch()
    with pytest.raises(ValueError, match="Unknown target"):
        verify_binary_arch("unknown-platform", binary)


@pytest.mark.parametrize(
    ("target", "field", "value"),
    [
        ("linux-arm64", "CPUType", "Arm 64-bits (Armv8/AArch64)"),
        ("linux-x64", "CPUType", "AMD x86-64"),
        ("macos-arm64", "CPUType", "ARM 64-bit"),
        ("macos-x64", "CPUType", "x86 64-bit"),
        ("windows-arm64", "MachineType", "ARM64 little endian"),
        ("windows-x64", "MachineType", "AMD AMD64"),
    ],
)
def test_matching_arch(tmp_path, target, field, value):
    """Binary with matching architecture passes verification."""
    binary = tmp_path / "test.bin"
    binary.touch()

    mock_output = [{field: value}]
    with patch("repomatic.binary.run_exiftool", return_value=mock_output[0]):
        # Should not raise.
        verify_binary_arch(target, binary)


@pytest.mark.parametrize(
    ("target", "field", "wrong_value"),
    [
        ("linux-arm64", "CPUType", "AMD x86-64"),
        ("linux-x64", "CPUType", "Arm 64-bits"),
        ("macos-arm64", "CPUType", "x86 64-bit"),
        ("windows-x64", "MachineType", "ARM64"),
    ],
)
def test_mismatched_arch(tmp_path, target, field, wrong_value):
    """Binary with mismatched architecture raises AssertionError."""
    binary = tmp_path / "test.bin"
    binary.touch()

    mock_output = {field: wrong_value}
    with (
        patch("repomatic.binary.run_exiftool", return_value=mock_output),
        pytest.raises(AssertionError, match="Binary architecture mismatch"),
    ):
        verify_binary_arch(target, binary)


def test_missing_field(tmp_path):
    """Missing exiftool field raises AssertionError."""
    binary = tmp_path / "test.bin"
    binary.touch()

    # Return empty metadata.
    mock_output: dict[str, str] = {}
    with (
        patch("repomatic.binary.run_exiftool", return_value=mock_output),
        pytest.raises(AssertionError, match="Binary architecture mismatch"),
    ):
        verify_binary_arch("linux-arm64", binary)


@pytest.mark.parametrize("target_id, target_data", NUITKA_BUILD_TARGETS.items())
def test_nuitka_targets(target_id: str, target_data: dict[str, str]) -> None:
    assert isinstance(target_id, str)
    assert isinstance(target_data, dict)

    assert set(target_data) == {
        "os",
        "platform_id",
        "arch",
        "extension",
    }, f"Unexpected keys in target data for {target_id}"

    assert isinstance(target_data["os"], str)
    assert isinstance(target_data["platform_id"], str)
    assert isinstance(target_data["arch"], str)
    assert isinstance(target_data["extension"], str)

    assert set(target_data["os"]).issubset(ascii_lowercase + digits + "-.")
    assert target_data["platform_id"] in ALL_IDS
    assert target_data["arch"] in {"arm64", "x64"}
    assert set(target_data["extension"]).issubset(ascii_lowercase)

    assert target_id == target_data["platform_id"] + "-" + target_data["arch"]
    assert set(target_id).issubset(ascii_lowercase + digits + "-")


def test_skip_binary_build_branches_constant():
    """Test that SKIP_BINARY_BUILD_BRANCHES contains expected branch names."""
    assert isinstance(SKIP_BINARY_BUILD_BRANCHES, frozenset)
    # Verify the list contains expected branches for non-code changes.
    assert "sync-mailmap" in SKIP_BINARY_BUILD_BRANCHES
    assert "format-markdown" in SKIP_BINARY_BUILD_BRANCHES
    assert "format-images" in SKIP_BINARY_BUILD_BRANCHES
    assert "sync-gitignore" in SKIP_BINARY_BUILD_BRANCHES
    # Verify branches that affect code are NOT in the list.
    assert "format-python" not in SKIP_BINARY_BUILD_BRANCHES
    assert "main" not in SKIP_BINARY_BUILD_BRANCHES


def test_skip_binary_build_disjoint_from_version_bumps():
    """Version-bump branches rewrite the version string baked into the
    Nuitka binary, so they must not appear in SKIP_BINARY_BUILD_BRANCHES.
    """
    assert SKIP_BINARY_BUILD_BRANCHES.isdisjoint(VERSION_BUMP_BRANCHES)


def test_skip_binary_build_property_is_bool():
    """Test that skip_binary_build always returns a boolean.

    The actual value depends on CI context: in push events where only
    non-binary-affecting files changed, it is ``True``; otherwise ``False``.
    """
    metadata = Metadata()
    assert isinstance(metadata.skip_binary_build, bool)


def test_nuitka_enabled_default():
    """Test that nuitka.enabled config defaults to True."""
    metadata = Metadata()
    assert metadata.config.nuitka_enabled is True


def test_nuitka_disabled_skips_matrix(tmp_path, monkeypatch):
    """Test that nuitka_matrix returns None when nuitka is disabled in pyproject.toml."""
    pyproject_content = """\
[project]
name = "test-project"
version = "1.0.0"

[project.scripts]
my-cli = "my_package.__main__:main"

[tool.repomatic]
nuitka.enabled = false
"""
    pyproject_file = tmp_path / "pyproject.toml"
    pyproject_file.write_text(pyproject_content)

    # Override the pyproject path to point to our temporary file.
    monkeypatch.setattr(Metadata, "pyproject_path", pyproject_file)

    metadata = Metadata()
    assert metadata.config.nuitka_enabled is False
    assert metadata.nuitka_matrix is None
