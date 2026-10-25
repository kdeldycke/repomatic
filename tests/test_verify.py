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

"""Tests for `repomatic.release.verify`: native header verification."""

from __future__ import annotations

import struct
import sys
from pathlib import Path

import pytest
from click_extra.testing import CliRunner
from extra_platforms import AARCH64, X86_64
from packaging.version import Version

from repomatic.cli.main import repomatic
from repomatic.release.binary import MACHINE_IDS, NUITKA_BUILD_TARGETS, BinaryFormat
from repomatic.release.verify import (
    _elf_info,
    _macho_info,
    _pe_machine,
    verify_binary_arch,
    verify_binary_floor,
)
from tests.conftest import (
    EM_AARCH64,
    EM_X86_64,
    macho_cpu_type,
    make_elf,
    make_fat,
    make_macho,
    make_pe,
    pe_machine_id,
)


def _machines_for(binary_format: BinaryFormat) -> set[int]:
    """Every machine identifier recorded for one executable format."""
    return {
        machine
        for (candidate, _arch), machine in MACHINE_IDS.items()
        if candidate is binary_format
    }


@pytest.mark.parametrize(
    ("target", "payload"),
    [
        ("linux-arm64", make_elf(EM_AARCH64)),
        ("linux-x64", make_elf(EM_X86_64)),
        ("macos-arm64", make_macho(macho_cpu_type(AARCH64))),
        ("macos-x64", make_fat([make_macho(macho_cpu_type(X86_64))])),
        ("windows-arm64", make_pe(pe_machine_id(AARCH64))),
        ("windows-x64", make_pe(pe_machine_id(X86_64))),
    ],
)
def test_matching_arch(tmp_path, target, payload):
    """Binary with matching architecture passes verification."""
    binary = tmp_path / "test.bin"
    binary.write_bytes(payload)
    # Should not raise.
    verify_binary_arch(NUITKA_BUILD_TARGETS[target], binary)


