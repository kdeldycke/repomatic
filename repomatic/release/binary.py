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

"""Binary build targets and the names of their release assets.

Defines the Nuitka compilation targets for all supported platforms, the
executable format each one compiles to, and the naming of the binaries a
release publishes. The header parsing that verifies a compiled binary lives in
{mod}`repomatic.release.verify`.
"""

from __future__ import annotations

import re
import shutil
import struct
from dataclasses import dataclass
from enum import Enum

from extra_platforms import AARCH64, LINUX, MACOS, WINDOWS, X86_64

from ..matrix.axes import (
    LINUX_ARM64_RUNNER,
    LINUX_X64_RUNNER,
    MACOS_ARM64_RUNNER,
    MACOS_X64_RUNNER,
    WINDOWS_ARM64_RUNNER,
    WINDOWS_X64_RUNNER,
)

TYPE_CHECKING = False
if TYPE_CHECKING:
    from pathlib import Path
    from typing import Final

    from extra_platforms import Architecture, Group, Platform


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


MACHO_MAGIC_64: Final[int] = 0xFEEDFACF
"""Magic of a 64-bit Mach-O header, in the file's own (little) endianness."""

MACHO_FAT_MAGICS: Final[frozenset[int]] = frozenset((0xCAFEBABE, 0xCAFEBABF))
"""Big-endian magics of universal (fat) Mach-O containers, 32- and 64-bit."""


class BinaryFormat(Enum):
    """Executable container format a compiled binary uses.

    Each member owns what varies by format: the name messages print, the file
    extension its executables take, and what a target's floor measures. The
    floor label doubles as the enforceability flag, since a format recording no
    measurable floor has nothing to label. The header parser for each format is
    {func}`~repomatic.release.verify.read_header`.

    ```{note}
    Members carry no magic bytes of their own: {meth}`detect` needs to read
    them in a fixed order (a fat Mach-O and a PE both fail an equality test on
    the first four bytes), so the probe stays one method rather than a table.
    ```
    """

    ELF = ("elf", "bin", "glibc")
    MACHO = ("macho", "bin", "macOS")
    PE = ("pe", "exe", None)

    def __init__(self, label: str, extension: str, floor_label: str | None) -> None:
        self.label = label
        """Lowercase format name, as printed in verification messages."""
        self.extension = extension
        """File extension executables of this format take."""
        self.floor_label = floor_label
        """What a target's floor measures for this format, or `None`.

        `None` marks a format whose headers record nothing a scan can check,
        so {attr}`BuildTarget.enforced_floor` reports no floor to enforce.
        Only PE is in that case: its version headers are nominal, and the
        Windows floor is CPython's own support policy, not a linker artifact.
        """

    @classmethod
    def detect(cls, path: Path) -> BinaryFormat | None:
        """Identify a file's executable format from its magic bytes.

        :param path: Path to the file to probe.
        :return: The matching format, or `None` for anything unrecognized.
        """
        with path.open("rb") as stream:
            magic = stream.read(4)
        if magic == b"\x7fELF":
            return cls.ELF
        if magic[:2] == b"MZ":
            return cls.PE
        if len(magic) == 4 and (
            struct.unpack("<I", magic)[0] == MACHO_MAGIC_64
            or struct.unpack(">I", magic)[0] in MACHO_FAT_MAGICS
        ):
            return cls.MACHO
        return None


PLATFORM_FORMATS: Final[dict[Platform | Group, BinaryFormat]] = {
    LINUX: BinaryFormat.ELF,
    MACOS: BinaryFormat.MACHO,
    WINDOWS: BinaryFormat.PE,
}
"""Executable format each build platform compiles to.

Read through {attr}`BuildTarget.binary_format`, which is how every caller
reaches it.
"""

