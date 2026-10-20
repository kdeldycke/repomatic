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

"""Tests for the repository lint package."""

from __future__ import annotations

import importlib
import json
import pkgutil
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

import pytest
import tomlrt
import yaml
from click_extra.testing import CliRunner

from repomatic import lint_repo
from repomatic.bundle import get_data_content
from repomatic.checks import CheckResult
from repomatic.cli.main import repomatic
from repomatic.cli.repo import show_metadata
from repomatic.cloudflare.r2 import PAGES_MAX_FILE_SIZE
from repomatic.components import COMPONENTS, SyncMode, ToolConfigComponent
from repomatic.config import (
    Config,
    LabelsConfig,
    LintRepoConfig,
    load_repomatic_config,
)
from repomatic.deps.pins import Candidate
from repomatic.github.gh import GhCommandError
from repomatic.github.labels import (
    LabelConfigError,
    _names_in_labelmaker_config,
    declared_label_names,
    serialize_inline_labels,
)
from repomatic.github.token import PAT_PERMISSION_PROBES
from repomatic.github.workflow_sync import generate_thin_caller
from repomatic.http import FetchError, get_bytes
from repomatic.lint_repo.context import (
    LintContext,
    _resolve_declared_labels,
    documentation_url,
)
from repomatic.lint_repo.pins import (
    check_inline_pins_match_upstream,
    check_manpages_toolchain,
    check_registry_pin_cooldown,
    check_self_pin_cooldown_exemption,
    check_setup_uv_checksum_coverage,
    check_setup_uv_version_pin,
)
from repomatic.lint_repo.repo import (
    RELEASE_LIST_LIMIT,
    check_description_matches,
    check_funding_file,
    check_install_guide_downloads,
    check_package_name_vs_repo,
    check_stale_draft_releases,
    check_topics_subset_of_keywords,
    check_undeclared_labels,
    check_website_for_sphinx,
)
from repomatic.lint_repo.roster import (
    REPO_CHECKS,
    RepoCheck,
    repo_check_reference,
    run_repo_lint,
)
from repomatic.lint_repo.settings import (
    check_branch_ruleset_on_default,
    check_classic_branch_protection,
    check_immutable_releases,
    check_pat_permissions,
    check_pat_scope,
    check_pypi_trusted_publisher,
    check_sha_pinning_required,
    check_tag_protection_rules,
)
from repomatic.lint_repo.site import (
    MAX_REPORTED_DEAD_URLS,
    check_cloudflare_r2_secrets,
    check_oversized_site_files,
    check_pages_redirect_preserved,
    check_pages_redirects,
    check_stale_gh_pages_branch,
    check_wrangler_config,
)
from repomatic.lint_repo.tool_config import (
    anchored_exclude_core,
    check_bootstrap_config_drift,
    check_superseded_local_excludes,
    missing_template_entries,
    template_member_name,
)
from repomatic.lint_repo.workflows import (
    check_metadata_keys,
    check_pr_templates,
    check_python_version_consistency,
    check_runner_images,
    check_test_matrix_excludes,
    check_thin_caller_pins,
    check_thin_caller_secrets,
    check_thin_caller_triggers,
    check_workflow_dispatch,
    check_workflow_permissions,
)
from repomatic.matrix.axes import UNSTABLE_PYTHON_VERSIONS
from repomatic.metadata.core import METADATA_VALUE_OPTIONS, requested_metadata_keys
from repomatic.pypi import TrustedPublisher
from repomatic.release.prepare_release import SELF_PIN_COOLDOWN_EXEMPTION
from repomatic.tooling.registry import (
    ArchiveFormat,
    BinarySpec,
    NpmSpec,
    ToolSpec,
)
from repomatic.upstream import DEFAULT_REPO, INSTALL_GUIDE_PATH
from tests.conftest import PROJECT_ROOT, metadata_from_pyproject, pat_results


@pytest.fixture(autouse=True)
def _undated_registry_pins():
    """Give no release date to any registry pin, as a runner with no network does.

    The `registry-pin-cooldown` check applies to every context, so each test of
    the whole roster asks PyPI and npm about every pin. `_no_real_http` refuses
    the network, but the HTTP cache still answers from the last real run on the
    machine, with real verdicts and megabytes of JSON to parse. A test of the
    check patches the lookup itself, which shadows this stub.
    """
    with patch("repomatic.lint_repo.pins.tool_candidates", return_value=[]):
        yield


def _offline(**fields: Any) -> LintContext:
    """A context whose repository object is already read, so no check reads it.

    :param fields: `LintContext` fields, plus `repo_object`: the object the
        checks read in place of `GET /repos/{repo}`, `None` for an unreadable one.
    """
    repo_object = fields.pop("repo_object", None)
    ctx = LintContext(**fields)
    ctx.__dict__["repo_object"] = repo_object
    return ctx


def _workflows(**texts: str) -> LintContext:
    """A context whose workflow files are *texts*, keyed by stem, and none on disk."""
    ctx = LintContext()
    ctx.__dict__["workflow_texts"] = {
        Path(f".github/workflows/{stem}.yaml"): text for stem, text in texts.items()
    }
    return ctx


def _findings(ctx: LintContext) -> dict[str, list[CheckResult]]:
    """The results `lint-repo` reports on *ctx*, grouped by check name."""
    found: dict[str, list[CheckResult]] = {}
    for check, result in run_repo_lint(ctx):
        found.setdefault(check.name, []).append(result)
    return found


def _fails(ctx: LintContext) -> bool:
    """Whether `lint-repo` exits non-zero on *ctx*."""
    return any(check.fails(result) for check, result in run_repo_lint(ctx))


def _roster_check(name: str) -> RepoCheck:
    """The roster entry called *name*."""
    return next(check for check in REPO_CHECKS if check.name == name)


def test_repo_object_is_read_once():
    """One `GET /repos/{repo}` serves every check reading the repository object."""
    payload = {"homepage": "https://example.com", "description": "A package"}
    ctx = LintContext(repo="owner/repo")
    with patch("repomatic.lint_repo.context.gh_json", return_value=payload) as api:
        assert ctx.repo_object == payload
        assert ctx.repo_object == payload
    api.assert_called_once_with(["api", "repos/owner/repo"], soft=True)


@pytest.mark.parametrize(
    ("repo", "payload"),
    (
        pytest.param(None, {"description": "A package"}, id="no-repository"),
        pytest.param("owner/repo", None, id="unreadable"),
        pytest.param("owner/repo", ["not", "an", "object"], id="not-an-object"),
    ),
)
def test_repo_object_is_none_when_it_cannot_be_read(repo, payload):
    """No repository, or an unreadable answer, reads as an absent object."""
    ctx = LintContext(repo=repo)
    with patch("repomatic.lint_repo.context.gh_json", return_value=payload):
        assert ctx.repo_object is None


def test_slug_refuses_a_context_without_a_repository():
    """A check reading the API through an ungated context fails loudly."""
    with pytest.raises(ValueError, match="no repository is named"):
        _ = LintContext().slug


def test_rulesets_are_read_once():
    """The two ruleset checks share one read of the rulesets API."""
    rulesets = [{"name": "main", "target": "branch", "enforcement": "active"}]
    ctx = LintContext(repo="owner/repo")
    with patch("repomatic.lint_repo.context.gh_json", return_value=rulesets) as api:
        check_tag_protection_rules(ctx)
        check_branch_ruleset_on_default(ctx)
    api.assert_called_once()


def test_names_match():
    """No warning when names match."""
    result = check_package_name_vs_repo(
        LintContext(package_name="my-package", repo_name="my-package")
    )
    assert result.passed is True
    assert "matches" in result.message


def test_names_differ():
    """Warning when names differ."""
    result = check_package_name_vs_repo(
        LintContext(package_name="my-package", repo_name="my-repo")
    )
    assert result.passed is False
    assert "differs" in result.message
    assert "my-package" in result.message
    assert "my-repo" in result.message


def test_sphinx_with_website():
    """No warning when Sphinx project has website."""
    result = check_website_for_sphinx(
        _offline(
            repo="owner/repo", repo_object={"homepage": "https://docs.example.com"}
        )
    )
    assert result.passed is True
    assert "https://docs.example.com" in result.message


@pytest.mark.parametrize("homepage", (None, ""))
def test_sphinx_without_website(homepage):
    """Warning when Sphinx project has no website, whatever GitHub stores for none."""
    result = check_website_for_sphinx(
        _offline(repo="owner/repo", repo_object={"homepage": homepage})
    )
    assert result.passed is False
    assert "Sphinx" in result.message
    assert "not set" in result.message


def test_sphinx_website_unreadable_repository():
    """An unreadable repository object skips, rather than reporting no website."""
    result = check_website_for_sphinx(_offline(repo="owner/repo", repo_object=None))
    assert result.passed is None
    assert "skipped" in result.message


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
        _offline(
            repo="owner/repo", docs_url=docs_url, repo_object={"homepage": homepage_url}
        )
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
        _offline(
            repo="owner/repo", docs_url=docs_url, repo_object={"homepage": homepage_url}
        )
    )
    assert result.passed is False
    assert homepage_url in result.message
    assert docs_url in result.message


def test_sphinx_website_without_declared_docs_url():
    """Keep the presence-only check when the project declares no docs URL."""
    result = check_website_for_sphinx(
        _offline(
            repo="owner/repo", repo_object={"homepage": "https://kiwi.github.io/papaya"}
        )
    )
    assert result.passed is True
    assert "is set" in result.message


def test_descriptions_match():
    """No error when descriptions match."""
    result = check_description_matches(
        _offline(
            repo="owner/repo",
            project_description="A cool package",
            repo_object={"description": "A cool package"},
        )
    )
    assert result.passed is True
    assert "matches" in result.message


@pytest.mark.parametrize("description", ("Different description", None))
def test_descriptions_differ(description):
    """Error when descriptions differ, an empty repository description included."""
    result = check_description_matches(
        _offline(
            repo="owner/repo",
            project_description="A cool package",
            repo_object={"description": description},
        )
    )
    assert result.passed is False
    assert "!=" in result.message


def test_description_unreadable_repository():
    """An unreadable repository object skips, never fails a fatal check."""
    result = check_description_matches(
        _offline(repo="owner/repo", project_description="A cool package")
    )
    assert result.passed is None
    assert "skipped" in result.message


@pytest.mark.parametrize(
    "name", ("website-for-sphinx", "description-matches", "topics-subset-of-keywords")
)
def test_repository_object_checks_need_a_repository(name):
    """A check reading the repository object never runs without a repository.

    Without the gate, the description check compared the project's description
    against nothing and failed, fatally, on every local run naming no
    repository.
    """
    fields: dict[str, Any] = {
        "is_sphinx": True,
        "project_description": "A cool package",
        "keywords": ["fig"],
    }
    check = _roster_check(name)
    assert not check.applies(LintContext(**fields))
    assert check.applies(LintContext(repo="owner/repo", **fields))


def test_all_checks_pass():
    """No check fails the run when the repository matches the project."""
    ctx = _offline(
        package_name="my-package",
        repo_name="my-package",
        is_sphinx=True,
        project_description="A cool package",
        repo="owner/repo",
        repo_object={
            "homepage": "https://example.com",
            "description": "A cool package",
        },
    )
    assert not _fails(ctx)


def test_description_mismatch():
    """A description mismatch fails the run."""
    ctx = _offline(
        project_description="A cool package",
        repo="owner/repo",
        repo_object={"homepage": None, "description": "Different description"},
    )
    assert _fails(ctx)
    (result,) = _findings(ctx)["description-matches"]
    assert result.passed is False
    assert _roster_check("description-matches").fatal


def test_package_name_warning():
    """A package name mismatch warns, and the run still passes."""
    ctx = LintContext(package_name="my-package", repo_name="different-repo")
    assert not _fails(ctx)
    (result,) = _findings(ctx)["package-name-vs-repo"]
    assert result.passed is False


def test_website_warning():
    """A missing website warns, and the run still passes."""
    ctx = _offline(
        is_sphinx=True,
        repo="owner/repo",
        repo_object={"homepage": None, "description": None},
    )
    assert not _fails(ctx)
    (result,) = _findings(ctx)["website-for-sphinx"]
    assert result.passed is False


def test_website_docs_url_mismatch_warning():
    """A website field pointing away from the docs URL warns without failing."""
    ctx = _offline(
        is_sphinx=True,
        docs_url="https://papaya.example",
        repo="owner/repo",
        repo_object={"homepage": "https://kiwi.github.io/papaya", "description": None},
    )
    assert not _fails(ctx)
    (result,) = _findings(ctx)["website-for-sphinx"]
    assert result.passed is False
    assert "https://papaya.example" in result.message


@pytest.mark.parametrize(
    ("unsubscribe_active", "has_notifications_pat", "expected"),
    (
        (False, False, None),
        (True, False, False),
        (True, True, True),
    ),
)
def test_notifications_pat_check(unsubscribe_active, has_notifications_pat, expected):
    """The notifications PAT check only fires when the workflow is opted in."""
    ctx = LintContext(
        config=Config(notification_unsubscribe=unsubscribe_active),
        has_notifications_pat=has_notifications_pat,
    )
    assert not _fails(ctx)
    found = _findings(ctx).get("notifications-pat-secret", [])
    expected_passed = [] if expected is None else [expected]
    assert [result.passed for result in found] == expected_passed


@pytest.mark.parametrize(
    ("site_deploy", "is_sphinx", "has_token", "expected"),
    (
        pytest.param("github-pages", True, False, None, id="other-target-silent"),
        pytest.param(
            "cloudflare-pages",
            True,
            False,
            "CLOUDFLARE_API_TOKEN not configured",
            id="no-token",
        ),
        pytest.param(
            "cloudflare-pages",
            # No Sphinx at all: a site built by the repository's own workflow
            # (a Pelican blog, a hand-rolled tree) needs the same credential,
            # so declaring the target is the whole opt-in.
            False,
            False,
            "CLOUDFLARE_API_TOKEN not configured",
            id="non-sphinx-site-still-checked",
        ),
        pytest.param(
            "cloudflare-pages",
            True,
            True,
            "CLOUDFLARE_API_TOKEN is configured, and the account resolves",
            id="token-alone-is-enough",
        ),
    ),
)
def test_cloudflare_secrets_check(site_deploy, is_sphinx, has_token, expected):
    """The token is the whole of the credential.

    The account is derived from it at run time: a token scoped to nothing
    but `Cloudflare Pages: Edit` still enumerates the account it belongs
    to, so no second identifier is asked for or reported on.
    """
    ctx = LintContext(
        config=Config(site_deploy=site_deploy),
        is_sphinx=is_sphinx,
        has_cloudflare_api_token=has_token,
    )
    assert not _fails(ctx)
    found = _findings(ctx).get("cloudflare-pages-secrets", [])
    if expected is None:
        assert found == []
    else:
        (result,) = found
        assert expected in result.message


def _r2_config(bucket: str) -> Config:
    """A Cloudflare Pages configuration declaring *bucket*, or no bucket at all."""
    return Config(
        site_deploy="cloudflare-pages",
        site_cloudflare_r2_bucket=bucket,
        site_cloudflare_r2_domain="files.example.com" if bucket else "",
    )


@pytest.mark.parametrize(
    ("bucket", "applies"),
    (
        pytest.param("", False, id="no-bucket-reads-no-key"),
        pytest.param("papaya-files", True, id="bucket-needs-its-keys"),
    ),
)
def test_cloudflare_r2_secrets_follow_the_declared_bucket(bucket, applies):
    """Without a bucket the deploy drops oversized files by design, reading no key."""
    check = _roster_check("cloudflare-r2-secrets")
    assert check.applies(LintContext(config=_r2_config(bucket))) is applies


@pytest.mark.parametrize(
    ("has_keys", "passed", "fragment"),
    (
        pytest.param(False, False, "the deploy drops every file", id="missing"),
        pytest.param(True, True, "are configured", id="configured"),
    ),
)
def test_cloudflare_r2_secrets(has_keys, passed, fragment):
    ctx = LintContext(
        config=_r2_config("papaya-files"), has_cloudflare_r2_keys=has_keys
    )
    result = check_cloudflare_r2_secrets(ctx)
    assert result.passed is passed
    assert fragment in result.message


