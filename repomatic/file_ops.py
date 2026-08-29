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

"""How this package writes a file it owns.

The counterpart of {mod}`repomatic.file_inventory`, which answers what is on
disk: this one decides how something lands there. Format-neutral by design, and
the reason it is its own module rather than a helper beside one format's
readers, where the CSV writer, the SVG chart renderer and the changelog freeze
each had to reach across for it.

```{note}
Every write here compares before it acts, which is what the whole package leans
on rather than a nicety. A generator rewriting its output unconditionally turns
each scheduled run into a commit, and a sync job that opens a pull request would
open one forever.
```
"""

from __future__ import annotations

import logging
from pathlib import Path


def write_if_changed(path: Path, content: str) -> bool:
    """Write *content* to *path*, leaving an already-matching file alone.

    Creates the parent directories when missing.

    Reads the file back to compare, so it is right for a caller holding only the
    content it wants. A caller that already has the previous text in hand can
    compare in memory instead and skip the read, which is what
    {meth}`~repomatic.release.prepare_release.PrepareRelease._update_file` does before
    delegating the write.

    :param path: File to write.
    :param content: The full text the file should hold.
    :return: `True` when the file was created or its content changed.
    """
    if path.exists() and path.read_text(encoding="UTF-8") == content:
        logging.debug(f"{path} already up to date.")
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="UTF-8")
    return True
