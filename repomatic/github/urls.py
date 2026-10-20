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

"""Read a GitHub repository out of the forms that name it.

A repository reaches this package in several spellings: the `owner/name` slug
of `GITHUB_REPOSITORY`, an HTTPS remote, an SSH remote, and a `project_urls`
entry that points into the repository rather than at it. This module is the one
reader of all of them, so every consumer derives the same `(owner, name)` pair
from the same input.

A leaf module with no project-internal imports, so any module can import it
without the risk of an import cycle.
"""

from __future__ import annotations

import re

GITHUB_REPO_RE = re.compile(
    r"(?:^|[/@])(?:www\.)?github\.com[:/]+(?P<owner>[^/\s:?#]+)/(?P<name>[^/\s?#]+)"
)
"""A repository on `github.com`, in a URL or in a remote.

The host must start the string or follow a `/` (a URL scheme) or an `@` (an
SSH user), so a host that only ends in `github.com` does not match, and neither
does `api.github.com`, whose first path segment is not an owner. After the host
comes a `/` in a URL, or a `:` in an SSH remote such as
`git@github.com:owner/name.git`. The match stops after the name, so a deep link
like `https://github.com/owner/name/issues/1` reads as its repository.
"""


def parse_github_repo(url: str) -> tuple[str, str] | None:
    """Read the `(owner, name)` pair of the GitHub repository *url* points at.

    Accepts an HTTPS URL, an SSH remote (`git@github.com:owner/name.git` and
    `ssh://git@github.com/owner/name`), and a deep link into the repository
    (`https://github.com/owner/name/blob/main/readme.md`). A trailing `.git`,
    slash, query or fragment is dropped.

    :param url: A URL or a git remote.
    :return: The `(owner, name)` pair, or `None` when *url* names no repository
        on `github.com`: another host, or a URL with an owner and no name.
    """
    match = GITHUB_REPO_RE.search(url.strip())
    if match is None:
        return None
    name = match["name"].removesuffix(".git")
    if not name:
        return None
    return match["owner"], name


def split_slug(slug: str) -> tuple[str, str]:
    """Split an `owner/name` slug into its two halves.

    :param slug: A repository slug, like the value of `GITHUB_REPOSITORY`.
    :return: The `(owner, name)` pair. A slug with no `/` is all name, with an
        empty owner.
    """
    owner, _, name = slug.rpartition("/")
    return owner, name


def repo_name(slug: str) -> str:
    """The name half of an `owner/name` slug.

    It is also the package name an `owner/name` upstream implies: the inline
    `package==X.Y.Z` pins of a workflow name the package by its repository.

    :param slug: A repository slug.
    :return: The repository name. A slug with no `/` is returned whole.
    """
    return split_slug(slug)[1]
