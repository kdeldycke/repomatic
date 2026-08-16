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

import inspect
from pathlib import Path
from unittest.mock import patch

import pytest

from repomatic import lint_repo
from repomatic.cli import metadata as metadata_command
from repomatic.github.token import PAT_PERMISSION_PROBES, probe_pat_permission
from repomatic.lint_repo import (
    REPO_CHECKS,
    CheckResult,
    LintContext,
    check_branch_ruleset_on_default,
    check_description_matches,
    check_funding_file,
    check_immutable_releases,
    check_inline_pins_match_upstream,
    check_install_guide_downloads,
    check_metadata_keys,
    check_package_name_vs_repo,
    check_pat_stale_statuses_permission,
    check_pr_templates,
    check_pypi_trusted_publisher,
    check_python_version_consistency,
    check_runner_images,
    check_self_pin_cooldown_exemption,
    check_setup_uv_version_pin,
    check_sha_pinning_required,
    check_stale_draft_releases,
    check_test_matrix_excludes,
    check_topics_subset_of_keywords,
    check_website_for_sphinx,
    check_workflow_permissions,
    documentation_url,
    get_repo_metadata,
    literal_runners,
    run_repo_lint,
)
from repomatic.matrix_axes import UNSTABLE_PYTHON_VERSIONS
from repomatic.metadata import METADATA_VALUE_OPTIONS
from repomatic.prepare_release import SELF_PIN_COOLDOWN_EXEMPTION
from repomatic.pypi import TrustedPublisher
from repomatic.registry import INSTALL_GUIDE_PATH
from tests.conftest import metadata_from_pyproject, pat_results


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


@pytest.mark.parametrize(
    ("project_urls", "expected"),
    (
        (None, None),
        ({}, None),
        ({"Homepage": "https://papaya.example"}, None),
        (
            {"Documentation": "https://docs.papaya.example"},
            "https://docs.papaya.example",
        ),
        # PEP 621 fixes neither the case nor the wording of the key.
        (
            {"documentation": "https://docs.papaya.example"},
            "https://docs.papaya.example",
        ),
        ({"DOCS": "https://docs.papaya.example"}, "https://docs.papaya.example"),
        # `Documentation` outranks `Docs` when a project declares both.
        (
            {"Docs": "https://kiwi.example", "Documentation": "https://papaya.example"},
            "https://papaya.example",
        ),
        # An empty value is not a declaration.
        (
            {"Documentation": "  ", "Docs": "https://papaya.example"},
            "https://papaya.example",
        ),
    ),
)
def test_documentation_url(project_urls, expected):
    """Resolve the documentation site a project declares in `[project.urls]`."""
    assert documentation_url(project_urls) == expected


@pytest.mark.parametrize(
    ("homepage_url", "docs_url"),
    (
        # GitHub stores the trailing slash a browser appends, while
        # `[project.urls]` is usually written without one.
        ("https://papaya.example/", "https://papaya.example"),
        ("https://papaya.example", "https://papaya.example/"),
        ("https://kiwi.github.io/papaya/", "https://kiwi.github.io/papaya"),
        # Scheme and host are case-insensitive per RFC 3986.
        ("HTTPS://Papaya.Example", "https://papaya.example"),
    ),
)
def test_sphinx_website_matches_docs_url(homepage_url, docs_url):
    """Pass when the website field and the declared docs URL name one address."""
    result = check_website_for_sphinx(
        "owner/repo", is_sphinx=True, homepage_url=homepage_url, docs_url=docs_url
    )
    assert result.passed is True
    assert "matches" in result.message


@pytest.mark.parametrize(
    ("homepage_url", "docs_url"),
    (
        # The move this check exists to catch: the documentation gained a
        # domain and the sidebar kept pointing at the origin it left.
        ("https://kiwi.github.io/papaya", "https://papaya.example"),
        # A path's case is significant, unlike a host's.
        ("https://papaya.example/Docs", "https://papaya.example/docs"),
        # Two origins, not one.
        ("http://papaya.example", "https://papaya.example"),
        ("https://papaya.example/docs", "https://papaya.example/manual"),
    ),
)
def test_sphinx_website_differs_from_docs_url(homepage_url, docs_url):
    """Fail when the website field names something other than the docs URL."""
    result = check_website_for_sphinx(
        "owner/repo", is_sphinx=True, homepage_url=homepage_url, docs_url=docs_url
    )
    assert result.passed is False
    assert homepage_url in result.message
    assert docs_url in result.message


def test_sphinx_website_without_declared_docs_url():
    """Keep the presence-only check when the project declares no docs URL."""
    result = check_website_for_sphinx(
        "owner/repo",
        is_sphinx=True,
        homepage_url="https://kiwi.github.io/papaya",
        docs_url=None,
    )
    assert result.passed is True
    assert "is set" in result.message


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


def test_all_checks_pass():
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


def test_website_docs_url_mismatch_warning(capsys):
    """A website field pointing away from the docs URL warns without failing."""
    with patch("repomatic.lint_repo.get_repo_metadata") as mock_get:
        mock_get.return_value = {
            "homepageUrl": "https://kiwi.github.io/papaya",
            "description": None,
        }
        exit_code = run_repo_lint(
            is_sphinx=True,
            docs_url="https://papaya.example",
            repo="owner/repo",
        )
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "::warning::" in captured.out
        assert "https://papaya.example" in captured.out


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


