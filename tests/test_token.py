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

import logging
from email.message import Message
from io import BytesIO
from unittest.mock import patch
from urllib.error import HTTPError

import pytest
from click_extra.testing import CliRunner

from repomatic.checks import CheckResult
from repomatic.cli.main import repomatic
from repomatic.github.gh import GhCommandError
from repomatic.github.token import (
    GITHUB_RATE_LIMIT_URL,
    PAT_PERMISSION_PROBES,
    PAT_SCOPE_PROBE,
    PAT_SCOPE_PROBE_LIMIT,
    PatPermissionResults,
    check_all_pat_permissions,
    check_pat_repository_scope,
    validate_classic_pat_scope,
    validate_gh_api_access,
    validate_notifications_token,
)
from tests.conftest import FakeResponse

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Mapping

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


@pytest.mark.parametrize(
    ("scopes", "warned"),
    (
        pytest.param(["notifications"], False, id="exact-scope"),
        pytest.param(["notifications", "repo"], True, id="extra-scopes"),
    ),
)
def test_validate_notifications_token_warns_on_extra_scopes(caplog, scopes, warned):
    """A token holding more than the `notifications` scope gets a warning."""
    with (
        patch("repomatic.github.token.validate_classic_pat_scope", return_value=scopes),
        caplog.at_level(logging.WARNING),
    ):
        validate_notifications_token()
    assert ("more scopes than needed" in caplog.text) is warned


def test_validate_notifications_token_propagates():
    """A validation failure propagates to the CLI entry point."""
    with (
        patch(
            "repomatic.github.token.validate_classic_pat_scope",
            side_effect=RuntimeError("bad scope"),
        ),
        pytest.raises(RuntimeError, match="bad scope"),
    ):
        validate_notifications_token()


def test_unsubscribe_threads_runs_the_notifications_validator():
    """`unsubscribe-threads` stops on the validator of this module, and says why."""
    with (
        patch(
            "repomatic.github.token.validate_notifications_token",
            side_effect=RuntimeError("bad scope"),
        ),
        patch("repomatic.cli.github.unsubscribe_threads") as unsubscribe,
    ):
        result = CliRunner().invoke(repomatic, ["unsubscribe-threads", "--dry-run"])

    assert result.exit_code == 1
    assert "bad scope" in result.output
    unsubscribe.assert_not_called()


@pytest.mark.parametrize("probe", PAT_PERMISSION_PROBES, ids=lambda p: p.field)
def test_pat_permission_probe_pass(probe):
    """Pass with the probe's success message when the API call succeeds."""
    with patch("repomatic.github.token.run_gh_command") as mock_gh:
        mock_gh.return_value = ""
        passed, msg = probe.run("owner/repo")
        assert passed is True
        assert msg == probe.success
        probed_endpoint = mock_gh.call_args.args[0][1]
        assert probed_endpoint == probe.endpoint.format(repo="owner/repo")


@pytest.mark.parametrize("probe", PAT_PERMISSION_PROBES, ids=lambda p: p.field)
def test_pat_permission_probe_fail_403(probe):
    """Fail with the missing-permission message when the API call 403s."""
    with patch("repomatic.github.token.run_gh_command") as mock_gh:
        mock_gh.side_effect = GhCommandError("HTTP 403: Forbidden")
        passed, msg = probe.run("owner/repo")
        assert passed is False
        assert probe.permission in msg
        assert "Update the PAT" in msg


def test_pat_vulnerability_alerts_permission_404():
    """A 404 is reported as 'alerts not enabled', not as a missing permission."""
    vuln_probe = next(
        probe
        for probe in PAT_PERMISSION_PROBES
        if probe.field == "vulnerability_alerts"
    )
    with patch("repomatic.github.token.run_gh_command") as mock_gh:
        mock_gh.side_effect = GhCommandError("HTTP 404: Not Found")
        passed, msg = vuln_probe.run("owner/repo")
        assert passed is False
        assert "not enabled" in msg
        assert "--method PUT" in msg


