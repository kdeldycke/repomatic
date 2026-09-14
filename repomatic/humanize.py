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
from datetime import datetime, timezone

import arrow
from click_extra import format_size

TYPE_CHECKING = False
if TYPE_CHECKING:
    from datetime import date

SECONDS_PER_DAY = 86400
"""Divisor turning an mtime delta into whole days."""


def parse_iso_datetime(value: str) -> datetime | None:
    """Parse an ISO 8601 / RFC 3339 timestamp into a timezone-aware datetime.

    The package-wide parser for any timestamp an external service writes:
    {mod}`repomatic.deps.dep_report` reads PyPI upload times, {mod}`repomatic.deps.uv`
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


def utc_midnight(day: date) -> datetime:
    """The UTC instant *day* starts on.

    What a date-granular datasource hands {func}`format_countdown` and
    {func}`format_elapsed`, which take instants so no caller can silently drop
    a precision it holds. A date holds none to drop: midnight is the whole of
    what it says.
    """
    return datetime(day.year, day.month, day.day, tzinfo=timezone.utc)


def _dated_delta(instant: datetime, reference: datetime) -> str:
    """*instant* as a date, with arrow's phrase for the gap to *reference*.

    The half {func}`format_countdown` and {func}`format_elapsed` share. Both
    read the same gap, and differ only in which direction they refuse to
    phrase.
    """
    phrase = arrow.get(instant).humanize(arrow.get(reference))
    return f"{instant.strftime('%Y-%m-%d')} ({phrase})"


def format_countdown(deadline: datetime, reference: datetime) -> str:
    """Render *deadline* as a date, with a countdown to it.

    The countdown measures the two instants, not their calendar dates. A
    deadline later today then reads `in 5 hours`, where truncating to dates
    rendered `just now`: a reader takes that for something already elapsed,
    the opposite of what the line reports.

    Both parameters are instants for that reason, and a {class}`~datetime.date`
    is rejected by the type checker. Every deadline this renders is computed at
    instant granularity (uv records a package `upload-time` to the second, and
    a cooldown cutoff is `now - span`), so a caller holding that precision must
    not drop it. A date-granular datasource passes the UTC midnight its date
    starts on through {func}`utc_midnight`, on *both* arguments: an instant
    measured against a midnight reference is off by up to a day.

    :param deadline: The instant counted down to.
    :param reference: The current instant, for the relative offset.
    :return: A string like `2026-06-25 (in 4 days)` or `2026-06-25 (in 5
        hours)`, or the bare date once the deadline has passed.
    """
    if deadline <= reference:
        return deadline.strftime("%Y-%m-%d")
    return _dated_delta(deadline, reference)


def format_elapsed(instant: datetime, reference: datetime) -> str:
    """Render *instant* as a date, with how long ago it happened.

    The mirror of {func}`format_countdown`, for a date a reader looks back at
    rather than waits for: a package upload, a release, a recorded reading. The
    same precision rules apply, since the two read the same gap.

    Each drops the phrase in the direction it cannot speak for. A countdown
    says nothing about a deadline already passed, and this says nothing about
    an instant still ahead, where `in 3 days` would contradict the column it
    sits in. Both then render the bare date.

    :param instant: The instant being looked back at.
    :param reference: The current instant, for the relative offset.
    :return: A string like `2026-06-24 (2 days ago)` or `2026-06-24 (6 hours
        ago)`, or the bare date while *instant* is still ahead.
    """
    if instant > reference:
        return instant.strftime("%Y-%m-%d")
    return _dated_delta(instant, reference)


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
