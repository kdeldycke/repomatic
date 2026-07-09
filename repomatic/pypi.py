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
"""PyPI API client for package metadata lookups.

Provides a shared HTTP client and domain-specific query functions used by
{mod}`repomatic.changelog` (release dates, yanked status) and
{mod}`repomatic.uv` (source repository discovery for release notes).
"""

from __future__ import annotations

import json
import logging
from http.client import IncompleteRead
from typing import NamedTuple
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .cache import get_cached_response, store_response
from .config import load_repomatic_config

PYPI_API_URL = "https://pypi.org/pypi/{package}/json"
"""PyPI JSON API URL for fetching all release metadata for a package."""

PYPI_PROJECT_URL = "https://pypi.org/project/{package}/{version}/"
"""PyPI project page URL for a specific version."""

PYPI_PROVENANCE_URL = (
    "https://pypi.org/integrity/{package}/{version}/{filename}/provenance"
)
"""PyPI integrity API endpoint exposing PEP 740 attestation bundles for a file.

The response includes a ``publisher`` object per bundle that names the OIDC
identity used to upload (kind, repository, workflow filename, environment).
This is the only public surface where the OIDC ``job_workflow_ref`` claim is
observable: project-level Trusted Publisher settings live behind the owner-only
``/manage/project/<name>/settings/publishing/`` page.
"""

PYPI_TRUSTED_PUBLISHER_SETTINGS_URL = (
    "https://pypi.org/manage/project/{package}/settings/publishing/"
)
"""Owner-only page where Trusted Publisher entries are registered."""

PYPI_TRUSTED_PUBLISHER_WORKFLOW = "release.yaml"
"""Workflow filename each downstream registers as the Trusted Publisher.

The caller-side ``publish-pypi`` job is appended to ``release.yaml`` in every
downstream repo (reshaped from the canonical entry by
``repomatic.github.workflow_sync._render_publish_pypi_job``), and the composite
action it invokes inherits the calling job's OIDC context. The OIDC
``job_workflow_ref`` claim therefore names this file: that is what the PyPI
Trusted Publisher entry must match.
"""


def pypi_trusted_publisher_settings_url(
    package: str,
    *,
    owner: str | None = None,
    repository: str | None = None,
    workflow_filename: str | None = None,
    environment: str | None = None,
) -> str:
    """Build the PyPI Trusted Publisher settings page URL for a project.

    Without keyword arguments, returns the bare settings URL. When any GitHub
    publisher field is provided, appends the query string PyPI's settings page
    consumes to activate the GitHub tab and pre-populate the form: see the
    ``manage_project_oidc_publishers_prefill`` view in
    [pypi/warehouse](https://github.com/pypi/warehouse/blob/main/warehouse/manage/views/oidc_publishers.py).

    :param package: PyPI project name.
    :param owner: GitHub owner (user or org) prefilled in the form.
    :param repository: GitHub repository name prefilled in the form.
    :param workflow_filename: Workflow filename prefilled in the form (e.g.,
        :data:`PYPI_TRUSTED_PUBLISHER_WORKFLOW`).
    :param environment: GitHub Actions environment name prefilled in the form.
    :return: The settings URL, optionally with a ``?provider=github&…`` suffix.
    """
    base = PYPI_TRUSTED_PUBLISHER_SETTINGS_URL.format(package=package)
    fields = {
        "owner": owner,
        "repository": repository,
        "workflow_filename": workflow_filename,
        "environment": environment,
    }
    prefill = {key: value for key, value in fields.items() if value}
    if not prefill:
        return base
    # `provider=github` selects the GitHub tab and routes the remaining
    # parameters to the GitHub publisher form.
    query = urlencode({"provider": "github", **prefill})
    return f"{base}?{query}"


PYPI_LABEL = "🐍 PyPI"
"""Display label for PyPI releases in admonitions."""

# Keys in PyPI `project_urls` that typically point to a changelog,
# checked in priority order.
_CHANGELOG_URL_KEYS = (
    "Changelog",
    "Changes",
    "Change Log",
    "Release Notes",
    "History",
)

# Keys in PyPI `project_urls` that typically point to a GitHub repository,
# checked in priority order.
_SOURCE_URL_KEYS = (
    "Source",
    "Source Code",
    "Source code",
    "Repository",
    "Code",
    "Homepage",
)


