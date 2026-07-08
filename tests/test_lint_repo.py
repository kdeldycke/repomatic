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

"""Tests for repository linting module."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from repomatic.github.token import (
    PatPermissionResults,
    check_pat_contents_permission,
    check_pat_issues_permission,
    check_pat_pull_requests_permission,
    check_pat_vulnerability_alerts_permission,
    check_pat_workflows_permission,
)
from repomatic.lint_repo import (
    check_description_matches,
    check_funding_file,
    check_inline_pins_match_upstream,
    check_package_name_vs_repo,
    check_pat_stale_statuses_permission,
    check_pypi_trusted_publisher,
    check_stale_draft_releases,
    check_test_matrix_excludes,
    check_topics_subset_of_keywords,
    check_website_for_sphinx,
    get_repo_metadata,
    run_repo_lint,
)
from repomatic.metadata import Metadata
from repomatic.pypi import TrustedPublisher


def test_successful_fetch():
    """Fetch and parse repo metadata."""
    with patch("repomatic.lint_repo.run_gh_command") as mock_gh:
        mock_gh.return_value = (
            '{"homepageUrl": "https://example.com", "description": "A package"}'
        )
        result = get_repo_metadata("owner/repo")
        assert result == {
            "homepageUrl": "https://example.com",
            "description": "A package",
        }


def test_empty_fields():
    """Handle empty fields."""
    with patch("repomatic.lint_repo.run_gh_command") as mock_gh:
        mock_gh.return_value = '{"homepageUrl": "", "description": ""}'
        result = get_repo_metadata("owner/repo")
        assert result == {"homepageUrl": None, "description": None}


def test_api_failure():
    """Handle API failure."""
    with patch("repomatic.lint_repo.run_gh_command") as mock_gh:
        mock_gh.side_effect = RuntimeError("gh command failed")
        result = get_repo_metadata("owner/repo")
        assert result == {"homepageUrl": None, "description": None}


def test_json_parse_error():
    """Handle JSON parse error."""
    with patch("repomatic.lint_repo.run_gh_command") as mock_gh:
        mock_gh.return_value = "not json"
        result = get_repo_metadata("owner/repo")
        assert result == {"homepageUrl": None, "description": None}


def test_names_match():
    """No warning when names match."""
    warning, msg = check_package_name_vs_repo("my-package", "my-package")
    assert warning is None
    assert "matches" in msg


def test_names_differ():
    """Warning when names differ."""
    warning, _msg = check_package_name_vs_repo("my-package", "my-repo")
    assert warning is not None
    assert "differs" in warning
    assert "my-package" in warning
    assert "my-repo" in warning


def test_no_package_name():
    """Skip when no package name."""
    warning, msg = check_package_name_vs_repo(None, "my-repo")
    assert warning is None
    assert "skipped" in msg


def test_not_sphinx():
    """Skip when not a Sphinx project."""
    warning, msg = check_website_for_sphinx("owner/repo", is_sphinx=False)
    assert warning is None
    assert "skipped" in msg


def test_sphinx_with_website():
    """No warning when Sphinx project has website."""
    warning, msg = check_website_for_sphinx(
        "owner/repo", is_sphinx=True, homepage_url="https://docs.example.com"
    )
    assert warning is None
    assert "https://docs.example.com" in msg


def test_sphinx_without_website():
    """Warning when Sphinx project has no website."""
    warning, _msg = check_website_for_sphinx(
        "owner/repo", is_sphinx=True, homepage_url=None
    )
    assert warning is not None
    assert "Sphinx" in warning
    assert "not set" in warning


def test_sphinx_fetches_metadata():
    """Fetch metadata when homepage_url not provided."""
    with patch("repomatic.lint_repo.get_repo_metadata") as mock_get:
        mock_get.return_value = {"homepageUrl": "https://example.com"}
        warning, _msg = check_website_for_sphinx("owner/repo", is_sphinx=True)
        assert warning is None
        mock_get.assert_called_once_with("owner/repo")


def test_descriptions_match():
    """No error when descriptions match."""
    error, msg = check_description_matches(
        "owner/repo",
        project_description="A cool package",
        repo_description="A cool package",
    )
    assert error is None
    assert "matches" in msg


def test_descriptions_differ():
    """Error when descriptions differ."""
    error, _msg = check_description_matches(
        "owner/repo",
        project_description="A cool package",
        repo_description="Different description",
    )
    assert error is not None
    assert "!=" in error


def test_no_project_description():
    """Skip when no project description."""
    error, msg = check_description_matches(
        "owner/repo", project_description=None, repo_description="Something"
    )
    assert error is None
    assert "skipped" in msg


def test_fetches_metadata():
    """Fetch metadata when repo_description not provided."""
    with patch("repomatic.lint_repo.get_repo_metadata") as mock_get:
        mock_get.return_value = {"description": "A cool package"}
        error, _msg = check_description_matches(
            "owner/repo", project_description="A cool package"
        )
        assert error is None
        mock_get.assert_called_once_with("owner/repo")


def test_all_checks_pass(capsys):
    """Return 0 when all checks pass."""
    with patch("repomatic.lint_repo.get_repo_metadata") as mock_get:
        mock_get.return_value = {
            "homepageUrl": "https://example.com",
            "description": "A cool package",
        }
        exit_code = run_repo_lint(
            package_name="my-package",
            repo_name="my-package",
            is_sphinx=True,
            project_description="A cool package",
            repo="owner/repo",
        )
        assert exit_code == 0


def test_description_mismatch(capsys):
    """Return 1 when description mismatches."""
    with patch("repomatic.lint_repo.get_repo_metadata") as mock_get:
        mock_get.return_value = {
            "homepageUrl": None,
            "description": "Different description",
        }
        exit_code = run_repo_lint(
            project_description="A cool package",
            repo="owner/repo",
        )
        assert exit_code == 1
        captured = capsys.readouterr()
        assert "::error::" in captured.out


def test_package_name_warning(capsys):
    """Emit warning for package name mismatch but still pass."""
    exit_code = run_repo_lint(
        package_name="my-package",
        repo_name="different-repo",
    )
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "::warning::" in captured.out


def test_website_warning(capsys):
    """Emit warning for missing website but still pass."""
    with patch("repomatic.lint_repo.get_repo_metadata") as mock_get:
        mock_get.return_value = {"homepageUrl": None, "description": None}
        exit_code = run_repo_lint(
            is_sphinx=True,
            repo="owner/repo",
        )
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "::warning::" in captured.out


@pytest.mark.parametrize(
    ("unsubscribe_active", "has_notifications_pat", "expected"),
    (
        (False, False, None),
        (True, False, "::warning::"),
        (True, True, "✓ REPOMATIC_NOTIFICATIONS_PAT secret is configured."),
    ),
)
def test_notifications_pat_check(
    capsys, unsubscribe_active, has_notifications_pat, expected
):
    """The notifications PAT check only fires when the workflow is opted in."""
    exit_code = run_repo_lint(
        unsubscribe_active=unsubscribe_active,
        has_notifications_pat=has_notifications_pat,
    )
    assert exit_code == 0
    captured = capsys.readouterr()
    if expected is None:
        assert "REPOMATIC_NOTIFICATIONS_PAT" not in captured.out
    else:
        assert expected in captured.out


def test_minimal_run(capsys):
    """Run with no checks enabled."""
    exit_code = run_repo_lint()
    assert exit_code == 0


def test_topics_no_keywords():
    """Skip when no keywords provided."""
    warning, msg = check_topics_subset_of_keywords("owner/repo", keywords=None)
    assert warning is None
    assert "skipped" in msg


def test_topics_all_in_keywords():
    """No warning when all topics are in keywords."""
    with patch("repomatic.lint_repo.run_gh_command") as mock_gh:
        mock_gh.return_value = "python\nautomation\n"
        warning, msg = check_topics_subset_of_keywords(
            "owner/repo", keywords=["python", "automation", "cli"]
        )
        assert warning is None
        assert "2" in msg


def test_topics_extra_not_in_keywords():
    """Warning when topics exist that are not in keywords."""
    with patch("repomatic.lint_repo.run_gh_command") as mock_gh:
        mock_gh.return_value = "python\nunknown-topic\n"
        warning, _msg = check_topics_subset_of_keywords(
            "owner/repo", keywords=["python", "cli"]
        )
        assert warning is not None
        assert "unknown-topic" in warning


def test_topics_api_failure():
    """Skip gracefully when API call fails."""
    with patch("repomatic.lint_repo.run_gh_command") as mock_gh:
        mock_gh.side_effect = RuntimeError("gh command failed")
        warning, msg = check_topics_subset_of_keywords(
            "owner/repo", keywords=["python"]
        )
        assert warning is None
        assert "skipped" in msg


def test_topics_empty_response():
    """Skip when no topics are set on the repo."""
    with patch("repomatic.lint_repo.run_gh_command") as mock_gh:
        mock_gh.return_value = ""
        warning, msg = check_topics_subset_of_keywords(
            "owner/repo", keywords=["python"]
        )
        assert warning is None
        assert "skipped" in msg


def _graphql_response(*, is_fork: bool = False, has_sponsors: bool = True) -> str:
    """Build a mock GraphQL response for funding checks."""
    return json.dumps({
        "data": {
            "repository": {"isFork": is_fork},
            "repositoryOwner": {"hasSponsorsListing": has_sponsors},
        },
    })


def test_funding_file_exists(tmp_path, monkeypatch):
    """No warning when funding file already exists."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "FUNDING.yml").write_text("github: owner\n")
    warning, msg = check_funding_file("owner/repo")
    assert warning is None
    assert "found" in msg