@pytest.mark.parametrize(
    ("big", "bucket", "passed", "fragment"),
    (
        pytest.param(False, "", True, "No tracked file", id="nothing-oversized"),
        pytest.param(
            True, "", False, "The Cloudflare deploy drops any", id="no-bucket-warns"
        ),
        pytest.param(True, "papaya-files", True, "move to R2", id="bucket-serves"),
    ),
)
def test_oversized_site_files(tmp_path, big, bucket, passed, fragment):
    """Warn about a tracked file the Pages deploy would drop, unless R2 takes it."""
    files = []
    if big:
        atlas = tmp_path / "atlas.zip"
        with atlas.open("wb") as stream:
            stream.truncate(PAGES_MAX_FILE_SIZE + 1)
        files.append(atlas)
    ctx = LintContext(config=_r2_config(bucket))
    # Seed the cached inventory: the check must not walk this repository.
    ctx.__dict__["oversized_files"] = files
    result = check_oversized_site_files(ctx)
    assert result.passed is passed
    assert fragment in result.message


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
    check = _roster_check("pages-deployment-source")
    ctx = LintContext(
        config=Config(site_deploy=site_deploy), is_sphinx=True, repo="orchard/papaya"
    )
    assert check.applies(ctx) is probed


@pytest.mark.parametrize(
    ("pages", "history", "docs_url", "passed", "fragment"),
    (
        pytest.param(
            {"cname": "repomatic.net"},
            None,
            "https://repomatic.net",
            True,
            "redirect to repomatic.net",
            id="custom-domain-matches",
        ),
        pytest.param(
            {"cname": "REPOMATIC.NET"},
            None,
            "https://repomatic.net/",
            True,
            "redirect to repomatic.net",
            id="case-insensitive",
        ),
        pytest.param(
            {"cname": None},
            None,
            "https://repomatic.net",
            False,
            "carries no custom domain",
            id="no-custom-domain-serves-a-stale-copy",
        ),
        pytest.param(
            {"cname": "old.example.com"},
            None,
            "https://repomatic.net",
            False,
            "redirects to old.example.com while",
            id="custom-domain-points-elsewhere",
        ),
        pytest.param(
            None,
            [{"id": 1}],
            "https://repomatic.net",
            False,
            "now answers 404",
            id="pages-disabled-after-publishing",
        ),
        pytest.param(
            None,
            [],
            "https://repomatic.net",
            None,
            "never published to GitHub Pages",
            id="pages-never-enabled",
        ),
        pytest.param(
            {"cname": "repomatic.net"},
            None,
            "https://kdeldycke.github.io/repomatic",
            False,
            "still documents kdeldycke.github.io",
            id="declared-url-not-moved-yet",
        ),
        pytest.param(
            None,
            None,
            None,
            None,
            "no documentation URL declared",
            id="nothing-declared",
        ),
    ),
)
def test_pages_redirect_preserved(pages, history, docs_url, passed, fragment):
    """The old `github.io` space must keep redirecting after a move.

    Disabling Pages is the unrecoverable mistake: it deletes the redirect and
    every historical link with it, silently, because nothing inside the
    repository changes. Leaving the custom domain unset is the quieter one,
    where the old host serves a copy that stops being rebuilt the moment the
    deploy job is gated off.
    """
    ctx = LintContext(repo="kdeldycke/repomatic", docs_url=docs_url)
    with patch("repomatic.lint_repo.site.gh_json") as api:
        api.side_effect = [pages, history]
        result = check_pages_redirect_preserved(ctx)
    assert result.passed is passed
    assert fragment in result.message


def test_pages_redirect_check_follows_the_deploy_target():
    """Only a repository that moved away is asked to keep redirecting."""
    check = _roster_check("pages-redirect-preserved")
    cloudflare = Config(site_deploy="cloudflare-pages")
    assert check.applies(LintContext(repo="kdeldycke/repomatic", config=cloudflare))
    assert not check.applies(
        LintContext(
            repo="kdeldycke/repomatic", config=Config(site_deploy="github-pages")
        )
    )
    # Without a repository there is no Pages API to ask.
    assert not check.applies(LintContext(config=cloudflare))


def _lock_with(tmp_path: Path, *packages: tuple[str, str]) -> Path:
    """A minimal `uv.lock` holding one `[[package]]` entry per name/version pair."""
    lock = tmp_path / "uv.lock"
    lock.write_text(
        "version = 1\n"
        + "".join(
            f'\n[[package]]\nname = "{name}"\nversion = "{version}"\n'
            for name, version in packages
        ),
        encoding="UTF-8",
    )
    return lock


MANPAGES_CONTEXT = LintContext(config=Config(manpages_script="basket.cli:basket"))
"""A project opting into man pages, whose lockfile each test writes."""


@pytest.mark.parametrize(
    ("version", "passed"),
    (
        # 8.9.1 is the release the manpages job broke on: `wrap --man` stopped
        # writing roff in 9.0.0.
        ("8.9.1", False),
        ("9.0.0", True),
        ("9.1.0", True),
        ("10.0.0.dev1", True),
    ),
)
def test_manpages_toolchain_reads_the_locked_click_extra(
    tmp_path, monkeypatch, version, passed
):
    """The floor is checked against `uv.lock`, which the job installs from."""
    monkeypatch.chdir(tmp_path)
    _lock_with(tmp_path, ("click-extra", version), ("cherry", "1.0"))
    result = check_manpages_toolchain(MANPAGES_CONTEXT)
    assert result.passed is passed
    assert version in result.message


def test_manpages_toolchain_matches_a_non_canonical_lock_name(tmp_path, monkeypatch):
    """A lock spelling the package `Click_Extra` names the same project."""
    monkeypatch.chdir(tmp_path)
    _lock_with(tmp_path, ("Click_Extra", "9.0.0"))
    assert check_manpages_toolchain(MANPAGES_CONTEXT).passed is True


@pytest.mark.parametrize("packages", ((), (("cherry", "1.0"),)))
def test_manpages_toolchain_is_indeterminate_without_click_extra(
    tmp_path, monkeypatch, packages
):
    """A lock that never mentions click-extra answers neither pass nor fail."""
    monkeypatch.chdir(tmp_path)
    _lock_with(tmp_path, *packages)
    assert check_manpages_toolchain(MANPAGES_CONTEXT).passed is None


def test_manpages_toolchain_is_indeterminate_without_a_lock(tmp_path, monkeypatch):
    """A project with no lockfile is reported as unknown, not broken."""
    monkeypatch.chdir(tmp_path)
    result = check_manpages_toolchain(MANPAGES_CONTEXT)
    assert result.passed is None
    assert "missing" in result.message


def test_manpages_toolchain_only_applies_to_an_opted_in_project():
    """The roster entry stays silent until `manpages.script` is set."""
    check = _roster_check("manpages-toolchain")
    assert check.applies(LintContext()) is False
    assert check.applies(MANPAGES_CONTEXT) is True


BOOTSTRAP_CONFIGS = SyncMode.BOOTSTRAP.components()
"""Every tool config `init` seeds once and never revisits."""


def _tools(
    tool_table: Mapping[str, Any], lint_repo: LintRepoConfig | None = None
) -> LintContext:
    """A context holding *tool_table* as `[tool]`, and *lint_repo* as its record."""
    config = Config() if lint_repo is None else Config(lint_repo=lint_repo)
    return LintContext(config=config, tool_table=tool_table)


def _template_carries(template: Mapping, path: str, member: str | None) -> bool:
    """Whether a bundled template holds the entry a placeholder declaration names."""
    node = template
    for part in path.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return False
        node = node[part]
    return True if member is None else member in node


@pytest.mark.parametrize(
    ("template", "local", "expected"),
    (
        pytest.param({"basket": 1}, {}, (("basket", None),), id="missing-key"),
        pytest.param({"basket": 1}, {"basket": 2}, (), id="own-scalar-kept"),
        pytest.param({}, {"basket": 1}, (), id="local-only-key-kept"),
        pytest.param(
            {"fruits": ["apple", "pear"]},
            {"fruits": ["pear"]},
            (("fruits", "apple"),),
            id="missing-list-member",
        ),
        pytest.param(
            {"fruits": ["apple"]},
            {"fruits": ["apple", "plum"]},
            (),
            id="local-only-member-kept",
        ),
        pytest.param(
            {"basket": {"fruits": ["apple"], "weight": 3}},
            {"basket": {"fruits": []}},
            (("basket.fruits", "apple"), ("basket.weight", None)),
            id="nested-table",
        ),
        pytest.param(
            {"crates": [{"fruit": "apple", "ripe": True}]},
            {"crates": [{"fruit": "pear", "ripe": True}]},
            (("crates", '{ fruit = "apple", ripe = true }'),),
            id="missing-table-member",
        ),
        pytest.param(
            {"crates": [{"fruit": "apple", "ripe": True}]},
            {"crates": [{"ripe": True, "fruit": "apple"}]},
            (),
            id="table-member-in-another-key-order",
        ),
    ),
)
def test_missing_template_entries(template, local, expected):
    """Only absence is drift: a tuned value and a local addition are not."""
    assert tuple(missing_template_entries(template, local)) == expected


@pytest.mark.parametrize(
    ("member", "expected"),
    (
        pytest.param("apple", "apple", id="string"),
        pytest.param(3, "3", id="number"),
        pytest.param(
            {"fruit": "apple", "crates": ["north", "south"], "ripe": True},
            '{ fruit = "apple", crates = ["north", "south"], ripe = true }',
            id="table",
        ),
    ),
)
def test_template_member_name(member, expected):
    """A scalar is its own name, and a table is named by its inline TOML."""
    assert template_member_name(member) == expected


@pytest.mark.parametrize("comp", BOOTSTRAP_CONFIGS, ids=lambda c: c.name)
def test_bootstrap_config_drift_passes_on_its_own_template(comp):
    """A section holding the bundle verbatim is missing nothing."""
    local = tomlrt.loads(get_data_content(comp.source_file))
    results = tuple(check_bootstrap_config_drift(_tools({comp.tool_name: local})))
    assert [result.passed for result in results] == [True]


@pytest.mark.parametrize("comp", BOOTSTRAP_CONFIGS, ids=lambda c: c.name)
def test_bootstrap_config_drift_reports_a_dropped_entry(comp):
    """A knob the template gained after seeding reaches nobody on its own.

    `kdeldycke/dotfiles` carried a `[tool.ruff]` seeded before the bundle
    added `D301` to `lint.ignore`. `init` never revisits a BOOTSTRAP section,
    so the rule stayed absent with nothing reporting it.
    """
    local = tomlrt.loads(get_data_content(comp.source_file))
    dropped = next(iter(local))
    del local[dropped]
    results = tuple(check_bootstrap_config_drift(_tools({comp.tool_name: local})))
    assert [result.passed for result in results] == [False]
    assert f"`{dropped}`" in results[0].message


def test_bootstrap_config_drift_keeps_a_customized_placeholder():
    """`--cov=.` names the project's own package, so its absence is not drift."""
    local = tomlrt.loads(get_data_content("pytest.toml"))
    local["addopts"].remove("--cov=.")
    results = tuple(check_bootstrap_config_drift(_tools({"pytest": local})))
    assert [result.passed for result in results] == [True]


def test_bootstrap_config_drift_reads_the_pytest_legacy_table():
    """A section kept under `ini_options` is measured there, not reported empty.

    pytest refuses a file carrying both `[tool.pytest]` keys and
    `[tool.pytest.ini_options]`, so reporting the template's keys as missing from
    `[tool.pytest]` would prescribe a configuration pytest rejects.
    """
    legacy = tomlrt.loads(get_data_content("pytest.toml"))
    results = tuple(
        check_bootstrap_config_drift(_tools({"pytest": {"ini_options": legacy}}))
    )
    assert [result.passed for result in results] == [True]

    del legacy["xfail_strict"]
    results = tuple(
        check_bootstrap_config_drift(_tools({"pytest": {"ini_options": legacy}}))
    )
    assert [result.passed for result in results] == [False]
    assert results[0].message.startswith("[tool.pytest.ini_options] does not carry")
    assert "`xfail_strict`" in results[0].message


def _template_leaves(
    table: Mapping, keys: tuple[str, ...] = ()
) -> Iterator[tuple[tuple[str, ...], object]]:
    """Yield each key of a template, then each member of a list it holds."""
    for key, value in table.items():
        if isinstance(value, Mapping):
            yield from _template_leaves(value, (*keys, key))
            continue
        yield (*keys, key), None
        if isinstance(value, list):
            for member in value:
                yield (*keys, key), member


def test_template_table_member_names_write_the_member():
    """The name of a table must be TOML that writes that same table.

    The name is what a maintainer pastes into the section. A name that parsed
    to anything else would have them copy an entry the template does not hold,
    and the warning would stay.
    """
    tables = [
        (comp.source_file, member)
        for comp in BOOTSTRAP_CONFIGS
        for _, member in _template_leaves(
            tomlrt.loads(get_data_content(comp.source_file))
        )
        if isinstance(member, Mapping)
    ]
    assert tables, "No seeded template holds a table in a list."
    for source, member in tables:
        name = template_member_name(member)
        assert tomlrt.loads(f"member = {name}")["member"] == member, (
            f"{source}: {name!r} does not write the table it names."
        )


def _reportable_entries() -> Iterator:
    """Every entry of a seeded template whose absence `lint-repo` reports."""
    for comp in BOOTSTRAP_CONFIGS:
        template = tomlrt.loads(get_data_content(comp.source_file))
        for position, (keys, raw) in enumerate(_template_leaves(template)):
            member = None if raw is None else template_member_name(raw)
            path = ".".join(keys)
            if (path, member) in comp.customizable_entries:
                continue
            kind = "key" if member is None else "member"
            yield pytest.param(
                comp, keys, member, id=f"{comp.name}:{path}:{kind}{position}"
            )


def _declining(*leaf: str) -> LintRepoConfig:
    """Build the table that declines the one entry at `leaf`, for a fixed reason."""
    declined: dict | str = "Orchard closed."
    for key in reversed(leaf):
        declined = {key: declined}
    assert isinstance(declined, dict)
    return LintRepoConfig(declined=declined)


@pytest.mark.parametrize(("comp", "keys", "member"), tuple(_reportable_entries()))
def test_bootstrap_config_drift_declines_any_reportable_entry(comp, keys, member):
    """Every entry the check reports can be declined, at its path in the section.

    A missing key and a missing list member are named the way the tool's own
    section names them, so no entry needs a syntax of its own.
    """
    local = tomlrt.loads(get_data_content(comp.source_file))
    parent = local
    for key in keys[:-1]:
        parent = parent[key]
    if member is None:
        del parent[keys[-1]]
    else:
        members = parent[keys[-1]]
        members.remove(next(m for m in members if template_member_name(m) == member))

    # The control: without the record, the absence is a warning.
    results = tuple(check_bootstrap_config_drift(_tools({comp.tool_name: local})))
    assert [result.passed for result in results] == [False]

    leaf = (
        (comp.tool_name, *keys) if member is None else (comp.tool_name, *keys, member)
    )
    results = tuple(
        check_bootstrap_config_drift(_tools({comp.tool_name: local}, _declining(*leaf)))
    )
    assert [result.passed for result in results] == [None, True]
    assert results[0].message.startswith(f"[{comp.tool_section}] declines `")
    assert results[0].message.endswith(": Orchard closed.")
    assert "every other entry" in results[1].message


def test_bootstrap_config_drift_keeps_reporting_the_siblings_of_a_declined_member():
    """Declining one member of a list says nothing about the others."""
    local = tomlrt.loads(get_data_content("pytest.toml"))
    local["addopts"].remove("--numprocesses=auto")
    local["addopts"].remove("--cov-report=term")
    results = tuple(
        check_bootstrap_config_drift(
            _tools(
                {"pytest": local},
                _declining("pytest", "addopts", "--numprocesses=auto"),
            )
        )
    )
    assert [result.passed for result in results] == [None, False]
    assert '"--cov-report=term"' in results[1].message
    assert '"--numprocesses=auto"' not in results[1].message