def _fetch_json(package: str) -> dict | None:
    """Fetch the full JSON metadata for a PyPI package.

    Results are cached under the `pypi` namespace. Freshness TTL is read
    from `CacheConfig.pypi_ttl`.

    ```{warning}
    Returns `None` for every failure mode: HTTP 4xx/5xx, network error,
    timeout, JSON parse error. Callers cannot tell "package not on PyPI"
    apart from "transient API failure" from this signal alone. Even if
    this helper preserved the HTTP status, a `404` from
    `/pypi/<name>/json` is not authoritative either — Warehouse 404s
    registered projects with no releases, and registered names can
    appear in the `simple` / `list_packages` indexes while still 404'ing
    on the JSON endpoint (see
    [pypi/warehouse#1388](https://github.com/pypi/warehouse/issues/1388)
    and
    [pypi/warehouse#9536](https://github.com/pypi/warehouse/issues/9536)).
    Callers that drive destructive operations on this result must add a
    sanity check at the call site — see
    {data}`repomatic.changelog.EMPTY_PYPI_SANITY_THRESHOLD` for an
    example.
    ```

    :param package: The PyPI package name.
    :return: Parsed JSON response, or `None` on any failure.
    """
    ttl = load_repomatic_config().cache.pypi_ttl
    cached = get_cached_response("pypi", package, ttl)
    if cached is not None:
        try:
            return json.loads(cached)  # type: ignore[no-any-return]
        except json.JSONDecodeError:
            pass

    url = PYPI_API_URL.format(package=package)
    request = Request(url, headers={"Accept": "application/json"})
    for retry in (True, False):
        try:
            with urlopen(request, timeout=10) as response:
                raw = response.read()
                result: dict[str, object] = json.loads(raw)
            break
        except (URLError, TimeoutError, json.JSONDecodeError, IncompleteRead) as exc:
            # A truncated body is transient (a flaky connection or an
            # interfering proxy), so it earns one retry; the other failure
            # modes fail straight away.
            if retry and isinstance(exc, IncompleteRead):
                continue
            logging.debug(f"PyPI lookup failed for {package}: {exc}")
            return None

    if ttl > 0:
        store_response("pypi", package, raw)
    return result


class PyPIRelease(NamedTuple):
    """Release metadata for a single version from PyPI."""

    date: str
    """Earliest upload date across all files in `YYYY-MM-DD` format."""

    yanked: bool
    """Whether all files for this version are yanked."""

    package: str
    """PyPI package name this release was fetched from.

    Needed for projects that were renamed: older versions live under a
    former package name and their PyPI URLs must point to that name, not
    the current one.
    """


def get_release_dates(package: str) -> dict[str, PyPIRelease]:
    """Get upload dates and yanked status for all versions from PyPI.

    Fetches the package metadata in a single API call. For each version,
    selects the **earliest** upload time across all distribution files as
    the canonical release date. A version is considered yanked only if
    **all** of its files are yanked.

    :param package: The PyPI package name.
    :return: Dict mapping version strings to {class}`PyPIRelease` tuples.
        Empty dict if the package is not found or the request fails.
    """
    data = _fetch_json(package)
    if data is None:
        return {}

    result: dict[str, PyPIRelease] = {}
    for version, files in data.get("releases", {}).items():
        if not files:
            continue
        # Select the earliest upload time across all distribution files.
        dates = [f["upload_time"][:10] for f in files if f.get("upload_time")]
        if not dates:
            continue
        earliest_date = min(dates)
        # A version is yanked only if every file is yanked.
        all_yanked = all(f.get("yanked", False) for f in files)
        result[version] = PyPIRelease(
            date=earliest_date, yanked=all_yanked, package=package
        )

    return result


def get_source_url(package: str) -> str | None:
    """Discover the GitHub repository URL for a PyPI package.

    Queries the PyPI JSON API and scans `project_urls` for keys that
    typically point to a source repository on GitHub.

    :param package: The PyPI package name.
    :return: The GitHub repository URL, or `None` if not found.
    """
    data = _fetch_json(package)
    if data is None:
        return None

    project_urls: dict[str, str] = data.get("info", {}).get("project_urls") or {}
    for key in _SOURCE_URL_KEYS:
        candidate = project_urls.get(key, "")
        if "github.com" in candidate:
            return candidate.rstrip("/").removesuffix(".git")
    # Fallback: scan all values for a GitHub URL.
    for candidate in project_urls.values():
        if "github.com" in candidate:
            return candidate.rstrip("/").removesuffix(".git")
    return None


class TrustedPublisher(NamedTuple):
    """OIDC publisher metadata extracted from a PyPI provenance bundle."""

    kind: str
    """Publisher kind, e.g., ``"GitHub"`` or ``"GitLab"``."""

    repository: str
    """Repository slug (``"owner/name"`` for GitHub publishers)."""

    workflow: str
    """Workflow filename within ``.github/workflows/`` (e.g., ``"release.yaml"``)."""

    environment: str | None
    """GitHub Actions environment name, when the publisher was scoped to one."""


