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

from unittest.mock import patch

import pytest

from repomatic.github.token import (
    PAT_PERMISSION_PROBES,
    PatPermissionResults,
    probe_pat_permission,
)
from repomatic.lint_repo import (
    check_branch_ruleset_on_default,
    check_description_matches,
    check_funding_file,
    check_immutable_releases,
    check_inline_pins_match_upstream,
    check_package_name_vs_repo,
    check_pat_stale_statuses_permission,
    check_pypi_trusted_publisher,
    check_sha_pinning_required,
    check_stale_draft_releases,
    check_test_matrix_excludes,
    check_topics_subset_of_keywords,
    check_website_for_sphinx,
    check_workflow_permissions,
    get_repo_metadata,
    run_repo_lint,
)
from repomatic.metadata import Metadata
from repomatic.pypi import TrustedPublisher
from tests.conftest import all_pass_pat_results


def test_successful_fetch():
    """Fetch and parse repo metadata."""
    with patch("repomatic.lint_repo.gh_api_json") as mock_gh:
        mock_gh.return_value = {
            "homepageUrl": "https://example.com",
            "description": "A package",
        }
        result = get_repo_metadata("owner/repo")
        assert result == {
            "homepageUrl": "https://example.com",
            "description": "A package",
        }


def test_empty_fields():
    """Handle empty fields."""
    with patch("repomatic.lint_repo.gh_api_json") as mock_gh:
        mock_gh.return_value = {"homepageUrl": "", "description": ""}
        result = get_repo_metadata("owner/repo")
        assert result == {"homepageUrl": None, "description": None}


def test_unreadable_metadata():
    """Both fields are None when the payload cannot be read.

    `gh_api_json` collapses a failed call and unparsable output into one `None`,
    so there is a single outcome to assert on here.
    """
    with patch("repomatic.lint_repo.gh_api_json") as mock_gh:
        mock_gh.return_value = None
        result = get_repo_metadata("owner/repo")
        assert result == {"homepageUrl": None, "description": None}


def test_names_match():
    """No warning when names match."""
    result = check_package_name_vs_repo("my-package", "my-package")
    assert result.passed is True
    assert "matches" in result.message


def test_names_differ():
    """Warning when names differ."""
    result = check_package_name_vs_repo("my-package", "my-repo")
    assert result.passed is False
    assert "differs" in result.message
    assert "my-package" in result.message
    assert "my-repo" in result.message


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
    result = check_website_for_sphinx(
        "owner/repo", is_sphinx=True, homepage_url="https://docs.example.com"
    )
    assert result.passed is True
    assert "https://docs.example.com" in result.message


def test_sphinx_without_website():
    """Warning when Sphinx project has no website."""
    result = check_website_for_sphinx("owner/repo", is_sphinx=True, homepage_url=None)
    assert result.passed is False
    assert "Sphinx" in result.message
    assert "not set" in result.message


def test_sphinx_fetches_metadata():
    """Fetch metadata when homepage_url not provided."""
    with patch("repomatic.lint_repo.get_repo_metadata") as mock_get:
        mock_get.return_value = {"homepageUrl": "https://example.com"}
        result = check_website_for_sphinx("owner/repo", is_sphinx=True)
        assert result.passed is True
        mock_get.assert_called_once_with("owner/repo")


def test_descriptions_match():
    """No error when descriptions match."""
    result = check_description_matches(
        "owner/repo",
        project_description="A cool package",
        repo_description="A cool package",
    )
    assert result.passed is True
    assert "matches" in result.message


def test_descriptions_differ():
    """Error when descriptions differ."""
    result = check_description_matches(
        "owner/repo",
        project_description="A cool package",
        repo_description="Different description",
    )
    assert result.passed is False
    assert "!=" in result.message


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
        result = check_description_matches(
            "owner/repo", project_description="A cool package"
        )
        assert result.passed is True
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
        result = check_topics_subset_of_keywords(
            "owner/repo", keywords=["python", "automation", "cli"]
        )
        assert result.passed is True
        assert "2" in result.message


def test_topics_extra_not_in_keywords():
    """Warning when topics exist that are not in keywords."""
    with patch("repomatic.lint_repo.run_gh_command") as mock_gh:
        mock_gh.return_value = "python\nunknown-topic\n"
        result = check_topics_subset_of_keywords(
            "owner/repo", keywords=["python", "cli"]
        )
        assert result.passed is False
        assert "unknown-topic" in result.message


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


def _graphql_response(*, is_fork: bool = False, has_sponsors: bool = True) -> dict:
    """Build a mock GraphQL payload for funding checks."""
    return {
        "data": {
            "repository": {"isFork": is_fork},
            "repositoryOwner": {"hasSponsorsListing": has_sponsors},
        },
    }


