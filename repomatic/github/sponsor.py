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

Uses the GitHub GraphQL API via the `gh` CLI to query sponsorship data, with
pagination for accounts that have more than 100 sponsors.
"""

from __future__ import annotations

import logging

from .gh import iter_graphql_nodes

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Iterator


SPONSORS_QUERY = """
query($owner: String!, $cursor: String) {
  repositoryOwner(login: $owner) {
    ... on Sponsorable {
      sponsorshipsAsMaintainer(first: 100, after: $cursor, includePrivate: true) {
        pageInfo { hasNextPage endCursor }
        nodes { sponsorEntity { ... on User { login } ... on Organization { login } } }
      }
    }
  }
}
"""
"""GraphQL query for an account's sponsors, a user or an organization alike.

`repositoryOwner` resolves both kinds of account, and both implement the
`Sponsorable` interface, so one query covers either without a probe for the
kind first.
"""


def iter_sponsors(owner: str) -> Iterator[str]:
    """Iterate over every sponsor of *owner*, following pagination.

    :param owner: The user or organization to query.
    :yields: Login names of sponsors.
    :raises GhCommandError: When a `gh` invocation fails.
    """
    for node in iter_graphql_nodes(
        SPONSORS_QUERY,
        ("repositoryOwner", "sponsorshipsAsMaintainer"),
        {"owner": owner},
    ):
        login = (node.get("sponsorEntity") or {}).get("login")
        if login:
            yield login


def is_sponsor(owner: str, user: str) -> bool:
    """Check if a user is a sponsor of an owner.

    A failed query reads as "not a sponsor": the label it gates is a
    convenience, so an API outage must not fail the job that asks.

    :param owner: The GitHub username or organization to check sponsorship for.
    :param user: The GitHub username to check if they are a sponsor.
    :return: True if user is a sponsor of owner, False otherwise.
    """
    try:
        result = user in iter_sponsors(owner)
    except RuntimeError as error:
        logging.warning(f"Could not read the sponsors of {owner!r}: {error}")
        return False
    logging.info(f"User {user!r} {'is' if result else 'is not'} a sponsor of {owner!r}")
    return result
