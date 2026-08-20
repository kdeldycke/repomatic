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

"""Binary build targets and verification utilities.

Defines the Nuitka compilation targets for all supported platforms and
provides native binary verification: architecture and minimum-OS floors are
parsed straight from the executables' ELF, Mach-O and PE headers, so no
external tool is needed on runners or inside build containers.
"""

from __future__ import annotations

import hashlib
import logging
import re
import shutil
import struct
from pathlib import Path

from elftools.elf.elffile import ELFFile
from elftools.elf.gnuversions import GNUVerNeedSection

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator
    from typing import Final


BINARY_ASSET_SUFFIXES = (".bin", ".exe")
"""File extensions identifying compiled binaries among release assets.

The one definition of "a compiled release asset": `scan-virustotal` uploads
this set, the release workflow downloads it (`--pattern` flags in
`_release-engine.yaml`), `docs/binaries.md` lists it, and the dev-release
asset globs derive from it.
"""

PYTHON_DIST_SUFFIXES = (".tar.gz", ".whl")
"""File extensions identifying Python distributions among release assets.

The counterpart of {data}`BINARY_ASSET_SUFFIXES`, and the same kind of single
definition: {func}`pack_binary_assets` excludes this set from the binary
upload list (`create-release` already attached those), and the dev-release
asset globs add it on top of the compiled binaries.
"""


def compute_file_sha256(path: Path) -> str:
    """Compute the SHA-256 hex digest of a file.

    :param path: Path to the file.
    :return: Lowercase hex digest string.
    """
    sha256 = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(65536):
            sha256.update(chunk)
    return sha256.hexdigest()


# manylinux_2_28 (AlmaLinux 8, glibc 2.28) build containers from
# https://quay.io/pypa/, digest-pinned like action SHAs. Both digests below
# resolve the 2026.07.25-1 tag; bump them manually alongside runner pins.
_MANYLINUX_2_28_X86_64 = (
    "quay.io/pypa/manylinux_2_28_x86_64"
    "@sha256:fdb9a9c223b215604dc7b6f7e8fff4b39bfea5fbaa7777a2e5544a60dfa437f8"
)
_MANYLINUX_2_28_AARCH64 = (
    "quay.io/pypa/manylinux_2_28_aarch64"
    "@sha256:e7035406e58d96b7407246af1f6514a3cbd753a0025b42b9adfbeadd3b29ba80"
)


