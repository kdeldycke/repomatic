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

"""GitHub token validation, and the probes of what `REPOMATIC_PAT` can do.

The validators run before a command that depends on the GitHub API, so a user
gets a clear error at startup rather than an opaque failure mid-run. The probes
read the permissions and the repository scope of the configured
`REPOMATIC_PAT`: `lint-repo` reports what they find, and `setup-guide` asks the
reader to fix it.

```{note} Why `REPOMATIC_PAT` is needed

GitHub's `GITHUB_TOKEN` cannot modify workflow files in `.github/`. Neither
`contents: write`, `actions: write`, nor `permissions: write-all` grant this
ability. The only way to push changes to workflow YAML files is through a
fine-grained Personal Access Token with the **Workflows** permission. Without
it, GitHub rejects the push:

    ! [remote rejected] branch_xxx -> branch_xxx (refusing to allow a
    GitHub App to create or update workflow
    `.github/workflows/my_workflow.yaml` without `workflows` permission)

Events triggered by `GITHUB_TOKEN` also start no new workflow runs (see
[GitHub docs](https://docs.github.com/en/actions/security-guides/automatic-token-authentication#using-the-github_token-in-a-workflow)),
so tag pushes need the PAT to trigger downstream workflows too.

The **Settings → Actions → General → Workflow permissions** setting has no
effect on this limitation: it is a hard security boundary that GitHub enforces
whatever the repository settings say.

The permission has to reach the actual `git push`, not just the `gh` CLI.
Setting `GH_TOKEN` in a step's `env` only authenticates the `gh` and
`repomatic` API calls of that process. A bare `git push`
(`git_ops.force_push_branch`, which every `pr-sync` template goes through)
authenticates with the credentials `actions/checkout` configured for the job,
which default to `github.token` whatever `GH_TOKEN` says. A job whose diff can
touch `.github/workflows/` needs
`token: ${{ secrets.REPOMATIC_PAT || github.token }}` on its own checkout step
too, or the push is rejected as if the PAT had never been set.

Every job falls back to `GITHUB_TOKEN` when the PAT is unavailable
(`secrets.REPOMATIC_PAT || github.token`), and then silently fails any
operation that needs the `workflows` permission or that must trigger a workflow.

Token permission mapping:

- **Workflows**: PRs that touch `.github/workflows/` files.
- **Contents**: tag pushes, release publishing, PR branch creation.
- **Pull requests**: every PR-creating job.
- **Dependabot alerts**: `fix-vulnerable-deps` reads vulnerability alerts.
- **Issues**: the setup guide issue.
- **Administration**: reads the Actions settings the setup guide verifies,
  SHA pinning required and the fork-PR contributor-approval policy. Read-only:
  repomatic never writes a repository setting.
- **Metadata**: required for every fine-grained token API operation.
```
"""

from __future__ import annotations

import json
import logging
from collections import UserDict
from typing import NamedTuple

from ..checks import CheckResult
from ..http import FetchError, request
from .gh import GhCommandError, api_headers, resolve_gh_token, run_gh_command
from .status import with_status_annotation
from .urls import split_slug


def _classify_pat_error(error: GhCommandError, missing_permission_msg: str) -> str:
    """Build a permission-check failure message from a failed `gh` call.

    Returns the canonical "missing permission" message only for an `HTTP 403`
    (the unambiguous signature of a token lacking the relevant scope). For any
    other failure mode (401 from an auth incident, 5xx from GitHub, network
    errors) the actual error is surfaced so operators see the real cause
    instead of being misled toward PAT scopes that are already correct. A
    [githubstatus.com](https://www.githubstatus.com) annotation is appended
    when an incident is active.

    :param error: The failure raised by {func}`~repomatic.github.gh.run_gh_command`.
    :param missing_permission_msg: Message to return when the failure is
        unambiguously a 403 (permission missing).
    :return: The failure message of the {class}`~repomatic.checks.CheckResult`.
    """
    if error.status == 403:
        msg = missing_permission_msg
    else:
        msg = f"GitHub API call failed: {str(error).strip()}"
    return with_status_annotation(msg)


