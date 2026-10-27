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

"""Shared rendering of dependency-update reports.

The markdown diff, held-back, and cooldown-bypass tables, the release-notes
sections, and the comparison URLs that every updater's PR body and terminal
output route through: `sync-uv-lock`, `sync-deps`, `sync-dep-sources`, the
three version-sync bumpers (`sync-tool-versions`, `sync-action-pins`,
`sync-workflow-pins`), and `audit --fix`.

The computations stay with their datasources ({mod}`repomatic.deps.lockfile`
and {mod}`repomatic.deps.cooldown` for the lock, {mod}`repomatic.deps.pins` for
GitHub/PyPI/npm); this module only renders their results.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from ..github.releases import ReleaseNotesRequest, fetch_github_release_notes
from ..humanize import (
    format_countdown,
    format_elapsed,
    parse_iso_datetime,
    utc_midnight,
)
from ..markdown import demote_markdown_headings, sanitize_markdown_mentions
from ..pypi import (
    PYPI_PACKAGE_URL,
    get_changelog_url as get_pypi_changelog_url,
    get_source_url as get_pypi_source_url,
    pypi_release_dates,
)
from ..tabular import render_markdown_table

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

RELEASE_NOTES_MAX_LENGTH = 2000
"""Maximum characters per package release body before truncation."""


# ---------------------------------------------------------------------------
# Shared primitives
# ---------------------------------------------------------------------------


def link_name(name: str, name_urls: Mapping[str, str] | None) -> str:
    """Render a table's subject cell, linked when a URL is known for it.

    :param name: Package, action or tool name.
    :param name_urls: Mapping of names to their URL. Names absent from it (or
        a `None` mapping) render as plain text.
    :return: A markdown link, or the bare name.
    """
    if name_urls and name in name_urls:
        return f"[{name}]({name_urls[name]})"
    return name


def markdown_section(
    heading: str,
    note: str,
    headers: tuple[str, ...],
    rows: list[tuple[str, ...]],
) -> str:
    """Assemble a report section: heading, optional intro, and a table.

    The one place the section layout is defined, so every updater's PR body
    keeps the same shape. The alignment row is derived from *headers* rather
    than written out, which is what stops a column from being added to one
    and not the other.

    :param heading: Full heading line, emoji included, without the `## `.
    :param note: Intro paragraph shown between heading and table. Omitted
        when empty.
    :param headers: Column titles.
    :param rows: One tuple of pre-rendered cells per row, each as long as
        *headers*.
    :return: The rendered markdown, with no trailing newline.
    """
    lines = [f"## {heading}", ""]
    if note:
        lines += [note, ""]
    lines.append(render_markdown_table(headers, rows, align=("left",) * len(headers)))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Date parsing and formatting
# ---------------------------------------------------------------------------


def format_upload_date(iso_datetime: str) -> str:
    """Format an ISO 8601 datetime as a human-readable date string.

    :param iso_datetime: An ISO 8601 datetime string (e.g.,
        `"2026-03-13T12:00:00Z"`).
    :return: A formatted date like `2026-03-13`, or the raw string if parsing
        fails.
    """
    dt = parse_iso_datetime(iso_datetime)
    if dt is None:
        return iso_datetime
    return dt.strftime("%Y-%m-%d")


def format_released(raw_upload: str, reference: date | datetime | None) -> str:
    """Format an upload time as a date, optionally with a relative hint.

    The report-level wrapper around {func}`~repomatic.humanize.format_elapsed`,
    adding the conventions a table cell needs: an empty input renders empty, an
    unparsable one renders verbatim, and an absent reference drops the hint.

    It also pairs the precisions, which is the one thing a caller must not get
    wrong. An upload instant measured against a midnight reference understates
    its age by up to a day, so a date reference truncates the upload to match
    it. Pass the instant wherever the caller holds one: a release published
    earlier today then reads `6 hours ago`, where two dates can only say
    `just now`.

    :param raw_upload: ISO 8601 upload-time string, or empty.
    :param reference: Instant, or day, to measure the offset from. When `None`,
        only the absolute date is returned.
    :return: A string like `2026-06-24 (2 days ago)`, the bare date when
        *reference* is `None`, or empty when *raw_upload* is empty.
    """
    if not raw_upload:
        return ""
    dt = parse_iso_datetime(raw_upload)
    if dt is None:
        return format_upload_date(raw_upload)
    if reference is None:
        return dt.strftime("%Y-%m-%d")
    if isinstance(reference, datetime):
        return format_elapsed(dt, reference)
    return format_elapsed(utc_midnight(dt.date()), utc_midnight(reference))


# ---------------------------------------------------------------------------
# Diff table
# ---------------------------------------------------------------------------


def pypi_name_urls(names: Iterable[str]) -> dict[str, str]:
    """Map each package name to its PyPI project URL.

    Convenience for the `name_urls` of the renderers below, when the names
    come from a PyPI-resolved source (`sync-uv-lock`, `fix-vulnerable-deps`).
    """
    return {name: PYPI_PACKAGE_URL.format(package=name) for name in names}


def format_exclude_newer_note(exclude_newer: str) -> str:
    """Render the uv `exclude-newer` cutoff sentence for a diff table.

    The {func}`format_diff_table` counterpart for `sync-uv-lock` and
    `fix-vulnerable-deps`, which gate on uv's absolute `exclude-newer`
    timestamp. The relative-cooldown updaters
    ({mod}`repomatic.deps.pins`) render their own
    `minimum-release-age` note instead.

    :param exclude_newer: ISO 8601 datetime from the lock's
        `[options].exclude-newer`, as carried by
        {attr}`repomatic.deps.lockfile.LockFile.exclude_newer`, or empty.
    :return: A one-line markdown note, or empty when *exclude_newer* is empty.
    """
    if not exclude_newer:
        return ""
    cutoff = format_upload_date(exclude_newer)
    return (
        "Resolved with [`exclude-newer`]"
        "(https://docs.astral.sh/uv/reference/settings/#exclude-newer)"
        f" cutoff: `{cutoff}`."
    )


def format_diff_table(
    changes: list[tuple[str, str, str]],
    upload_times: Mapping[str, str] | None = None,
    cooldown_note: str = "",
    comparison_urls: Mapping[str, str] | None = None,
    reference_date: date | None = None,
    name_urls: Mapping[str, str] | None = None,
    heading: str = "Updated packages",
    subject: str = "Package",
    released_overrides: Mapping[str, str] | None = None,
    change_labels: Mapping[str, str] | None = None,
    provenance: Mapping[str, str] | None = None,
) -> str:
    """Format version changes as a markdown table with heading.

    The shared PR-body table for every dependency updater (`sync-uv-lock`,
    `fix-vulnerable-deps`, `sync-tool-versions`, `sync-action-pins`,
    `sync-workflow-pins`) so they all render identically.

    When `upload_times` is provided, a "Released" column is added so
    reviewers can visually verify that all updated packages respect the
    cooldown. A row whose version was decided outside that cooldown check
    (the upstream toolkit's lockstep-aligned pin) marks itself through
    `released_overrides` instead of showing a date, so the exemption reads
    as deliberate rather than as missing data. When `cooldown_note` is
    provided, that pre-rendered sentence (the absolute `exclude-newer`
    cutoff for uv, or the relative `minimum-release-age` cutoff for the
    version-sync updaters) is shown above the table.

    :param changes: List of `(name, old_version, new_version)` tuples
        as returned by {func}`repomatic.deps.lockfile.diff_lock_versions`.
    :param upload_times: Optional mapping of package names to ISO 8601
        upload-time strings, as held by
        {attr}`repomatic.deps.lockfile.LockFile.upload_times`.
    :param cooldown_note: Optional pre-rendered markdown sentence describing
        the cooldown cutoff, shown above the table. Build it with
        {func}`format_exclude_newer_note` (uv) or
        {func}`repomatic.deps.pins.format_cooldown_note` (version-sync).
    :param comparison_urls: Optional mapping of names to comparison URLs,
        linked on the change cell (see {func}`build_comparison_urls`).
    :param reference_date: When set, each "Released" date gains a relative
        hint (`2026-06-24 (2 days ago)`) measured from this date.
    :param name_urls: Optional mapping of names to a URL the name links to
        (PyPI, GitHub, npm). Names absent from the mapping render plain. Pass
        {func}`pypi_name_urls` for PyPI-sourced changes.
    :param heading: Noun after `## 🆙 ` (e.g. `Updated tools`).
    :param subject: Header for the first (name) column (e.g. `Tool`, `Action`).
    :param released_overrides: Optional mapping of names to literal markdown
        replacing their "Released" cell. An override on a changed name also
        forces the column on, even without `upload_times`; entries for
        unchanged names are ignored.
    :param change_labels: Optional mapping of names to literal markdown
        leading the "Change" cell of their version move, the way `🆕 new:`
        leads an added package. Marks a move the arrow alone misreads, like a
        pin stepped back to an older release.
    :param provenance: Optional mapping of names to literal markdown for a
        "Provenance" column, which renders when any changed name has an entry.
        A changed name without one shows a dash. The column sits right after
        "Change", before "Released": who published a release belongs with
        what moved, and dates close the row.
    :return: A markdown string with a `## 🆙 {heading}` heading and table,
        or an empty string if there are no changes.
    """
    if not changes:
        return ""
    released_overrides = released_overrides or {}
    change_labels = change_labels or {}
    changed_names = {name for name, _old, _new in changes}
    show_uploaded = bool(upload_times) or bool(
        changed_names & released_overrides.keys()
    )
    provenance = provenance or {}
    show_provenance = bool(changed_names & provenance.keys())
    rows: list[tuple[str, ...]] = []
    for name, old, new in changes:
        if old and new:
            change = f"`{old}` \u2192 `{new}`"
            if comparison_urls and name in comparison_urls:
                change = f"[{change}]({comparison_urls[name]})"
            if name in change_labels:
                change = f"{change_labels[name]}: {change}"
        elif new:
            change = f"🆕 new: `{new}`"
        else:
            change = f"🗑️ removed: `{old}`"
        cells = [link_name(name, name_urls), change]
        if show_provenance:
            cells.append(provenance.get(name, "—"))
        if show_uploaded:
            if name in released_overrides:
                uploaded = released_overrides[name]
            else:
                raw_time = upload_times.get(name, "") if upload_times else ""
                uploaded = format_released(raw_time, reference_date)
            cells.append(uploaded)
        rows.append(tuple(cells))

    headers: tuple[str, ...] = (subject, "Change")
    if show_provenance:
        headers = (*headers, "Provenance")
    if show_uploaded:
        headers = (*headers, "Released")
    return markdown_section(f"🆙 {heading}", cooldown_note, headers, rows)


# ---------------------------------------------------------------------------
# Held-back-by-cooldown table
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HeldBackPackage:
    """A newer release withheld from the lock by the `exclude-newer` cooldown.

    Built by {func}`repomatic.deps.cooldown.compute_held_back_packages` for the `## Held
    back by cooldown` report section: a package has already published a newer
    version, but it is still inside the cooldown window, so `uv lock --upgrade`
    keeps the older {attr}`locked_version`.
    """

    name: str
    """Package name, as it appears on PyPI."""

    locked_version: str
    """Version held in the lock: the newest release outside the cooldown."""

    available_version: str
    """Newer version already on the index, still inside the cooldown window."""

    released: str
    """Upload date of {attr}`available_version` (`YYYY-MM-DD`), or empty when
    the lock records no upload time (a git or path source)."""

    eligible: str
    """Date {attr}`available_version` leaves the cooldown and becomes lockable,
    with a human-readable countdown (`2026-06-25 (in 4 days)`), or empty when
    it cannot be computed."""

    @property
    def row(self) -> tuple[str, str, str, str, str]:
        """The release as a terminal row.

        Its name, then the {data}`HELD_BACK_COLUMNS` cells.
        """
        return (
            self.name,
            self.locked_version,
            self.available_version,
            self.released,
            self.eligible,
        )


EXCLUDE_NEWER_HELD_BACK_NOTE = (
    "Newer releases already published but withheld because they are still"
    " inside the [`exclude-newer`](https://docs.astral.sh/uv/reference/"
    "settings/#exclude-newer) cooldown window."
)
"""Intro paragraph for the `sync-uv-lock` held-back section.

The {mod}`repomatic.deps.pins` updaters pass their own `minimum-release-age`
wording to {func}`format_held_back_table` instead.
"""

HELD_BACK_COLUMNS = ("Locked", "Available", "Released", "Eligible")
"""Held-back columns following the caller-supplied subject column.

Shared with {func}`repomatic.cli.deps.print_plan_tables`, so a run's
markdown PR body and its terminal table name the same columns in the same
order instead of drifting apart as two hand-kept literals.
"""


def build_held_back(
    name: str,
    pinned: str,
    available: str,
    available_date: str,
    min_age: timedelta,
    today: date,
) -> HeldBackPackage:
    """Assemble a {class}`HeldBackPackage` row from raw selection data.

    The formatting half of the version-sync held-back report:
    {func}`repomatic.deps.pins.select_held_back` picks the withheld
    candidate, and this turns its raw version and upload date into the same
    `released`/`eligible` strings
    {func}`repomatic.deps.cooldown.compute_held_back_packages` produces for uv, so
    {func}`format_held_back_table` renders both identically. Unlike the uv
    path, no second resolution is needed: the candidates are already in hand
    from the datasource sweep.

    :param name: Display name (package, action slug, or tool).
    :param pinned: Version this run settled on (held in place by the cooldown).
    :param available: The newer version still inside the cooldown window.
    :param available_date: Upload date of *available* (`YYYY-MM-DD`), or empty.
    :param min_age: The `minimum-release-age` cooldown width.
    :param today: Reference date for the relative countdown.
    :return: A populated {class}`HeldBackPackage`.
    """
    released = format_released(available_date, today)
    eligible = ""
    upload_dt = parse_iso_datetime(available_date)
    if upload_dt is not None:
        # The datasource sweeps report a release date and no time of day, so
        # this countdown stays day-granular.
        eligible = format_countdown(upload_dt + min_age, utc_midnight(today))
    return HeldBackPackage(name, pinned, available, released, eligible)


def format_held_back_table(
    held_back: list[HeldBackPackage],
    note: str = EXCLUDE_NEWER_HELD_BACK_NOTE,
    *,
    name_urls: Mapping[str, str] | None = None,
    subject: str = "Package",
) -> str:
    """Format cooldown-withheld releases as a markdown section.

    Shared by every cooldown-gated updater: `sync-uv-lock` (rows from
    {func}`repomatic.deps.cooldown.compute_held_back_packages`) and the version-sync
    commands (rows from {func}`build_held_back`), so the section renders
    identically.

    :param held_back: Withheld releases as {class}`HeldBackPackage` rows.
    :param note: Intro paragraph describing the cooldown. Defaults to the uv
        `exclude-newer` wording; version-sync passes its `minimum-release-age`
        wording.
    :param name_urls: Optional mapping of names to a URL the name links to
        (PyPI, GitHub, npm). Names absent from the mapping render plain.
    :param subject: Header for the first column (e.g. `Action`, `Tool`).
    :return: A markdown string with a `## ⏸️ Held back by cooldown` heading
        and table, or an empty string when *held_back* is empty.
    """
    if not held_back:
        return ""
    return markdown_section(
        "⏸️ Held back by cooldown",
        note,
        (subject, *HELD_BACK_COLUMNS),
        [
            (
                link_name(pkg.name, name_urls),
                f"`{pkg.locked_version}`",
                f"`{pkg.available_version}`",
                pkg.released,
                pkg.eligible,
            )
            for pkg in held_back
        ],
    )


# ---------------------------------------------------------------------------
# Held-back-for-provenance table
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProvenanceHold:
    """A release withheld because it lost the provenance its predecessor had.

    Built by {func}`repomatic.deps.provenance.review_bumps`: the kept release
    was uploaded through trusted publishing, and the withheld one was not.
    """

    name: str
    """Name of the row: the tool or the package."""

    kept: str
    """Version left in place, which carries provenance."""

    withheld: str
    """Version the run would have adopted, which carries none."""

    publisher: str
    """Trusted publisher of {attr}`kept`, like `pypa/packaging`, or empty when
    its provenance names none."""

    @property
    def row(self) -> tuple[str, str, str, str]:
        """The hold as a terminal row.

        Its name, then the {data}`PROVENANCE_HOLD_COLUMNS` cells.
        """
        return self.name, self.kept, self.withheld, self.publisher


PROVENANCE_HOLD_COLUMNS = ("Kept", "Withheld", "Last publisher")
"""Columns of the provenance-hold table, after the caller-supplied subject.

Shared with {func}`repomatic.cli.deps.print_plan_tables`, for the reason
{data}`HELD_BACK_COLUMNS` is.
"""

PROVENANCE_HOLD_NOTE = (
    "Each withheld release carries no [PEP 740](https://peps.python.org/pep-0740/)"
    " provenance, while the release it would replace was uploaded by the trusted"
    " publisher named here. A publishing token used outside that workflow uploads"
    " exactly this. Confirm the release with its maintainers before adopting it by"
    " hand: this check holds the automated bump only."
)
"""Intro paragraph of the provenance-hold section."""


def format_provenance_holds(
    holds: list[ProvenanceHold],
    *,
    name_urls: Mapping[str, str] | None = None,
    subject: str = "Package",
) -> str:
    """Format the releases withheld for missing provenance as a markdown section.

    :param holds: The withheld releases.
    :param name_urls: Optional mapping of names to a URL the name links to.
    :param subject: Header for the first column (e.g. `Tool`, `Package`).
    :return: A markdown string with a `## 🛡️ Held back for missing provenance`
        heading and table, or an empty string when *holds* is empty.
    """
    if not holds:
        return ""
    return markdown_section(
        "🛡️ Held back for missing provenance",
        PROVENANCE_HOLD_NOTE,
        (subject, *PROVENANCE_HOLD_COLUMNS),
        [
            (
                link_name(hold.name, name_urls),
                f"`{hold.kept}`",
                f"`{hold.withheld}`",
                f"`{hold.publisher}`" if hold.publisher else "—",
            )
            for hold in holds
        ],
    )


# ---------------------------------------------------------------------------
# Cooldown-bypass table
# ---------------------------------------------------------------------------


BYPASS_NEEDS_RELEASE = "needs release"
"""Expiry placeholder for a freeze holding an unreleased version.

A fixed-timestamp `exclude-newer-package` entry whose held version has no
upload time in the lock (a git, path, or otherwise unpublished source) can
never age past the rolling `exclude-newer` cutoff on its own: the freeze only
ends once the package ships a release the lock can adopt. The markdown report
renders the marker in italics to set it apart from real dates.
"""

BYPASS_NOT_LOCKED = "not locked"
"""Held-version placeholder for an entry whose package the lock does not hold.

A tool the registry pins outside the lockfile, or a dependency the project
dropped, has no locked version for the entry to hold. The entry still expires:
`sync-uv-lock` prunes it once the rolling `exclude-newer` cutoff has passed the
entry's own timestamp. The markdown report renders the marker in italics, like
{data}`BYPASS_NEEDS_RELEASE`.
"""


@dataclass(frozen=True)
class BypassForecast:
    """A cooldown-bypass freeze and the date it self-clears.

    Built by {func}`repomatic.deps.cooldown.active_forecasts` (freezes still
    active) and {func}`repomatic.deps.cooldown.pruned_forecasts` (freezes the run
    just cleared) for the `## ❄️ Cooldown bypasses` report section: a
    fixed-timestamp `exclude-newer-package` entry holds {attr}`name` at
    {attr}`held_version` until that version ages past the `exclude-newer`
    cutoff, at which point `sync-uv-lock` prunes the entry and the package
    resumes normal cooldown resolution.
    """

    name: str
    """Package name, as it appears on PyPI."""

    held_version: str
    """Version the freeze holds in the lock. Empty when the lock holds no
    version of the package, which the reports render as
    {data}`BYPASS_NOT_LOCKED`."""

    expires: str
    """Date the freeze expires and the entry is pruned, with a human-readable
    countdown (`2026-07-08 (in 2 days)`, in the past for an already-cleared
    freeze), {data}`BYPASS_NEEDS_RELEASE` when the held version has no upload
    time in the lock, or empty when there is no rolling `exclude-newer` span
    to forecast against. For a package the lock does not hold, the date is
    projected from the entry's own cutoff."""

    @property
    def awaits_release(self) -> bool:
        """Whether the freeze holds an unreleased version.

        Only a release can end such a freeze.
        """
        return self.expires == BYPASS_NEEDS_RELEASE

    @property
    def row(self) -> tuple[str, str, str]:
        """The freeze as a terminal row of {data}`BYPASS_COLUMNS`."""
        return self.name, self.held_version or BYPASS_NOT_LOCKED, self.expires


BYPASS_SECTION_NOTE = (
    "Packages pulled in ahead of the cooldown by an [`exclude-newer-package`]"
    "(https://docs.astral.sh/uv/reference/settings/#exclude-newer-package)"
    " freeze."
)
"""Intro paragraph for the `sync-uv-lock` cooldown-bypasses section."""

BYPASS_COLUMNS = ("Package", "Held at", "Held until")
"""Columns of the cooldown-bypass table.

Shared with {func}`repomatic.cli.deps.print_plan_tables` for the reason
{data}`HELD_BACK_COLUMNS` is.
"""


def format_bypass_section(
    forecasts: list[BypassForecast],
    pruned: list[BypassForecast] | None = None,
    frozen: list[str] | None = None,
    *,
    stale_comments: list[str] | None = None,
    name_urls: Mapping[str, str] | None = None,
) -> str:
    """Format the cooldown-bypass lifecycle as a single markdown table.

    The `sync-uv-lock` report section covering `exclude-newer-package`
    freezes. Every lifecycle state is a row in one table so the section scans
    like the `## 🆙 Updated packages` one: freezes still active render plain,
    entries this run rewrote into freeze cutoffs are labelled `📌 frozen:`,
    and expired entries this run removed from `pyproject.toml` are labelled
    `🧹 cleared:`, keeping the version and expiry data the freeze had. A
    freeze holding an unreleased version is labelled `🚧 unreleased:` and its
    {data}`BYPASS_NEEDS_RELEASE` expiry renders in italics. An entry for a
    package the lock does not hold shows {data}`BYPASS_NOT_LOCKED` in place of
    a version.

    :param forecasts: Active freezes from
        {func}`repomatic.deps.cooldown.active_forecasts`.
    :param pruned: Expired entries the run removed, snapshot by
        {func}`repomatic.deps.cooldown.pruned_forecasts` before the prune.
    :param frozen: Names of the entries the run rewrote into freeze cutoffs;
        their *forecasts* rows get the `📌 frozen:` label.
    :param stale_comments: Cleared entries the comment above
        `exclude-newer-package` still names, from
        {meth}`repomatic.deps.cooldown.UvSettings.comment_mentions`. A warning under the
        table asks for the comment to be updated by hand.
    :param name_urls: Optional mapping of names to a URL the name links to.
        Names absent from the mapping render plain.
    :return: A markdown string with a `## ❄️ Cooldown bypasses` heading and
        table, or an empty string when there is no row to report.
    """
    frozen_names = set(frozen or [])
    entries = sorted(
        [(forecast, "cleared") for forecast in pruned or []]
        + [
            (forecast, "frozen" if forecast.name in frozen_names else "")
            for forecast in forecasts
        ],
        key=lambda row: row[0].name,
    )
    if not entries:
        return ""

    rows: list[tuple[str, ...]] = []
    for forecast, marker in entries:
        held = (
            f"`{forecast.held_version}`"
            if forecast.held_version
            else f"*{BYPASS_NOT_LOCKED}*"
        )
        if marker == "cleared":
            held = f"🧹 cleared: {held}"
        elif marker == "frozen":
            held = f"📌 frozen: {held}"
        elif forecast.awaits_release:
            held = f"🚧 unreleased: {held}"
        expires = forecast.expires
        if forecast.awaits_release:
            expires = f"*{expires}*"
        rows.append((link_name(forecast.name, name_urls), held, expires))

    section = markdown_section(
        "❄️ Cooldown bypasses",
        BYPASS_SECTION_NOTE,
        BYPASS_COLUMNS,
        rows,
    )
    if stale_comments:
        names = ", ".join(f"`{name}`" for name in stale_comments)
        section += (
            "\n\n> [!WARNING]\n"
            "> The comment above `exclude-newer-package` in `pyproject.toml` still"
            f" names {names}, which this run cleared. Update it by hand."
        )
    return section


# ---------------------------------------------------------------------------
# GitHub release notes
# ---------------------------------------------------------------------------


def fetch_release_notes(
    changes: list[tuple[str, str, str]],
) -> dict[str, tuple[str, list[tuple[str, str]]]]:
    """Fetch release notes for all updated PyPI packages.

    For each package with a new version, discovers the GitHub repository via
    PyPI, and reads the release notes of the versions the bump covers through
    {func}`~repomatic.github.releases.fetch_github_release_notes`, which lists
    the releases of a repository once. A version PyPI marks yanked is left out:
    a pulled release has no notes worth advertising. Falls back to a changelog
    link from PyPI `project_urls` when no GitHub release has notes.

    :param changes: List of `(name, old_version, new_version)` tuples.
    :return: A dict mapping package names to `(repo_url, versions)` tuples
        where `versions` is a list of `(tag, body)` pairs sorted ascending,
        in the order of *changes*. Only packages with at least one non-empty
        body are included. When a changelog URL is used as fallback, `tag` is
        empty and `body` contains a markdown link.
    """
    requests = []
    for name, old, new in changes:
        if not new:
            # Skip removed packages.
            continue
        repo_url = get_pypi_source_url(name)
        if not repo_url:
            logging.debug(f"No GitHub URL found for {name}.")
            continue
        yanked = frozenset(
            version
            for version, release in pypi_release_dates(name).items()
            if release.yanked
        )
        requests.append(ReleaseNotesRequest(name, repo_url, old, new, skip=yanked))

    found = fetch_github_release_notes(requests)
    notes: dict[str, tuple[str, list[tuple[str, str]]]] = {}
    for request in requests:
        if request.name in found:
            notes[request.name] = found[request.name]
            continue
        # Fallback: link to a changelog page from PyPI project_urls.
        changelog_url = get_pypi_changelog_url(request.name)
        if changelog_url:
            notes[request.name] = (
                request.repo_url,
                [("", f"[Changelog]({changelog_url})")],
            )
            logging.debug(
                f"Using PyPI changelog URL for {request.name}: {changelog_url}"
            )
        else:
            logging.debug(
                f"No release body or changelog for {request.name} {request.new}."
            )
    return notes


def format_release_notes(
    notes: dict[str, tuple[str, list[tuple[str, str]]]],
) -> str:
    """Render release notes as collapsible `<details>` blocks.

    A `### Release notes` heading (an h3, nesting the section under the PR
    body's h2 update table) with one collapsible section per package, each
    version introduced by an h4 tag heading. Long release bodies are
    truncated to {data}`RELEASE_NOTES_MAX_LENGTH` characters with a link to
    the full release.

    :param notes: A dict mapping package names to `(repo_url, versions)`
        tuples where `versions` is a list of `(tag, body)` pairs, as
        returned by {func}`fetch_release_notes`.
    :return: A markdown string with the release notes section, or an empty
        string if no notes are available.
    """
    if not notes:
        return ""
    lines = ["### Release notes", ""]
    for name, (repo_url, versions) in sorted(notes.items()):
        lines.extend(("<details>", f"<summary><code>{name}</code></summary>", ""))
        for tag, body in versions:
            # Upstream bodies bring their own h1/h2 sections: demote them
            # below the h4 version heading so they never collide with the
            # PR body's h2 hierarchy.
            body = demote_markdown_headings(sanitize_markdown_mentions(body), 5)
            if tag:
                release_url = f"{repo_url}/releases/tag/{tag}"
                lines.extend((f"#### [`{tag}`]({release_url})", ""))
                if len(body) > RELEASE_NOTES_MAX_LENGTH:
                    truncated = body[:RELEASE_NOTES_MAX_LENGTH].rsplit("\n", 1)[0]
                    lines.extend((
                        truncated,
                        "",
                        f"... [Full release notes]({release_url})",
                    ))
                else:
                    lines.append(body)
            else:
                lines.append(body)
            lines.append("")
        lines.extend(("</details>", ""))
    return "\n".join(lines).rstrip()


def build_comparison_urls(
    changes: list[tuple[str, str, str]],
    notes: dict[str, tuple[str, list[tuple[str, str]]]],
) -> dict[str, str]:
    """Build GitHub comparison URLs from version changes and release notes.

    Uses the tag format discovered by {func}`fetch_release_notes` to construct
    comparison URLs. Only packages with both old and new versions and a known
    GitHub repository are included.

    A package whose notes carry no tag at all is skipped. That happens when
    {func}`fetch_release_notes` found no GitHub release for the range and fell
    back to a changelog link, which is positive evidence that the tags this
    URL would name do not exist: guessing a `v` prefix there yields a 404 in
    the PR body.

    :param changes: List of `(name, old_version, new_version)` tuples.
    :param notes: Release notes dict as returned by {func}`fetch_release_notes`.
    :return: Dict mapping package names to GitHub comparison URLs.
    """
    urls: dict[str, str] = {}
    for name, old, new in changes:
        if not old or not new or name not in notes:
            continue
        repo_url, versions = notes[name]
        # Determine tag prefix from the first discovered tag.
        tags = [tag for tag, _ in versions if tag]
        if not tags:
            continue
        prefix = "v" if tags[0].startswith("v") else ""
        urls[name] = f"{repo_url}/compare/{prefix}{old}...{prefix}{new}"
    return urls
