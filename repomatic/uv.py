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

"""uv lock file operations and vulnerability auditing.

This module provides utilities for managing `uv.lock` files: parsing versions,
computing diff tables, auditing for vulnerabilities, and fetching release notes
from GitHub.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import arrow
import tomlrt
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version
from tomlrt import Table

from .github.pr_body import sanitize_markdown_mentions
from .github.releases import get_github_release_body
from .pypi import (
    get_changelog_url as get_pypi_changelog_url,
    get_release_dates as get_pypi_release_dates,
    get_source_url as get_pypi_source_url,
)

if sys.version_info >= (3, 11):
    from enum import StrEnum
else:
    from backports.strenum import StrEnum

TYPE_CHECKING = False
if TYPE_CHECKING:
    from typing import Any


# ---------------------------------------------------------------------------
# Command builders
# ---------------------------------------------------------------------------


def uv_cmd(subcommand: str, *, frozen: bool = False) -> list[str]:
    """Build a `uv <subcommand>` command prefix with standard flags.

    Always includes `--no-progress`.  Adds `--frozen` when requested
    (appropriate for `run`, `export`, `sync` — not for `lock`).
    """
    cmd = ["uv", "--no-progress", subcommand]
    if frozen:
        cmd.append("--frozen")
    return cmd


def uvx_cmd(exclude_newer: str | None = None) -> list[str]:
    """Build a `uvx` command prefix with standard flags.

    When *exclude_newer* is set (a `YYYY-MM-DD` date), adds `--exclude-newer` so
    the isolated resolution honors the `minimum-release-age` cooldown, gating the
    tool's transitive dependencies by upload date.
    """
    cmd = ["uvx", "--no-progress"]
    if exclude_newer:
        cmd += ["--exclude-newer", exclude_newer]
    return cmd


RELEASE_NOTES_MAX_LENGTH = 2000

"""Maximum characters per package release body before truncation."""

# ---------------------------------------------------------------------------
# uv audit parsing
# ---------------------------------------------------------------------------

MIN_UV_AUDIT_JSON_VERSION = Version("0.11.15")
"""Minimum `uv` version exposing `uv audit --output-format json`.

The structured JSON output landed in uv 0.11.15 as a preview feature. Below
this, `uv audit` emits only human-readable text, so `_run_uv_audit`
refuses to run rather than silently scanning nothing.
"""

_SUPPORTED_AUDIT_SCHEMA_VERSIONS = frozenset({"preview"})
"""`uv audit --output-format json` schema versions the parser understands.

The JSON layout is a uv *preview* feature whose schema may change without
warning, so {func}`parse_uv_audit_json` gates on the report's advertised
`schema.version` and raises for any version not listed here (rather than risk
misreading a changed layout as "no vulnerabilities"). Add a version once its
field layout is verified against the parser.
"""


class AdvisorySource(StrEnum):
    """Where a vulnerability advisory was detected.

    Each source has a distinct upstream database and ingestion pipeline, so
    coverage diverges in practice (e.g., GHSA frequently lists a CVE before
    the PyPA Advisory Database mirrors it). Tracking the source per
    {class}`VulnerablePackage` lets the union deduplicate by advisory ID
    while still attributing each entry to the database that produced it.
    """

    UV_AUDIT = "uv-audit"
    """Detected by `uv audit` (PyPA Advisory Database, OSV-backed)."""

    GITHUB_ADVISORIES = "github-advisories"
    """Detected via the repository's Dependabot alerts (GitHub Advisory Database)."""


@dataclass
class VulnerablePackage:
    """A single vulnerability advisory for a Python package."""

    name: str
    """Package name."""

    current_version: str
    """Currently resolved version."""

    advisory_id: str
    """Advisory identifier (e.g., `GHSA-xxxx-xxxx-xxxx`)."""

    advisory_title: str
    """Short description of the vulnerability."""

    fixed_version: str
    """Version that contains the fix, or empty string if unknown."""

    advisory_url: str
    """URL to the advisory details."""

    aliases: set[str] = field(default_factory=set)
    """Alternate identifiers for the same advisory (CVE, GHSA, PYSEC, OSV).

    Advisory databases cross-reference each other: the PyPA database (via
    `uv audit`) keys records by OSV/`PYSEC` IDs while listing the matching
    `GHSA`/`CVE` IDs as aliases, and Dependabot keys by `GHSA` while listing
    the `CVE`. {func}`collect_vulnerable_packages` unions entries whose
    identifier sets overlap, so a shared alias deduplicates the same
    advisory reported under different primary IDs by different sources.
    """

    sources: set[AdvisorySource] = field(default_factory=set)
    """Advisory databases that surfaced this entry.

    A set rather than a single value because the same advisory can be
    reported by multiple sources after deduplication. Empty when the
    advisory came from a code path that pre-dates source attribution.
    """

    source_urls: dict[AdvisorySource, str] = field(default_factory=dict)
    """Per-source URL pointing to the advisory page in each database.

    Each source has its own canonical URL even when reporting the same
    advisory ID (PyPA's `osv.dev` page vs. GitHub's `/advisories/` page),
    so the rendered table can link the source name to the database that
    actually surfaced it.
    """


def parse_uv_audit_json(output: str) -> list[VulnerablePackage]:
    """Parse `uv audit --output-format json` output into vulnerability records.

    The structured contract avoids the regex fragility of scraping
    human-readable lines, and exposes the advisory `aliases` (cross-referenced
    CVE/GHSA/PYSEC IDs) that let {func}`collect_vulnerable_packages`
    deduplicate the same advisory across sources.

    :param output: stdout from `uv audit --output-format json`.
    :return: A list of {class}`VulnerablePackage` entries (empty when the
        audit found nothing).
    :raises RuntimeError: when the output is unusable as JSON (empty,
        malformed, or carrying an unrecognized `schema.version`). Raising
        rather than returning an empty list keeps the scanner from silently
        passing when the preview schema changes under it.
    """
    output = output.strip()
    if not output:
        raise RuntimeError("`uv audit --output-format json` produced no output.")
    try:
        report = json.loads(output)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"`uv audit` did not return valid JSON: {error}.") from error
    # A non-object payload (list, scalar) has no recognizable schema, so it
    # fails the same version guard as a missing or unknown schema version.
    schema = report.get("schema") if isinstance(report, dict) else None
    version = schema.get("version") if isinstance(schema, dict) else None
    if version not in _SUPPORTED_AUDIT_SCHEMA_VERSIONS:
        raise RuntimeError(
            f"Unrecognized `uv audit` JSON schema version {version!r}; expected "
            f"one of {sorted(_SUPPORTED_AUDIT_SCHEMA_VERSIONS)}. The preview "
            "schema may have changed: update parse_uv_audit_json."
        )

    vulns: list[VulnerablePackage] = []
    for entry in report.get("vulnerabilities") or []:
        dependency = entry.get("dependency") or {}
        # `display_id` is uv's preferred human-facing identifier (matching its
        # text output); `id` is the OSV record's primary key. Keep every other
        # known identifier as an alias for cross-source deduplication.
        primary = entry.get("display_id") or entry.get("id") or ""
        aliases = {entry.get("id") or "", *(entry.get("aliases") or [])}
        aliases.discard("")
        aliases.discard(primary)
        fix_versions = entry.get("fix_versions") or []
        url = entry.get("link") or ""
        vulns.append(
            VulnerablePackage(
                name=dependency.get("name", ""),
                current_version=dependency.get("version", ""),
                advisory_id=primary,
                advisory_title=entry.get("summary") or "",
                fixed_version=", ".join(fix_versions),
                advisory_url=url,
                aliases=aliases,
                sources={AdvisorySource.UV_AUDIT},
                source_urls={AdvisorySource.UV_AUDIT: url} if url else {},
            )
        )
    return vulns


