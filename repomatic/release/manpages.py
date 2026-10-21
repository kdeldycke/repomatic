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

"""Packing of the man pages a release attaches.

The release workflow renders one roff `.1` file per command of the
`[tool.repomatic] manpages.script` tree, then attaches them to the release as a
single tarball. {func}`pack_manpages` builds that tarball.

The archive is byte-deterministic: members are sorted and carry no timestamp
or owner, so packing an unchanged tree twice gives the same file, and the same
digest for its attestation.
"""

from __future__ import annotations

import gzip
import tarfile

TYPE_CHECKING = False
if TYPE_CHECKING:
    from pathlib import Path
    from typing import Final

MANPAGES_ARCHIVE_SUFFIX: Final[str] = ".tar.gz"
"""Extension of the man-page tarball, after its asset name."""

_MEMBER_MODE: Final[int] = 0o644
"""Permission bits every archived page carries, whatever the checkout gave it."""


def pack_manpages(man_dir: Path, archive: Path) -> list[str]:
    """Bundle the pages of *man_dir* into the gzipped tarball *archive*.

    Each member keeps its path below *man_dir*, prefixed with the name of the
    directory itself, as `tar --create man/` would store it. A reader unpacking
    the archive gets the same `man/` tree the release job rendered.

    :param man_dir: Directory holding the rendered pages.
    :param archive: The tarball to write. An existing file is replaced.
    :return: The member names, in archive order.
    :raises ValueError: When *man_dir* holds no file.
    """
    pages = sorted(path for path in man_dir.rglob("*") if path.is_file())
    if not pages:
        msg = f"{man_dir} holds no man page to pack."
        raise ValueError(msg)

    members = []
    with (
        archive.open("wb") as raw,
        # A fixed modification time and an empty name keep the gzip header
        # free of anything that changes from one run to the next.
        gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed,
        tarfile.open(fileobj=compressed, mode="w") as tar,
    ):
        for page in pages:
            name = f"{man_dir.name}/{page.relative_to(man_dir).as_posix()}"
            info = tar.gettarinfo(str(page), arcname=name)
            info.mtime = 0
            info.mode = _MEMBER_MODE
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            with page.open("rb") as content:
                tar.addfile(info, content)
            members.append(name)
    return members
