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

The permission has to reach the actual `git push`, not just the `gh` CLI:
setting `GH_TOKEN` in a step's `env` only authenticates `gh`/`repomatic` API
calls made by that process. A bare `git push` (`git_ops.force_push_branch`,
which every `pr-sync` template goes through) instead authenticates with
whatever credentials `actions/checkout` configured for the job, which
defaults to `github.token` regardless of `GH_TOKEN`. A job whose diff can
touch `.github/workflows/` needs `token: ${{ secrets.REPOMATIC_PAT ||
github.token }}` on its own checkout step too, or the push is rejected
exactly as if the PAT had never been set.

Jobs that use `REPOMATIC_PAT`:

- `autofix.yaml`: fix-typos, sync-repomatic, sync-action-pins,
  sync-workflow-pins (PRs touching `.github/workflows/` files),
  sync-tool-versions (upstream-only dependency PRs), fix-vulnerable-deps
  (reads the GitHub Advisory Database).
- `changelog.yaml`: prepare-release (freezes versions in workflow files).
- `release.yaml`: create-tag (push triggers `on.push.tags`),
  create-release (triggers downstream workflows).

All jobs fall back to `GITHUB_TOKEN` when the PAT is unavailable
(`secrets.REPOMATIC_PAT || github.token`), but
operations requiring the `workflows` permission or workflow triggering
will silently fail.

Token permission mapping:

- **Workflows** — PRs that touch `.github/workflows/` files.
- **Contents** — Tag pushes, release publishing, PR branch creation.
- **Pull requests** — All PR-creating jobs.
- **Dependabot alerts** — fix-vulnerable-deps reads vulnerability alerts.
- **Issues** — Setup guide issue.
- **Administration** — Reads the Actions settings the setup guide verifies:
  SHA pinning required, and the fork-PR contributor-approval policy.
  Read-only: repomatic never writes a repository setting.
