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

"""Native header verification of compiled binaries.

The architecture and the minimum-OS floor of a binary are parsed straight from
its ELF, Mach-O and PE headers, so no external tool is needed on runners or
inside build containers.

Only `verify-binary` reads headers. This module is kept apart from the build
roster in {mod}`repomatic.release.binary`, which `show-metadata` loads on every
workflow run.
"""

from __future__ import annotations

import io
import logging
import re
import struct
from itertools import chain
from typing import NamedTuple

from packaging.version import Version

from .binary import MACHO_FAT_MAGICS, MACHO_MAGIC_64, BinaryFormat

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator
    from pathlib import Path
    from typing import IO, Final

    from .binary import BuildTarget

LC_VERSION_MIN_MACOSX: Final[int] = 0x24
"""Mach-O load command carrying the minimum macOS version (pre-10.14 SDKs)."""

LC_BUILD_VERSION: Final[int] = 0x32
"""Mach-O load command carrying the platform and minimum OS (10.14+ SDKs)."""

MACHO_PLATFORM_MACOS: Final[int] = 1
"""`platform` field value naming macOS inside an `LC_BUILD_VERSION` command."""

SHT_GNU_VERNEED: Final[int] = 0x6FFFFFFE
"""Section type of `.gnu.version_r`, the table of symbol versions a file needs."""

_GLIBC_RELEASE_RE: Final[re.Pattern[str]] = re.compile(r"GLIBC_(\d+(?:\.\d+)*)")
"""A `.gnu.version_r` entry naming a glibc release, like `GLIBC_2.34`.

glibc also versions feature nodes, like the `GLIBC_ABI_DT_RELR` that every file
linked with packed relocations requires. They name no release, so they set no
floor."""

_PE_PROBE_BYTES: Final[int] = 65536
"""Upper bound read when probing for a PE header: enough to cover any
real-world DOS-stub offset to the COFF header without reading the whole
binary."""


class _SectionHeader(NamedTuple):
    """One entry of the ELF section header table, in field order."""

    name: int
    type: int
    flags: int
    addr: int
    offset: int
    size: int
    link: int
    info: int
    addralign: int
    entsize: int


