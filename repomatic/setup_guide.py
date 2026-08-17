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
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import cached_property
from pathlib import Path

from click_extra import TableFormat, render_table

from .github import token
from .github.gh import run_gh_command
from .github.issue import BOT_ISSUE_LABEL, manage_issue_lifecycle
from .github.pr_body import render_template
from .lint_repo import (
    CheckResult,
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
    from collections.abc import Callable

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


CANNOT_VERIFY = (
    "\n\n> [!NOTE]\n"
    "> This setting could not be verified: `REPOMATIC_PAT` is missing the"
    " **Administration: Read-only** permission. Update the token with the"
    " pre-filled link in the first step. The setting may well be correct"
    " already, but nothing here can confirm it."
)
"""Note appended to a step whose probe could not run.

Both settings it covers are read through Administration-scoped endpoints, so a
PAT issued without that permission answers `403` and the check lands on `None`.
Saying so in the step beats dropping it: dropping also hid the token gap
itself, since the missing permission had no other symptom.
"""


@dataclass
class GuideContext:
    """Everything the steps read, resolved once per run.

    The expensive lookups (PAT permission probes, `pyproject.toml`) are cached
    properties, so a step that never asks never pays and two steps asking the
    same question share one answer.
    """

    config: Config
    """The resolved `[tool.repomatic]` configuration."""

    repo: str | None
    """Repository in `owner/repo` form, or `None` when undetectable."""

    has_pat: bool
    """Whether `REPOMATIC_PAT` is configured."""

    has_notifications_pat: bool
    """Whether `REPOMATIC_NOTIFICATIONS_PAT` is configured."""

    has_virustotal_key: bool
    """Whether `VIRUSTOTAL_API_KEY` is configured."""

    has_cloudflare_api_token: bool
    """Whether `CLOUDFLARE_API_TOKEN` is configured."""

    @cached_property
    def md(self) -> Metadata:
        """CI and project context, for the repository identity fields."""
        return Metadata()

    @cached_property
    def has_changelog(self) -> bool:
        """Whether the configured changelog exists on disk."""
        return Path(self.config.changelog_location).exists()

    @cached_property
    def nuitka_active(self) -> bool:
        """Whether this project compiles binaries with Nuitka."""
        return bool(self.config.nuitka_enabled and self.md.script_entries)

    @cached_property
    def pypi_package_name(self) -> str | None:
        """The PyPI name to register a Trusted Publisher for, if any.

        Gated on `is_python_package` rather than on `package_name` being set: a
        uv virtual project declares `[project] name` purely to carry
        dependencies, so the name alone says nothing about whether anything is
        ever published. Asking those projects to register a publisher points
        them at a PyPI name they do not own, for a workflow file they do not
        have.
        """
        return self.md.package_name if self.md.is_python_package else None

    @cached_property
    def pat_results(self) -> token.PatPermissionResults | None:
        """The PAT permission probe results, or `None` when unrunnable."""
        if not (self.has_pat and self.repo):
            return None
        return token.check_all_pat_permissions(self.repo)

    @cached_property
    def missing_permissions_section(self) -> str:
        """Warning table naming the permissions the configured PAT lacks."""
        failures = self.pat_results.failed() if self.pat_results else []
        if not failures:
            return ""
        table = render_table(
            [[message] for _field_name, message in failures],
            headers=["Permission issue"],
            table_format=TableFormat.GITHUB,
        )
        return (
            "> [!WARNING]\n"
            "> Your `REPOMATIC_PAT` secret is configured but missing"
            " some permissions.\n"
            "> Update the token using the pre-filled link below.\n\n"
            f"{table}\n"
        )

    @property
    def token_ok(self) -> bool:
        """Whether a PAT is configured and every permission probe passed."""
        return self.has_pat and not self.missing_permissions_section

    @property
    def cloudflare_secrets_ok(self) -> bool:
        """Whether the Cloudflare Pages deploy can authenticate.

        The token alone settles it: the account it belongs to is derived
        from it at run time, even when it is scoped to nothing but
        `Cloudflare Pages: Edit`, so there is no second identifier to
        configure and nothing else to ask for here.
        """
        return self.has_cloudflare_api_token

    @property
    def cloudflare_token_name(self) -> str:
        """Suggested name for the deploy token, carrying the month it was made.

        Cloudflare's token list shows what a token can do and never how old it
        is, while the rotation procedure turns entirely on telling the
        incumbent from its replacement. Stamping the month into the name is
        what makes a token approaching its one-year expiry obvious at a
        glance, and what lets the two coexist unambiguously during a handover.

        Recomputed per run, so the name the guide suggests stays current while
        the step is still open. It stops moving once the issue closes, which
        is the point at which the body is no longer rewritten.
        """
        return f"{self.md.repo_name}-deploy-{datetime.now(timezone.utc):%Y-%m}"

    def deploys_to(self, target: str) -> bool:
        """Whether this repository publishes its site to *target*.

        One host's setup step is the other's noise, and the guide asks about
        exactly the one `site.deploy` names: a Cloudflare-hosted project has
        no GitHub Pages source to set, and the probe for it answers `404`
        forever.

        The GitHub Pages half stays gated on Sphinx, because the Docs workflow
        is the only publisher repomatic runs for that host and it only builds
        Sphinx trees. The Cloudflare half follows the declaration alone: a
        repository whose site is built by its own workflow still needs the
        project and the two credentials this guide walks through.
        """
        if self.config.site_deploy != target:
            return False
        if target == "github-pages":
            return bool(self.md.is_sphinx)
        return True

    @property
    def dependabot_ok(self) -> bool:
        """Whether vulnerability alerts are confirmed enabled.

        Piggybacks the Dependabot alerts permission probe, which only answers
        `200` when the alerts themselves are on.
        """
        return bool(self.pat_results and self.pat_results.vulnerability_alerts[0])

    def probe_settings(self, check: Callable[[str], CheckResult]) -> bool | None:
        """Run a repository-settings *check*, or report it as failed.

        Without a PAT or a repository there is nothing to read, and the step
        is reported incomplete rather than indeterminate: the reader still has
        to perform it.
        """
        if not (self.has_pat and self.repo):
            return False
        return check(self.repo).passed

    def probe_trusted_publisher(self) -> bool | None:
        """Whether PyPI provenance confirms the Trusted Publisher entry.

        Needs no PAT: the probe hits the public PyPI integrity API.
        """
        if not (self.repo and self.pypi_package_name):
            return None
        return check_pypi_trusted_publisher(self.repo, self.pypi_package_name).passed


@dataclass(frozen=True)
class SetupStep:
    """One step of the setup guide, declared once and read by every phase.

    The guide used to spell each step out four times: a probe, a render, a
    template keyword and a clause of the close gate. Keeping the four in sync
    was manual, and the applicability guards were duplicated between the probe
    and the render. One entry per step now drives all four.
    """

    placeholder: str
    """The `$name` this step's rendered block fills in `setup-guide.md`."""

    title: str
    """Heading shown in the collapsible section's `<summary>` line."""

    template: str
    """Template rendered as the section's body."""

    probe: Callable[[GuideContext], bool | None] = lambda ctx: False
    """Read the step's completion state. Tri-state, per {class}`CheckResult`."""

    applies: Callable[[GuideContext], bool] = lambda ctx: True
    """Whether this repository needs the step at all.

    A step that does not apply renders nothing and satisfies its gate, so a
    non-Sphinx project is never asked about Pages.
    """

    args: Callable[[GuideContext], dict[str, str | None]] = lambda ctx: {
        "repo_url": ctx.md.repo_url,
        "repo_slug": ctx.md.repo_slug,
    }
    """Template variables, defaulting to the pair almost every step wants."""

    gates_closure: bool = True
    """Whether this step's outcome can hold the issue open.

    `False` for the two steps with nothing to probe (immutable releases,
    the final verification), which would otherwise wedge the issue open
    forever.
    """

    tolerates_unknown: bool = False
    """Whether an indeterminate probe (`None`) satisfies the gate.

    `True` for the settings read through Administration-scoped endpoints: a
    PAT without that permission answers `403`, and a reader has no way to
    satisfy a check that cannot run, so it must not block the issue closing.
    Everywhere else `None` is treated as incomplete, keeping the step
    prompting.
    """

    explains_unverifiable: bool = False
    """Append {data}`CANNOT_VERIFY` to the body when the probe answered `None`.

    The reader is looking at a setting they were told to configure, so the
    difference between "verified" and "nobody could look" belongs on screen.
    """

    def outcome(self, ctx: GuideContext) -> bool | None:
        """The step's state as the section renders it.

        `None` survives only where {attr}`tolerates_unknown` says an
        unreadable probe is not the reader's fault; elsewhere it collapses to
        incomplete so the section stays open.
        """
        passed = self.probe(ctx)
        if passed is None and not self.tolerates_unknown:
            return False
        return passed

    def render(self, ctx: GuideContext) -> str:
        """Render this step's collapsible section, empty when it does not apply."""
        if not self.applies(ctx):
            return ""
        passed = self.outcome(ctx)
        content = render_template(self.template, **self.args(ctx))
        if self.explains_unverifiable and passed is None:
            content += CANNOT_VERIFY
        return _wrap_setup_step(self.title, content, passed=passed)

    def satisfied(self, ctx: GuideContext) -> bool:
        """Whether this step lets the issue close.

        A step that does not apply, or that gates nothing, is always
        satisfied.
        """
        if not self.gates_closure or not self.applies(ctx):
            return True
        passed = self.outcome(ctx)
        return (
            passed is None
            if self.tolerates_unknown and passed is None
            else bool(passed)
        )


SETUP_STEPS: tuple[SetupStep, ...] = (
    SetupStep(
        placeholder="step_token",
        title="Create and configure the token",
        template="setup-guide-token",
        probe=lambda ctx: ctx.token_ok,
        args=lambda ctx: {
            "repo_url": ctx.md.repo_url,
            "repo_name": ctx.md.repo_name,
            "repo_owner": ctx.md.repo_owner,
            "repo_slug": ctx.md.repo_slug,
        },
    ),
    SetupStep(
        placeholder="step_dependabot",
        title="Configure Dependabot settings",
        template="setup-guide-dependabot",
        probe=lambda ctx: ctx.dependabot_ok,
    ),
    SetupStep(
        placeholder="immutable_releases_step",
        title="Enable immutable releases",
        template="immutable-releases",
        probe=lambda ctx: ctx.probe_settings(check_immutable_releases),
        applies=lambda ctx: ctx.has_changelog,
        args=lambda ctx: {"repo_url": ctx.md.repo_url},
        gates_closure=False,
        tolerates_unknown=True,
    ),
    SetupStep(
        placeholder="step_branch_ruleset",
        title="Protect the main branch",
        template="setup-guide-branch-ruleset",
        # An unreadable rulesets API answers `None`, which this guide treats as
        # incomplete: the step is the only place a maintainer is told to
        # protect the branch, so an indeterminate probe must keep prompting.
        probe=lambda ctx: ctx.probe_settings(check_branch_ruleset_on_default),
        args=lambda ctx: {"repo_url": ctx.md.repo_url},
    ),
    SetupStep(
        placeholder="step_fork_pr_approval",
        title="Require approval for fork PR workflows",
        template="setup-guide-fork-pr-approval",
        probe=lambda ctx: ctx.probe_settings(check_fork_pr_approval_policy),
        tolerates_unknown=True,
        explains_unverifiable=True,
    ),
    SetupStep(
        placeholder="step_sha_pinning_required",
        title="Require SHA pinning for GitHub Actions",
        template="setup-guide-sha-pinning-required",
        probe=lambda ctx: ctx.probe_settings(check_sha_pinning_required),
        tolerates_unknown=True,
        explains_unverifiable=True,
    ),
    SetupStep(
        placeholder="step_pypi_trusted_publisher",
        title="Register the PyPI Trusted Publisher entry",
        template="setup-guide-pypi-trusted-publisher",
        # Indeterminate (never released, or a pre-OIDC release carrying no
        # provenance) counts as incomplete, so the step keeps prompting until a
        # successful OIDC-attested upload is observed.
        probe=lambda ctx: ctx.probe_trusted_publisher(),
        applies=lambda ctx: bool(ctx.pypi_package_name),
        args=lambda ctx: {
            "package_name": ctx.pypi_package_name,
            "repo_owner": ctx.md.repo_owner,
            "repo_name": ctx.md.repo_name,
            "workflow_filename": PYPI_TRUSTED_PUBLISHER_WORKFLOW,
            "settings_url": pypi_trusted_publisher_settings_url(
                ctx.pypi_package_name or "",
                owner=ctx.md.repo_owner,
                repository=ctx.md.repo_name,
                workflow_filename=PYPI_TRUSTED_PUBLISHER_WORKFLOW,
            ),
        },
    ),
    SetupStep(
        placeholder="step_pages_source",
        title="Set GitHub Pages deployment source to GitHub Actions",
        template="setup-guide-pages-source",
        probe=lambda ctx: (
            check_pages_deployment_source(ctx.repo).passed if ctx.repo else None
        ),
        applies=lambda ctx: ctx.deploys_to("github-pages"),
    ),
    SetupStep(
        placeholder="step_cloudflare_pages",
        title="Configure the Cloudflare Pages credentials",
        template="setup-guide-cloudflare-pages",
        # Unlike the VirusTotal key below, this is a prerequisite rather than
        # an enhancement: `wrangler` cannot authenticate without both values,
        # so the deploy job fails outright instead of skipping. The step holds
        # the issue open until each one is set.
        probe=lambda ctx: ctx.cloudflare_secrets_ok,
        applies=lambda ctx: ctx.deploys_to("cloudflare-pages"),
        args=lambda ctx: {
            "repo_name": ctx.md.repo_name,
            "repo_owner": ctx.md.repo_owner,
            "repo_slug": ctx.md.repo_slug,
            "repo_url": ctx.md.repo_url,
            "token_name": ctx.cloudflare_token_name,
        },
    ),
    SetupStep(
        placeholder="step_virustotal",
        title="Configure VirusTotal scanning (optional)",
        template="setup-guide-virustotal",
        probe=lambda ctx: ctx.has_virustotal_key,
        applies=lambda ctx: ctx.nuitka_active,
    ),
    SetupStep(
        placeholder="step_notifications_pat",
        title="Create and configure the notifications token",
        template="setup-guide-notifications-pat",
        # The unsubscribe workflow skips silently without the secret, so the
        # guide is the only onboarding surface for it.
        probe=lambda ctx: ctx.has_notifications_pat,
        applies=lambda ctx: ctx.config.notification_unsubscribe,
    ),
    SetupStep(
        placeholder="step_verify",
        title="Verify the setup",
        template="setup-guide-verify",
        gates_closure=False,
    ),
)
"""Every step of the setup guide, in the order the issue body lists them."""


def _org_tip(repo_owner: str | None) -> str:
    """Suggest a machine user when the repository owner is an organization."""
    if not repo_owner:
        return ""
    try:
        owner_type = run_gh_command(
            ["api", f"users/{repo_owner}", "--jq", ".type"],
        ).strip()
    except RuntimeError:
        logging.debug(f"Failed to detect owner type for {repo_owner!r}.")
        return ""
    if owner_type != "Organization":
        return ""
    return (
        "> 💡 **For organizations**: Consider using a"
        " [machine user account](https://docs.github.com/en/"
        "get-started/learning-about-github/types-of-github-accounts"
        "#personal-accounts) or a dedicated service account to own"
        " the PAT, rather than tying it to an individual's account."
    )


def manage_setup_guide(
    config: Config,
    *,
    has_pat: bool,
    has_notifications_pat: bool,
    has_virustotal_key: bool,
    has_cloudflare_api_token: bool = False,
    repo: str | None,
) -> None:
    """Render the setup guide issue body and drive the issue lifecycle.

    Walks {data}`SETUP_STEPS`: each step probes its own state, renders its
    collapsible section, and reports whether it lets the issue close. The
    issue closes only when every applicable gating step passes.

    :param config: The resolved `[tool.repomatic]` configuration.
    :param has_pat: Whether `REPOMATIC_PAT` is configured.
    :param has_notifications_pat: Whether `REPOMATIC_NOTIFICATIONS_PAT` is
        configured.
    :param has_virustotal_key: Whether `VIRUSTOTAL_API_KEY` is configured.
    :param has_cloudflare_api_token: Whether `CLOUDFLARE_API_TOKEN` is
        configured.
    :param repo: Repository in `owner/repo` format; permission and settings
        checks are skipped when `None`.
    """
    ctx = GuideContext(
        config=config,
        repo=repo,
        has_pat=has_pat,
        has_notifications_pat=has_notifications_pat,
        has_virustotal_key=has_virustotal_key,
        has_cloudflare_api_token=has_cloudflare_api_token,
    )

    sections: dict[str, str | None] = {
        step.placeholder: step.render(ctx) for step in SETUP_STEPS
    }
    setup_body = render_template(
        "setup-guide",
        missing_permissions_section=ctx.missing_permissions_section,
        org_tip=_org_tip(ctx.md.repo_owner),
        repo_url=ctx.md.repo_url,
        **sections,
    )

    manage_issue_lifecycle(
        has_issues=not all(step.satisfied(ctx) for step in SETUP_STEPS),
        body=setup_body,
        labels=[BOT_ISSUE_LABEL],
        title="Repomatic setup guide",
        no_issues_comment=(
            "PAT configured, all permissions verified, repository settings complete."
        ),
    )
