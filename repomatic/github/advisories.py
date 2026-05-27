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

"""GitHub Advisory Database (GHSA) client for Dependabot alerts.

Reads the open Dependabot alerts on a repository and converts them into the
{class}`~repomatic.uv.VulnerablePackage` shape consumed by
{func}`~repomatic.uv.fix_vulnerable_deps`. Used as a second advisory source
alongside `uv audit`.

```{note} Why GHSA in addition to `uv audit`

`uv audit` queries the [PyPA Advisory
Database](https://github.com/pypa/advisory-database) (OSV-backed). GitHub's
Dependabot alerts query the [GitHub Advisory
Database](https://github.com/advisories) (GHSA). Coverage diverges in
practice: GHSA frequently lists a CVE before the PyPA database mirrors it,
and transitive lockfile vulnerabilities sometimes only surface in GHSA. By
unioning both sources, `fix-vulnerable-deps` catches CVEs that either
database alone would miss.
```
"""

from __future__ import annotations

import json
import logging

from .gh import run_gh_command

TYPE_CHECKING = False
if TYPE_CHECKING:
    from ..uv import VulnerablePackage


def fetch_dependabot_alerts(repo: str) -> list[VulnerablePackage]:
    """Fetch open `pip`-ecosystem Dependabot alerts for a repository.

    Calls ``GET /repos/{repo}/dependabot/alerts?state=open&ecosystem=pip``
    via the `gh` CLI, then maps each alert into a
    {class}`~repomatic.uv.VulnerablePackage` tagged with
    {attr}`~repomatic.uv.AdvisorySource.GITHUB_ADVISORIES`.

    Returns an empty list when the API is unreachable, the token lacks the
    `Dependabot alerts` permission, or the repository has no open alerts.
    A network or auth failure must not break the autofix workflow: the
    `uv audit` source is still consulted independently.

    :param repo: Repository in `owner/repo` format.
    :return: List of {class}`~repomatic.uv.VulnerablePackage` entries with
        a known fixed version. Alerts without `first_patched_version` are
        skipped (no upgrade target).
    """
    from ..uv import AdvisorySource, VulnerablePackage

    try:
        raw = run_gh_command([
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

    try:
        alerts = json.loads(raw) if raw.strip() else []
    except json.JSONDecodeError as exc:
        logging.warning(f"Could not parse Dependabot alerts response: {exc}.")
        return []

    vulns: list[VulnerablePackage] = []
    for alert in alerts:
        vuln = alert.get("security_vulnerability") or {}
        package = vuln.get("package") or {}
        advisory = alert.get("security_advisory") or {}
        dependency = alert.get("dependency") or {}
        name = package.get("name", "")
        first_patched = (vuln.get("first_patched_version") or {}).get("identifier", "")
        if not name or not first_patched:
            continue
        # Prefer the version uv resolved against (when known) over the alert
        # metadata, which only carries the vulnerable range, not the actual
        # locked version.
        current_version = ""
        manifest_path = dependency.get("manifest_path", "")
        if manifest_path.endswith("uv.lock"):
            current_version = ""  # filled in by the caller from parse_lock_versions
        ghsa_id = advisory.get("ghsa_id", "")
        summary = advisory.get("summary", "")
        # Cross-referenced identifiers (CVE, GHSA) let the same advisory
        # deduplicate against `uv audit`, which keys records by OSV/PYSEC IDs.
        aliases = {advisory.get("cve_id") or ""}
        for identifier in advisory.get("identifiers") or []:
            aliases.add(identifier.get("value") or "")
        aliases.discard("")
        aliases.discard(ghsa_id)
        url = advisory.get("html_url") or (
            f"https://github.com/advisories/{ghsa_id}" if ghsa_id else ""
        )
        vulns.append(
            VulnerablePackage(
                name=name,
                current_version=current_version,
                advisory_id=ghsa_id,
                advisory_title=summary,
                fixed_version=first_patched,
                advisory_url=url,
                aliases=aliases,
                sources={AdvisorySource.GITHUB_ADVISORIES},
                source_urls=({AdvisorySource.GITHUB_ADVISORIES: url} if url else {}),
            )
        )
    logging.info(f"Fetched {len(vulns)} fixable Dependabot alert(s) for {repo}.")
    return vulns
