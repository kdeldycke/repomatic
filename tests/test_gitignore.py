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

"""Tests for `.gitignore` generation from gitignore.io templates."""

from __future__ import annotations

from unittest.mock import patch
from urllib.error import URLError

import pytest

from repomatic.config import Config, GitignoreConfig
from repomatic.gitignore import (
    GITIGNORE_BASE_CATEGORIES,
    GITIGNORE_IO_URL,
    build_gitignore,
)
from tests.conftest import FakeResponse


def _config(extra_categories: tuple[str, ...] = (), extra_content: str = "") -> Config:
    """Build a Config carrying just the `gitignore` fields under test."""
    return Config(
        gitignore=GitignoreConfig(
            extra_categories=list(extra_categories),
            extra_content=extra_content,
        ),
    )


def test_build_gitignore_requests_base_categories():
    """The base categories are fetched and the response body is returned."""
    body = b"# managed by repomatic\n*.log\n"
    with patch(
        "repomatic.gitignore.urlopen", return_value=FakeResponse(body)
    ) as mock_urlopen:
        content = build_gitignore(_config())

    assert content == body.decode("UTF-8")
    request = mock_urlopen.call_args.args[0]
    expected_url = f"{GITIGNORE_IO_URL}/{','.join(GITIGNORE_BASE_CATEGORIES)}"
    assert request.full_url == expected_url
    # The socket timeout is part of the contract: a stalled fetch must fail,
    # not hang the sync.
    assert mock_urlopen.call_args.kwargs["timeout"] == 10
    assert request.get_header("User-agent").startswith("repomatic/")


def test_build_gitignore_merges_and_dedupes_extra_categories():
    """Extra categories append in order, and duplicates of a base one drop."""
    # "python" is already a base category (deduplicated); "papaya" is new.
    config = _config(extra_categories=("python", "papaya"))
    with patch(
        "repomatic.gitignore.urlopen", return_value=FakeResponse(b"content")
    ) as mock_urlopen:
        build_gitignore(config)

    request = mock_urlopen.call_args.args[0]
    expected_categories = [*GITIGNORE_BASE_CATEGORIES, "papaya"]
    assert request.full_url == f"{GITIGNORE_IO_URL}/{','.join(expected_categories)}"


def test_build_gitignore_appends_extra_content():
    """Configured extra content is appended after the fetched template."""
    config = _config(extra_content="# custom\n*.tmp")
    with patch("repomatic.gitignore.urlopen", return_value=FakeResponse(b"base\n")):
        content = build_gitignore(config)

    assert content == "base\n\n# custom\n*.tmp\n"


def test_build_gitignore_propagates_fetch_error():
    """A failed gitignore.io fetch surfaces to the caller, not silently empty."""
    with (
        patch("repomatic.gitignore.urlopen", side_effect=URLError("unreachable")),
        pytest.raises(URLError),
    ):
        build_gitignore(_config())