- **Metadata** — Required for all fine-grained token API operations.
```
"""

from __future__ import annotations

import functools
import json
import logging
from dataclasses import dataclass, fields
from typing import NamedTuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from click_extra import ClickException

from ..http import DEFAULT_TIMEOUT
from .gh import api_headers, resolve_gh_token, run_gh_command
from .status import with_status_annotation

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Callable
    from types import ModuleType
    from typing import Any


def require_token(
    module: ModuleType, attr: str
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator that runs a token validator before the Click command body.

    Uses late-bound `getattr(module, attr)` so that
    `unittest.mock.patch` can replace the module attribute after import
    and the decorator sees the mock at call time.
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                getattr(module, attr)()
            except RuntimeError as exc:
                raise ClickException(str(exc))
            return func(*args, **kwargs)

        return wrapper

    return decorator


def _classify_pat_error(stderr: str, missing_permission_msg: str) -> str:
    """Build a permission-check failure message from a `gh` stderr.

    Returns the canonical "missing permission" message only when stderr
    actually says `HTTP 403` (the unambiguous signature of a token
    lacking the relevant scope). For any other failure mode (401 from
    an auth incident, 5xx from GitHub, network errors) the actual stderr
    is surfaced so operators see the real cause instead of being misled
    toward PAT scopes that are already correct. A
    [githubstatus.com](https://www.githubstatus.com) annotation is
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
    return with_status_annotation(msg)


class PatProbe(NamedTuple):
    """One fine-grained PAT permission probe.

    A read-only API call whose `HTTP 403` unambiguously identifies the
    missing fine-grained permission. Rows live in
    {data}`PAT_PERMISSION_PROBES`.
    """

    field: str
    """The {class}`PatPermissionResults` field receiving this probe's result."""

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
"""The PAT permission probes, one per {class}`PatPermissionResults` field."""


def probe_pat_permission(repo: str, probe: PatProbe) -> tuple[bool, str]:
    """Run one PAT permission probe against *repo*.

    :param repo: Repository in 'owner/repo' format.
    :param probe: The {class}`PatProbe` row to execute.
    :return: Tuple of (passed, message).
    """
    try:
        run_gh_command(["api", probe.endpoint.format(repo=repo), "--silent"])
    except RuntimeError as exc:
        stderr = str(exc)
        if probe.not_found and "HTTP 404" in stderr:
            return False, with_status_annotation(probe.not_found.format(repo=repo))
        return False, _classify_pat_error(
            stderr,
            f"Token lacks {probe.permission!r} permission. "
            "Update the PAT to include this permission.",
        )
    return True, probe.success


@dataclass
class PatPermissionResults:
    """Results of all PAT permission checks.

    Each field holds a `(passed, message)` tuple from the corresponding
    {data}`PAT_PERMISSION_PROBES` row.
    """

    administration: tuple[bool, str]
    """Result of the `administration` {data}`PAT_PERMISSION_PROBES` row."""

    contents: tuple[bool, str]
    """Result of the `contents` {data}`PAT_PERMISSION_PROBES` row."""

    issues: tuple[bool, str]
    """Result of the `issues` {data}`PAT_PERMISSION_PROBES` row."""

    pull_requests: tuple[bool, str]
    """Result of the `pull_requests` {data}`PAT_PERMISSION_PROBES` row."""

    vulnerability_alerts: tuple[bool, str]
    """Result of the `vulnerability_alerts` {data}`PAT_PERMISSION_PROBES` row."""

    workflows: tuple[bool, str]
    """Result of the `workflows` {data}`PAT_PERMISSION_PROBES` row."""

    def iter_results(self) -> list[tuple[bool, str]]:
        """Every probe's `(passed, message)` pair, in field order.

        Each field is a plain `tuple[bool, str]` filled by
        {func}`check_all_pat_permissions`, so there is nothing optional to
        filter: the one loop serves both the full listing and
        {meth}`failed`'s view of it.
        """
        return [getattr(self, f.name) for f in fields(self)]

    def failed(self) -> list[tuple[str, str]]:
        """Return `(field_name, message)` pairs for each failed check."""
        return [
            (f.name, result[1])
            for f, result in zip(fields(self), self.iter_results())
            if not result[0]
        ]


def check_all_pat_permissions(repo: str) -> PatPermissionResults:
    """Run all PAT permission checks and return structured results.

    This is the single entry point for PAT permission validation. Both
    `lint-repo` and `setup-guide` call this function so that adding a
    new permission check benefits all consumers automatically.

    :param repo: Repository in 'owner/repo' format.
    :return: {class}`PatPermissionResults` with all check outcomes.
    """
    return PatPermissionResults(**{
        probe.field: probe_pat_permission(repo, probe)
        for probe in PAT_PERMISSION_PROBES
    })


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


def validate_gh_api_access() -> tuple[int, dict[str, str], str]:
    """Smoke-test the GitHub API and return parsed response.

    Calls `GET https://api.github.com/rate_limit` with the token from
    environment variables.

    Does not go through {func}`~repomatic.http.get_json`, which returns only
    the parsed body: this is the one caller that needs the *response headers*,
    since `X-OAuth-Scopes` is what tells a classic PAT apart from a
    fine-grained one. It still borrows that module's
    {data}`~repomatic.http.DEFAULT_TIMEOUT`, so a stalled connection fails the
    check instead of hanging the job that runs it.

    :return: Tuple of `(status_code, headers, body)`.
    :raises RuntimeError: If the API returns a 4xx/5xx status, or cannot be
        reached at all (network error, timeout).
    """
    request = Request("https://api.github.com/rate_limit", headers=api_headers())

    try:
        with urlopen(request, timeout=DEFAULT_TIMEOUT) as response:
            status = response.status
            headers = {k.lower(): v for k, v in response.headers.items()}
            body = response.read().decode()
    except HTTPError as exc:
        message = ""
        try:
            message = json.loads(exc.read().decode()).get("message", "")
        except (json.JSONDecodeError, AttributeError):
            pass
        detail = f"GitHub API returned an error ({exc.code})."
        if message:
            detail += f" GitHub says: {message}"
        raise RuntimeError(with_status_annotation(detail)) from exc
    except (URLError, TimeoutError) as exc:
        # Unreachable rather than refused. Only possible to hit now that the
        # request carries a timeout, and the caller's contract promises a
        # RuntimeError for every failure, so it cannot escape raw.
        detail = f"Could not reach the GitHub API: {exc}"
        raise RuntimeError(with_status_annotation(detail)) from exc

    return status, headers, body


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
