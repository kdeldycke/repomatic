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

When run in GitHub Actions, the owner defaults to
{class}`~repomatic.metadata.core.Metadata`'s view of the repository; the author
and issue/PR number come from the event-payload readers in
{mod}`~repomatic.github.actions`.
"""

from __future__ import annotations

import logging
from functools import lru_cache

from ..metadata.core import Metadata
from .gh import iter_graphql_nodes

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Iterator


def get_default_owner() -> str | None:
    """Get the repository owner from CI context.

    Delegates to {attr}`Metadata.repo_owner
    <repomatic.metadata.env.EnvironmentMetadata.repo_owner>`.
    """
    owner = Metadata().repo_owner
    return owner if owner else None


SPONSORS_QUERY_TEMPLATE = """
query($owner: String!, $cursor: String) {
  %s(login: $owner) {
    sponsorshipsAsMaintainer(first: 100, after: $cursor, includePrivate: true) {
      pageInfo { hasNextPage endCursor }
      nodes { sponsorEntity { ... on User { login } ... on Organization { login } } }
    }
  }
}
"""
"""GraphQL query for an account's sponsors, parameterized on the account kind.

The user and organization queries are identical except for the node naming the
account (`user` or `organization`), which doubles as the response's data path:
{func}`_iter_sponsors` interpolates it into both places.
"""


def _iter_sponsors(owner: str, kind: str) -> Iterator[str]:
    """Iterate over all sponsors using pagination.

    :param owner: The owner (user or org) to query.
    :param kind: The GraphQL node naming the account, `user` or `organization`.
    :yields: Login names of sponsors.
    """
    for node in iter_graphql_nodes(
        SPONSORS_QUERY_TEMPLATE % kind,
        (kind, "sponsorshipsAsMaintainer"),
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
        for login in _iter_sponsors(owner, "user"):
            sponsors.add(login)
        logging.debug(f"Found {len(sponsors)} sponsors for user {owner}")
        return frozenset(sponsors)
    except RuntimeError:
        logging.debug(f"User query failed for {owner}, trying organization query")

    # Fall back to organization query.
    try:
        for login in _iter_sponsors(owner, "organization"):
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