@pytest.mark.parametrize(
    ("site_deploy", "is_sphinx", "has_token", "has_account", "expected"),
    (
        pytest.param(
            "github-pages", True, False, False, None, id="other-target-silent"
        ),
        pytest.param(
            "cloudflare-pages",
            True,
            False,
            False,
            "CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN not configured",
            id="neither-secret",
        ),
        pytest.param(
            "cloudflare-pages",
            # No Sphinx at all: a site built by the repository's own workflow
            # (a Pelican blog, a hand-rolled tree) needs the same credentials,
            # so declaring the target is the whole opt-in.
            False,
            False,
            False,
            "CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN not configured",
            id="non-sphinx-site-still-checked",
        ),
        pytest.param(
            "cloudflare-pages",
            True,
            True,
            False,
            "CLOUDFLARE_ACCOUNT_ID not configured",
            id="account-id-missing",
        ),
        pytest.param(
            "cloudflare-pages",
            True,
            False,
            True,
            "CLOUDFLARE_API_TOKEN not configured",
            id="token-missing",
        ),
        pytest.param(
            "cloudflare-pages",
            True,
            True,
            True,
            "✓ Cloudflare Pages credentials are configured.",
            id="both-secrets",
        ),
    ),
)
def test_cloudflare_secrets_check(
    capsys, site_deploy, is_sphinx, has_token, has_account, expected
):
    """The Cloudflare check fires only on its target, and names what is missing.

    Setting one value and forgetting the other is the common way to arrive
    here, so the half already in place must not be reported as missing.
    """
    exit_code = run_repo_lint(
        is_sphinx=is_sphinx,
        site_deploy=site_deploy,
        has_cloudflare_api_token=has_token,
        has_cloudflare_account_id=has_account,
    )
    assert exit_code == 0
    captured = capsys.readouterr()
    if expected is None:
        assert "CLOUDFLARE" not in captured.out
    else:
        assert expected in captured.out


@pytest.mark.parametrize(
    ("site_deploy", "probed"),
    (
        pytest.param("github-pages", True, id="github-pages-probes"),
        pytest.param("cloudflare-pages", False, id="cloudflare-pages-skips"),
    ),
)
def test_pages_deployment_source_follows_the_deploy_target(site_deploy, probed):
    """The Pages source check is skipped for a project that deploys elsewhere.

    `GET /repos/{repo}/pages` answers `404` for a repository that never
    enabled Pages, which the check reads as indeterminate. Reported against a
    Cloudflare-hosted project that is a permanent skip line about a setting
    nobody will ever change.
    """
    with (
        patch("repomatic.lint_repo.get_repo_metadata") as mock_get,
        patch("repomatic.lint_repo.check_pages_deployment_source") as probe,
    ):
        mock_get.return_value = {"homepageUrl": None, "description": None}
        probe.return_value = CheckResult(True, "Pages source is GitHub Actions.")
        run_repo_lint(
            is_sphinx=True,
            site_deploy=site_deploy,
            repo="orchard/papaya",
        )
    assert probe.called is probed


def _lint_context_in(tmp_path, monkeypatch, **kwargs) -> LintContext:
    """A `LintContext` whose filesystem lookups resolve inside *tmp_path*."""
    monkeypatch.chdir(tmp_path)
    return LintContext(**kwargs)


def test_pages_redirects_clean_file_passes(tmp_path, monkeypatch):
    """A file the engine keeps whole reports one green line."""
    (tmp_path / "_redirects").write_text(
        "/old /new 301\n/blog/* /articles/:splat 301\n",
        encoding="UTF-8",
    )
    ctx = _lint_context_in(tmp_path, monkeypatch)
    results = list(lint_repo._pages_redirects(ctx))
    assert [result.passed for result in results] == [True]
    assert "2 rules survive" in results[0].message


def test_pages_redirects_reports_dropped_rules(tmp_path, monkeypatch):
    """Every line the engine drops surfaces with its file position.

    These are the failures `wrangler pages deploy` swallows: a duplicate
    source, a bad status, an infinite loop. Each dropped line is a redirect
    that looks committed and is dead in production.
    """
    (tmp_path / "_redirects").write_text(
        "/a /b 301\n"
        "/a /c 301\n"  # Duplicate source, first one wins silently.
        "/d /e 418\n"  # Status the engine refuses.
        "/f/ /f/index.html\n",  # Infinite loop through .html stripping.
        encoding="UTF-8",
    )
    ctx = _lint_context_in(tmp_path, monkeypatch)
    messages = [
        result.message
        for result in lint_repo._pages_redirects(ctx)
        if result.passed is False
    ]
    assert len(messages) == 3
    assert "duplicate rule" in messages[0]
    assert "Valid status codes" in messages[1]
    assert "Infinite loop" in messages[2]


def test_pages_redirects_reports_budget_abort_and_misordering(tmp_path, monkeypatch):
    """The silent kill switch: statics after a dynamic burn the 100 budget.

    The file below opens with a dynamic rule, so every following static is
    charged against the dynamic budget of 100. The 101st charged rule (line
    101, however static it looks) aborts the parse, and everything below it
    is discarded without a word from the deploy pipeline.
    """
    lines = ["/blog/* /articles/:splat 301"]
    lines += [f"/old-{index} /new-{index} 301" for index in range(100)]
    lines += ["/casualty /survivor 301"]
    (tmp_path / "_redirects").write_text("\n".join(lines) + "\n", encoding="UTF-8")
    ctx = _lint_context_in(tmp_path, monkeypatch)
    failures = [
        result.message
        for result in lint_repo._pages_redirects(ctx)
        if result.passed is False
    ]
    assert any("stops reading at line 101" in message for message in failures)
    # Lines 2 to 100 survive as dynamically-charged statics; line 101 died on
    # the budget and line 102 was never read.
    assert any("99 exact rule(s) sit below" in message for message in failures)


def test_pages_redirects_check_is_fatal_and_gated_on_the_file():
    """The roster entry must fail the run: dropped rules are already broken."""
    check = next(check for check in REPO_CHECKS if check.name == "pages-redirects")
    assert check.fatal
    context_with = LintContext()
    context_with.__dict__["redirects_files"] = [Path("_redirects")]
    context_without = LintContext()
    context_without.__dict__["redirects_files"] = []
    assert check.applies(context_with)
    assert not check.applies(context_without)


