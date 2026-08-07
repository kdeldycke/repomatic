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

"""uv lock file operations.

Utilities for managing `uv.lock` files: parsing versions, computing version
diffs and cooldown forecasts (held-back releases, bypass expiries), and managing
`exclude-newer-package` cooldown overrides. The shared markdown rendering of
these results lives in {mod}`repomatic.dep_report`.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import tomlrt
from click_extra import parse_friendly_duration, parse_iso8601_duration
from packaging.version import InvalidVersion, Version
from tomlrt import Table

from .dep_report import (
    BYPASS_NEEDS_RELEASE,
    BypassForecast,
    HeldBackPackage,
    format_eligible,
    format_released,
    parse_iso_datetime,
)

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


# ---------------------------------------------------------------------------
# pyproject.toml exclude-newer-package management
# ---------------------------------------------------------------------------

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
    duration = parse_friendly_duration(value) or parse_iso8601_duration(value)
    if duration is not None:
        return datetime.now(timezone.utc) - duration
    return parse_iso_datetime(value)


def project_exclude_newer(pyproject_path: Path) -> str:
    """Read the project's own `[tool.uv] exclude-newer` window.

    ```{caution}
    Always pass this back to `uv lock` and `uv sync` as an explicit
    `--exclude-newer` flag rather than letting uv pick the value up from
    `pyproject.toml` on its own. CI exports a `UV_EXCLUDE_NEWER` covering every
    ad-hoc install (see `claude.md` § Cooldown on every install), and that
    environment variable *outranks* `[tool.uv]`: left implicit, a CI lock would
    resolve against the ambient window while a developer running the same
    command locally resolves against this one, and `sync-uv-lock` would churn
    between the two. A CLI flag outranks the environment, which pins the
    project's own policy.
    ```

    :param pyproject_path: Path to the `pyproject.toml` file.
    :return: The configured window verbatim (a friendly duration, an ISO 8601
        span or an absolute timestamp), or an empty string when unset.
    """
    doc = tomlrt.loads(pyproject_path.read_text(encoding="UTF-8"))
    window = doc.get("tool", {}).get("uv", {}).get("exclude-newer", "")
    return str(window) if window else ""


def packages_outside_cooldown(
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
    exclude_newer_str = project_exclude_newer(pyproject_path)
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
        upload_dt = parse_iso_datetime(upload_str)
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
    """Freeze cutoff that holds a package within a day of its locked version.

    A cooldown bypass must *hold* the locked version, not track the latest
    release (which is what a `"0 day"` span does: it disables the cooldown,
    so `uv lock --upgrade` keeps pulling newer releases and the entry never
    ages out). Returns an `exclude-newer-package` cutoff a little past the
    locked version's upload time so uv keeps that version and rejects
    releases published after the window.

    The cutoff rounds up to a whole-day UTC boundary through
    {func}`date_to_utc_cutoff`: the second UTC midnight after the upload, so
    a version uploaded on `2026-07-01` freezes at `2026-07-03T00:00:00Z`.
    Rounding to a full UTC timestamp (rather than a bare `YYYY-MM-DD` date,
    which uv re-expands per locking-machine timezone) stops `uv.lock` from
    ping-ponging between local and CI locks. Rounding *up* by a day keeps the
    held version comfortably inside its own window: uv's cutoff is exclusive
    ("uploaded prior to") and PyPI upload times carry sub-second precision,
    so a cutoff pinned right at the upload instant risks landing before it and
    excluding the very version meant to be held.

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

    :param upload_str: The locked version's `upload-time` from `uv.lock`.
    :return: A `YYYY-MM-DDT00:00:00Z` cutoff timestamp, or `None` when
        *upload_str* is empty or unparsable (git and path sources have no
        upload time).
    """
    upload_dt = parse_iso_datetime(upload_str)
    if upload_dt is None:
        return None
    return freeze_cutoff_after(upload_dt.date())


def freeze_cutoff_after(day: date) -> str:
    """The `exclude-newer-package` cutoff holding a version uploaded on *day*.

    One day of margin, rounded to a whole-day UTC boundary: see
    {func}`_freeze_cutoff` for the full margin and timezone rationale. The
    single source of that policy, shared with
    {class}`repomatic.dep_sources.ReleaseSwap`.

    :param day: The held version's upload date.
    :return: A `YYYY-MM-DDT00:00:00Z` cutoff timestamp.
    """
    return date_to_utc_cutoff(day + timedelta(days=1))


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
    version (a whole-day boundary just past that version's upload) so that
    subsequent `uv lock --upgrade` runs (the `sync-uv-lock` job) hold the
    package within that freeze window instead of tracking the latest release,
    until it ages past the `exclude-newer` cooldown and
    {func}`prune_stale_exclude_newer_packages` drops the entry. See
    `_freeze_cutoff` for the window's width and its same-day-patch
    caveat. Packages with no upload time in the lock (git or path sources)
    fall back to a permanent `"0 day"` span.

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
    package: releases past the freeze window are excluded until the held
    version ages past the global cooldown, at which point the entry is pruned
    and the package rejoins normal resolution.

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
            parse_friendly_duration(text) is not None
            or parse_iso8601_duration(text) is not None
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
        upload_dt = parse_iso_datetime(upload_str)
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
        duration = parse_iso8601_duration(span)
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
    return parse_iso8601_duration(span) if span else None


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
            upload_dt = parse_iso_datetime(raw_upload)
            if upload_dt is not None:
                eligible = format_eligible((upload_dt + span).date(), now.date())
        held.append(HeldBackPackage(name, old_version, new_version, released, eligible))
    return held


def _forecast_expiry(upload_str: str, span: timedelta | None, today: date) -> str:
    """Format the date a held version ages past the rolling cooldown cutoff.

    :param upload_str: The held version's `upload-time` from `uv.lock`.
    :param span: The rolling `exclude-newer` span, or `None` when the cutoff
        is absolute.
    :param today: Reference date for the relative hint.
    :return: The humanized expiry, {data}`repomatic.dep_report.BYPASS_NEEDS_RELEASE`
        when the version has no upload time, or empty when *span* is absent.
    """
    upload_dt = parse_iso_datetime(upload_str)
    if upload_dt is None:
        return BYPASS_NEEDS_RELEASE
    if span is None:
        return ""
    return format_eligible((upload_dt + span).date(), today)


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
            parse_friendly_duration(value) is not None
            or parse_iso8601_duration(value) is not None
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


# ---------------------------------------------------------------------------
# High-level operations
# ---------------------------------------------------------------------------


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

    # Step 3: Run uv lock --upgrade in the project directory. The project's own
    # exclude-newer window is passed explicitly so CI's ambient UV_EXCLUDE_NEWER
    # cannot retime the lock; see project_exclude_newer.
    project_dir = lock_path.parent
    lock_cmd = [*uv_cmd("lock"), "--upgrade"]
    if pyproject_path.exists():
        window = project_exclude_newer(pyproject_path)
        if window:
            lock_cmd += ["--exclude-newer", window]
    logging.info(f"Running {' '.join(lock_cmd)} in {project_dir}...")
    subprocess.run(lock_cmd, check=True, cwd=project_dir)

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
