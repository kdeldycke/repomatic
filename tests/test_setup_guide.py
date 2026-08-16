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

import re
from contextlib import ExitStack, contextmanager
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from repomatic.cli import repomatic as repomatic_cli
from repomatic.github.pr_body import load_template
from repomatic.github.token import PatPermissionResults
from repomatic.lint_repo import CheckResult
from repomatic.setup_guide import SETUP_STEPS
from tests.conftest import pat_results

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Iterator
    from unittest.mock import MagicMock

# A neutral slug fed through GITHUB_REPOSITORY so Metadata resolves the repo
# from the environment (no `gh repo view` call) while the body still renders
# real repository links.
REPO_SLUG = "orchard/papaya"

MISSING_ALERTS_PAT = pat_results(
    vulnerability_alerts=(
        False,
        (
            "Token lacks 'Dependabot alerts: Read-only' permission."
            " Update the PAT to include this permission."
        ),
    )
)
"""A PAT passing every probe but the Dependabot-alerts one."""


@contextmanager
def _offline_setup_guide(
    *,
    pat: PatPermissionResults | None = None,
    branch_ok: CheckResult | None = None,
    immutable_ok: CheckResult | None = None,
    fork_pr_ok: CheckResult | None = None,
    sha_pinning_ok: CheckResult | None = None,
    pypi_ok: CheckResult | None = None,
    pages_ok: CheckResult | None = None,
    owner_type: str = "User",
) -> Iterator[tuple[MagicMock, list[str]]]:
    """Patch every network seam `manage_setup_guide` reaches.

    Each check passes by default, so the issue closes unless an argument
    overrides one seam to exercise its failure branch. Yields the captured
    `manage_issue_lifecycle` mock plus the list of issue bodies it received,
    so callers can assert on both the lifecycle decision and the rendered
    markdown.

    :param pat: Result of the PAT permission probe (defaults to all checks
        passing).
    :param branch_ok: Return of the branch-ruleset check.
    :param immutable_ok: Return of the immutable-releases check.
    :param fork_pr_ok: Return of the fork-PR approval-policy check.
    :param sha_pinning_ok: Return of the SHA-pinning-required check.
    :param pypi_ok: Return of the PyPI Trusted Publisher check.
    :param pages_ok: Return of the Pages deployment-source check.
    :param owner_type: `.type` the org-detection `gh api users/…` call returns.
    """
    if pat is None:
        pat = pat_results()
    if branch_ok is None:
        branch_ok = CheckResult(True, "Active branch rulesets found: main.")
    if immutable_ok is None:
        immutable_ok = CheckResult(True, "Immutable releases enabled.")
    if fork_pr_ok is None:
        fork_pr_ok = CheckResult(True, "Fork PR approval is required.")
    if sha_pinning_ok is None:
        sha_pinning_ok = CheckResult(True, "SHA pinning required: enabled.")
    if pypi_ok is None:
        pypi_ok = CheckResult(True, "Trusted publisher is configured.")
    if pages_ok is None:
        pages_ok = CheckResult(True, "Pages deployment source is GitHub Actions.")
    bodies: list[str] = []
    with ExitStack() as stack:
        enter = stack.enter_context
        enter(patch("repomatic.github.token.validate_gh_token_env"))
        enter(
            patch(
                "repomatic.github.token.check_all_pat_permissions",
                return_value=pat,
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
                "repomatic.setup_guide.check_sha_pinning_required",
                return_value=sha_pinning_ok,
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
        lifecycle.side_effect = lambda **kw: bodies.append(kw["body"])
        yield lifecycle, bodies


FULLY_CONFIGURED = {
    "HAS_CLOUDFLARE_ACCOUNT_ID": "true",
    "HAS_CLOUDFLARE_API_TOKEN": "true",
}
"""Secrets a fully-configured repository holds, beyond what `_invoke` sets.

These tests run against this repository's own `pyproject.toml`, which declares
`site.deploy = "cloudflare-pages"` while it dogfoods that lane, so the step
asking for the Cloudflare credentials applies to them. A test asserting that
everything passes has to supply them, or it asserts the state of this
repository's secrets rather than the behaviour it means to cover.
"""


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
    pyproject.write_text("[tool.repomatic]\nsetup-guide = false\n", encoding="UTF-8")
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(repomatic_cli, ["setup-guide"])
    assert result.exit_code == 0
    mock_lifecycle.assert_not_called()


@pytest.mark.parametrize(
    ("branch_ok", "has_vt_key", "expected_has_issues"),
    (
        pytest.param(
            CheckResult(True, "Active branch rulesets found: main."),
            True,
            False,
            id="all-pass-closes",
        ),
        pytest.param(
            CheckResult(False, "No active branch rulesets found."),
            True,
            True,
            id="branch-missing-opens",
        ),
        pytest.param(
            CheckResult(True, "Active branch rulesets found: main."),
            False,
            True,
            id="vt-key-missing-opens",
        ),
        pytest.param(
            CheckResult(None, "Branch ruleset check: skipped (could not query)."),
            True,
            True,
            id="branch-indeterminate-opens",
        ),
    ),
)
def test_setup_guide_lifecycle_reflects_check_outcomes(
    branch_ok, has_vt_key, expected_has_issues
):
    """The issue closes only when every verifiable step passes.

    All checks pass by default, so the issue closes; a failing branch ruleset
    or a missing VirusTotal key (with Nuitka active in this repo) keeps it
    open. An indeterminate ruleset probe (an unreadable API) counts as
    incomplete too, so the step keeps prompting rather than quietly passing.
    """
    args = ["setup-guide", "--has-pat", "--repo", REPO_SLUG]
    if has_vt_key:
        args.append("--has-virustotal-key")
    with _offline_setup_guide(branch_ok=branch_ok) as (lifecycle, _bodies):
        result = _invoke(args, env=FULLY_CONFIGURED)
    assert result.exit_code == 0
    assert lifecycle.call_args_list[0][1]["has_issues"] is expected_has_issues


def test_setup_guide_pat_missing_permission_keeps_issue_open():
    """When PAT is configured but a permission is missing, the issue stays open."""
    with _offline_setup_guide(pat=MISSING_ALERTS_PAT) as (
        lifecycle,
        _bodies,
    ):
        result = _invoke(["setup-guide", "--has-pat", "--repo", REPO_SLUG])
    assert result.exit_code == 0
    assert lifecycle.call_count == 1
    assert lifecycle.call_args_list[0][1]["has_issues"] is True


def test_setup_guide_pat_missing_permission_body_contains_warning():
    """When PAT has missing permissions, the issue body contains a warning section."""
    with _offline_setup_guide(pat=MISSING_ALERTS_PAT) as (
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


MISSING_ADMIN_PAT = pat_results(
    administration=(
        False,
        (
            "Token lacks 'Administration: Read-only' permission."
            " Update the PAT to include this permission."
        ),
    )
)
"""A PAT passing every probe but the Administration one."""

UNREADABLE = CheckResult(None, "Check skipped (could not query).")
"""What an Administration-scoped probe answers to a token without it."""


@pytest.mark.parametrize(
    ("title", "fork_pr_ok", "sha_pinning_ok"),
    (
        pytest.param(
            "Require approval for fork PR workflows", UNREADABLE, None, id="fork-pr"
        ),
        pytest.param(
            "Require SHA pinning for GitHub Actions", None, UNREADABLE, id="sha-pinning"
        ),
    ),
)
def test_setup_guide_unverifiable_step_renders_with_reason(
    title, fork_pr_ok, sha_pinning_ok
):
    """A check that could not run keeps its step, and says why.

    Both settings are read through Administration-scoped endpoints. Deleting
    the step on a 403 removed the only notice that the setting is unverified,
    and hid the token gap itself: a missing Administration permission has no
    other symptom. The step now renders indeterminate, naming what to fix.

    The seam left at `None` keeps its passing default, so each case exercises
    one unreadable probe beside one that resolved.
    """
    with _offline_setup_guide(fork_pr_ok=fork_pr_ok, sha_pinning_ok=sha_pinning_ok) as (
        _lifecycle,
        bodies,
    ):
        _invoke(["setup-guide", "--has-pat", "--repo", REPO_SLUG])
    content = bodies[0]
    assert f"<summary>ℹ️ <strong>{title}</strong></summary>" in content
    assert "could not be verified" in content
    assert "**Administration: Read-only**" in content


def test_setup_guide_unverifiable_step_does_not_block_closing():
    """An unreadable probe leaves the issue closable.

    The reader cannot satisfy a check that never runs, so holding the issue
    open on it would be the same trap the PyPI publisher gate used to set.
    """
    with _offline_setup_guide(fork_pr_ok=UNREADABLE, sha_pinning_ok=UNREADABLE) as (
        lifecycle,
        _bodies,
    ):
        _invoke(
            [
                "setup-guide",
                "--has-pat",
                "--repo",
                REPO_SLUG,
                "--has-virustotal-key",
            ],
            env=FULLY_CONFIGURED,
        )
    assert lifecycle.call_args_list[0][1]["has_issues"] is False


@pytest.mark.parametrize(
    ("fork_pr_ok", "sha_pinning_ok"),
    (
        pytest.param(
            CheckResult(False, "Fork PR approval is not required."), None, id="fork-pr"
        ),
        pytest.param(
            None,
            CheckResult(False, "SHA pinning is not required."),
            id="sha-pinning",
        ),
    ),
)
def test_setup_guide_disabled_setting_keeps_issue_open(fork_pr_ok, sha_pinning_ok):
    """A readable probe reporting the setting off holds the issue open.

    The counterpart to the unreadable case above: here the reader *can* act, so
    the guide has to keep asking. The seam left at `None` keeps its passing
    default, so each case turns exactly one setting off.
    """
    with _offline_setup_guide(fork_pr_ok=fork_pr_ok, sha_pinning_ok=sha_pinning_ok) as (
        lifecycle,
        _bodies,
    ):
        _invoke([
            "setup-guide",
            "--has-pat",
            "--repo",
            REPO_SLUG,
            "--has-virustotal-key",
        ])
    assert lifecycle.call_args_list[0][1]["has_issues"] is True


GATE_EXEMPT_STEPS = {
    # A walkthrough with no settings API behind it: gating on it would wedge
    # the issue open forever, which is why it renders ❌ on every run.
    "Verify the setup",
    # Informational, per the gate comment in `manage_setup_guide`.
    "Enable immutable releases",
}
"""Setup steps deliberately left out of the issue's close gate."""


def test_setup_guide_requests_administration_permission():
    """The token step asks for Administration, in the form and in the table."""
    with _offline_setup_guide() as (_lifecycle, bodies):
        _invoke(["setup-guide"])
    content = bodies[0]
    assert "administration=read" in content
    assert "**Administration**" in content


def test_setup_guide_missing_administration_is_reported():
    """A PAT without Administration is named in the permission warning table."""
    with _offline_setup_guide(pat=MISSING_ADMIN_PAT) as (_lifecycle, bodies):
        _invoke(["setup-guide", "--has-pat", "--repo", REPO_SLUG])
    content = bodies[0]
    assert "missing some permissions" in content
    assert "Administration: Read-only" in content


PACKAGE_PYPROJECT = """\
[project]
name = "papaya"
version = "1.0.0"
"""
"""A plain PEP 621 project, which uv builds and publishes."""

VIRTUAL_PYPROJECT = """\
[project]
name = "papaya"
version = "1.0.0"

[tool.uv]
package = false
"""
"""A uv virtual project: same `[project] name`, nothing ever published."""


@pytest.mark.parametrize(
    ("pyproject", "step_rendered", "expected_has_issues"),
    (
        pytest.param(PACKAGE_PYPROJECT, True, True, id="package-renders-step"),
        pytest.param(VIRTUAL_PYPROJECT, False, False, id="virtual-project-skips-step"),
    ),
)
def test_setup_guide_pypi_step_gated_on_package_build(
    tmp_path, monkeypatch, pyproject, step_rendered, expected_has_issues
):
    """The Trusted Publisher step follows `is_python_package`, not `package_name`.

    Both projects below declare the same `[project] name`, so a gate reading
    that name alone cannot tell them apart. Only the first one is built and
    published, and only it should be asked to register a publisher.

    The lifecycle assertion is the point of the test. With the publisher check
    failing and every other check passing, the virtual project still closes its
    issue, where a `package_name` gate would hold it open forever: the name is
    set, so the gate keeps demanding provenance for an upload that no workflow
    in the repository can ever perform.
    """
    (tmp_path / "pyproject.toml").write_text(pyproject, encoding="UTF-8")
    monkeypatch.chdir(tmp_path)
    unregistered = CheckResult(False, "PyPI Trusted Publisher mismatch for 'papaya'.")
    with _offline_setup_guide(pypi_ok=unregistered) as (lifecycle, bodies):
        result = _invoke(["setup-guide", "--has-pat", "--repo", REPO_SLUG])
    assert result.exit_code == 0
    assert ("Trusted Publisher" in bodies[0]) is step_rendered
    assert lifecycle.call_args_list[0][1]["has_issues"] is expected_has_issues


def test_setup_guide_virtual_project_skips_pypi_probe(tmp_path, monkeypatch):
    """A virtual project never reaches the PyPI API.

    The probe is skipped rather than run-and-ignored, so a repository that
    happens to share its `[project] name` with an unrelated PyPI project is
    never measured against that stranger's release.
    """
    (tmp_path / "pyproject.toml").write_text(VIRTUAL_PYPROJECT, encoding="UTF-8")
    monkeypatch.chdir(tmp_path)
    with (
        _offline_setup_guide() as (_lifecycle, _bodies),
        patch("repomatic.setup_guide.check_pypi_trusted_publisher") as probe,
    ):
        result = _invoke(["setup-guide", "--has-pat", "--repo", REPO_SLUG])
    assert result.exit_code == 0
    probe.assert_not_called()


def test_setup_guide_nuitka_disabled_hides_vt_step(tmp_path, monkeypatch):
    """When Nuitka is disabled, the VT step is omitted from the setup guide."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        "[project]\nname = 'papaya'\nversion = '1.0'\n\n"
        "[tool.repomatic.nuitka]\nenabled = false\n",
        encoding="UTF-8",
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


PAGES_STEP = "Set GitHub Pages deployment source to GitHub Actions"
"""Title of the step only a GitHub Pages project is asked to perform."""

CLOUDFLARE_STEP = "Configure the Cloudflare Pages credentials"
"""Title of the step only a Cloudflare Pages project is asked to perform."""


def _site_project(tmp_path, monkeypatch, target: str, *, sphinx: bool = True) -> None:
    """Materialize a project deploying its site to *target*, and enter it.

    The `docs/conf.py` is what `Metadata.is_sphinx` looks for. The GitHub
    Pages step is gated on it; the Cloudflare step follows the declared
    target alone, so *sphinx* can be turned off to model a repository whose
    site is built by its own workflow.
    """
    if sphinx:
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "conf.py").write_text(
            "project = 'papaya'\n", encoding="UTF-8"
        )
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 'papaya'\nversion = '1.0'\n\n"
        f"[tool.repomatic]\nsite.deploy = '{target}'\n",
        encoding="UTF-8",
    )
    monkeypatch.chdir(tmp_path)


@pytest.mark.parametrize(
    ("target", "rendered", "omitted"),
    (
        pytest.param("github-pages", PAGES_STEP, CLOUDFLARE_STEP, id="github-pages"),
        pytest.param(
            "cloudflare-pages", CLOUDFLARE_STEP, PAGES_STEP, id="cloudflare-pages"
        ),
    ),
)
def test_setup_guide_asks_about_the_configured_host_only(
    tmp_path, monkeypatch, target, rendered, omitted
):
    """Exactly one deploy step renders, the one `site.deploy` names.

    The two hosts want different things set up and only one of them ever runs,
    so a guide showing both sends the maintainer to configure a deployment
    surface nothing publishes to.
    """
    _site_project(tmp_path, monkeypatch, target)
    with _offline_setup_guide() as (_lifecycle, bodies):
        result = _invoke(["setup-guide", "--has-pat", "--repo", REPO_SLUG])
    assert result.exit_code == 0
    assert rendered in bodies[0]
    assert omitted not in bodies[0]


@pytest.mark.parametrize(
    ("env", "expected_has_issues"),
    (
        pytest.param({}, True, id="neither-secret"),
        pytest.param({"HAS_CLOUDFLARE_API_TOKEN": "true"}, True, id="token-only"),
        pytest.param({"HAS_CLOUDFLARE_ACCOUNT_ID": "true"}, True, id="account-only"),
        pytest.param(
            {
                "HAS_CLOUDFLARE_ACCOUNT_ID": "true",
                "HAS_CLOUDFLARE_API_TOKEN": "true",
            },
            False,
            id="both-secrets",
        ),
    ),
)
def test_setup_guide_holds_open_until_both_cloudflare_secrets_land(
    tmp_path, monkeypatch, env, expected_has_issues
):
    """Half the credentials is not enough to close the issue.

    Unlike the VirusTotal key, a missing Cloudflare value fails the deploy job
    rather than skipping it, so the step gates closure. `wrangler` needs both,
    and a run configured with only one is as broken as one configured with
    neither.
    """
    _site_project(tmp_path, monkeypatch, "cloudflare-pages")
    with _offline_setup_guide() as (lifecycle, _bodies):
        result = _invoke(["setup-guide", "--has-pat", "--repo", REPO_SLUG], env=env)
    assert result.exit_code == 0
    assert lifecycle.call_args_list[0][1]["has_issues"] is expected_has_issues


def test_setup_guide_asks_a_non_sphinx_site_for_its_cloudflare_credentials(
    tmp_path, monkeypatch
):
    """Declaring the Cloudflare target is the opt-in, Sphinx or not.

    A repository whose site is built by its own workflow (a Pelican blog, a
    hand-rolled static tree) deploys with the same project and the same two
    secrets, so the guide walks it through them. The GitHub Pages step stays
    away: repomatic runs no publisher for that combination.
    """
    _site_project(tmp_path, monkeypatch, "cloudflare-pages", sphinx=False)
    with _offline_setup_guide() as (lifecycle, bodies):
        result = _invoke(["setup-guide", "--has-pat", "--repo", REPO_SLUG])
    assert result.exit_code == 0
    assert CLOUDFLARE_STEP in bodies[0]
    assert PAGES_STEP not in bodies[0]
    # And the missing credentials hold the issue open, same as for Sphinx.
    assert lifecycle.call_args_list[0][1]["has_issues"] is True


def test_setup_guide_closes_with_github_pages_never_configured(tmp_path, monkeypatch):
    """A Cloudflare project closes its issue with no GitHub Pages source set.

    `check_pages_deployment_source` answers `404` for a repository that never
    enabled Pages, which the step reads as `None` and collapses to incomplete
    because it tolerates no unknown. Gated on `is_sphinx` alone, that wedged
    the guide open forever on a Cloudflare-hosted project, demanding a
    deployment source for a host nothing publishes to.
    """
    _site_project(tmp_path, monkeypatch, "cloudflare-pages")
    unconfigured = CheckResult(None, "Pages deployment source check: skipped.")
    with _offline_setup_guide(pages_ok=unconfigured) as (lifecycle, _bodies):
        result = _invoke(
            ["setup-guide", "--has-pat", "--repo", REPO_SLUG],
            env={
                "HAS_CLOUDFLARE_ACCOUNT_ID": "true",
                "HAS_CLOUDFLARE_API_TOKEN": "true",
            },
        )
    assert result.exit_code == 0
    assert lifecycle.call_args_list[0][1]["has_issues"] is False


def test_every_setup_step_feeds_the_close_gate():
    """Every rendered step decides the issue's fate, or is exempt on purpose.

    A step whose outcome no gate reads renders its ❌ into a body nobody
    reopens. `sha_pinning_ok` shipped that way: the guide closed itself with
    "repository settings complete" while the setting was off, and the only
    remaining symptom was a `lint-repo` warning nobody reads twice. Asserting
    over the table means the next step has to be gated or exempted
    deliberately.
    """
    ungated = {
        step.title
        for step in SETUP_STEPS
        if not step.gates_closure and step.title not in GATE_EXEMPT_STEPS
    }
    assert not ungated, f"setup steps rendered but never gated: {sorted(ungated)}"
    # Keep the exemptions honest: a title that no longer renders must not
    # linger here, silently excusing a future step that reuses the name.
    assert GATE_EXEMPT_STEPS <= {step.title for step in SETUP_STEPS}


def test_setup_step_placeholders_match_the_template():
    """Every step fills a `$placeholder` the guide template actually declares.

    A step whose placeholder the template never names renders into nothing,
    and `string.Template.substitute` raises on a template placeholder no step
    fills, so the two rosters have to agree exactly.
    """
    _meta, body = load_template("setup-guide")
    declared = set(re.findall(r"\$([a-z_]+)", body))
    filled = {step.placeholder for step in SETUP_STEPS}
    assert filled <= declared, f"steps fill unknown placeholders: {filled - declared}"
    # The template's remaining placeholders are the ones the driver fills.
    assert declared - filled == {
        "missing_permissions_section",
        "org_tip",
        "repo_url",
    }