class PatProbe(NamedTuple):
    """One fine-grained PAT permission probe.

    A read-only API call whose `HTTP 403` unambiguously identifies the
    missing fine-grained permission. Rows live in
    {data}`PAT_PERMISSION_PROBES`.
    """

    field: str
    """The key of this probe's result in {class}`PatPermissionResults`."""

    permission: str
    """Fine-grained permission label, as GitHub's PAT form spells it."""

    endpoint: str
    """Read-only probe endpoint, with a `{repo}` placeholder."""

    success: str
    """Message reported when the probe returns a 2xx."""

    not_found: str = ""
    """Message template (with `{repo}`) when the probe 404s.

    Empty for probes whose 404 carries no special meaning: those fall
    through to the generic failure classification.
    """

    @property
    def name(self) -> str:
        """The permission's name without its access level, like `Contents`."""
        return self.permission.partition(":")[0]

    def run(self, repo: str) -> CheckResult:
        """Run this probe against *repo*.

        :param repo: Repository in `owner/repo` format.
        :return: A passing result, or a failing one that names the cause.
        """
        try:
            run_gh_command(["api", self.endpoint.format(repo=repo), "--silent"])
        except GhCommandError as exc:
            if self.not_found and exc.status == 404:
                return CheckResult(
                    False, with_status_annotation(self.not_found.format(repo=repo))
                )
            return CheckResult(
                False,
                _classify_pat_error(
                    exc,
                    f"Token lacks {self.permission!r} permission. "
                    "Update the PAT to include this permission.",
                ),
            )
        return CheckResult(True, self.success)


PAT_PERMISSION_PROBES: tuple[PatProbe, ...] = (
    # `GET /repos/{repo}/actions/permissions` is Administration-scoped, as is
    # the `fork-pr-contributor-approval` endpoint beside it. Both back a
    # setup-guide check, and without this permission both answer 403, leaving
    # those two settings permanently unverified.
    PatProbe(
        "administration",
        "Administration: Read-only",
        "repos/{repo}/actions/permissions",
        "Administration: token has access",
    ),
    PatProbe(
        "contents",
        "Contents: Read and Write",
        "repos/{repo}/contents/.github",
        "Contents: token has access",
    ),
    PatProbe(
        "issues",
        "Issues: Read and Write",
        "repos/{repo}/issues?per_page=1&state=all",
        "Issues: token has access",
    ),
    PatProbe(
        "pull_requests",
        "Pull requests: Read and Write",
        "repos/{repo}/pulls?per_page=1&state=all",
        "Pull requests: token has access",
    ),
    # The Dependabot alerts listing endpoint correctly maps to the
    # `vulnerability_alerts` permission scope; the older
    # `GET /repos/{repo}/vulnerability-alerts` endpoint requires the
    # `Administration: read` permission instead. A 200 also proves alerts
    # are enabled; a 404 means they are not (distinct from a missing
    # permission), hence the dedicated `not_found` message.
    PatProbe(
        "vulnerability_alerts",
        "Dependabot alerts: Read-only",
        "repos/{repo}/dependabot/alerts?per_page=1",
        "Dependabot alerts: token has access, alerts enabled",
        not_found=(
            "Vulnerability alerts are not enabled on the repository. "
            "Enable them: gh api repos/{repo}/vulnerability-alerts"
            " --method PUT"
        ),
    ),
    # Fine-grained PATs with the Workflows permission get `actions:read`
    # access; without it this endpoint returns 403.
    PatProbe(
        "workflows",
        "Workflows: Read and Write",
        "repos/{repo}/actions/workflows?per_page=1",
        "Workflows: token has access",
    ),
)
"""The PAT permission probes, in report order."""


class PatPermissionResults(UserDict[str, CheckResult]):
    """The result of each PAT permission probe, keyed by {attr}`PatProbe.field`.

    Built by {func}`check_all_pat_permissions`, in the order of
    {data}`PAT_PERMISSION_PROBES`.
    """

    def failed(self) -> list[tuple[str, str]]:
        """Return `(field, message)` pairs for each failed probe."""
        return [
            (field, result.message)
            for field, result in self.items()
            if not result.passed
        ]


def check_all_pat_permissions(repo: str) -> PatPermissionResults:
    """Run every PAT permission probe against *repo*.

    The single entry point for PAT permission validation. Both `lint-repo` and
    `setup-guide` call it, so a new row of {data}`PAT_PERMISSION_PROBES`
    reaches every consumer.

    :param repo: Repository in `owner/repo` format.
    :return: The result of each probe.
    """
    return PatPermissionResults(
        (probe.field, probe.run(repo)) for probe in PAT_PERMISSION_PROBES
    )