@pytest.mark.parametrize(
    ("target", "payload"),
    [
        # Wrong architecture, right format.
        ("linux-arm64", make_elf(EM_X86_64)),
        ("macos-arm64", make_macho(macho_cpu_type(X86_64))),
        ("windows-x64", make_pe(pe_machine_id(AARCH64))),
        # Wrong executable format entirely.
        ("linux-x64", make_pe(pe_machine_id(X86_64))),
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
        verify_binary_arch(NUITKA_BUILD_TARGETS[target], binary)


def test_verify_binary_rejects_an_unknown_target(tmp_path):
    """The command refuses a target outside the build roster, before any read."""
    binary = tmp_path / "test.bin"
    binary.touch()
    result = CliRunner().invoke(
        repomatic,
        ["verify-binary", "--target", "unknown-platform", "--binary", str(binary)],
    )
    assert result.exit_code == 2, result.output
    assert "unknown-platform" in result.output


def test_verify_binary_passes_a_matching_binary(tmp_path):
    """A binary built for its target passes the architecture and OS floor checks."""
    binary = tmp_path / "test.exe"
    binary.write_bytes(make_pe(pe_machine_id(X86_64)))
    result = CliRunner().invoke(
        repomatic,
        ["verify-binary", "--target", "windows-x64", "--binary", str(binary)],
    )
    assert result.exit_code == 0, result.output
    assert "Binary architecture verified for windows-x64" in result.output
    assert "OS floor verified for windows-x64." in result.output


def test_elf_info_without_verneed(tmp_path):
    """A sectionless ELF reports its machine and no glibc requirement."""
    sample = tmp_path / "sample.bin"
    sample.write_bytes(make_elf(EM_X86_64))
    assert _elf_info(sample) == (EM_X86_64, None)


def test_elf_info_skips_glibc_feature_versions(tmp_path):
    """A feature entry like `GLIBC_ABI_DT_RELR` sets no glibc floor.

    Files linked with packed relocations require it, like the Python
    interpreter Arch Linux ships.
    """
    sample = tmp_path / "sample.bin"
    versions = ("GLIBC_2.34", "GLIBC_ABI_DT_RELR", "GLIBC_2.38")
    sample.write_bytes(make_elf(EM_X86_64, versions))
    assert _elf_info(sample) == (EM_X86_64, "2.38")


def test_elf_info_orders_glibc_versions_numerically(tmp_path):
    """`2.4` is older than `2.38`, which a comparison of strings gets wrong."""
    sample = tmp_path / "sample.bin"
    sample.write_bytes(make_elf(EM_X86_64, ("GLIBC_2.4", "GLIBC_2.38", "GLIBC_2.17")))
    assert _elf_info(sample) == (EM_X86_64, "2.38")


@pytest.mark.parametrize(
    ("wide", "big_endian"),
    [(True, True), (False, False), (False, True)],
    ids=["elf64-be", "elf32-le", "elf32-be"],
)
def test_elf_info_reads_both_classes_and_byte_orders(tmp_path, wide, big_endian):
    """The parser follows the class and byte order the identification declares."""
    sample = tmp_path / "sample.bin"
    sample.write_bytes(
        make_elf(EM_AARCH64, ("GLIBC_2.17",), wide=wide, big_endian=big_endian)
    )
    assert _elf_info(sample) == (EM_AARCH64, "2.17")


@pytest.mark.parametrize(
    ("wide", "big_endian"),
    [(True, False), (True, True), (False, False), (False, True)],
    ids=["elf64-le", "elf64-be", "elf32-le", "elf32-be"],
)
def test_elf_info_reads_extended_section_numbering(tmp_path, wide, big_endian):
    """A count held in the `sh_size` of entry 0 still reads every section.

    Past `0xff00` sections, `e_shnum` is `0` and the count moves there, as the
    ELF specification has it. Read as a file with no section, the glibc floor
    would vanish and the floor check pass any binary.
    """
    sample = tmp_path / "sample.bin"
    sample.write_bytes(
        make_elf(
            EM_X86_64,
            ("GLIBC_2.28",),
            wide=wide,
            big_endian=big_endian,
            extended_numbering=True,
        )
    )
    assert _elf_info(sample) == (EM_X86_64, "2.28")


def test_elf_info_reads_a_section_count_past_the_file_as_what_it_holds(tmp_path):
    """A count larger than the file reads the sections the file holds.

    The read is sized by the file, never by a count a corrupt `sh_size` can
    put in the billions.
    """
    content = bytearray(make_elf(EM_X86_64, ("GLIBC_2.17",), extended_numbering=True))
    shoff = struct.unpack_from("<Q", content, 40)[0]
    # The `sh_size` of entry 0, 32 bytes into an ELF64 section header.
    struct.pack_into("<Q", content, shoff + 32, 2**62)
    sample = tmp_path / "sample.bin"
    sample.write_bytes(bytes(content))
    assert _elf_info(sample) == (EM_X86_64, "2.17")


def test_elf_info_rejects_a_truncated_header(tmp_path):
    """A file cut inside its ELF header is reported, not read as sectionless."""
    sample = tmp_path / "sample.bin"
    sample.write_bytes(make_elf(EM_X86_64)[:40])
    with pytest.raises(ValueError, match="Truncated ELF header"):
        _elf_info(sample)


def test_macho_info_fat_slices(tmp_path):
    """CPU types accumulate and the floor maxes across fat slices."""
    sample = tmp_path / "sample.bin"
    sample.write_bytes(
        make_fat([
            make_macho(macho_cpu_type(AARCH64), "12.4"),
            make_macho(macho_cpu_type(X86_64), "10.15"),
        ])
    )
    cpu_types, floor = _macho_info(sample)
    assert cpu_types == _machines_for(BinaryFormat.MACHO)
    assert floor == "12.4"


def test_pe_machine_parsing(tmp_path):
    """The COFF machine field is read from PE headers, None elsewhere."""
    sample = tmp_path / "sample.exe"
    sample.write_bytes(make_pe(pe_machine_id(AARCH64)))
    assert _pe_machine(sample) == pe_machine_id(AARCH64)
    sample.write_bytes(b"MZ not a real PE")
    assert _pe_machine(sample) is None


def test_running_interpreter_parses():
    """The parsers agree with the running interpreter's own executable."""
    executable = Path(sys.executable)
    if sys.platform.startswith("linux"):
        machine, floor = _elf_info(executable)
        assert machine in _machines_for(BinaryFormat.ELF)
        assert floor is None or Version(floor) >= Version("2")
    elif sys.platform == "darwin":
        cpu_types, floor = _macho_info(executable)
        assert cpu_types & _machines_for(BinaryFormat.MACHO)
        assert floor is not None
    elif sys.platform == "win32":
        assert _pe_machine(executable) in _machines_for(BinaryFormat.PE)


def test_floor_within_bounds_macos(tmp_path):
    """Files at or below the declared macOS floor pass verification."""
    binary = tmp_path / "test.bin"
    binary.write_bytes(make_macho(macho_cpu_type(AARCH64), "11.0"))
    dist = tmp_path / "app.dist"
    dist.mkdir()
    (dist / "lib.dylib").write_bytes(make_macho(macho_cpu_type(AARCH64), "10.9"))
    # Should not raise.
    verify_binary_floor(NUITKA_BUILD_TARGETS["macos-arm64"], binary, [dist])


def test_floor_exceeded_macos(tmp_path):
    """A dist file above the declared macOS floor fails verification."""
    binary = tmp_path / "test.bin"
    binary.write_bytes(make_macho(macho_cpu_type(AARCH64), "11.0"))
    dist = tmp_path / "app.dist"
    dist.mkdir()
    (dist / "lib.dylib").write_bytes(make_macho(macho_cpu_type(AARCH64), "26.0"))
    with pytest.raises(ValueError, match="OS floor exceeded") as excinfo:
        verify_binary_floor(NUITKA_BUILD_TARGETS["macos-arm64"], binary, [dist])
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
    binary.write_bytes(make_elf(EM_X86_64, (f"GLIBC_{measured}",)))
    target = NUITKA_BUILD_TARGETS["linux-x64"]
    if passes:
        verify_binary_floor(target, binary)
    else:
        with pytest.raises(ValueError, match="OS floor exceeded"):
            verify_binary_floor(target, binary)


def test_floor_windows_is_documentation_only(tmp_path):
    """Windows targets have no enforceable floor and always pass."""
    binary = tmp_path / "test.exe"
    binary.write_bytes(make_pe(pe_machine_id(X86_64)))
    # Should not raise, and must not even look at the headers.
    verify_binary_floor(NUITKA_BUILD_TARGETS["windows-x64"], binary)
