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

"""Build and manage the setup guide issue.

Backs the `setup-guide` command: composes the repository-settings checks from
{mod}`repomatic.lint_repo` and the PAT permission probes from
{mod}`repomatic.github.token` with the `setup-guide-*` templates into a single
issue body, then drives the issue lifecycle. Each setup step renders as a
collapsible section whose open/closed state and emoji reflect the check
outcome, and the issue closes only once every verifiable step passes.
"""

from __future__ import annotations

import logging
from pathlib import Path

from click_extra import TableFormat, render_table

from .github import token
from .github.gh import run_gh_command
from .github.issue import manage_issue_lifecycle
from .github.pr_body import render_template
from .lint_repo import (
    check_branch_ruleset_on_default,
    check_fork_pr_approval_policy,
    check_immutable_releases,
    check_pages_deployment_source,
    check_pypi_trusted_publisher,
    check_sha_pinning_required,
)
from .metadata import Metadata
from .pypi import (
    PYPI_TRUSTED_PUBLISHER_WORKFLOW,
    pypi_trusted_publisher_settings_url,
)

TYPE_CHECKING = False
if TYPE_CHECKING:
    from .config import Config


def _wrap_setup_step(title: str, content: str, *, passed: bool | None) -> str:
    """Wrap a setup step in a collapsible `<details>` block with status emoji.

    Incomplete steps (`passed=False`) render as open sections with a
    warning emoji. Completed steps (`passed=True`) render collapsed
    with a checkmark. Indeterminate steps (`passed=None`) render
    collapsed with an info emoji when the check could not run.

    :param title: Step heading shown in the `<summary>` line.
    :param content: Markdown body of the step.
    :param passed: Whether the step is verified complete. `None` means the
        check could not run, like insufficient token permissions.
    :return: HTML `<details>` block string.
    """
    if passed is None:
        emoji = "ℹ️"
        open_attr = ""
    elif passed:
        emoji = "✅"
        open_attr = ""
    else:
        emoji = "❌"
        open_attr = " open"
    return (
        f"<details{open_attr}>\n"
        f"<summary>{emoji} <strong>{title}</strong></summary>\n\n"
        f"{content}\n\n"
        f"</details>"
    )


