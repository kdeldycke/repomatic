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

"""Tests for `repomatic.release.binary`: the build roster and its asset names."""

from __future__ import annotations

import json
import struct
from string import ascii_lowercase, digits

import pytest
from extra_platforms import (
    AARCH64,
    ALL_ARCHITECTURES,
    ALL_IDS,
    FREEBSD,
    LINUX,
    MACOS,
    UBUNTU,
    UNKNOWN_PLATFORM,
    WINDOWS,
    X86_64,
)
from packaging.version import Version

from repomatic.metadata.core import Metadata
from repomatic.release.binary import (
    BINARY_ASSET_SUFFIXES,
    MACHINE_IDS,
    NUITKA_BUILD_TARGETS,
    NUITKA_DISABLE_CCACHE_FLAG,
    BinaryFormat,
    BuildTarget,
    current_build_target,
    pack_binary_assets,
)
from tests.conftest import metadata_from_pyproject

TYPE_CHECKING = False
if TYPE_CHECKING:
    from extra_platforms import Architecture

EM_AARCH64 = 183
EM_X86_64 = 62


def make_elf(machine: int) -> bytes:
    """Craft a minimal 64-bit little-endian ELF header with no sections."""
    ident = b"\x7fELF" + bytes([2, 1, 1, 0]) + bytes(8)
    return ident + struct.pack(
        "<HHIQQQIHHHHHH", 2, machine, 1, 0, 0, 0, 0, 64, 56, 0, 64, 0, 0
    )


def pack_macho_version(version: str) -> int:
    """Pack a `major.minor` string into a Mach-O version integer."""
    major, _, minor = version.partition(".")
    return (int(major) << 16) | (int(minor or 0) << 8)


def make_macho(cpu_type: int, min_os: str = "11.0") -> bytes:
    """Craft a minimal 64-bit Mach-O header with one LC_BUILD_VERSION command."""
    build_version = struct.pack(
        "<IIIIII", 0x32, 24, 1, pack_macho_version(min_os), 0, 0
    )
    header = struct.pack(
        "<IIIIIIII", 0xFEEDFACF, cpu_type, 0, 2, 1, len(build_version), 0, 0
    )
    return header + build_version


def make_fat(slices: list[bytes]) -> bytes:
    """Wrap Mach-O slices into a universal (fat) container."""
    header = struct.pack(">II", 0xCAFEBABE, len(slices))
    offset = 8 + 20 * len(slices)
    entries = b""
    for chunk in slices:
        entries += struct.pack(">iiIII", 0, 0, offset, len(chunk), 0)
        offset += len(chunk)
    return header + entries + b"".join(slices)


def make_pe(machine: int) -> bytes:
    """Craft a minimal PE header exposing only the COFF machine type."""
    dos_header = b"MZ" + bytes(58) + struct.pack("<I", 64)
    return dos_header + b"PE\0\0" + struct.pack("<H", machine) + bytes(18)


def macho_cpu_type(arch: Architecture) -> int:
    """The Mach-O `cputype` for an architecture, narrowed for the byte builders.

    {data}`~repomatic.release.binary.MACHINE_IDS` spans every format, so its values are
    `str | int`: a pyelftools machine name on ELF, a raw header integer on the
    other two. These two accessors carry that correspondence for the helpers
    below, which write real header bytes and need the integer.
    """
    value = MACHINE_IDS[BinaryFormat.MACHO, arch]
    assert isinstance(value, int)
    return value


def pe_machine_id(arch: Architecture) -> int:
    """The PE COFF `Machine` value for an architecture, narrowed to `int`."""
    value = MACHINE_IDS[BinaryFormat.PE, arch]
    assert isinstance(value, int)
    return value


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
    assert target in NUITKA_BUILD_TARGETS


