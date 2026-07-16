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

"""Tests for the setup guide issue lifecycle and step rendering.

The setup guide fans out to the GitHub and PyPI APIs: the PAT permission
probe, the repository-settings checks, and an org-type lookup. Every seam is
patched here through {func}`_offline_setup_guide`, so the suite runs offline
and fast while the real issue-body assembly (template rendering, per-step
collapse state, and the close/keep-open decision) stays under test.
"""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from repomatic.cli import repomatic as repomatic_cli
from repomatic.github.token import PatPermissionResults
from tests.conftest import all_pass_pat_results

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Iterator
    from unittest.mock import MagicMock

# A neutral slug fed through GITHUB_REPOSITORY so Metadata resolves the repo
# from the environment (no `gh repo view` call) while the body still renders
# real repository links.
REPO_SLUG = "orchard/papaya"


def _partial_fail_pat_results() -> PatPermissionResults:
    """Build a PatPermissionResults with the Dependabot alerts check failing."""
    return PatPermissionResults(
        contents=(True, "Contents: token has access"),
        issues=(True, "Issues: token has access"),
        pull_requests=(True, "Pull requests: token has access"),
        vulnerability_alerts=(
            False,
            "Token lacks 'Dependabot alerts: Read-only' permission."
            " Update the PAT to include this permission.",
        ),
        workflows=(True, "Workflows: token has access"),
    )


@contextmanager
def _offline_setup_guide(
    *,
    pat_results: PatPermissionResults | None = None,
    branch_ok: tuple[bool, str] = (True, "Active branch rulesets found: main."),
    immutable_ok: tuple[bool | None, str] = (True, "Immutable releases enabled."),
    fork_pr_ok: tuple[bool | None, str] = (True, "Fork PR approval is required."),
    pypi_ok: tuple[bool | None, str] = (True, "Trusted publisher is configured."),
    pages_ok: tuple[bool | None, str] = (
        True,
        "Pages deployment source is GitHub Actions.",
    ),
    owner_type: str = "User",
) -> Iterator[tuple[MagicMock, list[str]]]:
    """Patch every network seam `manage_setup_guide` reaches.

    Each check passes by default, so the issue closes unless an argument
    overrides one seam to exercise its failure branch. Yields the captured
    `manage_issue_lifecycle` mock plus the list of issue bodies it received,
    so callers can assert on both the lifecycle decision and the rendered
    markdown.

    :param pat_results: Result of the PAT permission probe (defaults to all
        checks passing).
    :param branch_ok: Return of the branch-ruleset check.
    :param immutable_ok: Return of the immutable-releases check.
    :param fork_pr_ok: Return of the fork-PR approval-policy check.
    :param pypi_ok: Return of the PyPI Trusted Publisher check.
    :param pages_ok: Return of the Pages deployment-source check.
    :param owner_type: `.type` the org-detection `gh api users/…` call returns.
    """
    if pat_results is None:
        pat_results = all_pass_pat_results()
    bodies: list[str] = []
    with ExitStack() as stack:
        enter = stack.enter_context
        enter(patch("repomatic.github.token.validate_gh_token_env"))
        enter(
            patch(
                "repomatic.github.token.check_all_pat_permissions",
                return_value=pat_results,
            )
        )
        enter(
            patch(
                "repomatic.setup_guide.check_branch_ruleset_on_default",
                return_value=branch_ok,
            )
        )
        enter(
            patch(
                "repomatic.setup_guide.check_immutable_releases",
                return_value=immutable_ok,
            )
        )
        enter(
            patch(
                "repomatic.setup_guide.check_fork_pr_approval_policy",
                return_value=fork_pr_ok,
            )
        )
        enter(
            patch(
                "repomatic.setup_guide.check_pypi_trusted_publisher",
                return_value=pypi_ok,
            )
        )
        enter(
            patch(
                "repomatic.setup_guide.check_pages_deployment_source",
                return_value=pages_ok,
            )
        )
        enter(patch("repomatic.setup_guide.run_gh_command", return_value=owner_type))
        lifecycle = enter(patch("repomatic.setup_guide.manage_issue_lifecycle"))
        lifecycle.side_effect = lambda **kw: bodies.append(
            kw["body_file"].read_text(encoding="utf-8")
        )
        yield lifecycle, bodies


def _invoke(args: list[str], env: dict[str, str] | None = None):
    """Invoke the setup-guide CLI with GITHUB_REPOSITORY and no ambient PAT.

    Pre-setting GITHUB_REPOSITORY keeps Metadata offline; clearing
    REPOMATIC_PAT makes the `--has-pat` auto-detection deterministic.
    """
    full_env = {"GITHUB_REPOSITORY": REPO_SLUG, "REPOMATIC_PAT": "", **(env or {})}
    return CliRunner(env=full_env).invoke(repomatic_cli, args)


def test_setup_guide_no_pat_opens_setup_issue():
    """When no PAT is configured, the setup issue opens."""
    with _offline_setup_guide() as (lifecycle, _bodies):
        result = _invoke(["setup-guide"])
    assert result.exit_code == 0
    assert lifecycle.call_count == 1
    setup_kwargs = lifecycle.call_args_list[0][1]
    assert setup_kwargs["has_issues"] is True
    assert setup_kwargs["labels"] == ["🤖 ci"]
    assert setup_kwargs["title"] == "Repomatic setup guide"


