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

"""PEP 440 version primitives, shared by the whole package.

Every version this package reads comes from somewhere it does not control (a
lock file, a package index, a git tag), so parsing has to tolerate junk, and
nearly every module compares or normalizes versions somewhere. These helpers
stay out of {mod}`repomatic.deps.pins`, whose import graph reaches the GitHub,
PyPI and npm clients: a module below it in that graph could not import them
without closing a cycle. A leaf module with no dependency beyond `packaging` is
what lets everyone import the one copy.
"""

from __future__ import annotations

import re

from packaging.version import InvalidVersion, Version

BUMP_PARTS = ("minor", "major")
"""The version parts a bump-version job may propose.

The `changelog.yaml` bump matrix runs one job per entry, and
{func}`~repomatic.metadata.project.is_version_bump_allowed` gates each against
the last release. A patch bump is absent by design: the post-release commit
already writes the next `.dev0` floor, so nothing has to propose one.
"""

DEV_SUFFIX_RE = re.compile(r"\.dev\d*$")
"""Match the trailing PEP 440 developmental-release segment of a version."""


def safe_version(value: str) -> Version | None:
    """Parse a PEP 440 version, returning `None` for anything unparsable.

    Collapsing the `try`/`except InvalidVersion` into one helper keeps the
    callers reading as the filters they are.

    :param value: A version string.
    :return: The parsed {class}`~packaging.version.Version`, or `None` when
        *value* is empty or not PEP 440.
    """
    if not value:
        return None
    try:
        return Version(value)
    except InvalidVersion:
        return None


def parse_tag_version(tag: str) -> Version | None:
    """Parse a git tag as a version, dropping its `v` prefix.

    The `v` belongs to the tag namespace and the version string itself is bare,
    so tag `v1.2.3` names version `1.2.3`. A tag outside the version scheme
    (`nightly`, `v1.2.3_hotfix`) is not an error: every caller reads tags it
    does not control, and one odd tag must not stop the others from parsing.

    :param tag: A git tag, with or without its `v` prefix.
    :return: The parsed {class}`~packaging.version.Version`, or `None` when the
        tag names no PEP 440 version.
    """
    return safe_version(tag.removeprefix("v"))


def is_newer(new: str, old: str) -> bool:
    """Return `True` when *new* is a strictly higher version than *old*.

    Unparsable versions compare as not-newer, so a malformed candidate never
    triggers a bump.
    """
    new_v, old_v = safe_version(new), safe_version(old)
    if new_v is None or old_v is None:
        return False
    return new_v > old_v


def strip_dev_suffix(version: str) -> str:
    """Drop any PEP 440 `.devN` segment from *version*.

    `"5.10.0.dev0"` becomes `"5.10.0"`. A version carrying no developmental
    segment is returned unchanged, so the call is safe to apply blindly.
    """
    return DEV_SUFFIX_RE.sub("", version)