def format_vulnerability_table(vulns: list[VulnerablePackage]) -> str:
    """Format vulnerability data as a markdown table.

    Includes a `Sources` column listing the advisory databases that surfaced
    each entry, so reviewers can see which database (PyPA Advisory DB,
    GitHub Advisory DB, or both) detected the vulnerability.

    :param vulns: List of {class}`VulnerablePackage` entries.
    :return: A markdown string with a `## Vulnerabilities` heading and table,
        or an empty string if no vulnerabilities are provided.
    """
    if not vulns:
        return ""
    lines = [
        "## Vulnerabilities",
        "",
        "| Package | Advisory | Current | Fixed | Sources |",
        "| :-- | :-- | :-- | :-- | :-- |",
    ]
    for v in vulns:
        pkg_link = f"[{v.name}](https://pypi.org/project/{v.name}/)"
        if v.advisory_url:
            adv_link = f"[{v.advisory_id}]({v.advisory_url})"
        else:
            adv_link = v.advisory_id
        fixed = f"`{v.fixed_version}`" if v.fixed_version else "unknown"
        # Link each source name to its own advisory URL so reviewers can
        # inspect both databases when they agree.
        source_cells: list[str] = []
        for s in sorted(v.sources, key=lambda s: s.value):
            url = v.source_urls.get(s, "")
            label = f"`{s.value}`"
            source_cells.append(f"[{label}]({url})" if url else label)
        sources = ", ".join(source_cells) or "—"
        lines.append(
            f"| {pkg_link} | {adv_link}: {v.advisory_title} "
            f"| `{v.current_version}` | {fixed} | {sources} |"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# pyproject.toml exclude-newer-package management
# ---------------------------------------------------------------------------

_RELATIVE_DURATION_RE = re.compile(r"^(\d+)\s+(seconds?|minutes?|hours?|days?|weeks?)$")
"""Matches uv's "friendly" relative duration syntax.

Accepts `N second(s)`, `N minute(s)`, `N hour(s)`, `N day(s)`, and
`N week(s)`. Calendar units (months, years) are not allowed: uv resolves
durations to a fixed number of seconds (a day is 24 hours, DST ignored),
so calendar arithmetic would be ambiguous. See
[astral-sh/uv#19475](https://github.com/astral-sh/uv/pull/19475) for the
canonical surface uv documents on this field.
"""

_LOCK_DURATION_RE = re.compile(
    r"^P"
    r"(?:(?P<weeks>\d+)W)?"
    r"(?:(?P<days>\d+)D)?"
    r"(?:T"
    r"(?:(?P<hours>\d+)H)?"
    r"(?:(?P<minutes>\d+)M)?"
    r"(?:(?P<seconds>\d+(?:\.\d+)?)S)?"
    r")?$"
)
"""Matches the subset of ISO 8601 durations uv emits in `uv.lock`'s
`options.exclude-newer-span`: `P{N}W`, `P{N}D`, `PT{N}S`, and combinations."""

LOCK_TIMESTAMP_SENTINEL = "0001-01-01T00:00:00Z"
"""Placeholder uv writes to `options.exclude-newer` in `uv.lock` when the
user-configured value is a relative span. The real cutoff is in
`options.exclude-newer-span` as an ISO 8601 duration."""


def _build_inline_table(entries: dict[str, str]) -> Table:
    """Build a tomlrt inline table with pyproject-fmt-compatible formatting.

    :param entries: Mapping of key-value pairs for the inline table.
    :return: A `tomlrt` inline `Table` with canonical formatting.
    """
    return Table.inline({k: entries[k] for k in sorted(entries)})


def _parse_relative_duration(value: str) -> timedelta | None:
    """Parse a uv "friendly" relative duration string into a timedelta.

    Handles `N second(s)`, `N minute(s)`, `N hour(s)`, `N day(s)`, and
    `N week(s)`. Calendar units (months, years) are not allowed.

    :param value: The duration string from `exclude-newer` in
        `pyproject.toml`.
    :return: A {class}`~datetime.timedelta`, or `None` if the value is not
        a recognized relative duration.
    """
    match = _RELATIVE_DURATION_RE.match(value.strip())
    if not match:
        return None
    count = int(match.group(1))
    unit = match.group(2)
    if unit.startswith("second"):
        return timedelta(seconds=count)
    if unit.startswith("minute"):
        return timedelta(minutes=count)
    if unit.startswith("hour"):
        return timedelta(hours=count)
    if unit.startswith("week"):
        return timedelta(weeks=count)
    return timedelta(days=count)


def _parse_lock_duration(value: str) -> timedelta | None:
    """Parse an ISO 8601 duration from `uv.lock` into a timedelta.

    Handles the subset uv emits in `options.exclude-newer-span` and the
    per-package `span` field: `P{N}W`, `P{N}D`, `PT{N}S`, and
    combinations like `P1DT2H`.

    :param value: The duration string from `uv.lock`.
    :return: A {class}`~datetime.timedelta`, or `None` if the value is
        not a recognized lock-file duration. A bare `P` returns `None`.
    """
    match = _LOCK_DURATION_RE.match(value.strip())
    if not match:
        return None
    parts = match.groupdict()
    if not any(parts.values()):
        return None
    return timedelta(
        weeks=int(parts["weeks"] or 0),
        days=int(parts["days"] or 0),
        hours=int(parts["hours"] or 0),
        minutes=int(parts["minutes"] or 0),
        seconds=float(parts["seconds"] or 0),
    )


def _parse_iso_datetime(value: str) -> datetime | None:
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


def _resolve_exclude_newer_cutoff(value: str) -> datetime | None:
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
    :return: An absolute cutoff datetime, or `None` if *value* is empty
        or matches none of the recognized forms.
    """
    if not value:
        return None
    duration = _parse_relative_duration(value) or _parse_lock_duration(value)
    if duration is not None:
        return datetime.now(timezone.utc) - duration
    return _parse_iso_datetime(value)


def _packages_outside_cooldown(
    pyproject_path: Path,
    lock_path: Path,
    packages: set[str],
) -> set[str]:
    """Return the subset of *packages* whose upload time exceeds the cooldown.

    A package needs an `exclude-newer-package` exemption only when its locked
    version was uploaded *after* the `exclude-newer` cutoff, meaning a regular
    `uv lock --upgrade` would not resolve it.

    :param pyproject_path: Path to the `pyproject.toml` file.
    :param lock_path: Path to the `uv.lock` file.
    :param packages: Candidate package names.
    :return: The subset that actually requires a `"0 day"` override.
    """
    content = pyproject_path.read_text(encoding="UTF-8")
    doc = tomlrt.loads(content)
    exclude_newer_str = doc.get("tool", {}).get("uv", {}).get("exclude-newer", "")
    if not exclude_newer_str:
        return packages

    cutoff = _resolve_exclude_newer_cutoff(exclude_newer_str)
    if cutoff is None:
        # Cannot determine window: be safe and exempt everything.
        return packages

    upload_times = parse_lock_upload_times(lock_path)
    outside: set[str] = set()
    for pkg in packages:
        upload_str = upload_times.get(pkg, "")
        if not upload_str:
            # No upload time (git/path source): exempt to be safe.
            outside.add(pkg)
            continue
        upload_dt = _parse_iso_datetime(upload_str)
        if upload_dt is None:
            outside.add(pkg)
            continue
        if upload_dt >= cutoff:
            logging.info(
                f"Exempting {pkg}: upload time {upload_str} is after"
                " the exclude-newer cutoff."
            )
            outside.add(pkg)
        else:
            logging.info(
                f"Skipping exemption for {pkg}: upload time {upload_str}"
                " is within the exclude-newer window."
            )
    return outside


def _bare_date(value: str) -> date | None:
    """Parse an `exclude-newer-package` value that is a bare `YYYY-MM-DD` date.

    Returns the parsed {class}`~datetime.date` only when *value* is exactly a
    calendar date with no time component: the timezone-ambiguous form uv
    re-expands per locking-machine timezone. Relative spans (`0 day`), full
    RFC 3339 timestamps, and unparsable values all return `None`.

    :param value: An `exclude-newer-package` entry value.
    :return: The {class}`~datetime.date`, or `None` when *value* is not a
        bare date.
    """
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value.strip()):
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


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
    cutoff = datetime(day.year, day.month, day.day, tzinfo=timezone.utc) + timedelta(
        days=1
    )
    return cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")


def _freeze_cutoff(upload_str: str) -> str | None:
    """Freeze cutoff that holds a package at its currently-locked version.

    A cooldown bypass must *hold* the locked version, not track the latest
    release (which is what a `"0 day"` span does: it disables the cooldown,
    so `uv lock --upgrade` keeps pulling newer releases and the entry never
    ages out). Returns an `exclude-newer-package` cutoff just past the locked
    version's upload time so uv keeps that version and rejects anything
    published later.

    The cutoff is the day after the upload, rendered as an explicit UTC
    timestamp via {func}`date_to_utc_cutoff` rather than a bare `YYYY-MM-DD`
    date. A bare date is re-expanded in the locking machine's local timezone,
    making `uv.lock` ping-pong between locks run locally and in CI; the
    timestamp is stored verbatim. The day-plus margin keeps the held version
    safely inside the window.

    :param upload_str: The locked version's `upload-time` from `uv.lock`.
    :return: A `YYYY-MM-DDT00:00:00Z` cutoff timestamp, or `None` when
        *upload_str* is empty or unparsable (git and path sources have no
        upload time).
    """
    upload_dt = _parse_iso_datetime(upload_str)
    if upload_dt is None:
        return None
    return date_to_utc_cutoff((upload_dt + timedelta(days=1)).date())


def _bypass_entries(pyproject_path: Path) -> dict[str, str]:
    """Read the `[tool.uv].exclude-newer-package` entries from `pyproject.toml`.

    :param pyproject_path: Path to the `pyproject.toml` file.
    :return: Package name to raw entry value (a freeze timestamp or a relative
        span), or an empty mapping when the file or the table is absent.
    """
    if not pyproject_path.exists():
        return {}
    content = pyproject_path.read_text(encoding="UTF-8")
    table = tomlrt.loads(content).get("tool", {}).get("uv", {})
    pkg_table = table.get("exclude-newer-package")
    if not pkg_table:
        return {}
    return {pkg: str(value) for pkg, value in pkg_table.items()}


def upsert_exclude_newer_packages(
    pyproject_path: Path,
    entries: dict[str, str],
) -> bool:
    """Insert or replace `[tool.uv].exclude-newer-package` entries.

    The write primitive shared by {func}`add_exclude_newer_packages` (which
    computes freeze cutoffs from the lock and never overwrites) and
    `sync-dep-sources` (which supplies exact cutoffs and must replace the
    stale value a git-tracking era left behind).

    :param pyproject_path: Path to the `pyproject.toml` file.
    :param entries: Package name to cutoff value (a freeze timestamp or a
        relative span). Existing entries for the same names are overwritten.
    :return: `True` if the file was updated, `False` if no changes were
        needed.
    """
    content = pyproject_path.read_text(encoding="UTF-8")
    doc = tomlrt.loads(content)

    uv = doc.get("tool", {}).get("uv")
    if uv is None:
        logging.warning(
            f"No [tool.uv] found in {pyproject_path}."
            " Cannot persist cooldown exemptions."
        )
        return False

    pkg_table = uv.get("exclude-newer-package")
    if pkg_table is None and "exclude-newer" not in uv:
        logging.warning(
            "No [tool.uv] exclude-newer or exclude-newer-package found in"
            f" {pyproject_path}. Cannot persist cooldown exemptions."
        )
        return False

    # Merge existing entries with the new ones and rebuild the inline table
    # to produce pyproject-fmt-compatible formatting.
    all_entries = dict(pkg_table) if pkg_table is not None else {}
    if all(all_entries.get(pkg) == value for pkg, value in entries.items()):
        logging.debug("All exclude-newer-package entries already up to date.")
        return False
    all_entries.update(entries)
    new_table = _build_inline_table(all_entries)
    if pkg_table is not None:
        uv["exclude-newer-package"] = new_table
    else:
        # Insert right after `exclude-newer` to match pyproject-fmt's
        # ordering rule for `[tool.uv]`. Appending at the end would
        # produce a file that pyproject-fmt rewrites on its next run,
        # leaking the security fix into a follow-up format-pyproject PR.
        uv["exclude-newer-package"] = new_table
        order = [key for key in uv if key != "exclude-newer-package"]
        order.insert(order.index("exclude-newer") + 1, "exclude-newer-package")
        uv.sort(key=order.index)

    pyproject_path.write_text(tomlrt.dumps(doc), encoding="UTF-8")
    logging.info(
        f"Upserted {', '.join(sorted(entries))} in exclude-newer-package"
        f" in {pyproject_path}."
    )
    return True


def add_exclude_newer_packages(
    pyproject_path: Path,
    packages: set[str],
    lock_path: Path,
) -> bool:
    """Add packages to `[tool.uv].exclude-newer-package` in `pyproject.toml`.

    Persists for each package the `_freeze_cutoff` of its currently-locked
    version (just after that version shipped) so that subsequent
    `uv lock --upgrade` runs (e.g. from the `sync-uv-lock` job) hold the
    package at that version instead of tracking newer releases, until it
    ages past the `exclude-newer` cooldown and
    {func}`prune_stale_exclude_newer_packages` drops the entry. Packages
    with no upload time in the lock (git or path sources) fall back to a
    permanent `"0 day"` span.

    Skips packages that already have an entry. Returns `True` if the file
    was modified.

    :param pyproject_path: Path to the `pyproject.toml` file.
    :param packages: Package names to add.
    :param lock_path: Path to the `uv.lock` file, read to resolve each
        package's locked-version upload time.
    :return: `True` if the file was updated, `False` if no changes were
        needed.
    """
    existing = set(_bypass_entries(pyproject_path))
    to_add = packages - existing
    if not to_add:
        logging.debug("All packages already in exclude-newer-package, nothing to add.")
        return False

    # Freeze at the locked version; fall back to a permanent span when the
    # package has no PyPI upload time (git or path source).
    upload_times = parse_lock_upload_times(lock_path)
    entries = {
        pkg: _freeze_cutoff(upload_times.get(pkg, "")) or "0 day" for pkg in to_add
    }
    return upsert_exclude_newer_packages(pyproject_path, entries)


def freeze_exclude_newer_packages(pyproject_path: Path, lock_path: Path) -> set[str]:
    """Convert relative-span cooldown bypasses into fixed freeze cutoffs.

    A `"0 day"` (or any relative-span) `exclude-newer-package` entry tells
    uv to ignore the cooldown and resolve to the *latest* release, so the
    package keeps moving and {func}`prune_stale_exclude_newer_packages`
    never sees its locked version age out. Rewriting the span as the
    `_freeze_cutoff` of the locked version instead *holds* the
    package: newer releases are excluded until the held version ages past
    the global cooldown, at which point the entry is pruned and the package
    rejoins normal resolution.

    Also migrates any legacy bare `YYYY-MM-DD` fixed entry to the equivalent
    explicit UTC timestamp (see {func}`date_to_utc_cutoff`), so uv stops
    re-expanding it per locking-machine timezone. Entries already carrying a
    full timestamp are left untouched (idempotent). Packages with no upload
    time in the lock (git or path sources) keep their span: they have no PyPI
    release to freeze against.

    :param pyproject_path: Path to the `pyproject.toml` file.
    :param lock_path: Path to the `uv.lock` file.
    :return: The names of the packages whose entry was rewritten (span frozen
        or bare date pinned); empty when no entry needed rewriting (the file
        is then left untouched).
    """
    content = pyproject_path.read_text(encoding="UTF-8")
    doc = tomlrt.loads(content)
    pkg_table = doc.get("tool", {}).get("uv", {}).get("exclude-newer-package")
    if not pkg_table:
        return set()

    upload_times = parse_lock_upload_times(lock_path)
    frozen: dict[str, str] = {}
    rewritten: set[str] = set()
    for pkg, value in pkg_table.items():
        text = str(value)
        # Only relative spans track the latest release; fixed cutoffs already hold.
        is_span = (
            _parse_relative_duration(text) is not None
            or _parse_lock_duration(text) is not None
        )
        if is_span:
            freeze = _freeze_cutoff(upload_times.get(pkg, ""))
            if freeze is not None:
                frozen[pkg] = freeze
                rewritten.add(pkg)
                logging.info(f"Freezing {pkg} cooldown bypass at {freeze}.")
                continue
            # Git/path source with no release to freeze against: keep the span.
            frozen[pkg] = value
            continue
        # Fixed cutoff. Upgrade a legacy bare YYYY-MM-DD date to an explicit
        # UTC timestamp so uv stops re-expanding it per locking-machine
        # timezone. Already-pinned timestamps return None here, keeping the
        # pass idempotent.
        bare = _bare_date(text)
        if bare is not None:
            pinned = date_to_utc_cutoff(bare)
            frozen[pkg] = pinned
            rewritten.add(pkg)
            logging.info(f"Pinning {pkg} cooldown date {text} to {pinned}.")
            continue
        frozen[pkg] = value

    if not rewritten:
        return set()

    doc["tool"]["uv"]["exclude-newer-package"] = _build_inline_table(frozen)
    pyproject_path.write_text(tomlrt.dumps(doc), encoding="UTF-8")
    return rewritten


def prune_stale_exclude_newer_packages(
    pyproject_path: Path,
    lock_path: Path,
) -> set[str]:
    """Remove stale entries from `[tool.uv].exclude-newer-package`.

    ```{note}
    This is a workaround until uv supports native pruning.
    See [uv#18792](https://github.com/astral-sh/uv/issues/18792).
    ```

    An entry is stale when its locked version's upload time falls before the
    `exclude-newer` cutoff, meaning `uv lock --upgrade` would resolve to
    the same (or newer) version without the `"0 day"` override.

    Packages without an upload time in the lock file (git or path sources)
    are treated as permanent exemptions and never pruned.

    :param pyproject_path: Path to the `pyproject.toml` file.
    :param lock_path: Path to the `uv.lock` file.
    :return: The names of the pruned packages; empty when nothing was stale
        (the file is then left untouched).
    """
    content = pyproject_path.read_text(encoding="UTF-8")
    doc = tomlrt.loads(content)
    uv = doc.get("tool", {}).get("uv", {})

    exclude_newer_str = uv.get("exclude-newer", "")
    pkg_table = uv.get("exclude-newer-package")
    if not pkg_table or not exclude_newer_str:
        return set()

    cutoff = _resolve_exclude_newer_cutoff(exclude_newer_str)
    if cutoff is None:
        logging.warning(
            f"Cannot parse exclude-newer value {exclude_newer_str!r}; skipping prune."
        )
        return set()

    upload_times = parse_lock_upload_times(lock_path)

    stale: set[str] = set()
    for pkg in pkg_table:
        upload_str = upload_times.get(pkg, "")
        if not upload_str:
            # No upload time: git/path source, permanent exemption.
            logging.debug(f"Keeping {pkg}: no upload time in lock.")
            continue
        upload_dt = _parse_iso_datetime(upload_str)
        if upload_dt is None:
            continue
        if upload_dt < cutoff:
            stale.add(pkg)
            logging.info(f"Pruning {pkg}: upload time {upload_str} is before cutoff.")
        else:
            logging.debug(f"Keeping {pkg}: upload time {upload_str} is after cutoff.")

    if not stale:
        logging.debug("No stale exclude-newer-package entries.")
        return set()

    # Rebuild the inline table without stale entries to produce
    # pyproject-fmt-compatible formatting.
    remaining = {k: v for k, v in pkg_table.items() if k not in stale}
    if remaining:
        uv["exclude-newer-package"] = _build_inline_table(remaining)
    else:
        del uv["exclude-newer-package"]

    result = tomlrt.dumps(doc)
    pyproject_path.write_text(result, encoding="UTF-8")
    logging.info(
        f"Pruned {', '.join(sorted(stale))} from"
        f" exclude-newer-package in {pyproject_path}."
    )
    return stale


# ---------------------------------------------------------------------------
# Lock file version parsing
# ---------------------------------------------------------------------------


def parse_lock_versions(lock_path: Path) -> dict[str, str]:
    """Parse a `uv.lock` file and return a mapping of package names to versions.

    :param lock_path: Path to the `uv.lock` file.
    :return: A dict mapping normalized package names to their version strings.
    """
    if not lock_path.exists():
        return {}
    with lock_path.open("rb") as f:
        data = tomlrt.load(f)
    return {
        pkg["name"]: pkg["version"]
        for pkg in data.get("package", [])
        if "name" in pkg and "version" in pkg
    }


def parse_lock_upload_times(lock_path: Path) -> dict[str, str]:
    """Parse a `uv.lock` file and return a mapping of package names to upload times.

    Extracts the `upload-time` field from each package's `sdist` entry.

    :param lock_path: Path to the `uv.lock` file.
    :return: A dict mapping normalized package names to ISO 8601 upload-time
        strings. Packages without an `sdist` or `upload-time` are omitted.
    """
    if not lock_path.exists():
        return {}
    with lock_path.open("rb") as f:
        data = tomlrt.load(f)
    result = {}
    for pkg in data.get("package", []):
        name = pkg.get("name", "")
        upload_time = pkg.get("sdist", {}).get("upload-time", "")
        if name and upload_time:
            result[name] = upload_time
    return result


def parse_lock_exclude_newer(lock_path: Path) -> str:
    """Parse the effective `exclude-newer` cutoff from a `uv.lock` file.

    When the user configures a relative span (`exclude-newer = "1 week"`
    in `pyproject.toml`), uv writes the {data}`LOCK_TIMESTAMP_SENTINEL`
    into `options.exclude-newer` and the real value into
    `options.exclude-newer-span` as an ISO 8601 duration. In that case
    the effective cutoff is computed as `now - span`.

    :param lock_path: Path to the `uv.lock` file.
    :return: An ISO 8601 datetime string for the effective cutoff, or an
        empty string if neither field is present (or the sentinel is
        present without a parseable span).
    """
    if not lock_path.exists():
        return ""
    with lock_path.open("rb") as f:
        data = tomlrt.load(f)
    options = data.get("options", {})
    timestamp: str = options.get("exclude-newer", "")
    if timestamp and timestamp != LOCK_TIMESTAMP_SENTINEL:
        return timestamp
    span = options.get("exclude-newer-span", "")
    if span:
        duration = _parse_lock_duration(span)
        if duration is not None:
            cutoff = datetime.now(timezone.utc) - duration
            return cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
    return ""


def load_lock_data(lock_path: Path | None = None) -> dict[str, Any]:
    """Load and parse a `uv.lock` file.

    :param lock_path: Path to uv.lock file. If None, looks in current directory.
    :return: Parsed TOML data as a dict, or empty dict if the file does not exist.
    """
    if lock_path is None:
        lock_path = Path("uv.lock")
    if not lock_path.exists():
        return {}
    with lock_path.open("rb") as f:
        return tomlrt.load(f)


@dataclass
class LockSpecifiers:
    """Dependency specifiers extracted from a `uv.lock` file.

    Two views of the same data, built in a single pass over the lock packages:

    `by_package`
        ``{package_name: {dep_name: specifier}}``. Every dependency declared by
        a package (main and dev) keyed by the declaring package name. Used for
        edge labels in dependency graphs.

    `by_subgraph`
        ``{subgraph_name: {dep_name: specifier}}``. Primary dependencies keyed
        by dev-group name or extra name. Used for node labels inside subgraphs.
    """

    by_package: dict[str, dict[str, str]]
    by_subgraph: dict[str, dict[str, str]]


def parse_lock_specifiers(
    lock_path: Path | None = None,
    *,
    lock_data: dict[str, Any] | None = None,
) -> LockSpecifiers:
    """Parse `uv.lock` and extract dependency specifiers.

    A single pass builds two complementary indexes from
    `[package.metadata].requires-dist` and
    `[package.metadata.requires-dev]`. See {class}`LockSpecifiers` for the
    two views returned.

    :param lock_path: Path to uv.lock file. If None, looks in current directory.
        Ignored when *lock_data* is provided.
    :param lock_data: Pre-loaded lock data from {func}`load_lock_data`. When
        provided, skips file I/O.
    """
    if lock_data is None:
        lock_data = load_lock_data(lock_path)

    by_package: dict[str, dict[str, str]] = {}
    by_subgraph: dict[str, dict[str, str]] = {}

    for package in lock_data.get("package", []):
        pkg_name = package.get("name", "")
        if not pkg_name:
            continue

        pkg_deps: dict[str, str] = {}
        metadata = package.get("metadata", {})

        # Parse requires-dist for main dependencies.
        for dep in metadata.get("requires-dist", []):
            if not isinstance(dep, dict):
                continue
            dep_name = dep.get("name", "")
            specifier = dep.get("specifier", "")
            if dep_name and specifier:
                pkg_deps[dep_name] = specifier
            # Also index by extra when a marker is present.
            marker = dep.get("marker", "")
            match = re.match(r"extra\s*==\s*'([^']+)'", marker)
            if match and dep_name:
                extra_name = match.group(1)
                by_subgraph.setdefault(extra_name, {})[dep_name] = specifier

        # Parse requires-dev for dev group dependencies.
        requires_dev = metadata.get("requires-dev", {})
        for group_name, group_deps in requires_dev.items():
            group_specs: dict[str, str] = {}
            for dep in group_deps:
                if isinstance(dep, dict):
                    dep_name = dep.get("name", "")
                    specifier = dep.get("specifier", "")
                    if dep_name:
                        group_specs[dep_name] = specifier
                        if specifier:
                            pkg_deps[dep_name] = specifier
            if group_specs:
                by_subgraph[group_name] = group_specs

        if pkg_deps:
            by_package[pkg_name] = pkg_deps

    return LockSpecifiers(by_package=by_package, by_subgraph=by_subgraph)


# ---------------------------------------------------------------------------
# Lock file diff formatting
# ---------------------------------------------------------------------------


def format_upload_date(iso_datetime: str) -> str:
    """Format an ISO 8601 datetime as a human-readable date string.

    :param iso_datetime: An ISO 8601 datetime string (e.g.,
        `"2026-03-13T12:00:00Z"`).
    :return: A formatted date like `2026-03-13`, or the raw string if parsing
        fails.
    """
    dt = _parse_iso_datetime(iso_datetime)
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
    dt = _parse_iso_datetime(raw_upload)
    if dt is None:
        return format_upload_date(raw_upload)
    iso = dt.strftime("%Y-%m-%d")
    if reference is None:
        return iso
    return f"{iso} ({arrow.get(dt.date()).humanize(arrow.get(reference))})"


def diff_lock_versions(
    before: dict[str, str],
    after: dict[str, str],
) -> list[tuple[str, str, str]]:
    """Compare two version mappings and return the list of changes.

    :param before: Package versions before the upgrade.
    :param after: Package versions after the upgrade.
    :return: A sorted list of `(name, old_version, new_version)` tuples.
        `old_version` is empty for added packages; `new_version` is empty
        for removed packages.
    """
    changes = []
    for name in sorted(set(before) | set(after)):
        old = before.get(name, "")
        new = after.get(name, "")
        if old != new:
            changes.append((name, old, new))
    return changes


def pypi_name_urls(changes: list[tuple[str, str, str]]) -> dict[str, str]:
    """Map each changed package name to its PyPI project URL.

    Convenience for {func}`format_diff_table`'s `name_urls` when the changes
    come from a PyPI-resolved source (`sync-uv-lock`, `fix-vulnerable-deps`).
    """
    return {name: f"https://pypi.org/project/{name}/" for name, _old, _new in changes}


def format_exclude_newer_note(exclude_newer: str) -> str:
    """Render the uv `exclude-newer` cutoff sentence for a diff table.

    The {func}`format_diff_table` counterpart for `sync-uv-lock` and
    `fix-vulnerable-deps`, which gate on uv's absolute `exclude-newer`
    timestamp. The relative-cooldown updaters
    ({mod}`repomatic.version_sync`) render their own
    `minimum-release-age` note instead.

    :param exclude_newer: ISO 8601 datetime from the lock's
        `[options].exclude-newer`, as returned by
        {func}`parse_lock_exclude_newer`, or empty.
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
) -> str:
    """Format version changes as a markdown table with heading.

    The shared PR-body table for every dependency updater (`sync-uv-lock`,
    `fix-vulnerable-deps`, `sync-tool-versions`, `sync-action-pins`,
    `sync-workflow-pins`) so they all render identically.

    When `upload_times` is provided, a "Released" column is added so
    reviewers can visually verify that all updated packages respect the
    cooldown. When `cooldown_note` is provided, that pre-rendered sentence
    (the absolute `exclude-newer` cutoff for uv, or the relative
    `minimum-release-age` cutoff for the version-sync updaters) is shown
    above the table.

    :param changes: List of `(name, old_version, new_version)` tuples
        as returned by {func}`diff_lock_versions`.
    :param upload_times: Optional mapping of package names to ISO 8601
        upload-time strings, as returned by {func}`parse_lock_upload_times`.
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
    :return: A markdown string with a `## 🆙 {heading}` heading and table,
        or an empty string if there are no changes.
    """
    if not changes:
        return ""
    name_urls = name_urls or {}
    show_uploaded = bool(upload_times)
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
            raw_time = upload_times.get(name, "")  # type: ignore[union-attr]
            uploaded = format_released(raw_time, reference_date)
            lines.append(f"| {link} | {change} | {uploaded} |")
        else:
            lines.append(f"| {link} | {change} |")
    return "\n".join(lines)


