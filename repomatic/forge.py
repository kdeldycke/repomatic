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

"""Read a repository's metrics from whichever forge hosts it.

Answers one question for one repository: how many accounts follow it, when it
was created, when it last shipped, and when it was last touched. GitHub, GitLab
(on any instance) and Forgejo or Gitea (likewise) each expose that through a
different API, and {func}`repo_metrics` picks the right one from the URL's
host.

One call per repository on every forge, which is what lets a single sampler
collect every metric {mod}`repomatic.metrics` records rather than one call per
metric family.
"""

from __future__ import annotations

import logging
import urllib.parse
from dataclasses import dataclass

from .github.gh import gh_graphql
from .http import FetchError, get_json

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping
    from typing import Any

FORGE_APIS: dict[str, str] = {
    "codeberg.org": "forgejo",
    "github.com": "github",
    "gitlab.com": "gitlab",
}
"""Forge software each known host runs, which is what selects the API to call.

Never guessed from the host name: an unknown host raises instead, so a subject
landing on a fourth kind of forge has to declare how to read it rather than
silently sampling nothing. Extend it through
`[tool.repomatic.metrics] forges`, which a repository uses to name the
self-hosted instances it tracks (`salsa.debian.org` runs GitLab,
`gitlab.archlinux.org` too).
"""

FORGE_USER_AGENT = "repomatic forge metrics collector"
"""Sent to every forge API, where a browser identity backfires.

Several self-hosted GitLab instances answer a browser user-agent with a page
rather than a payload, returning kilobytes of HTML where the same URL fetched
under a plain agent returns a small JSON document. Nothing errors, so the
symptom is a subject quietly missing from the readings rather than a failed
run.
"""

GITHUB_HOST = "github.com"
"""The one host whose deep collectors exist.

An exact star reconstruction reads per-star timestamps, and an archive backfill
mines `github.com` pages: both are GitHub-only, so a subject elsewhere is
skipped by them rather than failed.
"""

GITHUB_METRICS_QUERY = """
query($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) {
    createdAt
    stargazerCount
    latestRelease { publishedAt }
    defaultBranchRef { target { ... on Commit { committedDate } } }
    refs(refPrefix: "refs/tags/", first: 1,
         orderBy: {field: TAG_COMMIT_DATE, direction: DESC}) {
      nodes {
        target {
          ... on Commit { committedDate }
          ... on Tag { target { ... on Commit { committedDate } } }
        }
      }
    }
  }
}
"""
"""Reads a repository's whole metric set in one call.

One call where REST needs four, and correct where REST is not: `/tags` answers
in an order nobody should assume, so a fallback trusting it can date a live
project a decade into the past. Ordering on `TAG_COMMIT_DATE` states the
question instead of hoping the default matches it.

The commit date is read off the default branch rather than from the
repository's `pushedAt`, which any push to any branch bumps.
"""


@dataclass(frozen=True)
class ForgeMetrics:
    """One repository's metrics, as any forge reports them."""

    stars: int
    """Count of accounts following the repository on its own forge."""

    created: str | None = None
    """ISO date the repository was opened.

    The one date a star count is known to be zero, which is what gives a
    history an origin and a by-age chart something to align on.
    """

    release: str | None = None
    """ISO date of the newest release or tag, `None` when a project has neither."""

    release_source: str | None = None
    """Where {attr}`release` came from: a `release` object, or a bare `tag`.

    Recorded because the two are not the same claim. A release is something the
    project announced; a tag is only the newest thing it labelled, which is the
    closest available answer for the many projects that never cut a release.
    """

    commit: str | None = None
    """ISO date of the newest commit on the default branch.

    The half of the activity reading that stays true for a rolling repository.
    A widely used package archive can go a decade without tagging a release
    while being committed to several times a day: a release date alone would
    report it as long dead.
    """

    def readings(self) -> Iterator[tuple[str, str]]:
        """Yield each metric this reading carries, as `(metric id, value)`.

        The bridge between a typed forge answer and the metric store, which
        holds every value as text. A metric the forge did not answer yields
        nothing rather than an empty string, so a project with no release adds
        no row instead of a blank one.

        {attr}`created` is deliberately absent: it is not a metric but the
        origin of one, recorded by the sampler as a `stars` reading of zero on
        that date.
        """
        yield "stars", str(self.stars)
        if self.commit:
            yield "commit", self.commit
        if self.release:
            yield "release", self.release
            yield "release_source", self.release_source or "release"