def test_pages_redirects_honours_gitignore(tmp_path, monkeypatch):
    """A generated copy of the file must not be audited, only the source.

    A static site build copies `_redirects` verbatim into its output tree, so
    without the gitignore filter every finding would be reported twice, and a
    stale build artifact could fail the lint after the source was fixed.
    """
    (tmp_path / "_redirects").write_text("/a /b 301\n", encoding="UTF-8")
    (tmp_path / "output").mkdir()
    (tmp_path / "output" / "_redirects").write_text("/a /b 301\n", encoding="UTF-8")
    (tmp_path / ".gitignore").write_text("output/\n", encoding="UTF-8")
    ctx = _lint_context_in(tmp_path, monkeypatch)
    assert ctx.redirects_files == [Path("_redirects")]


@pytest.mark.parametrize(
    ("wrangler_toml", "context_kwargs", "expected_failures"),
    (
        pytest.param(
            'name = "papaya-site"\ncompatibility_date = "2026-06-16"\n',
            {
                "site_cloudflare_project": "papaya-site",
                "site_cloudflare_compatibility_date": "2026-06-16",
            },
            [],
            id="everything-agrees",
        ),
        pytest.param(
            'name = "renamed-by-hand"\n',
            {"site_cloudflare_project": "papaya-site"},
            ["names project 'renamed-by-hand' while the deploy targets"],
            id="project-name-diverges",
        ),
        pytest.param(
            'name = "papaya"\ncompatibility_date = "2023-03-01"\n',
            {
                "repo_name": "papaya",
                "site_cloudflare_compatibility_date": "2026-06-16",
            },
            ["says compatibility_date 2023-03-01"],
            id="stale-compatibility-date",
        ),
        pytest.param(
            "compatibility_date = [broken\n",
            {},
            ["does not parse"],
            id="unparsable-file",
        ),
    ),
)
def test_wrangler_config_check(
    tmp_path, monkeypatch, wrangler_toml, context_kwargs, expected_failures
):
    """`wrangler.toml` must agree with the declared project name and date.

    The file only feeds local wrangler commands on a Direct Upload project,
    which is exactly why nothing else ever catches it lying: one project's
    compatibility date sat three years behind the live value this way.
    """
    (tmp_path / "wrangler.toml").write_text(wrangler_toml, encoding="UTF-8")
    ctx = _lint_context_in(tmp_path, monkeypatch, **context_kwargs)
    failures = [
        result.message
        for result in lint_repo._wrangler_config(ctx)
        if result.passed is False
    ]
    assert len(failures) == len(expected_failures)
    for message, expected in zip(failures, expected_failures, strict=True):
        assert expected in message


def test_minimal_run():
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
        "github: owner\n", encoding="UTF-8"
    )
    result = check_funding_file("owner/repo")
    assert result.passed is True
    assert "found" in result.message


def test_funding_file_exists_lowercase(tmp_path, monkeypatch):
    """Detect funding file regardless of case."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "funding.yml").write_text(
        "github: owner\n", encoding="UTF-8"
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
    (workflows / "ci.yaml").write_text(workflow, encoding="UTF-8")
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


# --- Repository-local PR body template check unit tests ---


CANONICAL_TEMPLATE_PATH = ".github/pr-templates/sync-fruit-basket.md"
"""Where a repository-local PR body template is expected to live."""

CONFORMING_TEMPLATE = "---\ntitle: Sync fruit basket\nfooter: false\n---\n\nBody.\n"
"""A template passing every frontmatter rule."""


def _write_template_case(root, arg_path, file_path, body):
    """Write a workflow referencing *arg_path*, and *body* at *file_path*.

    :param root: Repository root to populate.
    :param arg_path: Path the workflow passes to `--template-file`.
    :param file_path: Where the template is actually written, or `None` to
        leave the referenced path dangling.
    :param body: Template content.
    """
    workflows = root / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "autofix.yaml").write_text(
        "on: push\n"
        "permissions: {}\n"
        "jobs:\n"
        "  sync-fruit-basket:\n"
        "    runs-on: ubuntu-slim\n"
        "    steps:\n"
        "      - run: >\n"
        "          repomatic pr-body --output-format github-actions\n"
        f"          --template-file {arg_path}\n",
        encoding="UTF-8",
    )
    if file_path is not None:
        target = root / file_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="UTF-8")


@pytest.mark.parametrize(
    ("arg_path", "file_path", "body", "needle"),
    [
        pytest.param(
            CANONICAL_TEMPLATE_PATH,
            CANONICAL_TEMPLATE_PATH,
            CONFORMING_TEMPLATE,
            None,
            id="conforming",
        ),
        pytest.param(
            ".github/pr-templates/sync-fruit-baskets.md",
            CANONICAL_TEMPLATE_PATH,
            CONFORMING_TEMPLATE,
            "does not exist",
            id="dangling-reference",
        ),
        pytest.param(
            ".github/pr-sync-fruit-basket.md",
            ".github/pr-sync-fruit-basket.md",
            CONFORMING_TEMPLATE,
            "outside",
            id="flat-github-dir",
        ),
        pytest.param(
            "pr-templates/sync-fruit-basket.md",
            "pr-templates/sync-fruit-basket.md",
            CONFORMING_TEMPLATE,
            "outside",
            id="repo-root-dir",
        ),
        pytest.param(
            CANONICAL_TEMPLATE_PATH,
            CANONICAL_TEMPLATE_PATH,
            "---\nfooter: false\n---\n\nBody.\n",
            "`title`",
            id="no-title",
        ),
        pytest.param(
            CANONICAL_TEMPLATE_PATH,
            CANONICAL_TEMPLATE_PATH,
            "---\ntitle: Sync fruit basket\nfooter: 'false'\n---\n\nBody.\n",
            "bare boolean",
            id="quoted-footer",
        ),
        pytest.param(
            CANONICAL_TEMPLATE_PATH,
            CANONICAL_TEMPLATE_PATH,
            "---\ntitle: Sync fruit basket\nfooter: 'False'\n---\n\nBody.\n",
            "bare boolean",
            id="capitalized-footer",
        ),
        pytest.param(
            CANONICAL_TEMPLATE_PATH,
            CANONICAL_TEMPLATE_PATH,
            "---\ntitle: Sync fruit basket\n---\n\nBody.\n",
            "no `footer` field",
            id="missing-footer",
        ),
    ],
)
def test_pr_templates(tmp_path, monkeypatch, arg_path, file_path, body, needle):
    """Flag misplaced, missing, and malformed repository-local PR templates."""
    monkeypatch.chdir(tmp_path)
    _write_template_case(tmp_path, arg_path, file_path, body)
    failures = [r for r in check_pr_templates() if r.passed is False]
    if needle is None:
        assert not failures
    else:
        assert any(needle in r.message for r in failures)


def test_pr_templates_equals_form(tmp_path, monkeypatch):
    """`--template-file=path` is matched the same as the space-separated form."""
    monkeypatch.chdir(tmp_path)
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "autofix.yaml").write_text(
        "on: push\n"
        "jobs:\n"
        "  sync-fruit-basket:\n"
        "    steps:\n"
        "      - run: repomatic pr-body"
        " --template-file=.github/pr-sync-fruit-basket.md\n",
        encoding="UTF-8",
    )
    (tmp_path / ".github" / "pr-sync-fruit-basket.md").write_text(
        CONFORMING_TEMPLATE, encoding="UTF-8"
    )
    failures = [r for r in check_pr_templates() if r.passed is False]
    assert any("outside" in r.message for r in failures)


def test_pr_templates_referenced_and_on_disk_reported_once(tmp_path, monkeypatch):
    """A template both referenced and on disk is one candidate, not two.

    A workflow always spells the path with `/`, while the directory glob
    yields the native separator, so a naive union of the two reports the same
    template twice on Windows, the second time without its referencing
    workflow.
    """
    monkeypatch.chdir(tmp_path)
    _write_template_case(
        tmp_path, CANONICAL_TEMPLATE_PATH, CANONICAL_TEMPLATE_PATH, CONFORMING_TEMPLATE
    )
    assert [r.message for r in check_pr_templates()] == [
        f"PR template `{CANONICAL_TEMPLATE_PATH}`: conforms."
    ]


def test_pr_templates_unreferenced_still_checked(tmp_path, monkeypatch):
    """A template no workflow names is still validated, so drift stays visible."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    template = tmp_path / CANONICAL_TEMPLATE_PATH
    template.parent.mkdir(parents=True)
    template.write_text("---\ntitle: Sync fruit basket\n---\n", encoding="UTF-8")
    failures = [r for r in check_pr_templates() if r.passed is False]
    assert any("no `footer` field" in r.message for r in failures)