def manage_setup_guide(
    config: Config,
    *,
    has_pat: bool,
    has_notifications_pat: bool,
    has_virustotal_key: bool,
    repo: str | None,
) -> None:
    """Render the setup guide issue body and drive the issue lifecycle.

    Runs the per-step checks (PAT permissions, branch ruleset, immutable
    releases, fork PR approval, PyPI Trusted Publisher, Pages source), renders
    each as a collapsible section, and opens, updates, or closes the setup
    issue accordingly. The issue closes only when all verifiable steps pass.

    :param config: The resolved `[tool.repomatic]` configuration.
    :param has_pat: Whether `REPOMATIC_PAT` is configured.
    :param has_notifications_pat: Whether `REPOMATIC_NOTIFICATIONS_PAT` is
        configured.
    :param has_virustotal_key: Whether `VIRUSTOTAL_API_KEY` is configured.
    :param repo: Repository in `owner/repo` format; permission and settings
        checks are skipped when `None`.
    """
    # Resolve repo identity for template variables.
    md = Metadata()
    repo_name = md.repo_name
    repo_owner = md.repo_owner
    repo_slug = md.repo_slug
    repo_url = md.repo_url

    # --- Per-step checks ---

    # Token + permissions check.
    missing_permissions_section = ""
    has_permission_failures = False
    dependabot_ok = False
    if has_pat and repo:
        pat_results = token.check_all_pat_permissions(repo)
        failures = pat_results.failed()
        if failures:
            has_permission_failures = True
            table = render_table(
                [[message] for _field_name, message in failures],
                headers=["Permission issue"],
                table_format=TableFormat.GITHUB,
            )
            missing_permissions_section = (
                "> [!WARNING]\n"
                "> Your `REPOMATIC_PAT` secret is configured but missing"
                " some permissions.\n"
                "> Update the token using the pre-filled link below.\n\n"
                f"{table}\n"
            )
        # Vulnerability alerts are confirmed enabled when the Dependabot
        # alerts permission check passes (HTTP 200 from the alerts API).
        dependabot_ok = pat_results.vulnerability_alerts[0]

    token_ok = has_pat and not has_permission_failures

    # Branch ruleset check. An unreadable rulesets API answers `None`, which
    # this guide treats as incomplete: the step is the only place a maintainer
    # is told to protect the branch, so an indeterminate probe must keep
    # prompting rather than quietly pass.
    branch_ok: bool | None = False
    if has_pat and repo:
        branch_ok = check_branch_ruleset_on_default(repo).passed

    # Immutable releases check.
    has_changelog = Path(config.changelog_location).exists()
    immutable_ok: bool | None = False
    if has_pat and repo and has_changelog:
        immutable_ok = check_immutable_releases(repo).passed

    # Fork PR approval policy check.
    fork_pr_ok: bool | None = False
    if has_pat and repo:
        fork_pr_ok = check_fork_pr_approval_policy(repo).passed

    # SHA pinning required policy check.
    sha_pinning_ok: bool | None = False
    if has_pat and repo:
        sha_pinning_ok = check_sha_pinning_required(repo).passed

    # PyPI Trusted Publisher check (only for projects that publish to PyPI).
    # The probe does not need a PAT: it hits the public PyPI integrity API.
    # Gated on `is_python_package` rather than on `package_name` being set: a uv
    # virtual project declares `[project] name` purely to carry dependencies, so
    # the name alone says nothing about whether anything is ever published. It is
    # the same predicate `RepoScope.PACKAGE_ONLY` resolves against, which is what
    # already keeps `release.yaml` out of such a repository. Asking those projects
    # to register a publisher points them at a PyPI name they do not own, for a
    # workflow file they do not have.
    pypi_package_name = md.package_name if md.is_python_package else None
    pypi_publisher_ok: bool | None = None
    if repo and pypi_package_name:
        pypi_publisher_ok = check_pypi_trusted_publisher(repo, pypi_package_name).passed

    # Pages deployment source check (Sphinx projects only).
    pages_ok: bool | None = None
    if md.is_sphinx and repo:
        pages_ok = check_pages_deployment_source(repo).passed

    # --- Render each step as a collapsible section ---

    step_token = _wrap_setup_step(
        "Create and configure the token",
        render_template(
            "setup-guide-token",
            repo_url=repo_url,
            repo_name=repo_name,
            repo_owner=repo_owner,
            repo_slug=repo_slug,
        ),
        passed=token_ok,
    )

    step_dependabot = _wrap_setup_step(
        "Configure Dependabot settings",
        render_template(
            "setup-guide-dependabot",
            repo_url=repo_url,
            repo_slug=repo_slug,
        ),
        passed=dependabot_ok,
    )

    immutable_releases_step = ""
    if has_changelog:
        immutable_releases_step = _wrap_setup_step(
            "Enable immutable releases",
            render_template("immutable-releases", repo_url=repo_url),
            passed=immutable_ok,
        )

    step_branch_ruleset = _wrap_setup_step(
        "Protect the main branch",
        render_template("setup-guide-branch-ruleset", repo_url=repo_url),
        passed=branch_ok or False,
    )

    # Both settings below are read through Administration-scoped endpoints, so
    # a PAT issued without that permission answers 403 and the check lands on
    # `None`. Say so in the step instead of dropping it: the reader is looking
    # at a setting they were told to configure, and the difference between
    # "verified" and "nobody could look" belongs on screen. Dropping it also
    # hid the token gap itself, since the missing permission had no other
    # symptom.
    cannot_verify = (
        "\n\n> [!NOTE]\n"
        "> This setting could not be verified: `REPOMATIC_PAT` is missing the"
        " **Administration: Read-only** permission. Update the token with the"
        " pre-filled link in the first step. The setting may well be correct"
        " already, but nothing here can confirm it."
    )

    step_fork_pr_approval = _wrap_setup_step(
        "Require approval for fork PR workflows",
        render_template(
            "setup-guide-fork-pr-approval",
            repo_url=repo_url,
            repo_slug=repo_slug,
        )
        + (cannot_verify if fork_pr_ok is None else ""),
        passed=fork_pr_ok,
    )

    step_sha_pinning_required = _wrap_setup_step(
        "Require SHA pinning for GitHub Actions",
        render_template(
            "setup-guide-sha-pinning-required",
            repo_url=repo_url,
            repo_slug=repo_slug,
        )
        + (cannot_verify if sha_pinning_ok is None else ""),
        passed=sha_pinning_ok,
    )

    # PyPI Trusted Publisher step: only relevant for projects that publish to
    # PyPI. Treat indeterminate (None: never released, or pre-OIDC release with
    # no provenance) as incomplete so the step keeps prompting until a
    # successful OIDC-attested upload is observed.
    step_pypi_trusted_publisher = ""
    if pypi_package_name:
        step_pypi_trusted_publisher = _wrap_setup_step(
            "Register the PyPI Trusted Publisher entry",
            render_template(
                "setup-guide-pypi-trusted-publisher",
                package_name=pypi_package_name,
                repo_owner=repo_owner,
                repo_name=repo_name,
                workflow_filename=PYPI_TRUSTED_PUBLISHER_WORKFLOW,
                settings_url=pypi_trusted_publisher_settings_url(
                    pypi_package_name,
                    owner=repo_owner,
                    repository=repo_name,
                    workflow_filename=PYPI_TRUSTED_PUBLISHER_WORKFLOW,
                ),
            ),
            passed=pypi_publisher_ok or False,
        )

    # Pages deployment source step: only relevant for Sphinx projects.
    # Treat "not configured" (None) as incomplete so the step renders open.
    step_pages_source = ""
    if md.is_sphinx:
        step_pages_source = _wrap_setup_step(
            "Set GitHub Pages deployment source to GitHub Actions",
            render_template(
                "setup-guide-pages-source",
                repo_url=repo_url,
                repo_slug=repo_slug,
            ),
            passed=pages_ok or False,
        )

    # VirusTotal step: only relevant when Nuitka binary compilation is active.
    nuitka_active = config.nuitka_enabled and bool(md.script_entries)
    step_virustotal = ""
    if nuitka_active:
        step_virustotal = _wrap_setup_step(
            "Configure VirusTotal scanning (optional)",
            render_template(
                "setup-guide-virustotal",
                repo_url=repo_url,
                repo_slug=repo_slug,
            ),
            passed=has_virustotal_key,
        )

    # Notifications PAT step: only relevant when the unsubscribe workflow is
    # opted in via notification.unsubscribe. The workflow skips silently
    # without the secret, so the guide is the only onboarding surface.
    step_notifications_pat = ""
    if config.notification_unsubscribe:
        step_notifications_pat = _wrap_setup_step(
            "Create and configure the notifications token",
            render_template(
                "setup-guide-notifications-pat",
                repo_url=repo_url,
                repo_slug=repo_slug,
            ),
            passed=has_notifications_pat,
        )

    step_verify = _wrap_setup_step(
        "Verify the setup",
        render_template(
            "setup-guide-verify",
            repo_url=repo_url,
            repo_slug=repo_slug,
        ),
        passed=False,
    )

    # Detect if the repository owner is an organization.
    org_tip = ""
    if repo_owner:
        try:
            owner_type = run_gh_command(
                ["api", f"users/{repo_owner}", "--jq", ".type"],
            ).strip()
            if owner_type == "Organization":
                org_tip = (
                    "> 💡 **For organizations**: Consider using a"
                    " [machine user account](https://docs.github.com/en/"
                    "get-started/learning-about-github/types-of-github-accounts"
                    "#personal-accounts) or a dedicated service account to own"
                    " the PAT, rather than tying it to an individual's account."
                )
        except RuntimeError:
            logging.debug(f"Failed to detect owner type for {repo_owner!r}.")

    # --- Assemble issue body ---

    setup_body = render_template(
        "setup-guide",
        missing_permissions_section=missing_permissions_section,
        step_token=step_token,
        step_dependabot=step_dependabot,
        immutable_releases_step=immutable_releases_step,
        step_branch_ruleset=step_branch_ruleset,
        step_fork_pr_approval=step_fork_pr_approval,
        step_sha_pinning_required=step_sha_pinning_required,
        step_pypi_trusted_publisher=step_pypi_trusted_publisher,
        step_pages_source=step_pages_source,
        step_virustotal=step_virustotal,
        step_notifications_pat=step_notifications_pat,
        step_verify=step_verify,
        org_tip=org_tip,
        repo_url=repo_url,
    )
    # Close issue only when all verifiable steps pass.
    # Immutable releases and verify are excluded (no API to check).
    # Fork PR approval counts only when determinate: an unreadable probe must
    # not wedge the issue open, since the reader has no way to satisfy a check
    # that never runs. Its step still renders, carrying the reason it could not
    # be checked, so the gap stays visible without being a blocker.
    # Pages source: when is_sphinx, treat "not configured" (None) as a
    # failure so the setup guide reopens with the Pages step.
    vt_ok = not nuitka_active or has_virustotal_key
    notifications_ok = not config.notification_unsubscribe or has_notifications_pat
    # Branch ruleset: an indeterminate probe (`None`) keeps the issue open, the
    # same verdict an outright failure gets. Spelled out rather than left to
    # `None` being falsy, so the intent survives the next edit.
    branch_gate = bool(branch_ok)
    fork_pr_gate = fork_pr_ok is not False
    pages_gate = bool(pages_ok) if md.is_sphinx else pages_ok is not False
    # Trusted Publisher: when the project publishes to PyPI, only close once
    # provenance confirms the entry is wired. None (no published release yet,
    # or pre-OIDC release) keeps the step open. When the project does not
    # publish to PyPI, the gate is a no-op. Reading `package_name` here instead
    # would wedge the issue permanently open on every uv virtual project: the
    # name is set, so the gate demands provenance for an upload that never
    # happens, and no amount of completing the other steps closes the issue.
    pypi_publisher_gate = bool(pypi_publisher_ok) if pypi_package_name else True
    needs_issue = not (
        token_ok
        and dependabot_ok
        and branch_gate
        and vt_ok
        and notifications_ok
        and fork_pr_gate
        and pages_gate
        and pypi_publisher_gate
    )

    manage_issue_lifecycle(
        has_issues=needs_issue,
        body=setup_body,
        labels=["🤖 ci"],
        title="Repomatic setup guide",
        no_issues_comment=(
            "PAT configured, all permissions verified, repository settings complete."
        ),
    )