def split_repo_url(url: str) -> tuple[str, str]:
    """Split a repository URL into its host and its owner/name path.

    :param url: An `https://host/owner/name` repository URL.
    :return: The `(host, path)` pair.
    :raises ValueError: When the URL carries no host or no owner/name path.
    """
    host, _, path = url.removeprefix("https://").removeprefix("http://").partition("/")
    path = path.rstrip("/").removesuffix(".git")
    owner, _, name = path.partition("/")
    if not host or not owner or not name:
        msg = f"Not an https://host/owner/name repository URL: {url!r}"
        raise ValueError(msg)
    return host, path


def canonical_url(subject: str) -> str:
    """Normalize a configured subject into the URL the store keys on.

    A bare `owner/name` is GitHub, which is what a repository declaring a
    handful of peers writes. Anything else is already a URL and only needs its
    trailing decoration removed. One spelling in the store keeps a subject
    addressable whichever way its configuration named it.

    :param subject: An `owner/name` slug or a full repository URL.
    :return: The canonical `https://host/owner/name` URL.
    :raises ValueError: When neither shape parses.
    """
    if "://" not in subject:
        subject = f"https://{GITHUB_HOST}/{subject.strip('/')}"
    host, path = split_repo_url(subject)
    return f"https://{host}/{path}"


def forge_of(url: str, extra_forges: Mapping[str, str] | None = None) -> str:
    """Name the forge software running the host of *url*.

    :param url: An `https://host/owner/name` repository URL.
    :param extra_forges: Host-to-forge entries a repository declared for the
        self-hosted instances it tracks, merged over {data}`FORGE_APIS`.
    :return: One of `forgejo`, `github` or `gitlab`.
    :raises ValueError: When the host is not declared anywhere, which is
        deliberate: a silently unsampled subject is worse than a loud one.
    """
    host, _path = split_repo_url(url)
    forges = {**FORGE_APIS, **(extra_forges or {})}
    forge = forges.get(host)
    if forge is None:
        known = ", ".join(sorted(forges))
        msg = (
            f"Unknown forge host {host!r}. Declare it in "
            f"[tool.repomatic.metrics] forges. Known hosts: {known}."
        )
        raise ValueError(msg)
    if forge not in {"forgejo", "github", "gitlab"}:
        msg = (
            f"Unsupported forge {forge!r} for {host!r}: pick forgejo, github or gitlab."
        )
        raise ValueError(msg)
    return forge


def newest_dated(
    release: str | None,
    tag: str | None,
) -> tuple[str | None, str | None]:
    """Pick whichever of a project's newest release and newest tag is more recent.

    Not a preference for releases: plenty of projects carry a tag newer than
    their latest release object, some by close to a year, so always reading the
    release would report them as idle. ISO dates compare as strings, which is
    the whole of the arithmetic here.

    :param release: ISO date of the newest release, or `None`.
    :param tag: ISO date of the newest tag, or `None`.
    :return: The `(date, source)` pair, both `None` when a project has neither.
    """
    if release and tag:
        return (release, "release") if release >= tag else (tag, "tag")
    if release:
        return release, "release"
    if tag:
        return tag, "tag"
    return None, None


def forge_json(url: str) -> Any | None:
    """Read one JSON document from a forge's public API.

    Covers every forge but GitHub, whose authentication `gh` already carries.
    The instances read here (GitLab and Forgejo) serve their project metadata
    to anonymous callers, so no token is involved and none is asked for.

    :param url: The API endpoint to read.
    :return: The parsed payload, or `None` when the call or the parse failed.
    """
    try:
        payload, _raw = get_json(url, headers={"User-Agent": FORGE_USER_AGENT})
    except FetchError as error:
        logging.debug(f"Could not read {url}: {error}")
        return None
    return payload