def test_bootstrap_config_drift_declines_an_entry_of_the_pytest_legacy_table():
    """An entry is named below the tool, wherever the section keeps its keys."""
    legacy = tomlrt.loads(get_data_content("pytest.toml"))
    del legacy["xfail_strict"]
    results = tuple(
        check_bootstrap_config_drift(
            _tools(
                {"pytest": {"ini_options": legacy}},
                _declining("pytest", "xfail_strict"),
            )
        )
    )
    assert [result.passed for result in results] == [None, True]
    assert results[0].message.startswith(
        "[tool.pytest.ini_options] declines `xfail_strict`"
    )


@pytest.mark.parametrize(
    "reason",
    (
        pytest.param("", id="empty"),
        pytest.param("  ", id="blank"),
        pytest.param(True, id="boolean"),
        pytest.param(["Orchard closed."], id="list"),
        pytest.param({}, id="empty-table"),
    ),
)
def test_bootstrap_config_drift_wants_a_reason(reason):
    """An entry mapped to no reason declines nothing: the reason is the point."""
    local = tomlrt.loads(get_data_content("pytest.toml"))
    local["addopts"].remove("--numprocesses=auto")
    lint_repo_table = LintRepoConfig(
        declined={"pytest": {"addopts": {"--numprocesses=auto": reason}}}
    )
    results = tuple(
        check_bootstrap_config_drift(_tools({"pytest": local}, lint_repo_table))
    )
    assert [result.passed for result in results] == [False, False]
    assert '"--numprocesses=auto"' in results[0].message
    assert 'entry `pytest.addopts."--numprocesses=auto"` gives no reason' in (
        results[1].message
    )


@pytest.mark.parametrize(
    ("leaf", "verdict"),
    (
        pytest.param(
            ("pytest", "testpaths"),
            "`[tool.pytest]` carries what it names",
            id="carried-key",
        ),
        pytest.param(
            ("pytest", "addopts", "--durations=10"),
            "`[tool.pytest]` carries what it names",
            id="carried-member",
        ),
        pytest.param(
            ("pytest", "orchard"),
            "the bundled `pytest.toml` does not",
            id="key-the-template-lacks",
        ),
        pytest.param(
            ("pytest",),
            "`[tool.pytest]` carries what it names",
            id="whole-section",
        ),
        pytest.param(
            ("mypy", "pretty"),
            "the repository has no `[tool.mypy]` section",
            id="section-not-adopted",
        ),
        pytest.param(
            ("typos", "files"),
            "`typos` is not a tool config `init` seeds once",
            id="not-a-seeded-config",
        ),
    ),
)
def test_bootstrap_config_drift_reports_an_entry_that_declines_nothing(leaf, verdict):
    """A record that outlived its subject is reported, like the gap it hid.

    The section took the entry back, the template dropped it, or the path
    never named one: in each case the record now states a decision nobody is
    making, and the next real gap at that path would arrive already declined.
    """
    local = tomlrt.loads(get_data_content("pytest.toml"))
    results = tuple(
        check_bootstrap_config_drift(_tools({"pytest": local}, _declining(*leaf)))
    )
    assert [result.passed for result in results] == [True, False]
    assert "every entry" in results[0].message
    assert "declines nothing" in results[1].message
    assert verdict in results[1].message


def test_bootstrap_config_drift_roster_entry_reads_the_declined_table():
    """The roster hands the check the table, not only the section it measures."""
    check = _roster_check("bootstrap-config-drift")
    local = tomlrt.loads(get_data_content("pytest.toml"))
    local["addopts"].remove("--numprocesses=auto")
    ctx = _tools(
        {"pytest": local}, _declining("pytest", "addopts", "--numprocesses=auto")
    )
    assert [result.passed for result in check.results(ctx)] == [None, True]


def test_bootstrap_config_drift_is_settled_in_this_repository():
    """This repository carries or declines every entry of its seeded templates.

    A declined table is keyed by its inline TOML, so the record stops applying
    the day that rendering changes. The check then only warns, which nobody
    reads on a green run.
    """
    pyproject = tomlrt.loads(
        (PROJECT_ROOT / "pyproject.toml").read_text(encoding="UTF-8")
    )
    ctx = LintContext(
        config=load_repomatic_config(pyproject), tool_table=pyproject["tool"]
    )
    results = check_bootstrap_config_drift(ctx)
    assert [result.message for result in results if result.passed is False] == []


def test_lint_context_keeps_the_configuration_it_is_given(tmp_path, monkeypatch):
    """`from_project` hands the checks the configuration the command resolved."""
    metadata_from_pyproject(tmp_path, monkeypatch, '[project]\nname = "basket"\n')
    config = Config(
        lint_repo=_declining("pytest", "testpaths"), minimum_release_age="3 days"
    )
    ctx = LintContext.from_project(config)
    assert ctx.config is config


@pytest.mark.parametrize("comp", BOOTSTRAP_CONFIGS, ids=lambda c: c.name)
def test_customizable_entries_exist_in_their_template(comp):
    """A placeholder declaration goes stale silently, and then hides real drift."""
    template = tomlrt.loads(get_data_content(comp.source_file))
    for path, member in comp.customizable_entries:
        assert _template_carries(template, path, member), (
            f"{comp.name}: `{path}` member {member!r} is no longer in"
            f" {comp.source_file}, so its exemption now hides a real gap."
        )


@pytest.mark.parametrize(
    "comp",
    tuple(
        comp
        for comp in COMPONENTS
        if isinstance(comp, ToolConfigComponent)
        and comp.sync_mode is not SyncMode.BOOTSTRAP
    ),
    ids=lambda c: c.name,
)
def test_customizable_entries_are_bootstrap_only(comp):
    """An ongoing sync re-derives the section, so it has no placeholder to keep."""
    assert not comp.customizable_entries


BUNDLED_LYCHEE_EXCLUDES = tuple(
    str(item) for item in tomlrt.loads(get_data_content("lychee.toml"))["exclude"]
)
"""The canonical `[tool.lychee] exclude` list every deployed repository starts from."""


@pytest.mark.parametrize(
    ("pattern", "expected"),
    (
        pytest.param(
            r"^https://(www\.)?x\.com(/.*)?$",
            r"x\.com",
            id="anchored-host",
        ),
        pytest.param(
            r"^https://(www\.)?medium\.com(/.*)?$",
            r"medium\.com",
            id="anchored-host-optional-www",
        ),
        pytest.param(
            r"^https://www\.npmjs\.com/package/.*$",
            None,
            id="anchored-with-path",
        ),
        pytest.param(r"archive\.ph", None, id="bare-has-no-form-to-supersede"),
        pytest.param(r"x\.com", None, id="bare-is-not-anchored"),
        pytest.param(r"^https://star-history\.com/.*$", None, id="no-optional-www"),
    ),
)
def test_anchored_exclude_core(pattern, expected):
    """Only an anchored host-only pattern replaced a bare form."""
    assert anchored_exclude_core(pattern) == expected


def test_superseded_local_excludes_passes_on_the_bundle():
    """A section holding the bundle verbatim superseded nothing."""
    ctx = _tools({"lychee": {"exclude": list(BUNDLED_LYCHEE_EXCLUDES)}})
    results = tuple(check_superseded_local_excludes(ctx))
    assert [result.passed for result in results] == [True]


def test_superseded_local_excludes_reports_a_bare_entry_the_bundle_anchored():
    """The graft appends a local-only item and never retires a replaced one.

    Every awesome list carried a bare `x\\.com` beside the anchored form the
    bundle replaced it with, so lychee went on excluding every domain ending
    in `x.com` in all four of them.
    """
    local = [*BUNDLED_LYCHEE_EXCLUDES, r"x\.com"]
    results = tuple(
        check_superseded_local_excludes(_tools({"lychee": {"exclude": local}}))
    )
    assert [result.passed for result in results] == [False]
    assert r"Drop `x\.com`" in results[0].message


def test_superseded_local_excludes_replaces_a_bare_entry_left_alone():
    """A section no sync reaches lacks the anchored form, so a drop loses the host.

    A repository outside the awesome-only scope keeps its own `[tool.lychee]`
    copy, carrying the bare `x\\.com` where the bundle now has the anchored
    form. Dropping it there would stop excluding `x.com` itself.
    """
    anchored = r"^https://(www\.)?x\.com(/.*)?$"
    assert anchored in BUNDLED_LYCHEE_EXCLUDES
    local = [
        r"x\.com" if item == anchored else item for item in BUNDLED_LYCHEE_EXCLUDES
    ]
    results = tuple(
        check_superseded_local_excludes(_tools({"lychee": {"exclude": local}}))
    )
    assert [result.passed for result in results] == [False]
    assert rf"Replace `x\.com` with `{anchored}`" in results[0].message
    assert "Drop" not in results[0].message


def test_superseded_local_excludes_keeps_a_local_bare_entry_with_no_twin():
    """A repository's own bare pattern is a customization, not a leftover."""
    local = [*BUNDLED_LYCHEE_EXCLUDES, r"cloudlinux\.com"]
    results = tuple(
        check_superseded_local_excludes(_tools({"lychee": {"exclude": local}}))
    )
    assert [result.passed for result in results] == [True]


def test_superseded_local_excludes_keeps_a_local_anchored_entry():
    """An anchored local pattern narrows rather than broadens, so it is kept."""
    local = [*BUNDLED_LYCHEE_EXCLUDES, r"^https://example\.com/feed$"]
    results = tuple(
        check_superseded_local_excludes(_tools({"lychee": {"exclude": local}}))
    )
    assert [result.passed for result in results] == [True]


def test_superseded_local_excludes_ignores_a_tool_with_no_patterns():
    """Only a synced array of URL patterns has an anchored/bare distinction."""
    assert (
        tuple(check_superseded_local_excludes(_tools({"uv": {"exclude": ["x"]}}))) == ()
    )
    assert tuple(check_superseded_local_excludes(_tools({}))) == ()


def test_bundled_excludes_never_carry_both_forms():
    """Guard the bundle itself against the pair this check reports downstream.

    Not retroactive coverage: the bundle anchored `x\\.com` without ever
    shipping both forms, so the stale copies lived downstream alone. What this
    catches is the next time an entry is anchored here, since a bare pattern
    beside its anchored replacement is canonical in both and no downstream
    check can report it as drift.
    """
    cores = {
        core
        for core in (anchored_exclude_core(item) for item in BUNDLED_LYCHEE_EXCLUDES)
        if core is not None
    }
    both = [item for item in BUNDLED_LYCHEE_EXCLUDES if item in cores]
    assert not both, (
        f"lychee.toml carries {both} beside the anchored form that supersedes"
        f" it, so every deployed repository excludes more than the anchor"
        f" intends."
    )


def test_every_host_only_anchored_bundle_entry_is_readable():
    """An anchored entry the core extractor cannot read silently narrows the check.

    {func}`~repomatic.lint_repo.tool_config.anchored_exclude_core` recognises one
    shape. A host-only entry written in another answers `None`, so a bare
    downstream copy of it would go unreported. A path-shaped entry is expected
    to answer `None`: no bare form of one was ever bundled on its own.
    """
    for item in BUNDLED_LYCHEE_EXCLUDES:
        if not item.startswith("^") or anchored_exclude_core(item) is not None:
            continue
        after_scheme = item.split("://", 1)[-1].rstrip("$")
        assert "/" in after_scheme, (
            f"lychee.toml carries the host-only anchored entry {item!r}, which"
            f" `anchored_exclude_core` cannot read, so a bare downstream copy"
            f" of it goes unreported. Teach the extractor the shape."
        )


def _lint_context_in(tmp_path, monkeypatch, **fields) -> LintContext:
    """A `LintContext` whose filesystem lookups resolve inside *tmp_path*."""
    monkeypatch.chdir(tmp_path)
    return LintContext(**fields)


def test_pages_redirects_clean_file_passes(tmp_path, monkeypatch):
    """A file the engine keeps whole reports one green line."""
    (tmp_path / "_redirects").write_text(
        "/old /new 301\n/blog/* /articles/:splat 301\n",
        encoding="UTF-8",
    )
    ctx = _lint_context_in(tmp_path, monkeypatch)
    results = list(check_pages_redirects(ctx))
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
        for result in check_pages_redirects(ctx)
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
        for result in check_pages_redirects(ctx)
        if result.passed is False
    ]
    assert any("stops reading at line 101" in message for message in failures)
    # The messages read the budgets off the engine replica's constants.
    assert any("2000-rule static budget" in message for message in failures)
    # Lines 2 to 100 survive as dynamically-charged statics; line 101 died on
    # the budget and line 102 was never read.
    assert any("99 exact rule(s) sit below" in message for message in failures)
    # The count alone does not say which URL broke, so the abort names them.
    assert any("/casualty no longer redirects" in message for message in failures)


def test_pages_redirects_names_a_url_a_surviving_pattern_captured(
    tmp_path, monkeypatch
):
    """An abandoned exact rule whose URL a broader pattern now answers.

    The reader can find a dead redirect by following it. One that still
    redirects, to somewhere its author never wrote, looks healthy from the
    outside, so naming the new destination is the only way it surfaces.
    """
    lines = [f"/p{index}/:x /new{index} 301" for index in range(99)]
    lines += ["/fruit/* /harvest 301"]  # 100th dynamic rule, still parsed.
    lines += ["/burst/:y /boom 301"]  # 101st: the budget dies here.
    lines += ["/fruit/papaya /papaya-harvest 301"]  # Abandoned below the line.
    (tmp_path / "_redirects").write_text("\n".join(lines) + "\n", encoding="UTF-8")
    ctx = _lint_context_in(tmp_path, monkeypatch)
    failures = [
        result.message
        for result in check_pages_redirects(ctx)
        if result.passed is False
    ]
    assert any(
        "/fruit/papaya now redirects to /harvest instead of /papaya-harvest" in message
        for message in failures
    )


def test_pages_redirects_caps_the_urls_it_names(tmp_path, monkeypatch):
    """A tail of hundreds is summarized, not dumped into one lint message."""
    lines = [f"/p{index}/:x /new{index} 301" for index in range(101)]
    lines += [f"/lost-{index} /found-{index} 301" for index in range(20)]
    (tmp_path / "_redirects").write_text("\n".join(lines) + "\n", encoding="UTF-8")
    ctx = _lint_context_in(tmp_path, monkeypatch)
    abort = next(
        result.message
        for result in check_pages_redirects(ctx)
        if result.passed is False and "stops reading" in result.message
    )
    assert abort.count("no longer redirects") == MAX_REPORTED_DEAD_URLS
    # 101st dynamic rule plus 20 abandoned exact rules, minus the 5 named.
    assert "and 16 more rule(s) below them" in abort


def test_pages_redirects_names_the_exact_rules_a_pattern_shadows(tmp_path, monkeypatch):
    """An exact rule below a pattern that matches it is dead, and the lint says so.

    Moving it above the patterns is the fix, and it is also a change: the rule
    starts to fire. The failure names each such rule with the pattern that
    answers in its place, so the reader knows which answers the reorder moves.
    """
    (tmp_path / "_redirects").write_text(
        "/fruits/* /basket/:splat 301\n/fruits/pear /pear 301\n/roots/beet /beet 301\n",
        encoding="UTF-8",
    )
    ctx = _lint_context_in(tmp_path, monkeypatch)
    failures = [
        result.message
        for result in check_pages_redirects(ctx)
        if result.passed is False
    ]
    assert len(failures) == 1
    assert "2 exact rule(s) sit below" in failures[0]
    assert "/fruits/pear (line 2) is answered by /fruits/* on line 1" in failures[0]
    # The other late rule still fires: no pattern above it matches its source.
    assert "/roots/beet" not in failures[0]


