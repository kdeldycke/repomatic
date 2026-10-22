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

"""The `exclude-newer` cooldown and its per-package bypasses.

`[tool.uv] exclude-newer` holds every resolution a window behind the index, and
an `exclude-newer-package` entry lets one package through ahead of it. This
module owns that lifecycle:

- {class}`ExemptionValue` reads one entry, whatever form it is written in.
- {class}`UvSettings` edits the `[tool.uv]` table: each command reads
  `pyproject.toml` once, and writes it once.
- The prune and freeze passes age the entries against the lock.
- The forecasts date the day each freeze clears.
- {func}`compute_held_back_packages` probes the releases the window holds back.
- {func}`sync_uv_lock` drives all of it for `sync-uv-lock`.

The markdown rendering of these results lives in {mod}`repomatic.deps.report`.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from enum import Enum

import tomlrt
from click_extra import parse_friendly_duration, parse_iso8601_duration
from tomlrt import Table

from ..humanize import format_countdown, parse_iso_datetime, utc_midnight
from ..pyproject import uv_table
from ..versions import is_newer
from .lockfile import UTC_TIMESTAMP_FORMAT, LockFile, diff_lock_versions
from .report import (
    BYPASS_NEEDS_RELEASE,
    BypassForecast,
    HeldBackPackage,
    format_released,
)
from .requirements import pep503_pattern
from .uv import run_uv, uv_lock_command

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping
    from pathlib import Path
    from typing import Any

EXEMPTION_KEY = "exclude-newer-package"
"""The `[tool.uv]` key holding the per-package bypasses."""

LIFT_SPAN = "0 day"
"""The entry value that lifts the cooldown for a package for good.

Written for a package the lock holds from a git or path source: it has no
upload time, so no freeze can hold it.
"""

_BARE_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
"""A calendar date with no time of day."""


def _parse_span(value: str) -> timedelta | None:
    """Parse a relative span in either of the two forms uv accepts.

    Each parser is tested against `None`: a `0 days` span parses to
    `timedelta(0)`, which is falsy.

    :param value: A friendly duration (`1 week`) or an ISO 8601 one (`P7D`).
    :return: The span, or `None` when *value* is no span.
    """
    span = parse_friendly_duration(value)
    return span if span is not None else parse_iso8601_duration(value)


def resolve_exclude_newer_span(value: str) -> timedelta | None:
    """Resolve a `[tool.uv].exclude-newer` value to the width of its window.

    The rolling half of {func}`resolve_exclude_newer_cutoff`. Two of the three
    forms uv accepts name a *span*, so the cutoff moves with every resolution
    and a release inside the window ages out of it on a date anyone can
    compute: that date is what the cooldown forecasts report. The third form,
    an absolute timestamp, names a fixed instant no release ever ages past, so
    it has no span and nothing to forecast.

    :param value: The string read from `[tool.uv].exclude-newer`.
    :return: The window width, or `None` when *value* is empty or absolute.
    """
    if not value:
        return None
    return _parse_span(value)


def resolve_exclude_newer_cutoff(
    value: str, now: datetime | None = None
) -> datetime | None:
    """Resolve a `[tool.uv].exclude-newer` value to an absolute cutoff datetime.

    [uv accepts three forms](https://github.com/astral-sh/uv/pull/19475)
    in this field:

    - A "friendly" duration (`24 hours`, `30 minutes`, `1 day`, `1 week`):
      subtracted from the current UTC time.
    - An ISO 8601 duration (`PT24H`, `P7D`, `P30D`, `P1W`, combinations
      like `P1DT2H`): subtracted from the current UTC time.
    - An RFC 3339 / ISO 8601 timestamp (`2026-03-18T16:39:02Z`): returned
      verbatim as the cutoff.

    Forms are tried in the order above so a duration is never mistaken
    for a timestamp.

    :param value: The string read from `[tool.uv].exclude-newer` in
        `pyproject.toml`.
    :param now: The instant a span counts back from. Read from the clock when
        `None`.
    :return: An absolute cutoff datetime, or `None` if *value* is empty
        or matches none of the recognized forms.
    """
    if not value:
        return None
    span = _parse_span(value)
    if span is not None:
        return (now or datetime.now(UTC)) - span
    return parse_iso_datetime(value)


def date_to_utc_cutoff(day: date) -> str:
    """Render an `exclude-newer-package` cutoff date as an explicit UTC instant.

    ```{warning}
    uv reads a bare `YYYY-MM-DD` in `exclude-newer-package` as the start of
    the *following* day in the locking machine's local timezone, then writes
    that absolute instant into `uv.lock`'s `[options.exclude-newer-package]`
    block. The same date therefore lands as a different timestamp depending on
    where `uv lock` ran: `2026-06-13` becomes `2026-06-14T00:00:00Z` on a UTC
    CI runner but `2026-06-13T20:00:00Z` on a UTC+4 laptop. Every local lock
    then flips the value one way and every CI lock flips it back: an endless
    `sync-uv-lock` ping-pong.
    ```

    Pinning the cutoff to that same next-day-midnight boundary expressed in
    UTC removes the ambiguity: uv stores a full RFC 3339 timestamp verbatim,
    identically on every machine.

    :param day: The cutoff date (the bare date uv would otherwise expand).
    :return: A `YYYY-MM-DDT00:00:00Z` timestamp at the start of the day after
        *day*, matching uv's exclusive end-of-day expansion pinned to UTC.
    """
    return utc_midnight(day + timedelta(days=1)).strftime(UTC_TIMESTAMP_FORMAT)


def freeze_cutoff_after(day: date) -> str:
    """The `exclude-newer-package` cutoff that holds a version uploaded on *day*.

    A cooldown bypass must *hold* the locked version, not track the latest
    release, which is what a `"0 day"` span does: it disables the cooldown, so
    `uv lock --upgrade` keeps pulling newer releases and the entry never ages
    out. The cutoff this returns sits a little past the upload, so uv keeps that
    version and rejects releases published after the window.

    The cutoff rounds up to a whole-day UTC boundary through
    {func}`date_to_utc_cutoff`: the second UTC midnight after the upload, so a
    version uploaded on `2026-07-01` freezes at `2026-07-03T00:00:00Z`. A full
    UTC timestamp, rather than a bare `YYYY-MM-DD` date that uv re-expands per
    locking-machine timezone, stops `uv.lock` from ping-ponging between local
    and CI locks. Rounding *up* by a day keeps the held version inside its own
    window: uv's cutoff is exclusive ("uploaded prior to") and PyPI upload times
    carry sub-second precision, so a cutoff pinned at the upload instant risks
    landing before it and excluding the very version meant to be held.

    ```{note}
    The margin makes the freeze hold a **window**, not a single version: a
    release published later the same day, or anywhere on the following
    calendar day, falls before the cutoff and is adopted on the next
    `uv lock --upgrade`. This is deliberate and accepted: the absorbed
    release is a strictly newer build of a package just chosen to bypass the
    global cooldown, so taking its immediate follow-up is low-risk, and the
    dependency floor still sets the minimum acceptable version. Holding
    *exactly* the locked version would mean pinning the cutoff to the upload
    instant plus an epsilon, trading this safety margin for the precision
    fragility above.
    ```

    The single source of that policy, shared with
    {class}`repomatic.deps.sources.ReleaseSwap`.

    :param day: The held version's upload date.
    :return: A `YYYY-MM-DDT00:00:00Z` cutoff timestamp.
    """
    return date_to_utc_cutoff(day + timedelta(days=1))


class ExemptionKind(Enum):
    """The forms an `exclude-newer-package` value takes."""

    LIFT = "lift"
    """The `false` boolean: the cooldown is lifted for the package.

    uv's spelling of what a zero span says, so every pass reads it as one.
    """

    SPAN = "span"
    """A relative span (`0 day`, `P7D`), counted back from each resolution."""

    DATE = "date"
    """A bare `YYYY-MM-DD` date, which uv expands per locking-machine timezone.

    See {func}`date_to_utc_cutoff`.
    """

    TIMESTAMP = "timestamp"
    """A full RFC 3339 instant: a freeze."""


@dataclass(frozen=True)
class ExemptionValue:
    """One `exclude-newer-package` value, read the way uv reads it.

    The one place that tells the forms apart, so the prune, the freeze, the
    forecasts and the admission check all classify an entry the same way.
    """

    raw: object
    """The value as written: a string, or the `false` boolean."""

    kind: ExemptionKind | None
    """The form of {attr}`raw`, or `None` when no form matches."""

    span: timedelta | None = None
    """The width of a {attr}`ExemptionKind.SPAN` value."""

    day: date | None = None
    """The date of a {attr}`ExemptionKind.DATE` value."""

    instant: datetime | None = None
    """The UTC instant a {attr}`ExemptionKind.TIMESTAMP` value names, or the UTC
    end of the day a {attr}`ExemptionKind.DATE` value names."""

    @classmethod
    def parse(cls, raw: object) -> ExemptionValue:
        """Classify a raw value read from `[tool.uv].exclude-newer-package`."""
        if raw is False:
            return cls(raw, ExemptionKind.LIFT)
        text = str(raw).strip()
        span = _parse_span(text)
        if span is not None:
            return cls(raw, ExemptionKind.SPAN, span=span)
        if _BARE_DATE_RE.fullmatch(text):
            try:
                day = date.fromisoformat(text)
            except ValueError:
                return cls(raw, None)
            return cls(
                raw,
                ExemptionKind.DATE,
                day=day,
                instant=utc_midnight(day + timedelta(days=1)),
            )
        instant = parse_iso_datetime(text)
        if instant is None:
            return cls(raw, None)
        return cls(raw, ExemptionKind.TIMESTAMP, instant=instant)

    @property
    def tracks_latest(self) -> bool:
        """Whether the entry follows the latest release, rather than holding one.

        A lift and a span both do. That is what separates a bypass, which the
        freeze pass pins, from a freeze, which already holds.
        """
        return self.kind in (ExemptionKind.LIFT, ExemptionKind.SPAN)

    @property
    def fixed_cutoff(self) -> datetime | None:
        """The instant a freeze stops admitting releases.

        Only a full timestamp has one this package trusts. A bare date names an
        instant that depends on the timezone of the locking machine, so the
        freeze pass pins it to UTC first, and the next run reads the pinned
        form. A lift and a span track the latest release, and have none.
        """
        return self.instant if self.kind is ExemptionKind.TIMESTAMP else None

    @property
    def pinned(self) -> str | None:
        """The explicit UTC timestamp of a bare date, `None` for other kinds."""
        if self.day is None:
            return None
        return date_to_utc_cutoff(self.day)

    def admits(self, released: date, now: datetime) -> bool:
        """Whether the entry admits a release dated *released*.

        Reads each form the way uv does, so the answer matches what an install
        under the entry resolves:

        - A lift admits every release.
        - A span admits every upload older than the span.
        - A bare date admits uploads up to the end of that day, read in UTC as
          {func}`date_to_utc_cutoff` explains.
        - A full timestamp admits uploads before that instant.

        A datasource reports the day of a release, not the instant of its
        upload. So the entry admits the release only when its cutoff is at or
        after the end of that day, or at or after *now* when *now* comes first:
        no upload of the release is later than either. A cutoff inside the
        release day can admit the release or not, and it counts as not.

        :param released: The release date the datasource reports.
        :param now: The current instant, which a span counts back from.
        :return: `True` when every upload of the release is older than the
            cutoff. `False` for an earlier cutoff, and for a value no form
            matches.
        """
        if self.kind is ExemptionKind.LIFT:
            return True
        cutoff = now - self.span if self.span is not None else self.instant
        if cutoff is None:
            return False
        return cutoff >= min(utc_midnight(released + timedelta(days=1)), now)


def exemption_admits(value: object, released: date, now: datetime) -> bool:
    """Whether a raw `exclude-newer-package` value admits a release.

    For a caller holding the value as `tomlrt` read it. See
    {meth}`ExemptionValue.admits`.
    """
    return ExemptionValue.parse(value).admits(released, now)


def _inline_table(entries: Mapping[str, Any]) -> Table:
    """Build the inline table pyproject-fmt writes, keys sorted."""
    return Table.inline({key: entries[key] for key in sorted(entries)})


def _mentions_package(text: str, name: str) -> bool:
    """Whether *text* names the package *name*, in any PEP 503 spelling."""
    pattern = rf"(?<![\w.-]){pep503_pattern(name)}(?![\w-])"
    return re.search(pattern, text, re.IGNORECASE) is not None


class UvSettings:
    """The `[tool.uv]` table of one `pyproject.toml`, read once, written once.

    Every pass of the cooldown lifecycle edits the same in-memory document, and
    {meth}`save` writes it back. A command therefore reads the file once and
    writes it at most once, however many passes it runs.

    A missing file reads as an empty one, and is never created: there is no
    table to edit.
    """

    def __init__(
        self, path: Path, original: str | None, text: str | None = None
    ) -> None:
        self.path = path
        """The `pyproject.toml` the settings are read from and saved to."""

        self._original = original
        """The text on disk, or `None` when the file does not exist."""

        self.doc: Any = tomlrt.loads(text if text is not None else original or "")
        """The editable document, formatting trivia included."""

    @classmethod
    def load(cls, path: Path, text: str | None = None) -> UvSettings:
        """Read the settings of *path*.

        :param path: The `pyproject.toml` file.
        :param text: The content to edit, when the caller already changed it in
            memory (the bundled `[tool.uv]` policy pins, say). Defaults to the
            file's own content. {meth}`save` compares against the file either
            way.
        """
        original = path.read_text(encoding="UTF-8") if path.exists() else None
        return cls(path, original, text)

    @property
    def table(self) -> Any:
        """The `[tool.uv]` table, or an empty mapping when the file has none."""
        return uv_table(self.doc)

    @property
    def window(self) -> str:
        """The project's own `exclude-newer` window, verbatim, or empty when unset.

        ```{caution}
        Always pass this back to `uv lock` and `uv sync` as an explicit
        `--exclude-newer` flag rather than letting uv pick the value up from
        `pyproject.toml` on its own. CI exports a `UV_EXCLUDE_NEWER` covering
        every ad-hoc install (see `docs/workflows.md` § Install-time cooldown),
        and that environment variable *outranks* `[tool.uv]`: left implicit, a
        CI lock would resolve against the ambient window while a developer
        running the same command locally resolves against this one, and
        `sync-uv-lock` would churn between the two. A CLI flag outranks the
        environment, which pins the project's own policy. {meth}`lock_command`
        does it.
        ```
        """
        window = self.table.get("exclude-newer", "")
        return str(window) if window else ""

    def entries(self) -> dict[str, ExemptionValue]:
        """The `exclude-newer-package` entries, keyed as the project spells them."""
        table = self.table.get(EXEMPTION_KEY) or {}
        return {name: ExemptionValue.parse(value) for name, value in table.items()}

    def lock_command(
        self, *extra: str, exclude_newer_packages: Mapping[str, str] | None = None
    ) -> list[str]:
        """A `uv lock` argv carrying the project's own window. See {attr}`window`.

        :param extra: Extra `uv lock` arguments.
        :param exclude_newer_packages: Per-package windows for this run only.
        """
        return uv_lock_command(
            *extra,
            exclude_newer=self.window,
            exclude_newer_packages=exclude_newer_packages,
        )

    def _write_entries(self, entries: Mapping[str, Any]) -> None:
        """Replace the `exclude-newer-package` table, or drop it when empty."""
        if entries:
            self.table[EXEMPTION_KEY] = _inline_table(entries)
        else:
            del self.table[EXEMPTION_KEY]

    def prune(self, lock: LockFile, now: datetime) -> dict[str, ExemptionValue]:
        """Remove the stale `exclude-newer-package` entries.

        ```{todo}
        Delete this pruning pass once uv prunes stale `exclude-newer-package`
        entries natively:
        [uv#18792](https://github.com/astral-sh/uv/issues/18792).
        ```

        An entry is stale when its locked version's upload time falls before the
        `exclude-newer` cutoff, meaning `uv lock --upgrade` would resolve to the
        same (or newer) version without the override.

        Packages the lock holds without an upload time (git or path sources)
        are treated as permanent exemptions and never pruned.

        A package the lock does not hold at all has no locked version to age: a
        tool the registry pins outside the lockfile, or a dependency the
        project dropped. Its entry ages by its own cutoff instead. A fixed
        timestamp is stale once the `exclude-newer` cutoff has passed it,
        because the entry then admits no release the cooldown still withholds.
        A span or a lift never ages, and stays.

        ```{caution}
        A fixed cutoff replaces the rolling window for its package: uv resolves
        nothing uploaded after it. An entry left in place past its date
        therefore blocks every later release of that package, which is why
        this pass removes it and does not leave the cleanup to a reviewer.
        ```

        :param lock: The lock the entries hold their versions in.
        :param now: The instant the rolling window counts back from.
        :return: The pruned entries, keyed by name, with the values they had.
        """
        entries = self.entries()
        window = self.window
        if not entries or not window:
            return {}
        cutoff = resolve_exclude_newer_cutoff(window, now)
        if cutoff is None:
            logging.warning(
                f"Cannot parse exclude-newer value {window!r}; skipping prune."
            )
            return {}

        stale: dict[str, ExemptionValue] = {}
        for name, value in entries.items():
            if not lock.version_of(name):
                # The lock holds no version of the package, so the entry ages
                # by its own cutoff. See the docstring.
                fixed = value.fixed_cutoff
                if fixed is not None and fixed <= cutoff:
                    stale[name] = value
                    logging.info(
                        f"Pruning {name}: not in the lock, and its cutoff"
                        f" {value.raw} is before the cooldown cutoff."
                    )
                else:
                    logging.debug(
                        f"Keeping {name}: not in the lock, cutoff {value.raw}."
                    )
                continue
            upload = lock.uploaded_at(name)
            if upload is None:
                # No upload time: git or path source, a permanent exemption.
                logging.debug(f"Keeping {name}: no upload time in the lock.")
            elif upload < cutoff:
                stale[name] = value
                logging.info(f"Pruning {name}: upload time {upload} is before cutoff.")
            else:
                logging.debug(f"Keeping {name}: upload time {upload} is after cutoff.")

        if stale:
            self._write_entries({
                name: value.raw for name, value in entries.items() if name not in stale
            })
            logging.info(
                f"Pruned {', '.join(sorted(stale))} from {EXEMPTION_KEY}"
                f" in {self.path}."
            )
        else:
            logging.debug(f"No stale {EXEMPTION_KEY} entries.")
        return stale

    def freeze(self, lock: LockFile) -> set[str]:
        """Rewrite the bypasses that track the latest release into freezes.

        A span or a lift tells uv to ignore the cooldown and resolve to the
        *latest* release, so the package keeps moving and {meth}`prune` never
        sees its locked version age out. Rewriting the entry as the
        {func}`freeze_cutoff_after` cutoff of the locked version instead
        *holds* the package: releases past the freeze window are excluded until
        the held version ages past the global cooldown, at which point the
        entry is pruned and the package rejoins normal resolution.

        Also pins any bare `YYYY-MM-DD` entry to the equivalent explicit UTC
        timestamp (see {func}`date_to_utc_cutoff`), so uv stops re-expanding it
        per locking-machine timezone. Entries already carrying a full timestamp
        are left untouched, which keeps the pass idempotent. Packages with no
        upload time in the lock (git or path sources) keep their span: they
        have no release to freeze against. An entry finds its locked release in
        any PEP 503 spelling of its key, and keeps the key as written.

        :param lock: The lock holding the versions to freeze at.
        :return: The names of the entries rewritten.
        """
        entries = self.entries()
        rewritten: dict[str, str] = {}
        for name, value in entries.items():
            if value.tracks_latest:
                upload = lock.uploaded_at(name)
                if upload is not None:
                    rewritten[name] = freeze_cutoff_after(upload.date())
                    logging.info(
                        f"Freezing {name} cooldown bypass at {rewritten[name]}."
                    )
            elif value.pinned is not None:
                rewritten[name] = value.pinned
                logging.info(
                    f"Pinning {name} cooldown date {value.raw} to {value.pinned}."
                )
        if rewritten:
            self._write_entries({
                name: rewritten.get(name, value.raw) for name, value in entries.items()
            })
        return set(rewritten)

    def upsert(self, entries: Mapping[str, str]) -> bool:
        """Insert or replace `exclude-newer-package` entries.

        :param entries: Package name to cutoff value (a freeze timestamp or a
            span). Existing entries for the same names are overwritten.
        :return: `True` when the table changed. `False` when nothing needed to,
            and when the project has no `[tool.uv]` cooldown to attach the
            entries to.
        """
        uv = self.doc.get("tool", {}).get("uv")
        if uv is None:
            logging.warning(
                f"No [tool.uv] found in {self.path}. Cannot persist cooldown"
                " exemptions."
            )
            return False
        table = uv.get(EXEMPTION_KEY)
        if table is None and "exclude-newer" not in uv:
            logging.warning(
                f"No [tool.uv] exclude-newer or {EXEMPTION_KEY} found in"
                f" {self.path}. Cannot persist cooldown exemptions."
            )
            return False

        merged = dict(table) if table is not None else {}
        if all(merged.get(name) == value for name, value in entries.items()):
            logging.debug(f"All {EXEMPTION_KEY} entries already up to date.")
            return False
        merged.update(entries)
        uv[EXEMPTION_KEY] = _inline_table(merged)
        if table is None:
            # Right after `exclude-newer`, which is where pyproject-fmt orders
            # it in `[tool.uv]`. Appended at the end, the next format-pyproject
            # run would move it, leaking this edit into a follow-up PR.
            order = [key for key in uv if key != EXEMPTION_KEY]
            order.insert(order.index("exclude-newer") + 1, EXEMPTION_KEY)
            uv.sort(key=order.index)
        logging.info(
            f"Upserted {', '.join(sorted(entries))} in {EXEMPTION_KEY} in {self.path}."
        )
        return True

    def exempt(self, names: Iterable[str], lock: LockFile, now: datetime) -> set[str]:
        """Freeze each of *names* whose locked release is still inside the window.

        A package needs an entry only when its locked version was uploaded
        after the `exclude-newer` cutoff, so that a regular `uv lock --upgrade`
        would not resolve it. Each new entry holds that version: see
        {func}`freeze_cutoff_after`. A package with no upload time in the lock
        (a git or path source) gets a permanent {data}`LIFT_SPAN` instead. A
        project with no window, or one that does not parse, exempts every name
        to be safe. A name that already has an entry keeps it.

        :param names: Package names, in any PEP 503 spelling. Each entry keeps
            the spelling it is given.
        :param lock: The lock holding the versions to freeze at.
        :param now: The instant the rolling window counts back from.
        :return: The names of the entries added.
        """
        cutoff = resolve_exclude_newer_cutoff(self.window, now)
        needed = set()
        for name in names:
            upload = lock.uploaded_at(name)
            if cutoff is None or upload is None or upload >= cutoff:
                logging.info(
                    f"Exempting {name}: its locked release is inside the window."
                )
                needed.add(name)
            else:
                logging.info(
                    f"Skipping exemption for {name}: upload time {upload} already"
                    " clears the exclude-newer cutoff."
                )
        to_add = needed - set(self.entries())
        if not to_add:
            return set()
        additions: dict[str, str] = {}
        for name in to_add:
            upload = lock.uploaded_at(name)
            additions[name] = (
                freeze_cutoff_after(upload.date()) if upload is not None else LIFT_SPAN
            )
        return to_add if self.upsert(additions) else set()

    def comment_mentions(self, names: Iterable[str]) -> list[str]:
        """Return the *names* the comments on `exclude-newer-package` still mention.

        Pruning rebuilds the inline table but keeps the comment block above its
        key, because that block describes the table as a whole. A sentence
        written to justify one entry therefore outlives the entry, and reads as
        a live exemption to the next person who opens the file. Rewriting prose
        is not safe to automate, so {func}`sync_uv_lock` reports these names for
        a human to edit. A table pruned to nothing loses its comments along
        with its key, so only a partial prune leaves one behind.

        A name matches case-insensitively and with any separator PEP 503 treats
        as equal: `lemon-curd` also finds `lemon_curd`.

        :param names: Package names to look for, typically the entries pruned.
        :return: The names the comments still mention, sorted.
        """
        table = self.table
        if EXEMPTION_KEY not in table:
            return []
        lines = [line for line in table.leading_block.get(EXEMPTION_KEY, ()) if line]
        if eol := table.comments.get(EXEMPTION_KEY):
            lines.append(eol)
        comment = "\n".join(lines)
        return sorted(name for name in names if _mentions_package(comment, name))

    def save(self) -> bool:
        """Write the document back, when it differs from the file on disk.

        :return: `True` when the file was written.
        """
        if self._original is None:
            return False
        text = tomlrt.dumps(self.doc)
        if text == self._original:
            return False
        self.path.write_text(text, encoding="UTF-8")
        self._original = text
        return True


def _forecast(
    lock: LockFile, name: str, value: ExemptionValue, now: datetime
) -> BypassForecast:
    """Forecast the day one `exclude-newer-package` entry clears.

    Mirrors the two conditions {meth}`UvSettings.prune` applies. A package the
    lock holds clears once its upload time plus the rolling span has passed: a
    held version with no upload time can only clear by a release, which the
    forecast marks {data}`~repomatic.deps.report.BYPASS_NEEDS_RELEASE`. A
    package the lock does not hold clears once the entry's own cutoff plus the
    span has passed.

    :param lock: The lock the forecast is about.
    :param name: The package the entry names, in any PEP 503 spelling.
    :param value: The entry.
    :param now: Reference instant for the relative hint. The prune it
        forecasts compares instants, so a freeze lifting later today is hours
        away, not a day.
    :return: The forecast. Its held version is empty for a package the lock
        does not hold, and its expiry is empty when the lock records no span.
    """
    span = lock.cooldown_span
    held_version = lock.version_of(name)
    start = lock.uploaded_at(name) if held_version else value.fixed_cutoff
    if held_version and start is None:
        expires = BYPASS_NEEDS_RELEASE
    elif start is None or span is None:
        expires = ""
    else:
        expires = format_countdown(start + span, now)
    return BypassForecast(name, held_version, expires)


def active_forecasts(
    entries: Mapping[str, ExemptionValue], lock: LockFile, now: datetime
) -> list[BypassForecast]:
    """Forecast when each active freeze clears.

    Covers the freezes alone. A span or a lift tracks the latest release: it is
    a permanent exemption for a package with no release to age against (a git
    or path source), so it never expires and would repeat a static row in every
    report. Auditing those is left to the dependency review (see
    `docs/dependencies.md`).

    :param entries: The `exclude-newer-package` entries.
    :param lock: The lock that describes the state the report is about.
    :param now: Reference instant for the relative hints.
    :return: Forecasts sorted by package name.
    """
    return [
        _forecast(lock, name, entries[name], now)
        for name in sorted(entries)
        if not entries[name].tracks_latest
    ]


def pruned_forecasts(
    pruned: Mapping[str, ExemptionValue], lock: LockFile, now: datetime
) -> list[BypassForecast]:
    """Snapshot the entries a prune cleared, for their `(cleared)` rows.

    *lock* must be the pre-upgrade lock: once an entry is pruned the package
    rejoins normal resolution, so the post-upgrade lock may hold a newer
    version whose upload time would misstate what the freeze held and when it
    aged out.

    :param pruned: The entries {meth}`UvSettings.prune` removed.
    :param lock: The lock before the upgrade.
    :param now: Reference instant for the relative hints.
    :return: One record per entry, sorted by name, with the version the freeze
        held and the (past) day it expired.
    """
    return [_forecast(lock, name, pruned[name], now) for name in sorted(pruned)]


def compute_held_back_packages(
    lock_path: Path, lock: LockFile, bypassed: Iterable[str]
) -> list[HeldBackPackage]:
    """Find releases withheld from the lock only by the cooldown.

    Re-resolves the lock with the cooldown lifted and diffs the result against
    the in-cooldown lock. Both the global `exclude-newer` cutoff and every
    per-package `exclude-newer-package` freeze are raised to the current
    instant, so a release blocked by a cooldown-bypass freeze is reported like
    any cooldown-blocked one. That keeps the section's wording and "Eligible"
    math honest: {meth}`UvSettings.prune` drops a freeze as soon as its held
    version exits the window, so any release a freeze still blocks is
    necessarily inside the global window too, and becomes lockable on its own
    cooldown-exit date. Versions pinned by a specifier or capped by a
    `requires-python` bound resolve identically with and without the lift, so
    they are excluded.

    The probe writes `uv.lock` and restores it byte-for-byte in a `finally`,
    so the canonical in-cooldown lock is left untouched even when resolution
    or parsing fails. Its output is captured, since the probe runs under the
    `sync-deps` progress trail.

    ```{note}
    This runs a second `uv lock` resolution. It is the report's only cost and
    is skipped by `sync-uv-lock --no-held-back`.
    ```

    :param lock_path: Path to the `uv.lock` file.
    :param lock: The parsed lock on disk, the in-cooldown state.
    :param bypassed: The packages carrying an `exclude-newer-package` entry.
    :return: Held-back packages sorted by name. Empty when the probe fails or
        nothing is withheld.
    """
    if not lock_path.exists():
        return []
    saved = lock_path.read_bytes()
    now = datetime.now(UTC)
    cutoff = now.strftime(UTC_TIMESTAMP_FORMAT)
    # A command-line window beats the pyproject.toml entry of the same
    # package, lifting each freeze for the duration of the probe.
    probe = uv_lock_command(
        "--upgrade",
        exclude_newer=cutoff,
        exclude_newer_packages=dict.fromkeys(bypassed, cutoff),
    )
    try:
        run_uv(probe, cwd=lock_path.parent)
        lifted = LockFile.load(lock_path)
        latest = lifted.versions
        uploads = lifted.upload_times
    except subprocess.CalledProcessError as error:
        logging.warning(
            "Cooldown probe (uv lock --exclude-newer) failed; "
            "skipping the held-back report."
        )
        logging.debug(str(error))
        return []
    finally:
        lock_path.write_bytes(saved)

    span = lock.cooldown_span
    held = []
    for name in sorted(lock.versions.keys() & latest.keys()):
        old_version, new_version = lock.versions[name], latest[name]
        if not is_newer(new_version, old_version):
            continue
        raw_upload = uploads.get(name, "")
        eligible = ""
        upload = parse_iso_datetime(raw_upload)
        if upload is not None and span is not None:
            eligible = format_countdown(upload + span, now)
        held.append(
            HeldBackPackage(
                name,
                old_version,
                new_version,
                format_released(raw_upload, now),
                eligible,
            )
        )
    return held


@dataclass
class SyncResult:
    """The outcome of {func}`sync_uv_lock`."""

    changes: list[tuple[str, str, str]]
    """Version changes as `(name, old_version, new_version)` tuples."""

    lock: LockFile
    """The lock the run leaves on disk."""

    pruned_bypasses: list[BypassForecast] = field(default_factory=list)
    """Expired `exclude-newer-package` entries removed from `pyproject.toml`,
    each with the version and (past) expiry the freeze had, snapshot against
    the pre-upgrade lock by {func}`pruned_forecasts`."""

    frozen_bypasses: list[str] = field(default_factory=list)
    """`exclude-newer-package` entries rewritten into freeze cutoffs."""

    stale_bypass_comments: list[str] = field(default_factory=list)
    """Pruned entries the comment above `exclude-newer-package` still names,
    found by {meth}`UvSettings.comment_mentions`."""

    bypass_forecasts: list[BypassForecast] = field(default_factory=list)
    """Active cooldown-bypass freezes with their expiry forecasts (post-run
    state)."""


def sync_uv_lock(lock_path: Path, settings: UvSettings) -> SyncResult:
    """Re-lock with `--upgrade` and report version changes.

    First prunes the stale `exclude-newer-package` entries from
    `pyproject.toml` and freezes the survivors at their locked version, then
    runs `uv lock --upgrade` to update transitive dependencies.

    ```{note}
    When the upgrade changes no package versions and was not driven by a
    `pyproject.toml` cooldown edit, the pre-upgrade lock is restored
    byte-for-byte. `uv lock --upgrade` otherwise rewrites semantically
    equivalent environment markers in a form that varies by uv version and by
    whether the resolution ran fresh or incrementally: a transitive
    dependency reachable only below Python 3.11 has its
    `python_full_version < '3.13'` marker flipped to the equivalent
    `< '3.11'`, or back, with no change to the resolved package set. Committed
    by one machine and re-flipped by the next, that cosmetic churn drives an
    endless `sync-uv-lock` ping-pong of empty PRs. Since the job exists only
    to move dependency *versions* forward, a run that moves none has nothing
    to contribute and is discarded. This mirrors the timezone-pinning fix in
    {func}`date_to_utc_cutoff`.
    ```

    :param lock_path: Path to the `uv.lock` file.
    :param settings: The settings of the project beside the lock.
    :return: The version changes, the lock left on disk, and the
        cooldown-bypass lifecycle: entries pruned, frozen, and still active
        with their expiry forecasts.
    """
    project_dir = lock_path.parent
    now = datetime.now(UTC)

    # Prune the bypasses whose hold has aged past the cooldown, then freeze the
    # survivors at their locked version, so the upgrade below holds them
    # instead of tracking newer releases. Both passes, and the snapshot of the
    # cleared entries, read the lock as it stands before the upgrade.
    pre = LockFile.load(lock_path)
    pruned = settings.prune(pre, now)
    frozen = settings.freeze(pre)
    stale_comments = settings.comment_mentions(pruned)
    if stale_comments:
        logging.warning(
            f"The comment above {EXEMPTION_KEY} in {settings.path} still names"
            f" {', '.join(stale_comments)}, which this run cleared. Update it by"
            " hand."
        )
    settings.save()

    # The raw bytes let a cosmetic-only upgrade be rolled back verbatim.
    lock_before = lock_path.read_bytes() if lock_path.exists() else None
    lock_cmd = settings.lock_command("--upgrade")
    logging.info(f"Running {' '.join(lock_cmd)} in {project_dir}...")
    try:
        run_uv(lock_cmd, cwd=project_dir)
    except subprocess.CalledProcessError as error:
        logging.error(str(error.stderr).strip())
        raise

    post = LockFile.load(lock_path)
    changes = diff_lock_versions(pre.versions, post.versions)

    # With no version change and no cooldown edit behind the run, any remaining
    # diff is uv's marker re-normalization (see the note above). Keeping it
    # would open an empty sync PR that the next machine reverts.
    final = post
    if (
        not changes
        and not (pruned or frozen)
        and lock_before is not None
        and lock_path.read_bytes() != lock_before
    ):
        lock_path.write_bytes(lock_before)
        final = pre
        logging.info(
            "Restored uv.lock: uv lock --upgrade changed no package versions, "
            "only equivalent environment markers. Discarding cosmetic churn."
        )

    return SyncResult(
        changes=changes,
        lock=final,
        pruned_bypasses=pruned_forecasts(pruned, pre, now),
        frozen_bypasses=sorted(frozen),
        stale_bypass_comments=stale_comments,
        bypass_forecasts=active_forecasts(settings.entries(), final, now),
    )
