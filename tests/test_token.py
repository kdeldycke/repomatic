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

"""Tests for :mod:`repomatic.github.token`."""

from __future__ import annotations

from email.message import Message
from io import BytesIO
from unittest.mock import patch
from urllib.error import HTTPError

import pytest

from repomatic.checks import CheckResult
from repomatic.github.token import (
    GITHUB_RATE_LIMIT_URL,
    PAT_PERMISSION_PROBES,
    check_all_pat_permissions,
    validate_classic_pat_scope,
    validate_gh_api_access,
)
from tests.conftest import FakeResponse

_TOKEN_ENV = {"PATH": "/usr/bin", "GH_TOKEN": "fig-token"}
"""An environment holding one token, and nothing else a lookup could find."""


def test_validate_gh_api_access_returns_the_headers():
    """The headers are the one part of the answer the scope check reads."""
    response = FakeResponse(b"{}", headers={"X-OAuth-Scopes": "notifications, repo"})
    with (
        patch.dict("os.environ", _TOKEN_ENV, clear=True),
        patch("repomatic.http.urlopen", return_value=response) as mock_open,
    ):
        assert validate_gh_api_access() == {"x-oauth-scopes": "notifications, repo"}
    sent = mock_open.call_args.args[0]
    assert sent.full_url == GITHUB_RATE_LIMIT_URL
    assert sent.get_header("Authorization") == "Bearer fig-token"


@pytest.mark.parametrize(
    ("body", "fragment"),
    (
        pytest.param(
            b'{"message": "Bad credentials"}',
            "GitHub says: Bad credentials",
            id="api-message",
        ),
        pytest.param(b"<html>gateway</html>", "returned an error (401).", id="no-json"),
    ),
)
def test_validate_gh_api_access_names_the_refusal(body, fragment):
    """An error status names the status, and the API's own message when it has one."""
    error = HTTPError(
        GITHUB_RATE_LIMIT_URL, 401, "Unauthorized", Message(), BytesIO(body)
    )
    with (
        patch.dict("os.environ", _TOKEN_ENV, clear=True),
        patch("repomatic.http.urlopen", side_effect=error),
        pytest.raises(RuntimeError, match=r"\(401\)") as excinfo,
    ):
        validate_gh_api_access()
    assert fragment in str(excinfo.value)


def test_validate_gh_api_access_names_an_unreachable_api():
    """A network failure says the API could not be reached at all."""
    with (
        patch.dict("os.environ", _TOKEN_ENV, clear=True),
        pytest.raises(RuntimeError, match="Could not reach the GitHub API"),
    ):
        validate_gh_api_access()


@pytest.mark.parametrize(
    ("headers", "error"),
    (
        pytest.param({"X-OAuth-Scopes": "notifications, repo"}, None, id="classic"),
        pytest.param({}, "must be a classic PAT", id="fine-grained"),
        pytest.param({"X-OAuth-Scopes": "repo"}, "scope is required", id="no-scope"),
    ),
)
def test_validate_classic_pat_scope(headers, error):
    """Only a classic token holding the scope passes."""
    response = FakeResponse(b"{}", headers=headers)
    with (
        patch.dict("os.environ", _TOKEN_ENV, clear=True),
        patch("repomatic.http.urlopen", return_value=response),
    ):
        if error is None:
            assert validate_classic_pat_scope("notifications") == [
                "notifications",
                "repo",
            ]
        else:
            with pytest.raises(RuntimeError, match=error):
                validate_classic_pat_scope("notifications")


def test_pat_permission_results_are_check_results():
    """Every probe answers with the shared result type."""
    with patch("repomatic.github.token.run_gh_command", return_value=""):
        results = check_all_pat_permissions("orchard/basket")
    assert all(isinstance(result, CheckResult) for result in results.iter_results())
    assert len(results.iter_results()) == len(PAT_PERMISSION_PROBES)
    assert results.failed() == []
