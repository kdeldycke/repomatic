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

"""Tests for :mod:`repomatic.github.gh`."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

import pytest

from repomatic.github.gh import (
    GhCommandError,
    GhOutputError,
    _graphql_args,
    api_headers,
    gh_api_pages,
    gh_executable,
    gh_graphql,
    gh_json,
    iter_graphql_nodes,
    parse_create_output,
    run_gh_command,
)
from repomatic.tooling.registry import BinaryInstallError


@pytest.fixture(autouse=True)
def _no_transient_retry():
    """Disable the same-token transient-401 retry loop by default.

    The retry loop sleeps before each attempt, which would slow every test
    that exercises a 401 path.  Tests that target the retry behavior
    override this fixture to set the schedule themselves.
    """
    with (
        patch("repomatic.github.gh._TRANSIENT_AUTH_BACKOFF_SECONDS", ()),
        patch("repomatic.github.gh._TRANSIENT_THROTTLE_BACKOFF_SECONDS", ()),
        patch("repomatic.github.gh.time.sleep"),
    ):
        yield


def _make_result(
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> CompletedProcess[str]:
    return CompletedProcess(
        args=["gh"], returncode=returncode, stdout=stdout, stderr=stderr
    )


# Minimal env: no token vars at all.  Tests that need specific vars add them.
_CLEAN_ENV = {"PATH": "/usr/bin", "HOME": "/tmp"}


# -- api_headers -------------------------------------------------------------


@pytest.mark.parametrize(
    ("env", "expected_token"),
    (
        ({"REPOMATIC_PAT": "pat", "GH_TOKEN": "gh", "GITHUB_TOKEN": "gt"}, "pat"),
        ({"GH_TOKEN": "gh", "GITHUB_TOKEN": "gt"}, "gh"),
        ({"GITHUB_TOKEN": "gt"}, "gt"),
    ),
)
def test_api_headers_follows_token_precedence(env, expected_token):
    """Direct API calls authenticate with the same winner the gh CLI gets."""
    with patch.dict("os.environ", {**_CLEAN_ENV, **env}, clear=True):
        assert api_headers() == {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {expected_token}",
        }


def test_api_headers_unauthenticated_without_token():
    """No token means no Authorization header, not an empty one."""
    with patch.dict("os.environ", _CLEAN_ENV, clear=True):
        assert api_headers() == {"Accept": "application/vnd.github+json"}


# -- Token resolution: REPOMATIC_PAT > GH_TOKEN > GITHUB_TOKEN ----------------


def test_repomatic_pat_injected_as_gh_token():
    """REPOMATIC_PAT is passed to gh as GH_TOKEN."""
    with (
        patch("repomatic.github.gh.run") as mock_run,
        patch.dict(
            "os.environ",
            {**_CLEAN_ENV, "REPOMATIC_PAT": "pat-value"},
            clear=True,
        ),
    ):
        mock_run.return_value = _make_result(stdout="ok\n")
        assert run_gh_command(["issue", "list"]) == "ok\n"
        env_used = mock_run.call_args.kwargs["env"]
        assert env_used["GH_TOKEN"] == "pat-value"


def test_gh_token_used_when_no_repomatic_pat():
    """Falls back to native GH_TOKEN when REPOMATIC_PAT is absent."""
    with (
        patch("repomatic.github.gh.run") as mock_run,
        patch.dict(
            "os.environ",
            {**_CLEAN_ENV, "GH_TOKEN": "gh-tok"},
            clear=True,
        ),
    ):
        mock_run.return_value = _make_result(stdout="ok\n")
        assert run_gh_command(["issue", "list"]) == "ok\n"
        # The canonical resolve_gh_token() winner is injected explicitly,
        # value-identical to the native GH_TOKEN.
        env_used = mock_run.call_args.kwargs["env"]
        assert env_used["GH_TOKEN"] == "gh-tok"


def test_github_token_promoted_to_gh_token():
    """GITHUB_TOKEN is promoted to GH_TOKEN when GH_TOKEN is absent."""
    with (
        patch("repomatic.github.gh.run") as mock_run,
        patch.dict(
            "os.environ",
            {**_CLEAN_ENV, "GITHUB_TOKEN": "gha-tok"},
            clear=True,
        ),
    ):
        mock_run.return_value = _make_result(stdout="ok\n")
        assert run_gh_command(["issue", "list"]) == "ok\n"
        env_used = mock_run.call_args.kwargs["env"]
        assert env_used["GH_TOKEN"] == "gha-tok"


def test_empty_repomatic_pat_ignored():
    """Empty REPOMATIC_PAT (unconfigured secret) is treated as absent."""
    with (
        patch("repomatic.github.gh.run") as mock_run,
        patch.dict(
            "os.environ",
            {**_CLEAN_ENV, "REPOMATIC_PAT": "", "GH_TOKEN": "gh-tok"},
            clear=True,
        ),
    ):
        mock_run.return_value = _make_result(stdout="ok\n")
        run_gh_command(["issue", "list"])
        # The empty PAT loses to GH_TOKEN, whose value is injected explicitly.
        env_used = mock_run.call_args.kwargs["env"]
        assert env_used["GH_TOKEN"] == "gh-tok"


# -- Fallback on 401 Bad Credentials ------------------------------------------


def test_success_no_fallback():
    """Successful commands never trigger fallback."""
    with patch("repomatic.github.gh.run") as mock_run:
        mock_run.return_value = _make_result(stdout="ok\n")
        assert run_gh_command(["issue", "list"]) == "ok\n"
        assert mock_run.call_count == 1


def test_non_401_error_no_fallback():
    """Non-401 errors raise immediately without retry."""
    with (
        patch("repomatic.github.gh.run") as mock_run,
        patch.dict("os.environ", {"GH_TOKEN": "pat", "GITHUB_TOKEN": "gha"}),
    ):
        mock_run.return_value = _make_result(returncode=1, stderr="not found")
        with pytest.raises(RuntimeError, match="not found"):
            run_gh_command(["issue", "list"])
        assert mock_run.call_count == 1


@pytest.mark.parametrize(
    ("env", "clear"),
    [
        pytest.param(
            {"GH_TOKEN": "same-token", "GITHUB_TOKEN": "same-token"},
            False,
            id="tokens-identical",
        ),
        pytest.param(
            {**_CLEAN_ENV, "GH_TOKEN": "expired-pat"},
            True,
            id="github-token-missing",
        ),
    ],
)
def test_401_no_fallback(env, clear):
    """401 does not retry when fallback is unavailable."""
    with (
        patch("repomatic.github.gh.run") as mock_run,
        patch.dict("os.environ", env, clear=clear),
    ):
        mock_run.return_value = _make_result(
            returncode=1,
            stderr="Bad credentials",
        )
        with pytest.raises(RuntimeError, match="Bad credentials"):
            run_gh_command(["issue", "list"])
        assert mock_run.call_count == 1


def test_401_retries_with_github_token():
    """401 Bad credentials triggers retry with GITHUB_TOKEN."""
    with (
        patch("repomatic.github.gh.run") as mock_run,
        patch.dict(
            "os.environ",
            {"GH_TOKEN": "expired-pat", "GITHUB_TOKEN": "gha-token"},
        ),
    ):
        mock_run.side_effect = [
            _make_result(returncode=1, stderr="Bad credentials"),
            _make_result(stdout="fallback ok\n"),
        ]
        result = run_gh_command(["issue", "list"])
        assert result == "fallback ok\n"
        assert mock_run.call_count == 2
        retry_env = mock_run.call_args_list[1].kwargs["env"]
        assert retry_env["GH_TOKEN"] == "gha-token"


def test_401_requires_authentication_retries_with_github_token():
    """401 Requires authentication also triggers the GITHUB_TOKEN fallback.

    Surfaces during GitHub auth incidents and from fine-grained PAT scope
    mismatches that GitHub treats as anonymous for the targeted resource.
    """
    with (
        patch("repomatic.github.gh.run") as mock_run,
        patch.dict(
            "os.environ",
            {"GH_TOKEN": "scoped-pat", "GITHUB_TOKEN": "gha-token"},
        ),
    ):
        mock_run.side_effect = [
            _make_result(
                returncode=1,
                stderr=(
                    "non-200 OK status code: 401 Unauthorized "
                    'body: "{ \\"message\\": \\"Requires authentication\\" }"'
                ),
            ),
            _make_result(stdout="fallback ok\n"),
        ]
        result = run_gh_command(["issue", "list"])
        assert result == "fallback ok\n"
        assert mock_run.call_count == 2
        retry_env = mock_run.call_args_list[1].kwargs["env"]
        assert retry_env["GH_TOKEN"] == "gha-token"


def test_repomatic_pat_401_falls_back_to_github_token():
    """Expired REPOMATIC_PAT falls back to GITHUB_TOKEN."""
    with (
        patch("repomatic.github.gh.run") as mock_run,
        patch.dict(
            "os.environ",
            {**_CLEAN_ENV, "REPOMATIC_PAT": "expired-pat", "GITHUB_TOKEN": "gha-token"},
            clear=True,
        ),
    ):
        mock_run.side_effect = [
            _make_result(returncode=1, stderr="Bad credentials"),
            _make_result(stdout="fallback ok\n"),
        ]
        result = run_gh_command(["issue", "list"])
        assert result == "fallback ok\n"
        assert mock_run.call_count == 2
        # First call uses REPOMATIC_PAT.
        first_env = mock_run.call_args_list[0].kwargs["env"]
        assert first_env["GH_TOKEN"] == "expired-pat"
        # Second call uses GITHUB_TOKEN.
        retry_env = mock_run.call_args_list[1].kwargs["env"]
        assert retry_env["GH_TOKEN"] == "gha-token"


def test_401_fallback_also_fails():
    """When fallback also fails, original 401 error is raised."""
    with (
        patch("repomatic.github.gh.run") as mock_run,
        patch.dict(
            "os.environ",
            {"GH_TOKEN": "expired-pat", "GITHUB_TOKEN": "gha-token"},
        ),
    ):
        mock_run.side_effect = [
            _make_result(returncode=1, stderr="Bad credentials"),
            _make_result(returncode=1, stderr="Resource not accessible"),
        ]
        with pytest.raises(RuntimeError, match="Bad credentials"):
            run_gh_command(["issue", "list"])
        assert mock_run.call_count == 2


def test_failure_message_includes_github_status_annotation():
    """A live githubstatus.com incident is appended to the raised RuntimeError."""
    annotation = (
        "GitHub Status reports an active incident (critical): "
        "Major Service Outage. See https://www.githubstatus.com for details."
    )
    with (
        patch("repomatic.github.gh.run") as mock_run,
        patch("repomatic.github.status.status_annotation", return_value=annotation),
    ):
        mock_run.return_value = _make_result(returncode=1, stderr="some error")
        with pytest.raises(RuntimeError) as excinfo:
            run_gh_command(["issue", "list"])
        assert "some error" in str(excinfo.value)
        assert "active incident" in str(excinfo.value)
        assert "githubstatus.com" in str(excinfo.value)


@pytest.mark.parametrize(
    ("stderr", "status"),
    (
        pytest.param(
            "HTTP 404: Not Found (https://api.github.com/x)", 404, id="leading"
        ),
        pytest.param("Resource not accessible (HTTP 403)", 403, id="trailing"),
        pytest.param("dial tcp: lookup api.github.com: no such host", None, id="none"),
    ),
)
def test_failure_carries_the_http_status(stderr, status):
    """The raised error keeps what a caller branches on, parsed once."""
    with patch("repomatic.github.gh.run") as mock_run:
        mock_run.return_value = _make_result(
            returncode=1, stdout='{"partial": true}', stderr=stderr
        )
        with pytest.raises(GhCommandError) as excinfo:
            run_gh_command(["api", "repos/kiwi/orchard"])
    assert excinfo.value.status == status
    assert excinfo.value.stderr == stderr
    assert excinfo.value.stdout == '{"partial": true}'


def test_command_error_defaults_its_stderr_to_the_message():
    """An error built from a message alone still reads its status off it."""
    error = GhCommandError("HTTP 422: Unprocessable Entity")
    assert error.stderr == "HTTP 422: Unprocessable Entity"
    assert error.status == 422


def test_failure_message_omits_status_when_no_incident():
    """The healthy status annotation (empty string) leaves the error untouched."""
    with (
        patch("repomatic.github.gh.run") as mock_run,
        patch("repomatic.github.status.status_annotation", return_value=""),
    ):
        mock_run.return_value = _make_result(returncode=1, stderr="some error\n")
        with pytest.raises(RuntimeError) as excinfo:
            run_gh_command(["issue", "list"])
        assert str(excinfo.value) == "some error\n"


# -- Transient same-token retry on 401 ----------------------------------------


def test_401_transient_clears_on_same_token_retry():
    """A 401 that clears on the same token resolves without falling back."""
    with (
        patch("repomatic.github.gh.run") as mock_run,
        patch("repomatic.github.gh._TRANSIENT_AUTH_BACKOFF_SECONDS", (1, 3)),
        patch("repomatic.github.gh.time.sleep") as mock_sleep,
        patch.dict(
            "os.environ",
            {**_CLEAN_ENV, "REPOMATIC_PAT": "pat-value"},
            clear=True,
        ),
    ):
        mock_run.side_effect = [
            _make_result(returncode=1, stderr="Requires authentication"),
            _make_result(stdout="retry ok\n"),
        ]
        assert run_gh_command(["issue", "list"]) == "retry ok\n"
        assert mock_run.call_count == 2
        # Second attempt used the same token, after one sleep.
        first_env = mock_run.call_args_list[0].kwargs["env"]
        second_env = mock_run.call_args_list[1].kwargs["env"]
        assert first_env["GH_TOKEN"] == second_env["GH_TOKEN"] == "pat-value"
        mock_sleep.assert_called_once_with(1)


def test_401_transient_retry_exhausts_then_falls_back():
    """When every same-token retry returns 401, the cross-token fallback fires."""
    with (
        patch("repomatic.github.gh.run") as mock_run,
        patch("repomatic.github.gh._TRANSIENT_AUTH_BACKOFF_SECONDS", (1, 3)),
        patch("repomatic.github.gh.time.sleep") as mock_sleep,
        patch.dict(
            "os.environ",
            {**_CLEAN_ENV, "REPOMATIC_PAT": "pat-value", "GITHUB_TOKEN": "gha"},
            clear=True,
        ),
    ):
        mock_run.side_effect = [
            _make_result(returncode=1, stderr="Requires authentication"),
            _make_result(returncode=1, stderr="Requires authentication"),
            _make_result(returncode=1, stderr="Requires authentication"),
            _make_result(stdout="fallback ok\n"),
        ]
        assert run_gh_command(["issue", "list"]) == "fallback ok\n"
        # 1 initial + 2 same-token retries + 1 cross-token fallback.
        assert mock_run.call_count == 4
        assert [c.args[0] for c in mock_sleep.call_args_list] == [1, 3]


def test_401_transient_retry_disabled_when_schedule_empty():
    """An empty backoff schedule disables the retry loop entirely."""
    with (
        patch("repomatic.github.gh.run") as mock_run,
        patch("repomatic.github.gh._TRANSIENT_AUTH_BACKOFF_SECONDS", ()),
        patch("repomatic.github.gh.time.sleep") as mock_sleep,
        patch.dict(
            "os.environ",
            {**_CLEAN_ENV, "REPOMATIC_PAT": "pat-value"},
            clear=True,
        ),
    ):
        mock_run.return_value = _make_result(
            returncode=1,
            stderr="Requires authentication",
        )
        with pytest.raises(RuntimeError, match="Requires authentication"):
            run_gh_command(["issue", "list"])
        assert mock_run.call_count == 1
        mock_sleep.assert_not_called()


def test_non_401_skips_transient_retry():
    """Non-401 failures bypass the transient-retry loop and raise immediately."""
    with (
        patch("repomatic.github.gh.run") as mock_run,
        patch("repomatic.github.gh._TRANSIENT_AUTH_BACKOFF_SECONDS", (1, 3)),
        patch("repomatic.github.gh.time.sleep") as mock_sleep,
        patch.dict(
            "os.environ",
            {**_CLEAN_ENV, "REPOMATIC_PAT": "pat-value"},
            clear=True,
        ),
    ):
        mock_run.return_value = _make_result(returncode=1, stderr="Resource not found")
        with pytest.raises(RuntimeError, match="Resource not found"):
            run_gh_command(["issue", "list"])
        assert mock_run.call_count == 1
        mock_sleep.assert_not_called()


# -- Transient same-token retry on secondary rate limits ----------------------


_THROTTLE_STDERR = (
    "GraphQL: No server is currently available to service your request. "
    "Sorry about that. Please try resubmitting your request later."
)


def test_secondary_limit_clears_on_same_token_retry():
    """A throttled call that clears on the same token resolves without fallback."""
    with (
        patch("repomatic.github.gh.run") as mock_run,
        patch("repomatic.github.gh._TRANSIENT_THROTTLE_BACKOFF_SECONDS", (15, 30)),
        patch("repomatic.github.gh.time.sleep") as mock_sleep,
        patch.dict(
            "os.environ",
            {**_CLEAN_ENV, "REPOMATIC_PAT": "pat-value"},
            clear=True,
        ),
    ):
        mock_run.side_effect = [
            _make_result(returncode=1, stderr=_THROTTLE_STDERR),
            _make_result(stdout="retry ok\n"),
        ]
        assert run_gh_command(["api", "graphql"]) == "retry ok\n"
        assert mock_run.call_count == 2
        # Second attempt used the same token, after one sleep.
        first_env = mock_run.call_args_list[0].kwargs["env"]
        second_env = mock_run.call_args_list[1].kwargs["env"]
        assert first_env["GH_TOKEN"] == second_env["GH_TOKEN"] == "pat-value"
        mock_sleep.assert_called_once_with(15)


def test_secondary_limit_retry_exhausts_then_raises():
    """An exhausted throttle schedule raises, without the cross-token fallback.

    A throttled token is not a wrong one: swapping credentials against an
    abuse-detection refusal would only spread the burst across two
    identities, so the fallback stays scoped to auth markers.
    """
    with (
        patch("repomatic.github.gh.run") as mock_run,
        patch("repomatic.github.gh._TRANSIENT_THROTTLE_BACKOFF_SECONDS", (15, 30)),
        patch("repomatic.github.gh.time.sleep") as mock_sleep,
        patch.dict(
            "os.environ",
            {**_CLEAN_ENV, "REPOMATIC_PAT": "pat-value", "GITHUB_TOKEN": "gha"},
            clear=True,
        ),
    ):
        mock_run.return_value = _make_result(returncode=1, stderr=_THROTTLE_STDERR)
        with pytest.raises(RuntimeError, match="No server is currently available"):
            run_gh_command(["api", "graphql"])
        # 1 initial + 2 same-token retries, and no GITHUB_TOKEN attempt.
        assert mock_run.call_count == 3
        assert [c.args[0] for c in mock_sleep.call_args_list] == [15, 30]
        for call in mock_run.call_args_list:
            assert call.kwargs["env"]["GH_TOKEN"] == "pat-value"


def test_bad_credentials_raises_without_waiting():
    """A revoked token fails on the first call instead of paying the back-off.

    `Bad credentials` is deterministic: the same token cannot stop being
    revoked, so the same-token retry can only add idle seconds and flood the
    log with warnings that bury the real cause.
    """
    with (
        patch("repomatic.github.gh.run") as mock_run,
        patch("repomatic.github.gh._TRANSIENT_AUTH_BACKOFF_SECONDS", (1, 3)),
        patch("repomatic.github.gh.time.sleep") as mock_sleep,
        patch.dict(
            "os.environ",
            {**_CLEAN_ENV, "REPOMATIC_PAT": "revoked"},
            clear=True,
        ),
    ):
        mock_run.return_value = _make_result(returncode=1, stderr="Bad credentials")
        with pytest.raises(RuntimeError, match="Bad credentials"):
            run_gh_command(["issue", "list"])
        assert mock_run.call_count == 1
        mock_sleep.assert_not_called()


def test_bad_credentials_still_reaches_cross_token_fallback():
    """Skipping the same-token wait does not skip the second credential.

    `GITHUB_TOKEN` is issued by Actions itself, so it can succeed where a
    revoked PAT cannot: only the pointless wait is dropped, not the recovery.
    """
    with (
        patch("repomatic.github.gh.run") as mock_run,
        patch("repomatic.github.gh._TRANSIENT_AUTH_BACKOFF_SECONDS", (1, 3)),
        patch("repomatic.github.gh.time.sleep") as mock_sleep,
        patch.dict(
            "os.environ",
            {**_CLEAN_ENV, "REPOMATIC_PAT": "revoked", "GITHUB_TOKEN": "gha"},
            clear=True,
        ),
    ):
        mock_run.side_effect = [
            _make_result(returncode=1, stderr="Bad credentials"),
            _make_result(stdout="fallback ok\n"),
        ]
        assert run_gh_command(["issue", "list"]) == "fallback ok\n"
        # 1 initial call + 0 same-token retries + 1 cross-token fallback.
        assert mock_run.call_count == 2
        assert mock_run.call_args_list[1].kwargs["env"]["GH_TOKEN"] == "gha"
        mock_sleep.assert_not_called()


# ---------------------------------------------------------------------------
# GraphQL cursor pagination
# ---------------------------------------------------------------------------


def _page(nodes, has_next=False, cursor=""):
    """Build a one-connection GraphQL response body for the paginator."""
    return json.dumps({
        "data": {
            "search": {
                "nodes": nodes,
                "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
            }
        }
    })


def test_iter_graphql_nodes_single_page():
    """A single page yields its nodes and stops without a cursor request."""
    with patch("repomatic.github.gh.run_gh_command") as mock_gh:
        mock_gh.return_value = _page([{"id": "melon"}, {"id": "papaya"}])
        nodes = list(iter_graphql_nodes("query", ("search",)))
    assert nodes == [{"id": "melon"}, {"id": "papaya"}]
    assert mock_gh.call_count == 1
    assert not any("cursor=" in arg for arg in mock_gh.call_args.args[0])


def test_iter_graphql_nodes_follows_cursor():
    """Pagination follows `endCursor` until `hasNextPage` goes false."""
    with patch("repomatic.github.gh.run_gh_command") as mock_gh:
        mock_gh.side_effect = [
            _page([{"id": "melon"}], has_next=True, cursor="CUR1"),
            _page([{"id": "papaya"}]),
        ]
        nodes = list(iter_graphql_nodes("query", ("search",)))
    assert [node["id"] for node in nodes] == ["melon", "papaya"]
    assert mock_gh.call_count == 2
    second_args = mock_gh.call_args_list[1].args[0]
    assert "cursor=CUR1" in second_args


def test_iter_graphql_nodes_skips_null_nodes():
    """Null nodes (as the `search` connection can emit) are dropped."""
    with patch("repomatic.github.gh.run_gh_command") as mock_gh:
        mock_gh.return_value = _page([None, {"id": "melon"}, None])
        nodes = list(iter_graphql_nodes("query", ("search",)))
    assert nodes == [{"id": "melon"}]


def test_iter_graphql_nodes_sends_the_page_size_on_every_request():
    """Each page asks for the same size, as a typed field."""
    with patch("repomatic.github.gh.run_gh_command") as mock_gh:
        mock_gh.side_effect = [
            _page([{"id": "melon"}, {"id": "papaya"}], has_next=True, cursor="CUR1"),
            _page([{"id": "cherry"}]),
        ]
        nodes = list(
            iter_graphql_nodes(
                "query", ("search",), page_size_var="pageSize", page_size=2
            )
        )
    assert [node["id"] for node in nodes] == ["melon", "papaya", "cherry"]
    for call in mock_gh.call_args_list:
        args = call.args[0]
        assert args[args.index("pageSize=2") - 1] == "--field"


def test_iter_graphql_nodes_variable_flags():
    """String variables pass with `-f`, ints and bools with `-F`."""
    with patch("repomatic.github.gh.run_gh_command") as mock_gh:
        mock_gh.return_value = _page([])
        list(
            iter_graphql_nodes(
                "query",
                ("search",),
                {"owner": "melon", "count": 5, "flag": True},
            )
        )
    args = mock_gh.call_args.args[0]
    assert args[args.index("owner=melon") - 1] == "--raw-field"
    assert args[args.index("count=5") - 1] == "--field"
    assert args[args.index("flag=True") - 1] == "--field"


@pytest.mark.parametrize(
    ("value", "flag"),
    (
        # A repository named like a number, a boolean or null stays a string.
        pytest.param("2048", "--raw-field", id="numeric-string"),
        pytest.param("true", "--raw-field", id="boolean-string"),
        pytest.param("null", "--raw-field", id="null-string"),
        pytest.param("orchard", "--raw-field", id="plain-string"),
        # A real number or boolean keeps its GraphQL type.
        pytest.param(5, "--field", id="int"),
        pytest.param(True, "--field", id="bool"),
    ),
)
def test_graphql_args_types_each_variable(value, flag):
    """`--field` would read `2048` as a number, so a string rides `--raw-field`."""
    args = _graphql_args("query", {"name": value})
    assert args[:4] == ["api", "graphql", "--raw-field", "query=query"]
    assert args[args.index(f"name={value}") - 1] == flag


def test_gh_graphql_passes_a_numeric_repository_name_as_a_string():
    """A repository named `2048` reaches a `String!` variable as a string."""
    with patch("repomatic.github.gh.run_gh_command") as mock_gh:
        mock_gh.return_value = json.dumps({"data": {"repository": {"name": "2048"}}})
        data = gh_graphql("query", owner="kiwi", name="2048")
    assert data == {"repository": {"name": "2048"}}
    args = mock_gh.call_args.args[0]
    assert args[args.index("name=2048") - 1] == "--raw-field"
    assert args[args.index("owner=kiwi") - 1] == "--raw-field"


_PARTIAL_ANSWER = json.dumps({
    "data": {"apricot": {"id": "A"}, "cherry": None},
    "errors": [{"message": "Could not resolve to a node", "path": ["cherry"]}],
})
"""A GraphQL answer that failed for one field and resolved the other."""


def test_gh_graphql_partial_keeps_the_resolved_data():
    """With `partial`, the data printed beside the errors is the answer."""
    error = GhCommandError("GraphQL: Could not resolve", stdout=_PARTIAL_ANSWER)
    with patch("repomatic.github.gh.run_gh_command", side_effect=error):
        assert gh_graphql("query", partial=True) == {
            "apricot": {"id": "A"},
            "cherry": None,
        }


@pytest.mark.parametrize(
    ("stdout", "partial"),
    (
        pytest.param(_PARTIAL_ANSWER, False, id="strict-by-default"),
        pytest.param("", True, id="no-output"),
        pytest.param("not json", True, id="unparsable-output"),
        pytest.param('{"errors": []}', True, id="no-data"),
    ),
)
def test_gh_graphql_raises_without_usable_data(stdout, partial):
    """A failure with nothing to keep, or without `partial`, still raises."""
    error = GhCommandError("GraphQL: boom", stdout=stdout)
    with (
        patch("repomatic.github.gh.run_gh_command", side_effect=error),
        pytest.raises(GhCommandError, match="boom"),
    ):
        gh_graphql("query", partial=partial)


@pytest.mark.parametrize("soft", (True, False))
def test_gh_json_parses_the_output(soft):
    """A command printing JSON hands back the parsed value, soft or not."""
    with patch("repomatic.github.gh.run_gh_command", return_value='{"plum": 1}'):
        assert gh_json(["api", "repos/kiwi/orchard"], soft=soft) == {"plum": 1}


@pytest.mark.parametrize(
    ("patch_kwargs", "error"),
    (
        pytest.param(
            {"side_effect": GhCommandError("HTTP 502: Bad Gateway")},
            GhCommandError,
            id="command-failure",
        ),
        pytest.param({"return_value": "not json"}, GhOutputError, id="not-json"),
    ),
)
def test_gh_json_raises_unless_soft(patch_kwargs, error):
    """Both failure modes raise, and `soft` turns both into `None`."""
    with patch("repomatic.github.gh.run_gh_command", **patch_kwargs):
        with pytest.raises(error):
            gh_json(["api", "repos/kiwi/orchard"])
        assert gh_json(["api", "repos/kiwi/orchard"], soft=True) is None


@pytest.mark.parametrize(
    ("pages", "key", "expected"),
    (
        pytest.param([[{"id": 1}, {"id": 2}], [{"id": 3}]], "", [1, 2, 3], id="arrays"),
        pytest.param(
            [{"workflow_runs": [{"id": 1}]}, {"workflow_runs": [{"id": 2}]}],
            "workflow_runs",
            [1, 2],
            id="wrapped",
        ),
        pytest.param([{"total_count": 0}], "workflow_runs", [], id="wrapped-empty"),
        pytest.param([], "", [], id="no-page"),
    ),
)
def test_gh_api_pages_flattens_every_page(pages, key, expected):
    """Every page's items come back as one list, in API order."""
    with patch(
        "repomatic.github.gh.run_gh_command", return_value=json.dumps(pages)
    ) as mock_gh:
        items = gh_api_pages("repos/kiwi/orchard/fruits?per_page=100", key=key)
    assert [item["id"] for item in items] == expected
    assert mock_gh.call_args.args[0] == [
        "api",
        "--paginate",
        "--slurp",
        "repos/kiwi/orchard/fruits?per_page=100",
    ]


