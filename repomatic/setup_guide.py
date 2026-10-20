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

Backs the `setup-guide` command: composes the repository-settings checks of
{mod}`repomatic.lint_repo` and the PAT permission probes of
{mod}`repomatic.github.token` with the `setup-guide-*` templates into a single
issue body, then drives the issue lifecycle. The guide reads the same
{class}`~repomatic.lint_repo.context.LintContext` as `lint-repo`, so the two
commands agree on every gate.

Each setup step renders as a collapsible section whose open/closed state and
emoji reflect the check outcome, and the issue closes only once every
verifiable step passes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from click_extra import TableFormat, render_table

from .github.issue import manage_issue_lifecycle
from .github.pr_body import render_template
from .github.threads import BOT_ISSUE_LABEL
from .lint_repo.settings import (
    check_branch_ruleset_on_default,
    check_fork_pr_approval_policy,
    check_immutable_releases,
    check_pypi_trusted_publisher,
    check_sha_pinning_required,
)
from .lint_repo.site import check_pages_deployment_source
from .pypi import (
    PYPI_TRUSTED_PUBLISHER_WORKFLOW,
    pypi_trusted_publisher_settings_url,
)

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Callable

    from .checks import CheckResult
    from .lint_repo.context import LintContext


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
    "\n\n> ℹ️ **Note**: This setting could not be verified: `REPOMATIC_PAT` is"
    " missing the **Administration: Read-only** permission. Update the token with"
    " the pre-filled link in the first step."
)
"""Note appended to a step whose probe could not run.

Both settings it covers are read through Administration-scoped endpoints, so a
PAT issued without that permission answers `403` and the check lands on `None`.
The note is the only place the missing permission shows: it has no other
symptom.
"""


