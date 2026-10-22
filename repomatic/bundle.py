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

"""Raw access to the data files bundled in `repomatic/data/`.

The lowest layer of bundled-data access, deliberately dependency-free so any
module can read a data file without import cycles. Policy layers sit above:
`repomatic init` reads only the files {data}`~repomatic.components.COMPONENTS`
declares, and {mod}`repomatic.tooling.runner` resolves tool configs.
"""

from __future__ import annotations

from functools import cache
from importlib.resources import as_file, files


@cache
def get_data_content(filename: str) -> str:
    """Get the content of a bundled data file.

    This is the low-level function for reading any file from `repomatic/data/`.

    Memoized for the process: bundled data is immutable for the life of an
    installed release, and the workflow lint re-reads the same canonical
    workflow per downstream file it checks.

    :param filename: Name of the file to retrieve (e.g., "labels.toml").
    :return: Content of the file as a string.
    :raises FileNotFoundError: If the file doesn't exist.
    """
    data_files = files("repomatic.data")
    with as_file(data_files.joinpath(filename)) as path:
        if path.exists():
            return path.read_text(encoding="UTF-8")

    msg = f"Data file not found: {filename}"
    raise FileNotFoundError(msg)
