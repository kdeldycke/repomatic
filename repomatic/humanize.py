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

"""Human-readable renderings of raw file-system quantities.

Byte counts and modification times reach the user through more than one surface
(the image-optimization summary, the `repomatic cache` tables), and each surface
should spell them the same way. One home for those conversions keeps the wording
consistent and keeps the formatters out of the modules that merely happen to be
the first consumer.

Dependency-free beyond `click_extra`, so any module can import it without
risking a cycle.
"""

from __future__ import annotations

import time

from click_extra import format_size

SECONDS_PER_DAY = 86400
"""Divisor turning an mtime delta into whole days."""


def format_file_size(size_bytes: int) -> str:
    """Format a byte count as a human-readable string.

    A thin binding of {func}`click_extra.format_size` to the JEDEC unit style
    (binary powers with the customary `KB`/`MB` symbols), matching the format
    produced by `calibreapp/image-actions`.
    """
    return format_size(size_bytes, units="jedec")


def format_age(mtime: float) -> str:
    """Format a file mtime as a human-readable age string.

    Rounds down to whole days, since the cache tables it feeds exist to answer
    "is this stale?", not to time anything precisely.

    :param mtime: POSIX timestamp, as returned by `Path.stat().st_mtime`.
    :return: `"today"`, `"1 day"`, or `"{n} days"`.
    """
    age_days = int((time.time() - mtime) / SECONDS_PER_DAY)
    if age_days == 0:
        return "today"
    if age_days == 1:
        return "1 day"
    return f"{age_days} days"
