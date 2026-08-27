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
{mod}`repomatic.changelog` (release dates, yanked status),
{mod}`repomatic.version_sync` (release candidates) and
{mod}`repomatic.dep_report` (source repository discovery for release notes).
Also the home of what counts as the public index at all
({data}`PYPI_INDEX_HOSTS`), which the shippability gate reads.
"""

from __future__ import annotations

from typing import NamedTuple
from urllib.parse import urlencode, urlsplit

from .config import load_repomatic_config
from .http import get_cached_json, get_json_soft
from .versions import safe_version

TYPE_CHECKING = False
if TYPE_CHECKING:
    from typing import Any

    from packaging.version import Version

PYPI_INDEX_HOSTS = frozenset({"pypi.org", "www.pypi.org"})
"""Hosts a package may be resolved from and still count as published.

Anything else is a private index, a staging index (TestPyPI lives on
`test.pypi.org`, deliberately absent) or a proxy: a user running
`pip install` or `uvx` against the default index reaches none of them, so a
dependency pinned there is no more installable than one pinned to a git
branch.
"""


def is_pypi_url(url: str) -> bool:
    """Whether *url* points at the public Python Package Index.

    :param url: An index or registry URL.
    :return: `True` when its host is one of {data}`PYPI_INDEX_HOSTS`.
    """
    return urlsplit(url).hostname in PYPI_INDEX_HOSTS


PYPI_API_URL = "https://pypi.org/pypi/{package}/json"
"""PyPI JSON API URL for fetching all release metadata for a package."""

PYPI_PACKAGE_URL = "https://pypi.org/project/{package}/"
"""PyPI project homepage URL for a package (no version pinned)."""

PYPI_PROJECT_URL = "https://pypi.org/project/{package}/{version}/"
"""PyPI project page URL for a specific version."""

PYPI_PROVENANCE_URL = (
    "https://pypi.org/integrity/{package}/{version}/{filename}/provenance"
)
"""PyPI integrity API endpoint exposing PEP 740 attestation bundles for a file.

The response includes a `publisher` object per bundle that names the OIDC
identity used to upload (kind, repository, workflow filename, environment).
This is the only public surface where the OIDC `job_workflow_ref` claim is
observable: project-level Trusted Publisher settings live behind the owner-only
`/manage/project/<name>/settings/publishing/` page.
"""

PYPI_TRUSTED_PUBLISHER_SETTINGS_URL = (
    "https://pypi.org/manage/project/{package}/settings/publishing/"
)
"""Owner-only page where Trusted Publisher entries are registered."""

PYPI_TRUSTED_PUBLISHER_WORKFLOW = "release.yaml"
"""Workflow filename each downstream registers as the Trusted Publisher.