def test_pack_binary_assets(tmp_path):
    """Aliases are byte-identical copies; Python distributions are skipped."""
    (tmp_path / "repomatic-1.2.3-linux-arm64.bin").write_bytes(b"elf-bytes")
    (tmp_path / "repomatic-1.2.3-windows-x64.exe").write_bytes(b"pe-bytes")
    (tmp_path / "repomatic-manpages.attestation.json").write_bytes(b"{}")
    (tmp_path / "repomatic-1.2.3.tar.gz").write_bytes(b"sdist")
    (tmp_path / "repomatic-1.2.3-py3-none-any.whl").write_bytes(b"wheel")

    uploads = pack_binary_assets(tmp_path, "1.2.3")

    assert [path.name for path in uploads] == [
        "repomatic-1.2.3-linux-arm64.bin",
        "repomatic-1.2.3-windows-x64.exe",
        "repomatic-linux-arm64.bin",
        "repomatic-manpages.attestation.json",
        "repomatic-windows-x64.exe",
    ]
    assert (tmp_path / "repomatic-linux-arm64.bin").read_bytes() == b"elf-bytes"
    assert (tmp_path / "repomatic-windows-x64.exe").read_bytes() == b"pe-bytes"
    # Idempotent: a second run returns the same list with the same bytes.
    assert pack_binary_assets(tmp_path, "1.2.3") == uploads


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (make_elf(EM_X86_64), BinaryFormat.ELF),
        (make_macho(0x0100000C), BinaryFormat.MACHO),
        (make_fat([make_macho(0x01000007)]), BinaryFormat.MACHO),
        (make_pe(0x8664), BinaryFormat.PE),
        (b"plain text", None),
        (b"", None),
    ],
)
def test_binary_format_detection(tmp_path, payload, expected):
    """Executable formats are recognized from their magic bytes."""
    sample = tmp_path / "sample.bin"
    sample.write_bytes(payload)
    assert BinaryFormat.detect(sample) is expected


ASSET_ARCH_LABELS = {AARCH64: "arm64", X86_64: "x64"}
"""Short architecture spellings frozen into the published release-asset names.

`BuildTarget.arch` carries the canonical extra-platforms id (`aarch64`,
`x86_64`), while every published download URL says `arm64` or `x64`. This maps
one onto the other, so a target key and its fields cannot drift apart.
"""


@pytest.mark.parametrize(
    ("target_id", "build_target"), sorted(NUITKA_BUILD_TARGETS.items())
)
def test_nuitka_targets(target_id: str, build_target: BuildTarget) -> None:
    assert isinstance(target_id, str)
    assert build_target.id == target_id

    assert set(build_target.runner).issubset(ascii_lowercase + digits + "-.")
    assert build_target.platform.id in ALL_IDS
    assert build_target.arch in ALL_ARCHITECTURES
    # Keyed on the platform, not on the format the extension is derived from,
    # so a wrong `BinaryFormat.extension` cannot satisfy both sides.
    assert build_target.extension == (
        "exe" if build_target.platform is WINDOWS else "bin"
    )
    assert f".{build_target.extension}" in BINARY_ASSET_SUFFIXES

    # Every target declares a floor; only Linux compiles in a container, and
    # what the version counts differs by format (a glibc symbol version below
    # 3, an OS release above 10).
    assert Version(build_target.floor) >= Version(
        "2" if build_target.binary_format is BinaryFormat.ELF else "10"
    )
    if build_target.platform is LINUX:
        assert build_target.container is not None
        assert build_target.container.startswith("quay.io/pypa/manylinux_2_28_")
        assert "@sha256:" in build_target.container
    else:
        assert build_target.container is None

    # The key is the published asset identifier, frozen on the short
    # architecture spelling rather than the canonical id the field carries.
    label = ASSET_ARCH_LABELS[build_target.arch]
    assert target_id == f"{build_target.platform.id}-{label}"
    assert set(target_id).issubset(ascii_lowercase + digits + "-")