def test_pat_check_non_403_surfaces_raw_error():
    """A 401 from an upstream incident surfaces the raw stderr, not a scope hint."""
    with patch("repomatic.github.token.run_gh_command") as mock_gh:
        mock_gh.side_effect = GhCommandError("HTTP 401: Requires authentication")
        passed, msg = PAT_PERMISSION_PROBES[0].run("owner/repo")
        assert passed is False
        assert "GitHub API call failed" in msg
        assert "401" in msg
        assert "Update the PAT" not in msg


def test_pat_check_annotates_with_github_status_incident():
    """An active githubstatus.com incident is appended to the failure message."""
    with (
        patch("repomatic.github.token.run_gh_command") as mock_gh,
        patch(
            "repomatic.github.status.status_annotation",
            return_value="GitHub Status reports an active incident (major): "
            "Partial System Outage. See https://www.githubstatus.com for details.",
        ),
    ):
        mock_gh.side_effect = GhCommandError("HTTP 401: Requires authentication")
        passed, msg = PAT_PERMISSION_PROBES[0].run("owner/repo")
        assert passed is False
        assert "active incident" in msg
        assert "githubstatus.com" in msg


@pytest.mark.parametrize(
    ("probe", "name"),
    (
        pytest.param(PAT_PERMISSION_PROBES[0], "Administration", id="one-word"),
        pytest.param(PAT_PERMISSION_PROBES[3], "Pull requests", id="two-words"),
    ),
)
def test_pat_probe_name_drops_the_access_level(probe, name):
    """A probe's name is its permission as GitHub's PAT form labels it."""
    assert probe.name == name


def test_pat_permission_results_map_each_probe_field():
    """Every probe answers with the shared result type, under its own field."""
    with patch("repomatic.github.token.run_gh_command", return_value=""):
        results = check_all_pat_permissions("orchard/basket")
    assert list(results) == [probe.field for probe in PAT_PERMISSION_PROBES]
    assert all(isinstance(result, CheckResult) for result in results.values())
    assert results.failed() == []


def test_pat_permission_results_name_the_failed_fields():
    """`failed()` pairs each failing field with its message, in probe order."""
    results = PatPermissionResults(
        contents=CheckResult(True, "Contents: token has access"),
        issues=CheckResult(False, "Token lacks issues."),
        workflows=CheckResult(False, "Token lacks workflows."),
    )
    assert results.failed() == [
        ("issues", "Token lacks issues."),
        ("workflows", "Token lacks workflows."),
    ]


REFUSED = "Resource not accessible by personal access token (HTTP 403)"
"""What `gh` reports when a token asks a repository it does not cover."""


def _gh_api_stand_in(args: list[str], answers: Mapping[str, str | None]) -> str:
    """Answer the scope check's `gh api` calls, method switch included.

    `gh api` sends a `POST` as soon as a field is passed, unless `--method` says
    otherwise, and GitHub answers a `POST` on a listing endpoint with a 404. The
    stand-in keeps that rule, so the listing succeeds only when its argv asks for
    a `GET`.

    :param args: Arguments the check hands to `gh`.
    :param answers: The owner's repositories, in the order the listing returns
        them. Each maps to the `gh` error its Actions settings raise, or to
        `None` where the token reads them.
    :return: The output `gh` would print.
    """
    if "/users/orchard/repos" in args:
        if "--method" in args:
            sends_get = args[args.index("--method") + 1] == "GET"
        else:
            sends_get = not {"--raw-field", "--field"} & set(args)
        if not sends_get:
            raise GhCommandError("Not Found (HTTP 404)")
        return "".join(f"{name}\n" for name in answers)
    # GitHub resolves a repository name whatever its case.
    settings = {
        PAT_SCOPE_PROBE.endpoint.format(repo=name).casefold(): error
        for name, error in answers.items()
    }
    if error := settings[args[1].casefold()]:
        raise GhCommandError(error)
    return ""


def test_pat_repository_scope_probes_an_endpoint_only_its_token_reads():
    """The probe is the Administration row, and no other row can replace it.

    A public repository answers the contents, issues, pull requests and
    workflows endpoints to any caller, so a 2xx from one of them on another
    repository says nothing about the token.
    """
    assert PAT_SCOPE_PROBE.field == "administration"
    assert PAT_SCOPE_PROBE in PAT_PERMISSION_PROBES


