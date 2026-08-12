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

"""Check if a GitHub user is a sponsor of another user or organization.

Uses the GitHub GraphQL API via the `gh` CLI to query sponsorship data.
Supports both user and organization owners, with pagination for accounts
that have more than 100 sponsors.

When run in GitHub Actions, defaults are read from
{class}`~repomatic.metadata.Metadata` for owner and repository, and from
`GITHUB_EVENT_PATH` for the author and issue/PR number.
"""

from __future__ import annotations

import logging
from functools import lru_cache

from ..metadata import Metadata
from .actions import get_github_event
from .gh import iter_graphql_nodes
from .issue import add_labels

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Iterator
    from typing import Any


def get_default_owner() -> str | None:
    """Get the repository owner from CI context.

    Delegates to {attr}`Metadata.repo_owner
    <repomatic.metadata.Metadata.repo_owner>`.
    """
    owner = Metadata().repo_owner
    return owner if owner else None


def get_event_pull_request() -> dict[str, Any]:
    """Return the event payload's `pull_request` node, empty when absent.

    Truthiness, not key presence, is the test every reader below shares. A
    payload carrying `pull_request` as an empty object has no PR to act on,
    and it has to read that way to {func}`is_pull_request` as well as to the
    default lookups: testing `"pull_request" in event` here (as this module
    once did) let the two disagree, so `is_pull_request` reported a pull
    request while {func}`get_default_number` fell through to the issue branch.
    """
    return get_github_event().get("pull_request") or {}


def get_event_subject() -> dict[str, Any]:
    """Return the issue or pull request the current event is about.

    Pull requests win: the two nodes are mutually exclusive on the events this
    module handles, and preferring the PR keeps these lookups reading the same
    node {func}`is_pull_request` reports on.

    :return: The subject node, or an empty dict when the event carries neither.
    """
    return get_event_pull_request() or get_github_event().get("issue") or {}


def get_default_author() -> str | None:
    """Get the issue/PR author from the GitHub event payload."""
    login = get_event_subject().get("user", {}).get("login")
    return str(login) if login else None


def get_default_number() -> int | None:
    """Get the issue/PR number from the GitHub event payload."""
    number = get_event_subject().get("number")
    return int(number) if number else None


def is_pull_request() -> bool:
    """Check if the current event is a pull request."""
    return bool(get_event_pull_request())


# GraphQL query for user sponsors.
USER_SPONSORS_QUERY = """
query($owner: String!, $cursor: String) {
  user(login: $owner) {
    sponsorshipsAsMaintainer(first: 100, after: $cursor, includePrivate: true) {
      pageInfo { hasNextPage endCursor }
      nodes { sponsorEntity { ... on User { login } ... on Organization { login } } }
    }
  }
}
"""

# GraphQL query for organization sponsors.
ORG_SPONSORS_QUERY = """
query($owner: String!, $cursor: String) {
  organization(login: $owner) {
    sponsorshipsAsMaintainer(first: 100, after: $cursor, includePrivate: true) {
      pageInfo { hasNextPage endCursor }
      nodes { sponsorEntity { ... on User { login } ... on Organization { login } } }
    }
  }
}
"""


def _iter_sponsors(owner: str, query: str, data_path: str) -> Iterator[str]:
    """Iterate over all sponsors using pagination.

    :param owner: The owner (user or org) to query.
    :param query: The GraphQL query to use.
    :param data_path: Path to the data in the response (e.g., `"user"`).
    :yields: Login names of sponsors.
    """
    for node in iter_graphql_nodes(
        query,
        (data_path, "sponsorshipsAsMaintainer"),
        {"owner": owner},
    ):
        login = node.get("sponsorEntity", {}).get("login")
        if login:
            yield login


@lru_cache(maxsize=32)
def get_sponsors(owner: str) -> frozenset[str]:
    """Get all sponsors for a user or organization.

    Tries the user query first, then falls back to organization query.

    Results are cached to avoid redundant API calls within the same process.

    :param owner: The GitHub username or organization name.
    :return: Frozenset of sponsor login names.
    """
    sponsors: set[str] = set()

    # Try user query first.
    try:
        for login in _iter_sponsors(owner, USER_SPONSORS_QUERY, "user"):
            sponsors.add(login)
        logging.debug(f"Found {len(sponsors)} sponsors for user {owner}")
        return frozenset(sponsors)
    except RuntimeError:
        logging.debug(f"User query failed for {owner}, trying organization query")

    # Fall back to organization query.
    try:
        for login in _iter_sponsors(owner, ORG_SPONSORS_QUERY, "organization"):
            sponsors.add(login)
        logging.debug(f"Found {len(sponsors)} sponsors for organization {owner}")
    except RuntimeError:
        logging.debug(f"Organization query also failed for {owner}")

    return frozenset(sponsors)


def is_sponsor(owner: str, user: str) -> bool:
    """Check if a user is a sponsor of an owner.

    :param owner: The GitHub username or organization to check sponsorship for.
    :param user: The GitHub username to check if they are a sponsor.
    :return: True if user is a sponsor of owner, False otherwise.
    """
    sponsors = get_sponsors(owner)
    result = user in sponsors
    logging.info(f"User {user!r} {'is' if result else 'is not'} a sponsor of {owner!r}")
    return result


def add_sponsor_label(
    repo: str,
    number: int,
    label: str,
    is_pr: bool = False,
) -> bool:
    """Add the sponsor label to an issue or PR.

    A thin alias over {func}`~repomatic.github.issue.add_labels`, kept so this
    module's caller reads in its own vocabulary while both labellers share one
    mechanism.

    :param repo: The repository in "owner/repo" format.
    :param number: The issue or PR number.
    :param label: The label to add.
    :param is_pr: True if this is a PR, False for an issue.
    :return: True if label was added successfully, False otherwise.
    """
    return add_labels(repo, number, [label], is_pr=is_pr)
