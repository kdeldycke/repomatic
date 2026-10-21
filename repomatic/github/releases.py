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
versions), tag-to-version extraction, tag-to-SHA resolution, and the
range-to-release-notes fetch every dependency report shares
({func}`fetch_github_release_notes`). The {mod}`repomatic.deps.pins` adapters
and the {mod}`repomatic.deps.report` release-notes helper build on top of these
reads, and so do the review steps of the prepare-release checklist
({func}`release_review_steps`).

The write helpers live here too, so every release writer carries the same
arguments and failure contract: {func}`edit_release_notes`, the shared
`gh release edit` path behind the dev pre-release sync
({mod}`repomatic.github.dev_release`) and the changelog-to-release-notes sync
({mod}`repomatic.github.release_sync`), {func}`create_release` behind the
release draft, and {func}`upload_release_files` behind every asset upload.
"""

from __future__ import annotations

import json
import logging
import re
from typing import NamedTuple

from ..cache import get_cached_response, store_response
from ..config import load_repomatic_config
from ..http import FetchError, get_json
from ..versions import parse_tag_version
from .gh import GhCommandError, api_headers, run_gh_command
from .urls import parse_github_repo

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from pathlib import Path

    from packaging.version import Version

GITHUB_API_RELEASES_URL = "https://api.github.com/repos/{owner}/{repo}/releases"
"""GitHub API URL for fetching all releases for a repository."""

GITHUB_API_TAG_REF_URL = (
    "https://api.github.com/repos/{owner}/{repo}/git/ref/tags/{tag}"
)
"""GitHub API URL for resolving a tag name to its git object."""

GITHUB_API_TAG_OBJECT_URL = "https://api.github.com/repos/{owner}/{repo}/git/tags/{sha}"
"""GitHub API URL for dereferencing an annotated tag object to its commit."""

RELEASES_PAGE_SIZE = 100
"""Releases per page of a listing, the most the REST API serves."""

RELEASE_TAGS_CACHE = "github-release-tags"
"""HTTP-cache namespace of the release map {func}`get_release_tags` reads."""

RELEASE_NOT_FOUND_MARKER = "release not found"
"""What `gh release edit` writes to its error output for a tag with no release."""


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
            + f"?per_page={RELEASES_PAGE_SIZE}&page={page}"
        )
        try:
            data = get_json(url, headers=api_headers())
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
        # A short page is the last one: asking for the next would only fetch
        # an empty one.
        if len(data) < RELEASES_PAGE_SIZE:
            break
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

    html_url: str = ""
    """Browser URL of the release page.

    For a **draft** release this is the only resolvable link: drafts have no
    public `releases/tag/<tag>` URL, so GitHub serves them at an unguessable
    `releases/tag/untagged-<hash>` path exposed only in this field. The
    prepare-release PR body links the rolling dev pre-release through it (see
    {func}`dev_release_url_and_previous_version`).
    """


def get_release_tags(
    repo_url: str, *, force_refresh: bool = False
) -> dict[str, GitHubRelease]:
    """Get every published release, keyed by its raw, unstripped tag name.

    Fetches every release page on a cache miss, and caches a non-empty map
    under {data}`RELEASE_TAGS_CACHE`. Every tag scheme is kept, so a caller
    extracts the version with its own pattern: lychee tags `lychee-v…`, biome
    tags `@biomejs/biome@…`. Uses `published_at` (falling back to
    `created_at`) for the date.

    A draft is left out. It is visible to a maintainer's token alone, so a map
    holding it would answer differently per token, and a draft has no
    published notes to compare or link to.

    :param repo_url: Repository URL (like `https://github.com/user/repo`).
    :param force_refresh: Ignore any cached map and re-fetch. A cached map
        predating a release reports it as absent, so callers acting on an
        absence around release time should re-confirm live.
    :return: Dict mapping raw tag names to {class}`GitHubRelease` tuples.
        Empty only when the repository has no published release or *repo_url*
        does not parse to an `owner/repo` pair.
    :raises GitHubReleasesUnavailable: When any page fetch fails or returns
        unparsable JSON. An empty return value means "the repo has no
        releases"; a raised exception means "the answer is unknown."
    """
    parsed = parse_github_repo(repo_url)
    if parsed is None:
        return {}
    owner, repo = parsed

    cache_key = f"{owner}/{repo}"
    ttl = load_repomatic_config().cache.github_releases_ttl
    cached = (
        None
        if force_refresh
        else get_cached_response(RELEASE_TAGS_CACHE, cache_key, ttl)
    )
    if cached is not None:
        try:
            data = json.loads(cached)
            return {
                tag: GitHubRelease(date=r["date"], body=r["body"])
                for tag, r in data.items()
            }
        except (json.JSONDecodeError, KeyError, TypeError):
            pass

    result: dict[str, GitHubRelease] = {}
    for release in _fetch_release_pages(owner, repo):
        tag = release.get("tag_name", "")
        date = _release_date(release)
        if tag and date and not release.get("draft"):
            result[tag] = GitHubRelease(date=date, body=release.get("body") or "")

    if result and ttl > 0:
        serialized = {
            tag: {"date": r.date, "body": r.body} for tag, r in result.items()
        }
        store_response(RELEASE_TAGS_CACHE, cache_key, json.dumps(serialized).encode())

    return result


def get_github_releases(
    repo_url: str, *, force_refresh: bool = False
) -> dict[str, GitHubRelease]:
    """Get the published releases tagged `vX.Y.Z`, keyed by bare version.

    The {func}`get_release_tags` map, narrowed to the `v`-prefixed tags with
    the prefix stripped.

    :param repo_url: Repository URL (like `https://github.com/user/repo`).
    :param force_refresh: Ignore any cached map and re-fetch: see
        {func}`get_release_tags`.
    :return: Dict mapping version strings to {class}`GitHubRelease` tuples.
    :raises GitHubReleasesUnavailable: When any page fetch fails or returns
        unparsable JSON.
    """
    tags = get_release_tags(repo_url, force_refresh=force_refresh)
    return {tag[1:]: release for tag, release in tags.items() if tag.startswith("v")}


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
    parsed = parse_github_repo(repo_url)
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
                html_url=release.get("html_url", ""),
            )
        )
    return releases


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

    target = parse_tag_version(version)
    dev_release_url = None
    finals: list[Version] = []
    for release in releases:
        parsed = parse_tag_version(release.tag)
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


def release_review_steps(repo_url: str | None, version: str) -> tuple[str, str]:
    """Build the two optional review steps of the prepare-release checklist.

    The `How-to release` list opens with two review steps that render only
    when their GitHub data is reachable, so the checklist degrades to the bare
    merge instructions offline (or when the dev pre-release is disabled):

    - **Dev pre-release review**: links the rolling `v{version}.dev0` draft
      through its {attr}`~ReleaseWithAssets.html_url` (drafts have no public
      tag URL). Omitted when no such draft is visible.
    - **Full-changes review**: links the `v{previous}...main` comparison.
      Omitted when no prior release exists to compare against.

    Both come from a single {func}`dev_release_url_and_previous_version`
    lookup. Each returned string is a complete ordered-list line ending in a
    newline (or empty), written with a `1.` marker so the surrounding lazily
    numbered list renumbers correctly however many steps survive.

    :param repo_url: Repository URL. Without one, both steps are empty.
    :param version: The release version being prepared (like `1.2.3`).
    :return: A `(dev_release_review, changes_review)` pair of list-item lines.
    """
    if not repo_url:
        return "", ""
    dev_release_url, previous_version = dev_release_url_and_previous_version(
        repo_url, version
    )

    dev_release_review = ""
    if dev_release_url:
        dev_release_review = (
            f"1. Review the [`v{version}.dev0` GitHub release]({dev_release_url})\n"
        )

    changes_review = ""
    if previous_version:
        changes_review = (
            "1. Review the full changes: "
            f"[`v{previous_version}...main`]"
            f"({repo_url}/compare/v{previous_version}...main)\n"
        )

    return dev_release_review, changes_review


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
    parsed = parse_github_repo(repo_url)
    if parsed is None:
        return None
    owner, repo = parsed

    ref_url = GITHUB_API_TAG_REF_URL.format(owner=owner, repo=repo, tag=tag)
    try:
        data = get_json(ref_url, headers=api_headers())
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
        data = get_json(tag_url, headers=api_headers())
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

    :param tag: Git tag name of the release (like `v1.2.3`).
    :param repository: GitHub repository in `owner/name` form.
    :param body: The new release body text.
    :param title: When non-empty, also replace the release title.
    :return: `True` when the edit landed, `False` when no release carries
        *tag*.
    :raises GhCommandError: When the edit failed for any other reason. A
        transient failure is not an absence: a caller reading it as one would
        create a second release for the same tag.
    """
    args = ["release", "edit", tag, "--repo", repository, "--notes", body]
    if title:
        args += ["--title", title]
    try:
        run_gh_command(args)
    except GhCommandError as error:
        if _is_missing_release(error):
            logging.debug(f"No release carries {tag} in {repository}.")
            return False
        logging.warning(f"Could not edit the release notes of {tag}: {error}")
        raise
    return True