NUITKA_BUILD_TARGETS = {
    "linux-arm64": {
        "os": "ubuntu-26.04-arm",
        "platform_id": "linux",
        "arch": "arm64",
        "extension": "bin",
        "container": _MANYLINUX_2_28_AARCH64,
        "glibc_floor": "2.28",
    },
    "linux-x64": {
        "os": "ubuntu-26.04",
        "platform_id": "linux",
        "arch": "x64",
        "extension": "bin",
        "container": _MANYLINUX_2_28_X86_64,
        "glibc_floor": "2.28",
    },
    "macos-arm64": {
        "os": "macos-26",
        "platform_id": "macos",
        "arch": "arm64",
        "extension": "bin",
        # First Apple-silicon macOS, and the python-build-standalone target.
        "min_os": "11.0",
    },
    "macos-x64": {
        "os": "macos-26-intel",
        "platform_id": "macos",
        "arch": "x64",
        "extension": "bin",
        # The python-build-standalone x86_64 deployment target.
        "min_os": "10.15",
    },
    "windows-arm64": {
        "os": "windows-11-arm",
        "platform_id": "windows",
        "arch": "arm64",
        "extension": "exe",
        "min_os": "11",
    },
    "windows-x64": {
        "os": "windows-2025",
        "platform_id": "windows",
        "arch": "x64",
        "extension": "exe",
        "min_os": "10",
    },
}
"""GitHub-hosted runner matrix for Nuitka builds, keyed by target name.

The key doubles as the compiled binary's short target identifier: it names the
published release asset, so it is chosen for user-friendliness and must stay
stable (download URLs and `docs/binaries.md` match on it).

Values are dictionaries with the following keys:

- `os`: Operating system name, as used in [GitHub-hosted runners](https://docs.github.com/en/actions/writing-workflows/choosing-where-your-workflow-runs/choosing-the-runner-for-a-job#standard-github-hosted-runners-for-public-repositories).

    ```{hint}
    One compile job per target, each on one of the six runners the test matrix
    already covers, so a published binary is built on an image the suite is
    validated against. The targets are exactly
    {data}`~repomatic.lint_repo.KNOWN_RUNNERS`, not a separate selection: an
    image is added here by widening the test axes, never on its own.
    ```

- `platform_id`: Platform identifier, as defined by [Extra Platform](https://github.com/kdeldycke/extra-platforms).

- `arch`: Architecture identifier.

    ```{note}
    Architecture IDs are [inspired from those specified for self-hosted runners](https://docs.github.com/en/actions/hosting-your-own-runners/managing-self-hosted-runners/supported-architectures-and-operating-systems-for-self-hosted-runners#supported-processor-architectures)
    ```

    ```{todo}
    Evaluate replacing the `platform_id` / `arch` pair with a single
    [target triple](https://mcyoung.xyz/2025/04/14/target-triples/).
    ```

- `extension`: File extension of the compiled binary.

- `container`: OCI image the Linux compile and self-test jobs run in, via the
  `container:` key of the release workflow. Compiling inside `manylinux_2_28`
  caps the toolchain at glibc 2.28, so binaries stop inheriting the floor of
  whatever glibc the current runner image ships. Linux targets only: GitHub
  Actions containers do not exist for macOS and Windows runners.

- `glibc_floor`: highest glibc symbol version the compiled artifacts may
  require, matching the build container. Enforced by
  {func}`~repomatic.binary.verify_binary_floor` and documented in
  `docs/binaries.md`.

- `min_os`: minimum OS version the binary runs on. On macOS the release
  workflow exports it as `MACOSX_DEPLOYMENT_TARGET` at compile time (without
  it, compiled objects and processed dylibs inherit the build runner's macOS
  version) and {func}`~repomatic.binary.verify_binary_floor` enforces it. On
  Windows it is documentation-only: the floor is CPython's own Windows
  support policy, not a linker artifact.
"""


FLAT_BUILD_TARGETS = [
    {"target": target_id} | target_data
    for target_id, target_data in NUITKA_BUILD_TARGETS.items()
]
"""List of build targets in a flat format, suitable for matrix inclusion."""


def binary_name(package: str, target: str, version: str | None = None) -> str:
    """Compose a compiled binary's release-asset filename.

    The one definition of the naming convention:
    ``{package}-{version}-{target}.{ext}`` for the versioned upload, and with
    no *version* the stable alias (``{package}-{target}.{ext}``) backing the
    `releases/latest/download` URLs. The extension comes from
    {data}`NUITKA_BUILD_TARGETS`.
    """
    extension = NUITKA_BUILD_TARGETS[target]["extension"]
    middle = f"-{version}" if version else ""
    return f"{package}{middle}-{target}.{extension}"


def versionless_alias(filename: str, version: str) -> str | None:
    """Map a versioned binary filename to its stable alias, or `None`.

    Strips the `-{version}-` segment (`papaya-1.2.3-linux-arm64.bin` becomes
    `papaya-linux-arm64.bin`). Returns `None` for filenames that carry no such
    segment or are not compiled binaries, so callers can filter and map in one
    pass.
    """
    marked = f"-{version}-"
    if marked not in filename or not filename.endswith(BINARY_ASSET_SUFFIXES):
        return None
    return filename.replace(marked, "-", 1)


def binary_filename_re(package: str) -> re.Pattern[str]:
    """Match a *package* binary filename, versioned or versionless.

    Captures `target` and `ext`, both alternations derived from
    {data}`NUITKA_BUILD_TARGETS` so a new build target extends the pattern
    without anyone editing a regex. The release freeze rewrites both spellings
    onto the versioned form through this; `tests/test_platform_keys.py` pins
    the pattern against every target.
    """
    targets = "|".join(sorted(NUITKA_BUILD_TARGETS))
    extensions = "|".join(
        sorted({data["extension"] for data in NUITKA_BUILD_TARGETS.values()})
    )
    return re.compile(
        rf"{re.escape(package)}(?:-[\d.]+)?-"
        rf"(?P<target>{targets})\.(?P<ext>{extensions})"
    )


