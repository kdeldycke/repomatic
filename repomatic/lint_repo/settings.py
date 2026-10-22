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

"""Checks of the repository settings the setup guide asks for.

The Actions policies and the immutable releases setting each read one endpoint,
through a {class}`RepoSettingProbe`. The rulesets and the branch protection
rules decide how the default branch is protected. The PyPI Trusted Publisher
entry, the repository secrets and the `REPOMATIC_PAT` permissions complete the
list.

```{note}
Most of these endpoints need the `Administration: Read-only` permission. A
token without it gets a `403`, which a check reports as skipped, never as a
failure.
```
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..checks import CheckResult
from ..github.gh import gh_graphql, gh_json
from ..github.urls import split_slug
from ..pypi import (
    PYPI_TRUSTED_PUBLISHER_WORKFLOW,
    get_latest_release_file,
    get_trusted_publishers,
    pypi_trusted_publisher_settings_url,
)

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Iterator
    from typing import Any, Final

    from .context import LintContext


@dataclass(frozen=True)
class RepoSettingProbe:
    """One repository setting to read, and how its values map to verdicts.

    The repository-setting checks share one shape: read one API endpoint
    through `gh`, inspect one field, and map its value onto a pass, a failure
    naming the settings page, or an indeterminate skip. What varies is declared
    here, and {meth}`check` runs it. A probe with no *passing* set reads its
    field as a boolean toggle and never skips on an unknown value.
    """

    label: str
    """Check name opening every skip message."""

    endpoint: str
    """API path under `repos/{repo}/` to read."""

    field: str
    """Response field carrying the setting."""

    pass_message: str
    """Success message; `{value}` interpolates the field's value."""

    fail_message: str
    """Failure message; `{repo}` interpolates the repository slug."""

    passing: frozenset[str] | None = None
    """Values that pass; `None` reads the field as a boolean toggle."""

    failing: frozenset[str] = frozenset()
    """Values that fail explicitly; anything in neither set skips."""

    unknown: str = "unknown value {value!r}"
    """Skip reason for a value in neither set; `{value}` interpolates it."""

    unavailable: str = "could not query API"
    """Skip reason when the endpoint cannot be read."""

    def check(self, repo: str) -> CheckResult:
        """Read this setting on *repo*.

        :param repo: Repository in `owner/repo` format.
        :return: A `CheckResult`. `passed` is `None` when the check could not
            run.
        """
        data = gh_json(["api", f"repos/{repo}/{self.endpoint}"], soft=True)
        if not isinstance(data, dict):
            return CheckResult(None, f"{self.label}: skipped ({self.unavailable}).")

        value = data.get(self.field, "")
        if self.passing is None:
            if value:
                return CheckResult(True, self.pass_message)
            return CheckResult(False, self.fail_message.format(repo=repo))
        if value in self.passing:
            return CheckResult(True, self.pass_message.format(value=value))
        if value in self.failing:
            return CheckResult(False, self.fail_message.format(repo=repo))
        return CheckResult(
            None, f"{self.label}: skipped ({self.unknown.format(value=value)})."
        )


FORK_PR_APPROVAL_POLICY: Final = RepoSettingProbe(
    label="Fork PR approval policy check",
    endpoint="actions/permissions/fork-pr-contributor-approval",
    field="approval_policy",
    pass_message="Fork PR approval policy: {value}.",
    # `{repo}` is filled by `RepoSettingProbe.check`, not an f-string.
    fail_message=(
        "Fork PR approval policy is 'first_time_contributors_new_to_github',"
        " which only catches brand-new GitHub accounts."
        " Set it to 'first_time_contributors' (or stricter) under"
        " https://github.com/{repo}/settings/actions"
        " to require approval for any first-time contributor."
    ),
    passing=frozenset({"first_time_contributors", "all_external_contributors"}),
    failing=frozenset({"first_time_contributors_new_to_github"}),
    unknown="unknown policy {value!r}",
)
"""The fork PR approval policy, read by {func}`check_fork_pr_approval_policy`."""

SHA_PINNING_REQUIRED: Final = RepoSettingProbe(
    label="SHA pinning required check",
    endpoint="actions/permissions",
    field="sha_pinning_required",
    pass_message="SHA pinning required: enabled.",
    # `{repo}` is filled by `RepoSettingProbe.check`, not an f-string.
    fail_message=(
        "SHA pinning is not required for GitHub Actions in this repository."
        " Enable it under"
        " https://github.com/{repo}/settings/actions"
        " (Actions permissions → Require actions to be pinned to a"
        " full-length commit SHA) so GitHub rejects any unpinned action"
        " reference, not just the ones zizmor happens to catch."
    ),
)
"""The `sha_pinning_required` policy, read by {func}`check_sha_pinning_required`."""