The caller-side `publish-pypi` job is appended to `release.yaml` in every
downstream repo (reshaped from the canonical entry by
`repomatic.github.workflow_sync._render_publish_pypi_job`), and the composite
action it invokes inherits the calling job's OIDC context. The OIDC
`job_workflow_ref` claim therefore names this file: that is what the PyPI
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
    `manage_project_oidc_publishers_prefill` view in
    [pypi/warehouse](https://github.com/pypi/warehouse/blob/main/warehouse/manage/views/oidc_publishers.py).

    :param package: PyPI project name.
    :param owner: GitHub owner (user or org) prefilled in the form.
    :param repository: GitHub repository name prefilled in the form.
    :param workflow_filename: Workflow filename prefilled in the form (e.g.,
        {data}`PYPI_TRUSTED_PUBLISHER_WORKFLOW`).
    :param environment: GitHub Actions environment name prefilled in the form.
    :return: The settings URL, optionally with a `?provider=github&…` suffix.
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

# Keys in PyPI `project_urls` that typically point to a changelog, checked in
# priority order. Lowercase, because they are looked up in a lowercased index
# of the project's own keys: see the note on `_SOURCE_URL_KEYS`.
_CHANGELOG_URL_KEYS = (
    "changelog",
    "changes",
    "change log",
    "release notes",
    "history",
)

# Keys in PyPI `project_urls` that typically point to a GitHub repository,
# checked in priority order. Matched case-insensitively: PyPI preserves whatever
# spelling the project wrote, so `Homepage`, `homepage` and `HomePage` all occur
# in the wild and a case-sensitive miss silently falls through to the scan below,
# which would happily return a Bug Tracker URL.
_SOURCE_URL_KEYS = (
    "source",
    "source code",
    "repository",
    "code",
    "homepage",
)


def _fetch_json(package: str, *, force_refresh: bool = False) -> dict[str, Any] | None:
    """Fetch the full JSON metadata for a PyPI package.

    Results are cached under the `pypi` namespace. Freshness TTL is read
    from `CacheConfig.pypi_ttl`.

    ```{caution}
    Within that TTL a cached snapshot taken before a release was published
    reports that release as missing, which is indistinguishable from "never
    published" at this layer. A caller about to act on an *absence* around
    release time wants *force_refresh*: see
    {func}`repomatic.changelog.lint_changelog_dates`, which re-confirms live
    before retracting an availability claim.
    ```

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
    :param force_refresh: Ignore any cached response and re-fetch.
    :return: Parsed JSON response, or `None` on any failure.
    """
    if not force_refresh and package in _FETCH_MEMO:
        return _FETCH_MEMO[package]
    result = get_cached_json(
        "pypi",
        package,
        PYPI_API_URL.format(package=package),
        ttl=load_repomatic_config().cache.pypi_ttl,
        log_label=f"PyPI lookup failed for {package}",
        force_refresh=force_refresh,
    )
    _FETCH_MEMO[package] = result
    return result


_FETCH_MEMO: dict[str, dict[str, Any] | None] = {}
"""In-process snapshots of {func}`_fetch_json` answers, keyed by package.

The disk cache spares the network, not the decode: a mature package's JSON
payload runs to hundreds of kilobytes, and one report renders two or three
accessors per package off the same document. Holding the parsed result for
the process keeps that to one decode. A *force_refresh* bypasses the memo and
replaces the snapshot, so the live re-confirm path stays live; a `None`
failure is memoized too, deliberately, since retrying a dead lookup once per
accessor only multiplies the timeout.
"""


def _project_urls(package: str) -> dict[str, str]:
    """Fetch a package's `project_urls`, or an empty mapping.

    Shared by the `project_urls` scanners ({func}`get_source_url` and
    {func}`get_changelog_url`), which each look the same keys up in a
    lowercased index of whatever spelling the project wrote: see the note on
    {data}`_SOURCE_URL_KEYS`.

    An unreachable or unknown package yields `{}` rather than `None`, so a
    caller scanning for a key finds nothing and reports it the same way it
    reports a package that simply declares no matching URL.

    :param package: The PyPI package name.
    :return: The declared `project_urls` mapping, keys untouched.
    """
    data = _fetch_json(package)
    if data is None:
        return {}
    urls: dict[str, str] = data.get("info", {}).get("project_urls") or {}
    return urls


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

    yanked_reason: str = ""
    """Why the release was yanked, empty when PyPI records no reason.

    PyPI stores the reason per file and accepts a yank with none at all, so
    this carries the first non-empty one across the version's files.
    """


def get_release_dates(
    package: str, *, force_refresh: bool = False
) -> dict[str, PyPIRelease]:
    """Get upload dates and yanked status for all versions from PyPI.

    Fetches the package metadata in a single API call. For each version,
    selects the **earliest** upload time across all distribution files as
    the canonical release date. A version is considered yanked only if
    **all** of its files are yanked, and carries the first yank reason any
    of them records.

    :param package: The PyPI package name.
    :param force_refresh: Ignore any cached response and re-fetch.
    :return: Dict mapping version strings to {class}`PyPIRelease` tuples.
        Empty dict if the package is not found or the request fails.
    """
    data = _fetch_json(package, force_refresh=force_refresh)
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
        # The reason is per-file and optional: keep the first non-empty one.
        reason = next(
            (str(f["yanked_reason"]) for f in files if f.get("yanked_reason")),
            "",
        )
        result[version] = PyPIRelease(
            date=earliest_date,
            yanked=all_yanked,
            package=package,
            yanked_reason=reason,
        )

    return result


def github_repo_root(url: str) -> str | None:
    """Reduce any GitHub URL to its `https://github.com/owner/repo` root.

    A `project_urls` entry often points *inside* a repository (`/issues`,
    `/releases`, `/blob/main/CHANGELOG.md`), which is fine for a human-facing
    link but not for callers that derive an `owner/repo` API slug from it: the
    releases API would be asked for `repo/issues` and answer 404.

    :param url: Any URL, GitHub or not.
    :return: The repository root, or `None` when *url* names no GitHub
        repository (a bare `github.com`, or an owner with no repo).
    """
    cleaned = url.strip().rstrip("/").removesuffix(".git")
    _, separator, tail = cleaned.partition("github.com/")
    if not separator:
        return None
    parts = [segment for segment in tail.split("/") if segment]
    if len(parts) < 2:
        return None
    return f"https://github.com/{parts[0]}/{parts[1]}"


def get_source_url(package: str) -> str | None:
    """Discover the GitHub repository URL for a PyPI package.

    Queries the PyPI JSON API and scans `project_urls` for keys that typically
    point to a source repository on GitHub, then reduces the winner to its
    repository root so an API slug can be derived from it.

    :param package: The PyPI package name.
    :return: The GitHub repository URL, or `None` if not found.
    """
    project_urls = _project_urls(package)
    by_key = {key.lower(): value for key, value in project_urls.items()}
    for key in _SOURCE_URL_KEYS:
        root = github_repo_root(by_key.get(key, ""))
        if root:
            return root
    # Fallback: scan all values for a GitHub URL.
    for candidate in project_urls.values():
        root = github_repo_root(candidate)
        if root:
            return root
    return None


class TrustedPublisher(NamedTuple):
    """OIDC publisher metadata extracted from a PyPI provenance bundle."""

    kind: str
    """Publisher kind, e.g., `"GitHub"` or `"GitLab"`."""

    repository: str
    """Repository slug (`"owner/name"` for GitHub publishers)."""

    workflow: str
    """Workflow filename within `.github/workflows/` (e.g., `"release.yaml"`)."""

    environment: str | None
    """GitHub Actions environment name, when the publisher was scoped to one."""


def get_latest_release_file(package: str) -> tuple[str, str] | None:
    """Return `(version, filename)` for the latest non-yanked release on PyPI.

    Picks the version with the most recent earliest-upload time and returns
    a representative distribution file from that version. Wheels are
    preferred over sdists since wheels are guaranteed to exist for any
    package built with modern tooling.

    Two releases uploaded on the same day are ordered by PEP 440, not by the
    version string: a raw string comparison sorts `1.9.0` above `1.10.0` and
    would return the older of the two as the latest. Versions PEP 440 cannot
    parse are skipped, since nothing can rank them.

    :param package: The PyPI package name.
    :return: Tuple of `(version, filename)`, or `None` if the package
        has no published releases or the request fails.
    """
    data = _fetch_json(package)
    if data is None:
        return None

    releases: dict[str, list[dict]] = data.get("releases") or {}
    candidates: list[tuple[str, Version, str, str]] = []
    for version, files in releases.items():
        live_files = [f for f in files if not f.get("yanked", False)]
        if not live_files:
            continue
        upload_dates = [f["upload_time"] for f in live_files if f.get("upload_time")]
        if not upload_dates:
            continue
        parsed = safe_version(version)
        if parsed is None:
            continue
        wheels = [f for f in live_files if f.get("filename", "").endswith(".whl")]
        chosen = wheels[0] if wheels else live_files[0]
        candidates.append((min(upload_dates), parsed, version, chosen["filename"]))

    if not candidates:
        return None
    _date, _parsed, version, filename = max(candidates)
    return version, filename


def get_trusted_publishers(
    package: str, version: str, filename: str
) -> list[TrustedPublisher] | None:
    """Fetch PEP 740 provenance for a file and extract publisher entries.

    Calls {data}`PYPI_PROVENANCE_URL` and parses the `attestation_bundles`
    array. Each bundle's `publisher` object names the OIDC identity that
    uploaded the file.

    :param package: The PyPI package name.
    :param version: The release version (e.g., `"1.2.3"`).
    :param filename: The distribution filename (e.g.,
        `"my_pkg-1.2.3-py3-none-any.whl"`).
    :return: List of {class}`TrustedPublisher` entries (possibly empty when
        provenance exists but no bundles are present), or `None` when the
        endpoint returns 404 or any network/parse error occurs (signal that
        no provenance is available rather than that none was registered).
    """
    url = PYPI_PROVENANCE_URL.format(
        package=package, version=version, filename=filename
    )
    fetched = get_json_soft(
        url, f"PyPI provenance lookup failed for {package} {version}"
    )
    if fetched is None:
        return None
    data, _raw = fetched

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
    typically point to a changelog or release notes page. Keys are matched
    case-insensitively, for the reason spelled out on
    {data}`_SOURCE_URL_KEYS`: PyPI preserves whatever spelling the project
    wrote, so `Changelog`, `changelog` and `CHANGELOG` all occur in the wild.

    :param package: The PyPI package name.
    :return: The changelog URL, or `None` if not found.
    """
    by_key = {key.lower(): value for key, value in _project_urls(package).items()}
    for key in _CHANGELOG_URL_KEYS:
        candidate = by_key.get(key, "")
        if candidate:
            return candidate.rstrip("/")
    return None
