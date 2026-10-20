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

"""Tests for :mod:`repomatic.github.urls`."""

from __future__ import annotations

import pytest

from repomatic.github.urls import parse_github_repo, repo_name, split_slug


@pytest.mark.parametrize(
    ("url", "expected"),
    (
        pytest.param("https://github.com/papaya/kiwi", ("papaya", "kiwi"), id="https"),
        pytest.param("https://github.com/papaya/kiwi/", ("papaya", "kiwi"), id="slash"),
        pytest.param(
            "https://github.com/papaya/kiwi.git", ("papaya", "kiwi"), id="dot-git"
        ),
        pytest.param("http://github.com/papaya/kiwi", ("papaya", "kiwi"), id="http"),
        pytest.param(
            "https://www.github.com/papaya/kiwi", ("papaya", "kiwi"), id="www"
        ),
        pytest.param(
            "git@github.com:papaya/kiwi.git", ("papaya", "kiwi"), id="ssh-scp"
        ),
        pytest.param(
            "ssh://git@github.com/papaya/kiwi.git", ("papaya", "kiwi"), id="ssh-url"
        ),
        pytest.param(
            "git+https://github.com/papaya/kiwi", ("papaya", "kiwi"), id="vcs-url"
        ),
        pytest.param(
            "  https://github.com/papaya/kiwi\n", ("papaya", "kiwi"), id="padded"
        ),
        # A deep link reads as the repository it points into.
        pytest.param(
            "https://github.com/papaya/kiwi/issues/1",
            ("papaya", "kiwi"),
            id="issue",
        ),
        pytest.param(
            "https://github.com/papaya/kiwi/blob/main/changelog.md",
            ("papaya", "kiwi"),
            id="blob",
        ),
        pytest.param(
            "https://github.com/papaya/kiwi#readme", ("papaya", "kiwi"), id="fragment"
        ),
        pytest.param(
            "https://github.com/papaya/kiwi?tab=readme", ("papaya", "kiwi"), id="query"
        ),
        # A name that reads like a number stays the string it is.
        pytest.param("https://github.com/papaya/2048", ("papaya", "2048"), id="digits"),
        # No repository to point at.
        pytest.param("https://github.com/papaya", None, id="owner-only"),
        pytest.param("https://github.com/", None, id="bare-host"),
        pytest.param("https://gitlab.com/papaya/kiwi", None, id="other-forge"),
        pytest.param("https://notgithub.com/papaya/kiwi", None, id="lookalike-host"),
        pytest.param("https://api.github.com/repos/papaya/kiwi", None, id="api-host"),
        pytest.param("papaya/kiwi", None, id="bare-slug"),
        pytest.param("", None, id="empty"),
    ),
)
def test_parse_github_repo(url, expected):
    """Every spelling of a GitHub repository reads as the same pair."""
    assert parse_github_repo(url) == expected


@pytest.mark.parametrize(
    ("slug", "owner", "name"),
    (
        pytest.param("papaya/kiwi", "papaya", "kiwi", id="slug"),
        pytest.param("kiwi", "", "kiwi", id="name-only"),
    ),
)
def test_split_slug(slug, owner, name):
    """A slug splits into its halves, and a bare name is all name."""
    assert split_slug(slug) == (owner, name)
    assert repo_name(slug) == name
