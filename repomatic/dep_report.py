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

The computations stay with their datasources ({mod}`repomatic.uv` for the lock,
{mod}`repomatic.version_sync` for GitHub/PyPI/npm); this module only renders
their results.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import arrow
from packaging.version import InvalidVersion, Version

from .github.pr_body import demote_markdown_headings, sanitize_markdown_mentions
from .github.releases import get_github_release_body
from .pypi import (
    PYPI_PACKAGE_URL,
    get_changelog_url as get_pypi_changelog_url,
    get_release_dates as get_pypi_release_dates,
    get_source_url as get_pypi_source_url,
)

RELEASE_NOTES_MAX_LENGTH = 2000
"""Maximum characters per package release body before truncation."""


# ---------------------------------------------------------------------------
# Date parsing and formatting
# ---------------------------------------------------------------------------


def parse_iso_datetime(value: str) -> datetime | None:
    """Parse an ISO 8601 / RFC 3339 timestamp into a timezone-aware datetime.

    Uses arrow, so a nanosecond fractional second and a `Z` suffix (both of
    which Python 3.10's stdlib `datetime.fromisoformat` rejects) parse cleanly;
    sub-microsecond precision is truncated to fit `datetime`.

    arrow also supplies the `.humanize()` relative-time phrasing used in the
    sync report. whenever was the prior parser but has no humanizer; switch
    back once one lands: https://github.com/ariebovenberg/whenever/discussions/277

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


def format_released(raw_upload: str, reference: date | None) -> str:
    """Format an upload time as a date, optionally with a relative hint.

    :param raw_upload: ISO 8601 upload-time string, or empty.
    :param reference: Date to measure the relative offset from. When `None`,
        only the absolute date is returned.
    :return: A string like `2026-06-24 (2 days ago)`, the bare date when
        *reference* is `None`, or empty when *raw_upload* is empty.
    """
    if not raw_upload:
        return ""
    dt = parse_iso_datetime(raw_upload)
    if dt is None:
        return format_upload_date(raw_upload)
    iso = dt.strftime("%Y-%m-%d")
    if reference is None:
        return iso
    return f"{iso} ({arrow.get(dt.date()).humanize(arrow.get(reference))})"


def format_eligible(eligible: date, today: date) -> str:
    """Render an eligibility date with a human-readable countdown.

    :param eligible: The date a release leaves the cooldown window.
    :param today: The current date, for the relative offset.
    :return: A string like `2026-06-25 (in 4 days)`, `... (today)`, or the
        bare date once the window has elapsed.
    """
    iso = eligible.strftime("%Y-%m-%d")
    if eligible < today:
        return iso
    return f"{iso} ({arrow.get(eligible).humanize(arrow.get(today))})"


# ---------------------------------------------------------------------------
# Diff table
# ---------------------------------------------------------------------------


def pypi_name_urls(changes: list[tuple[str, str, str]]) -> dict[str, str]:
    """Map each changed package name to its PyPI project URL.

    Convenience for {func}`format_diff_table`'s `name_urls` when the changes
    come from a PyPI-resolved source (`sync-uv-lock`, `fix-vulnerable-deps`).
    """
    return {name: PYPI_PACKAGE_URL.format(package=name) for name, _old, _new in changes}


def format_exclude_newer_note(exclude_newer: str) -> str:
    """Render the uv `exclude-newer` cutoff sentence for a diff table.

    The {func}`format_diff_table` counterpart for `sync-uv-lock` and
    `fix-vulnerable-deps`, which gate on uv's absolute `exclude-newer`
    timestamp. The relative-cooldown updaters
    ({mod}`repomatic.version_sync`) render their own
    `minimum-release-age` note instead.

    :param exclude_newer: ISO 8601 datetime from the lock's
        `[options].exclude-newer`, as returned by
        {func}`repomatic.uv.parse_lock_exclude_newer`, or empty.
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
    upload_times: dict[str, str] | None = None,
    cooldown_note: str = "",
    comparison_urls: dict[str, str] | None = None,
    reference_date: date | None = None,
    name_urls: dict[str, str] | None = None,
    heading: str = "Updated packages",
    subject: str = "Package",
    released_overrides: dict[str, str] | None = None,
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
        as returned by {func}`repomatic.uv.diff_lock_versions`.
    :param upload_times: Optional mapping of package names to ISO 8601
        upload-time strings, as returned by
        {func}`repomatic.uv.parse_lock_upload_times`.
    :param cooldown_note: Optional pre-rendered markdown sentence describing
        the cooldown cutoff, shown above the table. Build it with
        {func}`format_exclude_newer_note` (uv) or
        {func}`repomatic.version_sync.format_cooldown_note` (version-sync).
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
    :return: A markdown string with a `## 🆙 {heading}` heading and table,
        or an empty string if there are no changes.
    """
    if not changes:
        return ""
    name_urls = name_urls or {}
    released_overrides = released_overrides or {}
    changed_names = {name for name, _old, _new in changes}
    show_uploaded = bool(upload_times) or bool(
        changed_names & released_overrides.keys()
    )
    lines = [f"## 🆙 {heading}", ""]
    if cooldown_note:
        lines.append(cooldown_note)
        lines.append("")
    if show_uploaded:
        lines.append(f"| {subject} | Change | Released |")
        lines.append("| :-- | :-- | :-- |")
    else:
        lines.append(f"| {subject} | Change |")
        lines.append("| :-- | :-- |")
    for name, old, new in changes:
        link = f"[{name}]({name_urls[name]})" if name in name_urls else name
        if old and new:
            change = f"`{old}` \u2192 `{new}`"
            if comparison_urls and name in comparison_urls:
                change = f"[{change}]({comparison_urls[name]})"
        elif new:
            change = f"🆕 new: `{new}`"
        else:
            change = f"🗑️ removed: `{old}`"
        if show_uploaded:
            if name in released_overrides:
                uploaded = released_overrides[name]
            else:
                raw_time = upload_times.get(name, "") if upload_times else ""
                uploaded = format_released(raw_time, reference_date)
            lines.append(f"| {link} | {change} | {uploaded} |")
        else:
            lines.append(f"| {link} | {change} |")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Held-back-by-cooldown table
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HeldBackPackage:
    """A newer release withheld from the lock by the `exclude-newer` cooldown.

    Built by {func}`repomatic.uv.compute_held_back_packages` for the `## Held
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


EXCLUDE_NEWER_HELD_BACK_NOTE = (
    "Newer releases already published but withheld because they are still"
    " inside the [`exclude-newer`](https://docs.astral.sh/uv/reference/"
    "settings/#exclude-newer) cooldown window."
)
"""Intro paragraph for the `sync-uv-lock` held-back section.

The {mod}`repomatic.version_sync` updaters pass their own `minimum-release-age`
wording to {func}`format_held_back_table` instead.
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
    {func}`repomatic.version_sync.select_held_back` picks the withheld
    candidate, and this turns its raw version and upload date into the same
    `released`/`eligible` strings
    {func}`repomatic.uv.compute_held_back_packages` produces for uv, so
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
        eligible = format_eligible((upload_dt + min_age).date(), today)
    return HeldBackPackage(name, pinned, available, released, eligible)


def format_held_back_table(
    held_back: list[HeldBackPackage],
    note: str = EXCLUDE_NEWER_HELD_BACK_NOTE,
    *,
    name_urls: dict[str, str] | None = None,
    subject: str = "Package",
) -> str:
    """Format cooldown-withheld releases as a markdown section.

    Shared by every cooldown-gated updater: `sync-uv-lock` (rows from
    {func}`repomatic.uv.compute_held_back_packages`) and the version-sync
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
    name_urls = name_urls or {}
    lines = [
        "## ⏸️ Held back by cooldown",
        "",
        note,
        "",
        f"| {subject} | Locked | Available | Released | Eligible |",
        "| :-- | :-- | :-- | :-- | :-- |",
    ]
    for pkg in held_back:
        link = (
            f"[{pkg.name}]({name_urls[pkg.name]})"
            if pkg.name in name_urls
            else pkg.name
        )
        lines.append(
            f"| {link} | `{pkg.locked_version}` | `{pkg.available_version}` |"
            f" {pkg.released} | {pkg.eligible} |"
        )
    return "\n".join(lines)


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


@dataclass(frozen=True)
class BypassForecast:
    """A cooldown-bypass freeze and the date it self-clears.

    Built by {func}`repomatic.uv.compute_bypass_forecasts` (freezes still
    active) and {func}`repomatic.uv.compute_pruned_forecasts` (freezes the run
    just cleared) for the `## ❄️ Cooldown bypasses` report section: a
    fixed-timestamp `exclude-newer-package` entry holds {attr}`name` at
    {attr}`held_version` until that version ages past the `exclude-newer`
    cutoff, at which point `sync-uv-lock` prunes the entry and the package
    resumes normal cooldown resolution.
    """

    name: str
    """Package name, as it appears on PyPI."""

    held_version: str
    """Version the freeze holds in the lock."""

    expires: str
    """Date the freeze expires and the entry is pruned, with a human-readable
    countdown (`2026-07-08 (in 2 days)`, in the past for an already-cleared
    freeze), {data}`BYPASS_NEEDS_RELEASE` when the held version has no upload
    time in the lock, or empty when there is no rolling `exclude-newer` span
    to forecast against."""


BYPASS_SECTION_NOTE = (
    "Packages pulled in ahead of the cooldown by an [`exclude-newer-package`]"
    "(https://docs.astral.sh/uv/reference/settings/#exclude-newer-package)"
    " freeze. Each entry is cleared from `pyproject.toml` automatically once"
    " its held version ages past the `exclude-newer` cutoff."
)
"""Intro paragraph for the `sync-uv-lock` cooldown-bypasses section."""


def format_bypass_section(
    forecasts: list[BypassForecast],
    pruned: list[BypassForecast] | None = None,
    frozen: list[str] | None = None,
    *,
    name_urls: dict[str, str] | None = None,
) -> str:
    """Format the cooldown-bypass lifecycle as a single markdown table.

    The `sync-uv-lock` report section covering `exclude-newer-package`
    freezes. Every lifecycle state is a row in one table so the section scans
    like the `## 🆙 Updated packages` one: freezes still active render plain,
    entries this run rewrote into freeze cutoffs are labelled `📌 frozen:`,
    and expired entries this run removed from `pyproject.toml` are labelled
    `🧹 cleared:`, keeping the version and expiry data the freeze had. A
    freeze holding an unreleased version is labelled `🚧 unreleased:` and its
    {data}`BYPASS_NEEDS_RELEASE` expiry renders in italics.

    :param forecasts: Active freezes from
        {func}`repomatic.uv.compute_bypass_forecasts`.
    :param pruned: Expired entries the run removed, snapshot by
        {func}`repomatic.uv.compute_pruned_forecasts` before the prune.
    :param frozen: Names of the entries the run rewrote into freeze cutoffs;
        their *forecasts* rows get the `📌 frozen:` label.
    :param name_urls: Optional mapping of names to a URL the name links to.
        Names absent from the mapping render plain.
    :return: A markdown string with a `## ❄️ Cooldown bypasses` heading and
        table, or an empty string when there is no row to report.
    """
    frozen_names = set(frozen or [])
    rows = sorted(
        [(forecast, "cleared") for forecast in pruned or []]
        + [
            (forecast, "frozen" if forecast.name in frozen_names else "")
            for forecast in forecasts
        ],
        key=lambda row: row[0].name,
    )
    if not rows:
        return ""
    name_urls = name_urls or {}

    def link(name: str) -> str:
        return f"[{name}]({name_urls[name]})" if name in name_urls else name

    lines = [
        "## ❄️ Cooldown bypasses",
        "",
        BYPASS_SECTION_NOTE,
        "",
        "| Package | Held at | Held until |",
        "| :-- | :-- | :-- |",
    ]
    for forecast, marker in rows:
        held = f"`{forecast.held_version}`"
        if marker == "cleared":
            held = f"🧹 cleared: {held}"
        elif marker == "frozen":
            held = f"📌 frozen: {held}"
        elif forecast.expires == BYPASS_NEEDS_RELEASE:
            held = f"🚧 unreleased: {held}"
        expires = forecast.expires
        if expires == BYPASS_NEEDS_RELEASE:
            expires = f"*{expires}*"
        lines.append(f"| {link(forecast.name)} | {held} | {expires} |")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# GitHub release notes
# ---------------------------------------------------------------------------


def _versions_in_range(package: str, old: str, new: str) -> list[str]:
    """Return PyPI versions of *package* in the half-open range `(old, new]`.

    Versions are sorted in ascending order. Falls back to `[new]` if no
    intermediate versions are found or PyPI is unreachable.
    """
    releases = get_pypi_release_dates(package)
    if not releases:
        return [new]
    try:
        old_v = Version(old)
        new_v = Version(new)
    except InvalidVersion:
        return [new]
    intermediate = []
    for version_str in releases:
        try:
            v = Version(version_str)
        except InvalidVersion:
            continue
        if old_v < v <= new_v:
            intermediate.append((v, version_str))
    if not intermediate:
        return [new]
    intermediate.sort()
    return [s for _, s in intermediate]


def fetch_release_notes(
    changes: list[tuple[str, str, str]],
) -> dict[str, tuple[str, list[tuple[str, str]]]]:
    """Fetch release notes for all updated packages.

    For each package with a new version, discovers the GitHub repository via
    PyPI and fetches the release notes from GitHub Releases for all versions
    in the range `(old, new]`. Falls back to a changelog link from PyPI
    `project_urls` when no GitHub Release exists.

    :param changes: List of `(name, old_version, new_version)` tuples.
    :return: A dict mapping package names to `(repo_url, versions)` tuples
        where `versions` is a list of `(tag, body)` pairs sorted ascending.
        Only packages with at least one non-empty body are included. When a
        changelog URL is used as fallback, `tag` is empty and `body`
        contains a markdown link.
    """
    notes: dict[str, tuple[str, list[tuple[str, str]]]] = {}
    for name, old, new in changes:
        if not new:
            # Skip removed packages.
            continue
        repo_url = get_pypi_source_url(name)
        if not repo_url:
            logging.debug(f"No GitHub URL found for {name}.")
            continue

        # Discover all versions in the range (old, new].
        versions_to_fetch = _versions_in_range(name, old, new) if old else [new]

        fetched: list[tuple[str, str]] = []
        for version in versions_to_fetch:
            tag, body = get_github_release_body(repo_url, version)
            if body:
                fetched.append((tag, body))

        if not fetched:
            # Fallback: link to a changelog page from PyPI project_urls.
            changelog_url = get_pypi_changelog_url(name)
            if changelog_url:
                fetched.append(("", f"[Changelog]({changelog_url})"))
                logging.debug(f"Using PyPI changelog URL for {name}: {changelog_url}")
            else:
                logging.debug(f"No release body or changelog for {name} {new}.")

        if fetched:
            notes[name] = (repo_url, fetched)
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
        lines.append("<details>")
        lines.append(f"<summary><code>{name}</code></summary>")
        lines.append("")
        for tag, body in versions:
            # Upstream bodies bring their own h1/h2 sections: demote them
            # below the h4 version heading so they never collide with the
            # PR body's h2 hierarchy.
            body = demote_markdown_headings(sanitize_markdown_mentions(body), 5)
            if tag:
                release_url = f"{repo_url}/releases/tag/{tag}"
                lines.append(f"#### [`{tag}`]({release_url})")
                lines.append("")
                if len(body) > RELEASE_NOTES_MAX_LENGTH:
                    truncated = body[:RELEASE_NOTES_MAX_LENGTH].rsplit("\n", 1)[0]
                    lines.append(truncated)
                    lines.append("")
                    lines.append(f"... [Full release notes]({release_url})")
                else:
                    lines.append(body)
            else:
                lines.append(body)
            lines.append("")
        lines.append("</details>")
        lines.append("")
    return "\n".join(lines).rstrip()


def build_comparison_urls(
    changes: list[tuple[str, str, str]],
    notes: dict[str, tuple[str, list[tuple[str, str]]]],
) -> dict[str, str]:
    """Build GitHub comparison URLs from version changes and release notes.

    Uses the tag format discovered by {func}`fetch_release_notes` to construct
    comparison URLs. Only packages with both old and new versions and a known
    GitHub repository are included.

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
        prefix = "v"
        for tag, _ in versions:
            if tag:
                prefix = "v" if tag.startswith("v") else ""
                break
        urls[name] = f"{repo_url}/compare/{prefix}{old}...{prefix}{new}"
    return urls
