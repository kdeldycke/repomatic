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

"""Repository linting for GitHub Actions workflows.

This module provides consistency checks for repository metadata,
including package names, website fields, descriptions, and funding configuration.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import yaml

from .github.actions import NULL_SHA, AnnotationLevel, emit_annotation
from .github.gh import run_gh_command
from .github.token import check_all_pat_permissions
from .metadata import Metadata, stale_axis_values
from .pypi import (
    PYPI_TRUSTED_PUBLISHER_WORKFLOW,
    get_latest_release_file,
    get_trusted_publishers,
    pypi_trusted_publisher_settings_url,
)
from .registry import DEFAULT_REPO
from .version_sync import find_upstream_ref_versions


def get_repo_metadata(repo: str) -> dict[str, str | None]:
    """Fetch repository metadata from GitHub API.

    :param repo: Repository in 'owner/repo' format.
    :return: Dictionary with 'homepageUrl' and 'description' keys.
    """
    try:
        output = run_gh_command([
            "repo",
            "view",
            repo,
            "--json",
            "homepageUrl,description",
        ])
        data = json.loads(output)
        return {
            "homepageUrl": data.get("homepageUrl") or None,
            "description": data.get("description") or None,
        }
    except RuntimeError as e:
        logging.error(f"Failed to fetch repo metadata: {e}")
        return {"homepageUrl": None, "description": None}
    except json.JSONDecodeError as e:
        logging.error(f"Failed to parse repo metadata: {e}")
        return {"homepageUrl": None, "description": None}


def check_package_name_vs_repo(
    package_name: str | None, repo_name: str
) -> tuple[str | None, str]:
    """Check if package name matches repository name.

    :param package_name: The Python package name.
    :param repo_name: The repository name.
    :return: Tuple of (warning_message or None, info_message).
    """
    if not package_name:
        return None, "Package name check: skipped (no package name provided)"

    if package_name != repo_name:
        msg = (
            f"Package name '{package_name}' differs from repository name '{repo_name}'."
        )
        return msg, msg
    return None, f"Package name '{package_name}' matches repository name."


def check_website_for_sphinx(
    repo: str, is_sphinx: bool, homepage_url: str | None = None
) -> tuple[str | None, str]:
    """Check that Sphinx projects have a website set.

    :param repo: Repository in 'owner/repo' format.
    :param is_sphinx: Whether the project uses Sphinx documentation.
    :param homepage_url: The homepage URL from API (to avoid duplicate calls).
    :return: Tuple of (warning_message or None, info_message).
    """
    if not is_sphinx:
        return None, "Website check: skipped (not a Sphinx project)"

    if homepage_url is None:
        metadata = get_repo_metadata(repo)
        homepage_url = metadata.get("homepageUrl")

    if not homepage_url:
        msg = "Sphinx documentation detected but repository website field is not set."
        return msg, msg
    return None, f"Website field is set: {homepage_url}"


def check_description_matches(
    repo: str,
    project_description: str | None,
    repo_description: str | None = None,
) -> tuple[str | None, str]:
    """Check that repository description matches project description.

    :param repo: Repository in 'owner/repo' format.
    :param project_description: Description from pyproject.toml.
    :param repo_description: Description from API (to avoid duplicate calls).
    :return: Tuple of (error_message or None, info_message).
    """
    if not project_description:
        return None, "Description check: skipped (no project description provided)"

    if repo_description is None:
        metadata = get_repo_metadata(repo)
        repo_description = metadata.get("description")

    if project_description != repo_description:
        msg = (
            f"Repo description '{repo_description}' != "
            f"project description '{project_description}'."
        )
        return msg, msg
    return None, "Repository description matches project description."


def _funding_file_exists() -> bool:
    """Check whether a `.github/FUNDING.yml` file exists (case-insensitive).

    GitHub accepts any casing of the filename.
    """
    github_dir = Path(".github")
    if not github_dir.is_dir():
        return False
    return any(
        f.name.upper() == "FUNDING.YML" for f in github_dir.iterdir() if f.is_file()
    )


def check_funding_file(repo: str) -> tuple[str | None, str]:
    """Check that repos with GitHub Sponsors have a `FUNDING.yml`.

    Skips forks (they inherit the parent's sponsor button) and owners
    without a Sponsors listing. Uses the GraphQL API because the REST API
    does not expose `hasSponsorsListing`.

    :param repo: Repository in 'owner/repo' format.
    :return: Tuple of (warning_message or None, info_message).
    """
    if _funding_file_exists():
        return None, "Funding file found."

    owner, name = repo.split("/", 1)

    # Single GraphQL query for both isFork and hasSponsorsListing.
    query = (
        f"{{ repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) {{ isFork }}"
        f" repositoryOwner(login: {json.dumps(owner)}) {{"
        " ... on Sponsorable { hasSponsorsListing } } }"
    )

    try:
        output = run_gh_command(["api", "graphql", "--field", f"query={query}"])
    except RuntimeError as e:
        logging.warning(f"Could not query GitHub Sponsors status: {e}")
        return None, "Funding check: skipped (could not query GitHub API)"

    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        return None, "Funding check: skipped (could not parse API response)"

    repo_data = data.get("data", {}).get("repository", {})
    owner_data = data.get("data", {}).get("repositoryOwner", {})

    if repo_data.get("isFork"):
        return None, "Funding check: skipped (repository is a fork)"

    if not owner_data.get("hasSponsorsListing"):
        return None, "Funding check: skipped (owner has no GitHub Sponsors listing)"

    msg = (
        "Owner has GitHub Sponsors enabled but .github/FUNDING.yml is missing."
        " Create it to display the Sponsor button on the repository."
    )
    return msg, msg


def check_stale_draft_releases(repo: str) -> tuple[str | None, str]:
    """Check for draft releases that are not dev pre-releases.

    Draft releases whose tag does not end with `.dev0` are likely
    leftovers from abandoned or failed release attempts. The only
    expected drafts are the rolling dev pre-releases managed by
    `sync-dev-release`.

    :param repo: Repository in 'owner/repo' format.
    :return: Tuple of (warning_message or None, info_message).
    """
    try:
        output = run_gh_command([
            "release",
            "list",
            "--json",
            "tagName,isDraft",
            "--repo",
            repo,
        ])
    except RuntimeError:
        return None, "Stale draft releases check: skipped (API call failed)."

    try:
        releases = json.loads(output)
    except json.JSONDecodeError:
        return None, "Stale draft releases check: skipped (invalid JSON)."

    stale_drafts = [
        r["tagName"]
        for r in releases
        if r.get("isDraft") and not r["tagName"].endswith(".dev0")
    ]
    if stale_drafts:
        tags = ", ".join(stale_drafts)
        msg = f"Stale draft releases found: {tags}. Delete these leftover drafts."
        return msg, msg
    return None, "No stale draft releases."


def check_topics_subset_of_keywords(
    repo: str,
    keywords: list[str] | None = None,
) -> tuple[str | None, str]:
    """Check that GitHub repo topics are a subset of pyproject.toml keywords.

    :param repo: Repository in 'owner/repo' format.
    :param keywords: Keywords from pyproject.toml. If `None`, check is skipped.
    :return: Tuple of (warning_message or None, info_message).
    """
    if not keywords:
        return None, "Topics check: skipped (no keywords in pyproject.toml)"

    try:
        output = run_gh_command(["api", f"repos/{repo}", "--jq", ".topics[]"])
    except RuntimeError as e:
        logging.warning(f"Could not fetch GitHub topics: {e}")
        return None, "Topics check: skipped (could not fetch GitHub topics)"

    topics = {t.strip() for t in output.splitlines() if t.strip()}
    if not topics:
        return None, "Topics check: skipped (no GitHub topics set)"

    extra = sorted(topics - set(keywords))
    if extra:
        msg = (
            f"GitHub topics not in pyproject.toml keywords: {', '.join(extra)}. "
            "Add them to [project] keywords or remove from repo topics."
        )
        return msg, msg
    return None, f"All {len(topics)} GitHub topics are in pyproject.toml keywords."


def check_pat_repository_scope(repo: str) -> tuple[str | None, str]:
    """Check that the PAT is scoped to only the current repository.

    Fine-grained PATs should use **Only select repositories** to follow
    the principle of least privilege. This check detects tokens configured
    with **All repositories** access.

    Two strategies are tried in order:

    1. `GET /installation/repositories` — returns the repos the token
       can access, including a `repository_selection` field.
    2. Cross-repo probe — check `permissions.push` on another repo
       owned by the same user. If the token can push to a repo it should
       not have access to, it is over-scoped.

    :param repo: Repository in 'owner/repo' format.
    :return: Tuple of (warning_message or None, info_message).
    """
    # Strategy A: installation/repositories endpoint.
    try:
        output = run_gh_command([
            "api",
            "/installation/repositories",
            "--jq",
            ".repository_selection",
        ])
    except RuntimeError:
        logging.debug(
            "installation/repositories not available, trying cross-repo probe."
        )
    else:
        selection = output.strip()
        if selection == "all":
            msg = (
                "PAT has 'All repositories' access."
                " Scope it to 'Only select repositories' for least privilege."
            )
            return msg, msg
        return None, "PAT scope: correctly limited to selected repositories."

    # Strategy B: cross-repo probe.
    owner = repo.split("/", 1)[0]
    try:
        output = run_gh_command([
            "api",
            f"/users/{owner}/repos",
            "--jq",
            ".[].full_name",
            "-f",
            "per_page=10",
            "-f",
            "type=owner",
        ])
    except RuntimeError:
        return None, "PAT scope check: skipped (could not list owner repos)."

    other_repos = [
        r.strip() for r in output.splitlines() if r.strip() and r.strip() != repo
    ]
    if not other_repos:
        return None, "PAT scope check: skipped (no other repos to probe)."

    probe_repo = other_repos[0]
    try:
        output = run_gh_command([
            "api",
            f"repos/{probe_repo}",
            "--jq",
            ".permissions.push",
        ])
        if output.strip() == "true":
            msg = (
                f"PAT has push access to {probe_repo}."
                " Token is likely scoped to 'All repositories'"
                " instead of 'Only select repositories'."
            )
            return msg, msg
    except RuntimeError:
        return None, "PAT scope check: skipped (probe request failed)."

    return None, f"PAT scope: no push access to {probe_repo} (correctly scoped)."


def check_pat_stale_statuses_permission(repo: str) -> tuple[str | None, str]:
    """Detect a PAT that still grants the dropped `Commit statuses` permission.

    `REPOMATIC_PAT` stopped needing `statuses:write` once the Renovate
    integration (and its `stability-days` status checks) was removed. A
    fine-grained PAT cannot report its own granted permissions, so this probes
    behaviorally: it attempts to create a commit status on {data}`NULL_SHA`, a
    SHA that never resolves to a commit. GitHub authorizes the request before
    validating the resource, which splits the outcomes cleanly:

    - **HTTP 403**: the token lacks `statuses:write` (correctly scoped).
    - **HTTP 422** (`No commit found for SHA`): authorization passed and only
      the SHA was rejected, so the token still grants the permission. Warn.
    - **Anything else** (404, 5xx, network): indeterminate, stay silent.

    ```{note}

    Because {data}`NULL_SHA` never resolves, no commit status is ever
    created: the probe mutates nothing. Only an unambiguous 422 raises the
    warning, so a future change to GitHub's authorize-before-validate
    ordering degrades to under-reporting rather than a false warning.
    ```

    :param repo: Repository in 'owner/repo' format.
    :return: Tuple of (warning_message or None, info_message).
    """
    try:
        run_gh_command([
            "api",
            "--method",
            "POST",
            f"repos/{repo}/statuses/{NULL_SHA}",
            "-f",
            "state=success",
            "--silent",
        ])
    except RuntimeError as exc:
        stderr = str(exc)
        if "HTTP 422" in stderr:
            msg = (
                "PAT still grants the 'Commit statuses' permission, which"
                " repomatic no longer uses. Edit the token to remove it for"
                " least privilege."
            )
            return msg, msg
        if "HTTP 403" in stderr:
            return None, "Commit statuses: token correctly lacks the dropped scope."
        return None, "Stale Commit statuses check: skipped (indeterminate response)."
    # A 2xx is unreachable: NULL_SHA can never resolve to a commit.
    return None, "Stale Commit statuses check: skipped (unexpected success)."


def check_fork_pr_approval_policy(repo: str) -> tuple[bool | None, str]:
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

    Queries
    ``GET /repos/{repo}/actions/permissions/fork-pr-contributor-approval``
    and returns `False` when the policy is weaker than
    `first_time_contributors`.

    ```{note}

    This endpoint requires the `Actions: read` permission. When the
    `REPOMATIC_PAT` lacks it (or the API call fails for any other
    reason), the check returns `None` to signal that the result is
    indeterminate rather than negative.
    ```

    :param repo: Repository in 'owner/repo' format.
    :return: Tuple of (passed_or_None, message). `None` means the check
        could not run (API inaccessible, unparsable, or unknown policy).
    """
    try:
        output = run_gh_command([
            "api",
            f"repos/{repo}/actions/permissions/fork-pr-contributor-approval",
        ])
    except RuntimeError:
        return None, "Fork PR approval policy check: skipped (could not query API)."

    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        return (
            None,
            "Fork PR approval policy check: skipped (invalid JSON from API).",
        )

    policy = data.get("approval_policy", "")
    if policy in {"first_time_contributors", "all_external_contributors"}:
        return True, f"Fork PR approval policy: {policy}."

    if policy == "first_time_contributors_new_to_github":
        msg = (
            "Fork PR approval policy is 'first_time_contributors_new_to_github',"
            " which only catches brand-new GitHub accounts."
            " Set it to 'first_time_contributors' (or stricter) under"
            f" https://github.com/{repo}/settings/actions"
            " to require approval for any first-time contributor."
        )
        return False, msg

    return (
        None,
        f"Fork PR approval policy check: skipped (unknown policy '{policy}').",
    )


def check_tag_protection_rules(repo: str) -> tuple[str | None, str]:
    """Check that no tag rulesets could block the `create-tag` workflow job.

    Tag rulesets that restrict creation or require status checks can prevent
    `REPOMATIC_PAT` (or `GITHUB_TOKEN`) from pushing release tags. This
    check queries the repository rulesets API and warns when any ruleset
    targets tags.

    :param repo: Repository in 'owner/repo' format.
    :return: Tuple of (warning_message or None, info_message).
    """
    try:
        output = run_gh_command([
            "api",
            f"repos/{repo}/rulesets",
            "--method",
            "GET",
            "-f",
            "includes_parents=true",
        ])
    except RuntimeError:
        return None, "Tag protection check: skipped (could not query rulesets API)."

    try:
        rulesets = json.loads(output)
    except json.JSONDecodeError:
        return None, "Tag protection check: skipped (invalid JSON from rulesets API)."

    tag_rulesets = [
        r["name"]
        for r in rulesets
        if isinstance(r, dict)
        and r.get("target") == "tag"
        and r.get("enforcement") == "active"
    ]
    if tag_rulesets:
        names = ", ".join(tag_rulesets)
        msg = (
            f"Active tag rulesets found: {names}."
            " These may block the create-tag job from pushing release tags."
            " Ensure the REPOMATIC_PAT token is in the bypass list,"
            " or remove the rulesets."
        )
        return msg, msg
    return None, "No active tag rulesets found."


def check_branch_ruleset_on_default(repo: str) -> tuple[bool, str]:
    """Check that at least one active branch ruleset exists.

    Queries the same ``GET /repos/{repo}/rulesets`` endpoint as
    {func}`check_tag_protection_rules` and looks for active rulesets with
    `target == "branch"`. The presence of any such ruleset is taken as
    evidence that the default branch is protected (restrict deletions and
    block force pushes).

    ```{note}

    This is a heuristic: it does not verify the ruleset targets the
    default branch specifically, nor that it enables the exact rules
    recommended by the setup guide. A deeper check would require
    fetching each ruleset's conditions via
    ``GET /repos/{repo}/rulesets/{id}``, adding N+1 API calls.
    ```

    :param repo: Repository in 'owner/repo' format.
    :return: Tuple of (passed, message).
    """
    try:
        output = run_gh_command([
            "api",
            f"repos/{repo}/rulesets",
            "--method",
            "GET",
            "-f",
            "includes_parents=true",
        ])
    except RuntimeError:
        return False, "Branch ruleset check: skipped (could not query rulesets API)."

    try:
        rulesets = json.loads(output)
    except json.JSONDecodeError:
        return False, "Branch ruleset check: skipped (invalid JSON from rulesets API)."

    branch_rulesets = [
        r["name"]
        for r in rulesets
        if isinstance(r, dict)
        and r.get("target") == "branch"
        and r.get("enforcement") == "active"
    ]
    if branch_rulesets:
        names = ", ".join(branch_rulesets)
        return True, f"Active branch rulesets found: {names}."
    return False, "No active branch rulesets found protecting the default branch."


def check_immutable_releases(repo: str) -> tuple[bool | None, str]:
    """Check that immutable releases are enabled for the repository.

    Queries ``GET /repos/{repo}/immutable-releases`` and inspects the
    `enabled` field in the response.

    ```{note}

    This endpoint requires the "Administration: Read-only" permission on
    fine-grained PATs. The `REPOMATIC_PAT` does not include this scope
    (too broad), so the check returns `None` when the API call fails,
    signaling that the result is indeterminate rather than negative.
    ```

    :param repo: Repository in 'owner/repo' format.
    :return: Tuple of (passed_or_None, message). `None` means the check
        could not run (API inaccessible or unparsable).
    """
    try:
        output = run_gh_command(["api", f"repos/{repo}/immutable-releases"])
    except RuntimeError:
        return (
            None,
            "Immutable releases check: skipped (could not query API).",
        )

    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        return (
            None,
            "Immutable releases check: skipped (invalid JSON from API).",
        )

    if data.get("enabled"):
        return True, "Immutable releases are enabled."
    return False, "Immutable releases are not enabled."


def check_pages_deployment_source(repo: str) -> tuple[bool | None, str]:
    """Check that GitHub Pages is deployed via GitHub Actions, not a branch.

    The `docs.yaml` workflow uses `actions/upload-pages-artifact` and
    `actions/deploy-pages`, which require the Pages source to be set to
    **GitHub Actions** in the repository settings. Branch-based deployment
    (`legacy`) is incompatible.

    Queries ``GET /repos/{repo}/pages` and inspects the `build_type``
    field in the response.

    ```{note}

    A 404 means Pages is not configured at all. This is treated as
    indeterminate (`None`) rather than a failure, because the repo
    may not have deployed docs yet.
    ```

    :param repo: Repository in 'owner/repo' format.
    :return: Tuple of (passed_or_None, message). `None` means the check
        could not run (Pages not configured, or API inaccessible).
    """
    try:
        output = run_gh_command(["api", f"repos/{repo}/pages"])
    except RuntimeError:
        return (
            None,
            "Pages deployment source check: skipped (Pages not configured or API"
            " inaccessible).",
        )

    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        return (
            None,
            "Pages deployment source check: skipped (invalid JSON from API).",
        )

    build_type = data.get("build_type")
    if build_type == "workflow":
        return True, "GitHub Pages deployment source is set to GitHub Actions."
    if build_type == "legacy":
        msg = (
            "GitHub Pages deployment source is set to 'Deploy from a branch'."
            " Change it to 'GitHub Actions' under"
            f" https://github.com/{repo}/settings/pages"
            " so the docs.yaml workflow can deploy."
        )
        return False, msg
    return (
        None,
        f"Pages deployment source check: skipped (unknown build_type '{build_type}').",
    )


def check_pypi_trusted_publisher(
    repo: str,
    package_name: str | None,
) -> tuple[bool | None, str]:
    """Check that the PyPI Trusted Publisher entry is registered for this repo.

    PyPI's Trusted Publisher settings are owner-only at
    ``/manage/project/<name>/settings/publishing/`` and not exposed through
    any public API. The only public surface where the OIDC publisher is
    observable is the PEP 740 provenance attached to releases uploaded via
    OIDC: see :func:`repomatic.pypi.get_trusted_publishers`.
    This check probes the latest release's provenance and looks for a
    bundle whose ``repository`` matches ``repo`` and whose ``workflow``
    is :data:`PYPI_TRUSTED_PUBLISHER_WORKFLOW`.
    A match means the publisher is wired up and a previous release uploaded
    successfully through it.
    A mismatch (provenance exists but names a different repo or workflow)
    is a misconfiguration: typical cause is registering the upstream
    reusable workflow instead of the downstream caller's ``release.yaml``,
    which fails on the first upload after migration.
    Indeterminate (``None``) covers two cases that look identical from the
    outside: no published release yet, and provenance missing because past
    releases were uploaded via API token. In both cases the setup guide
    nags until the next OIDC-attested upload appears.

    :param repo: Repository in ``"owner/repo"`` format.
    :param package_name: PyPI package name. The check is skipped when not
        provided.
    :return: Tuple of ``(passed_or_None, message)``.
    """
    if not package_name:
        return None, ("PyPI Trusted Publisher check: skipped (no package name).")

    latest = get_latest_release_file(package_name)
    if latest is None:
        return None, (
            f"PyPI Trusted Publisher check: skipped (no released version of"
            f" '{package_name}' on PyPI yet)."
        )
    version, filename = latest

    publishers = get_trusted_publishers(package_name, version, filename)
    if publishers is None:
        return None, (
            f"PyPI Trusted Publisher check: skipped (no provenance for"
            f" '{package_name}' {version}; previous release likely uploaded"
            f" via API token)."
        )

    if not publishers:
        return None, (
            f"PyPI Trusted Publisher check: skipped (provenance for"
            f" '{package_name}' {version} contains no publisher bundles)."
        )

    for publisher in publishers:
        if (
            publisher.kind == "GitHub"
            and publisher.repository == repo
            and publisher.workflow == PYPI_TRUSTED_PUBLISHER_WORKFLOW
        ):
            return True, (
                f"PyPI Trusted Publisher matches: {publisher.repository}"
                f" via {publisher.workflow}."
            )

    observed = ", ".join(f"{p.repository}:{p.workflow}" for p in publishers)
    owner, _, repository = repo.partition("/")
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
    return False, msg


def check_stale_gh_pages_branch(repo: str) -> tuple[bool | None, str]:
    """Check for a leftover `gh-pages` branch after switching to GitHub Actions.

    When Pages is deployed via GitHub Actions, the `gh-pages` branch is no
    longer needed and should be deleted to avoid confusion.

    :param repo: Repository in 'owner/repo' format.
    :return: Tuple of (passed_or_None, message).
    """
    try:
        run_gh_command(["api", f"repos/{repo}/branches/gh-pages"])
    except RuntimeError:
        # 404: branch doesn't exist. That's the desired state.
        return True, "No stale gh-pages branch found."

    msg = (
        "Stale `gh-pages` branch detected. Pages is deployed via GitHub"
        " Actions, so this branch is no longer needed. Delete it with:"
        f" `gh api --method DELETE repos/{repo}/git/refs/heads/gh-pages`"
    )
    return False, msg


def check_workflow_permissions() -> list[tuple[str | None, str]]:
    """Check that workflows with custom jobs declare ``permissions: {}``.

    Thin-caller workflows (all jobs use `uses:` to call a reusable workflow)
    inherit permissions from the called workflow and do not need a top-level
    `permissions` key. Workflows that define their own `steps:` should
    declare ``permissions: {}`` to follow the principle of least privilege.

    :return: List of (warning_message or None, info_message) tuples.
    """
    results: list[tuple[str | None, str]] = []
    workflows_dir = Path(".github/workflows")
    if not workflows_dir.is_dir():
        return [(None, "Workflow permissions check: skipped (no .github/workflows/)")]

    for wf_path in sorted(workflows_dir.glob("*.yaml")):
        try:
            data = yaml.safe_load(wf_path.read_text(encoding="utf-8"))
        except (yaml.YAMLError, OSError) as e:
            logging.warning(f"Could not parse {wf_path}: {e}")
            continue

        if not isinstance(data, dict) or "jobs" not in data:
            continue

        # Determine if this is a thin-caller workflow (all jobs use reusable
        # workflows via "uses:" with no "steps:").
        jobs = data.get("jobs", {})
        has_custom_steps = any(
            "steps" in job for job in jobs.values() if isinstance(job, dict)
        )
        if not has_custom_steps:
            continue

        if "permissions" not in data:
            msg = (
                f"Workflow {wf_path.name} defines custom job steps but has no"
                " top-level `permissions` key. Add `permissions: {{}}` for"
                " least-privilege security."
            )
            results.append((msg, msg))
        else:
            results.append((None, f"Workflow {wf_path.name}: permissions declared."))

    if not results:
        results.append((
            None,
            "Workflow permissions check: no custom-step workflows found.",
        ))
    return results


def check_test_matrix_excludes() -> list[tuple[str | None, str]]:
    """Flag `[tool.repomatic.test-matrix] exclude` entries that match no axis.

    An exclude naming a value absent from every matrix axis (like a renamed
    runner) can never match a combination, so `Matrix.prune()` drops it
    silently and its exclusion intent is lost. Reporting it as a warning makes
    the drift visible in CI instead of silently weakening the matrix.

    :return: List of (warning_message or None, info_message) tuples.
    """
    metadata = Metadata()
    if not metadata.config.test_matrix.exclude:
        return [(None, "Test matrix excludes check: skipped (none configured).")]

    axes = metadata.test_matrix.all_variations()
    stale = metadata.stale_test_matrix_excludes
    if not stale:
        return [(None, "Test matrix excludes check: all entries match a live axis.")]

    results: list[tuple[str | None, str]] = []
    for entry in stale:
        bad = stale_axis_values(entry, axes)
        msg = (
            f"Test matrix exclude {entry} references values absent from the "
            f"matrix axes ({bad}); it is silently dropped and never takes "
            "effect. Update it (for instance after an upstream runner rename) "
            "or remove it."
        )
        results.append((msg, msg))
    return results


def check_inline_pins_match_upstream(
    workflow_dir: Path = Path(".github/workflows"),
    upstream_repo: str = DEFAULT_REPO,
) -> tuple[str | None, str]:
    """Check inline upstream pins match the workflow `uses:` ref version.

    A workflow that pins the upstream toolkit in a `run:` shell command (like
    ``uvx 'repomatic==1.2.3' metadata``) must keep that version in lockstep with
    the SHA-pinned `uses:` refs. A manual workflow sync bumps the refs but not
    the inline pin, and `sync-workflow-pins` only realigns it on its next
    scheduled run, so the pin can lag in between. When the stale version drops
    a symbol the newer refs rely on, the metadata job fails and a release can
    publish to PyPI yet never tag (the toolkit chicken-and-egg). Flag the
    drift so the lint fails before a release does.

    :param workflow_dir: Directory holding the workflow YAML files.
    :param upstream_repo: Upstream ``owner/repo``; its name is the inline
        package to match (e.g. ``repomatic``).
    :return: ``(error_message_or_None, info_message)``.
    """
    package = upstream_repo.rsplit("/", 1)[-1]
    if not workflow_dir.is_dir():
        return None, f"Inline {package} pin check: skipped (no {workflow_dir})."

    pin_re = re.compile(rf"\b{re.escape(package)}==(?P<version>[0-9]+(?:\.[0-9]+)*)")

    upstream_versions: set[str] = set()
    pins_by_file: dict[str, set[str]] = {}
    for wf in sorted(workflow_dir.glob("*.yaml")):
        try:
            content = wf.read_text(encoding="UTF-8")
        except OSError:
            continue
        upstream_versions.update(find_upstream_ref_versions(content, upstream_repo))
        found = {m["version"] for m in pin_re.finditer(content)}
        if found:
            pins_by_file[wf.name] = found

    if not upstream_versions or not pins_by_file:
        return None, f"Inline {package} pin check: nothing to compare."

    lagging = {
        name: versions
        for name, versions in pins_by_file.items()
        if not versions <= upstream_versions
    }
    expected = ", ".join(sorted(upstream_versions))
    if lagging:
        detail = "; ".join(
            f"{name} pins {', '.join(sorted(v))}" for name, v in sorted(lagging.items())
        )
        msg = f"Inline {package} pin lags the uses: ref version ({expected}): {detail}."
        return msg, msg
    return None, f"Inline {package} pins match the uses: refs ({expected})."


def run_repo_lint(
    package_name: str | None = None,
    repo_name: str | None = None,
    is_sphinx: bool = False,
    project_description: str | None = None,
    keywords: list[str] | None = None,
    repo: str | None = None,
    has_pat: bool = False,
    has_virustotal_key: bool = False,
    nuitka_active: bool = False,
    has_notifications_pat: bool = False,
    unsubscribe_active: bool = False,
) -> int:
    """Run all repository lint checks.

    Emits GitHub Actions annotations for each check result.

    :param package_name: The Python package name.
    :param repo_name: The repository name.
    :param is_sphinx: Whether the project uses Sphinx documentation.
    :param project_description: Description from pyproject.toml.
    :param keywords: Keywords list from pyproject.toml.
    :param repo: Repository in 'owner/repo' format.
    :param has_pat: Whether `GH_TOKEN` contains `REPOMATIC_PAT`.
    :param has_virustotal_key: Whether `VIRUSTOTAL_API_KEY` is configured.
    :param has_notifications_pat: Whether `REPOMATIC_NOTIFICATIONS_PAT` is
        configured.
    :param unsubscribe_active: Whether the unsubscribe workflow is opted in
        via `notification.unsubscribe`.
    :return: Exit code (0 for success, 1 for errors).
    """
    fatal_error = False

    # Fetch repo metadata once if we need it.
    repo_metadata: dict[str, str | None] | None = None
    if is_sphinx or project_description:
        if repo:
            repo_metadata = get_repo_metadata(repo)
        else:
            logging.warning("No repo specified, skipping API-based checks.")
            repo_metadata = {"homepageUrl": None, "description": None}

    # Check 1: Package name vs repo name.
    if package_name and repo_name:
        warning, msg = check_package_name_vs_repo(package_name, repo_name)
        if warning:
            emit_annotation(AnnotationLevel.WARNING, warning)
        print(f"{'⚠' if warning else '✓'} {msg}")

    # Check 2: Website for Sphinx projects.
    if is_sphinx:
        homepage_url = repo_metadata.get("homepageUrl") if repo_metadata else None
        warning, msg = check_website_for_sphinx(repo or "", is_sphinx, homepage_url)
        if warning:
            emit_annotation(AnnotationLevel.WARNING, warning)
        print(f"{'⚠' if warning else '✓'} {msg}")

    # Check 3: Pages deployment source (Sphinx projects only).
    if is_sphinx and repo:
        passed, msg = check_pages_deployment_source(repo)
        if passed is False:
            emit_annotation(AnnotationLevel.WARNING, msg)
            print(f"⚠ {msg}")
        elif passed is True:
            print(f"✓ {msg}")
        else:
            print(f"ℹ {msg}")

    # Check 3b: Stale gh-pages branch (Sphinx projects only).
    if is_sphinx and repo:
        passed, msg = check_stale_gh_pages_branch(repo)
        if passed is False:
            emit_annotation(AnnotationLevel.WARNING, msg)
            print(f"⚠ {msg}")
        elif passed is True:
            print(f"✓ {msg}")

    # Check 4: Description matches (fatal).
    if project_description:
        repo_description = repo_metadata.get("description") if repo_metadata else None
        error, msg = check_description_matches(
            repo or "", project_description, repo_description
        )
        if error:
            emit_annotation(AnnotationLevel.ERROR, error)
            fatal_error = True
        print(f"{'✗' if error else '✓'} {msg}")

    # Check 5: GitHub topics are a subset of pyproject.toml keywords.
    if keywords and repo:
        warning, msg = check_topics_subset_of_keywords(repo, keywords)
        if warning:
            emit_annotation(AnnotationLevel.WARNING, warning)
        print(f"{'⚠' if warning else '✓'} {msg}")

    # Check 6: Funding file present when owner has GitHub Sponsors.
    if repo:
        warning, msg = check_funding_file(repo)
        if warning:
            emit_annotation(AnnotationLevel.WARNING, warning)
        print(f"{'⚠' if warning else '✓'} {msg}")

    # Check 7: Stale draft releases (warning).
    if repo:
        warning, msg = check_stale_draft_releases(repo)
        if warning:
            emit_annotation(AnnotationLevel.WARNING, warning)
        print(f"{'⚠' if warning else '✓'} {msg}")

    # Check 8: Tag protection rules (warning).
    if repo:
        warning, msg = check_tag_protection_rules(repo)
        if warning:
            emit_annotation(AnnotationLevel.WARNING, warning)
        print(f"{'⚠' if warning else '✓'} {msg}")

    # Check 9: Fork PR approval policy strict enough (warning).
    if repo:
        passed, msg = check_fork_pr_approval_policy(repo)
        if passed is False:
            emit_annotation(AnnotationLevel.WARNING, msg)
            print(f"⚠ {msg}")
        elif passed is True:
            print(f"✓ {msg}")
        else:
            print(f"ℹ {msg}")

    # Check 9b: PyPI Trusted Publisher entry registered (warning).
    if repo and package_name:
        passed, msg = check_pypi_trusted_publisher(repo, package_name)
        if passed is False:
            emit_annotation(AnnotationLevel.WARNING, msg)
            print(f"⚠ {msg}")
        elif passed is True:
            print(f"✓ {msg}")
        else:
            print(f"ℹ {msg}")

    # Check 10: Workflow permissions declared on custom-step workflows.
    for warning, msg in check_workflow_permissions():
        if warning:
            emit_annotation(AnnotationLevel.WARNING, warning)
        print(f"{'⚠' if warning else '✓'} {msg}")

    # Check 10b: Test matrix excludes reference values present in a live axis.
    for warning, msg in check_test_matrix_excludes():
        if warning:
            emit_annotation(AnnotationLevel.WARNING, warning)
        print(f"{'⚠' if warning else '✓'} {msg}")

    # Check 10c: Inline upstream pins match the workflow uses: ref version (error).
    error, msg = check_inline_pins_match_upstream()
    if error:
        emit_annotation(AnnotationLevel.ERROR, error)
        fatal_error = True
    print(f"{'✗' if error else '✓'} {msg}")

    # Check 11: VIRUSTOTAL_API_KEY secret (warning, only when Nuitka builds are active).
    if nuitka_active:
        if has_virustotal_key:
            print("✓ VIRUSTOTAL_API_KEY secret is configured.")
        else:
            vt_msg = (
                "VIRUSTOTAL_API_KEY secret is not configured."
                " Release binaries will not be submitted to VirusTotal."
                " Get a free API key at https://www.virustotal.com/gui/my-apikey"
                " and add it as a repository secret."
            )
            emit_annotation(AnnotationLevel.WARNING, vt_msg)
            print(f"⚠ {vt_msg}")

    # Check 12: REPOMATIC_NOTIFICATIONS_PAT secret (warning, only when the
    # unsubscribe workflow is opted in via notification.unsubscribe).
    if unsubscribe_active:
        if has_notifications_pat:
            print("✓ REPOMATIC_NOTIFICATIONS_PAT secret is configured.")
        else:
            notif_msg = (
                "REPOMATIC_NOTIFICATIONS_PAT secret is not configured."
                " The unsubscribe workflow will skip silently."
                " Create a classic PAT with the notifications scope at"
                " https://github.com/settings/tokens/new"
                "?description=REPOMATIC_NOTIFICATIONS_PAT&scopes=notifications"
                " and add it as a repository secret."
            )
            emit_annotation(AnnotationLevel.WARNING, notif_msg)
            print(f"⚠ {notif_msg}")

    # PAT capability checks (only when REPOMATIC_PAT is configured).
    if not has_pat or not repo:
        if not has_pat:
            print("ℹ PAT capability checks: skipped (no REPOMATIC_PAT)")
        return 1 if fatal_error else 0

    results = check_all_pat_permissions(repo)

    for passed, msg in results.iter_results():
        if passed:
            print(f"✓ {msg}")
        else:
            emit_annotation(AnnotationLevel.ERROR, msg)
            print(f"✗ {msg}")
            fatal_error = True

    # Check PAT repository scope (warning, not fatal).
    warning, msg = check_pat_repository_scope(repo)
    if warning:
        emit_annotation(AnnotationLevel.WARNING, warning)
    print(f"{'⚠' if warning else '✓'} {msg}")

    # Check for the dropped Commit statuses permission (warning, not fatal).
    warning, msg = check_pat_stale_statuses_permission(repo)
    if warning:
        emit_annotation(AnnotationLevel.WARNING, warning)
    print(f"{'⚠' if warning else '✓'} {msg}")

    return 1 if fatal_error else 0