def _is_missing_release(error: GhCommandError) -> bool:
    """Whether a failed `gh release` command says the release does not exist."""
    return error.status == 404 or RELEASE_NOT_FOUND_MARKER in error.stderr


def release_exists(tag: str, repository: str) -> bool:
    """Whether *repository* has a release for *tag*, draft or published.

    :param tag: Git tag name of the release (like `v1.2.3`).
    :param repository: GitHub repository in `owner/name` form.
    :raises GhCommandError: When the lookup fails for another reason than an
        absent release. A transient failure is not an absence: a caller reading
        it as one would create a second release for the same tag.
    """
    try:
        run_gh_command([
            "release",
            "view",
            tag,
            "--repo",
            repository,
            "--json",
            "tagName",
        ])
    except GhCommandError as error:
        if _is_missing_release(error):
            return False
        raise
    return True


def create_release(
    tag: str,
    repository: str,
    *,
    target: str,
    notes: str,
    files: Sequence[Path] = (),
) -> bool:
    """Create the release draft of *tag*, unless a release already carries it.

    The draft is titled after its tag, targets *target*, and starts with
    *files* attached. It stays a draft: assets can still be added to it, and
    publishing it is the last step of the release, after which an immutable
    release locks them.

    Idempotent: a release that already exists, from an earlier attempt of the
    same run, is left as it is.

    :param tag: Git tag name of the release (like `v1.2.3`).
    :param repository: GitHub repository in `owner/name` form.
    :param target: The commit the tag points at, created with the release when
        the tag does not exist yet.
    :param notes: The release notes.
    :param files: Assets to attach.
    :return: `True` when the draft was created, `False` when a release
        already carried *tag*.
    :raises GhCommandError: When the lookup or the creation fails.
    """
    if release_exists(tag, repository):
        logging.info(f"Release {tag} already exists, skipping creation.")
        return False
    run_gh_command([
        "release",
        "create",
        tag,
        "--draft",
        "--target",
        target,
        "--title",
        tag,
        "--repo",
        repository,
        "--notes",
        notes,
        *(str(path) for path in files),
    ])
    logging.info(f"Created release draft {tag}.")
    return True