@pytest.mark.parametrize(
    ("line", "fragment"),
    (
        pytest.param(
            "/fruits/*/crate/* /basket 301",
            "cannot compile the source /fruits/*/crate/*",
            id="two-splats",
        ),
        pytest.param(
            "/tree/:kind /orchard/:variety 301",
            "holds :variety",
            id="name-never-captured",
        ),
        pytest.param("/tree/* /orchard/* 301", "holds *", id="literal-star"),
    ),
)
def test_pages_redirects_reports_rules_that_cannot_work(
    tmp_path, monkeypatch, line, fragment
):
    """Rules the parser keeps and production still breaks, each with its line.

    The engine reports none of them: it drops a source it cannot compile at
    run time, and a destination token it does not replace reaches the visitor
    as literal text.
    """
    (tmp_path / "_redirects").write_text(
        f"/herbs /garden 301\n{line}\n", encoding="UTF-8"
    )
    ctx = _lint_context_in(tmp_path, monkeypatch)
    failures = [
        result.message
        for result in check_pages_redirects(ctx)
        if result.passed is False
    ]
    assert len(failures) == 1
    assert failures[0].startswith("_redirects:2:")
    assert fragment in failures[0]


def test_pages_redirects_accepts_a_literal_colon_in_an_exact_rule(
    tmp_path, monkeypatch
):
    """An exact source captures nothing, so a colon in its destination is text."""
    (tmp_path / "_redirects").write_text(
        "/guide https://example.org/wiki/Help:Contents 301\n", encoding="UTF-8"
    )
    ctx = _lint_context_in(tmp_path, monkeypatch)
    results = list(check_pages_redirects(ctx))
    assert [result.passed for result in results] == [True]


def test_pages_redirects_check_is_fatal_and_gated_on_the_file():
    """The roster entry must fail the run: dropped rules are already broken."""
    check = _roster_check("pages-redirects")
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
    ("wrangler_toml", "config_fields", "repo_name", "expected_failures"),
    (
        pytest.param(
            'name = "papaya-site"\ncompatibility_date = "2026-06-16"\n',
            {
                "site_cloudflare_project": "papaya-site",
                "site_cloudflare_compatibility_date": "2026-06-16",
            },
            None,
            [],
            id="everything-agrees",
        ),
        pytest.param(
            'name = "renamed-by-hand"\n',
            {"site_cloudflare_project": "papaya-site"},
            None,
            ["names project 'renamed-by-hand' while the deploy targets"],
            id="project-name-diverges",
        ),
        pytest.param(
            'name = "papaya"\ncompatibility_date = "2023-03-01"\n',
            {"site_cloudflare_compatibility_date": "2026-06-16"},
            "papaya",
            ["says compatibility_date 2023-03-01"],
            id="stale-compatibility-date",
        ),
        pytest.param(
            "compatibility_date = [broken\n",
            {},
            None,
            ["does not parse"],
            id="unparsable-file",
        ),
    ),
)
def test_wrangler_config_check(
    tmp_path, monkeypatch, wrangler_toml, config_fields, repo_name, expected_failures
):
    """`wrangler.toml` must agree with the declared project name and date.

    The file only feeds local wrangler commands on a Direct Upload project,
    which is exactly why nothing else ever catches it lying: one project's
    compatibility date sat three years behind the live value this way.
    """
    (tmp_path / "wrangler.toml").write_text(wrangler_toml, encoding="UTF-8")
    ctx = _lint_context_in(
        tmp_path, monkeypatch, config=Config(**config_fields), repo_name=repo_name
    )
    failures = [
        result.message
        for result in check_wrangler_config(ctx)
        if result.passed is False
    ]
    assert len(failures) == len(expected_failures)
    for message, expected in zip(failures, expected_failures, strict=True):
        assert expected in message


def test_minimal_run():
    """A context naming nothing fails no check."""
    assert not _fails(LintContext())


def test_cli_passes_a_project_with_nothing_wrong(tmp_path, monkeypatch):
    """`lint-repo` prints each verdict and exits 0 when no fatal check fails."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    monkeypatch.delenv("REPOMATIC_PAT", raising=False)
    result = CliRunner().invoke(repomatic, ["lint-repo"])
    assert result.exit_code == 0, result.output
    assert "ℹ PAT capability checks: skipped (no REPOMATIC_PAT)" in result.output
    assert "::error::" not in result.output


def test_cli_fails_on_a_fatal_finding(tmp_path, monkeypatch):
    """A fatal check failing turns into an error annotation and exit code 1."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    monkeypatch.delenv("REPOMATIC_PAT", raising=False)
    (tmp_path / "_redirects").write_text("/a /b 301\n/a /c 301\n", encoding="UTF-8")
    result = CliRunner().invoke(repomatic, ["lint-repo"])
    assert result.exit_code == 1, result.output
    assert "::error::" in result.output
    assert "✗ _redirects:2:" in result.output


def _topics_context(topics: list[str], keywords: list[str]) -> LintContext:
    """A context whose repository carries *topics*, and the project *keywords*."""
    return _offline(
        repo="owner/repo", keywords=keywords, repo_object={"topics": topics}
    )


def test_topics_all_in_keywords():
    """No warning when all topics are in keywords."""
    result = check_topics_subset_of_keywords(
        _topics_context(["python", "automation"], ["python", "automation", "cli"])
    )
    assert result.passed is True
    assert "2" in result.message


def test_topics_extra_not_in_keywords():
    """Warning when topics exist that are not in keywords."""
    result = check_topics_subset_of_keywords(
        _topics_context(["python", "unknown-topic"], ["python", "cli"])
    )
    assert result.passed is False
    assert "unknown-topic" in result.message


def test_topics_match_keywords_case_insensitively():
    """GitHub lowercases topics, so a capitalized keyword still counts."""
    result = check_topics_subset_of_keywords(
        _topics_context(["cli", "mmdf", "babyl"], ["CLI", "MMDF", "Babyl"])
    )
    assert result.passed is True


@pytest.mark.parametrize(
    "keyword", ("Weather forecast", "weather  forecast", " Weather forecast ")
)
def test_topics_match_multi_word_keywords(keyword):
    """A topic holds no whitespace, so a phrase counts as its hyphenated form."""
    result = check_topics_subset_of_keywords(
        _topics_context(["weather-forecast"], [keyword])
    )
    assert result.passed is True


def test_topics_phrase_keyword_does_not_declare_its_words():
    """A phrase declares its hyphenated topic, not one topic per word."""
    result = check_topics_subset_of_keywords(
        _topics_context(["weather-forecast", "weather"], ["Weather forecast"])
    )
    assert result.passed is False
    assert "keywords: weather. " in result.message


def test_topics_unreadable_repository():
    """Skip gracefully when the repository object cannot be read."""
    result = check_topics_subset_of_keywords(
        _offline(repo="owner/repo", keywords=["python"])
    )
    assert result.passed is None
    assert "skipped" in result.message


def test_topics_empty_response():
    """Skip when no topics are set on the repo."""
    result = check_topics_subset_of_keywords(_topics_context([], ["python"]))
    assert result.passed is None
    assert "skipped" in result.message


def test_stale_gh_pages_branch_absent_passes():
    """A 404 is the desired state: the branch is gone."""
    with patch("repomatic.lint_repo.site.run_gh_command") as mock_gh:
        mock_gh.side_effect = GhCommandError("HTTP 404: Branch not found")
        result = check_stale_gh_pages_branch(LintContext(repo="owner/repo"))
    assert result.passed is True


def test_stale_gh_pages_branch_present_fails():
    """A readable branch is the stale leftover the check exists to flag."""
    with patch("repomatic.lint_repo.site.run_gh_command") as mock_gh:
        mock_gh.return_value = '{"name": "gh-pages"}'
        result = check_stale_gh_pages_branch(LintContext(repo="owner/repo"))
    assert result.passed is False
    assert "gh-pages" in result.message


def test_stale_gh_pages_branch_api_failure_skips():
    """An unreadable API is a skip, never a pass.

    An auth flap or a rate limit must not vouch for a branch nobody read.
    """
    with patch("repomatic.lint_repo.site.run_gh_command") as mock_gh:
        mock_gh.side_effect = GhCommandError("HTTP 502: upstream error")
        result = check_stale_gh_pages_branch(LintContext(repo="owner/repo"))
    assert result.passed is None
    assert "skipped" in result.message


def _sponsors(*, has_sponsors: bool = True) -> dict:
    """A GraphQL `data` envelope for the funding check, already unwrapped."""
    return {"repositoryOwner": {"hasSponsorsListing": has_sponsors}}


@pytest.mark.parametrize("filename", ("FUNDING.yml", "funding.yml"))
def test_funding_file_exists(tmp_path, monkeypatch, filename):
    """No warning when funding file already exists, whatever its case."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / filename).write_text("github: owner\n", encoding="UTF-8")
    result = check_funding_file(_offline(repo="owner/repo"))
    assert result.passed is True
    assert "found" in result.message


def test_funding_missing_with_sponsors(tmp_path, monkeypatch):
    """Warning when owner has sponsors but no funding file."""
    monkeypatch.chdir(tmp_path)
    with patch("repomatic.lint_repo.repo.gh_graphql") as mock_gh:
        mock_gh.return_value = _sponsors(has_sponsors=True)
        result = check_funding_file(
            _offline(repo="owner/repo", repo_object={"fork": False})
        )
    assert result.passed is False
    assert "FUNDING.yml" in result.message
    assert "Sponsor" in result.message
    mock_gh.assert_called_once()
    assert mock_gh.call_args.kwargs == {"owner": "owner"}


def test_funding_skipped_for_fork(tmp_path, monkeypatch):
    """Skip funding check for forked repositories, before asking about Sponsors."""
    monkeypatch.chdir(tmp_path)
    with patch("repomatic.lint_repo.repo.gh_graphql") as mock_gh:
        result = check_funding_file(
            _offline(repo="owner/repo", repo_object={"fork": True})
        )
    assert result.passed is None
    assert "fork" in result.message
    mock_gh.assert_not_called()


def test_funding_skipped_no_sponsors(tmp_path, monkeypatch):
    """Skip when owner has no GitHub Sponsors listing."""
    monkeypatch.chdir(tmp_path)
    with patch("repomatic.lint_repo.repo.gh_graphql") as mock_gh:
        mock_gh.return_value = _sponsors(has_sponsors=False)
        result = check_funding_file(
            _offline(repo="owner/repo", repo_object={"fork": False})
        )
    assert result.passed is None
    assert "no GitHub Sponsors" in result.message


@pytest.mark.parametrize(
    ("repo_object", "graphql_error"),
    (
        pytest.param(None, None, id="unreadable-repository"),
        pytest.param({"fork": False}, GhCommandError("boom"), id="unreadable-owner"),
    ),
)
def test_funding_unreadable_payload(tmp_path, monkeypatch, repo_object, graphql_error):
    """Skip gracefully when the GitHub API cannot be read."""
    monkeypatch.chdir(tmp_path)
    with patch("repomatic.lint_repo.repo.gh_graphql", side_effect=graphql_error):
        result = check_funding_file(
            _offline(repo="owner/repo", repo_object=repo_object)
        )
    assert result.passed is None
    assert "skipped" in result.message


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
    failures = [
        r for r in check_workflow_permissions(LintContext()) if r.passed is False
    ]
    if expect_fail:
        assert failures
        assert any(needle in r.message for r in failures)
    else:
        assert not failures


def test_workflow_permissions_skip_without_workflows(tmp_path, monkeypatch):
    """A repository holding no workflow has nothing to report on."""
    monkeypatch.chdir(tmp_path)
    (result,) = check_workflow_permissions(LintContext())
    assert result.passed is None
    assert "no workflows" in result.message


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
            # The renderer honors the quoted spelling, so the check must too.
            None,
            id="quoted-footer",
        ),
        pytest.param(
            CANONICAL_TEMPLATE_PATH,
            CANONICAL_TEMPLATE_PATH,
            "---\ntitle: Sync fruit basket\nfooter: 'False'\n---\n\nBody.\n",
            "keeps the attribution footer",
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
    failures = [r for r in check_pr_templates(LintContext()) if r.passed is False]
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
    failures = [r for r in check_pr_templates(LintContext()) if r.passed is False]
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
    assert [r.message for r in check_pr_templates(LintContext())] == [
        f"PR template `{CANONICAL_TEMPLATE_PATH}`: conforms."
    ]


def test_pr_templates_unreferenced_still_checked(tmp_path, monkeypatch):
    """A template no workflow names is still validated, so drift stays visible."""
    monkeypatch.chdir(tmp_path)
    template = tmp_path / CANONICAL_TEMPLATE_PATH
    template.parent.mkdir(parents=True)
    template.write_text("---\ntitle: Sync fruit basket\n---\n", encoding="UTF-8")
    failures = [r for r in check_pr_templates(LintContext()) if r.passed is False]
    assert any("no `footer` field" in r.message for r in failures)


@pytest.mark.parametrize(
    "make_workflows_dir",
    [
        pytest.param(False, id="no-workflow-dir"),
        pytest.param(True, id="nothing-to-check"),
    ],
)
def test_pr_templates_skipped(tmp_path, monkeypatch, make_workflows_dir):
    """Skip cleanly when there is no workflow directory, or nothing to check."""
    monkeypatch.chdir(tmp_path)
    if make_workflows_dir:
        (tmp_path / ".github" / "workflows").mkdir(parents=True)
    results = check_pr_templates(LintContext())
    assert all(r.passed is None for r in results)
    assert any("no repository-local templates" in r.message for r in results)


# --- Stale draft releases check unit tests ---


def test_stale_drafts_detected():
    """Warn about draft releases that are not dev pre-releases."""
    with patch("repomatic.lint_repo.repo.gh_json") as mock_gh:
        mock_gh.return_value = [
            {"tagName": "v6.1.2", "isDraft": True},
            {"tagName": "v6.2.0", "isDraft": False},
            {"tagName": "v6.3.0.dev0", "isDraft": True},
        ]
        result = check_stale_draft_releases(LintContext(repo="owner/repo"))
    assert result.passed is False
    assert "v6.1.2" in result.message
    assert "v6.3.0.dev0" not in result.message
    # The listing names its limit: `gh release list` stops at 30 otherwise.
    args = mock_gh.call_args.args[0]
    assert args[args.index("--limit") + 1] == str(RELEASE_LIST_LIMIT)


def test_stale_drafts_none():
    """No warning when only dev pre-release drafts exist."""
    with patch("repomatic.lint_repo.repo.gh_json") as mock_gh:
        mock_gh.return_value = [
            {"tagName": "v6.2.0", "isDraft": False},
            {"tagName": "v6.3.0.dev0", "isDraft": True},
        ]
        result = check_stale_draft_releases(LintContext(repo="owner/repo"))
    assert result.passed is True
    assert "No stale" in result.message


def test_stale_drafts_unreadable_payload():
    """Skip gracefully when the release list cannot be read."""
    with patch("repomatic.lint_repo.repo.gh_json", return_value=None):
        result = check_stale_draft_releases(LintContext(repo="owner/repo"))
    assert result.passed is None
    assert "skipped" in result.message


def test_stale_drafts_multiple():
    """List all stale draft tags in the warning."""
    with patch("repomatic.lint_repo.repo.gh_json") as mock_gh:
        mock_gh.return_value = [
            {"tagName": "v6.1.2", "isDraft": True},
            {"tagName": "v6.2.0-rc1", "isDraft": True},
        ]
        result = check_stale_draft_releases(LintContext(repo="owner/repo"))
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

GUIDE_CONTEXT = LintContext(repo="owner/repo")
"""The context the install guide checks read, a repository and nothing else."""


def test_install_guide_downloads_all_present(tmp_path, monkeypatch):
    """Pass when every referenced file is attached to its release."""
    monkeypatch.chdir(tmp_path)
    _write_install_guide(tmp_path, GUIDE_WITH_DOWNLOADS)
    with patch("repomatic.lint_repo.repo.gh_json") as mock_gh:
        mock_gh.return_value = {
            "assets": [
                {"name": "papaya-1.2.3-linux-x64.bin"},
                {"name": "papaya-1.2.3-macos-arm64.bin"},
            ]
        }
        result = check_install_guide_downloads(GUIDE_CONTEXT)
    assert result.passed is True


GUIDE_WITH_LATEST_ALIAS = (
    "$ curl --fail --remote-name "
    "https://github.com/owner/repo/releases/latest/download/papaya-macos-x64.bin\n"
)


def test_install_guide_downloads_latest_alias_present(tmp_path, monkeypatch):
    """Check a versionless alias against the latest release, with no tag."""
    monkeypatch.chdir(tmp_path)
    _write_install_guide(tmp_path, GUIDE_WITH_LATEST_ALIAS)
    with patch("repomatic.lint_repo.repo.gh_json") as mock_gh:
        mock_gh.return_value = {"assets": [{"name": "papaya-macos-x64.bin"}]}
        result = check_install_guide_downloads(GUIDE_CONTEXT)
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
    with patch("repomatic.lint_repo.repo.gh_json") as mock_gh:
        mock_gh.return_value = {"assets": [{"name": "papaya-fruit-macos-x64.bin"}]}
        result = check_install_guide_downloads(GUIDE_CONTEXT)
    assert result.passed is False
    assert "latest/papaya-macos-x64.bin" in result.message


def test_install_guide_downloads_missing_asset(tmp_path, monkeypatch):
    """Warn naming each referenced file the release does not carry."""
    monkeypatch.chdir(tmp_path)
    _write_install_guide(tmp_path, GUIDE_WITH_DOWNLOADS)
    with patch("repomatic.lint_repo.repo.gh_json") as mock_gh:
        mock_gh.return_value = {"assets": [{"name": "papaya-1.2.3-linux-x64.bin"}]}
        result = check_install_guide_downloads(GUIDE_CONTEXT)
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
    with patch("repomatic.lint_repo.repo.gh_json", return_value={"assets": []}):
        result = check_install_guide_downloads(GUIDE_CONTEXT)
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
    result = check_install_guide_downloads(GUIDE_CONTEXT)
    assert result.passed is None
    assert needle in result.message


def test_install_guide_downloads_unreadable_release(tmp_path, monkeypatch):
    """Skip rather than warn when the release cannot be read.

    A transient API failure must not be reported as a broken install guide.
    """
    monkeypatch.chdir(tmp_path)
    _write_install_guide(tmp_path, GUIDE_WITH_DOWNLOADS)
    with patch("repomatic.lint_repo.repo.gh_json", return_value=None):
        result = check_install_guide_downloads(GUIDE_CONTEXT)
    assert result.passed is None
    assert "skipped" in result.message


# --- SHA pinning required check unit tests ---


SETTINGS_CONTEXT = LintContext(repo="owner/repo")
"""The context the repository-setting checks read, a repository and nothing else."""


def test_sha_pinning_required_enabled():
    """Pass when the repository requires SHA pinning for Actions."""
    with patch("repomatic.lint_repo.settings.gh_json") as mock_gh:
        mock_gh.return_value = {
            "enabled": True,
            "allowed_actions": "all",
            "sha_pinning_required": True,
        }
        result = check_sha_pinning_required(SETTINGS_CONTEXT)
    assert result.passed is True
    assert "enabled" in result.message


def test_sha_pinning_required_disabled():
    """Warn with a fix link when SHA pinning is not required."""
    with patch("repomatic.lint_repo.settings.gh_json") as mock_gh:
        mock_gh.return_value = {
            "enabled": True,
            "allowed_actions": "all",
            "sha_pinning_required": False,
        }
        result = check_sha_pinning_required(SETTINGS_CONTEXT)
    assert result.passed is False
    assert "owner/repo/settings/actions" in result.message


def test_sha_pinning_required_unreadable_payload():
    """Skip gracefully when the permissions payload cannot be read."""
    with patch("repomatic.lint_repo.settings.gh_json", return_value=None):
        result = check_sha_pinning_required(SETTINGS_CONTEXT)
    assert result.passed is None
    assert "skipped" in result.message


# --- PAT checks in the roster ---


def test_pat_checks_skipped_without_pat():
    """The PAT checks report one skip line when no PAT is configured."""
    (result,) = _findings(LintContext(has_pat=False))["pat-permissions"]
    assert result.passed is None
    assert "skipped (no REPOMATIC_PAT)" in result.message


def test_pat_checks_need_a_repository_to_probe():
    """A PAT with no repository to probe reports nothing, rather than a pass."""
    assert list(check_pat_permissions(LintContext(has_pat=True))) == []


def test_pat_checks_all_pass():
    """Every probe's own success message reaches the report."""
    with patch(
        "repomatic.lint_repo.context.check_all_pat_permissions",
        return_value=pat_results(),
    ):
        results = list(
            check_pat_permissions(LintContext(repo="owner/repo", has_pat=True))
        )
    messages = [result.message for result in results]
    assert all(result.passed for result in results)
    assert "Contents: token has access" in messages
    assert "Issues: token has access" in messages
    assert "Pull requests: token has access" in messages
    assert "Dependabot alerts: token has access, alerts enabled" in messages
    assert "Workflows: token has access" in messages


