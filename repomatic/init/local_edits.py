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

"""Spot the local edits `repomatic init` overwrites in a skill or agent copy.

`init` replaces each skill and agent copy it manages with the one the running
release bundles. An edit a repository made to its copy, and never sent
upstream, is lost to that rewrite, and the diff shows the loss as routine sync
churn.

A copy holds no local edit when it matches a file that the release the
repository adopted before the run bundled: the release its upstream `uses:`
pins name. The wheel of that release holds every such file, so `init` reads it
from PyPI and compares digests, in the normalized form `init` writes to disk.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from functools import cached_property
from io import BytesIO
from pathlib import PurePosixPath
from zipfile import BadZipFile, ZipFile

from ..components import RemovedAsset
from ..file_ops import normalize_text
from ..http import FetchError, get_bytes
from ..pypi import release_wheel
from .upgrade import pins_running_release

CHECKED_COMPONENTS = frozenset(("skills", "subagents"))
"""Components whose copies `init` checks for local edits before it overwrites them.

Skills and agents are prose a repository edits by hand. The other bundled files
differ from the shipped ones for other reasons: `labels` is staged for one
command, and the sync jobs bump the pins of `publish-pypi-action` between two
releases.
"""


def shipped_digests(package: str, version: str) -> frozenset[str] | None:
    """The digests of the data files one release of *package* bundled.

    Reads the wheel PyPI serves for *version*, once its SHA-256 matches the one
    PyPI records. Hashes every text file under the `data/` folder of the
    package with {meth}`~repomatic.components.RemovedAsset.digest`, which is
    the form `init` writes to disk. The folder layout of the release does not
    matter, so a wheel that bundled a skill under another path still counts.

    :param package: The PyPI package name.
    :param version: The bare version of the release.
    :return: The digests, or `None` when the wheel could not be read.
    """
    wheel = release_wheel(package, version)
    if wheel is None:
        return None
    url, sha256 = wheel
    try:
        data = get_bytes(url)
    except FetchError as error:
        logging.debug(f"Could not download {url}: {error}")
        return None
    if hashlib.sha256(data).hexdigest() != sha256:
        logging.warning(f"{url} does not match the SHA-256 PyPI records for it.")
        return None
    digests: set[str] = set()
    try:
        with ZipFile(BytesIO(data)) as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                if PurePosixPath(info.filename).parts[1:2] != ("data",):
                    continue
                try:
                    text = archive.read(info).decode("UTF-8")
                except UnicodeDecodeError:
                    # A binary asset: no prose to compare.
                    continue
                digests.add(RemovedAsset.digest(text))
    except BadZipFile as error:
        logging.warning(f"{url} is not a readable wheel: {error}")
        return None
    return frozenset(digests)


@dataclass
class LocalEditGuard:
    """Spot the overwrites of one `init` run that lose a local edit."""

    package: str
    """PyPI name of the upstream package, like `repomatic`."""

    previous: str
    """Bare version of the release the repository adopted before the run."""

    _reported_unread: bool = field(default=False, init=False)
    """Whether {meth}`check` already reported that {attr}`shipped` is unknown."""

    @cached_property
    def shipped(self) -> frozenset[str] | None:
        """The digests of the files {attr}`previous` bundled.

        Read on first use, so a run that overwrites no copy reads nothing.
        `None` when the wheel could not be read.
        """
        return shipped_digests(self.package, self.previous)

    def check(self, label: str, current: str, new: str) -> str | None:
        """The warning to give when *new* replaces *current*.

        :param label: The repository-relative path of the copy.
        :param current: The text of the copy on disk.
        :param new: The bundled text about to replace it.
        :return: A warning when *current* matches no file {attr}`previous`
            bundled. One warning for the whole run when that release cannot be
            read. `None` otherwise, as for a change of trailing whitespace only.
        """
        if normalize_text(current) == normalize_text(new):
            return None
        # When the repository already pins the running release, that release
        # bundled *new* itself, so any other text is a local edit.
        if not pins_running_release(f"v{self.previous}"):
            shipped = self.shipped
            if shipped is None:
                if self._reported_unread:
                    return None
                self._reported_unread = True
                return (
                    f"Could not read the {self.package} `{self.previous}` wheel"
                    " from PyPI, so no overwritten skill or agent was checked for"
                    " local edits."
                )
            if RemovedAsset.digest(current) in shipped:
                return None
        return (
            f"Overwrote local edits in {label}: it matches no file"
            f" {self.package} `{self.previous}` bundled. Restore them from git"
            " and port them upstream, or the next sync reverts them again."
        )