MACHINE_IDS: Final[dict[tuple[BinaryFormat, Architecture], str | int]] = {
    # ELF `e_machine`, as decoded by pyelftools.
    (BinaryFormat.ELF, AARCH64): "EM_AARCH64",
    (BinaryFormat.ELF, X86_64): "EM_X86_64",
    # Mach-O header `cputype`.
    (BinaryFormat.MACHO, AARCH64): 0x0100000C,
    (BinaryFormat.MACHO, X86_64): 0x01000007,
    # PE COFF `Machine` field.
    (BinaryFormat.PE, AARCH64): 0xAA64,
    (BinaryFormat.PE, X86_64): 0x8664,
}
"""Machine identifier a binary's header carries, per format and architecture.

One table rather than three, keyed the way
{data}`~repomatic.tooling.registry.PlatformKey` keys the tool registry: the value
is a pyelftools machine name on ELF and a raw header integer on Mach-O and PE,
so a caller compares it against whatever the matching parser returns.
"""


@dataclass(frozen=True)
class BuildTarget:
    """One Nuitka compile target: a runner image, a platform and an architecture.

    Carries the metadata every consumer branches on, and the methods that
    interpret it, so a caller asks the target what its binaries must look like
    instead of re-deriving it from a platform name.

    Platform and architecture are [Extra
    Platforms](https://github.com/kdeldycke/extra-platforms) traits rather than
    free strings, which is the same vocabulary
    {data}`~repomatic.tooling.registry.PlatformKey` keys the downloaded-binary
    registry on. {meth}`as_matrix_entry` renders both back to their ids for the
    workflow matrix.

    Every field is irreducible: anything a format decides (the file extension,
    what the floor measures, which header field to read) lives on
    {class}`BinaryFormat` instead.
    """

    id: str
    """Short target identifier, chosen for user-friendliness.

    It names the published release asset, so it must stay stable: download
    URLs, `docs/install.md` and the `binaries.csv` catalog all match on it.

    ```{note}
    It is deliberately *not* derived from {attr}`platform` and {attr}`arch`,
    even though all six targets currently read `{platform}-{short arch}`. The
    asset names froze on the short `x64` and `arm64` spellings while those
    fields carry the canonical extra-platforms ids, and a future target
    splitting an existing pair (a musl Linux, say) would need a name the
    derivation cannot produce. `tests/test_binary.py` pins the pairing.
    ```
    """

    runner: str
    """Runner image, as named in [GitHub-hosted runners](https://docs.github.com/en/actions/writing-workflows/choosing-where-your-workflow-runs/choosing-the-runner-for-a-job#standard-github-hosted-runners-for-public-repositories).

    Named for what it holds, not for the `os` matrix key it renders to:
    `ubuntu-26.04-arm` is an image, and {attr}`platform` is the operating
    system.

    ```{hint}
    One compile job per target, each on one of the six runners the test matrix
    already covers, so a published binary is built on an image the suite is
    validated against. The targets are exactly
    {data}`~repomatic.matrix.axes.KNOWN_RUNNERS`, not a separate selection: an
    image is added here by widening the test axes, never on its own.
    ```
    """

    platform: Platform | Group
    """Operating system the binary runs on."""

    arch: Architecture
    """CPU architecture the binary is compiled for."""

    floor: str
    """Oldest runtime the binary is built to run on.

    What the version counts is the format's business, named by
    {attr}`BinaryFormat.floor_label`: a glibc symbol version on ELF, a macOS
    deployment target on Mach-O, a Windows release on PE. The first two are
    measured out of the compiled files by
    {func}`~repomatic.release.verify.verify_binary_floor`; the third is
    documentation, since PE version headers record nothing to measure.

    On macOS the release workflow also exports it as
    `MACOSX_DEPLOYMENT_TARGET` at compile time. Without it, compiled objects
    and processed dylibs inherit the build runner's own macOS version.
    """

    container: str | None = None
    """OCI image the Linux compile and self-test jobs run in.

    Passed to the `container:` key of the release workflow. Compiling inside
    `manylinux_2_28` caps the toolchain at glibc 2.28, so binaries stop
    inheriting the floor of whatever glibc the current runner image ships.

    Declared per target rather than derived from {attr}`platform`, which would
    assume every Linux target is a manylinux one. Absent on macOS and Windows:
    GitHub Actions containers do not exist for those runners.
    """

    @property
    def binary_format(self) -> BinaryFormat:
        """Executable format the compiler emits for this target."""
        return PLATFORM_FORMATS[self.platform]

    @property
    def extension(self) -> str:
        """File extension of the compiled binary."""
        return self.binary_format.extension

    @property
    def expected_machine(self) -> str | int:
        """Machine identifier this target's binaries carry in their header."""
        return MACHINE_IDS[self.binary_format, self.arch]

    @property
    def enforced_floor(self) -> str | None:
        """{attr}`floor`, or `None` when the format records nothing to measure."""
        return self.floor if self.binary_format.floor_label else None

    def as_matrix_entry(self) -> dict[str, str]:
        """Flat, JSON-safe mapping of this target, for GitHub matrix inclusion.

        Renders {attr}`platform` and {attr}`arch` back to their extra-platforms
        ids, the spelling the workflow expressions (`matrix.platform_id`) and
        `tests.yaml`'s runner check compare against, and {attr}`runner` back to
        the `os` key `runs-on:` reads. A target with no container omits the
        key, so an entry carries only what applies to it.
        """
        entry = {
            "target": self.id,
            "os": self.runner,
            "platform_id": self.platform.id,
            "arch": self.arch.id,
            "extension": self.extension,
            "floor": self.floor,
        }
        if self.container is not None:
            entry["container"] = self.container
        return entry


NUITKA_BUILD_TARGETS: Final[dict[str, BuildTarget]] = {
    target.id: target
    for target in (
        BuildTarget(
            id="linux-arm64",
            runner=LINUX_ARM64_RUNNER,
            platform=LINUX,
            arch=AARCH64,
            floor="2.28",
            container=_MANYLINUX_2_28_AARCH64,
        ),
        BuildTarget(
            id="linux-x64",
            runner=LINUX_X64_RUNNER,
            platform=LINUX,
            arch=X86_64,
            floor="2.28",
            container=_MANYLINUX_2_28_X86_64,
        ),
        BuildTarget(
            id="macos-arm64",
            runner=MACOS_ARM64_RUNNER,
            platform=MACOS,
            arch=AARCH64,
            # First Apple-silicon macOS, and the python-build-standalone target.
            floor="11.0",
        ),
        BuildTarget(
            id="macos-x64",
            runner=MACOS_X64_RUNNER,
            platform=MACOS,
            arch=X86_64,
            # The python-build-standalone x86_64 deployment target.
            floor="10.15",
        ),
        BuildTarget(
            id="windows-arm64",
            runner=WINDOWS_ARM64_RUNNER,
            platform=WINDOWS,
            arch=AARCH64,
            floor="11",
        ),
        BuildTarget(
            id="windows-x64",
            runner=WINDOWS_X64_RUNNER,
            platform=WINDOWS,
            arch=X86_64,
            floor="10",
        ),
    )
}
"""GitHub-hosted runner matrix for Nuitka builds, keyed by target name.

The roster is closed: every entry compiles on a runner the test matrix already
covers, and the key doubles as the compiled binary's published identifier. See
{class}`BuildTarget` for what each field means and which of them are frozen.

```{note} A target leaves the roster when its runner image does
A published binary needs a runner to compile on, so GitHub's *Available
Images* table sets how long an architecture lives here. `macos-x64` therefore
ships for as long as that table offers an Intel macOS label, on GitHub's
schedule rather than Apple's. The two diverge: Apple lets Mac App Store apps
go arm64-only and drops Intel in macOS 27, while an Intel Mac stays on
macOS 26 Tahoe and keeps running it, so those users still want the binary.

Drop the target once `sync-runner-images` reports the image deprecated, and
not before. Removing one is a single change across two files: this roster and
{data}`~repomatic.matrix.axes.TEST_RUNNERS_FULL` are held equal by
`tests/test_workflows.py::test_binary_build_targets_are_the_test_axes`. A
retirement onto a successor image needs no edit here: each target reads its
runner from a slot constant of {mod}`repomatic.matrix.axes`, which is what
`sync-runner-images` rewrites.
```
"""


FLAT_BUILD_TARGETS: Final[list[dict[str, str]]] = [
    target.as_matrix_entry() for target in NUITKA_BUILD_TARGETS.values()
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
    extension = NUITKA_BUILD_TARGETS[target].extension
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
        sorted({target.extension for target in NUITKA_BUILD_TARGETS.values()})
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
