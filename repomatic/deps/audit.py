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

"""Vulnerability audit and remediation for locked dependencies.

Backs the `audit` command and the `fix-vulnerable-deps` job: queries the
advisory sources enabled in `[tool.repomatic] vulnerable-deps.sources`,
unions and deduplicates their findings into {class}`VulnerablePackage`
records, and (`--fix`) upgrades each fixable package through uv.

Two advisory sources are consulted:

- `uv audit` queries the [PyPA Advisory
  Database](https://github.com/pypa/advisory-database) (OSV-backed).
- GitHub's Dependabot alerts query the [GitHub Advisory
  Database](https://github.com/advisories) (GHSA).

Coverage diverges in practice: GHSA frequently lists a CVE before the PyPA
database mirrors it, and transitive lockfile vulnerabilities sometimes only
surface in GHSA. By unioning both sources, `audit` catches CVEs that either
database alone would miss.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from click_extra import ColumnSpec
from packaging.utils import canonicalize_name

from ..github.gh import gh_json
from ..pypi import PYPI_PACKAGE_URL
from .cooldown import LIFT_SPAN, UvSettings
from .lockfile import LockFile, diff_lock_versions
from .report import (
    build_comparison_urls,
    fetch_release_notes,
    format_diff_table,
    format_exclude_newer_note,
    format_release_notes,
    markdown_section,
    pypi_name_urls,
)
from .uv import run_uv, uv_cmd

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Iterable
    from datetime import date
    from pathlib import Path

AUDIT_HEADER_DEFS: tuple[ColumnSpec, ...] = (
    ColumnSpec("package", "Package"),
    ColumnSpec("version", "Version"),
    ColumnSpec("advisory", "Advisory"),
    ColumnSpec("fixed", "Fixed"),
    ColumnSpec("sources", "Sources"),
)
"""Column definitions for the `repomatic audit` table.

Lives beside {class}`VulnerablePackage`, whose {attr}`~VulnerablePackage.row`
fills the columns, so the two cannot drift apart. The CLI derives its
`--sort-by` choices from it.
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
    coverage diverges in practice (GHSA frequently lists a CVE before the PyPA
    Advisory Database mirrors it, for one). Tracking the source per
    {class}`VulnerablePackage` lets the union deduplicate by advisory ID
    while still attributing each entry to the database that produced it.
    """

    UV_AUDIT = "uv-audit"
    """Detected by `uv audit` (PyPA Advisory Database, OSV-backed)."""

    GITHUB_ADVISORIES = "github-advisories"
    """Detected via the repository's Dependabot alerts (GitHub Advisory Database)."""

    @classmethod
    def from_config(cls, values: Iterable[str]) -> list[AdvisorySource]:
        """Read `[tool.repomatic] vulnerable-deps.sources`.

        An unknown name is logged and skipped. The list stays empty when the
        configuration names no known source: an empty list queries nothing,
        where `None` would mean every source.

        :param values: The configured source names.
        :return: The known sources, in configuration order.
        """
        sources = []
        for value in values:
            try:
                sources.append(cls(value))
            except ValueError:
                logging.warning(
                    f"Unknown vulnerable-deps source {value!r};"
                    f" expected one of {[source.value for source in cls]}."
                )
        return sources