def upload_release_files(tag: str, repository: str, files: Sequence[Path]) -> None:
    """Attach *files* to the release of *tag*, replacing same-named assets.

    The one upload path of every release writer: the release draft of the
    release workflow and the rolling dev pre-release alike. An upload to a
    published immutable release fails, so the release workflow attaches every
    asset while its release is still a draft.

    :param tag: Git tag name of the release (like `v1.2.3`).
    :param repository: GitHub repository in `owner/name` form.
    :param files: The files to attach. Empty uploads nothing.
    :raises GhCommandError: When the upload fails.
    """
    if not files:
        logging.info(f"Nothing to upload to {tag}.")
        return
    run_gh_command([
        "release",
        "upload",
        tag,
        *(str(path) for path in files),
        "--repo",
        repository,
        "--clobber",
    ])
    logging.info(f"Uploaded {len(files)} file(s) to {tag}.")


class ReleaseNotesRequest(NamedTuple):
    """One version bump whose GitHub release notes a report shows."""

    name: str
    """The name the report keys the notes by: a tool, a package, an action."""

    repo_url: str
    """The GitHub repository publishing the releases."""

    old: str
    """The bare version the bump leaves. Empty for a new dependency, whose
    notes are those of {attr}`new` alone."""

    new: str
    """The bare version the bump adopts."""

    tag_pattern: str | None = None
    """A regex with a `version` group, reading the version out of a tag. `None`
    for the `vX.Y.Z` scheme, which also passes a bare `X.Y.Z` tag through."""

    skip: frozenset[str] = frozenset()
    """Versions left out of the notes, like the releases a registry yanked: a
    pulled release has no notes worth showing."""