def pack_binary_assets(dist_dir: Path, version: str) -> list[Path]:
    """Pack a release's upload list, materializing the versionless aliases.

    Mirrors what the release engine's upload step needs: every file in
    *dist_dir* except the Python distributions (`create-release` already
    uploaded those), plus a byte-identical versionless alias copied beside
    each versioned binary so the stable `releases/latest/download` URLs always
    resolve. Aliases share their sibling's digest, which is what lets artifact
    attestations verify them unchanged and the binaries catalog collapse them
    (see `binaries_page._binary_assets`).

    Idempotent: re-running overwrites the same aliases with the same bytes.

    :param dist_dir: Directory holding the compiled binaries and attestation
        bundles downloaded from the build jobs.
    :param version: The release version whose binaries earn aliases.
    :return: Sorted paths to upload, aliases included.
    """
    uploads = {
        path
        for path in dist_dir.iterdir()
        if path.is_file() and not path.name.endswith(PYTHON_DIST_SUFFIXES)
    }
    for path in sorted(uploads):
        alias = versionless_alias(path.name, version)
        if alias is None:
            continue
        alias_path = dist_dir / alias
        shutil.copy2(path, alias_path)
        uploads.add(alias_path)
    return sorted(uploads)


BINARY_AFFECTING_PATHS: Final[tuple[str, ...]] = (
    ".github/workflows/_release-engine.yaml",
    ".github/workflows/release.yaml",
    "pyproject.toml",
    "tests/",
    "uv.lock",
)
"""Path prefixes that always affect compiled binaries, regardless of the project.

Project-specific source directories (derived from `[project.scripts]` in
`pyproject.toml`) are added dynamically by
{attr}`~repomatic.metadata.Metadata.binary_affecting_paths`.

The release workflow entries cover both layouts: upstream keeps the
`_release-engine.yaml` lane (which defines the Nuitka compile and binary
self-test jobs) in-repo, while downstream repos call the engine cross-repo
from their generated `release.yaml`, so a pin bump there rightly triggers a
rebuild.
"""

SKIP_BINARY_BUILD_BRANCHES: Final[frozenset[str]] = frozenset((
    # Autofix branches that don't affect compiled binaries.
    "format-json",
    "format-markdown",
    "format-images",
    "format-shell",
    "sync-gitignore",
    "sync-mailmap",
    "update-dep-graph",
))
"""Autofix branches whose changes cannot affect compiled binaries.

Members are PR branch names produced by autofix jobs that touch only
repository housekeeping (`.mailmap`, `.gitignore`, JSON, Markdown,
images, shell scripts, dependency graph). The binary output is
unchanged, so {attr}`~repomatic.metadata.Metadata.skip_binary_build`
returns `True` when the PR head branch matches a member, saving an
expensive Nuitka compilation.

```{note}
This set is intentionally disjoint from
{data}`repomatic.git_ops.VERSION_BUMP_BRANCHES`: version-bump branches do
change binaries (they rewrite the version string baked into the build), so
they belong to a different policy.
```
"""


PLATFORM_FORMATS: Final[dict[str, str]] = {
    "linux": "elf",
    "macos": "macho",
    "windows": "pe",
}
"""Executable format expected for each build platform."""

ELF_MACHINES: Final[dict[str, str]] = {
    "arm64": "EM_AARCH64",
    "x64": "EM_X86_64",
}
"""Expected ELF `e_machine` value (as decoded by pyelftools) per architecture."""

MACHO_CPU_TYPES: Final[dict[str, int]] = {
    "arm64": 0x0100000C,
    "x64": 0x01000007,
}
"""Expected Mach-O header `cputype` per architecture."""

PE_MACHINES: Final[dict[str, int]] = {
    "arm64": 0xAA64,
    "x64": 0x8664,
}
"""Expected PE COFF `Machine` field per architecture."""

MACHO_MAGIC_64: Final[int] = 0xFEEDFACF
"""Magic of a 64-bit Mach-O header, in the file's own (little) endianness."""

MACHO_FAT_MAGICS: Final[frozenset[int]] = frozenset((0xCAFEBABE, 0xCAFEBABF))
"""Big-endian magics of universal (fat) Mach-O containers, 32- and 64-bit."""