def test_funding_file_exists(tmp_path, monkeypatch):
    """No warning when funding file already exists."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "FUNDING.yml").write_text(
        "github: owner\n", encoding="utf-8"
    )
    result = check_funding_file("owner/repo")
    assert result.passed is True
    assert "found" in result.message


def test_funding_file_exists_lowercase(tmp_path, monkeypatch):
    """Detect funding file regardless of case."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "funding.yml").write_text(
        "github: owner\n", encoding="utf-8"
    )
    result = check_funding_file("owner/repo")
    assert result.passed is True
    assert "found" in result.message


def test_funding_missing_with_sponsors(tmp_path, monkeypatch):
    """Warning when owner has sponsors but no funding file."""
    monkeypatch.chdir(tmp_path)
    with patch("repomatic.lint_repo.gh_api_json") as mock_gh:
        mock_gh.return_value = _graphql_response(has_sponsors=True)
        result = check_funding_file("owner/repo")
        assert result.passed is False
        assert "FUNDING.yml" in result.message
        assert "Sponsor" in result.message


def test_funding_skipped_for_fork(tmp_path, monkeypatch):
    """Skip funding check for forked repositories."""
    monkeypatch.chdir(tmp_path)
    with patch("repomatic.lint_repo.gh_api_json") as mock_gh:
        mock_gh.return_value = _graphql_response(is_fork=True)
        warning, msg = check_funding_file("owner/repo")
        assert warning is None
        assert "fork" in msg


def test_funding_skipped_no_sponsors(tmp_path, monkeypatch):
    """Skip when owner has no GitHub Sponsors listing."""
    monkeypatch.chdir(tmp_path)
    with patch("repomatic.lint_repo.gh_api_json") as mock_gh:
        mock_gh.return_value = _graphql_response(has_sponsors=False)
        warning, msg = check_funding_file("owner/repo")
        assert warning is None
        assert "no GitHub Sponsors" in msg


@pytest.mark.parametrize(
    ("workflow", "expect_fail", "needle"),
    [
        pytest.param(
            "on: push\n"
            "permissions: {}\n"
            "jobs:\n"
            "  build:\n"
            "    uses: ./.github/workflows/_build.yaml\n",
            True,
            "`build`",
            id="starved-reusable-call",
        ),
        pytest.param(
            "on: push\n"
            "permissions: {}\n"
            "jobs:\n"
            "  build:\n"
            "    uses: ./.github/workflows/_build.yaml\n"
            "    permissions:\n"
            "      contents: write\n",
            False,
            None,
            id="granted-reusable-call",
        ),
        pytest.param(
            "on: push\njobs:\n  build:\n    uses: ./.github/workflows/_build.yaml\n",
            False,
            None,
            id="reusable-call-repo-default",
        ),
        pytest.param(
            "on: push\n"
            "jobs:\n"
            "  build:\n"
            "    runs-on: ubuntu-24.04\n"
            "    steps:\n"
            "      - run: echo apricot\n",
            True,
            "top-level `permissions`",
            id="custom-steps-missing-key",
        ),
    ],
)
def test_workflow_permissions(tmp_path, monkeypatch, workflow, expect_fail, needle):
    """Flag starved reusable calls and custom-step workflows missing the key."""
    monkeypatch.chdir(tmp_path)
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yaml").write_text(workflow, encoding="utf-8")
    failures = [r for r in check_workflow_permissions() if r.passed is False]
    if expect_fail:
        assert failures
        assert any(needle in r.message for r in failures)
    else:
        assert not failures


def test_funding_unreadable_payload(tmp_path, monkeypatch):
    """Skip gracefully when the GraphQL payload cannot be read."""
    monkeypatch.chdir(tmp_path)
    with patch("repomatic.lint_repo.gh_api_json") as mock_gh:
        mock_gh.return_value = None
        warning, msg = check_funding_file("owner/repo")
        assert warning is None
        assert "skipped" in msg


# --- Stale draft releases check unit tests ---


def test_stale_drafts_detected():
    """Warn about draft releases that are not dev pre-releases."""
    with patch("repomatic.lint_repo.gh_api_json") as mock_gh:
        mock_gh.return_value = [
            {"tagName": "v6.1.2", "isDraft": True},
            {"tagName": "v6.2.0", "isDraft": False},
            {"tagName": "v6.3.0.dev0", "isDraft": True},
        ]
        result = check_stale_draft_releases("owner/repo")
        assert result.passed is False
        assert "v6.1.2" in result.message
        assert "v6.3.0.dev0" not in result.message


