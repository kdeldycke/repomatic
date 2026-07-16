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

"""Tests for the setup guide issue lifecycle and step rendering."""

from __future__ import annotations

from unittest.mock import patch

from click.testing import CliRunner

from repomatic.cli import repomatic as repomatic_cli
from repomatic.github.token import PatPermissionResults
from tests.conftest import all_pass_pat_results


@patch("repomatic.github.token.validate_gh_token_env")
@patch("repomatic.setup_guide.manage_issue_lifecycle")
def test_setup_guide_no_pat_opens_setup_issue(mock_lifecycle, _mock_token):
    """When no PAT is configured, the setup issue opens."""
    runner = CliRunner()
    result = runner.invoke(repomatic_cli, ["setup-guide"])
    assert result.exit_code == 0
    assert mock_lifecycle.call_count == 1
    setup_kwargs = mock_lifecycle.call_args_list[0][1]
    assert setup_kwargs["has_issues"] is True
    assert setup_kwargs["labels"] == ["🤖 ci"]
    assert setup_kwargs["title"] == "Repomatic setup guide"


@patch("repomatic.github.token.validate_gh_token_env")
@patch("repomatic.setup_guide.manage_issue_lifecycle")
def test_setup_guide_pat_without_repo_keeps_issue_open(mock_lifecycle, _mock_token):
    """PAT without --repo cannot verify Dependabot or branch settings."""
    runner = CliRunner(env={"GITHUB_REPOSITORY": ""})
    result = runner.invoke(repomatic_cli, ["setup-guide", "--has-pat"])
    assert result.exit_code == 0
    assert mock_lifecycle.call_count == 1
    # Without --repo, dependabot and branch checks cannot run.
    assert mock_lifecycle.call_args_list[0][1]["has_issues"] is True


@patch("repomatic.github.token.validate_gh_token_env")
@patch("repomatic.setup_guide.manage_issue_lifecycle")
def test_setup_guide_body_contains_template(mock_lifecycle, _mock_token):
    """The setup body file contains the setup guide template content."""
    captured: list[str] = []
    mock_lifecycle.side_effect = lambda **kw: captured.append(
        kw["body_file"].read_text(encoding="UTF-8")
    )
    runner = CliRunner()
    runner.invoke(repomatic_cli, ["setup-guide"])
    content = captured[0]
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


def _partial_fail_pat_results():
    """Build a PatPermissionResults with one failing check."""
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


@patch("repomatic.github.token.validate_gh_token_env")
@patch("repomatic.setup_guide.manage_issue_lifecycle")
@patch("repomatic.setup_guide.check_branch_ruleset_on_default")
@patch("repomatic.github.token.check_all_pat_permissions")
@patch("repomatic.setup_guide.check_pages_deployment_source")
@patch("repomatic.setup_guide.check_pypi_trusted_publisher")
def test_setup_guide_all_checks_pass_closes_issue(
    mock_pypi, mock_pages, mock_check, mock_branch, mock_lifecycle, _mock_token
):
    """When PAT, permissions, branch ruleset, and VT key all pass, the issue closes."""
    mock_check.return_value = all_pass_pat_results()
    mock_branch.return_value = (True, "Active branch rulesets found: main.")
    mock_pages.return_value = (True, "Pages deployment source is GitHub Actions.")
    mock_pypi.return_value = (True, "Trusted publisher is configured.")
    runner = CliRunner()
    result = runner.invoke(
        repomatic_cli,
        ["setup-guide", "--has-pat", "--has-virustotal-key", "--repo", "owner/repo"],
    )
    assert result.exit_code == 0
    assert mock_lifecycle.call_count == 1
    assert mock_lifecycle.call_args_list[0][1]["has_issues"] is False


@patch("repomatic.github.token.validate_gh_token_env")
@patch("repomatic.setup_guide.manage_issue_lifecycle")
@patch("repomatic.setup_guide.check_branch_ruleset_on_default")
@patch("repomatic.github.token.check_all_pat_permissions")
def test_setup_guide_pat_missing_permission_keeps_issue_open(
    mock_check, mock_branch, mock_lifecycle, _mock_token
):
    """When PAT is configured but a permission is missing, the issue stays open."""
    mock_check.return_value = _partial_fail_pat_results()
    mock_branch.return_value = (True, "Active branch rulesets found: main.")
    runner = CliRunner()
    result = runner.invoke(
        repomatic_cli, ["setup-guide", "--has-pat", "--repo", "owner/repo"]
    )
    assert result.exit_code == 0
    assert mock_lifecycle.call_count == 1
    assert mock_lifecycle.call_args_list[0][1]["has_issues"] is True


@patch("repomatic.github.token.validate_gh_token_env")
@patch("repomatic.setup_guide.manage_issue_lifecycle")
@patch("repomatic.setup_guide.check_branch_ruleset_on_default")
@patch("repomatic.github.token.check_all_pat_permissions")
def test_setup_guide_pat_missing_permission_body_contains_warning(
    mock_check, mock_branch, mock_lifecycle, _mock_token
):
    """When PAT has missing permissions, the issue body contains a warning section."""
    mock_check.return_value = _partial_fail_pat_results()
    mock_branch.return_value = (True, "Active branch rulesets found: main.")
    captured: list[str] = []
    mock_lifecycle.side_effect = lambda **kw: captured.append(
        kw["body_file"].read_text(encoding="UTF-8")
    )
    runner = CliRunner()
    runner.invoke(repomatic_cli, ["setup-guide", "--has-pat", "--repo", "owner/repo"])
    content = captured[0]
    assert "missing some permissions" in content
    assert "Dependabot alerts" in content


