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

"""Read a repository's popularity and activity from whichever forge hosts it.

Answers one question for one repository: how many accounts follow it, when it
last shipped, and when it was last touched. GitHub, GitLab (on any instance)
and Forgejo or Gitea (likewise) each expose that through a different API, and
{func}`repo_metrics` picks the right one from the URL's host.

The readings accumulate in a committed JSON file, one record per tracked
project, rewritten only when a reading moves. Its companion
{mod}`repomatic.stars` keeps one record per repository *and* date instead: a
hundred projects sampled weekly would otherwise pile up thousands of records a
year that nothing ever reads chronologically. A record therefore carries the
date its reading was taken, which keeps a quiet week out of the diff and dates
a dead project's row honestly.
"""

from __future__ import annotations

import json
import logging
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone

from .github.gh import gh_graphql
from .http import FetchError, get_json

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path
    from typing import Any

FORGE_APIS: dict[str, str] = {
    "codeberg.org": "forgejo",
    "github.com": "github",
    "gitlab.com": "gitlab",
}
"""Forge software each known host runs, which is what selects the API to call.

Never guessed from the host name: an unknown host raises instead, so a project
landing on a fourth kind of forge has to declare how to read it rather than
silently sampling nothing. Extend it through
`[tool.repomatic.projects] forges`, which a repository uses to name the
self-hosted instances it tracks (`salsa.debian.org` runs GitLab,
`gitlab.archlinux.org` too).
"""

FORGE_USER_AGENT = "repomatic forge metrics collector"
"""Sent to every forge API, where a browser identity backfires.

Several self-hosted GitLab instances answer a browser user-agent with a page
rather than a payload, returning kilobytes of HTML where the same URL fetched
under a plain agent returns a small JSON document. Nothing errors, so the
symptom is a project quietly missing from the readings rather than a failed
run.
"""

PROJECT_SAMPLE_HEADER_DEFS: tuple[tuple[str, str], ...] = (
    ("Project", "project"),
    ("Stars", "stars"),
    ("Release", "release"),
    ("Commit", "commit"),
    ("Status", "status"),
)
"""Column definitions for the `repomatic sample-projects` table.

Lives beside the rows' domain model so the columns and the fields they render
cannot drift apart; the CLI derives its `--sort-by` choices from it.
"""

GITHUB_METRICS_QUERY = """
query($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) {
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
"""Reads a repository's stars, newest release, newest tag and last commit.

One call where REST needs three, and correct where REST is not: `/tags`
answers in an order nobody should assume, so a fallback trusting it can date a
live project a decade into the past. Ordering on `TAG_COMMIT_DATE` states the
question instead of hoping the default matches it.

The commit date is read off the default branch rather than from the
repository's `pushedAt`, which any push to any branch bumps.
"""


@dataclass(frozen=True)
class ForgeMetrics:
    """One repository's popularity and activity, as any forge reports them."""

    stars: int
    """Count of accounts following the repository on its own forge."""

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

    The second half of the activity reading, and the half that stays true for a
    rolling repository. A widely used package archive can go a decade without
    tagging a release while being committed to several times a day: a release
    date alone would report it as long dead.
    """


@dataclass(frozen=True)
class ProjectRecord:
    """One tracked project's reading, and the day it was taken."""

    project_id: str
    """Name the repository gives this project, and the record's identity."""

    repo: str
    """URL of the project's source repository."""

    sampled: str
    """Date the reading was taken, in `YYYY-MM-DD` form."""

    metrics: ForgeMetrics
    """What the forge answered on that date."""

    @property
    def reading(self) -> tuple[Any, ...]:
        """Everything but the date, for detecting a reading that moved.

        A week where nothing changed must leave the file untouched rather than
        restamping every unchanged row with today's date.
        """
        return (self.project_id, self.repo, self.metrics)

    def to_dict(self) -> dict[str, int | str]:
        """Flatten to a JSON-ready mapping, metrics inlined.

        The `id` and `date` keys spell what the attributes call `project_id`
        and `sampled`; the optional three are omitted rather than written as
        `null`, so a project with no release adds no key.
        """
        data: dict[str, int | str] = {
            "date": self.sampled,
            "id": self.project_id,
            "repo": self.repo,
            "stars": self.metrics.stars,
        }
        if self.metrics.commit:
            data["commit"] = self.metrics.commit
        if self.metrics.release:
            data["release"] = self.metrics.release
            data["release_source"] = self.metrics.release_source or "release"
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ProjectRecord:
        """Rebuild a record from its flattened JSON mapping."""
        return cls(
            project_id=data["id"],
            repo=data["repo"],
            sampled=data["date"],
            metrics=ForgeMetrics(
                stars=int(data["stars"]),
                release=data.get("release"),
                release_source=data.get("release_source"),
                commit=data.get("commit"),
            ),
        )


@dataclass(frozen=True)
class SampleOutcome:
    """What one project's sample produced, for the CLI to report."""

    project_id: str
    """The project the sampler was reading."""

    repo: str
    """URL it read from."""

    metrics: ForgeMetrics | None = None
    """The reading, or `None` when the forge could not be read."""

    error: str = ""
    """Why the reading failed, empty when it did not."""

    changed: bool = False
    """Whether this sample moved the stored record."""


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