@pytest.mark.parametrize(
    ("make_workflows_dir", "needle"),
    [
        pytest.param(False, "no .github/workflows", id="no-workflow-dir"),
        pytest.param(True, "no repository-local templates", id="nothing-to-check"),
    ],
)
def test_pr_templates_skipped(tmp_path, monkeypatch, make_workflows_dir, needle):
    """Skip cleanly when there is no workflow directory, or nothing to check."""
    monkeypatch.chdir(tmp_path)
    if make_workflows_dir:
        (tmp_path / ".github" / "workflows").mkdir(parents=True)
    results = check_pr_templates()
    assert all(r.passed is None for r in results)
    assert any(needle in r.message for r in results)


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


# --- Install guide download URL check unit tests ---


def _write_install_guide(root: Path, body: str) -> None:
    """Materialize an install guide under *root*, directories included."""
    guide = root / INSTALL_GUIDE_PATH
    guide.parent.mkdir(parents=True, exist_ok=True)
    guide.write_text(body, encoding="UTF-8")


GUIDE_WITH_DOWNLOADS = (
    "[Download `papaya-1.2.3-linux-x64.bin`]"
    "(https://github.com/owner/repo/releases/download/v1.2.3/"
    "papaya-1.2.3-linux-x64.bin)\n"
    "[Download `papaya-1.2.3-macos-arm64.bin`]"
    "(https://github.com/owner/repo/releases/download/v1.2.3/"
    "papaya-1.2.3-macos-arm64.bin)\n"
)


def test_install_guide_downloads_all_present(tmp_path, monkeypatch):
    """Pass when every referenced file is attached to its release."""
    monkeypatch.chdir(tmp_path)
    _write_install_guide(tmp_path, GUIDE_WITH_DOWNLOADS)
    with patch("repomatic.lint_repo.gh_api_json") as mock_gh:
        mock_gh.return_value = {
            "assets": [
                {"name": "papaya-1.2.3-linux-x64.bin"},
                {"name": "papaya-1.2.3-macos-arm64.bin"},
            ]
        }
        result = check_install_guide_downloads("owner/repo")
        assert result.passed is True


GUIDE_WITH_LATEST_ALIAS = (
    "$ curl --fail --remote-name "
    "https://github.com/owner/repo/releases/latest/download/papaya-macos-x64.bin\n"
)


def test_install_guide_downloads_latest_alias_present(tmp_path, monkeypatch):
    """Check a versionless alias against the latest release, with no tag."""
    monkeypatch.chdir(tmp_path)
    _write_install_guide(tmp_path, GUIDE_WITH_LATEST_ALIAS)
    with patch("repomatic.lint_repo.gh_api_json") as mock_gh:
        mock_gh.return_value = {"assets": [{"name": "papaya-macos-x64.bin"}]}
        result = check_install_guide_downloads("owner/repo")
        assert result.passed is True
        # No tag argument: `gh release view` then reads the latest release,
        # which is what the alias URL redirects to.
        assert mock_gh.call_args.args[0] == [
            "release",
            "view",
            "--json",
            "assets",
            "--repo",
            "owner/repo",
        ]


