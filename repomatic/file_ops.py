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

"""How this package puts a file on disk, and takes one off.

The counterpart of {mod}`repomatic.file_inventory`, which answers what is on
disk: this one decides how something lands there. Format-neutral by design, and
the reason it is its own module rather than a helper beside one format's
readers, where the CSV writer, the SVG chart renderer, the download cache and
the release freeze each had to reach across for one.

Three concerns, deliberately separate, because a caller needs to pick:

- {func}`write_if_changed` leaves a matching file untouched. Every generator
  leans on it rather than treating it as a nicety: one that rewrote its output
  unconditionally would turn each scheduled run into a commit, and a sync job
  opening a pull request would open one forever.
- {func}`atomic_write` makes a half-written file unobservable. What a cache
  another process reads needs, and what a killed run must not leave behind.
- {func}`unlink_with_empty_parents` removes a file or a whole component and
  takes the directories it emptied with it.
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from shutil import rmtree

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Callable


def write_if_changed(path: Path, content: str, previous: str | None = None) -> bool:
    """Write *content* to *path*, leaving an already-matching file alone.

    Creates the parent directories when missing.

    :param path: File to write.
    :param content: The full text the file should hold.
    :param previous: What the file holds already, when the caller has read it
        anyway. Left unset, the file is read back here to compare. A caller that
        transformed text it already had in hand passes it and skips the read.
    :return: `True` when the file was created or its content changed.
    """
    if previous is None and path.exists():
        previous = path.read_text(encoding="UTF-8")
    if previous == content:
        logging.debug(f"{path} already up to date.")
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="UTF-8")
    return True


def atomic_write(dest: Path, prefix: str, write: Callable[[Path], object]) -> None:
    """Write *dest* atomically: temp file in the target directory, then rename.

    The other half of the question {func}`write_if_changed` answers. That one
    asks whether the bytes differ; this one asks whether a reader can ever
    observe half of them. A cache another process reads, or a file a killed run
    leaves behind, needs this one.

    The rename is atomic on POSIX (same-filesystem rename) and safe on Windows
    (`Path.replace` overwrites atomically). *write* receives the temp path and
    fills it (its return value is ignored, so `write_text`/`write_bytes` pass
    straight through); partial writes are cleaned up on any failure.

    :param dest: Final path the temp file is renamed onto.
    :param prefix: Prefix for the temp file, so a stray one names its owner.
    :param write: Fills the temp path it is handed.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=prefix, suffix=".tmp")
    try:
        os.close(fd)
        write(Path(tmp))
        Path(tmp).replace(dest)
    except BaseException:
        # Clean up partial writes on any failure.
        Path(tmp).unlink(missing_ok=True)
        raise


def unlink_with_empty_parents(target: Path, root: Path) -> None:
    """Delete *target*, then prune now-empty parent directories up to *root*.

    *target* may be a directory, removed with everything it carries: what is
    being deleted is often a whole component rather than one file.

    Stops at the first parent that still holds something, and never touches
    *root* itself.

    :param target: File or directory to delete.
    :param root: Directory the upward pruning stops below.
    """
    if target.is_dir():
        rmtree(target)
    else:
        target.unlink()
    parent = target.parent
    while parent != root:
        try:
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent
