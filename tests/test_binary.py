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

import json
import struct
import sys
from pathlib import Path
from string import ascii_lowercase, digits
from unittest.mock import patch

import pytest
from extra_platforms import (
    AARCH64,
    ALL_ARCHITECTURES,
    ALL_IDS,
    LINUX,
    WINDOWS,
    X86_64,
)

TYPE_CHECKING = False
if TYPE_CHECKING:
    from extra_platforms import Architecture

from repomatic.binary import (
    BINARY_ASSET_SUFFIXES,
    MACHINE_IDS,
    NUITKA_BUILD_TARGETS,
    SKIP_BINARY_BUILD_BRANCHES,
    BinaryFormat,
    BuildTarget,
    _elf_info,
    _macho_info,
    _pe_machine,
    _version_key,
    pack_binary_assets,
    verify_binary_arch,
    verify_binary_floor,
)
from repomatic.git_ops import VERSION_BUMP_BRANCHES
from repomatic.github.actions import WorkflowEvent
from repomatic.metadata import Metadata
from tests.conftest import metadata_from_pyproject

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


def _macho_cpu_type(arch: Architecture) -> int:
    """The Mach-O `cputype` for an architecture, narrowed for the byte builders.

    {data}`~repomatic.binary.MACHINE_IDS` spans every format, so its values are
    `str | int`: a pyelftools machine name on ELF, a raw header integer on the
    other two. These two accessors carry that correspondence for the helpers
    below, which write real header bytes and need the integer.
    """
    value = MACHINE_IDS[BinaryFormat.MACHO, arch]
    assert isinstance(value, int)
    return value


def _pe_machine_id(arch: Architecture) -> int:
    """The PE COFF `Machine` value for an architecture, narrowed to `int`."""
    value = MACHINE_IDS[BinaryFormat.PE, arch]
    assert isinstance(value, int)
    return value


def _machines_for(binary_format: BinaryFormat) -> set[str | int]:
    """Every machine identifier recorded for one executable format."""
    return {
        machine
        for (candidate, _arch), machine in MACHINE_IDS.items()
        if candidate is binary_format
    }


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


def test_version_key_ordering():
    """Dotted versions compare numerically, not lexically."""
    assert _version_key("2.28") < _version_key("2.34")
    assert _version_key("2.4") < _version_key("2.38")
    assert _version_key("10.15") < _version_key("11.0")
    assert max(["2.4", "2.38", "2.17"], key=_version_key) == "2.38"


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


def test_unknown_target(tmp_path):
    """Unknown target raises ValueError."""
    binary = tmp_path / "test.bin"
    binary.touch()
    with pytest.raises(ValueError, match="Unknown target"):
        verify_binary_arch("unknown-platform", binary)


@pytest.mark.parametrize(
    ("target", "payload"),
    [
        ("linux-arm64", make_elf(EM_AARCH64)),
        ("linux-x64", make_elf(EM_X86_64)),
        ("macos-arm64", make_macho(_macho_cpu_type(AARCH64))),
        ("macos-x64", make_fat([make_macho(_macho_cpu_type(X86_64))])),
        ("windows-arm64", make_pe(_pe_machine_id(AARCH64))),
        ("windows-x64", make_pe(_pe_machine_id(X86_64))),
    ],
)
def test_matching_arch(tmp_path, target, payload):
    """Binary with matching architecture passes verification."""
    binary = tmp_path / "test.bin"
    binary.write_bytes(payload)
    # Should not raise.
    verify_binary_arch(target, binary)


@pytest.mark.parametrize(
    ("target", "payload"),
    [
        # Wrong architecture, right format.
        ("linux-arm64", make_elf(EM_X86_64)),
        ("macos-arm64", make_macho(_macho_cpu_type(X86_64))),
        ("windows-x64", make_pe(_pe_machine_id(AARCH64))),
        # Wrong executable format entirely.
        ("linux-x64", make_pe(_pe_machine_id(X86_64))),
        ("windows-x64", make_elf(EM_X86_64)),
        # Not an executable at all.
        ("linux-x64", b""),
    ],
)
def test_mismatched_arch(tmp_path, target, payload):
    """Binary with mismatched architecture or format raises ValueError."""
    binary = tmp_path / "test.bin"
    binary.write_bytes(payload)
    with pytest.raises(ValueError, match="Binary architecture mismatch"):
        verify_binary_arch(target, binary)


def test_elf_info_without_verneed(tmp_path):
    """A sectionless ELF reports its machine and no glibc requirement."""
    sample = tmp_path / "sample.bin"
    sample.write_bytes(make_elf(EM_X86_64))
    assert _elf_info(sample) == ("EM_X86_64", None)


def test_macho_info_fat_slices(tmp_path):
    """CPU types accumulate and the floor maxes across fat slices."""
    sample = tmp_path / "sample.bin"
    sample.write_bytes(
        make_fat([
            make_macho(_macho_cpu_type(AARCH64), "12.4"),
            make_macho(_macho_cpu_type(X86_64), "10.15"),
        ])
    )
    cpu_types, floor = _macho_info(sample)
    assert cpu_types == _machines_for(BinaryFormat.MACHO)
    assert floor == "12.4"


