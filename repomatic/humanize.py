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

"""Conversions between raw machine quantities and their human forms.

Byte counts, modification times and service timestamps reach the user through
more than one surface (the image-optimization summary, the `repomatic cache`
tables, every dependency report), and each surface should spell them the same
way. One home for those conversions keeps the wording consistent and keeps the
formatters out of the modules that merely happen to be the first consumer;
{func}`parse_iso_datetime` is the machine-to-`datetime` half the renderers
start from.

A leaf module with no project-internal imports, so any module can import it
without risking a cycle.
"""

from __future__ import annotations

import time

import arrow
from click_extra import format_size

TYPE_CHECKING = False
if TYPE_CHECKING:
    from datetime import datetime

SECONDS_PER_DAY = 86400
"""Divisor turning an mtime delta into whole days."""


def parse_iso_datetime(value: str) -> datetime | None:
    """Parse an ISO 8601 / RFC 3339 timestamp into a timezone-aware datetime.

    The package-wide parser for any timestamp an external service writes:
    {mod}`repomatic.dep_report` reads PyPI upload times, {mod}`repomatic.uv`
    lock timestamps, {mod}`repomatic.cloudflare` token expiries and
    {mod}`repomatic.github.job_timings` job clocks through it, so every
    consumer tolerates the same shapes.

    Uses arrow, so a nanosecond fractional second and a `Z` suffix (both of
    which Python 3.10's stdlib `datetime.fromisoformat` rejects) parse cleanly;
    sub-microsecond precision is truncated to fit `datetime`.

    arrow also supplies the `.humanize()` relative-time phrasing used in the
    sync report.

    ```{todo}
    Switch this parser back to whenever, the prior implementation, once it
    grows a humanizer:
    [whenever#277](https://github.com/ariebovenberg/whenever/discussions/277).
    ```

    :param value: An ISO 8601 / RFC 3339 instant, or empty.
    :return: A timezone-aware {class}`~datetime.datetime`, or `None` when
        *value* is empty or not a valid instant.
    """
    if not value:
        return None
    try:
        return arrow.get(value).datetime
    except (ValueError, TypeError):
        return None


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
