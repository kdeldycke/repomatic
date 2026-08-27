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

A stdlib-only leaf, like {mod}`repomatic.versions`: the digest used to live in
{mod}`repomatic.binary`, so the tool runner and the VirusTotal client each
imported the whole ELF and Mach-O parsing stack (pyelftools included) to hash
a file.
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
    sha256 = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(65536):
            sha256.update(chunk)
    return sha256.hexdigest()