def forge_of(url: str, extra_forges: Mapping[str, str] | None = None) -> str:
    """Name the forge software running the host of *url*.

    :param url: An `https://host/owner/name` repository URL.
    :param extra_forges: Host-to-forge entries a repository declared for the
        self-hosted instances it tracks, merged over {data}`FORGE_APIS`.
    :return: One of `forgejo`, `github` or `gitlab`.
    :raises ValueError: When the host is not declared anywhere, which is
        deliberate: a silently unsampled project is worse than a loud one.
    """
    host, _path = split_repo_url(url)
    forges = {**FORGE_APIS, **(extra_forges or {})}
    forge = forges.get(host)
    if forge is None:
        known = ", ".join(sorted(forges))
        msg = (
            f"Unknown forge host {host!r}. Declare it in "
            f"[tool.repomatic.projects] forges. Known hosts: {known}."
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
    :return: The repository's popularity and activity.
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
        release=dated,
        release_source=source,
        commit=branch["target"]["committedDate"][:10] if branch else None,
    )


def gitlab_metrics(host: str, path: str) -> ForgeMetrics | None:
    """Read a GitLab project, on whichever instance hosts it.

    :param host: The instance's hostname.
    :param path: The project's namespace path.
    :return: The project's popularity and activity, or `None` when unreadable.
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
        release=dated,
        release_source=source,
        commit=commits[0]["committed_date"][:10] if commits else None,
    )


def forgejo_metrics(host: str, path: str) -> ForgeMetrics | None:
    """Read a Forgejo or Gitea repository, on whichever instance hosts it.

    :param host: The instance's hostname.
    :param path: The repository's `owner/name` path.
    :return: The repository's popularity and activity, or `None` when unreadable.
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
    :return: The repository's popularity and activity, or `None` when the
        forge could not be read.
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


def load_project_records(path: Path) -> dict[str, ProjectRecord]:
    """Read the committed project readings, keyed by project ID.

    :param path: Path to the JSON readings file.
    :return: The records, empty when the file does not exist.
    :raises ValueError: When the file exists but cannot be parsed. Loud on
        purpose: a corrupt file must never be silently clobbered by the next
        {func}`save_project_records` write.
    """
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="UTF-8"))
        return {entry["id"]: ProjectRecord.from_dict(entry) for entry in data}
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
        msg = f"Malformed project readings file {path}: {error}"
        raise ValueError(msg) from error


def save_project_records(path: Path, records: Mapping[str, ProjectRecord]) -> bool:
    """Write the readings back, sorted by project ID and tab-indented.

    Serialized with the layout Biome's JSON formatter produces, so the
    `format-json` autofix job never rewrites it.

    :param path: Path to the JSON readings file.
    :param records: The records to write, keyed by project ID.
    :return: `True` when the file content changed.
    """
    ordered = [records[key].to_dict() for key in sorted(records)]
    content = json.dumps(ordered, indent="\t", sort_keys=True) + "\n"
    if path.exists() and path.read_text(encoding="UTF-8") == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="UTF-8")
    return True


def sample_projects(
    path: Path,
    repos: Mapping[str, str],
    extra_forges: Mapping[str, str] | None = None,
    sampled: str | None = None,
) -> list[SampleOutcome]:
    """Snapshot the popularity and activity of every tracked project.

    A repository that fails to answer keeps its previous reading rather than
    losing it: one flaky instance must not blank a column of whatever page
    renders these, and a stale figure carrying its own date is more useful
    than a hole. A project that left *repos* takes its reading with it.

    :param path: Path to the JSON readings file, created when missing.
    :param repos: Tracked projects, mapping each ID to its repository URL.
    :param extra_forges: Host-to-forge entries for self-hosted instances.
    :param sampled: Reading date in `YYYY-MM-DD` form. Today (UTC) when `None`.
    :return: One outcome per project, in ID order, plus one per dropped
        project carrying no metrics.
    """
    if sampled is None:
        sampled = datetime.now(tz=timezone.utc).date().isoformat()
    records = load_project_records(path)
    outcomes: list[SampleOutcome] = []

    for stale in sorted(set(records) - set(repos)):
        dropped = records.pop(stale)
        outcomes.append(
            SampleOutcome(
                stale, dropped.repo, error="dropped, no longer tracked", changed=True
            )
        )

    for project_id, url in sorted(repos.items()):
        try:
            metrics = repo_metrics(url, extra_forges)
        except (RuntimeError, ValueError, KeyError, IndexError, TypeError) as error:
            # Caught wide and per project: a repository gone private, a host
            # answering a payload of a shape nobody anticipated, or a forge
            # added without its API declared must cost one row, not every
            # other reading the run collected.
            outcomes.append(SampleOutcome(project_id, url, error=str(error)[:120]))
            continue
        if metrics is None:
            outcomes.append(SampleOutcome(project_id, url, error="unreadable"))
            continue
        record = ProjectRecord(project_id, url, sampled, metrics)
        previous = records.get(project_id)
        changed = previous is None or previous.reading != record.reading
        if changed:
            records[project_id] = record
        outcomes.append(SampleOutcome(project_id, url, metrics, changed=changed))

    if any(outcome.changed for outcome in outcomes):
        save_project_records(path, records)
    return outcomes