@pytest.mark.parametrize(
    ("answers", "passed", "fragment"),
    (
        pytest.param(
            {"orchard/apricot": None, "orchard/cherry": None},
            False,
            "PAT also has access to orchard/cherry",
            id="another-repository-answers",
        ),
        pytest.param(
            {"orchard/apricot": None, "orchard/cherry": REFUSED, "orchard/plum": None},
            False,
            "PAT also has access to orchard/plum",
            id="a-later-repository-answers",
        ),
        pytest.param(
            {
                "orchard/apricot": None,
                "orchard/cherry": REFUSED,
                "orchard/plum": REFUSED,
            },
            True,
            "no access to other repositories (2 probed)",
            id="every-probe-refused",
        ),
        pytest.param(
            {
                "orchard/apricot": None,
                "orchard/cherry": "Bad Gateway (HTTP 502)",
                "orchard/plum": REFUSED,
            },
            True,
            "no access to other repositories (1 probed)",
            id="one-refusal-beside-an-unclear-answer",
        ),
        pytest.param(
            {
                "orchard/apricot": None,
                "orchard/cherry": "Not Found (HTTP 404)",
                "orchard/plum": "Bad Gateway (HTTP 502)",
            },
            None,
            "no probe got a clear answer",
            id="no-clear-answer",
        ),
        pytest.param(
            {"orchard/apricot": REFUSED, "orchard/cherry": None},
            None,
            "no 'Administration: Read-only' access to probe with",
            id="token-cannot-probe",
        ),
        pytest.param(
            {"orchard/apricot": None},
            None,
            "no other repos to probe",
            id="no-other-repository",
        ),
        pytest.param(
            {"Orchard/Apricot": None},
            None,
            "no other repos to probe",
            id="own-name-in-another-case",
        ),
    ),
)
def test_pat_repository_scope_probes_other_repositories(answers, passed, fragment):
    """The probes of other repositories decide the scope.

    Only a repository that answers fails the check: a refusal passes it, and
    anything else leaves it undecided. The repository under check is never one
    of the others, however the listing spells its name.
    """
    with patch("repomatic.github.token.run_gh_command") as mock_gh:
        mock_gh.side_effect = lambda args: _gh_api_stand_in(args, answers)
        result = check_pat_repository_scope("orchard/apricot")
    assert result.passed is passed
    assert fragment in result.message


def test_pat_repository_scope_probes_the_most_recently_pushed_repositories():
    """The listing is sorted by push date, and the probes stop at the limit."""
    answers: dict[str, str | None] = {"orchard/apricot": None}
    answers.update({
        f"orchard/tree-{index}": REFUSED for index in range(PAT_SCOPE_PROBE_LIMIT + 5)
    })
    with patch("repomatic.github.token.run_gh_command") as mock_gh:
        mock_gh.side_effect = lambda args: _gh_api_stand_in(args, answers)
        result = check_pat_repository_scope("orchard/apricot")
    assert result.passed is True
    assert f"({PAT_SCOPE_PROBE_LIMIT} probed)" in result.message
    calls = [call.args[0] for call in mock_gh.call_args_list]
    listing = next(args for args in calls if "/users/orchard/repos" in args)
    assert "sort=pushed" in listing
    # One page holds this repository and a full quota of others.
    assert f"per_page={PAT_SCOPE_PROBE_LIMIT + 1}" in listing
    probed = [args[1] for args in calls if args[1].startswith("repos/")]
    # This repository first, then the others.
    assert len(probed) == 1 + PAT_SCOPE_PROBE_LIMIT
    # The installation endpoint refuses a personal token, so nothing asks it.
    assert not any("/installation/repositories" in args for args in calls)


def test_pat_repository_scope_skips_an_unreadable_listing():
    """A listing that fails leaves the check undecided."""

    def gh(args: list[str]) -> str:
        if "/users/orchard/repos" in args:
            raise GhCommandError("Bad Gateway (HTTP 502)")
        return ""

    with patch("repomatic.github.token.run_gh_command", side_effect=gh):
        result = check_pat_repository_scope("orchard/apricot")
    assert result.passed is None
    assert "could not list owner repos" in result.message
