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

from subprocess import CompletedProcess
from unittest.mock import patch

import pytest

from repomatic.github.gh import run_gh_command


@pytest.fixture(autouse=True)
def _no_status_probe():
    """Neutralize the githubstatus.com probe by default.

    Tests that exercise the incident annotation override this fixture by
    patching ``repomatic.github.gh.status_annotation`` themselves.
    """
    with patch(
        "repomatic.github.gh.status_annotation",
        return_value="",
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
        # No explicit env override: gh picks up GH_TOKEN natively.
        assert mock_run.call_args.kwargs.get("env") is None


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
        assert mock_run.call_args.kwargs.get("env") is None


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
        patch("repomatic.github.gh.status_annotation", return_value=""),
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
        patch("repomatic.github.gh.status_annotation", return_value=annotation),
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
        patch("repomatic.github.gh.status_annotation", return_value=""),
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