def get_latest_release_file(package: str) -> tuple[str, str] | None:
    """Return ``(version, filename)`` for the latest non-yanked release on PyPI.

    Picks the version with the most recent earliest-upload time and returns
    a representative distribution file from that version. Wheels are
    preferred over sdists since wheels are guaranteed to exist for any
    package built with modern tooling.

    :param package: The PyPI package name.
    :return: Tuple of ``(version, filename)``, or ``None`` if the package
        has no published releases or the request fails.
    """
    data = _fetch_json(package)
    if data is None:
        return None

    releases: dict[str, list[dict]] = data.get("releases") or {}
    candidates: list[tuple[str, str, str]] = []
    for version, files in releases.items():
        live_files = [f for f in files if not f.get("yanked", False)]
        if not live_files:
            continue
        upload_dates = [f["upload_time"] for f in live_files if f.get("upload_time")]
        if not upload_dates:
            continue
        candidates.append((min(upload_dates), version, ""))
        wheels = [f for f in live_files if f.get("filename", "").endswith(".whl")]
        chosen = wheels[0] if wheels else live_files[0]
        candidates[-1] = (min(upload_dates), version, chosen["filename"])

    if not candidates:
        return None
    candidates.sort()
    _, version, filename = candidates[-1]
    return version, filename


def get_trusted_publishers(
    package: str, version: str, filename: str
) -> list[TrustedPublisher] | None:
    """Fetch PEP 740 provenance for a file and extract publisher entries.

    Calls :data:`PYPI_PROVENANCE_URL` and parses the ``attestation_bundles``
    array. Each bundle's ``publisher`` object names the OIDC identity that
    uploaded the file.

    :param package: The PyPI package name.
    :param version: The release version (e.g., ``"1.2.3"``).
    :param filename: The distribution filename (e.g.,
        ``"my_pkg-1.2.3-py3-none-any.whl"``).
    :return: List of :class:`TrustedPublisher` entries (possibly empty when
        provenance exists but no bundles are present), or ``None`` when the
        endpoint returns 404 or any network/parse error occurs (signal that
        no provenance is available rather than that none was registered).
    """
    url = PYPI_PROVENANCE_URL.format(
        package=package, version=version, filename=filename
    )
    request = Request(url, headers={"Accept": "application/json"})
    for retry in (True, False):
        try:
            with urlopen(request, timeout=10) as response:
                raw = response.read()
                data: dict[str, object] = json.loads(raw)
            break
        except (URLError, TimeoutError, json.JSONDecodeError, IncompleteRead) as exc:
            # A truncated body is transient (a flaky connection or an
            # interfering proxy), so it earns one retry; the other failure
            # modes fail straight away.
            if retry and isinstance(exc, IncompleteRead):
                continue
            logging.debug(
                f"PyPI provenance lookup failed for {package} {version}: {exc}"
            )
            return None

    raw_bundles = data.get("attestation_bundles")
    bundles = raw_bundles if isinstance(raw_bundles, list) else []
    publishers: list[TrustedPublisher] = []
    for bundle in bundles:
        if not isinstance(bundle, dict):
            continue
        publisher = bundle.get("publisher")
        if not isinstance(publisher, dict):
            continue
        kind = publisher.get("kind")
        repository = publisher.get("repository")
        workflow = publisher.get("workflow")
        if not (
            isinstance(kind, str)
            and isinstance(repository, str)
            and isinstance(workflow, str)
        ):
            continue
        environment = publisher.get("environment")
        if environment is not None and not isinstance(environment, str):
            environment = None
        publishers.append(
            TrustedPublisher(
                kind=kind,
                repository=repository,
                workflow=workflow,
                environment=environment,
            )
        )
    return publishers


def get_changelog_url(package: str) -> str | None:
    """Discover the changelog URL for a PyPI package.

    Queries the PyPI JSON API and scans `project_urls` for keys that
    typically point to a changelog or release notes page.

    :param package: The PyPI package name.
    :return: The changelog URL, or `None` if not found.
    """
    data = _fetch_json(package)
    if data is None:
        return None

    project_urls: dict[str, str] = data.get("info", {}).get("project_urls") or {}
    for key in _CHANGELOG_URL_KEYS:
        candidate = project_urls.get(key, "")
        if candidate:
            return candidate.rstrip("/")
    return None