def test_install_guide_downloads_latest_alias_renamed(tmp_path, monkeypatch):
    """Warn on an alias naming a file the latest release renamed away.

    This is the `meta-package-manager` shape: binaries went from `mpm-*` to
    `meta-package-manager-*` in `7.0.0`, and nothing rewrites an alias URL, so
    the guide kept advertising a 404 across six releases.
    """
    monkeypatch.chdir(tmp_path)
    _write_install_guide(tmp_path, GUIDE_WITH_LATEST_ALIAS)
    with patch("repomatic.lint_repo.gh_api_json") as mock_gh:
        mock_gh.return_value = {"assets": [{"name": "papaya-fruit-macos-x64.bin"}]}
        result = check_install_guide_downloads("owner/repo")
        assert result.passed is False
        assert "latest/papaya-macos-x64.bin" in result.message


def test_install_guide_downloads_missing_asset(tmp_path, monkeypatch):
    """Warn naming each referenced file the release does not carry."""
    monkeypatch.chdir(tmp_path)
    _write_install_guide(tmp_path, GUIDE_WITH_DOWNLOADS)
    with patch("repomatic.lint_repo.gh_api_json") as mock_gh:
        mock_gh.return_value = {"assets": [{"name": "papaya-1.2.3-linux-x64.bin"}]}
        result = check_install_guide_downloads("owner/repo")
        assert result.passed is False
        assert "v1.2.3/papaya-1.2.3-macos-arm64.bin" in result.message
        # The healthy link is not reported as a problem.
        assert "v1.2.3/papaya-1.2.3-linux-x64.bin" not in result.message


def test_install_guide_downloads_release_without_assets(tmp_path, monkeypatch):
    """A release carrying no assets at all reports every referenced file.

    This is the `7.7.0` shape: the release published, so the API answers, but
    the binary upload never ran.
    """
    monkeypatch.chdir(tmp_path)
    _write_install_guide(tmp_path, GUIDE_WITH_DOWNLOADS)
    with patch("repomatic.lint_repo.gh_api_json") as mock_gh:
        mock_gh.return_value = {"assets": []}
        result = check_install_guide_downloads("owner/repo")
        assert result.passed is False
        assert "2 missing release file(s)" in result.message


@pytest.mark.parametrize(
    ("body", "needle"),
    (
        pytest.param(None, "no install guide", id="no-guide"),
        pytest.param(
            "Install it with `uv tool install papaya`.", "no release", id="no-urls"
        ),
    ),
)
def test_install_guide_downloads_skipped(tmp_path, monkeypatch, body, needle):
    """Skip when there is no guide, or no download URL to verify."""
    monkeypatch.chdir(tmp_path)
    if body is not None:
        _write_install_guide(tmp_path, body)
    result = check_install_guide_downloads("owner/repo")
    assert result.passed is None
    assert needle in result.message


def test_install_guide_downloads_unreadable_release(tmp_path, monkeypatch):
    """Skip rather than warn when the release cannot be read.

    A transient API failure must not be reported as a broken install guide.
    """
    monkeypatch.chdir(tmp_path)
    _write_install_guide(tmp_path, GUIDE_WITH_DOWNLOADS)
    with patch("repomatic.lint_repo.gh_api_json") as mock_gh:
        mock_gh.return_value = None
        result = check_install_guide_downloads("owner/repo")
        assert result.passed is None
        assert "skipped" in result.message


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