def test_gh_api_pages_raises_when_gh_cannot_reach_the_endpoint():
    """A paginated read fails loudly: a missing page would corrupt the result."""
    with (
        patch(
            "repomatic.github.gh.run_gh_command",
            side_effect=GhCommandError("HTTP 502: Bad Gateway"),
        ),
        pytest.raises(GhCommandError),
    ):
        gh_api_pages("repos/kiwi/orchard/fruits")


def test_iter_graphql_nodes_missing_connection_yields_nothing():
    """A response missing the connection path yields nothing and stops."""
    with patch("repomatic.github.gh.run_gh_command") as mock_gh:
        mock_gh.return_value = json.dumps({"data": {"user": None}})
        nodes = list(iter_graphql_nodes("query", ("user", "sponsorships")))
    assert nodes == []
    assert mock_gh.call_count == 1


@pytest.mark.parametrize(
    ("output", "kind", "expected"),
    (
        (
            "https://github.com/kate/fruits/pull/123",
            "pr",
            (123, "https://github.com/kate/fruits/pull/123"),
        ),
        (
            "https://github.com/kate/fruits/pull/123/",
            "pr",
            (123, "https://github.com/kate/fruits/pull/123"),
        ),
        (
            "https://github.com/kate/fruits/issues/7",
            "issue",
            (7, "https://github.com/kate/fruits/issues/7"),
        ),
        # `gh` prepends advisory lines of its own; only the last line counts.
        (
            "Warning: 3 uncommitted changes\nhttps://x/pull/9",
            "pr",
            (9, "https://x/pull/9"),
        ),
    ),
)
def test_parse_create_output(output, kind, expected):
    assert parse_create_output(output, kind) == expected


