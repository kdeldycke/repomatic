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

import calendar
import time
from datetime import UTC, datetime

from click_extra import format_size

TYPE_CHECKING = False
if TYPE_CHECKING:
    from datetime import date

MONTHS_PER_YEAR = 12
"""Months in a year."""

SECONDS_PER_MINUTE = 60
"""Seconds in a minute."""

SECONDS_PER_HOUR = 60 * SECONDS_PER_MINUTE
"""Seconds in an hour."""

SECONDS_PER_DAY = 24 * SECONDS_PER_HOUR
"""Seconds in a day: what turns an mtime delta into whole days."""

SECONDS_PER_WEEK = 7 * SECONDS_PER_DAY
"""Seconds in a week."""

SECONDS_PER_MONTH = 30.5 * SECONDS_PER_DAY
"""Seconds in a mean month: past it, {func}`format_relative` counts no weeks."""

SECONDS_PER_YEAR = 365 * SECONDS_PER_DAY
"""Seconds in a common year."""


def parse_iso_datetime(value: str | None) -> datetime | None:
    """Parse an ISO 8601 / RFC 3339 timestamp into a timezone-aware datetime.

    The package-wide parser for any timestamp an external service writes:
    {mod}`repomatic.deps.report` reads PyPI upload times,
    {mod}`repomatic.deps.lockfile` lock timestamps, {mod}`repomatic.cloudflare.api`
    token expiries and {mod}`repomatic.github.job_timings` job clocks through
    it, so every consumer tolerates the same shapes. A JSON or TOML lookup that
    found no value hands over `None`, which reads as no timestamp.

    {meth}`~datetime.datetime.fromisoformat` reads a `Z` suffix and a
    nanosecond fraction since Python 3.11, and truncates the digits `datetime`
    cannot hold. An instant with no offset is read as UTC, the one zone the
    services above write in.

    :param value: An ISO 8601 / RFC 3339 instant, or empty.
    :return: A timezone-aware {class}`~datetime.datetime`, or `None` when
        *value* is empty or not a valid instant.
    """
    if not value:
        return None
    try:
        instant = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return instant if instant.tzinfo else instant.replace(tzinfo=UTC)


def today_utc() -> date:
    """Today's date, in UTC.

    The one read of the clock behind every date this package stamps: a release
    heading, a scan record, a cooldown reference. UTC, so a runner and a laptop
    in another timezone agree on the day. Callers that write the date out use
    `today_utc().isoformat()`, which is the `YYYY-MM-DD` form.
    """
    return datetime.now(UTC).date()


def utc_midnight(day: date) -> datetime:
    """The UTC instant *day* starts on.

    What a date-granular datasource hands {func}`format_countdown` and
    {func}`format_elapsed`, which take instants so no caller can silently drop
    a precision it holds. A date holds none to drop: midnight is the whole of
    what it says.
    """
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


def shift_months(instant: datetime, months: int) -> datetime:
    """Move *instant* by *months* calendar months.

    The day is clamped to the target month, so one month before March 31 is
    February 28, or 29 in a leap year. The time of day is kept.

    :param instant: The instant to move.
    :param months: Calendar months to add, negative to subtract.
    :return: The moved instant.
    """
    index = instant.year * MONTHS_PER_YEAR + instant.month - 1 + months
    year, month = divmod(index, MONTHS_PER_YEAR)
    month += 1
    day = min(instant.day, calendar.monthrange(year, month)[1])
    return instant.replace(year=year, month=month, day=day)


def _calendar_months(later: datetime, earlier: datetime) -> int:
    """Whole calendar months from *earlier* to *later*, capped at a year.

    A remainder past 14 days counts as one more month.
    """
    months = (later.year - earlier.year) * MONTHS_PER_YEAR
    months += later.month - earlier.month
    while shift_months(earlier, months) > later:
        months -= 1
    if (later - shift_months(earlier, months)).days > 14:
        months += 1
    return min(months, MONTHS_PER_YEAR)


def format_relative(instant: datetime, reference: datetime) -> str:
    """Phrase the gap from *reference* to *instant*: `in 5 hours`, `2 days ago`.

    The buckets and their wording follow the `humanize()` of arrow `1.4`. Under
    ten seconds the gap is `just now`. Then come seconds, `a minute` under two,
    minutes, `an hour` under two, hours, `a day` under two, and days under a
    week. Past a week, whole calendar months come first, up to a year, and a
    remainder past 14 days counts as one more month: 15 days already read as
    `a month`. When no month counts, the gap reads `a week`, or `2 weeks` once
    it reaches 14 days. Past a year, it reads `a year` under two, then years.
    A count is truncated, never rounded, and starts at two: a single unit has
    its own phrase.

    :param instant: The instant phrased.
    :param reference: The instant the gap is measured from, usually now.
    :return: `in {gap}` for an *instant* after *reference*, `{gap} ago` for one
        before it, or `just now`.
    """
    delta = round((instant - reference).total_seconds())
    distance = abs(delta)
    if distance < 10:
        return "just now"
    if distance < SECONDS_PER_MINUTE:
        phrase = f"{distance} seconds"
    elif distance < 2 * SECONDS_PER_MINUTE:
        phrase = "a minute"
    elif distance < SECONDS_PER_HOUR:
        phrase = f"{max(distance // SECONDS_PER_MINUTE, 2)} minutes"
    elif distance < 2 * SECONDS_PER_HOUR:
        phrase = "an hour"
    elif distance < SECONDS_PER_DAY:
        phrase = f"{max(distance // SECONDS_PER_HOUR, 2)} hours"
    elif distance < 2 * SECONDS_PER_DAY:
        phrase = "a day"
    elif distance < SECONDS_PER_WEEK:
        phrase = f"{max(distance // SECONDS_PER_DAY, 2)} days"
    else:
        months = _calendar_months(max(instant, reference), min(instant, reference))
        if months >= 1 and distance < SECONDS_PER_YEAR:
            phrase = "a month" if months == 1 else f"{months} months"
        elif distance < 2 * SECONDS_PER_WEEK:
            phrase = "a week"
        elif distance < SECONDS_PER_MONTH:
            phrase = f"{max(distance // SECONDS_PER_WEEK, 2)} weeks"
        elif distance < 2 * SECONDS_PER_YEAR:
            phrase = "a year"
        else:
            phrase = f"{max(distance // SECONDS_PER_YEAR, 2)} years"
    return f"in {phrase}" if delta > 0 else f"{phrase} ago"


def _dated_delta(instant: datetime, reference: datetime) -> str:
    """*instant* as a date, with the phrase for the gap to *reference*.

    The half {func}`format_countdown` and {func}`format_elapsed` share. Both
    read the same gap, and differ only in which direction they refuse to
    phrase.
    """
    return f"{instant.strftime('%Y-%m-%d')} ({format_relative(instant, reference)})"


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

    A thin binding of {func}`click_extra.format_size` to the JEDEC unit style:
    binary powers with the customary `KB`/`MB` symbols.
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