def test_pat_checks_all_pass(capsys):
    """Return 0 when all PAT capability checks pass."""
    with (
        patch("repomatic.lint_repo.run_gh_command", return_value=""),
        patch(
            "repomatic.lint_repo.check_all_pat_permissions",
            return_value=pat_results(),
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
            return_value=pat_results(
                contents=(False, "Cannot access repository contents."),
                issues=(False, "Cannot access repository issues."),
                pull_requests=(False, "Cannot access repository pull requests."),
                vulnerability_alerts=(
                    False,
                    "Token lacks 'Dependabot alerts: Read-only' permission.",
                ),
                workflows=(False, "Cannot access repository workflows."),
            ),
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


@pytest.mark.parametrize(
    ("runner", "stale"),
    [
        pytest.param("macos-15-intel", True, id="renamed-runner"),
        # Must be a value the test axes actually carry: `ubuntu-slim` is a known
        # runner but no longer a matrix cell, so excluding it would be stale.
        pytest.param("ubuntu-26.04-arm", False, id="live-runner"),
    ],
)
def test_check_test_matrix_excludes(tmp_path, monkeypatch, runner, stale):
    """An exclude naming a runner the matrix no longer has is a warning."""
    metadata_from_pyproject(
        tmp_path,
        monkeypatch,
        '[project]\nname = "p"\nversion = "1.0.0"\n\n'
        "[tool.repomatic.test-matrix]\n"
        f'exclude = [{{os = "{runner}"}}]\n',
    )

    results = check_test_matrix_excludes()
    if stale:
        assert any(r.passed is False and runner in r.message for r in results)
    else:
        assert all(r.passed is not False for r in results)


def _write_project(tmp_path, monkeypatch, classifiers, requires_python=">= 3.11"):
    """Lay out a project declaring the given Python support, and chdir into it."""
    entries = "\n".join(
        f'  "Programming Language :: Python :: {c}",' for c in classifiers
    )
    metadata_from_pyproject(
        tmp_path,
        monkeypatch,
        '[project]\nname = "p"\nversion = "1.0.0"\n'
        f'requires-python = "{requires_python}"\n'
        f"classifiers = [\n{entries}\n]\n",
    )
    monkeypatch.chdir(tmp_path)


def _write_ci_workflow(tmp_path, body, name="ci.yaml"):
    """Materialize a workflow under *tmp_path*'s `.github/workflows/` directory.

    The checks under test discover workflows by globbing that directory, so the
    file has to sit at the conventional path rather than anywhere writable.
    """
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True, exist_ok=True)
    (workflows / name).write_text(body, encoding="UTF-8")


_MATRIX = (
    "on: push\n"
    "jobs:\n"
    "  tests:\n"
    "    strategy:\n"
    "      matrix:\n"
    "        python-version: [{versions}]\n"
    "    runs-on: ubuntu-slim\n"
    "    steps:\n"
    "      - run: echo apricot\n"
)


def test_python_floor_disagreeing_with_classifiers(tmp_path, monkeypatch):
    """A floor below the lowest classifier misreports what installs."""
    _write_project(tmp_path, monkeypatch, ["3.11", "3.12"], requires_python=">= 3.9")
    results = check_python_version_consistency()
    assert any(
        r.passed is False and "requires-python floor" in r.message for r in results
    )


def test_python_matrix_missing_an_advertised_boundary(tmp_path, monkeypatch):
    """Advertising a version the matrix never reaches is untested support."""
    _write_project(tmp_path, monkeypatch, ["3.11", "3.12", "3.13"])
    _write_ci_workflow(tmp_path, _MATRIX.format(versions='"3.11", "3.12"'))
    results = check_python_version_consistency()
    assert any(r.passed is False and "3.13" in r.message for r in results)


def test_python_matrix_may_skip_intermediate_versions(tmp_path, monkeypatch):
    """Only the ends of the range are mandatory, to keep CI load a free choice."""
    _write_project(tmp_path, monkeypatch, ["3.11", "3.12", "3.13"])
    _write_ci_workflow(tmp_path, _MATRIX.format(versions='"3.11", "3.13"'))
    assert all(r.passed is not False for r in check_python_version_consistency())


def test_python_matrix_testing_an_unadvertised_version(tmp_path, monkeypatch):
    """A released version tested but not advertised is support left unclaimed."""
    _write_project(tmp_path, monkeypatch, ["3.11", "3.12"])
    _write_ci_workflow(tmp_path, _MATRIX.format(versions='"3.11", "3.12", "3.13"'))
    results = check_python_version_consistency()
    assert any(r.passed is False and "3.13" in r.message for r in results)


def test_python_matrix_tolerates_unreleased_versions(tmp_path, monkeypatch):
    """An in-development version cannot be advertised, so it is exempt."""
    unstable = min(UNSTABLE_PYTHON_VERSIONS)
    _write_project(tmp_path, monkeypatch, ["3.11", "3.12"])
    _write_ci_workflow(
        tmp_path, _MATRIX.format(versions=f'"3.11", "3.12", "{unstable}"')
    )
    assert all(r.passed is not False for r in check_python_version_consistency())


def test_python_matrix_built_at_runtime_is_skipped(tmp_path, monkeypatch):
    """A `fromJSON` matrix is opaque, and its axes are canonical already."""
    _write_project(tmp_path, monkeypatch, ["3.11", "3.12"])
    _write_ci_workflow(
        tmp_path,
        "on: push\njobs:\n  tests:\n    strategy:\n"
        "      matrix: ${{ fromJSON(needs.metadata.outputs.metadata).test_matrix }}\n"
        "    runs-on: ubuntu-slim\n    steps:\n      - run: echo apricot\n",
    )
    results = check_python_version_consistency()
    assert any(r.passed is None and "no literal axis" in r.message for r in results)


@pytest.mark.parametrize(
    ("runner", "expect_fail", "needle"),
    [
        pytest.param("ubuntu-latest", True, "repoints", id="floating-alias"),
        # A real GitHub image this project runs nothing on.
        pytest.param("ubuntu-22.04", True, "not one of the images", id="unknown-image"),
        # Retired from the fleet: it lost the A/B against the full image, so a
        # job still naming it is now drift rather than a deliberate choice.
        pytest.param("ubuntu-slim", True, "not one of the images", id="retired-image"),
        pytest.param("ubuntu-26.04", False, None, id="known-image"),
        pytest.param("${{ matrix.os }}", False, None, id="expression"),
    ],
)
def test_runner_images(tmp_path, monkeypatch, runner, expect_fail, needle):
    """Floating aliases and off-axis images are flagged; expressions are not."""
    monkeypatch.chdir(tmp_path)
    _write_ci_workflow(
        tmp_path,
        f"on: push\njobs:\n  build:\n    runs-on: {runner}\n"
        "    steps:\n      - run: echo apricot\n",
    )
    failures = [r for r in check_runner_images() if r.passed is False]
    if expect_fail:
        assert failures
        assert any(needle in r.message for r in failures)
    else:
        assert not failures


def test_runner_images_ignores_thin_callers(tmp_path, monkeypatch):
    """A job with no steps delegates, and its runner is the callee's business."""
    monkeypatch.chdir(tmp_path)
    _write_ci_workflow(
        tmp_path,
        "on: push\njobs:\n  build:\n    uses: ./.github/workflows/_build.yaml\n",
    )
    results = check_runner_images()
    assert all(r.passed is not False for r in results)


@pytest.mark.parametrize(
    ("runner", "expected"),
    [
        # The whole reason this is separate from KNOWN_RUNNERS: an off-axis image
        # is still what the job runs on, and is precisely the one no axis is
        # watching for a deprecation announcement.
        pytest.param(
            "ubuntu-22.04", {"ubuntu-22.04": ["ci.yaml:build"]}, id="off-axis"
        ),
        pytest.param("ubuntu-26.04", {"ubuntu-26.04": ["ci.yaml:build"]}, id="on-axis"),
        pytest.param("ubuntu-latest", {"ubuntu-latest": ["ci.yaml:build"]}, id="alias"),
        pytest.param("${{ matrix.os }}", {}, id="expression"),
    ],
)
def test_literal_runners(tmp_path, monkeypatch, runner, expected):
    """Every literal `runs-on:` is reported, axis membership notwithstanding."""
    monkeypatch.chdir(tmp_path)
    _write_ci_workflow(
        tmp_path,
        f"on: push\njobs:\n  build:\n    runs-on: {runner}\n"
        "    steps:\n      - run: echo apricot\n",
    )
    assert literal_runners() == expected


def test_literal_runners_skips_thin_callers(tmp_path, monkeypatch):
    """A delegating job contributes no runner: it names none of its own."""
    monkeypatch.chdir(tmp_path)
    _write_ci_workflow(
        tmp_path,
        "on: push\njobs:\n  build:\n    uses: ./.github/workflows/_build.yaml\n",
    )
    assert literal_runners() == {}


def test_literal_runners_groups_every_location(tmp_path, monkeypatch):
    """One image named by several jobs reports all of them, sorted by file."""
    monkeypatch.chdir(tmp_path)
    job = "    runs-on: ubuntu-26.04\n    steps:\n      - run: echo apricot\n"
    _write_ci_workflow(tmp_path, f"on: push\njobs:\n  build:\n{job}  test:\n{job}")
    _write_ci_workflow(
        tmp_path, f"on: push\njobs:\n  deploy:\n{job}", name="deploy.yaml"
    )
    assert literal_runners() == {
        "ubuntu-26.04": ["ci.yaml:build", "ci.yaml:test", "deploy.yaml:deploy"],
    }


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
    """The canonical repo (local `uv run`, no upstream refs) has nothing to check."""
    (tmp_path / "tests.yaml").write_text(
        "      - run: uv --no-progress run --frozen -- repomatic metadata\n",
        encoding="UTF-8",
    )
    error, msg = check_inline_pins_match_upstream(tmp_path)
    assert error is None
    assert "nothing to compare" in msg


# ---------------------------------------------------------------------------
# Self-pin cooldown exemption check tests
# ---------------------------------------------------------------------------

_COOLDOWN_ENV = 'env:\n  UV_EXCLUDE_NEWER: "1 week"\n'
"""The workflow-level cooldown that makes an unexempt inline pin unresolvable."""


def test_self_pin_exemption_flags_unexempt_pin(tmp_path):
    """A pin resolving under a cooldown without the bypass is a fatal error."""
    (tmp_path / "tests.yaml").write_text(
        _COOLDOWN_ENV + "      - run: uvx --no-progress 'repomatic==7.11.0' metadata\n",
        encoding="UTF-8",
    )
    result = check_self_pin_cooldown_exemption(tmp_path)
    assert result.passed is False
    assert "tests.yaml" in result.message
    assert SELF_PIN_COOLDOWN_EXEMPTION in result.message


def test_self_pin_exemption_accepts_exempt_pin(tmp_path):
    """The spelling both writers emit passes."""
    (tmp_path / "tests.yaml").write_text(
        _COOLDOWN_ENV + f"      - run: uvx --no-progress {SELF_PIN_COOLDOWN_EXEMPTION}"
        " 'repomatic==7.11.0' metadata\n",
        encoding="UTF-8",
    )
    result = check_self_pin_cooldown_exemption(tmp_path)
    assert result.passed is True


def test_self_pin_exemption_ignores_workflow_without_cooldown(tmp_path):
    """No cooldown means nothing to exempt, so a bare pin resolves fine."""
    (tmp_path / "tests.yaml").write_text(
        "      - run: uvx --no-progress 'repomatic==7.11.0' metadata\n",
        encoding="UTF-8",
    )
    result = check_self_pin_cooldown_exemption(tmp_path)
    assert result.passed is None
    assert "no inline pin" in result.message


# ---------------------------------------------------------------------------
# setup-uv version pin check tests
# ---------------------------------------------------------------------------


def _setup_uv_step(version: str | None) -> str:
    """Render a `setup-uv` step, optionally pinning the uv it installs."""
    step = "      - uses: astral-sh/setup-uv@c771a70e # v9.0.0\n"
    if version:
        step += f'        with:\n          version: "{version}"\n'
    return step


def _setup_uv_workflow(*versions: str | None) -> str:
    """A one-job workflow whose steps pin *versions*, `None` for unpinned."""
    steps = "".join(_setup_uv_step(version) for version in versions)
    return f"on: push\njobs:\n  build:\n    steps:\n{steps}"


def test_setup_uv_version_pin_flags_unpinned_step(tmp_path):
    """A step with no `version:` input installs whatever uv is newest."""
    (tmp_path / "tests.yaml").write_text(_setup_uv_workflow(None), encoding="UTF-8")
    result = check_setup_uv_version_pin(tmp_path)
    assert result.passed is False
    assert "tests.yaml" in result.message


def test_setup_uv_version_pin_accepts_pinned_steps(tmp_path):
    """Every step naming the same version passes."""
    (tmp_path / "tests.yaml").write_text(
        _setup_uv_workflow("0.12.1", "0.12.1"), encoding="UTF-8"
    )
    result = check_setup_uv_version_pin(tmp_path)
    assert result.passed is True
    assert "0.12.1" in result.message


def test_setup_uv_version_pin_flags_unpinned_step_next_to_pinned_one(tmp_path):
    """A pinned step does not vouch for an unpinned one beside it."""
    (tmp_path / "tests.yaml").write_text(
        _setup_uv_workflow(None, "0.12.1"), encoding="UTF-8"
    )
    result = check_setup_uv_version_pin(tmp_path)
    assert result.passed is False


def test_setup_uv_version_pin_flags_split_versions(tmp_path):
    """Steps pinning two versions silently test two uv releases."""
    (tmp_path / "tests.yaml").write_text(_setup_uv_workflow("0.12.1"), encoding="UTF-8")
    (tmp_path / "lint.yaml").write_text(_setup_uv_workflow("0.12.2"), encoding="UTF-8")
    result = check_setup_uv_version_pin(tmp_path)
    assert result.passed is False
    assert "0.12.1" in result.message
    assert "0.12.2" in result.message


# ---------------------------------------------------------------------------
# Metadata key check tests
# ---------------------------------------------------------------------------


def _metadata_step(command):
    """A one-job workflow whose single step runs *command*."""
    return f"on: push\njobs:\n  metadata:\n    steps:\n      - run: {command}\n"


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        # Upstream spelling: the CLI comes from the project's own lockfile.
        pytest.param(
            "uv --no-progress run --frozen -- repomatic metadata --format github-json"
            ' --output "$GITHUB_OUTPUT" cli_scripts package_name',
            ["cli_scripts", "package_name"],
            id="uv-run",
        ),
        # Downstream spelling: the CLI comes from a pinned uvx environment.
        pytest.param(
            "uvx --no-progress 'repomatic==7.11.0' metadata --format github-json"
            ' --output "$GITHUB_OUTPUT" cli_scripts package_name',
            ["cli_scripts", "package_name"],
            id="uvx-pinned",
        ),
        # An option's value is never a key, whether attached or separate.
        pytest.param(
            "repomatic metadata --format=json current_version",
            ["current_version"],
            id="inline-option-value",
        ),
        pytest.param(
            "repomatic metadata -o out current_version",
            ["current_version"],
            id="short-option-value",
        ),
        # Words belonging to another command are not arguments to this one.
        pytest.param(
            "repomatic metadata current_version && echo done",
            ["current_version"],
            id="shell-operator",
        ),
        pytest.param(
            "repomatic metadata current_version\necho done",
            ["current_version"],
            id="second-line",
        ),
        pytest.param("uv run pytest -m once", [], id="unrelated-command"),
        # `metadata` also names a step id, an output and a job. Only the token
        # following the package invocation is the subcommand.
        pytest.param("echo metadata cli_scripts", [], id="bare-word"),
        pytest.param("grep metadata harvest.txt", [], id="bare-word-with-path"),
        # No positional key: the command dumps every one of them.
        pytest.param("uvx 'repomatic==7.11.0' metadata", [], id="no-keys"),
        pytest.param("uvx repomatic metadata --output out.json", [], id="only-options"),
        # An unbalanced quote is a line a shell rejects too: no guess beats a
        # wrong one.
        pytest.param("uvx repomatic metadata 'test_matrix", [], id="unbalanced-quote"),
    ],
)
def test_requested_metadata_keys(command, expected):
    """Positional keys are read off the command line the way Click reads them."""
    assert lint_repo.requested_metadata_keys(command, "repomatic") == expected


