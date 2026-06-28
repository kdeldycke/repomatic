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

"""GitHub Releases API integration."""

from __future__ import annotations

import json
import logging
import os
from typing import NamedTuple
from urllib.error import URLError
from urllib.request import Request, urlopen

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