@dataclass
class VulnerablePackage:
    """A single vulnerability advisory for a Python package."""

    name: str
    """Package name."""

    current_version: str
    """Currently resolved version."""

    advisory_id: str
    """Advisory identifier (like `GHSA-xxxx-xxxx-xxxx`)."""

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
    reported by multiple sources after deduplication. Empty only for
    entries built without source attribution (test fixtures); every
    production code path records at least one source.
    """

    source_urls: dict[AdvisorySource, str] = field(default_factory=dict)
    """Per-source URL pointing to the advisory page in each database.

    Each source has its own canonical URL even when reporting the same
    advisory ID (PyPA's `osv.dev` page against GitHub's `/advisories/` page),
    so the rendered table can link the source name to the database that
    actually surfaced it.
    """

    @property
    def row(self) -> tuple[str, str, str, str, str]:
        """The advisory as a row of {data}`AUDIT_HEADER_DEFS`."""
        return (
            self.name,
            self.current_version,
            self.advisory_id,
            self.fixed_version or "unknown",
            ", ".join(sorted(source.value for source in self.sources)) or "—",
        )


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
    rows: list[tuple[str, ...]] = []
    for v in vulns:
        pkg_link = f"[{v.name}]({PYPI_PACKAGE_URL.format(package=v.name)})"
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
        rows.append((
            pkg_link,
            f"{adv_link}: {v.advisory_title}",
            f"`{v.current_version}`",
            fixed,
            sources,
        ))
    return markdown_section(
        "Vulnerabilities",
        "",
        ("Package", "Advisory", "Current", "Fixed", "Sources"),
        rows,
    )


def _run_uv_audit(lock_path: Path) -> list[VulnerablePackage]:
    """Run `uv audit --output-format json` and parse the result.

    The `[tool.uv] required-version` floor and the uv the workflows pin both
    sit above the release that introduced the JSON output, and a uv too old
    to know the flag dies before printing anything, which the empty-output
    guard below reports.

    :param lock_path: Path to the `uv.lock` file (used to derive the project
        directory).
    :return: A list of {class}`VulnerablePackage` entries detected by
        `uv audit`. Empty when no vulnerabilities are found.
    :raises RuntimeError: when `uv` exits without emitting JSON (its stderr is
        surfaced as the cause), or when its JSON output is unparsable.
    """
    result = run_uv(
        [
            *uv_cmd("audit", frozen=True),
            "--output-format",
            "json",
            "--preview-features",
            "json-output",
        ],
        cwd=lock_path.parent,
        check=False,
    )
    # An empty stdout means uv died before emitting JSON (a `required-version`
    # mismatch, an unknown flag), not that the audit found nothing: its stderr
    # carries the actual cause, which the JSON parser cannot see. The exit
    # code cannot discriminate here since `uv audit` also exits non-zero when
    # it does find vulnerabilities.
    if not result.stdout.strip():
        stderr = result.stderr.strip()
        raise RuntimeError(
            "`uv audit --output-format json` produced no output"
            + (f":\n{stderr}" if stderr else " and no stderr.")
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
        ghsa = fetch_dependabot_alerts(repo)
        # The alerts carry the vulnerable range, not the locked version: read
        # it from the lock, in whatever spelling the alert names the package.
        if ghsa:
            lock = LockFile.load(lock_path)
            for v in ghsa:
                if not v.current_version:
                    v.current_version = lock.version_of(v.name)
            collected.extend(ghsa)

    # Deduplicate within each canonical package name, unioning sources. Two
    # advisories are the same when their identifier sets overlap: a shared
    # CVE/GHSA/PYSEC/OSV alias links the same advisory reported under
    # different primary IDs by different sources (a PYSEC from `uv audit`
    # and the equivalent GHSA from Dependabot, say).
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


@dataclass
class AuditFix:
    """What `audit --fix` found, and what it upgraded."""

    vulnerabilities: list[VulnerablePackage] = field(default_factory=list)
    """Every advisory the sources reported."""

    changes: list[tuple[str, str, str]] = field(default_factory=list)
    """The `(name, old, new)` version moves the upgrade made."""

    lock: LockFile = field(default_factory=LockFile)
    """The upgraded lock."""

    release_notes: dict[str, tuple[str, list[tuple[str, str]]]] = field(
        default_factory=dict
    )
    """Release notes of the upgraded packages, as
    {func}`repomatic.deps.report.fetch_release_notes` returns them."""

    @property
    def has_fixes(self) -> bool:
        """Whether the upgrade moved at least one package."""
        return bool(self.changes)

    def render(self, reference_date: date) -> str:
        """Render the advisories, the version moves and the release notes.

        :param reference_date: The day the "Released" hints count back from.
        :return: The markdown report, sections separated by a blank line.
        """
        sections = (
            format_vulnerability_table(self.vulnerabilities),
            format_diff_table(
                self.changes,
                self.lock.upload_times,
                format_exclude_newer_note(self.lock.exclude_newer),
                comparison_urls=build_comparison_urls(self.changes, self.release_notes),
                reference_date=reference_date,
                name_urls=pypi_name_urls(name for name, _old, _new in self.changes),
            ),
            format_release_notes(self.release_notes),
        )
        return "\n\n".join(section for section in sections if section)


def fix_vulnerable_deps(
    lock_path: Path,
    repo: str | None = None,
    sources: list[AdvisorySource] | None = None,
) -> AuditFix:
    """Detect vulnerable packages and upgrade them in the lock file.

    Queries every advisory source enabled by *sources* (defaults to all),
    then upgrades each fixable package with `uv lock --upgrade-package`
    using `--exclude-newer-package` to bypass the `exclude-newer` cooldown
    for security fixes. Also persists the exemptions in `pyproject.toml`
    so that subsequent `uv lock --upgrade` runs (from the `sync-uv-lock` job,
    say) do not downgrade the fixed packages back within the cooldown window.

    An upgrade that resolves to the versions already locked leaves the file
    byte-identical to how it was found, because uv writes the overrides it was
    handed into the lock's `[options]` table even when they change nothing.
    See the restore below.

    :param lock_path: Path to the `uv.lock` file.
    :param repo: Repository in `owner/repo` format. Required when
        {attr}`AdvisorySource.GITHUB_ADVISORIES` is among *sources*.
    :param sources: Advisory databases to consult. Defaults to all known
        sources.
    :return: The advisories, and the upgrade they led to. No change when
        nothing was fixable.
    """
    vulns = collect_vulnerable_packages(lock_path, repo=repo, sources=sources)
    if not vulns:
        logging.info("No vulnerabilities found.")
        return AuditFix()

    # Several advisories can target one package.
    fixable = sorted({v.name for v in vulns if v.fixed_version})
    if not fixable:
        logging.warning(
            f"Found {len(vulns)} vulnerabilities but none have a known fix version."
        )
        return AuditFix(vulnerabilities=vulns)
    logging.info(
        f"Found {len(vulns)} vulnerabilities across {len(fixable)} fixable"
        f" packages: {', '.join(fixable)}."
    )

    # The raw bytes ride along with the version map, so a resolution that
    # moves nothing can be rolled back verbatim.
    before = LockFile.load(lock_path)
    lock_before = lock_path.read_bytes()
    settings = UvSettings.load(lock_path.parent / "pyproject.toml")

    # One resolution for every package: sequential ones would undo each other.
    # The project's own window rides along explicitly (see UvSettings.window),
    # so only the named packages bypass the cooldown.
    upgrades: list[str] = []
    for name in fixable:
        upgrades += ["--upgrade-package", name]
    logging.info(f"Upgrading: {', '.join(fixable)}...")
    run_uv(
        settings.lock_command(
            *upgrades, exclude_newer_packages=dict.fromkeys(fixable, LIFT_SPAN)
        ),
        cwd=lock_path.parent,
    )

    after = LockFile.load(lock_path)
    changes = diff_lock_versions(before.versions, after.versions)
    if not changes:
        logging.info("No version changes after upgrading vulnerable packages.")
        # uv records the `--exclude-newer-package` overrides it was handed in
        # the lock's own `[options]` table, whether or not they moved the
        # resolution. A fix that cannot land (another dependency capping the
        # vulnerable package below its patched release) therefore still leaves
        # that one metadata line behind: enough for `fix-vulnerable-deps` to
        # open a pull request carrying no fix and an empty report, and which
        # the next `sync-uv-lock` re-lock strips again. Roll the file back so a
        # failed fix leaves no trace. Discarding any other rewrite uv made in
        # passing is intentional: normalizing the lock belongs to
        # `sync-uv-lock`, which runs in the same workflow.
        lock_path.write_bytes(lock_before)
        return AuditFix(vulnerabilities=vulns)

    # Persist an exemption only for a fixed release still inside the window: a
    # package a normal `uv lock --upgrade` reaches needs no override.
    upgraded = (name for name, _old, _new in changes)
    if settings.exempt(upgraded, after, datetime.now(UTC)):
        settings.save()

    return AuditFix(
        vulnerabilities=vulns,
        changes=changes,
        lock=after,
        release_notes=fetch_release_notes(changes),
    )


def fetch_dependabot_alerts(repo: str) -> list[VulnerablePackage]:
    """Fetch open `pip`-ecosystem Dependabot alerts for a repository.

    Calls `GET /repos/{repo}/dependabot/alerts?state=open&ecosystem=pip`
    via the `gh` CLI, then maps each alert into a
    {class}`VulnerablePackage` tagged with
    {attr}`AdvisorySource.GITHUB_ADVISORIES`.

    Returns an empty list when the API is unreachable, the token lacks the
    `Dependabot alerts` permission, or the repository has no open alerts.
    A network or auth failure must not break the autofix workflow: the
    `uv audit` source is still consulted independently.

    :param repo: Repository in `owner/repo` format.
    :return: List of {class}`VulnerablePackage` entries with
        a known fixed version. Alerts without `first_patched_version` are
        skipped (no upgrade target).
    """
    try:
        alerts = gh_json([
            "api",
            "--paginate",
            f"repos/{repo}/dependabot/alerts?state=open&ecosystem=pip&per_page=100",
        ])
    except RuntimeError as exc:
        logging.warning(
            f"Could not fetch Dependabot alerts for {repo}: {exc}."
            " Continuing with `uv audit` results only."
        )
        return []

    vulns: list[VulnerablePackage] = []
    for alert in alerts or []:
        vuln = alert.get("security_vulnerability") or {}
        package = vuln.get("package") or {}
        advisory = alert.get("security_advisory") or {}
        name = package.get("name", "")
        first_patched = (vuln.get("first_patched_version") or {}).get("identifier", "")
        if not name or not first_patched:
            continue
        ghsa_id = advisory.get("ghsa_id", "")
        # Cross-referenced identifiers (CVE, GHSA) let the same advisory
        # deduplicate against `uv audit`, which keys records by OSV/PYSEC IDs.
        aliases = {advisory.get("cve_id") or ""}
        aliases.update(
            identifier.get("value") or ""
            for identifier in advisory.get("identifiers") or []
        )
        aliases.discard("")
        aliases.discard(ghsa_id)
        url = advisory.get("html_url") or (
            f"https://github.com/advisories/{ghsa_id}" if ghsa_id else ""
        )
        vulns.append(
            VulnerablePackage(
                name=name,
                # The alert carries the vulnerable range, not the locked
                # version: collect_vulnerable_packages reads that from the lock.
                current_version="",
                advisory_id=ghsa_id,
                advisory_title=advisory.get("summary", ""),
                fixed_version=first_patched,
                advisory_url=url,
                aliases=aliases,
                sources={AdvisorySource.GITHUB_ADVISORIES},
                source_urls=({AdvisorySource.GITHUB_ADVISORIES: url} if url else {}),
            )
        )
    logging.info(f"Fetched {len(vulns)} fixable Dependabot alert(s) for {repo}.")
    return vulns