def test_setup_guide_pat_without_repo_keeps_issue_open():
    """PAT without --repo cannot verify Dependabot or branch settings."""
    with _offline_setup_guide() as (lifecycle, _bodies):
        result = _invoke(["setup-guide", "--has-pat"])
    assert result.exit_code == 0
    assert lifecycle.call_count == 1
    # Without --repo, dependabot and branch checks cannot run.
    assert lifecycle.call_args_list[0][1]["has_issues"] is True


def test_setup_guide_body_contains_template():
    """The setup body file contains the setup guide template content."""
    with _offline_setup_guide() as (_lifecycle, bodies):
        _invoke(["setup-guide"])
    content = bodies[0]
    assert "REPOMATIC_PAT" in content
    assert "Fine-grained tokens" in content


@patch("repomatic.github.token.validate_gh_token_env")
@patch("repomatic.setup_guide.manage_issue_lifecycle")
def test_setup_guide_disabled_skips(mock_lifecycle, _mock_token, tmp_path, monkeypatch):
    """When setup-guide is disabled in config, the command exits without action."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text("[tool.repomatic]\nsetup-guide = false\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(repomatic_cli, ["setup-guide"])
    assert result.exit_code == 0
    mock_lifecycle.assert_not_called()


@pytest.mark.parametrize(
    ("branch_ok", "has_vt_key", "expected_has_issues"),
    (
        pytest.param(
            (True, "Active branch rulesets found: main."),
            True,
            False,
            id="all-pass-closes",
        ),
        pytest.param(
            (False, "No active branch rulesets found."),
            True,
            True,
            id="branch-missing-opens",
        ),
        pytest.param(
            (True, "Active branch rulesets found: main."),
            False,
            True,
            id="vt-key-missing-opens",
        ),
    ),
)
def test_setup_guide_lifecycle_reflects_check_outcomes(
    branch_ok, has_vt_key, expected_has_issues
):
    """The issue closes only when every verifiable step passes.

    All checks pass by default, so the issue closes; a failing branch ruleset
    or a missing VirusTotal key (with Nuitka active in this repo) keeps it
    open.
    """
    args = ["setup-guide", "--has-pat", "--repo", REPO_SLUG]
    if has_vt_key:
        args.append("--has-virustotal-key")
    with _offline_setup_guide(branch_ok=branch_ok) as (lifecycle, _bodies):
        result = _invoke(args)
    assert result.exit_code == 0
    assert lifecycle.call_args_list[0][1]["has_issues"] is expected_has_issues


def test_setup_guide_pat_missing_permission_keeps_issue_open():
    """When PAT is configured but a permission is missing, the issue stays open."""
    with _offline_setup_guide(pat_results=_partial_fail_pat_results()) as (
        lifecycle,
        _bodies,
    ):
        result = _invoke(["setup-guide", "--has-pat", "--repo", REPO_SLUG])
    assert result.exit_code == 0
    assert lifecycle.call_count == 1
    assert lifecycle.call_args_list[0][1]["has_issues"] is True


def test_setup_guide_pat_missing_permission_body_contains_warning():
    """When PAT has missing permissions, the issue body contains a warning section."""
    with _offline_setup_guide(pat_results=_partial_fail_pat_results()) as (
        _lifecycle,
        bodies,
    ):
        _invoke(["setup-guide", "--has-pat", "--repo", REPO_SLUG])
    content = bodies[0]
    assert "missing some permissions" in content
    assert "Dependabot alerts" in content


def test_setup_guide_completed_step_collapsed():
    """Completed steps render as collapsed details blocks with a checkmark."""
    with _offline_setup_guide() as (_lifecycle, bodies):
        _invoke(["setup-guide", "--has-pat", "--repo", REPO_SLUG])
    content = bodies[0]
    # Token step should be collapsed with checkmark.
    assert "<details>\n<summary>✅ <strong>Create and configure the token" in content
    # Branch step should be collapsed with checkmark.
    assert "<details>\n<summary>✅ <strong>Protect the main branch" in content


def test_setup_guide_incomplete_step_expanded():
    """Incomplete steps render as open details blocks with an error indicator."""
    with _offline_setup_guide() as (_lifecycle, bodies):
        _invoke(["setup-guide"])
    content = bodies[0]
    # Token step should be expanded with warning indicator.
    assert "<details open>" in content
    assert "❌" in content
    assert "<strong>Create and configure the token" in content


def test_setup_guide_nuitka_disabled_hides_vt_step(tmp_path, monkeypatch):
    """When Nuitka is disabled, the VT step is omitted from the setup guide."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        "[project]\nname = 'papaya'\nversion = '1.0'\n\n"
        "[tool.repomatic.nuitka]\nenabled = false\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    with _offline_setup_guide() as (lifecycle, bodies):
        result = _invoke(["setup-guide", "--has-pat", "--repo", REPO_SLUG])
    assert result.exit_code == 0
    content = bodies[0]
    assert "VirusTotal" not in content
    # Issue closes without VT key when Nuitka is disabled.
    assert lifecycle.call_args_list[0][1]["has_issues"] is False


def test_setup_guide_vt_step_shown_when_nuitka_active():
    """When Nuitka is active, the VT step appears in the setup guide body."""
    with _offline_setup_guide() as (_lifecycle, bodies):
        _invoke(["setup-guide"])
    content = bodies[0]
    assert "VirusTotal" in content