PAT_SCOPE_PROBE = next(
    probe for probe in PAT_PERMISSION_PROBES if probe.field == "administration"
)
"""The permission probe {func}`check_pat_repository_scope` sends to other repositories.

The endpoint that reports a token's own repository selection,
`GET /installation/repositories`, refuses a personal access token. So the check
reads what the token is allowed to do. A token gets a 2xx from this endpoint
only for a repository it covers with `Administration: Read-only`. An answer
from another repository then proves the token covers it.

No other row of {data}`PAT_PERMISSION_PROBES` can do that. A public repository
answers the contents, issues, pull requests and workflows endpoints to any
caller. The Dependabot alerts endpoint answers `404` wherever alerts are off,
which says nothing about the token. Read on 2026-10-13: this endpoint answers
`401` to a caller with no token, and `403` to an account that does not
administer the repository.

The `permissions` object that ``GET /repos/{repo}`` returns cannot serve either:
it describes the account, not the token. Checked on 2026-10-20: a token limited
to one repository got `push: true` there for another repository of the same
owner.
"""

PAT_SCOPE_PROBE_LIMIT = 10
"""How many other repositories {func}`check_pat_repository_scope` probes.

Each probe is one API call, made on every `lint-repo` and `setup-guide` run. The
check takes the owner's most recently pushed repositories: a token shared
between repositories serves the active ones, and a token with access to all of
them gets an answer from the first one probed.
"""


def check_pat_repository_scope(repo: str) -> CheckResult:
    """Check that the PAT has no access to another repository of the same owner.

    A fine-grained PAT should name this repository alone under **Only select
    repositories**, to follow the principle of least privilege. The check sends
    {data}`PAT_SCOPE_PROBE` to the owner's {data}`PAT_SCOPE_PROBE_LIMIT` most
    recently pushed other repositories. The first one that answers fails the
    check.

    Each probe splits three ways:

    - **2xx**: the token covers that repository too. Fail, and name it.
    - **HTTP 403**: the token is refused there. The check passes once a probe
      was refused and none answered.
    - **Anything else** (404, 5xx, network): indeterminate for that repository.

    ```{note}
    Only a 2xx fails the check, so it never reports a token limited to this
    repository. A pass is narrower than it reads: it covers the probed
    repositories, and `/users/{owner}/repos` lists the public ones only. A
    token is also refused wherever its account has no admin role, whatever the
    token covers.
    ```

    :param repo: Repository in `owner/repo` format.
    :return: A `CheckResult`.
    """
    # A refusal from another repository means something only when this
    # repository answers the same request.
    endpoint = PAT_SCOPE_PROBE.endpoint
    try:
        run_gh_command(["api", endpoint.format(repo=repo), "--silent"])
    except GhCommandError:
        return CheckResult(
            None,
            "PAT scope check: skipped"
            f" (no {PAT_SCOPE_PROBE.permission!r} access to probe with).",
        )

    owner = split_slug(repo)[0]
    try:
        # `gh api` sends a POST once a field is passed, which this endpoint
        # answers with a 404. One more than the limit, so that leaving this
        # repository out of the page still fills the quota.
        output = run_gh_command([
            "api",
            "--method",
            "GET",
            f"/users/{owner}/repos",
            "--jq",
            ".[].full_name",
            "--raw-field",
            f"per_page={PAT_SCOPE_PROBE_LIMIT + 1}",
            "--raw-field",
            "type=owner",
            "--raw-field",
            "sort=pushed",
        ])
    except GhCommandError:
        return CheckResult(
            None, "PAT scope check: skipped (could not list owner repos)."
        )

    # GitHub matches a repository name whatever its case, and `--repo` takes
    # the name as typed: an exact comparison would probe this repository as
    # if it were another one.
    other_repos = [
        name
        for name in map(str.strip, output.splitlines())
        if name and name.casefold() != repo.casefold()
    ][:PAT_SCOPE_PROBE_LIMIT]
    if not other_repos:
        return CheckResult(None, "PAT scope check: skipped (no other repos to probe).")

    refused = 0
    for other in other_repos:
        try:
            run_gh_command(["api", endpoint.format(repo=other), "--silent"])
        except GhCommandError as exc:
            if exc.status == 403:
                refused += 1
            continue
        msg = (
            f"PAT also has access to {other}."
            " Limit it to this repository under 'Only select repositories'."
        )
        return CheckResult(False, msg)

    if not refused:
        return CheckResult(
            None, "PAT scope check: skipped (no probe got a clear answer)."
        )
    return CheckResult(
        True, f"PAT scope: no access to other repositories ({refused} probed)."
    )