def _elf_sections(
    stream: IO[bytes], order: str, wide: bool, shoff: int, shentsize: int, shnum: int
) -> list[_SectionHeader]:
    """The section header table of an ELF file, empty when it has none.

    A file with `SHN_LORESERVE` (`0xff00`) sections or more sets `e_shnum` to
    `0` and keeps the count in the `sh_size` of entry `0`, as the ELF
    specification has it. A table cut short yields the entries the file does
    hold, so a count larger than the file never sizes a read.

    :param stream: The open ELF file.
    :param order: The `struct` byte-order prefix, `<` or `>`.
    :param wide: `True` for ELF64, `False` for ELF32.
    :param shoff: File offset of the table, `0` when the file has none.
    :param shentsize: Byte length of one entry.
    :param shnum: Number of entries, from `e_shnum`.
    """
    if not shoff or not shentsize:
        return []
    layout = f"{order}IIQQQQIIQQ" if wide else f"{order}IIIIIIIIII"
    size = struct.calcsize(layout)
    end = stream.seek(0, io.SEEK_END)
    stream.seek(shoff)
    first = stream.read(size)
    if not shnum and len(first) == size:
        shnum = _SectionHeader(*struct.unpack(layout, first)).size
    # No entry starts past the end of the file: a count beyond it only grows
    # the read, since the filter below drops what the read cannot fill.
    shnum = min(shnum, (max(end - shoff, 0) + shentsize - 1) // shentsize)
    stream.seek(shoff)
    table = stream.read(shentsize * shnum)
    return [
        _SectionHeader(*struct.unpack_from(layout, table, index * shentsize))
        for index in range(shnum)
        if index * shentsize + size <= len(table)
    ]


def _verneed_names(
    data: bytes, strings: bytes, order: str, count: int
) -> Iterator[str]:
    """Yield the version names a `.gnu.version_r` section requires.

    *data* holds *count* `Elf_Verneed` entries, each chaining its `Elf_Vernaux`
    children by relative offsets; both are 16 bytes in either ELF class. The
    names live in *strings*, the table the section links to. Data cut short
    ends the walk.
    """
    entry = 0
    for _ in range(count):
        if entry + 16 > len(data):
            return
        _, aux_count, _, aux_offset, next_entry = struct.unpack_from(
            f"{order}HHIII", data, entry
        )
        aux = entry + aux_offset
        for _ in range(aux_count):
            if aux + 16 > len(data):
                return
            _, _, _, name_offset, next_aux = struct.unpack_from(
                f"{order}IHHII", data, aux
            )
            end = strings.find(b"\0", name_offset)
            name = strings[name_offset : end if end >= 0 else None]
            yield name.decode("ASCII", errors="replace")
            if not next_aux:
                break
            aux += next_aux
        if not next_entry:
            return
        entry += next_entry


def _elf_info(path: Path) -> tuple[int, str | None]:
    """Return the `e_machine` code and highest glibc requirement of an ELF file.

    The glibc requirement is the maximum `GLIBC_x.y` entry of the
    `.gnu.version_r` section: the version table the dynamic loader checks
    before letting the file run, and so the file's effective glibc floor.
    Feature entries like `GLIBC_ABI_DT_RELR` are skipped. Both ELF classes and
    both byte orders are read, as the identification bytes declare them.

    :raises ValueError: If the file ends inside its ELF header.
    """
    with path.open("rb") as stream:
        ident = stream.read(16)
        wide = ident[4:5] == b"\x02"
        order = ">" if ident[5:6] == b"\x02" else "<"
        layout = f"{order}HHIQQQIHHHHHH" if wide else f"{order}HHIIIIIHHHHHH"
        header = stream.read(struct.calcsize(layout))
        if len(ident) < 16 or len(header) < struct.calcsize(layout):
            raise ValueError(f"Truncated ELF header in {path}")
        fields = struct.unpack(layout, header)
        machine = int(fields[1])
        sections = _elf_sections(
            stream, order, wide, shoff=fields[5], shentsize=fields[10], shnum=fields[11]
        )
        versions: set[str] = set()
        for section in sections:
            if section.type != SHT_GNU_VERNEED or section.link >= len(sections):
                continue
            strtab = sections[section.link]
            stream.seek(strtab.offset)
            strings = stream.read(strtab.size)
            stream.seek(section.offset)
            data = stream.read(section.size)
            for name in _verneed_names(data, strings, order, section.info):
                release = _GLIBC_RELEASE_RE.fullmatch(name)
                if release:
                    versions.add(release.group(1))
    floor = max(versions, key=Version) if versions else None
    return machine, floor


def _decode_macho_version(value: int) -> str:
    """Decode a packed Mach-O version integer into a `major.minor` string."""
    return f"{value >> 16}.{(value >> 8) & 0xFF}"


def _macho_header_region(stream: IO[bytes], base: int, size: int | None) -> bytes:
    """Read one slice's header and load commands, never its whole body.

    The consumer parses nothing past the load commands, whose byte length the
    header itself declares (`sizeofcmds`, offset 20). Mirrors the bounded
    `_PE_PROBE_BYTES` read: a onefile macOS binary runs to tens of megabytes
    while its load commands run to kilobytes.

    :param stream: The open Mach-O file.
    :param base: Byte offset of the slice within the file.
    :param size: Byte length of the slice, capping the read; `None` for a thin
        file whose slice is the file itself.
    """
    stream.seek(base)
    header = stream.read(32)
    if len(header) < 32 or struct.unpack_from("<I", header, 0)[0] != MACHO_MAGIC_64:
        return header
    length = struct.unpack_from("<I", header, 20)[0]
    if size is not None:
        length = min(length, max(size - 32, 0))
    return header + stream.read(length)


def _macho_slices(stream: IO[bytes]) -> Iterator[bytes]:
    """Yield each slice's header-and-load-commands region (fat or thin)."""
    head = stream.read(8)
    if len(head) < 8:
        yield head
        return
    magic = struct.unpack_from(">I", head, 0)[0]
    if magic not in MACHO_FAT_MAGICS:
        yield _macho_header_region(stream, 0, None)
        return
    wide = magic == 0xCAFEBABF
    entry_format = ">iiQQII" if wide else ">iiIII"
    entry_size = struct.calcsize(entry_format)
    count = struct.unpack_from(">I", head, 4)[0]
    table = stream.read(count * entry_size)
    for index in range(count):
        fields = struct.unpack_from(entry_format, table, index * entry_size)
        offset, size = fields[2], fields[3]
        yield _macho_header_region(stream, offset, size)


def _macho_info(path: Path) -> tuple[set[int], str | None]:
    """Return the CPU types and highest macOS floor of a Mach-O file.

    The floor is the `minos` field of the `LC_BUILD_VERSION` load command
    (or the older `LC_VERSION_MIN_MACOSX`), maxed across the slices of a
    universal binary. It is what the loader compares to the running macOS
    before letting the file execute.
    """
    cpu_types: set[int] = set()
    floors: set[str] = set()
    with path.open("rb") as stream:
        chunks = list(_macho_slices(stream))
    for chunk in chunks:
        if len(chunk) < 32 or struct.unpack_from("<I", chunk, 0)[0] != MACHO_MAGIC_64:
            continue
        cpu_types.add(struct.unpack_from("<I", chunk, 4)[0])
        command_count = struct.unpack_from("<I", chunk, 16)[0]
        offset = 32
        for _ in range(command_count):
            command, command_size = struct.unpack_from("<II", chunk, offset)
            if command == LC_BUILD_VERSION:
                platform, minos = struct.unpack_from("<II", chunk, offset + 8)
                if platform == MACHO_PLATFORM_MACOS:
                    floors.add(_decode_macho_version(minos))
            elif command == LC_VERSION_MIN_MACOSX:
                minos = struct.unpack_from("<I", chunk, offset + 8)[0]
                floors.add(_decode_macho_version(minos))
            if command_size < 8:
                break
            offset += command_size
    floor = max(floors, key=Version) if floors else None
    return cpu_types, floor


def _pe_machine(path: Path) -> int | None:
    """Return the COFF machine type of a PE executable, `None` if not PE."""
    with path.open("rb") as stream:
        head = stream.read(_PE_PROBE_BYTES)
    if head[:2] != b"MZ" or len(head) < 0x40:
        return None
    pe_offset = struct.unpack_from("<I", head, 0x3C)[0]
    if pe_offset + 6 > len(head) or head[pe_offset : pe_offset + 4] != b"PE\0\0":
        return None
    return int(struct.unpack_from("<H", head, pe_offset + 4)[0])


def read_header(
    binary_format: BinaryFormat, path: Path
) -> tuple[frozenset[int | None], str | None]:
    """Parse a binary's headers into its machine set and measured floor.

    The machines are the raw header integers of the format (the ELF
    `e_machine` code, Mach-O CPU types, the PE machine id or `None` when
    unparsable), matching what {data}`~repomatic.release.binary.MACHINE_IDS`
    records as the expectation. The floor is the measured requirement
    {func}`verify_binary_floor` compares against the declared one: always
    `None` on PE, whose headers record nothing enforceable.

    :param binary_format: The format the file was detected as.
    :param path: Path to the binary file.
    :return: `(machines, floor)` as parsed from the headers.
    """
    if binary_format is BinaryFormat.ELF:
        machine, floor = _elf_info(path)
        return frozenset((machine,)), floor
    if binary_format is BinaryFormat.MACHO:
        cpu_types, floor = _macho_info(path)
        return frozenset(cpu_types), floor
    return frozenset((_pe_machine(path),)), None


def _iter_native_binaries(
    directories: Iterable[Path],
) -> Iterator[tuple[Path, BinaryFormat]]:
    """Yield each recognized executable under the given dirs, with its format.

    The format rides along so the caller never re-probes a file this walk
    already opened: a Nuitka dist tree holds hundreds of shared objects.
    """
    for directory in directories:
        for path in sorted(directory.rglob("*")):
            if path.is_file():
                detected = BinaryFormat.detect(path)
                if detected is not None:
                    yield path, detected


def verify_binary_arch(target: BuildTarget, binary_path: Path) -> None:
    """Verify that a binary matches the expected architecture for a target.

    Parses the executable's own headers, so it needs no external tool and
    behaves identically on runner VMs and inside build containers.

    :param target: The build target the binary was compiled for.
    :param binary_path: Path to the binary file.
    :raises ValueError: If binary format or architecture does not match.
    """
    expected_format = target.binary_format

    actual_format = BinaryFormat.detect(binary_path)
    if actual_format is not expected_format:
        got = actual_format.label if actual_format else "unrecognized"
        raise ValueError(
            f"Binary architecture mismatch!\n"
            f"Expected: {expected_format.label} executable for target {target.id!r}\n"
            f"Got: {got} file at {binary_path}"
        )

    reported, _ = read_header(expected_format, binary_path)

    expected = target.expected_machine
    if expected not in reported:
        raise ValueError(
            f"Binary architecture mismatch!\n"
            f"Expected: {expected!r} for target {target.id!r}\n"
            f"Got: {reported!r} from {binary_path}"
        )

    logging.info(
        f"Binary architecture matches: {expected!r} found in {binary_path} "
        f"for {target.id} target."
    )


def verify_binary_floor(
    target: BuildTarget,
    binary_path: Path,
    dist_dirs: Iterable[Path] = (),
) -> None:
    """Verify the binary and its dist tree stay within the target's OS floor.

    Scans the onefile binary itself plus every native library of the given
    Nuitka dist directories (whose content the onefile payload repacks), and
    compares each file's measured requirement to the target's declared floor.

    What the floor counts comes from the format, per
    {attr}`~repomatic.release.binary.BinaryFormat.floor_label`:

    - ELF: the highest `GLIBC_x.y` version requirement of each file. A higher
      requirement means a compiled object picked up symbols newer than the
      build container provides for, and the binary would die at load time on
      the distributions the floor promises.
    - Mach-O: the `minos` of each file, against the deployment target the
      build exports as `MACOSX_DEPLOYMENT_TARGET`.
    - PE: nothing. Its version headers are nominal, so
      {attr}`~repomatic.release.binary.BuildTarget.enforced_floor` reports no
      floor and this returns early; the Windows floor is CPython's own support
      policy, tracked in the docs.

    :param target: The build target the binary was compiled for.
    :param binary_path: Path to the binary file.
    :param dist_dirs: Nuitka dist directories to include in the scan.
    :raises ValueError: If any scanned file exceeds the declared floor.
    """
    declared = target.enforced_floor
    if declared is None:
        logging.info(f"No enforceable floor for {target.id}: documented floor only.")
        return

    expected_format = target.binary_format

    violations = []
    scanned = 0
    # chain() keeps the dist walk lazy: a `(binary_path, *generator)` tuple
    # display would drain it up front.
    candidates = chain(
        ((binary_path, BinaryFormat.detect(binary_path)),),
        _iter_native_binaries(dist_dirs),
    )
    for path, detected in candidates:
        if detected is not expected_format:
            continue
        _, measured = read_header(expected_format, path)
        scanned += 1
        if measured and Version(measured) > Version(declared):
            violations.append((path, measured))

    if violations:
        details = "\n".join(
            f"- {path}: requires {measured}" for path, measured in violations
        )
        raise ValueError(
            f"OS floor exceeded for {target.id}: declared "
            f"{expected_format.floor_label} floor is {declared}, "
            f"but these files require newer:\n{details}"
        )

    logging.info(
        f"{scanned} file(s) verified within the {declared} floor for {target.id}."
    )
