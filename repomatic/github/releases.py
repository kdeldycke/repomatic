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
{mod}`repomatic.version_sync` adapters and {mod}`repomatic.dep_report`
release-notes helper build on top of these reads.

One write helper lives here too: {func}`edit_release_notes`, the shared
`gh release edit` path behind the dev pre-release sync
({mod}`repomatic.github.dev_release`) and the changelog-to-release-notes sync
({mod}`repomatic.github.release_sync`), so both writers carry the same
arguments and failure contract.
"""

from __future__ import annotations

import json
import logging
import re
from typing import NamedTuple

from ..cache import get_cached_response, store_response
from ..config import load_repomatic_config
from ..http import FetchError, get_json
from ..versions import safe_version
from .gh import api_headers, run_gh_command

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Callable

    from packaging.version import Version

GITHUB_API_RELEASES_URL = "https://api.github.com/repos/{owner}/{repo}/releases"
"""GitHub API URL for fetching all releases for a repository."""

GITHUB_API_TAG_REF_URL = (
    "https://api.github.com/repos/{owner}/{repo}/git/ref/tags/{tag}"
)
"""GitHub API URL for resolving a tag name to its git object."""

GITHUB_API_TAG_OBJECT_URL = "https://api.github.com/repos/{owner}/{repo}/git/tags/{sha}"
"""GitHub API URL for dereferencing an annotated tag object to its commit."""

GITHUB_API_RELEASE_BY_TAG_URL = (
    "https://api.github.com/repos/{owner}/{repo}/releases/tags/{tag}"
)
"""GitHub API URL for fetching a single release by tag name."""


def owner_repo(repo_url: str) -> tuple[str, str] | None:
    """Extract `(owner, repo)` from a GitHub repository URL.

    :param repo_url: Repository URL (e.g. `https://github.com/user/repo`).
    :return: An `(owner, repo)` pair, or `None` when the URL does not parse.
    """
    parts = repo_url.rstrip("/").removesuffix(".git").split("/")
    if len(parts) < 2:
        return None
    return parts[-2], parts[-1]


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
            data, _raw = get_json(url, headers=api_headers())
        except FetchError as exc:
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


class ReleaseAsset(NamedTuple):
    """A single downloadable asset attached to a GitHub release."""

    name: str
    """Asset filename."""

    size: int
    """Asset size in bytes."""

    sha256: str
    """SHA-256 hex digest from the API's `digest` field.

    Empty for assets uploaded before GitHub started recording digests
    (mid-2025), where the API returns `digest: null`.
    """

    download_url: str
    """Public browser download URL."""


class ReleaseWithAssets(NamedTuple):
    """Full release metadata including its assets and visibility flags."""

    tag: str
    """Raw tag name (e.g. `v1.2.3`)."""

    date: str
    """Publication date in `YYYY-MM-DD` format."""

    draft: bool
    """`True` for draft releases, which are only visible to maintainers."""

    prerelease: bool
    """`True` for releases marked as pre-release."""

    assets: tuple[ReleaseAsset, ...]
    """Assets attached to the release, in API order."""

    body: str = ""
    """Release notes body (markdown).

    Carried so `sync-binaries --backfill-records` can recover detection
    snapshots from the legacy VirusTotal tables that release notes held
    before the scan history file existed.
    """

    html_url: str = ""
    """Browser URL of the release page.

    For a **draft** release this is the only resolvable link: drafts have no
    public `releases/tag/<tag>` URL, so GitHub serves them at an unguessable
    `releases/tag/untagged-<hash>` path exposed only in this field. The
    prepare-release PR body links the rolling dev pre-release through it (see
    {func}`dev_release_url_and_previous_version`).
    """


def _cached_release_map(
    namespace: str,
    repo_url: str,
    key_for: Callable[[str], str | None],
    *,
    force_refresh: bool = False,
) -> dict[str, GitHubRelease]:
    """Fetch a repository's releases as a keyed map, through the HTTP cache.

    The shared body of {func}`get_github_releases` and
    {func}`get_release_tags`: check the *namespace* cache, fetch every release
    page on a miss, key each dated release by `key_for(tag)` (releases mapped
    to `None` are dropped), and cache a non-empty result.

    :param namespace: HTTP-cache namespace to read and write.
    :param repo_url: Repository URL.
    :param key_for: Maps a raw tag name to the result key, or `None` to skip
        that release.
    :param force_refresh: Ignore any cached map and re-fetch every page.
    :return: Dict mapping keys to {class}`GitHubRelease` tuples. Empty when
        the repository has no releases or *repo_url* does not parse to an
        `owner/repo` pair.
    :raises GitHubReleasesUnavailable: When any page fetch fails or returns
        unparsable JSON.
    """
    parsed = owner_repo(repo_url)
    if parsed is None:
        return {}
    owner, repo = parsed

    cache_key = f"{owner}/{repo}"
    ttl = load_repomatic_config().cache.github_releases_ttl
    cached = None if force_refresh else get_cached_response(namespace, cache_key, ttl)
    if cached is not None:
        try:
            data = json.loads(cached)
            return {
                key: GitHubRelease(date=r["date"], body=r["body"])
                for key, r in data.items()
            }
        except (json.JSONDecodeError, KeyError, TypeError):
            pass

    result: dict[str, GitHubRelease] = {}
    for release in _fetch_release_pages(owner, repo):
        key = key_for(release.get("tag_name", ""))
        if key is None:
            continue
        date = _release_date(release)
        if date:
            result[key] = GitHubRelease(date=date, body=release.get("body", ""))

    # Cache non-empty results.
    if result and ttl > 0:
        serialized = {
            key: {"date": r.date, "body": r.body} for key, r in result.items()
        }
        store_response(namespace, cache_key, json.dumps(serialized).encode())

    return result


def get_github_releases(
    repo_url: str, *, force_refresh: bool = False
) -> dict[str, GitHubRelease]:
    """Get versions and dates for all GitHub releases.

    Fetches all releases via the GitHub API with pagination. Extracts
    version numbers by stripping the `v` prefix from tag names. Uses
    `published_at` (falling back to `created_at`) for the date.

    :param repo_url: Repository URL (e.g.
        `https://github.com/user/repo`).
    :param force_refresh: Ignore any cached map and re-fetch. A cached map
        predating a release reports it as absent, so callers acting on an
        absence around release time should re-confirm live.
    :return: Dict mapping version strings to {class}`GitHubRelease`
        tuples. Empty dict only when the repository genuinely has no
        releases (the API returned an empty page) or when `repo_url`
        does not parse to an `owner/repo` pair.
    :raises GitHubReleasesUnavailable: When any page fetch fails or
        returns unparsable JSON. An empty return value from this
        function means "the repo has no releases"; a raised exception
        means "the answer is unknown."
    """
    return _cached_release_map(
        "github-releases",
        repo_url,
        lambda tag: tag[1:] if tag.startswith("v") else None,
        force_refresh=force_refresh,
    )


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
    return _cached_release_map(
        "github-release-tags",
        repo_url,
        lambda tag: tag or None,
    )


def get_releases_with_assets(repo_url: str) -> list[ReleaseWithAssets]:
    """Get every release with its assets, visibility flags, and digests.

    Deliberately uncached, unlike {func}`get_github_releases`: the main
    consumer is `sync-binaries`, which runs minutes after a release is
    published and must see the assets that were just uploaded. A cached
    view would regenerate the binaries page from a pre-release snapshot.

    :param repo_url: Repository URL (e.g. `https://github.com/user/repo`).
    :return: One {class}`ReleaseWithAssets` per release (drafts and
        pre-releases included, for the caller to filter), in API order
        (newest first). Empty when the repository has no releases or
        *repo_url* does not parse to an `owner/repo` pair.
    :raises GitHubReleasesUnavailable: When any page fetch fails or returns
        unparsable JSON.
    """
    parsed = owner_repo(repo_url)
    if parsed is None:
        return []
    owner, repo = parsed

    releases = []
    for release in _fetch_release_pages(owner, repo):
        assets = tuple(
            ReleaseAsset(
                name=asset.get("name", ""),
                size=asset.get("size", 0),
                # The API reports digests as "sha256:<hex>"; strip the
                # algorithm prefix to match VirusTotal URL and git usage.
                sha256=(asset.get("digest") or "").removeprefix("sha256:"),
                download_url=asset.get("browser_download_url", ""),
            )
            for asset in release.get("assets", ())
        )
        releases.append(
            ReleaseWithAssets(
                tag=release.get("tag_name", ""),
                date=_release_date(release),
                draft=release.get("draft", False),
                prerelease=release.get("prerelease", False),
                assets=assets,
                body=release.get("body") or "",
                html_url=release.get("html_url", ""),
            )
        )
    return releases


def parse_release_version(tag: str) -> Version | None:
    """Parse a release tag as a version, or `None` for foreign tag schemes."""
    return safe_version(tag.removeprefix("v"))


def dev_release_url_and_previous_version(
    repo_url: str, version: str
) -> tuple[str | None, str | None]:
    """Look up the two release references the prepare-release PR body links to.

    A single {func}`get_releases_with_assets` fetch yields both:

    - **Dev pre-release URL**: the {attr}`~ReleaseWithAssets.html_url` of the
      draft pre-release whose version shares *version*'s release segment (the
      rolling `v{version}.dev0` draft). Drafts are visible only to
      authenticated maintainers, so an unauthenticated or token-less caller
      gets `None` here even when the draft exists.
    - **Previous version**: the highest final release (draft, pre-release, and
      `.dev` tags excluded) already published. At prepare-release time the tag
      for *version* does not exist yet, so this is the release the new one
      supersedes, used for the `v{previous}...main` comparison link.

    :param repo_url: Repository URL (e.g. `https://github.com/user/repo`).
    :param version: The release version being prepared (e.g. `1.2.3`), with
        the `.dev` suffix already stripped.
    :return: An `(dev_release_url, previous_version)` pair. Either element is
        `None` when its release cannot be found or the API is unavailable, so
        the caller degrades each list item independently.
    """
    try:
        releases = get_releases_with_assets(repo_url)
    except GitHubReleasesUnavailable:
        return None, None

    target = parse_release_version(version)
    dev_release_url = None
    finals: list[Version] = []
    for release in releases:
        parsed = parse_release_version(release.tag)
        if parsed is None:
            continue
        if release.draft:
            if (
                target is not None
                and parsed.is_devrelease
                and parsed.base_version == target.base_version
            ):
                dev_release_url = release.html_url or None
        elif not release.prerelease and not parsed.is_prerelease:
            finals.append(parsed)

    previous_version = str(max(finals)) if finals else None
    return dev_release_url, previous_version


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
    parsed = owner_repo(repo_url)
    if parsed is None:
        return None
    owner, repo = parsed

    ref_url = GITHUB_API_TAG_REF_URL.format(owner=owner, repo=repo, tag=tag)
    try:
        data, _raw = get_json(ref_url, headers=api_headers())
    except FetchError as exc:
        logging.debug(f"Tag ref lookup failed for {owner}/{repo}@{tag}: {exc}")
        return None
    obj = data.get("object", {})

    sha = obj.get("sha", "")
    # Lightweight tag: the ref already points at the commit.
    if obj.get("type") != "tag":
        return sha or None

    # Annotated tag: dereference the tag object to its target commit.
    tag_url = GITHUB_API_TAG_OBJECT_URL.format(owner=owner, repo=repo, sha=sha)
    try:
        data, _raw = get_json(tag_url, headers=api_headers())
    except FetchError as exc:
        logging.debug(f"Annotated tag deref failed for {owner}/{repo}@{tag}: {exc}")
        return None
    return data.get("object", {}).get("sha") or None


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


def edit_release_notes(
    tag: str, repository: str, body: str, *, title: str = ""
) -> bool:
    """Edit a release's notes (and optionally its title) in place.

    The one `gh release edit` path shared by every release writer, so the
    dev pre-release sync and the changelog-to-release-notes sync carry the
    same arguments and failure contract. Assets are never touched.

    :param tag: Git tag name of the release (e.g. `v1.2.3`).
    :param repository: GitHub repository in `owner/name` form.
    :param body: The new release body text.
    :param title: When non-empty, also replace the release title.
    :return: `True` when the edit landed, `False` when the release does not
        exist or the edit failed.
    """
    args = ["release", "edit", tag, "--repo", repository, "--notes", body]
    if title:
        args += ["--title", title]
    try:
        run_gh_command(args)
    except RuntimeError:
        return False
    return True


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
    parsed = owner_repo(repo_url)
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
            data, _raw = get_json(url, headers=api_headers())
        except FetchError:
            continue
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
        shape {func}`repomatic.dep_report.fetch_release_notes` returns, so
        {func}`repomatic.dep_report.format_release_notes` renders it unchanged. Only
        entries with at least one non-empty release body are included.
    """
    notes: dict[str, tuple[str, list[tuple[str, str]]]] = {}
    for name, repo_url, old, new, tag_pattern in items:
        try:
            tags = get_release_tags(repo_url)
        except GitHubReleasesUnavailable as exc:
            logging.warning(f"Skipping release notes for {name}: {exc}")
            continue
        old_version = parse_release_version(old)
        new_version = parse_release_version(new)
        if old_version is None or new_version is None:
            continue
        fetched: list[tuple[Version, str, str]] = []
        for tag, release in tags.items():
            version = extract_version(tag, tag_pattern)
            if not version or not release.body:
                continue
            parsed = parse_release_version(version)
            if parsed is None:
                continue
            if old_version < parsed <= new_version:
                fetched.append((parsed, tag, release.body))
        if fetched:
            fetched.sort(key=lambda entry: entry[0])
            notes[name] = (repo_url, [(tag, body) for _v, tag, body in fetched])
    return notes
