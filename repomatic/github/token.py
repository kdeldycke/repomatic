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

"""GitHub token validation utilities.

Provides early validation for CLI commands that depend on the GitHub API,
so users get clear error messages at startup rather than opaque failures
mid-execution.

```{note} Why `REPOMATIC_PAT` is needed

GitHub's `GITHUB_TOKEN` cannot modify workflow files in `.github/`.
Neither `contents: write`, `actions: write`, nor ``permissions:
write-all`` grant this ability. The only way to push changes to workflow
YAML files is via a fine-grained Personal Access Token with the
**Workflows** permission. Without it, pushes are rejected with::

    ! [remote rejected] branch_xxx -> branch_xxx (refusing to allow a
    GitHub App to create or update workflow
    `.github/workflows/my_workflow.yaml` without `workflows` permission)

Additionally, events triggered by `GITHUB_TOKEN` do not start new
workflow runs (see [GitHub docs](https://docs.github.com/en/actions/security-guides/automatic-token-authentication#using-the-github_token-in-a-workflow)),
so tag pushes also need the PAT to trigger downstream workflows.

The **Settings → Actions → General → Workflow permissions** setting has
no effect on this limitation — it's a hard security boundary enforced by
GitHub regardless of repository-level settings.

Jobs that use `REPOMATIC_PAT`:

- `autofix.yaml`: fix-typos, sync-repomatic
  (PRs touching `.github/workflows/` files).
- `changelog.yaml`: prepare-release (freezes versions in workflow files).
- `release.yaml`: create-tag (push triggers `on.push.tags`),
  create-release (triggers downstream workflows).
- `renovate.yaml`: renovate (dependency PRs, status checks, dashboard,
  vulnerability alerts).

All jobs fall back to `GITHUB_TOKEN` when the PAT is unavailable
(`secrets.REPOMATIC_PAT || github.token`), but
operations requiring the `workflows` permission or workflow triggering
will silently fail.

Token permission mapping:

- **Workflows** — PRs that touch `.github/workflows/` files.
- **Contents** — Tag pushes, release publishing, PR branch creation.
- **Pull requests** — All PR-creating jobs.
- **Commit statuses** — Renovate `stability-days` status checks.
- **Dependabot alerts** — Renovate vulnerability alert reading.
- **Issues** — Renovate Dependency Dashboard.
- **Metadata** — Required for all fine-grained token API operations.
```
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, fields
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from .gh import run_gh_command
from .status import status_annotation

# Canonical list of fine-grained PAT permissions required by REPOMATIC_PAT.
# Each tuple: (permission_name, access_level, reason).
# This is the single source of truth — the setup guide template, pre-filled
# token URL, and lint-repo capability checks must all agree with this list.
REQUIRED_PAT_PERMISSIONS = (
    ("Commit statuses", "Read and Write", "Renovate stability-days status checks."),
    (
        "Contents",
        "Read and Write",
        "Tag pushes, release publishing, PR branch creation.",
    ),
    (
        "Dependabot alerts",
        "Read-only",
        "Renovate reads vulnerability alerts for security PRs.",
    ),
    ("Issues", "Read and Write", "Renovate Dependency Dashboard."),
    ("Metadata", "Read-only", "Required for all fine-grained token API operations."),
    (
        "Pull requests",
        "Read and Write",
        "All PR-creating jobs (sync-repomatic, fix-typos, prepare-release, Renovate).",
    ),
    (
        "Workflows",
        "Read and Write",
        "Push changes to .github/workflows/ files.",
    ),
)


def _with_status_annotation(msg: str) -> str:
    """Append a {func}`~repomatic.github.status.status_annotation` when active.

    Returns *msg* unchanged when GitHub reports healthy or the probe
    fails, so callers can wrap any diagnostic without branching.
    """
    annotation = status_annotation()
    if annotation:
        return f"{msg} {annotation}"
    return msg


def _classify_pat_error(stderr: str, missing_permission_msg: str) -> str:
    """Build a permission-check failure message from a `gh` stderr.

    Returns the canonical "missing permission" message only when stderr
    actually says ``HTTP 403`` (the unambiguous signature of a token
    lacking the relevant scope). For any other failure mode (401 from
    an auth incident, 5xx from GitHub, network errors) the actual stderr
    is surfaced so operators see the real cause instead of being misled
    toward PAT scopes that are already correct. A
    `githubstatus.com <https://www.githubstatus.com>`_ annotation is
    appended when an incident is active.

    :param stderr: The raw error string from {func}`run_gh_command`.
    :param missing_permission_msg: Message to return when the failure is
        unambiguously a 403 (permission missing).
    :return: A diagnostic string suitable for the `(passed, msg)` tuple.
    """
    if "HTTP 403" in stderr:
        msg = missing_permission_msg
    else:
        msg = f"GitHub API call failed: {stderr.strip()}"
    return _with_status_annotation(msg)


def check_pat_contents_permission(repo: str) -> tuple[bool, str]:
    """Check that the token has contents permission.

    Tests read access via ``GET /repos/{owner}/{repo}/contents/.github``.

    :param repo: Repository in 'owner/repo' format.
    :return: Tuple of (passed, message).
    """
    try:
        run_gh_command([
            "api",
            f"repos/{repo}/contents/.github",
            "--silent",
        ])
    except RuntimeError as exc:
        return False, _classify_pat_error(
            str(exc),
            "Token lacks 'Contents: Read and Write' permission. "
            "Update the PAT to include this permission.",
        )
    return True, "Contents: token has access"


def check_pat_issues_permission(repo: str) -> tuple[bool, str]:
    """Check that the token has issues permission.

    Tests read access via ``GET /repos/{owner}/{repo}/issues``.

    :param repo: Repository in 'owner/repo' format.
    :return: Tuple of (passed, message).
    """
    try:
        run_gh_command([
            "api",
            f"repos/{repo}/issues?per_page=1&state=all",
            "--silent",
        ])
    except RuntimeError as exc:
        return False, _classify_pat_error(
            str(exc),
            "Token lacks 'Issues: Read and Write' permission. "
            "Update the PAT to include this permission.",
        )
    return True, "Issues: token has access"


def check_pat_pull_requests_permission(repo: str) -> tuple[bool, str]:
    """Check that the token has pull requests permission.

    Tests read access via ``GET /repos/{owner}/{repo}/pulls``.

    :param repo: Repository in 'owner/repo' format.
    :return: Tuple of (passed, message).
    """
    try:
        run_gh_command([
            "api",
            f"repos/{repo}/pulls?per_page=1&state=all",
            "--silent",
        ])
    except RuntimeError as exc:
        return False, _classify_pat_error(
            str(exc),
            "Token lacks 'Pull requests: Read and Write' permission. "
            "Update the PAT to include this permission.",
        )
    return True, "Pull requests: token has access"


def check_pat_vulnerability_alerts_permission(repo: str) -> tuple[bool, str]:
    """Check that the token has Dependabot alerts permission and alerts are enabled.

    Tests access via ``GET /repos/{owner}/{repo}/dependabot/alerts?per_page=1``.
    Returns 200 when the token has the `vulnerability_alerts` permission and
    alerts are enabled. Fails on 403 (token lacks the permission) or 404
    (alerts not enabled).

    ```{note}

    The older ``GET /repos/{owner}/{repo}/vulnerability-alerts`` endpoint
    requires the `Administration: read` fine-grained token permission,
    not `Dependabot alerts`. The Dependabot alerts listing endpoint used
    here correctly maps to the `vulnerability_alerts` permission scope.
    ```

    :param repo: Repository in 'owner/repo' format.
    :return: Tuple of (passed, message).
    """
    try:
        run_gh_command([
            "api",
            f"repos/{repo}/dependabot/alerts?per_page=1",
            "--silent",
        ])
    except RuntimeError as exc:
        stderr = str(exc)
        if "HTTP 404" in stderr:
            return False, _with_status_annotation(
                "Vulnerability alerts are not enabled on the repository. "
                f"Enable them: gh api repos/{repo}/vulnerability-alerts"
                " --method PUT"
            )
        return False, _classify_pat_error(
            stderr,
            "Token lacks 'Dependabot alerts: Read-only' permission. "
            "Update the PAT to include this permission.",
        )
    return True, "Dependabot alerts: token has access, alerts enabled"


def check_pat_workflows_permission(repo: str) -> tuple[bool, str]:
    """Check that the token has workflows permission.

    Tests access via ``GET /repos/{owner}/{repo}/actions/workflows``.
    Fine-grained PATs with the **Workflows** permission get `actions:read`
    access. Without it, this endpoint returns 403.

    :param repo: Repository in 'owner/repo' format.
    :return: Tuple of (passed, message).
    """
    try:
        run_gh_command([
            "api",
            f"repos/{repo}/actions/workflows?per_page=1",
            "--silent",
        ])
    except RuntimeError as exc:
        return False, _classify_pat_error(
            str(exc),
            "Token lacks 'Workflows: Read and Write' permission. "
            "Update the PAT to include this permission.",
        )
    return True, "Workflows: token has access"


def check_commit_statuses_permission(repo: str, sha: str) -> tuple[bool, str]:
    """Check that the token has commit statuses permission.

    Required for Renovate to set stability-days status checks.

    :param repo: Repository in 'owner/repo' format.
    :param sha: Commit SHA to check.
    :return: Tuple of (passed, message).
    """
    try:
        run_gh_command([
            "api",
            f"repos/{repo}/commits/{sha}/statuses",
            "--silent",
        ])
    except RuntimeError as exc:
        return False, _classify_pat_error(
            str(exc),
            "Token lacks 'Commit statuses: Read and Write' permission. "
            "Update the PAT to include this permission.",
        )
    return True, "Commit statuses: token has access"


@dataclass
class PatPermissionResults:
    """Results of all PAT permission checks.

    Each field holds a `(passed, message)` tuple from the corresponding
    `check_pat_*` function. The `commit_statuses` field is `None` when
    no commit SHA was available to probe.
    """

    contents: tuple[bool, str]
    """Result of {func}`check_pat_contents_permission`."""

    issues: tuple[bool, str]
    """Result of {func}`check_pat_issues_permission`."""

    pull_requests: tuple[bool, str]
    """Result of {func}`check_pat_pull_requests_permission`."""

    vulnerability_alerts: tuple[bool, str]
    """Result of {func}`check_pat_vulnerability_alerts_permission`."""

    workflows: tuple[bool, str]
    """Result of {func}`check_pat_workflows_permission`."""

    commit_statuses: tuple[bool, str] | None = None
    """Result of {func}`check_commit_statuses_permission`, or `None` if skipped."""

    def all_passed(self) -> bool:
        """Return `True` when every executed check passed."""
        for f in fields(self):
            result = getattr(self, f.name)
            if result is not None and not result[0]:
                return False
        return True

    def failed(self) -> list[tuple[str, str]]:
        """Return `(field_name, message)` pairs for each failed check."""
        failures: list[tuple[str, str]] = []
        for f in fields(self):
            result = getattr(self, f.name)
            if result is not None and not result[0]:
                failures.append((f.name, result[1]))
        return failures

    def iter_results(self) -> list[tuple[bool, str]]:
        """Yield all non-`None` `(passed, message)` tuples."""
        results: list[tuple[bool, str]] = []
        for f in fields(self):
            result = getattr(self, f.name)
            if result is not None:
                results.append(result)
        return results


def check_all_pat_permissions(
    repo: str,
    sha: str | None = None,
) -> PatPermissionResults:
    """Run all PAT permission checks and return structured results.

    This is the single entry point for PAT permission validation. Both
    `lint-repo` and `setup-guide` call this function so that adding a
    new permission check benefits all consumers automatically.

    :param repo: Repository in 'owner/repo' format.
    :param sha: Commit SHA for the statuses check. When `None`, the
        `commit_statuses` field is set to `None` (skipped).
    :return: {class}`PatPermissionResults` with all check outcomes.
    """
    return PatPermissionResults(
        contents=check_pat_contents_permission(repo),
        issues=check_pat_issues_permission(repo),
        pull_requests=check_pat_pull_requests_permission(repo),
        vulnerability_alerts=check_pat_vulnerability_alerts_permission(repo),
        workflows=check_pat_workflows_permission(repo),
        commit_statuses=(check_commit_statuses_permission(repo, sha) if sha else None),
    )


def validate_gh_token_env() -> None:
    """Check that a GitHub token environment variable is set.

    Lookup order: `REPOMATIC_PAT` > `GH_TOKEN` > `GITHUB_TOKEN`,
    matching {func}`run_gh_command <repomatic.github.gh.run_gh_command>`.

    :raises RuntimeError: If no variable is set.
    """
    if not (
        os.environ.get("REPOMATIC_PAT")
        or os.environ.get("GH_TOKEN")
        or os.environ.get("GITHUB_TOKEN")
    ):
        msg = (
            "No GitHub token found. "
            "Set REPOMATIC_PAT, GH_TOKEN, or GITHUB_TOKEN. "
            "Create one at https://github.com/settings/tokens"
        )
        raise RuntimeError(msg)


def _get_gh_token() -> str:
    """Return the GitHub token from environment variables.

    Lookup order: `REPOMATIC_PAT` > `GH_TOKEN` > `GITHUB_TOKEN`,
    matching {func}`run_gh_command <repomatic.github.gh.run_gh_command>`.
    """
    return (
        os.environ.get("REPOMATIC_PAT")
        or os.environ.get("GH_TOKEN")
        or os.environ.get("GITHUB_TOKEN")
        or ""
    )


def validate_gh_api_access() -> tuple[int, dict[str, str], str]:
    """Smoke-test the GitHub API and return parsed response.

    Calls `GET https://api.github.com/rate_limit` with the token from
    environment variables.

    :return: Tuple of `(status_code, headers, body)`.
    :raises RuntimeError: If the API returns a 4xx/5xx status.
    """
    request = Request("https://api.github.com/rate_limit")
    token = _get_gh_token()
    if token:
        request.add_header("Authorization", f"token {token}")

    try:
        response = urlopen(request)
    except HTTPError as exc:
        message = ""
        try:
            message = json.loads(exc.read().decode()).get("message", "")
        except (json.JSONDecodeError, AttributeError):
            pass
        detail = f"GitHub API returned an error ({exc.code})."
        if message:
            detail += f" GitHub says: {message}"
        raise RuntimeError(detail) from exc

    body = response.read().decode()
    headers = {k.lower(): v for k, v in response.headers.items()}
    return response.status, headers, body


def validate_classic_pat_scope(required_scope: str) -> list[str]:
    """Validate that the GitHub token is a classic PAT with the required scope.

    Checks:

    1. A GitHub token environment variable is set.
    2. GitHub API is reachable (smoke-test GET).
    3. Token is a classic PAT (has `X-OAuth-Scopes` header).
    4. Token has the required scope.

    :param required_scope: The OAuth scope to require
        (e.g. `"notifications"`).
    :return: The full list of scopes on the token.
    :raises RuntimeError: If any check fails.
    """
    validate_gh_token_env()
    _status_code, headers, _body = validate_gh_api_access()

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

    logging.info("Token validated: scopes='%s'.", scopes_header)
    return scope_list
