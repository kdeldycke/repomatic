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

"""GitHub Releases API client.

The single home for reading GitHub Releases: raw cached API access (tags,
versions, single bodies), tag-to-version extraction, tag-to-SHA resolution, and
the range-to-release-notes fetch shared by the dependency updaters. The
{mod}`repomatic.version_sync` adapters and {mod}`repomatic.uv` release-notes
helper build on top of these reads.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import NamedTuple
from urllib.error import URLError
from urllib.request import Request, urlopen

from packaging.version import InvalidVersion, Version

from ..cache import get_cached_response, store_response
from ..config import load_repomatic_config

GITHUB_API_RELEASES_URL = "https://api.github.com/repos/{owner}/{repo}/releases"
"""GitHub API URL for fetching all releases for a repository."""

GITHUB_API_TAG_REF_URL = (
    "https://api.github.com/repos/{owner}/{repo}/git/ref/tags/{tag}"
)
"""GitHub API URL for resolving a tag name to its git object."""

GITHUB_API_TAG_OBJECT_URL = (
    "https://api.github.com/repos/{owner}/{repo}/git/tags/{sha}"
)
"""GitHub API URL for dereferencing an annotated tag object to its commit."""

GITHUB_API_RELEASE_BY_TAG_URL = (
    "https://api.github.com/repos/{owner}/{repo}/releases/tags/{tag}"
)
"""GitHub API URL for fetching a single release by tag name."""


def _owner_repo(repo_url: str) -> tuple[str, str] | None:
    """Extract `(owner, repo)` from a GitHub repository URL.

    :param repo_url: Repository URL (e.g. `https://github.com/user/repo`).
    :return: An `(owner, repo)` pair, or `None` when the URL does not parse.
    """
    parts = repo_url.rstrip("/").removesuffix(".git").split("/")
    if len(parts) < 2:
        return None
    return parts[-2], parts[-1]


def _api_request(url: str) -> Request:
    """Build a GitHub API request, authenticated when a token is present.

    `GITHUB_TOKEN` or `GH_TOKEN` raises the rate limit from 60 to 1000
    requests/hour, which matters when iterating every tool and action in CI.
    """
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return Request(url, headers=headers)


def _fetch_release_pages(owner: str, repo: str) -> list[dict]:
    """Fetch every release for a repository, following pagination.

    :return: The concatenated list of raw release objects from the API.
    :raises GitHubReleasesUnavailable: When any page fetch fails or returns
        unparsable JSON, so callers never mistake an incomplete fetch for a
        repository with no releases.
    """
    releases: list[dict] = []
    page = 1
    while True:
        url = (
            GITHUB_API_RELEASES_URL.format(owner=owner, repo=repo)
            + f"?per_page=100&page={page}"
        )
        try:
            with urlopen(_api_request(url), timeout=10) as response:
                data = json.loads(response.read())
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            # A failed page fetch can corrupt the result in two ways: a total
            # failure on page 1 returns `{}` (indistinguishable from "repo has
            # no releases"), and a mid-pagination failure returns a silently
            # truncated dict. Both have produced changelog PRs that stripped
            # every GitHub link from the file. Raise so the caller cannot
            # mistake an incomplete fetch for a clean "nothing to see here."
            raise GitHubReleasesUnavailable(
                f"GitHub releases lookup failed for {owner}/{repo} "
                f"on page {page}: {exc}"
            ) from exc
        if not data:
            break
        releases.extend(data)
        page += 1
    return releases


def _release_date(release: dict) -> str:
    """Return a release's publication date in `YYYY-MM-DD` form.

    Prefers `published_at`, falling back to `created_at`. Empty when neither
    is present.
    """
    raw_date = release.get("published_at") or release.get("created_at", "")
    return raw_date[:10] if raw_date else ""


class GitHubReleasesUnavailable(RuntimeError):
    """Raised when the GitHub Releases API call could not complete cleanly.

    Signals a transient failure (network error, timeout, JSON parse error,
    or pagination breaking mid-stream) where the result cannot safely be
    treated as "no releases."

    Callers that drive destructive operations (rewriting the changelog,
    deleting tags, etc.) must catch this and refuse to act, rather than
    silently rewriting state with a corrupted or empty view of release
    history.
    """


class GitHubRelease(NamedTuple):
    """Release metadata for a single version from GitHub."""

    date: str
    """Publication date in `YYYY-MM-DD` format."""

    body: str
    """Release description body (markdown)."""


def get_github_releases(repo_url: str) -> dict[str, GitHubRelease]:
    """Get versions and dates for all GitHub releases.

    Fetches all releases via the GitHub API with pagination. Extracts
    version numbers by stripping the `v` prefix from tag names. Uses
    `published_at` (falling back to `created_at`) for the date.

    :param repo_url: Repository URL (e.g.
        `https://github.com/user/repo`).
    :return: Dict mapping version strings to {class}`GitHubRelease`
        tuples. Empty dict only when the repository genuinely has no
        releases (the API returned an empty page) or when `repo_url`
        does not parse to an `owner/repo` pair.
    :raises GitHubReleasesUnavailable: When any page fetch fails or
        returns unparsable JSON. An empty return value from this
        function means "the repo has no releases"; a raised exception
        means "we don't know."
    """
    parsed = _owner_repo(repo_url)
    if parsed is None:
        return {}
    owner, repo = parsed

    # Check cache.
    cache_key = f"{owner}/{repo}"
    ttl = load_repomatic_config().cache.github_releases_ttl
    cached = get_cached_response("github-releases", cache_key, ttl)
    if cached is not None:
        try:
            data = json.loads(cached)
            return {
                v: GitHubRelease(date=r["date"], body=r["body"])
                for v, r in data.items()
            }
        except (json.JSONDecodeError, KeyError, TypeError):
            pass

    result: dict[str, GitHubRelease] = {}
    for release in _fetch_release_pages(owner, repo):
        tag = release.get("tag_name", "")
        if tag.startswith("v"):
            date = _release_date(release)
            if date:
                result[tag[1:]] = GitHubRelease(date=date, body=release.get("body", ""))

    # Cache non-empty results.
    if result and ttl > 0:
        serialized = {v: {"date": r.date, "body": r.body} for v, r in result.items()}
        store_response(
            "github-releases",
            cache_key,
            json.dumps(serialized).encode(),
        )

    return result


def get_release_tags(repo_url: str) -> dict[str, GitHubRelease]:
    """Get all releases keyed by their raw, unstripped tag name.

    {func}`get_github_releases` keeps only `v`-prefixed tags (and strips the
    `v`), which drops tools whose release tags use another scheme (lychee's
    `lychee-v…`, biome's `@biomejs/biome@…`). `sync-tool-versions` and
    `sync-action-pins` need every tag, so the version can be extracted with a
    per-tool pattern.

    :param repo_url: Repository URL.
    :return: Dict mapping raw tag names to {class}`GitHubRelease` tuples.
        Empty only when the repository has no releases or `repo_url` does not
        parse to an `owner/repo` pair.
    :raises GitHubReleasesUnavailable: When any page fetch fails or returns
        unparsable JSON.
    """
    parsed = _owner_repo(repo_url)
    if parsed is None:
        return {}
    owner, repo = parsed

    cache_key = f"{owner}/{repo}"
    ttl = load_repomatic_config().cache.github_releases_ttl
    cached = get_cached_response("github-release-tags", cache_key, ttl)
    if cached is not None:
        try:
            data = json.loads(cached)
            return {
                t: GitHubRelease(date=r["date"], body=r["body"])
                for t, r in data.items()
            }
        except (json.JSONDecodeError, KeyError, TypeError):
            pass

    result: dict[str, GitHubRelease] = {}
    for release in _fetch_release_pages(owner, repo):
        tag = release.get("tag_name", "")
        date = _release_date(release)
        if tag and date:
            result[tag] = GitHubRelease(date=date, body=release.get("body", ""))

    if result and ttl > 0:
        serialized = {t: {"date": r.date, "body": r.body} for t, r in result.items()}
        store_response(
            "github-release-tags",
            cache_key,
            json.dumps(serialized).encode(),
        )

    return result


def resolve_tag_to_sha(repo_url: str, tag: str) -> str | None:
    """Resolve a release tag to its 40-character commit SHA.

    Reads the tag's git reference. An annotated tag points at an intermediate
    tag object, dereferenced one hop to the commit it targets; a lightweight
    tag points straight at the commit.

    :param repo_url: Repository URL.
    :param tag: The tag name to resolve (e.g. `v1.2.3`).
    :return: The commit SHA, or `None` when the tag cannot be resolved
        (network error, missing tag, or unexpected payload).
    """
    parsed = _owner_repo(repo_url)
    if parsed is None:
        return None
    owner, repo = parsed

    ref_url = GITHUB_API_TAG_REF_URL.format(owner=owner, repo=repo, tag=tag)
    try:
        with urlopen(_api_request(ref_url), timeout=10) as response:
            obj = json.loads(response.read()).get("object", {})
    except (URLError, TimeoutError, json.JSONDecodeError) as exc:
        logging.debug(f"Tag ref lookup failed for {owner}/{repo}@{tag}: {exc}")
        return None

    sha = obj.get("sha", "")
    # Lightweight tag: the ref already points at the commit.
    if obj.get("type") != "tag":
        return sha or None

    # Annotated tag: dereference the tag object to its target commit.
    tag_url = GITHUB_API_TAG_OBJECT_URL.format(owner=owner, repo=repo, sha=sha)
    try:
        with urlopen(_api_request(tag_url), timeout=10) as response:
            target = json.loads(response.read()).get("object", {})
    except (URLError, TimeoutError, json.JSONDecodeError) as exc:
        logging.debug(f"Annotated tag deref failed for {owner}/{repo}@{tag}: {exc}")
        return None
    return target.get("sha") or None


def extract_version(tag: str, tag_pattern: str | None) -> str | None:
    """Extract a version from a GitHub release tag.

    :param tag: The raw tag name.
    :param tag_pattern: A regex with a `version` named group, or `None` to
        strip a leading `v` (the common `vX.Y.Z` scheme).
    :return: The version string, or `None` when *tag_pattern* does not match.
    """
    if tag_pattern:
        match = re.match(tag_pattern, tag)
        return match.group("version") if match else None
    return tag.removeprefix("v")


def get_github_release_body(repo_url: str, version: str) -> tuple[str, str]:
    """Fetch the release notes body for a specific version from GitHub.

    Tries ``v{version}`` first (most common for Python packages), then the bare
    ``{version}`` tag.

    :param repo_url: GitHub repository URL.
    :param version: The version string (e.g. `7.13.5`).
    :return: A tuple of `(tag, body)` where `tag` is the matched tag name and
        `body` is the release notes markdown. Both are empty strings if no
        release is found.
    """
    parsed = _owner_repo(repo_url)
    if not parsed:
        return "", ""
    owner, repo = parsed

    # Cache keyed by version, not tag, since both tag spellings are tried.
    cache_key = f"{owner}/{repo}/{version}"
    ttl = load_repomatic_config().cache.github_release_ttl
    cached = get_cached_response("github-release", cache_key, ttl)
    if cached is not None:
        try:
            data = json.loads(cached)
            return data["tag"], data["body"]
        except (json.JSONDecodeError, KeyError, TypeError):
            pass

    for tag in (f"v{version}", version):
        url = GITHUB_API_RELEASE_BY_TAG_URL.format(owner=owner, repo=repo, tag=tag)
        try:
            with urlopen(_api_request(url), timeout=10) as response:
                data = json.loads(response.read())
        except (URLError, TimeoutError, json.JSONDecodeError):
            continue
        else:
            body = data.get("body", "")
            if ttl > 0:
                store_response(
                    "github-release",
                    cache_key,
                    json.dumps({"tag": tag, "body": body}).encode(),
                )
            return tag, body
    logging.debug(f"No GitHub release found for {repo_url} version {version}.")
    return "", ""


def fetch_github_release_notes(
    items: list[tuple[str, str, str, str, str | None]],
) -> dict[str, tuple[str, list[tuple[str, str]]]]:
    """Fetch GitHub release notes for a batch of version bumps.

    For each item, lists the repository's releases (a cached call, already warm
    from a prior candidate sweep) and keeps those whose extracted version lands
    in the half-open range `(old, new]`, oldest first. Non-GitHub datasources
    (npm, PyPI workflow literals) contribute no item here and render no notes.

    :param items: One `(name, repo_url, old, new, tag_pattern)` tuple per bumped
        pin, where *old* and *new* are bare versions and *tag_pattern* is the
        per-tool extraction regex (or `None` for the `vX.Y.Z` scheme).
    :return: A dict mapping names to `(repo_url, versions)` tuples, the same
        shape {func}`repomatic.uv.fetch_release_notes` returns, so
        {func}`repomatic.uv.format_release_notes` renders it unchanged. Only
        entries with at least one non-empty release body are included.
    """
    notes: dict[str, tuple[str, list[tuple[str, str]]]] = {}
    for name, repo_url, old, new, tag_pattern in items:
        try:
            tags = get_release_tags(repo_url)
        except GitHubReleasesUnavailable as exc:
            logging.warning(f"Skipping release notes for {name}: {exc}")
            continue
        try:
            old_version, new_version = Version(old), Version(new)
        except InvalidVersion:
            continue
        fetched: list[tuple[Version, str, str]] = []
        for tag, release in tags.items():
            version = extract_version(tag, tag_pattern)
            if not version or not release.body:
                continue
            try:
                parsed = Version(version)
            except InvalidVersion:
                continue
            if old_version < parsed <= new_version:
                fetched.append((parsed, tag, release.body))
        if fetched:
            fetched.sort(key=lambda entry: entry[0])
            notes[name] = (repo_url, [(tag, body) for _v, tag, body in fetched])
    return notes