def test_pat_checks_fail_on_missing_permission():
    """A missing permission fails the run."""
    ctx = LintContext(repo="owner/repo", has_pat=True)
    ctx.__dict__["pat_permissions"] = pat_results(
        contents=(False, "Cannot access repository contents."),
        vulnerability_alerts=(
            False,
            "Token lacks 'Dependabot alerts: Read-only' permission.",
        ),
    )
    ctx.__dict__["pat_scope"] = CheckResult(True, "PAT scope: limited.")
    assert _fails(ctx)
    failed = [r for r in _findings(ctx)["pat-permissions"] if r.passed is False]
    assert [r.message for r in failed] == [
        "Cannot access repository contents.",
        "Token lacks 'Dependabot alerts: Read-only' permission.",
    ]


def test_pat_scope_reads_the_shared_probe():
    """The roster reads the scope probe the setup guide reads too, once."""
    overscoped = CheckResult(False, "PAT also has access to orchard/cherry.")
    ctx = LintContext(repo="orchard/apricot", has_pat=True)
    with patch(
        "repomatic.lint_repo.context.check_pat_repository_scope",
        return_value=overscoped,
    ) as probe:
        assert check_pat_scope(ctx) == overscoped
        assert ctx.pat_scope == overscoped
    probe.assert_called_once_with("orchard/apricot")


def test_pat_permission_entry_names_every_probe():
    """The help and the docs list the permissions the probes cover, all of them."""
    check = _roster_check("pat-permissions")
    for probe in PAT_PERMISSION_PROBES:
        assert probe.name in check.summary
        assert probe.name in check.description


def test_bootstrap_entry_names_every_seeded_section():
    """The docs list every tool section `init` seeds once."""
    description = _roster_check("bootstrap-config-drift").description
    for comp in SyncMode.BOOTSTRAP.components():
        assert f"`[{comp.tool_section}]`" in description


# --- PyPI Trusted Publisher check unit tests ---


PUBLISHER_CONTEXT = LintContext(repo="owner/cherries", package_name="cherries")
"""A published package whose Trusted Publisher entry the tests below probe."""


def test_pypi_trusted_publisher_no_release():
    """Skip when the package has no published version."""
    with patch(
        "repomatic.lint_repo.settings.get_latest_release_file",
        return_value=None,
    ):
        passed, msg = check_pypi_trusted_publisher(PUBLISHER_CONTEXT)
    assert passed is None
    assert "no released version" in msg
    assert "cherries" in msg


def test_pypi_trusted_publisher_no_provenance():
    """Indeterminate when provenance fetch fails (pre-OIDC release)."""
    with (
        patch(
            "repomatic.lint_repo.settings.get_latest_release_file",
            return_value=("1.2.3", "cherries-1.2.3-py3-none-any.whl"),
        ),
        patch(
            "repomatic.lint_repo.settings.get_trusted_publishers",
            return_value=None,
        ),
    ):
        passed, msg = check_pypi_trusted_publisher(PUBLISHER_CONTEXT)
    assert passed is None
    assert "no provenance" in msg
    assert "API token" in msg


def test_pypi_trusted_publisher_empty_bundles():
    """Indeterminate when provenance contains no publisher bundles."""
    with (
        patch(
            "repomatic.lint_repo.settings.get_latest_release_file",
            return_value=("1.2.3", "cherries-1.2.3-py3-none-any.whl"),
        ),
        patch(
            "repomatic.lint_repo.settings.get_trusted_publishers",
            return_value=[],
        ),
    ):
        passed, msg = check_pypi_trusted_publisher(PUBLISHER_CONTEXT)
    assert passed is None
    assert "no publisher bundles" in msg