def test_stale_drafts_none():
    """No warning when only dev pre-release drafts exist."""
    with patch("repomatic.lint_repo.gh_api_json") as mock_gh:
        mock_gh.return_value = [
            {"tagName": "v6.2.0", "isDraft": False},
            {"tagName": "v6.3.0.dev0", "isDraft": True},
        ]
        result = check_stale_draft_releases("owner/repo")
        assert result.passed is True
        assert "No stale" in result.message


def test_stale_drafts_unreadable_payload():
    """Skip gracefully when the release list cannot be read."""
    with patch("repomatic.lint_repo.gh_api_json") as mock_gh:
        mock_gh.return_value = None
        warning, msg = check_stale_draft_releases("owner/repo")
        assert warning is None
        assert "skipped" in msg


def test_stale_drafts_multiple():
    """List all stale draft tags in the warning."""
    with patch("repomatic.lint_repo.gh_api_json") as mock_gh:
        mock_gh.return_value = [
            {"tagName": "v6.1.2", "isDraft": True},
            {"tagName": "v6.2.0-rc1", "isDraft": True},
        ]
        result = check_stale_draft_releases("owner/repo")
        assert result.passed is False
        assert "v6.1.2" in result.message
        assert "v6.2.0-rc1" in result.message


# --- SHA pinning required check unit tests ---


def test_sha_pinning_required_enabled():
    """Pass when the repository requires SHA pinning for Actions."""
    with patch("repomatic.lint_repo.gh_api_json") as mock_gh:
        mock_gh.return_value = {
            "enabled": True,
            "allowed_actions": "all",
            "sha_pinning_required": True,
        }
        result = check_sha_pinning_required("owner/repo")
        assert result.passed is True
        assert "enabled" in result.message


def test_sha_pinning_required_disabled():
    """Warn with a fix link when SHA pinning is not required."""
    with patch("repomatic.lint_repo.gh_api_json") as mock_gh:
        mock_gh.return_value = {
            "enabled": True,
            "allowed_actions": "all",
            "sha_pinning_required": False,
        }
        result = check_sha_pinning_required("owner/repo")
        assert result.passed is False
        assert "owner/repo/settings/actions" in result.message


def test_sha_pinning_required_unreadable_payload():
    """Skip gracefully when the permissions payload cannot be read."""
    with patch("repomatic.lint_repo.gh_api_json") as mock_gh:
        mock_gh.return_value = None
        result = check_sha_pinning_required("owner/repo")
        assert result.passed is None
        assert "skipped" in result.message


# --- PAT capability check unit tests ---


@pytest.mark.parametrize("probe", PAT_PERMISSION_PROBES, ids=lambda p: p.field)
def test_pat_permission_probe_pass(probe):
    """Pass with the probe's success message when the API call succeeds."""
    with patch("repomatic.github.token.run_gh_command") as mock_gh:
        mock_gh.return_value = ""
        passed, msg = probe_pat_permission("owner/repo", probe)
        assert passed is True
        assert msg == probe.success
        probed_endpoint = mock_gh.call_args.args[0][1]
        assert probed_endpoint == probe.endpoint.format(repo="owner/repo")


@pytest.mark.parametrize("probe", PAT_PERMISSION_PROBES, ids=lambda p: p.field)
def test_pat_permission_probe_fail_403(probe):
    """Fail with the missing-permission message when the API call 403s."""
    with (
        patch("repomatic.github.token.run_gh_command") as mock_gh,
        patch("repomatic.github.token.status_annotation", return_value=""),
    ):
        mock_gh.side_effect = RuntimeError("HTTP 403: Forbidden")
        passed, msg = probe_pat_permission("owner/repo", probe)
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
    with (
        patch("repomatic.github.token.run_gh_command") as mock_gh,
        patch("repomatic.github.token.status_annotation", return_value=""),
    ):
        mock_gh.side_effect = RuntimeError("HTTP 404: Not Found")
        passed, msg = probe_pat_permission("owner/repo", vuln_probe)
        assert passed is False
        assert "not enabled" in msg
        assert "--method PUT" in msg


def test_pat_check_non_403_surfaces_raw_error():
    """A 401 from an upstream incident surfaces the raw stderr, not a scope hint."""
    with (
        patch("repomatic.github.token.run_gh_command") as mock_gh,
        patch("repomatic.github.token.status_annotation", return_value=""),
    ):
        mock_gh.side_effect = RuntimeError("HTTP 401: Requires authentication")
        passed, msg = probe_pat_permission("owner/repo", PAT_PERMISSION_PROBES[0])
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
        passed, msg = probe_pat_permission("owner/repo", PAT_PERMISSION_PROBES[0])
        assert passed is False
        assert "active incident" in msg
        assert "githubstatus.com" in msg