def test_pe_machine_parsing(tmp_path):
    """The COFF machine field is read from PE headers, None elsewhere."""
    sample = tmp_path / "sample.exe"
    sample.write_bytes(make_pe(_pe_machine_id(AARCH64)))
    assert _pe_machine(sample) == _pe_machine_id(AARCH64)
    sample.write_bytes(b"MZ not a real PE")
    assert _pe_machine(sample) is None


def test_running_interpreter_parses():
    """The parsers agree with the running interpreter's own executable."""
    executable = Path(sys.executable)
    if sys.platform.startswith("linux"):
        machine, floor = _elf_info(executable)
        assert machine in _machines_for(BinaryFormat.ELF)
        assert floor is None or _version_key(floor) >= (2,)
    elif sys.platform == "darwin":
        cpu_types, floor = _macho_info(executable)
        assert cpu_types & _machines_for(BinaryFormat.MACHO)
        assert floor is not None
    elif sys.platform == "win32":
        assert _pe_machine(executable) in _machines_for(BinaryFormat.PE)


def test_floor_within_bounds_macos(tmp_path):
    """Files at or below the declared macOS floor pass verification."""
    binary = tmp_path / "test.bin"
    binary.write_bytes(make_macho(_macho_cpu_type(AARCH64), "11.0"))
    dist = tmp_path / "app.dist"
    dist.mkdir()
    (dist / "lib.dylib").write_bytes(make_macho(_macho_cpu_type(AARCH64), "10.9"))
    # Should not raise.
    verify_binary_floor("macos-arm64", binary, [dist])


def test_floor_exceeded_macos(tmp_path):
    """A dist file above the declared macOS floor fails verification."""
    binary = tmp_path / "test.bin"
    binary.write_bytes(make_macho(_macho_cpu_type(AARCH64), "11.0"))
    dist = tmp_path / "app.dist"
    dist.mkdir()
    (dist / "lib.dylib").write_bytes(make_macho(_macho_cpu_type(AARCH64), "26.0"))
    with pytest.raises(ValueError, match="OS floor exceeded") as excinfo:
        verify_binary_floor("macos-arm64", binary, [dist])
    assert "26.0" in str(excinfo.value)
    assert "lib.dylib" in str(excinfo.value)


@pytest.mark.parametrize(
    ("measured", "passes"),
    [
        ("2.17", True),
        ("2.28", True),
        ("2.39", False),
    ],
)
def test_floor_linux(tmp_path, measured, passes):
    """Linux floors compare the measured glibc requirement to the declared floor."""
    binary = tmp_path / "test.bin"
    binary.write_bytes(make_elf(EM_X86_64))
    with patch("repomatic.binary._elf_info", return_value=("EM_X86_64", measured)):
        if passes:
            verify_binary_floor("linux-x64", binary)
        else:
            with pytest.raises(ValueError, match="OS floor exceeded"):
                verify_binary_floor("linux-x64", binary)


def test_floor_windows_is_documentation_only(tmp_path):
    """Windows targets have no enforceable floor and always pass."""
    binary = tmp_path / "test.exe"
    binary.write_bytes(make_pe(_pe_machine_id(X86_64)))
    # Should not raise, and must not even look at the headers.
    verify_binary_floor("windows-x64", binary)


def test_floor_unknown_target(tmp_path):
    """Unknown target raises ValueError."""
    binary = tmp_path / "test.bin"
    binary.touch()
    with pytest.raises(ValueError, match="Unknown target"):
        verify_binary_floor("unknown-platform", binary)


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
    assert _version_key(build_target.floor) >= (
        (2,) if build_target.binary_format is BinaryFormat.ELF else (10,)
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


@pytest.mark.parametrize(
    "workflow_file",
    [
        ".github/workflows/_release-engine.yaml",
        ".github/workflows/release.yaml",
    ],
)
def test_skip_binary_build_release_workflow_push(workflow_file):
    """A push that only touches a release-pipeline workflow must rebuild binaries.

    Those workflows define how binaries are compiled and self-tested, so any
    change there needs a full matrix revalidation. Regression test for the
    ``release.yaml`` split leaving ``_release-engine.yaml`` outside
    ``BINARY_AFFECTING_PATHS``, which let engine-only fixes skip the Nuitka
    matrix.
    """
    metadata = Metadata()
    # Prime the cached properties with a push event touching a single file.
    metadata.__dict__["event_type"] = WorkflowEvent.push
    metadata.__dict__["head_branch"] = "main"
    metadata.__dict__["head_commit_message"] = "Water the papaya trees"
    metadata.__dict__["changed_files"] = (workflow_file,)
    assert metadata.skip_binary_build is False


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
    metadata = metadata_from_pyproject(tmp_path, monkeypatch, pyproject_content)
    assert metadata.config.nuitka_enabled is False
    assert metadata.nuitka_matrix is None