def in_bump_range(version: Version, old: Version | None, new: Version) -> bool:
    """Whether the notes of a bump from *old* to *new* cover *version*.

    The half-open range `(old, new]`. A pre-release inside it is left out, the
    way the sweep that picks *new* leaves it out. *new* itself is kept, even as
    a pre-release. With no *old*, the range is *new* alone.

    :param version: The version of a release.
    :param old: The version the bump leaves, or `None` for a new dependency.
    :param new: The version the bump adopts.
    """
    if old is None:
        return version == new
    return old < version <= new and (not version.is_prerelease or version == new)


def fetch_github_release_notes(
    requests: Iterable[ReleaseNotesRequest],
) -> dict[str, tuple[str, list[tuple[str, str]]]]:
    """Fetch the GitHub release notes of a batch of version bumps.

    For each request, lists the releases of the repository once (a cached
    call, often warm from a prior candidate sweep) and keeps those
    {func}`in_bump_range` covers, oldest first. A repository costs one listing
    per page of {data}`RELEASES_PAGE_SIZE` releases, however many versions the
    bump spans. Non-GitHub datasources (npm, PyPI workflow literals) make no
    request here and render no notes.

    :param requests: One request per bump.
    :return: A dict mapping names to `(repo_url, versions)` tuples, where
        `versions` holds a `(tag, body)` pair per release. Only names with at
        least one non-empty release body are included.
    """
    notes: dict[str, tuple[str, list[tuple[str, str]]]] = {}
    for request in requests:
        try:
            tags = get_release_tags(request.repo_url)
        except GitHubReleasesUnavailable as exc:
            logging.warning(f"Skipping release notes for {request.name}: {exc}")
            continue
        old_version = parse_tag_version(request.old) if request.old else None
        new_version = parse_tag_version(request.new)
        if new_version is None or (request.old and old_version is None):
            continue
        skipped = {parse_tag_version(version) for version in request.skip}
        fetched: list[tuple[Version, str, str]] = []
        for tag, release in tags.items():
            version = extract_version(tag, request.tag_pattern)
            if not version or not release.body:
                continue
            parsed = parse_tag_version(version)
            if (
                parsed is None
                or parsed in skipped
                or not in_bump_range(parsed, old_version, new_version)
            ):
                continue
            fetched.append((parsed, tag, release.body))
        if fetched:
            fetched.sort(key=lambda entry: entry[0])
            notes[request.name] = (
                request.repo_url,
                [(tag, body) for _version, tag, body in fetched],
            )
    return notes
