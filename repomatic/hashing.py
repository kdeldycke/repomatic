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

"""File digesting, shared by every integrity check.

A stdlib-only leaf, like {mod}`repomatic.versions`, so that hashing a file
never imports the ELF and Mach-O parsing stack of
{mod}`repomatic.release.binary`.
"""

from __future__ import annotations

import hashlib

TYPE_CHECKING = False
if TYPE_CHECKING:
    from pathlib import Path


def compute_file_sha256(path: Path) -> str:
    """Compute the SHA-256 hex digest of a file.

    :param path: Path to the file.
    :return: Lowercase hex digest string.
    """
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()