IMMUTABLE_RELEASES: Final = RepoSettingProbe(
    label="Immutable releases check",
    endpoint="immutable-releases",
    field="enabled",
    pass_message="Immutable releases are enabled.",
    fail_message="Immutable releases are not enabled.",
)
"""The immutable releases setting, read by {func}`check_immutable_releases`."""

BRANCH_PROTECTION_RULES_QUERY = """
query($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) {
    branchProtectionRules(first: 100) {
      nodes { pattern }
    }
  }
}
"""
"""Lists every branch protection rule a repository declares, by branch pattern.

GraphQL because REST cannot answer the question. `GET
/repos/{repo}/branches/{branch}/protection` reads one concrete branch, so
finding a rule needs the branch name up front and still misses any pattern
targeting a branch that does not exist yet. This field enumerates the rules
themselves.

The field also draws the line the check depends on: a ruleset never appears
here, and a branch protection rule never appears under `rulesets`. Verified on
2026-08-27 against a repository holding both at once, which listed the branch
protection rule here and the ruleset only there.
"""


def _active_rulesets(rulesets: list[dict[str, Any]], target: str) -> list[str]:
    """The names of the active rulesets aimed at *target*, `branch` or `tag`."""
    return [
        ruleset["name"]
        for ruleset in rulesets
        if isinstance(ruleset, dict)
        and ruleset.get("target") == target
        and ruleset.get("enforcement") == "active"
    ]


def check_fork_pr_approval_policy(ctx: LintContext) -> CheckResult:
    """Check that fork PR workflows require approval for first-time contributors.

    GitHub Actions has a per-repository policy that controls when workflows
    from fork pull requests must be approved by a maintainer before they run.
    The three values, from weakest to strongest, are
    `first_time_contributors_new_to_github`,
    `first_time_contributors`, and `all_external_contributors`.

    The default (`first_time_contributors_new_to_github`) only catches
    brand-new GitHub accounts, which is trivial to bypass with a slightly
    aged account. The minimum acceptable setting is `first_time_contributors`,
    which requires approval for any first-time contributor to this repository.
    This is one of the mitigations recommended in Astral's open-source security
    post: see https://astral.sh/blog/open-source-security-at-astral.

    ```{note}
    The endpoint needs the `Administration: Read-only` permission. When the
    `REPOMATIC_PAT` lacks it, or the API call fails for any other reason, the
    check reports an indeterminate result rather than a negative one.
    ```
    """
    return FORK_PR_APPROVAL_POLICY.check(ctx.slug)


def check_sha_pinning_required(ctx: LintContext) -> CheckResult:
    """Check that GitHub Actions must be pinned to a full-length commit SHA.

    GitHub has a per-repository policy, `sha_pinning_required`, that makes
    the platform itself refuse to run any workflow referencing an action by a
    mutable tag or branch instead of a commit SHA. repomatic already pins
    every action it generates, and the `lint-workflow-security` job runs
    `zizmor`, which flags an unpinned `uses:` ref. But a `zizmor` finding can be
    silenced inline (`# zizmor: ignore[...]`), so a hand-edited workflow could
    still slip a mutable tag past review. This repository setting is the
    platform-enforced backstop.

    ```{note}
    The endpoint needs the `Administration: Read-only` permission. When the
    `REPOMATIC_PAT` lacks it, or the API call fails for any other reason, the
    check reports an indeterminate result rather than a negative one.
    ```
    """
    return SHA_PINNING_REQUIRED.check(ctx.slug)


def check_tag_protection_rules(ctx: LintContext) -> CheckResult:
    """Check that no tag ruleset can block the `create-tag` workflow job.

    A tag ruleset that restricts creation or requires status checks can stop
    `REPOMATIC_PAT` (or `GITHUB_TOKEN`) from pushing release tags. The check
    warns about any active ruleset that targets tags.
    """
    if ctx.rulesets is None:
        return CheckResult(
            None, "Tag protection check: skipped (could not query rulesets API)."
        )

    tag_rulesets = _active_rulesets(ctx.rulesets, "tag")
    if tag_rulesets:
        names = ", ".join(tag_rulesets)
        msg = (
            f"Active tag rulesets found: {names}."
            " These may block the create-tag job from pushing release tags."
            " Ensure the REPOMATIC_PAT token is in the bypass list,"
            " or remove the rulesets."
        )
        return CheckResult(False, msg)
    return CheckResult(True, "No active tag rulesets found.")