def test_pat_stale_statuses_permission_present():
    """Warn when a 422 proves the token still grants Commit statuses."""
    with patch("repomatic.lint_repo.run_gh_command") as mock_gh:
        mock_gh.side_effect = RuntimeError("No commit found for SHA: 000... (HTTP 422)")
        result = check_pat_stale_statuses_permission("owner/repo")
        assert result.passed is False
        assert "Commit statuses" in result.message
        assert "least privilege" in result.message


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
        result = check_pat_stale_statuses_permission("owner/repo")
        assert result.passed is not False


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
            return_value=all_pass_pat_results(),
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
        'exclude = [{os = "macos-15-intel"}]\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(Metadata, "pyproject_path", pyproject_file)

    results = check_test_matrix_excludes()
    assert any(r.passed is False and "macos-15-intel" in r.message for r in results)


def test_check_test_matrix_excludes_clean(tmp_path, monkeypatch):
    """A live exclude produces no warning."""
    pyproject_file = tmp_path / "pyproject.toml"
    pyproject_file.write_text(
        '[project]\nname = "p"\nversion = "1.0.0"\n\n'
        "[tool.repomatic.test-matrix]\n"
        'exclude = [{os = "ubuntu-slim"}]\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(Metadata, "pyproject_path", pyproject_file)

    results = check_test_matrix_excludes()
    assert all(r.passed is not False for r in results)


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
    result = check_inline_pins_match_upstream(tmp_path)
    assert result.passed is False
    assert "6.28.1" in result.message
    assert "6.29.0" in result.message
    assert "tests.yaml" in result.message


def test_inline_pins_match_upstream_accepts_synced_pin(tmp_path):
    """An inline pin equal to the uses: ref version passes."""
    (tmp_path / "lint.yaml").write_text(_UPSTREAM_REF, encoding="UTF-8")
    (tmp_path / "tests.yaml").write_text(
        "      - run: uvx --no-progress 'repomatic==6.29.0' metadata\n",
        encoding="UTF-8",
    )
    result = check_inline_pins_match_upstream(tmp_path)
    assert result.passed is True
    assert "match" in result.message


def test_inline_pins_match_upstream_skips_without_refs(tmp_path):
    """The canonical repo (local `--from .`, no upstream refs) has nothing to check."""
    (tmp_path / "tests.yaml").write_text(
        "      - run: uvx --no-progress --from . repomatic metadata\n",
        encoding="UTF-8",
    )
    error, msg = check_inline_pins_match_upstream(tmp_path)
    assert error is None
    assert "nothing to compare" in msg


# ---------------------------------------------------------------------------
# Branch ruleset check tests
# ---------------------------------------------------------------------------


def test_check_branch_ruleset_found():
    """Active branch ruleset is detected."""

    rulesets = [
        {"name": "main", "target": "branch", "enforcement": "active"},
    ]
    with patch("repomatic.lint_repo.gh_api_json", return_value=rulesets):
        passed, msg = check_branch_ruleset_on_default("owner/repo")
    assert passed is True
    assert "main" in msg


def test_check_branch_ruleset_none():
    """No branch rulesets returns failure."""

    rulesets = [
        {"name": "tags", "target": "tag", "enforcement": "active"},
    ]
    with patch("repomatic.lint_repo.gh_api_json", return_value=rulesets):
        passed, _msg = check_branch_ruleset_on_default("owner/repo")
    assert passed is False


def test_check_branch_ruleset_api_error():
    """API failure defaults to incomplete (show the step)."""

    with patch("repomatic.lint_repo.gh_api_json", return_value=None):
        passed, msg = check_branch_ruleset_on_default("owner/repo")
    assert passed is False
    assert "skipped" in msg


# --- check_immutable_releases ---------------------------------------------------


def test_check_immutable_releases_enabled():
    """Immutable releases enabled is detected."""

    response = {"enabled": True, "enforced_by_owner": False}
    with patch("repomatic.lint_repo.gh_api_json", return_value=response):
        passed, msg = check_immutable_releases("owner/repo")
    assert passed is True
    assert "enabled" in msg


def test_check_immutable_releases_disabled():
    """Immutable releases disabled returns failure."""

    response = {"enabled": False, "enforced_by_owner": False}
    with patch("repomatic.lint_repo.gh_api_json", return_value=response):
        passed, msg = check_immutable_releases("owner/repo")
    assert passed is False
    assert "not enabled" in msg


def test_check_immutable_releases_api_error():
    """API failure returns None (indeterminate), not False."""

    with patch("repomatic.lint_repo.gh_api_json", return_value=None):
        passed, msg = check_immutable_releases("owner/repo")
    assert passed is None
    assert "skipped" in msg