@pytest.mark.parametrize(
    ("target_id", "build_target"), sorted(NUITKA_BUILD_TARGETS.items())
)
def test_matrix_entry_is_json_safe(target_id: str, build_target: BuildTarget) -> None:
    """No extra-platforms trait may leak into the GitHub matrix.

    `build_targets` and `nuitka_matrix` are serialized into the metadata JSON
    the release workflow reads, so an `Architecture` object reaching one of
    those dicts breaks the whole release lane at metadata time.
    """
    entry = build_target.as_matrix_entry()
    assert json.loads(json.dumps(entry)) == entry
    for key, value in entry.items():
        assert isinstance(key, str)
        assert isinstance(value, str)

    assert entry["target"] == target_id
    assert entry["platform_id"] == build_target.platform.id
    assert entry["arch"] == build_target.arch.id

    assert entry["os"] == build_target.runner
    assert entry["floor"] == build_target.floor

    expected_keys = {"target", "os", "platform_id", "arch", "extension", "floor"}
    if build_target.platform is LINUX:
        expected_keys.add("container")
    assert set(entry) == expected_keys, f"Unexpected matrix keys for {target_id}"


@pytest.mark.parametrize(
    ("target_id", "build_target"), sorted(NUITKA_BUILD_TARGETS.items())
)
def test_enforced_floor_follows_binary_format(
    target_id: str, build_target: BuildTarget
) -> None:
    """Only ELF and Mach-O record a floor a scan can measure.

    A PE carries a nominal version header, so its declared floor stays
    documentation and `verify_binary_floor` has nothing to enforce. The
    format's `floor_label` is what says which of the two a target is in.
    """
    if build_target.binary_format is BinaryFormat.PE:
        assert build_target.binary_format.floor_label is None
        assert build_target.enforced_floor is None
    else:
        assert build_target.binary_format.floor_label
        assert build_target.enforced_floor == build_target.floor


@pytest.mark.parametrize(
    ("target_id", "build_target"), sorted(NUITKA_BUILD_TARGETS.items())
)
def test_macos_compiles_without_ccache_at_its_floor(
    target_id: str, build_target: BuildTarget
) -> None:
    """macOS turns ccache off, keeps no cache, and pins its deployment target.

    Every other target adds no option and no variable, and keeps its cache.
    """
    if build_target.platform is MACOS:
        assert build_target.nuitka_flags == (NUITKA_DISABLE_CCACHE_FLAG,)
        assert build_target.compile_cache is False
        assert build_target.build_env == {
            "MACOSX_DEPLOYMENT_TARGET": build_target.floor
        }
    else:
        assert build_target.nuitka_flags == ()
        assert build_target.compile_cache is True
        assert build_target.build_env == {}


@pytest.mark.parametrize(
    ("platform", "architecture", "expected"),
    (
        pytest.param(MACOS, AARCH64, "macos-arm64", id="macos-arm"),
        pytest.param(MACOS, X86_64, "macos-x64", id="macos-intel"),
        pytest.param(WINDOWS, X86_64, "windows-x64", id="windows"),
        pytest.param(UBUNTU, AARCH64, "linux-arm64", id="linux-distribution"),
        pytest.param(UNKNOWN_PLATFORM, X86_64, "linux-x64", id="unnamed-linux"),
        pytest.param(FREEBSD, X86_64, None, id="no-target"),
    ),
)
def test_current_build_target(monkeypatch, platform, architecture, expected):
    """The machine's platform and architecture pick its target."""
    monkeypatch.setattr("repomatic.release.binary.current_platform", lambda: platform)
    monkeypatch.setattr(
        "repomatic.release.binary.current_architecture", lambda: architecture
    )
    target = current_build_target()
    assert (target.id if target else None) == expected


def test_nuitka_enabled_default():
    """Test that nuitka.enabled config defaults to True."""
    metadata = Metadata()
    assert metadata.config.nuitka_enabled is True


def test_nuitka_disabled_skips_matrix(tmp_path, monkeypatch):
    """`nuitka_matrix` is `None` when nuitka is disabled in `pyproject.toml`."""
    pyproject_content = """\
[project]
name = "test-project"
version = "1.0.0"

[project.scripts]
my-cli = "my_package.__main__:main"

[tool.repomatic]
nuitka.enabled = false
"""
    metadata = metadata_from_pyproject(tmp_path, monkeypatch, pyproject_content)
    assert metadata.config.nuitka_enabled is False
    assert metadata.nuitka_matrix is None