def test_metadata_keys_flags_a_retired_key(tmp_path, monkeypatch):
    """A key removed upstream fails the lint instead of the next workflow run."""
    monkeypatch.chdir(tmp_path)
    _write_ci_workflow(
        tmp_path,
        _metadata_step(
            "uvx --no-progress 'repomatic==7.11.0' metadata cli_scripts coverage_cells"
        ),
    )
    failures = [r for r in check_metadata_keys() if r.passed is False]
    assert failures
    assert "coverage_cells" in failures[0].message
    assert "--list-keys" in failures[0].message


def test_metadata_keys_accepts_current_keys(tmp_path, monkeypatch):
    """Keys the command still answers raise nothing."""
    monkeypatch.chdir(tmp_path)
    _write_ci_workflow(
        tmp_path, _metadata_step("repomatic metadata cli_scripts package_name")
    )
    results = check_metadata_keys()
    assert all(r.passed is not False for r in results)


def test_metadata_keys_skips_a_repo_that_never_calls_it(tmp_path, monkeypatch):
    """No invocation to read is a skip, not a pass."""
    monkeypatch.chdir(tmp_path)
    _write_ci_workflow(tmp_path, _metadata_step("echo apricot"))
    results = check_metadata_keys()
    assert [r.passed for r in results] == [None]