def check_branch_ruleset_on_default(ctx: LintContext) -> CheckResult:
    """Check that at least one active branch ruleset exists.

    Reads the same rulesets as {func}`check_tag_protection_rules`, and looks
    for an active ruleset with `target == "branch"`. The presence of any such
    ruleset is taken as evidence that the default branch is protected
    (restrict deletions and block force pushes).

    ```{note}
    This is a heuristic: it does not verify the ruleset targets the default
    branch specifically, nor that it enables the exact rules the setup guide
    recommends. A deeper check would read each ruleset's conditions through
    ``GET /repos/{repo}/rulesets/{id}``, adding N+1 API calls.
    ```

    :return: A `CheckResult`. `passed` is `None` when the rulesets API could
        not be read, matching {func}`check_tag_protection_rules`, which reads
        the same payload.
    """
    if ctx.rulesets is None:
        return CheckResult(
            None, "Branch ruleset check: skipped (could not query rulesets API)."
        )

    branch_rulesets = _active_rulesets(ctx.rulesets, "branch")
    if branch_rulesets:
        names = ", ".join(branch_rulesets)
        return CheckResult(True, f"Active branch rulesets found: {names}.")
    return CheckResult(
        False, "No active branch rulesets found protecting the default branch."
    )


def check_classic_branch_protection(ctx: LintContext) -> CheckResult:
    """Check that no branch protection rule survives beside the rulesets.

    Branch protection rules predate rulesets and GitHub still supports both,
    with no deprecation notice and no removal date. They are not alternatives
    a repository picks between: [both apply at
    once](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/about-rulesets),
    and where the two carry the same rule, the stricter version wins. A rule
    left behind after a migration therefore changes nothing on the day it is
    left, which is what makes it worth reporting: the branch policy is now
    split across two settings pages, and an edit to one page does not show on
    the other.

    {func}`check_branch_ruleset_on_default` states the other half of the same
    policy, that a ruleset must exist. Together they say the protection is a
    ruleset and only a ruleset.

    ```{note}
    Advisory, not fatal. A leftover rule protects the branch rather than
    exposing it, so the finding is a cleanup.
    ```

    :return: A `CheckResult`. `passed` is `None` when the API could not be
        read, matching the ruleset checks. Reading the rules needs admin on
        the repository, so a token without it skips rather than passes.
    """
    owner, name = split_slug(ctx.slug)
    try:
        data = gh_graphql(BRANCH_PROTECTION_RULES_QUERY, owner=owner, name=name)
    except (RuntimeError, KeyError) as error:
        logging.debug(f"Could not read branch protection rules for {ctx.slug}: {error}")
        return CheckResult(
            None,
            "Branch protection rule check: skipped"
            " (could not query the branchProtectionRules API).",
        )

    repository = (data or {}).get("repository") or {}
    nodes = (repository.get("branchProtectionRules") or {}).get("nodes") or []
    patterns = sorted(
        node["pattern"]
        for node in nodes
        if isinstance(node, dict) and node.get("pattern")
    )
    if not patterns:
        return CheckResult(
            True, "No branch protection rules found: rulesets are the only layer."
        )

    names = ", ".join(f"`{pattern}`" for pattern in patterns)
    msg = (
        f"Branch protection rules found on: {names}."
        " Rulesets already protect this repository, and the two layer, so the"
        " branch policy is split across two settings pages."
        " Move anything these rules still hold into a ruleset at"
        f" https://github.com/{ctx.slug}/settings/rules, then delete them at"
        f" https://github.com/{ctx.slug}/settings/branches."
    )
    return CheckResult(False, msg)


def check_immutable_releases(ctx: LintContext) -> CheckResult:
    """Check that immutable releases are enabled for the repository.

    ```{note}
    The endpoint needs the "Administration: Read-only" permission on
    fine-grained PATs. When the `REPOMATIC_PAT` lacks it, the check reports an
    indeterminate result rather than a negative one.
    ```
    """
    return IMMUTABLE_RELEASES.check(ctx.slug)