def test_funding_file_exists_lowercase(tmp_path, monkeypatch):
    """Detect funding file regardless of case."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "funding.yml").write_text("github: owner\n")
    warning, msg = check_funding_file("owner/repo")
    assert warning is None
    assert "found" in msg


def test_funding_missing_with_sponsors(tmp_path, monkeypatch):
    """Warning when owner has sponsors but no funding file."""
    monkeypatch.chdir(tmp_path)
    with patch("repomatic.lint_repo.run_gh_command") as mock_gh:
        mock_gh.return_value = _graphql_response(has_sponsors=True)
        warning, _msg = check_funding_file("owner/repo")
        assert warning is not None
        assert "FUNDING.yml" in warning
        assert "Sponsor" in warning


def test_funding_skipped_for_fork(tmp_path, monkeypatch):
    """Skip funding check for forked repositories."""
    monkeypatch.chdir(tmp_path)
    with patch("repomatic.lint_repo.run_gh_command") as mock_gh:
        mock_gh.return_value = _graphql_response(is_fork=True)
        warning, msg = check_funding_file("owner/repo")
        assert warning is None
        assert "fork" in msg


def test_funding_skipped_no_sponsors(tmp_path, monkeypatch):
    """Skip when owner has no GitHub Sponsors listing."""
    monkeypatch.chdir(tmp_path)
    with patch("repomatic.lint_repo.run_gh_command") as mock_gh:
        mock_gh.return_value = _graphql_response(has_sponsors=False)
        warning, msg = check_funding_file("owner/repo")
        assert warning is None
        assert "no GitHub Sponsors" in msg


def test_funding_api_failure(tmp_path, monkeypatch):
    """Skip gracefully when GraphQL API call fails."""
    monkeypatch.chdir(tmp_path)
    with patch("repomatic.lint_repo.run_gh_command") as mock_gh:
        mock_gh.side_effect = RuntimeError("gh command failed")
        warning, msg = check_funding_file("owner/repo")
        assert warning is None
        assert "skipped" in msg


def test_funding_json_parse_error(tmp_path, monkeypatch):
    """Skip gracefully when API returns invalid JSON."""
    monkeypatch.chdir(tmp_path)
    with patch("repomatic.lint_repo.run_gh_command") as mock_gh:
        mock_gh.return_value = "not json"
        warning, msg = check_funding_file("owner/repo")
        assert warning is None
        assert "skipped" in msg


# --- Stale draft releases check unit tests ---


def test_stale_drafts_detected():
    """Warn about draft releases that are not dev pre-releases."""
    with patch("repomatic.lint_repo.run_gh_command") as mock_gh:
        mock_gh.return_value = json.dumps([
            {"tagName": "v6.1.2", "isDraft": True},
            {"tagName": "v6.2.0", "isDraft": False},
            {"tagName": "v6.3.0.dev0", "isDraft": True},
        ])
        warning, _msg = check_stale_draft_releases("owner/repo")
        assert warning is not None
        assert "v6.1.2" in warning
        assert "v6.3.0.dev0" not in warning


def test_stale_drafts_none():
    """No warning when only dev pre-release drafts exist."""
    with patch("repomatic.lint_repo.run_gh_command") as mock_gh:
        mock_gh.return_value = json.dumps([
            {"tagName": "v6.2.0", "isDraft": False},
            {"tagName": "v6.3.0.dev0", "isDraft": True},
        ])
        warning, msg = check_stale_draft_releases("owner/repo")
        assert warning is None
        assert "No stale" in msg


def test_stale_drafts_api_failure():
    """Skip gracefully when API call fails."""
    with patch("repomatic.lint_repo.run_gh_command") as mock_gh:
        mock_gh.side_effect = RuntimeError("gh command failed")
        warning, msg = check_stale_draft_releases("owner/repo")
        assert warning is None
        assert "skipped" in msg


def test_stale_drafts_multiple():
    """List all stale draft tags in the warning."""
    with patch("repomatic.lint_repo.run_gh_command") as mock_gh:
        mock_gh.return_value = json.dumps([
            {"tagName": "v6.1.2", "isDraft": True},
            {"tagName": "v6.2.0-rc1", "isDraft": True},
        ])
        warning, _msg = check_stale_draft_releases("owner/repo")
        assert warning is not None
        assert "v6.1.2" in warning
        assert "v6.2.0-rc1" in warning


# --- PAT capability check unit tests ---


def test_pat_contents_permission_pass():
    """Pass when contents API call succeeds."""
    with patch("repomatic.github.token.run_gh_command") as mock_gh:
        mock_gh.return_value = "[]"
        passed, msg = check_pat_contents_permission("owner/repo")
        assert passed is True
        assert "Contents" in msg


def test_pat_contents_permission_fail():
    """Fail when contents API call raises with HTTP 403."""
    with (
        patch("repomatic.github.token.run_gh_command") as mock_gh,
        patch("repomatic.github.token.status_annotation", return_value=""),
    ):
        mock_gh.side_effect = RuntimeError("HTTP 403: Forbidden")
        passed, msg = check_pat_contents_permission("owner/repo")
        assert passed is False
        assert "Contents" in msg
        assert "Update the PAT" in msg


def test_pat_issues_permission_pass():
    """Pass when issues API call succeeds."""
    with patch("repomatic.github.token.run_gh_command") as mock_gh:
        mock_gh.return_value = "[]"
        passed, msg = check_pat_issues_permission("owner/repo")
        assert passed is True
        assert "Issues" in msg


def test_pat_issues_permission_fail():
    """Fail when issues API call raises with HTTP 403."""
    with (
        patch("repomatic.github.token.run_gh_command") as mock_gh,
        patch("repomatic.github.token.status_annotation", return_value=""),
    ):
        mock_gh.side_effect = RuntimeError("HTTP 403: Forbidden")
        passed, msg = check_pat_issues_permission("owner/repo")
        assert passed is False
        assert "Issues" in msg
        assert "Update the PAT" in msg


def test_pat_pull_requests_permission_pass():
    """Pass when pulls API call succeeds."""
    with patch("repomatic.github.token.run_gh_command") as mock_gh:
        mock_gh.return_value = "[]"
        passed, msg = check_pat_pull_requests_permission("owner/repo")
        assert passed is True
        assert "Pull requests" in msg


def test_pat_pull_requests_permission_fail():
    """Fail when pulls API call raises with HTTP 403."""
    with (
        patch("repomatic.github.token.run_gh_command") as mock_gh,
        patch("repomatic.github.token.status_annotation", return_value=""),
    ):
        mock_gh.side_effect = RuntimeError("HTTP 403: Forbidden")
        passed, msg = check_pat_pull_requests_permission("owner/repo")
        assert passed is False
        assert "Pull requests" in msg
        assert "Update the PAT" in msg


def test_pat_vulnerability_alerts_permission_pass():
    """Pass when vulnerability-alerts API call succeeds."""
    with patch("repomatic.github.token.run_gh_command") as mock_gh:
        mock_gh.return_value = ""
        passed, msg = check_pat_vulnerability_alerts_permission("owner/repo")
        assert passed is True
        assert "Dependabot alerts" in msg


def test_pat_vulnerability_alerts_permission_fail():
    """Fail when vulnerability-alerts API call raises with HTTP 403."""
    with (
        patch("repomatic.github.token.run_gh_command") as mock_gh,
        patch("repomatic.github.token.status_annotation", return_value=""),
    ):
        mock_gh.side_effect = RuntimeError("HTTP 403: Forbidden")
        passed, msg = check_pat_vulnerability_alerts_permission("owner/repo")
        assert passed is False
        assert "Dependabot alerts" in msg
        assert "Update the PAT" in msg


def test_pat_vulnerability_alerts_permission_404():
    """A 404 is reported as 'alerts not enabled', not as a missing permission."""
    with (
        patch("repomatic.github.token.run_gh_command") as mock_gh,
        patch("repomatic.github.token.status_annotation", return_value=""),
    ):
        mock_gh.side_effect = RuntimeError("HTTP 404: Not Found")
        passed, msg = check_pat_vulnerability_alerts_permission("owner/repo")
        assert passed is False
        assert "not enabled" in msg
        assert "--method PUT" in msg


def test_pat_workflows_permission_pass():
    """Pass when workflows API call succeeds."""
    with patch("repomatic.github.token.run_gh_command") as mock_gh:
        mock_gh.return_value = ""
        passed, msg = check_pat_workflows_permission("owner/repo")
        assert passed is True
        assert "Workflows" in msg


def test_pat_workflows_permission_fail():
    """Fail when workflows API call raises with HTTP 403."""
    with (
        patch("repomatic.github.token.run_gh_command") as mock_gh,
        patch("repomatic.github.token.status_annotation", return_value=""),
    ):
        mock_gh.side_effect = RuntimeError("HTTP 403: Forbidden")
        passed, msg = check_pat_workflows_permission("owner/repo")
        assert passed is False
        assert "Workflows" in msg
        assert "Update the PAT" in msg


def test_pat_check_non_403_surfaces_raw_error():
    """A 401 from an upstream incident surfaces the raw stderr, not a scope hint."""
    with (
        patch("repomatic.github.token.run_gh_command") as mock_gh,
        patch("repomatic.github.token.status_annotation", return_value=""),
    ):
        mock_gh.side_effect = RuntimeError("HTTP 401: Requires authentication")
        passed, msg = check_pat_contents_permission("owner/repo")
        assert passed is False
        assert "GitHub API call failed" in msg
        assert "401" in msg
        assert "Update the PAT" not in msg


def test_pat_check_annotates_with_github_status_incident():
    """An active githubstatus.com incident is appended to the failure message."""
    with (
        patch("repomatic.github.token.run_gh_command") as mock_gh,
        patch(
            "repomatic.github.token.status_annotation",
            return_value="GitHub Status reports an active incident (major): "
            "Partial System Outage. See https://www.githubstatus.com for details.",
        ),
    ):
        mock_gh.side_effect = RuntimeError("HTTP 401: Requires authentication")
        passed, msg = check_pat_contents_permission("owner/repo")
        assert passed is False
        assert "active incident" in msg
        assert "githubstatus.com" in msg


def test_pat_stale_statuses_permission_present():
    """Warn when a 422 proves the token still grants Commit statuses."""
    with patch("repomatic.lint_repo.run_gh_command") as mock_gh:
        mock_gh.side_effect = RuntimeError("No commit found for SHA: 000... (HTTP 422)")
        warning, _msg = check_pat_stale_statuses_permission("owner/repo")
        assert warning is not None
        assert "Commit statuses" in warning
        assert "least privilege" in warning


@pytest.mark.parametrize(
    "stderr",
    (
        "Resource not accessible by personal access token (HTTP 403)",
        "Not Found (HTTP 404)",
        "network unreachable",
    ),
)
def test_pat_stale_statuses_permission_absent_or_indeterminate(stderr):
    """Stay silent when the token lacks the scope (403) or the probe is inconclusive."""
    with patch("repomatic.lint_repo.run_gh_command") as mock_gh:
        mock_gh.side_effect = RuntimeError(stderr)
        warning, _msg = check_pat_stale_statuses_permission("owner/repo")
        assert warning is None


def test_pat_stale_statuses_permission_unexpected_success():
    """An impossible 2xx from the null-SHA probe stays silent rather than warning."""
    with patch("repomatic.lint_repo.run_gh_command") as mock_gh:
        mock_gh.return_value = ""
        warning, _msg = check_pat_stale_statuses_permission("owner/repo")
        assert warning is None


# --- PAT checks in run_repo_lint ---


def test_pat_checks_skipped_without_pat(capsys):
    """PAT checks are skipped when has_pat is False."""
    exit_code = run_repo_lint(has_pat=False)
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "skipped (no REPOMATIC_PAT)" in captured.out


def _all_pass_pat_results() -> PatPermissionResults:
    """Build a PatPermissionResults where every check passes."""
    return PatPermissionResults(
        contents=(True, "Contents: token has access"),
        issues=(True, "Issues: token has access"),
        pull_requests=(True, "Pull requests: token has access"),
        vulnerability_alerts=(
            True,
            "Dependabot alerts: token has access, alerts enabled",
        ),
        workflows=(True, "Workflows: token has access"),
    )


def _all_fail_pat_results() -> PatPermissionResults:
    """Build a PatPermissionResults where every check fails."""
    return PatPermissionResults(
        contents=(False, "Cannot access repository contents."),
        issues=(False, "Cannot access repository issues."),
        pull_requests=(False, "Cannot access repository pull requests."),
        vulnerability_alerts=(
            False,
            "Token lacks 'Dependabot alerts: Read-only' permission.",
        ),
        workflows=(False, "Cannot access repository workflows."),
    )


def test_pat_checks_all_pass(capsys):
    """Return 0 when all PAT capability checks pass."""
    with (
        patch("repomatic.lint_repo.run_gh_command", return_value=""),
        patch(
            "repomatic.lint_repo.check_all_pat_permissions",
            return_value=_all_pass_pat_results(),
        ),
    ):
        exit_code = run_repo_lint(
            repo="owner/repo",
            has_pat=True,
        )
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "Contents: token has access" in captured.out
        assert "Issues: token has access" in captured.out
        assert "Pull requests: token has access" in captured.out
        assert "Dependabot alerts: token has access" in captured.out
        assert "Workflows: token has access" in captured.out


def test_pat_checks_fail_on_missing_permission(capsys):
    """Return 1 when a PAT capability check fails."""
    with (
        patch("repomatic.lint_repo.run_gh_command", return_value=""),
        patch(
            "repomatic.lint_repo.check_all_pat_permissions",
            return_value=_all_fail_pat_results(),
        ),
    ):
        exit_code = run_repo_lint(
            repo="owner/repo",
            has_pat=True,
        )
        assert exit_code == 1
        captured = capsys.readouterr()
        assert "::error::" in captured.out


def test_pypi_trusted_publisher_no_package_name():
    """Skip when package name is not provided."""
    passed, msg = check_pypi_trusted_publisher("owner/repo", None)
    assert passed is None
    assert "skipped" in msg
    assert "no package name" in msg


def test_pypi_trusted_publisher_no_release():
    """Skip when the package has no published version."""
    with patch(
        "repomatic.lint_repo.get_latest_release_file",
        return_value=None,
    ):
        passed, msg = check_pypi_trusted_publisher("owner/cherries", "cherries")
    assert passed is None
    assert "no released version" in msg
    assert "cherries" in msg


def test_pypi_trusted_publisher_no_provenance():
    """Indeterminate when provenance fetch fails (pre-OIDC release)."""
    with (
        patch(
            "repomatic.lint_repo.get_latest_release_file",
            return_value=("1.2.3", "cherries-1.2.3-py3-none-any.whl"),
        ),
        patch(
            "repomatic.lint_repo.get_trusted_publishers",
            return_value=None,
        ),
    ):
        passed, msg = check_pypi_trusted_publisher("owner/cherries", "cherries")
    assert passed is None
    assert "no provenance" in msg
    assert "API token" in msg


def test_pypi_trusted_publisher_empty_bundles():
    """Indeterminate when provenance contains no publisher bundles."""
    with (
        patch(
            "repomatic.lint_repo.get_latest_release_file",
            return_value=("1.2.3", "cherries-1.2.3-py3-none-any.whl"),
        ),
        patch(
            "repomatic.lint_repo.get_trusted_publishers",
            return_value=[],
        ),
    ):
        passed, msg = check_pypi_trusted_publisher("owner/cherries", "cherries")
    assert passed is None
    assert "no publisher bundles" in msg


def test_pypi_trusted_publisher_match():
    """Pass when a publisher bundle names this repo and `release.yaml`."""
    with (
        patch(
            "repomatic.lint_repo.get_latest_release_file",
            return_value=("1.2.3", "cherries-1.2.3-py3-none-any.whl"),
        ),
        patch(
            "repomatic.lint_repo.get_trusted_publishers",
            return_value=[
                TrustedPublisher(
                    kind="GitHub",
                    repository="owner/cherries",
                    workflow="release.yaml",
                    environment=None,
                ),
            ],
        ),
    ):
        passed, msg = check_pypi_trusted_publisher("owner/cherries", "cherries")
    assert passed is True
    assert "matches" in msg
    assert "owner/cherries" in msg


def test_pypi_trusted_publisher_workflow_mismatch():
    """Fail when provenance names a different workflow."""
    with (
        patch(
            "repomatic.lint_repo.get_latest_release_file",
            return_value=("1.2.3", "cherries-1.2.3-py3-none-any.whl"),
        ),
        patch(
            "repomatic.lint_repo.get_trusted_publishers",
            return_value=[
                TrustedPublisher(
                    kind="GitHub",
                    repository="owner/cherries",
                    workflow="publish.yaml",
                    environment=None,
                ),
            ],
        ),
    ):
        passed, msg = check_pypi_trusted_publisher("owner/cherries", "cherries")
    assert passed is False
    assert "mismatch" in msg
    assert "publish.yaml" in msg
    assert "https://pypi.org/manage/project/cherries/settings/publishing/" in msg


def test_pypi_trusted_publisher_repository_mismatch():
    """Fail when provenance names a different repository (e.g., upstream)."""
    with (
        patch(
            "repomatic.lint_repo.get_latest_release_file",
            return_value=("1.2.3", "cherries-1.2.3-py3-none-any.whl"),
        ),
        patch(
            "repomatic.lint_repo.get_trusted_publishers",
            return_value=[
                TrustedPublisher(
                    kind="GitHub",
                    repository="upstream/orchard",
                    workflow="release.yaml",
                    environment=None,
                ),
            ],
        ),
    ):
        passed, msg = check_pypi_trusted_publisher("owner/cherries", "cherries")
    assert passed is False
    assert "upstream/orchard" in msg


def test_check_test_matrix_excludes_flags_stale(tmp_path, monkeypatch):
    """A stale exclude (a renamed runner) is reported as a warning."""
    pyproject_file = tmp_path / "pyproject.toml"
    pyproject_file.write_text(
        '[project]\nname = "p"\nversion = "1.0.0"\n\n'
        "[tool.repomatic.test-matrix]\n"
        'exclude = [{os = "macos-15-intel"}]\n'
    )
    monkeypatch.setattr(Metadata, "pyproject_path", pyproject_file)

    results = check_test_matrix_excludes()
    assert any(warning and "macos-15-intel" in warning for warning, _ in results)


def test_check_test_matrix_excludes_clean(tmp_path, monkeypatch):
    """A live exclude produces no warning."""
    pyproject_file = tmp_path / "pyproject.toml"
    pyproject_file.write_text(
        '[project]\nname = "p"\nversion = "1.0.0"\n\n'
        "[tool.repomatic.test-matrix]\n"
        'exclude = [{os = "ubuntu-slim"}]\n'
    )
    monkeypatch.setattr(Metadata, "pyproject_path", pyproject_file)

    results = check_test_matrix_excludes()
    assert all(warning is None for warning, _ in results)


# A SHA-pinned thin-caller `uses:` ref to the upstream toolkit, declaring v6.29.0.
_UPSTREAM_REF = (
    "jobs:\n"
    "  lint:\n"
    "    uses: kdeldycke/repomatic/.github/workflows/lint.yaml"
    "@1234567890abcdef1234567890abcdef12345678 # v6.29.0\n"
)


def test_inline_pins_match_upstream_flags_lagging_pin(tmp_path):
    """An inline pin behind the uses: ref version is a fatal error."""
    (tmp_path / "lint.yaml").write_text(_UPSTREAM_REF, encoding="UTF-8")
    (tmp_path / "tests.yaml").write_text(
        "      - run: uvx --no-progress 'repomatic==6.28.1' metadata\n",
        encoding="UTF-8",
    )
    error, _ = check_inline_pins_match_upstream(tmp_path)
    assert error is not None
    assert "6.28.1" in error
    assert "6.29.0" in error
    assert "tests.yaml" in error


def test_inline_pins_match_upstream_accepts_synced_pin(tmp_path):
    """An inline pin equal to the uses: ref version passes."""
    (tmp_path / "lint.yaml").write_text(_UPSTREAM_REF, encoding="UTF-8")
    (tmp_path / "tests.yaml").write_text(
        "      - run: uvx --no-progress 'repomatic==6.29.0' metadata\n",
        encoding="UTF-8",
    )
    error, msg = check_inline_pins_match_upstream(tmp_path)
    assert error is None
    assert "match" in msg


def test_inline_pins_match_upstream_skips_without_refs(tmp_path):
    """The canonical repo (local `--from .`, no upstream refs) has nothing to check."""
    (tmp_path / "tests.yaml").write_text(
        "      - run: uvx --no-progress --from . repomatic metadata\n",
        encoding="UTF-8",
    )
    error, msg = check_inline_pins_match_upstream(tmp_path)
    assert error is None
    assert "nothing to compare" in msg
