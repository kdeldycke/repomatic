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
from typing import NamedTuple
from urllib.error import URLError
from urllib.request import Request, urlopen

GITHUB_API_RELEASES_URL = "https://api.github.com/repos/{owner}/{repo}/releases"
"""GitHub API URL for fetching all releases for a repository."""


class GitHubRelease(NamedTuple):
    """Release metadata for a single version from GitHub."""

    date: str
    """Publication date in ``YYYY-MM-DD`` format."""

    body: str
    """Release description body (markdown)."""


def get_github_releases(repo_url: str) -> dict[str, GitHubRelease]:
    """Get versions and dates for all GitHub releases.

    Fetches all releases via the GitHub API with pagination. Extracts
    version numbers by stripping the ``v`` prefix from tag names. Uses
    ``published_at`` (falling back to ``created_at``) for the date.

    :param repo_url: Repository URL (e.g.
        ``https://github.com/user/repo``).
    :return: Dict mapping version strings to :class:`GitHubRelease`
        tuples. Empty dict if the request fails.
    """
    # Parse owner/repo from the URL.
    parts = repo_url.rstrip("/").split("/")
    if len(parts) < 2:
        return {}
    owner, repo = parts[-2], parts[-1]

    result: dict[str, GitHubRelease] = {}
    page = 1
    while True:
        url = (
            GITHUB_API_RELEASES_URL.format(owner=owner, repo=repo)
            + f"?per_page=100&page={page}"
        )
        request = Request(url, headers={"Accept": "application/vnd.github+json"})  # noqa: S310
        try:
            with urlopen(request, timeout=10) as response:  # noqa: S310
                data = json.loads(response.read())
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            logging.debug(f"GitHub releases lookup failed: {exc}")
            break
        if not data:
            break
        for release in data:
            tag = release.get("tag_name", "")
            if tag.startswith("v"):
                version = tag[1:]
                # Prefer published_at, fall back to created_at.
                raw_date = release.get("published_at") or release.get("created_at", "")
                date = raw_date[:10] if raw_date else ""
                if date:
                    body = release.get("body", "")
                    result[version] = GitHubRelease(date=date, body=body)
        page += 1

    return result