def test_metadata_value_options_match_the_command():
    """The hand-listed value options are the ones the command really declares.

    `check_metadata_keys` cannot import the CLI to ask (see
    `METADATA_VALUE_OPTIONS`), so the list is pinned here instead: an option
    gaining a value would otherwise make the lint read that value as a key.
    """
    declared = {
        opt
        for param in metadata_command.params
        if not getattr(param, "is_flag", False)
        for opt in (*param.opts, *param.secondary_opts)
        if opt.startswith("-")
    }
    assert declared == set(METADATA_VALUE_OPTIONS)


def test_metadata_keys_covers_every_workflow_of_this_repo():
    """This repository's own workflows only ask for keys that exist."""
    results = check_metadata_keys(Path(".github/workflows"))
    assert results
    assert all(r.passed is not False for r in results)


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
    """An unreadable rulesets API is indeterminate, not a failure.

    Matches `check_tag_protection_rules`, which reads the same payload. The
    setup guide still treats the `None` as incomplete and keeps the step open.
    """

    with patch("repomatic.lint_repo.gh_api_json", return_value=None):
        passed, msg = check_branch_ruleset_on_default("owner/repo")
    assert passed is None
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


def test_every_check_is_reachable_from_the_roster():
    """Every `check_*` this module defines is wired into `REPO_CHECKS`.

    `check_branch_ruleset_on_default` and `check_immutable_releases` shipped
    defined here but reached only from `setup_guide`, so `lint-repo` silently
    skipped two of its own checks. A check nobody calls is a check nobody
    benefits from: adding one now means adding it to the roster, or the
    omission fails here.
    """
    module_source = inspect.getsource(lint_repo)
    roster_source = module_source.split("REPO_CHECKS", 1)[1]
    defined = {
        name
        for name, value in vars(lint_repo).items()
        if name.startswith("check_")
        and callable(value)
        # Defined here, not imported from another module.
        and getattr(value, "__module__", "") == lint_repo.__name__
    }
    unreachable = {name for name in defined if name not in roster_source}
    assert not unreachable, (
        f"checks defined but never run by lint-repo: {sorted(unreachable)}."
        " Add them to REPO_CHECKS."
    )


def test_repo_check_names_are_unique():
    """Two checks sharing a name would make the roster ambiguous to read."""
    names = [check.name for check in REPO_CHECKS]
    assert len(set(names)) == len(names), "duplicate RepoCheck names"