def _missing_permissions_section(ctx: LintContext) -> str:
    """Warning table naming the permissions the configured PAT lacks."""
    failures = ctx.pat_permissions.failed() if ctx.pat_permissions else []
    if not failures:
        return ""
    table = render_table(
        [[message] for _field, message in failures],
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


def _scope_warning(ctx: LintContext) -> str:
    """Warning naming another repository the configured PAT covers.

    Empty unless the scope probe failed. A probe that could not conclude
    answers `None`, and the reader has nothing to act on then: the token step
    stays closed, as it does for a pass.
    """
    if ctx.pat_scope is None or ctx.pat_scope.passed is not False:
        return ""
    return f"> ⚠️ **Warning**: {ctx.pat_scope.message}"


def _token_ok(ctx: LintContext) -> bool:
    """Whether a PAT is configured, permitted and limited to this repository.

    The token step asks for this repository and nothing else, so a token that
    the scope probe finds on another repository has not done that step.
    """
    return (
        ctx.has_pat
        and not _missing_permissions_section(ctx)
        and not _scope_warning(ctx)
    )


def _dependabot_ok(ctx: LintContext) -> bool:
    """Whether vulnerability alerts are confirmed enabled.

    Piggybacks the Dependabot alerts permission probe, which only answers
    `200` when the alerts themselves are on.
    """
    alerts = (
        ctx.pat_permissions.get("vulnerability_alerts") if ctx.pat_permissions else None
    )
    return bool(alerts and alerts.passed)


def _probe_setting(
    ctx: LintContext, check: Callable[[LintContext], CheckResult]
) -> bool | None:
    """Run a repository-settings *check*, or report it as failed.

    Without a PAT or a repository there is nothing to read, and the step is
    reported incomplete rather than indeterminate: the reader still has to
    perform it.
    """
    if not (ctx.has_pat and ctx.repo):
        return False
    return check(ctx).passed


def _probe_trusted_publisher(ctx: LintContext) -> bool | None:
    """Whether PyPI provenance confirms the Trusted Publisher entry.

    Needs no PAT: the probe hits the public PyPI integrity API.
    """
    if not ctx.repo:
        return None
    return check_pypi_trusted_publisher(ctx).passed


def _pypi_package(ctx: LintContext) -> str | None:
    """The PyPI name to register a Trusted Publisher for, if any.

    Gated on `is_package` rather than on `package_name` being set: a uv
    virtual project declares `[project] name` purely to carry dependencies, so
    the name alone says nothing about whether anything is ever published.
    Asking those projects to register a publisher points them at a PyPI name
    they do not own, for a workflow file they do not have.
    """
    return ctx.package_name if ctx.is_package else None


def _dated_token_name(ctx: LintContext, purpose: str) -> str:
    """Suggested name for a Cloudflare token, carrying the month it was made.

    Cloudflare's token list shows what a token can do and never how old it is,
    while the rotation procedure turns entirely on telling the incumbent from
    its replacement. Stamping the month into the name is what makes a token
    approaching its one-year expiry obvious at a glance, and what lets the two
    coexist unambiguously during a handover.

    Recomputed per run, so the name the guide suggests stays current while the
    step is still open. It stops moving once the issue closes, which is the
    point at which the body is no longer rewritten.

    :param purpose: What the token is for, like `deploy` or `r2`.
    """
    return f"{ctx.md.repo_name}-{purpose}-{datetime.now(UTC):%Y-%m}"


def _default_args(ctx: LintContext) -> dict[str, str | None]:
    """The template variables almost every step wants."""
    return {"repo_url": ctx.md.repo_url, "repo_slug": ctx.md.repo_slug}


@dataclass(frozen=True)
class SetupStep:
    """One step of the setup guide, declared once and read by every phase.

    One entry per step drives its probe, its rendering, its template keyword
    and its part in the close gate, so the four stay in sync.
    """

    placeholder: str
    """The `$name` this step's rendered block fills in `setup-guide.md`."""

    title: str
    """Heading shown in the collapsible section's `<summary>` line."""

    template: str
    """Template rendered as the section's body."""

    probe: Callable[[LintContext], bool | None] = lambda ctx: False
    """Read the step's completion state.

    Tri-state, per {class}`~repomatic.checks.CheckResult`.
    """

    applies: Callable[[LintContext], bool] = lambda ctx: True
    """Whether this repository needs the step at all.

    A step that does not apply renders nothing and satisfies its gate, so a
    non-Sphinx project is never asked about Pages.
    """

    args: Callable[[LintContext], dict[str, str | None]] = _default_args
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

    def outcome(self, ctx: LintContext) -> bool | None:
        """Probe the step, once: its state as the section renders it.

        `None` survives only where {attr}`tolerates_unknown` says an
        unreadable probe is not the reader's fault; elsewhere it collapses to
        incomplete so the section stays open.
        """
        passed = self.probe(ctx)
        if passed is None and not self.tolerates_unknown:
            return False
        return passed

    def render(self, ctx: LintContext, passed: bool | None) -> str:
        """Render this step's collapsible section for its *passed* outcome."""
        content = render_template(self.template, **self.args(ctx))
        if self.explains_unverifiable and passed is None:
            content += CANNOT_VERIFY
        return _wrap_setup_step(self.title, content, passed=passed)

    def satisfied(self, passed: bool | None) -> bool:
        """Whether a step with the *passed* outcome lets the issue close."""
        if not self.gates_closure:
            return True
        if passed is None:
            return self.tolerates_unknown
        return passed


SETUP_STEPS: tuple[SetupStep, ...] = (
    SetupStep(
        placeholder="step_token",
        title="Create and configure the token",
        template="setup-guide-token",
        probe=_token_ok,
        args=lambda ctx: {
            "repo_url": ctx.md.repo_url,
            "repo_name": ctx.md.repo_name,
            "repo_owner": ctx.md.repo_owner,
            "repo_slug": ctx.md.repo_slug,
            "scope_warning": _scope_warning(ctx),
        },
    ),
    SetupStep(
        placeholder="step_dependabot",
        title="Configure Dependabot settings",
        template="setup-guide-dependabot",
        probe=_dependabot_ok,
    ),
    SetupStep(
        placeholder="immutable_releases_step",
        title="Enable immutable releases",
        template="immutable-releases",
        probe=lambda ctx: _probe_setting(ctx, check_immutable_releases),
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
        probe=lambda ctx: _probe_setting(ctx, check_branch_ruleset_on_default),
        args=lambda ctx: {"repo_url": ctx.md.repo_url},
    ),
    SetupStep(
        placeholder="step_fork_pr_approval",
        title="Require approval for fork PR workflows",
        template="setup-guide-fork-pr-approval",
        probe=lambda ctx: _probe_setting(ctx, check_fork_pr_approval_policy),
        tolerates_unknown=True,
        explains_unverifiable=True,
    ),
    SetupStep(
        placeholder="step_sha_pinning_required",
        title="Require SHA pinning for GitHub Actions",
        # The step's `gh api` call reads the settings back and writes them with
        # one field flipped: `PUT /actions/permissions` requires `enabled`, so a
        # payload carrying `sha_pinning_required` alone is rejected.
        template="setup-guide-sha-pinning-required",
        probe=lambda ctx: _probe_setting(ctx, check_sha_pinning_required),
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
        probe=_probe_trusted_publisher,
        applies=lambda ctx: bool(_pypi_package(ctx)),
        args=lambda ctx: {
            "package_name": _pypi_package(ctx),
            "repo_owner": ctx.md.repo_owner,
            "repo_name": ctx.md.repo_name,
            "workflow_filename": PYPI_TRUSTED_PUBLISHER_WORKFLOW,
            "settings_url": pypi_trusted_publisher_settings_url(
                _pypi_package(ctx) or "",
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
            check_pages_deployment_source(ctx).passed if ctx.repo else None
        ),
        applies=lambda ctx: ctx.deploys_to("github-pages"),
    ),
    SetupStep(
        placeholder="step_cloudflare_pages",
        title="Configure the Cloudflare Pages credentials",
        template="setup-guide-cloudflare-pages",
        # Unlike the VirusTotal key below, this is a prerequisite rather than
        # an enhancement: `wrangler` cannot authenticate without the token, so
        # the deploy job fails outright instead of skipping. The step holds the
        # issue open until it is set. The token alone settles it: the account
        # it belongs to is derived from it at run time.
        probe=lambda ctx: ctx.has_cloudflare_api_token,
        applies=lambda ctx: ctx.deploys_to("cloudflare-pages"),
        args=lambda ctx: {
            "repo_name": ctx.md.repo_name,
            "repo_slug": ctx.md.repo_slug,
            "token_name": _dated_token_name(ctx, "deploy"),
        },
    ),
    SetupStep(
        placeholder="step_cloudflare_r2",
        title="Configure the R2 upload keys",
        # No URL pre-fills the R2 token form, which reads `type` alone. The generic
        # form's `permissionGroupKeys` prefill sets every group at account scope and
        # drops bucket-scoped ones, so it cannot express a one-bucket token.
        template="setup-guide-cloudflare-r2",
        # The deploy still publishes without the keys, but it drops every file
        # over the Pages limit, the very files the declared bucket exists to
        # serve. So the step holds the issue open until both keys are set.
        probe=lambda ctx: ctx.has_cloudflare_r2_keys,
        applies=lambda ctx: bool(
            ctx.deploys_to("cloudflare-pages") and ctx.config.site_cloudflare_r2_bucket
        ),
        args=lambda ctx: {
            "bucket": ctx.config.site_cloudflare_r2_bucket,
            "domain": ctx.config.site_cloudflare_r2_domain,
            "repo_slug": ctx.md.repo_slug,
            "token_name": _dated_token_name(ctx, "r2"),
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
"""Every step of the setup guide, in the order the issue body lists them.

Each step's template takes the shape of the `REPOMATIC_PAT` step: one lead
sentence saying what to set and why, then numbered actions in the order they run.
An action gives the command, then at most one alternative, written `Or by hand:`
when it goes through the web UI. A form's values go in a field table. A note only
warns against a wrong action or names the cost of skipping one. Reasons and other
routes live in `docs/`, or in a comment beside the step.
"""


def _org_tip(ctx: LintContext) -> str:
    """Suggest a machine user when the repository owner is an organization.

    Reads the owner type off the repository object the context fetches once.
    """
    owner = (ctx.repo_object or {}).get("owner") or {}
    if owner.get("type") != "Organization":
        return ""
    return (
        "> 💡 **For organizations**: let a"
        " [machine user account](https://docs.github.com/en/"
        "get-started/learning-about-github/types-of-github-accounts"
        "#personal-accounts) or a service account own the PAT, instead of"
        " an individual's account."
    )


def manage_setup_guide(ctx: LintContext) -> None:
    """Render the setup guide issue body and drive the issue lifecycle.

    Walks {data}`SETUP_STEPS`: each step that applies probes its state once,
    renders its collapsible section from that state, and reports whether it
    lets the issue close. The issue closes only when every applicable gating
    step passes.

    :param ctx: What the steps read. A context without a repository skips the
        permission and settings probes.
    """
    sections: dict[str, str | None] = {}
    complete = True
    for step in SETUP_STEPS:
        if not step.applies(ctx):
            sections[step.placeholder] = ""
            continue
        passed = step.outcome(ctx)
        sections[step.placeholder] = step.render(ctx, passed)
        complete = complete and step.satisfied(passed)

    setup_body = render_template(
        "setup-guide",
        missing_permissions_section=_missing_permissions_section(ctx),
        org_tip=_org_tip(ctx),
        repo_url=ctx.md.repo_url,
        **sections,
    )

    manage_issue_lifecycle(
        has_issues=not complete,
        body=setup_body,
        labels=[BOT_ISSUE_LABEL],
        title="Repomatic setup guide",
        no_issues_comment=(
            "PAT configured, all permissions verified, repository settings complete."
        ),
    )