LC_VERSION_MIN_MACOSX: Final[int] = 0x24
"""Mach-O load command carrying the minimum macOS version (pre-10.14 SDKs)."""

LC_BUILD_VERSION: Final[int] = 0x32
"""Mach-O load command carrying the platform and minimum OS (10.14+ SDKs)."""

MACHO_PLATFORM_MACOS: Final[int] = 1
"""`platform` field value naming macOS inside an `LC_BUILD_VERSION` command."""

_PE_PROBE_BYTES: Final[int] = 65536
"""Upper bound read when probing for a PE header: enough to cover any
real-world DOS-stub offset to the COFF header without reading the whole
binary."""


def _version_key(version: str) -> tuple[int, ...]:
    """Sort key for dotted version strings like `2.28` or `10.15`."""
    return tuple(int(part) for part in version.split("."))


def _binary_format(path: Path) -> str | None:
    """Detect the executable format of a file from its magic bytes."""
    with path.open("rb") as stream:
        magic = stream.read(4)
    if magic == b"\x7fELF":
        return "elf"
    if magic[:2] == b"MZ":
        return "pe"
    if len(magic) == 4 and (
        struct.unpack("<I", magic)[0] == MACHO_MAGIC_64
        or struct.unpack(">I", magic)[0] in MACHO_FAT_MAGICS
    ):
        return "macho"
    return None


def _elf_info(path: Path) -> tuple[str, str | None]:
    """Return the machine and highest glibc requirement of an ELF file.

    The glibc requirement is the maximum `GLIBC_x.y` entry of the
    `.gnu.version_r` section: the version table the dynamic loader checks
    before letting the file run, and so the file's effective glibc floor.
    """
    versions = set()
    with path.open("rb") as stream:
        elf = ELFFile(stream)
        machine = str(elf.header["e_machine"])
        for section in elf.iter_sections():
            if not isinstance(section, GNUVerNeedSection):
                continue
            for _verneed, aux_iter in section.iter_versions():
                for aux in aux_iter:
                    if aux.name.startswith("GLIBC_"):
                        versions.add(aux.name.removeprefix("GLIBC_"))
    floor = max(versions, key=_version_key) if versions else None
    return machine, floor


def _decode_macho_version(value: int) -> str:
    """Decode a packed Mach-O version integer into a `major.minor` string."""
    return f"{value >> 16}.{(value >> 8) & 0xFF}"


def _macho_slices(data: bytes) -> Iterator[bytes]:
    """Yield each architecture slice of a Mach-O file (fat or thin)."""
    magic = struct.unpack_from(">I", data, 0)[0]
    if magic not in MACHO_FAT_MAGICS:
        yield data
        return
    wide = magic == 0xCAFEBABF
    entry_format = ">iiQQII" if wide else ">iiIII"
    entry_size = struct.calcsize(entry_format)
    count = struct.unpack_from(">I", data, 4)[0]
    for index in range(count):
        fields = struct.unpack_from(entry_format, data, 8 + index * entry_size)
        offset, size = fields[2], fields[3]
        yield data[offset : offset + size]


def _macho_info(path: Path) -> tuple[set[int], str | None]:
    """Return the CPU types and highest macOS floor of a Mach-O file.

    The floor is the `minos` field of the `LC_BUILD_VERSION` load command
    (or the older `LC_VERSION_MIN_MACOSX`), maxed across the slices of a
    universal binary. It is what the loader compares to the running macOS
    before letting the file execute.
    """
    data = path.read_bytes()
    cpu_types: set[int] = set()
    floors: set[str] = set()
    for chunk in _macho_slices(data):
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
    floor = max(floors, key=_version_key) if floors else None
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


def _iter_native_binaries(directories: Iterable[Path]) -> Iterator[Path]:
    """Yield files with a recognized executable format under the given dirs."""
    for directory in directories:
        for path in sorted(directory.rglob("*")):
            if path.is_file() and _binary_format(path) is not None:
                yield path


