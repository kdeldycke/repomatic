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
from click_extra import ClickException

from repomatic.github.gh import (
    api_headers,
    gh_executable,
    iter_graphql_nodes,
    parse_create_output,
    run_gh_command,
)


@pytest.fixture(autouse=True)
def _no_status_probe():
    """Neutralize the githubstatus.com probe by default.

    Tests that exercise the incident annotation override this fixture by
    patching ``repomatic.github.status.status_annotation`` themselves.
    """
    with patch(
        "repomatic.github.status.status_annotation",
        return_value="",
    ):
        yield


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
        patch("repomatic.github.status.status_annotation", return_value=""),
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


def test_iter_graphql_nodes_max_nodes_shrinks_last_page():
    """The budget caps the yield count and shrinks the final page request."""
    with patch("repomatic.github.gh.run_gh_command") as mock_gh:
        mock_gh.side_effect = [
            _page([{"id": "melon"}, {"id": "papaya"}], has_next=True, cursor="CUR1"),
            _page([{"id": "cherry"}], has_next=True, cursor="CUR2"),
        ]
        nodes = list(
            iter_graphql_nodes(
                "query",
                ("search",),
                page_size_var="pageSize",
                page_size=2,
                max_nodes=3,
            )
        )
    assert [node["id"] for node in nodes] == ["melon", "papaya", "cherry"]
    assert mock_gh.call_count == 2
    first_args = mock_gh.call_args_list[0].args[0]
    second_args = mock_gh.call_args_list[1].args[0]
    assert "pageSize=2" in first_args
    # Only one node left in the budget: the second request asks for one.
    assert "pageSize=1" in second_args


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
    with patch(
        "repomatic.tooling.tool_runner.ensure_binary", return_value=pinned_path
    ) as ensure:
        gh_executable.cache_clear()
        assert gh_executable() == str(pinned_path)
    assert ensure.call_args.args == ("gh",)
    gh_executable.cache_clear()


@pytest.mark.parametrize("failure", (ClickException("no network"), OSError("boom")))
def test_gh_executable_falls_back_to_path(failure, caplog):
    """A download failure degrades to `$PATH` loudly, never stranding the job."""
    caplog.set_level(logging.WARNING)
    with patch("repomatic.tooling.tool_runner.ensure_binary", side_effect=failure):
        gh_executable.cache_clear()
        assert gh_executable() == "gh"
    assert "falling back to whatever is on PATH" in caplog.text
    gh_executable.cache_clear()