def check_pypi_trusted_publisher(ctx: LintContext) -> CheckResult:
    """Check that the PyPI Trusted Publisher entry is registered for this repo.

    PyPI's Trusted Publisher settings are owner-only at
    `/manage/project/<name>/settings/publishing/` and not exposed through
    any public API. The only public surface where the OIDC publisher is
    observable is the PEP 740 provenance attached to releases uploaded via
    OIDC: see {func}`repomatic.pypi.get_trusted_publishers`.
    This check probes the latest release's provenance and looks for a
    bundle whose `repository` matches the repository and whose `workflow`
    is {data}`~repomatic.pypi.PYPI_TRUSTED_PUBLISHER_WORKFLOW`.
    A match means the publisher is wired up and a previous release uploaded
    successfully through it.
    A mismatch (provenance exists but names a different repo or workflow)
    is a misconfiguration: typical cause is registering the upstream
    reusable workflow instead of the downstream caller's `release.yaml`,
    which fails on the first upload after migration.
    Indeterminate (`None`) covers two cases that look identical from the
    outside: no published release yet, and provenance missing because past
    releases were uploaded via API token. In both cases the setup guide
    nags until the next OIDC-attested upload appears.
    """
    repo = ctx.slug
    package_name = ctx.package_name or ""
    latest = get_latest_release_file(package_name)
    if latest is None:
        return CheckResult(
            None,
            f"PyPI Trusted Publisher check: skipped (no released version of"
            f" '{package_name}' on PyPI yet).",
        )
    version, filename = latest

    publishers = get_trusted_publishers(package_name, version, filename)
    if publishers is None:
        return CheckResult(
            None,
            f"PyPI Trusted Publisher check: skipped (no provenance for"
            f" '{package_name}' {version}; previous release likely uploaded"
            f" via API token).",
        )

    if not publishers:
        return CheckResult(
            None,
            f"PyPI Trusted Publisher check: skipped (provenance for"
            f" '{package_name}' {version} contains no publisher bundles).",
        )

    for publisher in publishers:
        if (
            publisher.kind == "GitHub"
            and publisher.repository == repo
            and publisher.workflow == PYPI_TRUSTED_PUBLISHER_WORKFLOW
        ):
            return CheckResult(
                True,
                f"PyPI Trusted Publisher matches: {publisher.repository}"
                f" via {publisher.workflow}.",
            )

    observed = ", ".join(f"{p.repository}:{p.workflow}" for p in publishers)
    owner, repository = split_slug(repo)
    settings_url = pypi_trusted_publisher_settings_url(
        package_name,
        owner=owner,
        repository=repository,
        workflow_filename=PYPI_TRUSTED_PUBLISHER_WORKFLOW,
    )
    msg = (
        f"PyPI Trusted Publisher mismatch for '{package_name}' {version}."
        f" Expected {repo} via {PYPI_TRUSTED_PUBLISHER_WORKFLOW},"
        f" but provenance names: {observed}."
        f" Register the correct entry at {settings_url}."
    )
    return CheckResult(False, msg)


def check_virustotal_secret(ctx: LintContext) -> CheckResult:
    """Report whether the VirusTotal API key is available to release builds."""
    if ctx.has_virustotal_key:
        return CheckResult(True, "VIRUSTOTAL_API_KEY secret is configured.")
    return CheckResult(
        False,
        "VIRUSTOTAL_API_KEY secret is not configured."
        " Release binaries will not be submitted to VirusTotal."
        " Get a free API key at https://www.virustotal.com/gui/my-apikey"
        " and add it as a repository secret.",
    )


def check_notifications_secret(ctx: LintContext) -> CheckResult:
    """Report whether the unsubscribe workflow has the token it needs."""
    if ctx.has_notifications_pat:
        return CheckResult(True, "REPOMATIC_NOTIFICATIONS_PAT secret is configured.")
    return CheckResult(
        False,
        "REPOMATIC_NOTIFICATIONS_PAT secret is not configured."
        " The unsubscribe workflow will skip silently."
        " Create a classic PAT with the notifications scope at"
        " https://github.com/settings/tokens/new"
        "?description=REPOMATIC_NOTIFICATIONS_PAT&scopes=notifications"
        " and add it as a repository secret.",
    )


def check_pat_permissions(ctx: LintContext) -> Iterator[CheckResult]:
    """Yield one result per PAT permission probe, or a skip note without a PAT.

    The probes are the rows of {data}`~repomatic.github.token.PAT_PERMISSION_PROBES`.
    They need a repository to read, so a PAT with no repository yields nothing.
    """
    if not ctx.has_pat:
        yield CheckResult(None, "PAT capability checks: skipped (no REPOMATIC_PAT)")
        return
    if ctx.pat_permissions is not None:
        yield from ctx.pat_permissions.values()


def check_pat_scope(ctx: LintContext) -> CheckResult:
    """Check that the PAT reaches no other repository of the same owner.

    Reads the probe of {func}`repomatic.github.token.check_pat_repository_scope`,
    which the setup guide reads too.
    """
    return ctx.pat_scope or CheckResult(
        None, "PAT scope check: skipped (no REPOMATIC_PAT)."
    )