@pytest.mark.parametrize("kind", ("issue", "pr"))
@pytest.mark.parametrize(
    "output",
    ("", "no url here", "https://github.com/kate/fruits/pull/not-a-number"),
)
def test_parse_create_output_rejects_unparsable_output(output, kind):
    with pytest.raises(RuntimeError, match=f"`gh {kind} create`"):
        parse_create_output(output, kind)


def test_gh_executable_prefers_the_pinned_registry_binary():
    """The pinned, checksum-verified `gh` wins over whatever `$PATH` holds."""
    pinned_path = Path("/cache/gh")
    with patch("repomatic.github.gh.ensure_binary", return_value=pinned_path) as ensure:
        gh_executable.cache_clear()
        assert gh_executable() == str(pinned_path)
    assert ensure.call_args.args == ("gh",)
    gh_executable.cache_clear()


@pytest.mark.parametrize("failure", (BinaryInstallError("no network"), OSError("boom")))
def test_gh_executable_falls_back_to_path(failure, caplog):
    """A download failure degrades to `$PATH` loudly, never stranding the job."""
    caplog.set_level(logging.WARNING)
    with patch("repomatic.github.gh.ensure_binary", side_effect=failure):
        gh_executable.cache_clear()
        assert gh_executable() == "gh"
    assert "falling back to whatever is on PATH" in caplog.text
    gh_executable.cache_clear()