def test_pypi_trusted_publisher_match():
    """Pass when a publisher bundle names this repo and `release.yaml`."""
    with (
        patch(
            "repomatic.lint_repo.settings.get_latest_release_file",
            return_value=("1.2.3", "cherries-1.2.3-py3-none-any.whl"),
        ),
        patch(
            "repomatic.lint_repo.settings.get_trusted_publishers",
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
        passed, msg = check_pypi_trusted_publisher(PUBLISHER_CONTEXT)
    assert passed is True
    assert "matches" in msg
    assert "owner/cherries" in msg


def test_pypi_trusted_publisher_workflow_mismatch():
    """Fail when provenance names a different workflow."""
    with (
        patch(
            "repomatic.lint_repo.settings.get_latest_release_file",
            return_value=("1.2.3", "cherries-1.2.3-py3-none-any.whl"),
        ),
        patch(
            "repomatic.lint_repo.settings.get_trusted_publishers",
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
        passed, msg = check_pypi_trusted_publisher(PUBLISHER_CONTEXT)
    assert passed is False
    assert "mismatch" in msg
    assert "publish.yaml" in msg
    assert "https://pypi.org/manage/project/cherries/settings/publishing/" in msg


def test_pypi_trusted_publisher_repository_mismatch():
    """Fail when provenance names a different repository (like the upstream one)."""
    with (
        patch(
            "repomatic.lint_repo.settings.get_latest_release_file",
            return_value=("1.2.3", "cherries-1.2.3-py3-none-any.whl"),
        ),
        patch(
            "repomatic.lint_repo.settings.get_trusted_publishers",
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
        passed, msg = check_pypi_trusted_publisher(PUBLISHER_CONTEXT)
    assert passed is False
    assert "upstream/orchard" in msg


@pytest.mark.parametrize(
    ("fields", "applies"),
    (
        pytest.param({"is_package": True}, True, id="package"),
        pytest.param({"is_package": False}, False, id="virtual-project"),
        pytest.param({"is_package": True, "package_name": None}, False, id="no-name"),
    ),
)
def test_pypi_trusted_publisher_gated_on_a_published_package(fields, applies):
    """A uv virtual project shares its `[project] name`, and publishes nothing."""
    check = _roster_check("pypi-trusted-publisher")
    base: dict[str, Any] = {"repo": "owner/cherries", "package_name": "cherries"}
    assert check.applies(LintContext(**(base | fields))) is applies


@pytest.mark.parametrize(
    ("has_changelog", "applies"),
    (
        pytest.param(True, True, id="publishes-releases"),
        pytest.param(False, False, id="no-changelog"),
    ),
)
def test_immutable_releases_gated_on_the_changelog(has_changelog, applies):
    """The setup guide and the lint gate the immutable releases step alike.

    The release workflow publishes a GitHub release for any project holding a
    changelog, Python package or not, so the changelog is the gate.
    """
    check = _roster_check("immutable-releases")
    ctx = LintContext(repo="owner/repo", has_changelog=has_changelog)
    assert check.applies(ctx) is applies


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

    results = check_test_matrix_excludes(LintContext())
    if stale:
        assert any(r.passed is False and runner in r.message for r in results)
    else:
        assert all(r.passed is not False for r in results)


def test_check_test_matrix_excludes_names_only_the_stale_half(tmp_path, monkeypatch):
    """A partially stale exclude is reported by its stale values alone.

    Under `full-include` the emitted matrix is a flat job list whose
    `all_variations()` is empty, so its axes must come from elsewhere: read
    from there, every key of the entry would count as absent, and the message
    would blame the live half along with the stale one.
    """
    metadata_from_pyproject(
        tmp_path,
        monkeypatch,
        '[project]\nname = "p"\nversion = "1.0.0"\n\n'
        "[tool.repomatic.test-matrix]\n"
        'full-include = [{os = "ubuntu-26.04-arm", python-version = "3.10"}]\n'
        'exclude = [{os = "ubuntu-26.04", python-version = "9.99"}]\n',
    )

    results = check_test_matrix_excludes(LintContext())
    failures = [r for r in results if r.passed is False]
    assert len(failures) == 1
    # Only the value no axis carries is named as absent; the live runner half
    # appears in the quoted entry but never in the absent-values list.
    assert "({'python-version': '9.99'})" in failures[0].message


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
    results = check_python_version_consistency(LintContext())
    assert any(
        r.passed is False and "requires-python floor" in r.message for r in results
    )


def test_python_matrix_missing_an_advertised_boundary(tmp_path, monkeypatch):
    """Advertising a version the matrix never reaches is untested support."""
    _write_project(tmp_path, monkeypatch, ["3.11", "3.12", "3.13"])
    _write_ci_workflow(tmp_path, _MATRIX.format(versions='"3.11", "3.12"'))
    results = check_python_version_consistency(LintContext())
    assert any(r.passed is False and "3.13" in r.message for r in results)


def test_python_matrix_may_skip_intermediate_versions(tmp_path, monkeypatch):
    """Only the ends of the range are mandatory, to keep CI load a free choice."""
    _write_project(tmp_path, monkeypatch, ["3.11", "3.12", "3.13"])
    _write_ci_workflow(tmp_path, _MATRIX.format(versions='"3.11", "3.13"'))
    results = check_python_version_consistency(LintContext())
    assert all(r.passed is not False for r in results)


def test_python_matrix_testing_an_unadvertised_version(tmp_path, monkeypatch):
    """A released version tested but not advertised is support left unclaimed."""
    _write_project(tmp_path, monkeypatch, ["3.11", "3.12"])
    _write_ci_workflow(tmp_path, _MATRIX.format(versions='"3.11", "3.12", "3.13"'))
    results = check_python_version_consistency(LintContext())
    assert any(r.passed is False and "3.13" in r.message for r in results)


def test_python_matrix_tolerates_unreleased_versions(tmp_path, monkeypatch):
    """An in-development version cannot be advertised, so it is exempt."""
    unstable = min(UNSTABLE_PYTHON_VERSIONS)
    _write_project(tmp_path, monkeypatch, ["3.11", "3.12"])
    _write_ci_workflow(
        tmp_path, _MATRIX.format(versions=f'"3.11", "3.12", "{unstable}"')
    )
    results = check_python_version_consistency(LintContext())
    assert all(r.passed is not False for r in results)


def test_python_matrix_built_at_runtime_is_skipped(tmp_path, monkeypatch):
    """A `fromJSON` matrix is opaque, and its axes are canonical already."""
    _write_project(tmp_path, monkeypatch, ["3.11", "3.12"])
    _write_ci_workflow(
        tmp_path,
        "on: push\njobs:\n  tests:\n    strategy:\n"
        "      matrix: ${{ fromJSON(needs.metadata.outputs.metadata).test_matrix }}\n"
        "    runs-on: ubuntu-slim\n    steps:\n      - run: echo apricot\n",
    )
    results = check_python_version_consistency(LintContext())
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
def test_runner_images(runner, expect_fail, needle):
    """Floating aliases and off-axis images are flagged; expressions are not."""
    ctx = _workflows(
        ci=f"on: push\njobs:\n  build:\n    runs-on: {runner}\n"
        "    steps:\n      - run: echo apricot\n"
    )
    failures = [r for r in check_runner_images(ctx) if r.passed is False]
    if expect_fail:
        assert failures
        assert any(needle in r.message for r in failures)
    else:
        assert not failures


def test_runner_images_ignores_thin_callers():
    """A job with no steps delegates, and its runner is the callee's business."""
    ctx = _workflows(
        ci="on: push\njobs:\n  build:\n    uses: ./.github/workflows/_build.yaml\n"
    )
    assert all(r.passed is not False for r in check_runner_images(ctx))


# --- Thin caller and workflow dispatch checks ---


def _caller(workflow: str, ref: str = "v5.8.0", extra: str = "") -> str:
    """A one-job workflow calling the upstream *workflow* at *ref*."""
    return (
        f"---\njobs:\n  {workflow.removesuffix('.yaml')}:\n"
        f"    uses: {DEFAULT_REPO}/.github/workflows/{workflow}@{ref}\n{extra}"
    )


@pytest.mark.parametrize(
    ("body", "passed", "needle"),
    [
        pytest.param(
            '---\n"on":\n  workflow_dispatch:\n  push:\n', True, "has", id="present"
        ),
        pytest.param('---\n"on":\n  push:\n', False, "missing", id="absent"),
    ],
)
def test_workflow_dispatch(body, passed, needle):
    """The `workflow_dispatch` check reports presence and absence."""
    (result,) = check_workflow_dispatch(_workflows(candidate=body))
    assert result.passed is passed
    assert needle in result.message


@pytest.mark.parametrize(
    "body",
    (
        pytest.param('---\n"on":\n  workflow_call:\n', id="reusable"),
        pytest.param(
            '---\n"on":\n  pull_request:\n  workflow_call:\n', id="reusable-with-push"
        ),
        pytest.param(_caller("lint.yaml"), id="thin-caller"),
    ),
)
def test_workflow_dispatch_leaves_out_reusable_workflows_and_callers(body):
    """A reusable workflow starts as its callers decide, a thin caller as it calls."""
    (result,) = check_workflow_dispatch(_workflows(candidate=body))
    assert result.passed is None
    assert "no standalone workflow" in result.message


def test_workflow_dispatch_settled_in_this_repository():
    """This repository's own workflows carry the trigger, or are exempt from it.

    `_release-build.yaml`, `_release-engine.yaml` and `cancel-runs.yaml` lack
    a manual trigger on purpose, and `lint-repo` runs on every push here.
    """
    results = check_workflow_dispatch(LintContext())
    assert [r.message for r in results if r.passed is False] == []


@pytest.mark.parametrize(
    ("ref", "passed", "needle"),
    [
        pytest.param("v5.8.0", True, "version is pinned", id="version-tag"),
        pytest.param(
            "1234567890abcdef1234567890abcdef12345678",
            True,
            "version is pinned",
            id="commit-sha",
        ),
        pytest.param("main", False, "@main", id="floating-branch"),
    ],
)
def test_thin_caller_pins(ref, passed, needle):
    """A caller must pin the upstream workflow to a release, never to a branch."""
    (result,) = check_thin_caller_pins(_workflows(lint=_caller("lint.yaml", ref)))
    assert result.passed is passed
    assert needle in result.message


def test_thin_caller_checks_skip_without_a_thin_caller():
    """A repository calling no upstream workflow has no caller to check."""
    ctx = _workflows(ci='---\n"on":\n  push:\njobs:\n  build:\n    runs-on: x\n')
    for check in (
        check_thin_caller_pins,
        check_thin_caller_triggers,
        check_thin_caller_secrets,
    ):
        (result,) = check(ctx)
        assert result.passed is None
        assert "no thin caller" in result.message


def test_thin_caller_triggers_match():
    """Pass when triggers match canonical."""
    # lint.yaml has: workflow_dispatch, push, pull_request.
    body = _caller("lint.yaml").replace(
        "---\n", '---\n"on":\n  workflow_dispatch:\n  push:\n  pull_request:\n'
    )
    (result,) = check_thin_caller_triggers(_workflows(lint=body))
    assert result.passed is True


def test_thin_caller_triggers_missing():
    """Fail when a trigger is missing."""
    body = _caller("lint.yaml").replace("---\n", '---\n"on":\n  push:\n')
    (result,) = check_thin_caller_triggers(_workflows(lint=body))
    assert result.passed is False
    assert "missing:" in result.message


def test_thin_caller_triggers_extra():
    """Fail when a caller declares a trigger absent from canonical."""
    canonical = yaml.safe_load(get_data_content("cancel-runs.yaml"))
    triggers = canonical.get(True, canonical.get("on"))
    triggers.pop("workflow_call")
    triggers["workflow_dispatch"] = None
    body = yaml.safe_dump({"on": triggers}) + _caller("cancel-runs.yaml").removeprefix(
        "---\n"
    )
    (result,) = check_thin_caller_triggers(_workflows(**{"cancel-runs": body}))
    assert result.passed is False
    assert "extra: workflow_dispatch" in result.message


def test_generated_release_caller_lints_clean():
    """The generated downstream `release.yaml` passes every workflow check.

    It synthesizes `push` and `workflow_dispatch` because `_release-engine.yaml`
    is call-only, and its build lane calls a workflow that declares no secrets.
    """
    ctx = _workflows(release=generate_thin_caller("release.yaml", version="v5.8.0"))
    for check in (
        check_workflow_dispatch,
        check_thin_caller_pins,
        check_thin_caller_triggers,
        check_thin_caller_secrets,
        check_workflow_permissions,
    ):
        assert [r.message for r in check(ctx) if r.passed is False] == []


def test_generated_cancel_runs_caller_is_exempt_from_the_dispatch_check():
    """`cancel-runs.yaml` lacks a manual trigger on purpose, and so may its caller."""
    ctx = _workflows(**{
        "cancel-runs": generate_thin_caller("cancel-runs.yaml", version="v5.8.0")
    })
    (dispatch,) = check_workflow_dispatch(ctx)
    assert dispatch.passed is None
    (triggers,) = check_thin_caller_triggers(ctx)
    assert triggers.passed is True


ALL_RELEASE_SECRETS = (
    "    secrets:\n"
    "      REPOMATIC_PAT: ${{ secrets.REPOMATIC_PAT }}\n"
    "      VIRUSTOTAL_API_KEY: ${{ secrets.VIRUSTOTAL_API_KEY }}\n"
)
"""Every secret `_release-engine.yaml` declares, passed explicitly."""


@pytest.mark.parametrize(
    ("workflow", "secrets_block", "passed", "needle"),
    [
        pytest.param(
            "_release-engine.yaml",
            ALL_RELEASE_SECRETS,
            True,
            "all secrets passed",
            id="all-passed-explicitly",
        ),
        # `secrets: inherit` predates the explicit form and still satisfies it.
        pytest.param(
            "_release-engine.yaml",
            "    secrets: inherit\n",
            True,
            "inherit",
            id="inherit",
        ),
        pytest.param(
            "_release-engine.yaml",
            "",
            False,
            "no secrets passed",
            id="none-passed",
        ),
        pytest.param(
            "_release-engine.yaml",
            "    secrets:\n"
            "      VIRUSTOTAL_API_KEY: ${{ secrets.VIRUSTOTAL_API_KEY }}\n",
            False,
            "REPOMATIC_PAT",
            id="partially-passed",
        ),
        pytest.param(
            "lint.yaml",
            "    secrets:\n"
            "      CLOUDFLARE_API_TOKEN: ${{ secrets.CLOUDFLARE_API_TOKEN }}\n"
            "      CLOUDFLARE_R2_ACCESS_KEY_ID:"
            " ${{ secrets.CLOUDFLARE_R2_ACCESS_KEY_ID }}\n"
            "      CLOUDFLARE_R2_SECRET_ACCESS_KEY:"
            " ${{ secrets.CLOUDFLARE_R2_SECRET_ACCESS_KEY }}\n"
            "      REPOMATIC_NOTIFICATIONS_PAT:"
            " ${{ secrets.REPOMATIC_NOTIFICATIONS_PAT }}\n"
            "      REPOMATIC_PAT: ${{ secrets.REPOMATIC_PAT }}\n"
            "      VIRUSTOTAL_API_KEY: ${{ secrets.VIRUSTOTAL_API_KEY }}\n",
            True,
            "all secrets passed",
            id="lint-all-passed",
        ),
    ],
)
def test_thin_caller_secrets(workflow, secrets_block, passed, needle):
    """A caller must forward every secret its canonical workflow declares."""
    ctx = _workflows(caller=_caller(workflow, extra=secrets_block))
    (result,) = check_thin_caller_secrets(ctx)
    assert result.passed is passed
    assert needle in result.message


def test_thin_caller_secrets_read_the_job_calling_the_canonical_workflow():
    """Another job's `secrets:` never stands in for the canonical lane's.

    The build lane comes first in the downstream `release.yaml`. Read in
    document order, its partial mapping was held against the engine's
    secrets, and the engine lane's own complete mapping was never read.
    """
    body = (
        "---\njobs:\n"
        "  build:\n"
        f"    uses: {DEFAULT_REPO}/.github/workflows/_release-build.yaml@v5.8.0\n"
        "    secrets:\n"
        "      VIRUSTOTAL_API_KEY: ${{ secrets.VIRUSTOTAL_API_KEY }}\n"
        "  release:\n"
        f"    uses: {DEFAULT_REPO}/.github/workflows/_release-engine.yaml@v5.8.0\n"
        f"{ALL_RELEASE_SECRETS}"
    )
    (result,) = check_thin_caller_secrets(_workflows(release=body))
    assert result.passed is True
    assert result.message.startswith("release.yaml:release:")


# A SHA-pinned thin-caller `uses:` ref to the upstream toolkit, declaring v6.29.0.
_UPSTREAM_REF = (
    "jobs:\n"
    "  lint:\n"
    "    uses: kdeldycke/repomatic/.github/workflows/lint.yaml"
    "@1234567890abcdef1234567890abcdef12345678 # v6.29.0\n"
)


def test_inline_pins_match_upstream_flags_lagging_pin():
    """An inline pin behind the uses: ref version is a fatal error."""
    ctx = _workflows(
        lint=_UPSTREAM_REF,
        tests="      - run: uvx --no-progress 'repomatic==6.28.1' metadata\n",
    )
    result = check_inline_pins_match_upstream(ctx)
    assert result.passed is False
    assert "6.28.1" in result.message
    assert "6.29.0" in result.message
    assert "tests.yaml" in result.message


def test_inline_pins_match_upstream_accepts_synced_pin():
    """An inline pin equal to the uses: ref version passes."""
    ctx = _workflows(
        lint=_UPSTREAM_REF,
        tests="      - run: uvx --no-progress 'repomatic==6.29.0' metadata\n",
    )
    result = check_inline_pins_match_upstream(ctx)
    assert result.passed is True
    assert "match" in result.message


def test_inline_pins_match_upstream_skips_without_refs():
    """The canonical repo (local `uv run`, no upstream refs) has nothing to check."""
    ctx = _workflows(
        tests="      - run: uv --no-progress run --frozen -- repomatic metadata\n"
    )
    result = check_inline_pins_match_upstream(ctx)
    assert result.passed is None
    assert "nothing to compare" in result.message


# ---------------------------------------------------------------------------
# Self-pin cooldown exemption check tests
# ---------------------------------------------------------------------------

_COOLDOWN_ENV = 'env:\n  UV_EXCLUDE_NEWER: "1 week"\n'
"""The workflow-level cooldown that makes an unexempt inline pin unresolvable."""


def test_self_pin_exemption_flags_unexempt_pin():
    """A pin resolving under a cooldown without the bypass is a fatal error."""
    ctx = _workflows(
        tests=_COOLDOWN_ENV
        + "      - run: uvx --no-progress 'repomatic==7.11.0' metadata\n"
    )
    result = check_self_pin_cooldown_exemption(ctx)
    assert result.passed is False
    assert "tests.yaml" in result.message
    assert SELF_PIN_COOLDOWN_EXEMPTION in result.message


def test_self_pin_exemption_accepts_exempt_pin():
    """The spelling both writers emit passes."""
    ctx = _workflows(
        tests=_COOLDOWN_ENV
        + f"      - run: uvx --no-progress {SELF_PIN_COOLDOWN_EXEMPTION}"
        " 'repomatic==7.11.0' metadata\n"
    )
    result = check_self_pin_cooldown_exemption(ctx)
    assert result.passed is True


def test_self_pin_exemption_ignores_workflow_without_cooldown():
    """No cooldown means nothing to exempt, so a bare pin resolves fine."""
    ctx = _workflows(
        tests="      - run: uvx --no-progress 'repomatic==7.11.0' metadata\n"
    )
    result = check_self_pin_cooldown_exemption(ctx)
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


def test_setup_uv_version_pin_flags_unpinned_step():
    """A step with no `version:` input installs whatever uv is newest."""
    result = check_setup_uv_version_pin(_workflows(tests=_setup_uv_workflow(None)))
    assert result.passed is False
    assert "tests.yaml" in result.message


def test_setup_uv_version_pin_accepts_pinned_steps():
    """Every step naming the same version passes."""
    result = check_setup_uv_version_pin(
        _workflows(tests=_setup_uv_workflow("0.12.1", "0.12.1"))
    )
    assert result.passed is True
    assert "0.12.1" in result.message


def test_setup_uv_version_pin_flags_unpinned_step_next_to_pinned_one():
    """A pinned step does not vouch for an unpinned one beside it."""
    result = check_setup_uv_version_pin(
        _workflows(tests=_setup_uv_workflow(None, "0.12.1"))
    )
    assert result.passed is False


def test_setup_uv_version_pin_flags_split_versions():
    """Steps pinning two versions silently test two uv releases."""
    result = check_setup_uv_version_pin(
        _workflows(
            tests=_setup_uv_workflow("0.12.1"), lint=_setup_uv_workflow("0.12.2")
        )
    )
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
            "uv --no-progress run --frozen -- repomatic show-metadata"
            " --output-format github-json"
            ' --output "$GITHUB_OUTPUT" cli_scripts package_name',
            ["cli_scripts", "package_name"],
            id="uv-run",
        ),
        # Downstream spelling: the CLI comes from a pinned uvx environment.
        pytest.param(
            "uvx --no-progress 'repomatic==7.11.0' show-metadata --format github-json"
            ' --output "$GITHUB_OUTPUT" cli_scripts package_name',
            ["cli_scripts", "package_name"],
            id="uvx-pinned",
        ),
        # The alias 8.0.0 removed requests nothing: the command it names is gone.
        pytest.param(
            "repomatic metadata --format github-json current_version",
            [],
            id="removed-alias",
        ),
        # An option's value is never a key, whether attached or separate.
        pytest.param(
            "repomatic show-metadata --format=json current_version",
            ["current_version"],
            id="inline-option-value",
        ),
        pytest.param(
            "repomatic show-metadata --output-format json current_version",
            ["current_version"],
            id="separate-option-value",
        ),
        pytest.param(
            "repomatic show-metadata -o out current_version",
            ["current_version"],
            id="short-option-value",
        ),
        # Words belonging to another command are not arguments to this one.
        pytest.param(
            "repomatic show-metadata current_version && echo done",
            ["current_version"],
            id="shell-operator",
        ),
        pytest.param(
            "repomatic show-metadata current_version\necho done",
            ["current_version"],
            id="second-line",
        ),
        pytest.param("uv run pytest -m once", [], id="unrelated-command"),
        # `show-metadata` also names a step id, an output and a job. Only the
        # token following the package invocation is the subcommand.
        pytest.param("echo show-metadata cli_scripts", [], id="bare-word"),
        pytest.param("grep show-metadata harvest.txt", [], id="bare-word-with-path"),
        # No positional key: the command dumps every one of them.
        pytest.param("uvx 'repomatic==7.11.0' show-metadata", [], id="no-keys"),
        pytest.param(
            "uvx repomatic show-metadata --output out.json", [], id="only-options"
        ),
        # An unbalanced quote is a line a shell rejects too: no guess beats a
        # wrong one.
        pytest.param(
            "uvx repomatic show-metadata 'test_matrix", [], id="unbalanced-quote"
        ),
    ],
)
def test_requested_metadata_keys(command, expected):
    """Positional keys are read off the command line the way Click reads them."""
    assert requested_metadata_keys(command, "repomatic") == expected


def test_metadata_keys_flags_a_retired_key():
    """A key removed upstream fails the lint instead of the next workflow run."""
    ctx = _workflows(
        ci=_metadata_step(
            "uvx --no-progress 'repomatic==7.11.0' show-metadata cli_scripts"
            " coverage_cells"
        )
    )
    failures = [r for r in check_metadata_keys(ctx) if r.passed is False]
    assert failures
    assert "coverage_cells" in failures[0].message
    assert "show-metadata --list-keys" in failures[0].message


@pytest.mark.parametrize(
    "command",
    (
        pytest.param("repomatic metadata cli_scripts", id="with-keys"),
        pytest.param("uvx 'repomatic==7.11.0' metadata", id="without-keys"),
    ),
)
def test_metadata_keys_flags_the_removed_alias(command):
    """A step calling the `metadata` alias fails the lint, naming `show-metadata`.

    `8.0.0` removed the alias, so the call dies whatever keys it passes, and
    even when it passes none.
    """
    ctx = _workflows(ci=_metadata_step(command))
    (failure,) = [r for r in check_metadata_keys(ctx) if r.passed is False]
    assert "`repomatic metadata`" in failure.message
    assert "`repomatic show-metadata`" in failure.message


def test_metadata_keys_accepts_current_keys():
    """Keys the command still answers raise nothing."""
    ctx = _workflows(
        ci=_metadata_step("repomatic show-metadata cli_scripts package_name")
    )
    assert all(r.passed is not False for r in check_metadata_keys(ctx))


def test_metadata_keys_skips_a_repo_that_never_calls_it():
    """No invocation to read is a skip, not a pass."""
    ctx = _workflows(ci=_metadata_step("echo apricot"))
    assert [r.passed for r in check_metadata_keys(ctx)] == [None]


def test_metadata_value_options_match_the_command():
    """The hand-listed value options cover every one the command declares.

    `check_metadata_keys` cannot import the CLI to ask (see
    `METADATA_VALUE_OPTIONS`), so the list is pinned here instead: an option
    gaining a value would otherwise make the lint read that value as a key.
    The list may hold more, since a workflow pinned to an older release still
    spells that release's options.
    """
    declared = {
        opt
        for param in show_metadata.params
        if not getattr(param, "is_flag", False)
        for opt in (*param.opts, *param.secondary_opts)
        if opt.startswith("-")
    }
    assert declared <= set(METADATA_VALUE_OPTIONS)


def test_metadata_keys_covers_every_workflow_of_this_repo():
    """This repository's own workflows only ask for keys that exist."""
    results = check_metadata_keys(LintContext())
    assert results
    assert all(r.passed is not False for r in results)


# ---------------------------------------------------------------------------
# Ruleset and branch protection check tests
# ---------------------------------------------------------------------------


def _rulesets_context(rulesets: list[dict] | None) -> LintContext:
    """A context whose rulesets API answered *rulesets*."""
    ctx = LintContext(repo="owner/repo")
    ctx.__dict__["rulesets"] = rulesets
    return ctx


def test_check_branch_ruleset_found():
    """Active branch ruleset is detected."""
    ctx = _rulesets_context([
        {"name": "main", "target": "branch", "enforcement": "active"},
    ])
    passed, msg = check_branch_ruleset_on_default(ctx)
    assert passed is True
    assert "main" in msg


@pytest.mark.parametrize(
    "rulesets",
    (
        pytest.param(
            [{"name": "tags", "target": "tag", "enforcement": "active"}],
            id="tag-ruleset-only",
        ),
        pytest.param(
            [{"name": "main", "target": "branch", "enforcement": "disabled"}],
            id="disabled-branch-ruleset",
        ),
    ),
)
def test_check_branch_ruleset_none(rulesets):
    """No active branch ruleset returns failure."""
    passed, _msg = check_branch_ruleset_on_default(_rulesets_context(rulesets))
    assert passed is False


def test_check_branch_ruleset_api_error():
    """An unreadable rulesets API is indeterminate, not a failure.

    Matches `check_tag_protection_rules`, which reads the same payload. The
    setup guide still treats the `None` as incomplete and keeps the step open.
    """
    passed, msg = check_branch_ruleset_on_default(_rulesets_context(None))
    assert passed is None
    assert "skipped" in msg


@pytest.mark.parametrize(
    ("rulesets", "passed"),
    (
        pytest.param(
            [{"name": "release-tags", "target": "tag", "enforcement": "active"}],
            False,
            id="active-tag-ruleset",
        ),
        pytest.param(
            [{"name": "release-tags", "target": "tag", "enforcement": "evaluate"}],
            True,
            id="evaluate-mode",
        ),
        pytest.param(None, None, id="unreadable"),
    ),
)
def test_check_tag_protection_rules(rulesets, passed):
    """Only an active ruleset aimed at tags can block the release tag push."""
    result = check_tag_protection_rules(_rulesets_context(rulesets))
    assert result.passed is passed


def _graphql_rules(*patterns: str) -> dict:
    """Wrap branch patterns in the envelope `gh_graphql` hands back."""
    return {
        "repository": {
            "branchProtectionRules": {
                "nodes": [{"pattern": pattern} for pattern in patterns]
            }
        }
    }


def test_check_classic_branch_protection_none():
    """A repository holding only rulesets passes."""
    with patch(
        "repomatic.lint_repo.settings.gh_graphql", return_value=_graphql_rules()
    ):
        passed, msg = check_classic_branch_protection(SETTINGS_CONTEXT)
    assert passed is True
    assert "rulesets" in msg


@pytest.mark.parametrize(
    ("patterns", "expected"),
    (
        (("main",), ("`main`",)),
        # Sorted, so the message is stable whatever order the API answers in.
        (("release/*", "main"), ("`main`", "`release/*`")),
    ),
)
def test_check_classic_branch_protection_found(patterns, expected):
    """Every surviving rule is named, and both settings pages are linked."""
    with patch(
        "repomatic.lint_repo.settings.gh_graphql",
        return_value=_graphql_rules(*patterns),
    ):
        passed, msg = check_classic_branch_protection(SETTINGS_CONTEXT)
    assert passed is False
    positions = [msg.find(pattern) for pattern in expected]
    assert all(position >= 0 for position in positions)
    assert positions == sorted(positions)
    assert "https://github.com/owner/repo/settings/rules" in msg
    assert "https://github.com/owner/repo/settings/branches" in msg


def test_check_classic_branch_protection_api_error():
    """Reading the rules needs admin, so a refusal skips instead of passing.

    A token without admin gets the same `RuntimeError` as a network failure.
    Answering `True` there would report a clean repository on the strength of
    a question nobody managed to ask.
    """
    with patch(
        "repomatic.lint_repo.settings.gh_graphql", side_effect=GhCommandError("403")
    ):
        passed, msg = check_classic_branch_protection(SETTINGS_CONTEXT)
    assert passed is None
    assert "skipped" in msg


@pytest.mark.parametrize(
    "payload",
    (
        None,
        {},
        {"repository": None},
        {"repository": {"branchProtectionRules": None}},
        {"repository": {"branchProtectionRules": {"nodes": None}}},
    ),
)
def test_check_classic_branch_protection_partial_payload(payload):
    """A partial GraphQL response reads as no rules, never as a crash.

    GraphQL answers `200` with a null branch when a field resolves to nothing,
    so every level of the envelope can arrive empty.
    """
    with patch("repomatic.lint_repo.settings.gh_graphql", return_value=payload):
        passed, _msg = check_classic_branch_protection(SETTINGS_CONTEXT)
    assert passed is True


@pytest.mark.parametrize(
    ("response", "passed", "fragment"),
    (
        pytest.param(
            {"enabled": True, "enforced_by_owner": False}, True, "enabled", id="on"
        ),
        pytest.param(
            {"enabled": False, "enforced_by_owner": False},
            False,
            "not enabled",
            id="off",
        ),
        # An API failure is indeterminate, not a failure.
        pytest.param(None, None, "skipped", id="unreadable"),
    ),
)
def test_check_immutable_releases(response, passed, fragment):
    """The immutable releases setting reads as on, off or unknown."""
    with patch("repomatic.lint_repo.settings.gh_json", return_value=response):
        result = check_immutable_releases(SETTINGS_CONTEXT)
    assert result.passed is passed
    assert fragment in result.message


# ---------------------------------------------------------------------------
# Roster conformance tests
# ---------------------------------------------------------------------------


def _package_checks() -> dict[str, Callable]:
    """Every `check_*` function the `repomatic.lint_repo` modules define."""
    checks = {}
    for module_info in pkgutil.iter_modules(lint_repo.__path__):
        module = importlib.import_module(f"{lint_repo.__name__}.{module_info.name}")
        for name, value in vars(module).items():
            if (
                name.startswith("check_")
                and callable(value)
                and getattr(value, "__module__", "") == module.__name__
            ):
                checks[f"{module_info.name}.{name}"] = value
    return checks


def test_every_check_is_reachable_from_the_roster():
    """Every `check_*` the package defines is a check `lint-repo` runs.

    A check nobody calls is a check nobody benefits from: adding one means
    adding it to the roster, or the omission fails here.
    """
    run = {check.run for check in REPO_CHECKS}
    unreachable = sorted(
        name for name, func in _package_checks().items() if func not in run
    )
    assert not unreachable, (
        f"checks defined but never run by lint-repo: {unreachable}."
        " Add them to REPO_CHECKS."
    )


def test_repo_check_names_are_unique():
    """Two checks sharing a name would make the roster ambiguous to read."""
    names = [check.name for check in REPO_CHECKS]
    assert len(set(names)) == len(names), "duplicate RepoCheck names"


def test_help_lists_every_check():
    """`lint-repo --help` names every check the command runs, with its severity.

    The list renders from `REPO_CHECKS`, so this guards the wiring: a placeholder
    left unfilled, or a check the rendering drops, fails here.
    """
    result = CliRunner().invoke(repomatic, ["lint-repo", "--help"])
    assert result.exit_code == 0, result.output
    text = " ".join(result.output.split())
    assert "{checks}" not in text
    missing = [
        check.name
        for check in REPO_CHECKS
        if f"{check.summary} ({'error' if check.fatal else 'warning'})." not in text
    ]
    assert not missing, f"Checks absent from `lint-repo --help`: {missing}"


@pytest.mark.parametrize("check", REPO_CHECKS, ids=lambda check: check.name)
def test_check_description_opens_on_its_severity(check):
    """A check's docs bullet says whether it fails the job or only warns.

    A fatal check's description opens on `Fails`, any other on `Warns`, or on
    `Reports` for one that reports before it warns. The docs page renders these
    bullets under the `lint-repo` job, so a wrong verb misstates the severity.
    """
    verb = check.description.split(" ", 1)[0]
    if check.fatal:
        assert verb == "Fails", f"{check.name} is fatal: open on 'Fails'."
    else:
        assert verb in {"Warns", "Reports"}, f"{check.name} is not fatal: {verb!r}."
    assert not check.description.endswith("."), (
        f"{check.name}: job bullets on the page carry no closing period."
    )


def test_workflows_page_renders_every_check():
    """The `lint-repo` job docs render the roster rather than a hand-kept list."""
    page = (PROJECT_ROOT / "docs" / "workflows.md").read_text(encoding="UTF-8")
    job = page.partition("### 🏠 Lint repository metadata (`lint-repo`)")[2]
    job = job.partition("\n### ")[0]
    assert "print(repo_check_reference())" in job
    rendered = repo_check_reference().splitlines()
    assert rendered == [f"- {check.description}" for check in REPO_CHECKS]


@pytest.mark.parametrize(
    ("passed", "fatal", "fails"),
    (
        (False, True, True),
        (False, False, False),
        (None, True, False),
        (True, True, False),
    ),
)
def test_only_a_fatal_failure_fails_the_run(passed, fatal, fails):
    """A skip or an advisory failure leaves the run green."""
    check = RepoCheck("fig", lambda ctx: (), summary="", description="", fatal=fatal)
    assert check.fails(CheckResult(passed, "fig")) is fails


# ---------------------------------------------------------------------------
# setup-uv checksum coverage check tests
# ---------------------------------------------------------------------------


def _coverage(verified, *versions):
    """Run the coverage check over a workflow pinning *versions*."""
    ctx = _workflows(tests=_setup_uv_workflow(*versions))
    with patch(
        "repomatic.lint_repo.pins.setup_uv_verified_versions", return_value=verified
    ):
        return check_setup_uv_checksum_coverage(ctx)


def test_setup_uv_checksum_coverage_accepts_a_covered_pin():
    """A pinned uv the pinned action can hash is the state worth having."""
    result = _coverage(frozenset({"0.12.3"}), "0.12.3")
    assert result.passed is True
    assert "0.12.3" in result.message


def test_setup_uv_checksum_coverage_flags_an_uncovered_pin():
    """Two good pins can still verify nothing between them.

    `setup-uv` skips validation for a version absent from the table its own
    release bundles, on a debug line no CI log shows, so nothing else reports
    this.
    """
    result = _coverage(frozenset({"0.11.30"}), "0.12.3")
    assert result.passed is False
    assert "0.12.3" in result.message


def test_setup_uv_checksum_coverage_skips_on_an_unreadable_table():
    """An unreachable API is indeterminate, never a red run."""
    result = _coverage(None, "0.12.3")
    assert result.passed is None
    assert "unreadable" in result.message


def test_setup_uv_checksum_coverage_skips_without_a_pinned_version():
    """An unpinned step is the other check's finding, not this one's."""
    result = _coverage(frozenset({"0.12.3"}), None)
    assert result.passed is None
    assert "no pinned uv version" in result.message


def test_setup_uv_checksum_coverage_skips_without_workflows():
    """A repository with no workflow has nothing to check."""
    assert check_setup_uv_checksum_coverage(_workflows()).passed is None


# ---------------------------------------------------------------------------
# Registry pin cooldown check tests
# ---------------------------------------------------------------------------


COOLDOWN_NOW = datetime(2026, 10, 20, 12, tzinfo=UTC)
"""The clock the cooldown tests read, so that no test crosses a UTC midnight."""

YOUNG_RELEASE = "2026-10-18"
"""A release date inside the `1 week` window at {data}`COOLDOWN_NOW`."""

OLD_RELEASE = "2026-10-01"
"""A release date past the `1 week` window at {data}`COOLDOWN_NOW`."""

VENV_TOOL = ToolSpec(name="dragon-fruit", version="2.0.0", needs_venv=True)
"""A tool installed into the project virtualenv, which reads `[tool.uv]`."""

UVX_TOOL = ToolSpec(name="kiwi", version="1.5.0")
"""A tool installed by `uvx`, which reads no project configuration."""

NPM_TOOL = ToolSpec(name="mango", version="3.1.0", npm=NpmSpec())
"""A tool installed by npm into a scratch prefix."""

BINARY_TOOL = ToolSpec(
    name="fig",
    version="0.9.0",
    binary=BinarySpec(urls={}, checksums={}, archive_format=ArchiveFormat.TAR_GZ),
    source_url="https://github.com/orchard/fig",
)
"""A tool downloaded as a checksum-pinned binary, with no install-time cooldown."""


def _dated(released: str) -> Callable[[ToolSpec], list[Candidate]]:
    """A `tool_candidates` stand-in dating every pinned version at *released*."""
    return lambda spec: [Candidate(spec.version, released, spec.version)]


def _pin_cooldown(
    tool: ToolSpec,
    candidates: Callable[[ToolSpec], list[Candidate]],
    entries: Mapping[str, object] | None = None,
    minimum_release_age: str = "1 week",
) -> list[CheckResult]:
    """Run the cooldown check over *tool* alone, its releases from *candidates*."""
    ctx = LintContext(
        config=Config(minimum_release_age=minimum_release_age),
        tool_table={"uv": {"exclude-newer-package": dict(entries or {})}},
    )
    with patch("repomatic.lint_repo.pins.tool_candidates", side_effect=candidates):
        return check_registry_pin_cooldown(
            ctx, registry={tool.name: tool}, now=COOLDOWN_NOW
        )


@pytest.mark.parametrize(
    "entries",
    (
        pytest.param({}, id="no-entry"),
        pytest.param({"cherry": "0 day"}, id="entry-for-another-package"),
    ),
)
def test_registry_pin_cooldown_warns_without_an_entry(entries):
    """A virtualenv pin inside the window and with no entry fails to install.

    The warning carries all a maintainer needs to act on it: the pin, its
    release date, the day it clears the window, and the entry that admits it
    now, in the freeze form that `sync-uv-lock` prunes once it ages out.
    """
    (result,) = _pin_cooldown(VENV_TOOL, _dated(YOUNG_RELEASE), entries)
    assert result.passed is False
    assert result.message == (
        "`repomatic run dragon-fruit` cannot install 2.0.0, published 2026-10-18,"
        " until 2026-10-25 (in 5 days), when it clears the 1 week cooldown: add"
        ' `dragon-fruit = "2026-10-20T00:00:00Z"` to [tool.uv]'
        " exclude-newer-package to admit it now."
    )


@pytest.mark.parametrize(
    "value",
    (
        pytest.param("2026-10-18T00:00:00Z", id="start-of-the-release-day"),
        pytest.param("2026-10-17", id="bare-date-before-the-release-day"),
        pytest.param("2026-10-01T00:00:00Z", id="weeks-before-the-release"),
        pytest.param("1 week", id="span-older-than-the-release"),
        pytest.param("papaya", id="unparsable"),
    ),
)
def test_registry_pin_cooldown_warns_on_an_entry_older_than_the_release(value):
    """An entry whose cutoff can fall before the upload admits nothing.

    The datasource dates a release to the day, so a cutoff inside that day may
    still exclude the upload, and counts as not admitting it.
    """
    (result,) = _pin_cooldown(VENV_TOOL, _dated(YOUNG_RELEASE), {"dragon-fruit": value})
    assert result.passed is False
    assert 'add `dragon-fruit = "2026-10-20T00:00:00Z"`' in result.message


@pytest.mark.parametrize(
    "entries",
    (
        pytest.param({"dragon-fruit": "2026-10-20T00:00:00Z"}, id="freeze-form"),
        pytest.param({"dragon-fruit": "2026-10-19T00:00:00Z"}, id="end-of-day"),
        pytest.param({"dragon-fruit": "2026-10-18"}, id="bare-release-date"),
        pytest.param({"dragon-fruit": "0 day"}, id="zero-span"),
        pytest.param({"dragon-fruit": "P0D"}, id="zero-iso-span"),
        pytest.param({"dragon-fruit": False}, id="cooldown-lifted"),
        pytest.param({"Dragon_Fruit": "0 day"}, id="other-pep-503-spelling"),
    ),
)
def test_registry_pin_cooldown_passes_on_an_admitting_entry(entries):
    """An entry uv reads as admitting the release lets the install through."""
    (result,) = _pin_cooldown(VENV_TOOL, _dated(YOUNG_RELEASE), entries)
    assert result.passed is True
    assert result.message == (
        "dragon-fruit 2.0.0, published 2026-10-18, is inside the 1 week cooldown"
        " until 2026-10-25 (in 5 days), and its [tool.uv] exclude-newer-package"
        " entry admits it."
    )


@pytest.mark.parametrize("tool", (UVX_TOOL, NPM_TOOL), ids=lambda tool: tool.name)
def test_registry_pin_cooldown_warns_on_a_pin_no_entry_reaches(tool):
    """A `uvx` or npm pin inside the window waits it out, whatever the entries say.

    The warning names no entry: neither install reads one, so suggesting one
    would point at a fix that changes nothing.
    """
    (result,) = _pin_cooldown(tool, _dated(YOUNG_RELEASE), {tool.name: "0 day"})
    assert result.passed is False
    assert result.message.startswith(
        f"`repomatic run {tool.name}` cannot install {tool.version}, published"
        " 2026-10-18, until 2026-10-25 (in 5 days)"
    )
    assert "reads no exclude-newer-package entry" in result.message
    assert " = " not in result.message


@pytest.mark.parametrize(
    "tool", (VENV_TOOL, UVX_TOOL, NPM_TOOL), ids=lambda tool: tool.name
)
def test_registry_pin_cooldown_passes_past_the_window(tool):
    """A pin past the window installs on every backend, with or without entries."""
    (result,) = _pin_cooldown(tool, _dated(OLD_RELEASE))
    assert result.passed is True
    assert result.message == "PyPI and npm tool pins (1) are past the 1 week cooldown."


@pytest.mark.parametrize(
    "candidates",
    (
        pytest.param([], id="lookup-failed"),
        pytest.param(
            [Candidate("9.9.9", YOUNG_RELEASE, "9.9.9")], id="pinned-version-absent"
        ),
        pytest.param([Candidate("2.0.0", "", "2.0.0")], id="undated-release"),
    ),
)
def test_registry_pin_cooldown_skips_an_undated_pin(candidates):
    """A pin the datasource cannot date is indeterminate, never a failure."""
    (result,) = _pin_cooldown(VENV_TOOL, lambda _spec: candidates)
    assert result.passed is None
    assert result.message == (
        "Registry pin cooldown: skipped for dragon-fruit (no release date for the"
        " pinned version)."
    )


def test_registry_pin_cooldown_leaves_binary_tools_alone():
    """A binary pin is never looked up: its download applies no cooldown."""
    lookup = Mock(side_effect=_dated(YOUNG_RELEASE))
    assert _pin_cooldown(BINARY_TOOL, lookup) == []
    lookup.assert_not_called()


def test_registry_pin_cooldown_skips_without_a_cooldown():
    """With no window to clear, nothing is held back and nothing is looked up."""
    lookup = Mock(side_effect=_dated(YOUNG_RELEASE))
    (result,) = _pin_cooldown(VENV_TOOL, lookup, minimum_release_age="0 days")
    assert result.passed is None
    assert "skipped" in result.message
    lookup.assert_not_called()


def test_registry_pin_cooldown_never_fails_the_run():
    """Every registry pin inside the window warns, and `lint-repo` still passes."""
    check = _roster_check("registry-pin-cooldown")
    assert check.fatal is False
    today = datetime.now(UTC).date().isoformat()
    ctx = LintContext()
    with patch("repomatic.lint_repo.pins.tool_candidates", side_effect=_dated(today)):
        findings = _findings(ctx)
        assert not _fails(ctx)
    assert any(
        r.passed is False and r.message.startswith("`repomatic run ")
        for r in findings["registry-pin-cooldown"]
    )


# --- Undeclared labels check unit tests ---


def _labels_payload(*names: str) -> list[dict[str, str]]:
    """A `GET /repos/{repo}/labels` payload carrying *names*."""
    return [{"name": name, "color": "ffffff", "description": ""} for name in names]


def _declaring(*names: str) -> LintContext:
    """A context whose configured label sources declare *names*."""
    ctx = LintContext(repo="owner/repo")
    ctx.__dict__["declared_labels"] = frozenset(names)
    return ctx


def test_undeclared_labels_detected():
    """Report a label the repository carries that no source declares."""
    with patch("repomatic.lint_repo.repo.gh_json") as mock_gh:
        mock_gh.return_value = _labels_payload("🐛 bug", "📦 manager: nala")
        result = check_undeclared_labels(_declaring("🐛 bug"))
    assert result.passed is False
    assert "📦 manager: nala" in result.message
    # The declared one is never named as an orphan.
    assert "🐛 bug" not in result.message


def test_undeclared_labels_warns_before_deleting():
    """The finding carries the usage count step, not just the names.

    Deleting a label detaches every item carrying it, so a check that only
    named the orphans would invite exactly the loss it exists to prevent.
    """
    with patch("repomatic.lint_repo.repo.gh_json") as mock_gh:
        mock_gh.return_value = _labels_payload("📦 dependencies")
        result = check_undeclared_labels(_declaring("🐛 bug"))
    assert result.passed is False
    assert "detaches" in result.message
    assert "search/issues" in result.message


def test_undeclared_labels_clean():
    """No finding when every label on the repository is declared."""
    with patch("repomatic.lint_repo.repo.gh_json") as mock_gh:
        mock_gh.return_value = _labels_payload("🐛 bug", "🤖 ci")
        declared = _declaring("🐛 bug", "🤖 ci", "❔ question")
        result = check_undeclared_labels(declared)
    assert result.passed is True
    # A declared label the repository lacks is the sync's job, not a finding.
    assert "❔ question" not in result.message


def test_undeclared_labels_skips_on_an_unreadable_api():
    """An unreachable API is indeterminate, never a red run."""
    with patch("repomatic.lint_repo.repo.gh_json", return_value=None):
        result = check_undeclared_labels(_declaring("🐛 bug"))
    assert result.passed is None
    assert "skipped" in result.message


def test_undeclared_labels_is_advisory():
    """The check never fails the command.

    A repository may legitimately carry a hand-made label, and deleting one is
    a judgement call. Both make a red run the wrong answer.
    """
    assert _roster_check("undeclared-labels").fatal is False


def test_declared_labels_are_read_only_when_a_check_asks():
    """Building a context fetches no label definition.

    A `labels.extra-files` URL is downloaded to know the declared set, which
    only the undeclared-labels check needs, and only with a repository.
    """
    config = Config(labels=LabelsConfig(extra_files=["https://example.com/a.toml"]))
    with patch("repomatic.github.labels.get_bytes") as fetch:
        ctx = LintContext.from_project(config)
        assert not _roster_check("undeclared-labels").applies(ctx)
    fetch.assert_not_called()


def test_declared_label_names_reads_the_bundled_profiles():
    """The default profile is always read, the awesome one only for a list."""
    config = Config()
    plain = declared_label_names(config, is_awesome=False)
    awesome = declared_label_names(config, is_awesome=True)
    assert "🐛 bug" in plain
    assert "🆕 new link" not in plain
    assert awesome > plain
    assert "🆕 new link" in awesome


def test_declared_labels_follow_the_repository_name():
    """An `awesome-*` repository reads the awesome profile too."""
    assert LintContext(repo_name="awesome-fruits").is_awesome
    assert not LintContext(repo_name="fruits").is_awesome


def test_declared_label_names_excludes_rename_sources():
    """A `rename-from` value is not a declaration.

    It names a label expected to stop existing, so counting it as declared
    would hide the orphan left behind when the rename does not fire. That is
    how `meta-package-manager` carried `📦 dependencies` and its 864 items
    unreported for six months.
    """
    names = _names_in_labelmaker_config(
        serialize_inline_labels([
            {
                "name": "🔗 dependencies",
                "color": "9daf0d",
                "rename-from": ["📦 dependencies"],
            }
        ])
    )
    assert names == {"🔗 dependencies"}


LEMON_CONFIG = {
    "profiles": {"default": {"labels": [{"name": "🍋 lemon", "color": "fff44f"}]}}
}
"""One label definition, serialized below into each format `labelmaker` reads."""

LEMON_YAML = yaml.safe_dump(LEMON_CONFIG, allow_unicode=True)


@pytest.mark.parametrize(
    ("filename", "text"),
    [
        pytest.param(
            "citrus.toml", serialize_inline_labels([{"name": "🍋 lemon"}]), id="toml"
        ),
        pytest.param("citrus.json", json.dumps(LEMON_CONFIG), id="json"),
        # Plain JSON is valid JSON5, the subset read without a JSON5 parser.
        pytest.param("citrus.json5", json.dumps(LEMON_CONFIG), id="json5"),
        pytest.param("citrus.yaml", LEMON_YAML, id="yaml"),
        pytest.param("citrus.yml", LEMON_YAML, id="yml"),
        # `labelmaker` matches the extension case-insensitively.
        pytest.param("CITRUS.YAML", LEMON_YAML, id="uppercase"),
    ],
)
def test_names_in_labelmaker_config_reads_every_labelmaker_format(filename, text):
    """Each format `labelmaker` reads declares its labels.

    `sync-labels` applies an extra label file in any of them, so a TOML-only
    reader would call every label of a JSON or YAML file an orphan.
    """
    assert _names_in_labelmaker_config(text, filename=filename) == {"🍋 lemon"}


@pytest.mark.parametrize(
    ("filename", "text"),
    [
        pytest.param("citrus.toml", "this is not = valid = toml", id="broken-toml"),
        # JSON5 beyond the JSON subset: a comment, unquoted keys, a trailing comma.
        pytest.param(
            "citrus.json5",
            '// orchard\n{profiles: {default: {labels: [{name: "🍋 lemon"},]}}}',
            id="json5-syntax",
        ),
        pytest.param("citrus.yaml", "- lemon\n- lime\n", id="not-a-mapping"),
        pytest.param("citrus.cfg", "[profiles]", id="unknown-extension"),
    ],
)
def test_names_in_labelmaker_config_rejects_an_unreadable_file(filename, text):
    """A definition this module cannot read raises instead of declaring nothing.

    An empty answer would call every label of that file an orphan.
    """
    with pytest.raises(LabelConfigError, match="citrus"):
        _names_in_labelmaker_config(text, filename=filename)


def test_declared_label_names_reads_a_yaml_extra_labels_file(tmp_path):
    """A committed `extra-labels/` file counts whatever its format."""
    extra_dir = tmp_path / "extra-labels"
    extra_dir.mkdir()
    (extra_dir / "citrus.yaml").write_text(LEMON_YAML, encoding="UTF-8")
    names = declared_label_names(Config(), is_awesome=False, labels_dir=tmp_path)
    assert "🍋 lemon" in names
    assert "🐛 bug" in names


REMOTE_LABELS_URL = "https://example.com/orchard/citrus.toml"
"""A `labels.extra-files` entry, served through a mocked `get_bytes`."""


def test_declared_label_names_fetches_remote_extra_files():
    """A `labels.extra-files` definition counts without a staged export.

    `lint-repo` stages nothing, so the declared set reads the file from its URL.
    Otherwise every label only that file declares would read as an orphan.
    """
    remote = serialize_inline_labels([{"name": "🍋 lemon", "color": "fff44f"}])
    config = Config(labels=LabelsConfig(extra_files=[REMOTE_LABELS_URL]))
    with patch(
        "repomatic.github.labels.get_bytes", return_value=remote.encode("UTF-8")
    ) as mock_fetch:
        names = declared_label_names(config, is_awesome=False)
    mock_fetch.assert_called_once_with(REMOTE_LABELS_URL)
    assert "🍋 lemon" in names
    assert "🐛 bug" in names


@pytest.mark.parametrize(
    ("url", "fetch", "error"),
    [
        pytest.param(
            REMOTE_LABELS_URL,
            {"side_effect": FetchError("HTTP Error 404: Not Found")},
            FetchError,
            id="unreachable",
        ),
        # A malformed URL fails before any request, and still as a `FetchError`.
        pytest.param(
            "orchard/citrus.toml",
            {"wraps": get_bytes},
            FetchError,
            id="malformed",
        ),
        # JSON5 syntax, which `labelmaker` reads and this module has no parser for.
        pytest.param(
            "https://example.com/orchard/citrus.json5",
            {"return_value": b'{profiles: {default: {labels: [{name: "lime"},]}}}'},
            LabelConfigError,
            id="unreadable",
        ),
    ],
)
def test_undeclared_labels_skips_on_an_unusable_extra_file(url, fetch, error):
    """A definition that cannot be fetched or read leaves the check skipped.

    A short declared set would call every label of that file an orphan. The set
    resolves to unknown instead, and the check reports skipped without reading
    the repository's labels.
    """
    config = Config(labels=LabelsConfig(extra_files=[url]))
    with patch("repomatic.github.labels.get_bytes", **fetch):
        with pytest.raises(error, match="citrus"):
            declared_label_names(config, is_awesome=False)
        assert _resolve_declared_labels(config, is_awesome=False) is None

        check = _roster_check("undeclared-labels")
        ctx = LintContext(repo="owner/repo", config=config)
        assert check.applies(ctx)
        with patch("repomatic.lint_repo.repo.gh_json") as mock_gh:
            (result,) = check.results(ctx)
    mock_gh.assert_not_called()
    assert result.passed is None
    assert "skipped" in result.message