@patch("repomatic.github.token.validate_gh_token_env")
@patch("repomatic.setup_guide.manage_issue_lifecycle")
@patch("repomatic.setup_guide.check_branch_ruleset_on_default")
@patch("repomatic.github.token.check_all_pat_permissions")
def test_setup_guide_completed_step_collapsed(
    mock_check, mock_branch, mock_lifecycle, _mock_token
):
    """Completed steps render as collapsed details blocks with a checkmark."""
    mock_check.return_value = all_pass_pat_results()
    mock_branch.return_value = (True, "Active branch rulesets found: main.")
    captured: list[str] = []
    mock_lifecycle.side_effect = lambda **kw: captured.append(
        kw["body_file"].read_text(encoding="UTF-8")
    )
    runner = CliRunner()
    runner.invoke(repomatic_cli, ["setup-guide", "--has-pat", "--repo", "owner/repo"])
    content = captured[0]
    # Token step should be collapsed with checkmark.
    assert (
        "<details>\n<summary>\u2705 <strong>Create and configure the token" in content
    )
    # Branch step should be collapsed with checkmark.
    assert "<details>\n<summary>\u2705 <strong>Protect the main branch" in content


@patch("repomatic.github.token.validate_gh_token_env")
@patch("repomatic.setup_guide.manage_issue_lifecycle")
def test_setup_guide_incomplete_step_expanded(mock_lifecycle, _mock_token):
    """Incomplete steps render as open details blocks with an error indicator."""
    captured: list[str] = []
    mock_lifecycle.side_effect = lambda **kw: captured.append(
        kw["body_file"].read_text(encoding="UTF-8")
    )
    runner = CliRunner()
    runner.invoke(repomatic_cli, ["setup-guide"])
    content = captured[0]
    # Token step should be expanded with warning indicator.
    assert "<details open>" in content
    assert "\u274c" in content
    assert "<strong>Create and configure the token" in content


@patch("repomatic.github.token.validate_gh_token_env")
@patch("repomatic.setup_guide.manage_issue_lifecycle")
@patch("repomatic.setup_guide.check_branch_ruleset_on_default")
@patch("repomatic.github.token.check_all_pat_permissions")
def test_setup_guide_missing_branch_ruleset_keeps_issue_open(
    mock_check, mock_branch, mock_lifecycle, _mock_token
):
    """When PAT and permissions pass but branch ruleset is missing, issue stays open."""
    mock_check.return_value = all_pass_pat_results()
    mock_branch.return_value = (False, "No active branch rulesets found.")
    runner = CliRunner()
    result = runner.invoke(
        repomatic_cli, ["setup-guide", "--has-pat", "--repo", "owner/repo"]
    )
    assert result.exit_code == 0
    assert mock_lifecycle.call_args_list[0][1]["has_issues"] is True


@patch("repomatic.github.token.validate_gh_token_env")
@patch("repomatic.setup_guide.manage_issue_lifecycle")
@patch("repomatic.setup_guide.check_branch_ruleset_on_default")
@patch("repomatic.github.token.check_all_pat_permissions")
def test_setup_guide_missing_vt_key_keeps_issue_open(
    mock_check, mock_branch, mock_lifecycle, _mock_token
):
    """When Nuitka is active and VT key is missing, the issue stays open."""
    mock_check.return_value = all_pass_pat_results()
    mock_branch.return_value = (True, "Active branch rulesets found: main.")
    runner = CliRunner()
    result = runner.invoke(
        repomatic_cli, ["setup-guide", "--has-pat", "--repo", "owner/repo"]
    )
    assert result.exit_code == 0
    assert mock_lifecycle.call_args_list[0][1]["has_issues"] is True


@patch("repomatic.github.token.validate_gh_token_env")
@patch("repomatic.setup_guide.manage_issue_lifecycle")
@patch("repomatic.setup_guide.check_branch_ruleset_on_default")
@patch("repomatic.github.token.check_all_pat_permissions")
@patch("repomatic.setup_guide.check_pypi_trusted_publisher")
def test_setup_guide_nuitka_disabled_hides_vt_step(
    mock_pypi,
    mock_check,
    mock_branch,
    mock_lifecycle,
    _mock_token,
    tmp_path,
    monkeypatch,
):
    """When Nuitka is disabled, the VT step is omitted from the setup guide."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        "[project]\nname = 'test'\nversion = '1.0'\n\n"
        "[tool.repomatic.nuitka]\nenabled = false\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    mock_check.return_value = all_pass_pat_results()
    mock_branch.return_value = (True, "Active branch rulesets found: main.")
    mock_pypi.return_value = (True, "Trusted publisher is configured.")
    captured: list[str] = []
    mock_lifecycle.side_effect = lambda **kw: captured.append(
        kw["body_file"].read_text(encoding="UTF-8")
    )
    runner = CliRunner()
    result = runner.invoke(
        repomatic_cli, ["setup-guide", "--has-pat", "--repo", "owner/repo"]
    )
    assert result.exit_code == 0
    content = captured[0]
    assert "VirusTotal" not in content
    # Issue closes without VT key when Nuitka is disabled.
    assert mock_lifecycle.call_args_list[0][1]["has_issues"] is False


@patch("repomatic.github.token.validate_gh_token_env")
@patch("repomatic.setup_guide.manage_issue_lifecycle")
def test_setup_guide_vt_step_shown_when_nuitka_active(mock_lifecycle, _mock_token):
    """When Nuitka is active, the VT step appears in the setup guide body."""
    captured: list[str] = []
    mock_lifecycle.side_effect = lambda **kw: captured.append(
        kw["body_file"].read_text(encoding="UTF-8")
    )
    runner = CliRunner()
    runner.invoke(repomatic_cli, ["setup-guide"])
    content = captured[0]
    assert "VirusTotal" in content