def _check_target(target: str) -> dict[str, str]:
    """Return the target's build data, raising on unknown target names."""
    if target not in NUITKA_BUILD_TARGETS:
        msg = (
            f"Unknown target: {target!r}. "
            f"Valid targets: {', '.join(sorted(NUITKA_BUILD_TARGETS))}."
        )
        raise ValueError(msg)
    return NUITKA_BUILD_TARGETS[target]


def verify_binary_arch(target: str, binary_path: Path) -> None:
    """Verify that a binary matches the expected architecture for a target.

    Parses the executable's own headers, so it needs no external tool and
    behaves identically on runner VMs and inside build containers.

    :param target: Build target (e.g., 'linux-arm64', 'macos-x64').
    :param binary_path: Path to the binary file.
    :raises ValueError: If target is unknown.
    :raises AssertionError: If binary format or architecture does not match.
    """
    target_data = _check_target(target)
    arch = target_data["arch"]
    expected_format = PLATFORM_FORMATS[target_data["platform_id"]]

    actual_format = _binary_format(binary_path)
    if actual_format != expected_format:
        raise AssertionError(
            f"Binary architecture mismatch!\n"
            f"Expected: {expected_format} executable for target {target!r}\n"
            f"Got: {actual_format or 'unrecognized'} file at {binary_path}"
        )

    reported: set[object]
    expected: object
    if expected_format == "elf":
        machine, _ = _elf_info(binary_path)
        expected = ELF_MACHINES[arch]
        reported = {machine}
    elif expected_format == "macho":
        cpu_types, _ = _macho_info(binary_path)
        expected = MACHO_CPU_TYPES[arch]
        reported = set(cpu_types)
    else:
        expected = PE_MACHINES[arch]
        reported = {_pe_machine(binary_path)}

    if expected not in reported:
        raise AssertionError(
            f"Binary architecture mismatch!\n"
            f"Expected: {expected!r} for target {target!r}\n"
            f"Got: {reported!r} from {binary_path}"
        )

    logging.info(
        f"Binary architecture matches: {expected!r} found in {binary_path} "
        f"for {target} target."
    )


def verify_binary_floor(
    target: str,
    binary_path: Path,
    dist_dirs: Iterable[Path] = (),
) -> None:
    """Verify the binary and its dist tree stay within the target's OS floor.

    Scans the onefile binary itself plus every native library of the given
    Nuitka dist directories (whose content the onefile payload repacks), and
    compares each file's measured requirement to the target's declared floor:

    - Linux: the highest `GLIBC_x.y` version requirement of each ELF against
      `glibc_floor`. A higher requirement means a compiled object picked up
      symbols newer than the build container provides for, and the binary
      would die at load time on the distributions the floor promises.
    - macOS: the `minos` of each Mach-O against `min_os`, the deployment
      target the build exports as `MACOSX_DEPLOYMENT_TARGET`.
    - Windows: nothing. PE version headers are nominal; the floor is
      CPython's own Windows support policy, tracked in the docs.

    :param target: Build target (e.g., 'linux-arm64', 'macos-x64').
    :param binary_path: Path to the binary file.
    :param dist_dirs: Nuitka dist directories to include in the scan.
    :raises ValueError: If target is unknown.
    :raises AssertionError: If any scanned file exceeds the declared floor.
    """
    target_data = _check_target(target)
    platform_id = target_data["platform_id"]
    if platform_id == "windows":
        logging.info(f"No enforceable floor for {target}: documented floor only.")
        return

    floor_key = "glibc_floor" if platform_id == "linux" else "min_os"
    declared = target_data[floor_key]
    expected_format = PLATFORM_FORMATS[platform_id]

    violations = []
    scanned = 0
    for path in (binary_path, *_iter_native_binaries(dist_dirs)):
        if _binary_format(path) != expected_format:
            continue
        if expected_format == "elf":
            _, measured = _elf_info(path)
        else:
            _, measured = _macho_info(path)
        scanned += 1
        if measured and _version_key(measured) > _version_key(declared):
            violations.append((path, measured))

    if violations:
        details = "\n".join(
            f"- {path}: requires {measured}" for path, measured in violations
        )
        raise AssertionError(
            f"OS floor exceeded for {target}: declared {floor_key} is {declared}, "
            f"but these files require newer:\n{details}"
        )

    logging.info(
        f"{scanned} file(s) verified within the {declared} floor for {target}."
    )