def github_metrics(path: str) -> ForgeMetrics:
    """Read a GitHub repository through {data}`GITHUB_METRICS_QUERY`.

    :param path: The repository's `owner/name` path.
    :return: The repository's metrics.
    :raises RuntimeError: When the `gh` call fails.
    """
    owner, _, name = path.partition("/")
    repo = gh_graphql(GITHUB_METRICS_QUERY, owner=owner, name=name)["repository"]
    release = repo["latestRelease"]
    tag = None
    for node in repo["refs"]["nodes"]:
        # A lightweight tag points straight at its commit, an annotated one at
        # a tag object wrapping it, and the query asks for both shapes.
        target = node["target"]
        nested = target.get("target") or {}
        committed = target.get("committedDate") or nested.get("committedDate")
        if committed:
            tag = committed[:10]
    dated, source = newest_dated(release["publishedAt"][:10] if release else None, tag)
    branch = repo["defaultBranchRef"]
    return ForgeMetrics(
        stars=repo["stargazerCount"],
        created=str(repo["createdAt"])[:10],
        release=dated,
        release_source=source,
        commit=branch["target"]["committedDate"][:10] if branch else None,
    )


def gitlab_metrics(host: str, path: str) -> ForgeMetrics | None:
    """Read a GitLab project, on whichever instance hosts it.

    :param host: The instance's hostname.
    :param path: The project's namespace path.
    :return: The project's metrics, or `None` when unreadable.
    """
    project = f"https://{host}/api/v4/projects/{urllib.parse.quote(path, safe='')}"
    payload = forge_json(project)
    if not payload:
        return None
    releases = forge_json(f"{project}/releases?per_page=1")
    # Ordering spelled out rather than inherited: it is GitLab's current
    # default for tags, but the whole point of asking is to not depend on that.
    tags = forge_json(
        f"{project}/repository/tags?per_page=1&order_by=updated&sort=desc"
    )
    commits = forge_json(f"{project}/repository/commits?per_page=1")
    dated, source = newest_dated(
        releases[0]["released_at"][:10] if releases else None,
        tags[0]["commit"]["created_at"][:10] if tags else None,
    )
    return ForgeMetrics(
        stars=payload["star_count"],
        created=str(payload["created_at"])[:10] if payload.get("created_at") else None,
        release=dated,
        release_source=source,
        commit=commits[0]["committed_date"][:10] if commits else None,
    )


def forgejo_metrics(host: str, path: str) -> ForgeMetrics | None:
    """Read a Forgejo or Gitea repository, on whichever instance hosts it.

    :param host: The instance's hostname.
    :param path: The repository's `owner/name` path.
    :return: The repository's metrics, or `None` when unreadable.
    """
    repo = f"https://{host}/api/v1/repos/{path}"
    payload = forge_json(repo)
    if not payload:
        return None
    releases = forge_json(f"{repo}/releases?limit=1")
    tags = forge_json(f"{repo}/tags?limit=1")
    commits = forge_json(f"{repo}/commits?limit=1")
    dated, source = newest_dated(
        releases[0]["published_at"][:10] if releases else None,
        tags[0]["commit"]["created"][:10] if tags else None,
    )
    return ForgeMetrics(
        stars=payload["stars_count"],
        created=str(payload["created_at"])[:10] if payload.get("created_at") else None,
        release=dated,
        release_source=source,
        commit=commits[0]["commit"]["committer"]["date"][:10] if commits else None,
    )


def repo_metrics(
    url: str,
    extra_forges: Mapping[str, str] | None = None,
) -> ForgeMetrics | None:
    """Read one repository, through whichever API its host speaks.

    :param url: An `https://host/owner/name` repository URL.
    :param extra_forges: Host-to-forge entries for self-hosted instances.
    :return: The repository's metrics, or `None` when the forge could not be
        read.
    :raises ValueError: When the host declares no forge.
    :raises RuntimeError: When a GitHub call fails.
    """
    host, path = split_repo_url(url)
    forge = forge_of(url, extra_forges)
    if forge == "github":
        return github_metrics(path)
    if forge == "gitlab":
        return gitlab_metrics(host, path)
    return forgejo_metrics(host, path)