@dataclass(frozen=True)
class HeldBackPackage:
    """A newer release withheld from the lock by the `exclude-newer` cooldown.

    Built by {func}`compute_held_back_packages` for the `## Held back by
    cooldown` report section: a package has already published a newer version,
    but it is still inside the cooldown window, so `uv lock --upgrade` keeps
    the older {attr}`locked_version`.
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


def _lock_cooldown_span(lock_path: Path) -> timedelta | None:
    """Return the cooldown span from a lock's `options.exclude-newer-span`.

    :param lock_path: Path to the `uv.lock` file.
    :return: The configured cooldown width as a {class}`~datetime.timedelta`,
        or `None` when no relative span is recorded.
    """
    if not lock_path.exists():
        return None
    with lock_path.open("rb") as f:
        data = tomlrt.load(f)
    span = data.get("options", {}).get("exclude-newer-span", "")
    return _parse_lock_duration(span) if span else None


def _format_eligible(eligible: date, today: date) -> str:
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


def compute_held_back_packages(lock_path: Path) -> list[HeldBackPackage]:
    """Find releases withheld from the lock only by the cooldown.

    Re-resolves the lock with the cooldown lifted and diffs the result against
    the in-cooldown lock. Both the global `exclude-newer` cutoff and every
    per-package `exclude-newer-package` freeze are raised to the current
    instant, so a release blocked by a cooldown-bypass freeze is reported like
    any cooldown-blocked one. That keeps the section's wording and "Eligible"
    math honest: {func}`prune_stale_exclude_newer_packages` drops a freeze as
    soon as its held version exits the window, so any release a freeze still
    blocks is necessarily inside the global window too, and becomes lockable
    on its own cooldown-exit date. Versions pinned by a specifier or capped by
    a `requires-python` bound resolve identically with and without the lift,
    so they are excluded.

    The probe writes `uv.lock` and restores it byte-for-byte in a `finally`,
    so the canonical in-cooldown lock is left untouched even when resolution
    or parsing fails.

    ```{note}
    This runs a second `uv lock` resolution. It is the report's only cost and
    is skipped by `sync-uv-lock --no-held-back`.
    ```

    :param lock_path: Path to the `uv.lock` file.
    :return: Held-back packages sorted by name. Empty when the probe fails or
        nothing is withheld.
    """
    if not lock_path.exists():
        return []
    locked = parse_lock_versions(lock_path)
    span = _lock_cooldown_span(lock_path)
    saved = lock_path.read_bytes()
    now = datetime.now(timezone.utc)
    cutoff = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    # A CLI --exclude-newer-package beats the pyproject.toml entry for the
    # same package, lifting each freeze for the duration of the probe.
    lifts: list[str] = []
    for pkg in sorted(_bypass_entries(lock_path.parent / "pyproject.toml")):
        lifts.extend(["--exclude-newer-package", f"{pkg}={cutoff}"])
    try:
        subprocess.run(
            [*uv_cmd("lock"), "--upgrade", "--exclude-newer", cutoff, *lifts],
            check=True,
            cwd=lock_path.parent,
        )
        latest = parse_lock_versions(lock_path)
        uploads = parse_lock_upload_times(lock_path)
    except subprocess.CalledProcessError:
        logging.warning(
            "Cooldown probe (uv lock --exclude-newer) failed; "
            "skipping the held-back report."
        )
        return []
    finally:
        lock_path.write_bytes(saved)

    held = []
    for name in sorted(locked.keys() & latest.keys()):
        old_version, new_version = locked[name], latest[name]
        if old_version == new_version:
            continue
        try:
            if Version(new_version) <= Version(old_version):
                continue
        except InvalidVersion:
            continue
        raw_upload = uploads.get(name, "")
        released = format_released(raw_upload, now.date())
        eligible = ""
        if raw_upload and span is not None:
            upload_dt = _parse_iso_datetime(raw_upload)
            if upload_dt is not None:
                eligible = _format_eligible((upload_dt + span).date(), now.date())
        held.append(HeldBackPackage(name, old_version, new_version, released, eligible))
    return held


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
    `released`/`eligible` strings {func}`compute_held_back_packages` produces
    for uv, so {func}`format_held_back_table` renders both identically. Unlike
    the uv path, no second resolution is needed: the candidates are already in
    hand from the datasource sweep.

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
    upload_dt = _parse_iso_datetime(available_date)
    if upload_dt is not None:
        eligible = _format_eligible((upload_dt + min_age).date(), today)
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
    {func}`compute_held_back_packages`) and the version-sync commands (rows
    from {func}`build_held_back`), so the section renders identically.

    :param held_back: Withheld releases as {class}`HeldBackPackage` rows.
    :param note: Intro paragraph describing the cooldown. Defaults to the uv
        `exclude-newer` wording; version-sync passes its `minimum-release-age`
        wording.
    :param name_urls: Optional mapping of names to a URL the name links to
        (PyPI, GitHub, npm). Names absent from the mapping render plain.
    :param subject: Header for the first column (e.g. `Action`, `Tool`).
    :return: A markdown string with a `## 🔜 Held back by cooldown` heading
        and table, or an empty string when *held_back* is empty.
    """
    if not held_back:
        return ""
    name_urls = name_urls or {}
    lines = [
        "## 🔜 Held back by cooldown",
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

    Built by {func}`compute_bypass_forecasts` (freezes still active) and
    {func}`compute_pruned_forecasts` (freezes the run just cleared) for the
    `## ❄️ Cooldown bypasses` report section: a fixed-timestamp
    `exclude-newer-package` entry holds {attr}`name` at {attr}`held_version`
    until that version ages past the `exclude-newer` cutoff, at which point
    `sync-uv-lock` prunes the entry and the package resumes normal cooldown
    resolution.
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


def _forecast_expiry(upload_str: str, span: timedelta | None, today: date) -> str:
    """Format the date a held version ages past the rolling cooldown cutoff.

    :param upload_str: The held version's `upload-time` from `uv.lock`.
    :param span: The rolling `exclude-newer` span, or `None` when the cutoff
        is absolute.
    :param today: Reference date for the relative hint.
    :return: The humanized expiry, {data}`BYPASS_NEEDS_RELEASE` when the
        version has no upload time, or empty when *span* is absent.
    """
    upload_dt = _parse_iso_datetime(upload_str)
    if upload_dt is None:
        return BYPASS_NEEDS_RELEASE
    if span is None:
        return ""
    return _format_eligible((upload_dt + span).date(), today)


def compute_bypass_forecasts(
    pyproject_path: Path,
    lock_path: Path,
) -> list[BypassForecast]:
    """Forecast when each active cooldown-bypass freeze self-clears.

    Covers only the fixed-timestamp `exclude-newer-package` entries. Relative
    spans (`"0 day"`) are permanent exemptions for packages with no PyPI
    release to age against (git or path sources), so they never expire and
    would repeat a static row in every report; auditing them is left to the
    dependency review (see `docs/dependencies.md`). Entries for packages
    absent from the lock (dropped dependencies) are skipped for the same
    reason.

    The expiry mirrors the {func}`prune_stale_exclude_newer_packages`
    condition: the held version's upload time plus the rolling
    `exclude-newer` span, which is the day the next `sync-uv-lock` run
    prunes the entry.

    :param pyproject_path: Path to the `pyproject.toml` file.
    :param lock_path: Path to the `uv.lock` file.
    :return: Forecasts sorted by package name; empty when there is no freeze.
    """
    versions = parse_lock_versions(lock_path)
    uploads = parse_lock_upload_times(lock_path)
    span = _lock_cooldown_span(lock_path)
    today = datetime.now(timezone.utc).date()
    forecasts = []
    for pkg, value in sorted(_bypass_entries(pyproject_path).items()):
        is_span = (
            _parse_relative_duration(value) is not None
            or _parse_lock_duration(value) is not None
        )
        if is_span:
            continue
        held_version = versions.get(pkg, "")
        if not held_version:
            continue
        expires = _forecast_expiry(uploads.get(pkg, ""), span, today)
        forecasts.append(BypassForecast(pkg, held_version, expires))
    return forecasts


def compute_pruned_forecasts(
    names: set[str],
    lock_path: Path,
) -> list[BypassForecast]:
    """Snapshot the freezes a prune just cleared, for their `(cleared)` rows.

    Must run against the pre-upgrade `uv.lock`: once the entry is pruned the
    package rejoins normal resolution, so the post-upgrade lock may hold a
    newer version whose upload time would misstate what the freeze held and
    when it aged out.

    :param names: Names of the pruned entries, as returned by
        {func}`prune_stale_exclude_newer_packages`.
    :param lock_path: Path to the `uv.lock` file, still pre-upgrade.
    :return: One record per pruned entry, sorted by package name, with the
        version the freeze held and the (past) date it expired.
    """
    if not names:
        return []
    versions = parse_lock_versions(lock_path)
    uploads = parse_lock_upload_times(lock_path)
    span = _lock_cooldown_span(lock_path)
    today = datetime.now(timezone.utc).date()
    return [
        BypassForecast(
            pkg,
            versions.get(pkg, ""),
            _forecast_expiry(uploads.get(pkg, ""), span, today),
        )
        for pkg in sorted(names)
    ]


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

    :param forecasts: Active freezes from {func}`compute_bypass_forecasts`.
    :param pruned: Expired entries the run removed, snapshot by
        {func}`compute_pruned_forecasts` before the prune.
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

    A "Release notes" heading with one collapsible section per package. Long
    release bodies are truncated to
    {data}`RELEASE_NOTES_MAX_LENGTH` characters with a link to the full release.

    :param notes: A dict mapping package names to `(repo_url, versions)`
        tuples where `versions` is a list of `(tag, body)` pairs, as
        returned by {func}`fetch_release_notes`.
    :return: A markdown string with the release notes section, or an empty
        string if no notes are available.
    """
    if not notes:
        return ""
    lines = ["## Release notes", ""]
    for name, (repo_url, versions) in sorted(notes.items()):
        lines.append("<details>")
        lines.append(f"<summary><code>{name}</code></summary>")
        lines.append("")
        for tag, body in versions:
            body = sanitize_markdown_mentions(body)
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


# ---------------------------------------------------------------------------
# High-level operations
# ---------------------------------------------------------------------------


def _uv_version() -> Version:
    """Return the version of the `uv` binary on `PATH`.

    :return: The version parsed from `uv --version`.
    :raises RuntimeError: when `uv --version` output cannot be parsed.
    """
    result = subprocess.run(
        ["uv", "--version"],
        capture_output=True,
        text=True,
        encoding="UTF-8",
        check=False,
    )
    # `uv --version` prints e.g. `uv 0.11.15 (abc1234 2026-05-18)`.
    match = re.search(r"\d+\.\d+\.\d+", result.stdout)
    if not match:
        raise RuntimeError(f"Could not parse uv version from {result.stdout!r}.")
    return Version(match.group())


def _run_uv_audit(lock_path: Path) -> list[VulnerablePackage]:
    """Run `uv audit --output-format json` and parse the result.

    Requires uv >= {data}`MIN_UV_AUDIT_JSON_VERSION` for the structured JSON
    output (a preview feature); raises when the `uv` on `PATH` is older rather
    than silently scanning nothing. See {func}`parse_uv_audit_json`.

    :param lock_path: Path to the `uv.lock` file (used to derive the project
        directory).
    :return: A list of {class}`VulnerablePackage` entries detected by
        `uv audit`. Empty when no vulnerabilities are found.
    :raises RuntimeError: when `uv` is older than the minimum, or its JSON
        output is unparsable.
    """
    version = _uv_version()
    if version < MIN_UV_AUDIT_JSON_VERSION:
        raise RuntimeError(
            "Vulnerability scanning requires uv >= "
            f"{MIN_UV_AUDIT_JSON_VERSION} for `uv audit --output-format json`, "
            f"but found uv {version}."
        )
    result = subprocess.run(
        [
            *uv_cmd("audit", frozen=True),
            "--output-format",
            "json",
            "--preview-features",
            "json-output",
        ],
        capture_output=True,
        text=True,
        encoding="UTF-8",
        check=False,
        cwd=lock_path.parent,
    )
    return parse_uv_audit_json(result.stdout)


def collect_vulnerable_packages(
    lock_path: Path,
    repo: str | None = None,
    sources: list[AdvisorySource] | None = None,
) -> list[VulnerablePackage]:
    """Collect vulnerability advisories from all configured sources.

    Queries each enabled advisory database, then deduplicates entries per
    package by advisory identity: two entries merge when their identifier
    sets (`advisory_id` plus `aliases`) overlap, so the same advisory
    reported under a PYSEC/OSV ID by `uv audit` and a GHSA ID by Dependabot
    collapses into one. Merging preserves the union of `sources` so the
    rendered table credits both databases when they agree.

    Current versions reported by `uv audit` take precedence over the empty
    placeholder produced by the GHSA path, since `uv audit` reads the actual
    locked version while Dependabot alerts only carry the vulnerable range.
    When the GHSA path encounters a package that `uv audit` did not surface,
    the current version is filled in from the lock file.

    :param lock_path: Path to the `uv.lock` file.
    :param repo: Repository in `owner/repo` format. Required for the
        {attr}`AdvisorySource.GITHUB_ADVISORIES` source; pass `None` to skip
        it (the result then reflects `uv audit` only).
    :param sources: Advisory databases to consult. Defaults to all known
        sources.
    :return: Deduplicated list of {class}`VulnerablePackage` entries.
    """
    if sources is None:
        sources = list(AdvisorySource)

    collected: list[VulnerablePackage] = []
    if AdvisorySource.UV_AUDIT in sources:
        collected.extend(_run_uv_audit(lock_path))
    if AdvisorySource.GITHUB_ADVISORIES in sources and repo:
        from .github.advisories import fetch_dependabot_alerts

        ghsa = fetch_dependabot_alerts(repo)
        # Backfill current versions that the alerts API does not report.
        # uv.lock stores names PEP 503-normalized (lowercase, dashes), while
        # GHSA preserves the package's display name (e.g., "GitPython").
        # Index the lock by canonical name so case/separator mismatches
        # still resolve to the locked version.
        if ghsa:
            locked = parse_lock_versions(lock_path)
            locked_canonical = {canonicalize_name(k): v for k, v in locked.items()}
            for v in ghsa:
                if v.current_version:
                    continue
                pkg_canonical = canonicalize_name(v.name)
                if pkg_canonical in locked_canonical:
                    v.current_version = locked_canonical[pkg_canonical]
            collected.extend(ghsa)

    # Deduplicate within each canonical package name, unioning sources. Two
    # advisories are the same when their identifier sets overlap: a shared
    # CVE/GHSA/PYSEC/OSV alias links the same advisory reported under
    # different primary IDs by different sources (e.g. a PYSEC from `uv audit`
    # and the equivalent GHSA from Dependabot).
    groups: dict[str, list[tuple[VulnerablePackage, set[str]]]] = {}
    for v in collected:
        ids = {v.advisory_id, *v.aliases}
        ids.discard("")
        bucket = groups.setdefault(canonicalize_name(v.name), [])
        for existing, existing_ids in bucket:
            if ids & existing_ids:
                existing_ids |= ids
                existing.aliases |= ids - {existing.advisory_id}
                existing.sources |= v.sources
                for src, url in v.source_urls.items():
                    existing.source_urls.setdefault(src, url)
                # Prefer non-empty fields from whichever source has them.
                if not existing.current_version and v.current_version:
                    existing.current_version = v.current_version
                if not existing.fixed_version and v.fixed_version:
                    existing.fixed_version = v.fixed_version
                if not existing.advisory_url and v.advisory_url:
                    existing.advisory_url = v.advisory_url
                if not existing.advisory_title and v.advisory_title:
                    existing.advisory_title = v.advisory_title
                break
        else:
            bucket.append((v, ids))

    return sorted(
        (entry for bucket in groups.values() for entry, _ids in bucket),
        key=lambda v: (v.name.lower(), v.advisory_id),
    )


def fix_vulnerable_deps(
    lock_path: Path,
    repo: str | None = None,
    sources: list[AdvisorySource] | None = None,
) -> tuple[bool, str]:
    """Detect vulnerable packages and upgrade them in the lock file.

    Queries every advisory source enabled by *sources* (defaults to all),
    then upgrades each fixable package with `uv lock --upgrade-package`
    using `--exclude-newer-package` to bypass the `exclude-newer` cooldown
    for security fixes. Also persists the exemptions in `pyproject.toml`
    so that subsequent `uv lock --upgrade` runs (e.g. from the
    `sync-uv-lock` job) do not downgrade the fixed packages back within
    the cooldown window.

    :param lock_path: Path to the `uv.lock` file.
    :param repo: Repository in `owner/repo` format. Required when
        {attr}`AdvisorySource.GITHUB_ADVISORIES` is among *sources*.
    :param sources: Advisory databases to consult. Defaults to all known
        sources.
    :return: A tuple of `(has_fixes, diff_table)`. `has_fixes` is `True`
        when at least one vulnerable package was upgraded. `diff_table` is a
        markdown-formatted string with vulnerability details and version changes,
        or an empty string if no fixable vulnerabilities were found.
    """
    # Step 1: Collect vulnerabilities from every enabled advisory source.
    vulns = collect_vulnerable_packages(lock_path, repo=repo, sources=sources)
    if not vulns:
        logging.info("No vulnerabilities found.")
        return False, ""

    # Deduplicate packages: multiple advisories can target the same package.
    fixable_packages = {v.name for v in vulns if v.fixed_version}
    if not fixable_packages:
        logging.warning(
            f"Found {len(vulns)} vulnerabilities but none have a known fix version."
        )
        return False, ""

    logging.info(
        f"Found {len(vulns)} vulnerabilities across"
        f" {len(fixable_packages)} fixable packages: {', '.join(sorted(fixable_packages))}."
    )

    # Step 3: Snapshot versions before upgrading.
    before = parse_lock_versions(lock_path)

    # Step 4: Upgrade all fixable packages in a single resolution pass.
    # Running one command avoids sequential re-resolution undoing earlier upgrades.
    cmd = [*uv_cmd("lock")]
    for pkg in sorted(fixable_packages):
        cmd.extend([
            "--upgrade-package",
            pkg,
            "--exclude-newer-package",
            f"{pkg}=0 day",
        ])
    logging.info(f"Upgrading: {', '.join(sorted(fixable_packages))}...")
    subprocess.run(cmd, check=True, cwd=lock_path.parent)

    # Step 5: Compute version diff.
    after = parse_lock_versions(lock_path)
    changes = diff_lock_versions(before, after)
    if not changes:
        logging.info("No version changes after upgrading vulnerable packages.")
        return False, ""

    # Step 6: Persist cooldown exemptions only for packages whose fixed
    # version falls outside the exclude-newer window. Packages already
    # reachable by a normal `uv lock --upgrade` do not need an override.
    pyproject_path = lock_path.parent / "pyproject.toml"
    if pyproject_path.exists():
        upgraded = {name for name, _old, _new in changes}
        needs_exemption = _packages_outside_cooldown(
            pyproject_path,
            lock_path,
            upgraded,
        )
        if needs_exemption:
            add_exclude_newer_packages(pyproject_path, needs_exemption, lock_path)

    # Step 7: Build the combined output.
    vuln_table = format_vulnerability_table(vulns)
    upload_times = parse_lock_upload_times(lock_path)
    exclude_newer = parse_lock_exclude_newer(lock_path)
    diff_table = format_diff_table(
        changes,
        upload_times,
        format_exclude_newer_note(exclude_newer),
        name_urls=pypi_name_urls(changes),
        reference_date=datetime.now(timezone.utc).date(),
    )

    # Fetch and append release notes.
    notes = fetch_release_notes(changes)
    notes_section = format_release_notes(notes)

    sections = [vuln_table, diff_table]
    if notes_section:
        sections.append(notes_section)
    combined = "\n\n".join(s for s in sections if s)

    return True, combined


@dataclass
class SyncResult:
    """Result of a `sync-uv-lock` operation."""

    changes: list[tuple[str, str, str]]
    """Version changes as `(name, old_version, new_version)` tuples."""

    upload_times: dict[str, str]
    """Package name to ISO 8601 upload-time mapping from the lock file."""

    exclude_newer: str
    """The `exclude-newer` cutoff from the lock file, or empty string."""

    reverted: bool = False
    """Whether a cosmetic-only re-lock was discarded.

    `True` when `uv lock --upgrade` changed no package versions and was not
    driven by a `pyproject.toml` cooldown edit, so {func}`sync_uv_lock`
    restored the pre-upgrade lock verbatim. See that function for why such a
    run is dropped.
    """

    pruned_bypasses: list[BypassForecast] = field(default_factory=list)
    """Expired `exclude-newer-package` entries removed from `pyproject.toml`,
    each with the version and (past) expiry the freeze had, snapshot against
    the pre-upgrade lock by {func}`compute_pruned_forecasts`."""

    frozen_bypasses: list[str] = field(default_factory=list)
    """`exclude-newer-package` entries rewritten into freeze cutoffs."""

    bypass_forecasts: list[BypassForecast] = field(default_factory=list)
    """Active cooldown-bypass freezes with their expiry forecasts (post-run
    state)."""


def sync_uv_lock(lock_path: Path) -> SyncResult:
    """Re-lock with `--upgrade` and report version changes.

    First prunes stale `exclude-newer-package` entries from
    `pyproject.toml` (entries whose locked version was uploaded before the
    `exclude-newer` cutoff), then runs `uv lock --upgrade` to update
    transitive dependencies.

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
    :return: A {class}`SyncResult` with structured version change data and
        the cooldown-bypass lifecycle (entries pruned, frozen, and still
        active with their expiry forecasts).
    """
    # Step 1: Prune bypasses whose held version has aged past the cooldown
    # (so uv resolves them normally again), then freeze the survivors at
    # their locked version so the upgrade below holds them instead of
    # tracking newer releases.
    pyproject_path = lock_path.parent / "pyproject.toml"
    pruned: set[str] = set()
    pruned_records: list[BypassForecast] = []
    frozen: set[str] = set()
    if pyproject_path.exists():
        pruned = prune_stale_exclude_newer_packages(pyproject_path, lock_path)
        # Snapshot the cleared freezes now, while the lock still holds the
        # versions the entries froze (see compute_pruned_forecasts).
        pruned_records = compute_pruned_forecasts(pruned, lock_path)
        frozen = freeze_exclude_newer_packages(pyproject_path, lock_path)
    pyproject_changed = bool(pruned or frozen)

    # Step 2: Snapshot versions and the raw lock bytes before upgrading. The
    # bytes let Step 5 restore the lock verbatim when the upgrade is
    # cosmetic-only.
    before = parse_lock_versions(lock_path)
    lock_before = lock_path.read_bytes() if lock_path.exists() else None

    # Step 3: Run uv lock --upgrade in the project directory.
    project_dir = lock_path.parent
    logging.info(f"Running uv lock --upgrade in {project_dir}...")
    subprocess.run([*uv_cmd("lock"), "--upgrade"], check=True, cwd=project_dir)

    # Step 4: Compute version diff.
    after = parse_lock_versions(lock_path)
    changes = diff_lock_versions(before, after)
    upload_times = parse_lock_upload_times(lock_path)
    exclude_newer = parse_lock_exclude_newer(lock_path)

    # Step 5: Discard a cosmetic-only re-lock. With no version changes and no
    # cooldown edit behind the run, any remaining diff is uv's
    # nondeterministic marker re-normalization (see the note above); keeping
    # it would open an empty sync PR that the next machine reverts.
    reverted = False
    if (
        not changes
        and not pyproject_changed
        and lock_before is not None
        and lock_path.read_bytes() != lock_before
    ):
        lock_path.write_bytes(lock_before)
        reverted = True
        logging.info(
            "Restored uv.lock: uv lock --upgrade changed no package versions, "
            "only equivalent environment markers. Discarding cosmetic churn."
        )

    # Step 6: Forecast the expiry of the freezes still active, on the final
    # pyproject.toml and lock state the run leaves behind.
    return SyncResult(
        changes=changes,
        upload_times=upload_times,
        exclude_newer=exclude_newer,
        reverted=reverted,
        pruned_bypasses=pruned_records,
        frozen_bypasses=sorted(frozen),
        bypass_forecasts=compute_bypass_forecasts(pyproject_path, lock_path),
    )