def validate_gh_token_env() -> None:
    """Check that a GitHub token environment variable is set.

    Lookup order: `REPOMATIC_PAT` > `GH_TOKEN` > `GITHUB_TOKEN`,
    matching {func}`run_gh_command <repomatic.github.gh.run_gh_command>`.

    :raises RuntimeError: If no variable is set.
    """
    if not resolve_gh_token():
        msg = (
            "No GitHub token found. "
            "Set REPOMATIC_PAT, GH_TOKEN, or GITHUB_TOKEN. "
            "Create one at https://github.com/settings/tokens"
        )
        raise RuntimeError(msg)


GITHUB_RATE_LIMIT_URL = "https://api.github.com/rate_limit"
"""The GitHub API endpoint {func}`validate_gh_api_access` reads.

Any authenticated caller may read it, and reading it costs no rate limit.
"""


def _api_error_message(body: str) -> str:
    """The `message` field of a GitHub API error body, empty when it has none."""
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return ""
    message = payload.get("message") if isinstance(payload, dict) else None
    return message if isinstance(message, str) else ""


def validate_gh_api_access() -> dict[str, str]:
    """Smoke-test the GitHub API and return the response headers.

    Calls `GET https://api.github.com/rate_limit` with the token from
    environment variables, through {func}`~repomatic.http.request`. The headers
    are what a caller reads: `X-OAuth-Scopes` is what tells a classic PAT apart
    from a fine-grained one.

    :return: The response headers, keyed by lowercase name.
    :raises RuntimeError: If the API returns a 4xx/5xx status, or cannot be
        reached at all (network error, timeout).
    """
    try:
        response = request(GITHUB_RATE_LIMIT_URL, headers=api_headers())
    except FetchError as exc:
        if exc.status is None:
            detail = f"Could not reach the GitHub API: {exc}"
        else:
            detail = f"GitHub API returned an error ({exc.status})."
            message = _api_error_message(exc.body)
            if message:
                detail += f" GitHub says: {message}"
        raise RuntimeError(with_status_annotation(detail)) from exc
    return response.headers


def validate_classic_pat_scope(required_scope: str) -> list[str]:
    """Validate that the GitHub token is a classic PAT with the required scope.

    Checks:

    1. A GitHub token environment variable is set.
    2. GitHub API is reachable (smoke-test GET).
    3. Token is a classic PAT (has `X-OAuth-Scopes` header).
    4. Token has the required scope.

    :param required_scope: The OAuth scope to require (like `"notifications"`).
    :return: The full list of scopes on the token.
    :raises RuntimeError: If any check fails.
    """
    validate_gh_token_env()
    headers = validate_gh_api_access()

    scopes_header = headers.get("x-oauth-scopes")
    if scopes_header is None:
        msg = (
            "No X-OAuth-Scopes header found."
            " The token must be a classic PAT"
            " (fine-grained PATs are not supported)."
        )
        raise RuntimeError(msg)

    scope_list = [s.strip() for s in scopes_header.split(",") if s.strip()]
    if required_scope not in scope_list:
        msg = (
            f"Token scopes: '{scopes_header}'."
            f" The '{required_scope}' scope is required."
        )
        raise RuntimeError(msg)

    logging.info(f"Token validated: scopes='{scopes_header}'.")
    return scope_list


def validate_notifications_token() -> None:
    """Check the token can read the notifications API, and warn when it can do more.

    The unsubscribe command needs a classic PAT holding the `notifications`
    scope, per {func}`validate_classic_pat_scope`. A token holding other scopes
    works too, and gets a warning: least privilege asks for that scope alone.

    :raises RuntimeError: If the token is missing, unreadable, fine-grained, or
        lacks the scope.
    """
    scope_list = validate_classic_pat_scope("notifications")
    if scope_list != ["notifications"]:
        scopes_header = ", ".join(scope_list)
        logging.warning(
            f"GH_TOKEN has more scopes than needed: '{scopes_header}'. Only "
            "'notifications' is required."
        )
