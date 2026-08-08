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

Every check returns a {class}`CheckResult`, whose tri-state `passed` flag
distinguishes success, failure, and skipped/indeterminate outcomes uniformly.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import NamedTuple

import yaml
from click_extra import echo
from packaging.version import Version

from .frontmatter import split_frontmatter
from .github.actions import NULL_SHA, AnnotationLevel, emit_annotation
from .github.gh import gh_api_json, run_gh_command
from .github.matrix import stale_axis_values
from .github.token import check_all_pat_permissions
from .matrix_axes import (
    TEST_RUNNERS_FULL,
    TEST_RUNNERS_PR,
    UNSTABLE_PYTHON_VERSIONS,
)
from .metadata import Dialect, Metadata
from .pypi import (
    PYPI_TRUSTED_PUBLISHER_WORKFLOW,
    get_latest_release_file,
    get_trusted_publishers,
    pypi_trusted_publisher_settings_url,
)
from .registry import DEFAULT_REPO, WORKFLOW_TARGET_ROOT
from .version_sync import find_upstream_ref_versions

WORKFLOW_DIR = Path(WORKFLOW_TARGET_ROOT)
"""Directory every workflow check walks.

Derived from the registry constant `repomatic init` deploys against, so the
checks and the generator can never disagree about where a workflow lives.
"""

PR_TEMPLATE_DIR = Path(".github/pr-templates")
"""Canonical home for a repository's own `pr-body --template-file` templates.

`.github/` already namespaces by subdirectory (`ISSUE_TEMPLATE/`, `workflows/`,
`actions/`), and a dedicated one leaves each template's basename free to carry
the operation name, so it can match its job ID and PR branch. Templates sitting
flat in `.github/` need a `pr-` prefix purely to disambiguate, which breaks that
identity and puts them next to GitHub's own `pull_request_template.md`, an
unrelated human-facing file.
"""

PYTHON_CLASSIFIER_PREFIX = "Programming Language :: Python :: "
"""Prefix of the classifiers naming a supported interpreter version.

Only the dotted ones carry a version: the bare `3` and `3 :: Only` state the
major series, and `Implementation :: CPython` the interpreter.
"""

KNOWN_RUNNERS = frozenset(TEST_RUNNERS_FULL) | frozenset(TEST_RUNNERS_PR)
"""Every runner image the test matrix axes draw from.

The closest thing to a curated list of images a project should be running on,
and the one place carrying measured guidance on their relative speed and cost.
A job naming something outside it has been picked without that guidance.
"""

TEMPLATE_FILE_ARG_RE = re.compile(r"--template-file[=\s]+(?P<path>\S+)")
"""A `repomatic pr-body --template-file` argument inside a workflow `run:` block.

Matched against the raw YAML text rather than the parsed document: the argument
sits inside a folded scalar, so the surrounding `run:` value is one opaque
string whichever way the file is parsed.
"""


class CheckResult(NamedTuple):
    """Outcome of one repository check.

    `passed` is tri-state: `True` on success, `False` on failure, `None`
    when the check could not run or does not apply (skipped). `message` is
    the human-readable line for both terminal output and annotations.
    """

    passed: bool | None
    message: str


def _fetch_rulesets(repo: str) -> list[dict] | None:
    """Fetch the repository's rulesets, parents included.

    One endpoint serves two checks ({func}`check_tag_protection_rules` and
    {func}`check_branch_ruleset_on_default`), which filter the same payload on
    different `target` values. They still call it once each: their skip verdicts
    differ (`None` vs `False`), and sharing a single fetch would mean threading
    the payload through {func}`run_repo_lint`, which calls each check
    independently.

    :param repo: Repository in 'owner/repo' format.
    :return: The ruleset list, or `None` when the API could not be read.
    """
    rulesets = gh_api_json([
        "api",
        f"repos/{repo}/rulesets",
        "--method",
        "GET",
        "-f",
        "includes_parents=true",
    ])
    return rulesets if isinstance(rulesets, list) else None


def get_repo_metadata(repo: str) -> dict[str, str | None]:
    """Fetch repository metadata from GitHub API.

    :param repo: Repository in 'owner/repo' format.
    :return: Dictionary with 'homepageUrl' and 'description' keys. Both are
        `None` when the repository could not be read.
    """
    data = gh_api_json(["repo", "view", repo, "--json", "homepageUrl,description"])
    if data is None:
        logging.error(f"Failed to fetch metadata for {repo}.")
        return {"homepageUrl": None, "description": None}
    return {
        "homepageUrl": data.get("homepageUrl") or None,
        "description": data.get("description") or None,
    }


def check_package_name_vs_repo(package_name: str | None, repo_name: str) -> CheckResult:
    """Check if package name matches repository name.

    :param package_name: The Python package name.
    :param repo_name: The repository name.
    :return: A `CheckResult`.
    """
    if not package_name:
        return CheckResult(
            None, "Package name check: skipped (no package name provided)"
        )

    if package_name != repo_name:
        msg = (
            f"Package name '{package_name}' differs from repository name '{repo_name}'."
        )
        return CheckResult(False, msg)
    return CheckResult(True, f"Package name '{package_name}' matches repository name.")


def check_website_for_sphinx(
    repo: str, is_sphinx: bool, homepage_url: str | None = None
) -> CheckResult:
    """Check that Sphinx projects have a website set.

    :param repo: Repository in 'owner/repo' format.
    :param is_sphinx: Whether the project uses Sphinx documentation.
    :param homepage_url: The homepage URL from API (to avoid duplicate calls).
    :return: A `CheckResult`.
    """
    if not is_sphinx:
        return CheckResult(None, "Website check: skipped (not a Sphinx project)")

    if homepage_url is None:
        metadata = get_repo_metadata(repo)
        homepage_url = metadata.get("homepageUrl")

    if not homepage_url:
        msg = "Sphinx documentation detected but repository website field is not set."
        return CheckResult(False, msg)
    return CheckResult(True, f"Website field is set: {homepage_url}")


def check_description_matches(
    repo: str,
    project_description: str | None,
    repo_description: str | None = None,
) -> CheckResult:
    """Check that repository description matches project description.

    :param repo: Repository in 'owner/repo' format.
    :param project_description: Description from pyproject.toml.
    :param repo_description: Description from API (to avoid duplicate calls).
    :return: A `CheckResult`.
    """
    if not project_description:
        return CheckResult(
            None, "Description check: skipped (no project description provided)"
        )

    if repo_description is None:
        metadata = get_repo_metadata(repo)
        repo_description = metadata.get("description")

    if project_description != repo_description:
        msg = (
            f"Repo description '{repo_description}' != "
            f"project description '{project_description}'."
        )
        return CheckResult(False, msg)
    return CheckResult(True, "Repository description matches project description.")


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


def check_funding_file(repo: str) -> CheckResult:
    """Check that repos with GitHub Sponsors have a `FUNDING.yml`.

    Skips forks (they inherit the parent's sponsor button) and owners
    without a Sponsors listing. Uses the GraphQL API because the REST API
    does not expose `hasSponsorsListing`.

    :param repo: Repository in 'owner/repo' format.
    :return: A `CheckResult`.
    """
    if _funding_file_exists():
        return CheckResult(True, "Funding file found.")

    owner, name = repo.split("/", 1)

    # Single GraphQL query for both isFork and hasSponsorsListing.
    query = (
        f"{{ repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) {{ isFork }}"
        f" repositoryOwner(login: {json.dumps(owner)}) {{"
        " ... on Sponsorable { hasSponsorsListing } } }"
    )

    data = gh_api_json(["api", "graphql", "--field", f"query={query}"])
    if data is None:
        return CheckResult(None, "Funding check: skipped (could not query GitHub API)")

    repo_data = data.get("data", {}).get("repository", {})
    owner_data = data.get("data", {}).get("repositoryOwner", {})

    if repo_data.get("isFork"):
        return CheckResult(None, "Funding check: skipped (repository is a fork)")

    if not owner_data.get("hasSponsorsListing"):
        return CheckResult(
            None, "Funding check: skipped (owner has no GitHub Sponsors listing)"
        )

    msg = (
        "Owner has GitHub Sponsors enabled but .github/FUNDING.yml is missing."
        " Create it to display the Sponsor button on the repository."
    )
    return CheckResult(False, msg)


def check_stale_draft_releases(repo: str) -> CheckResult:
    """Check for draft releases that are not dev pre-releases.

    Draft releases whose tag does not end with `.dev0` are likely
    leftovers from abandoned or failed release attempts. The only
    expected drafts are the rolling dev pre-releases managed by
    `sync-dev-release`.

    :param repo: Repository in 'owner/repo' format.
    :return: A `CheckResult`.
    """
    releases = gh_api_json([
        "release",
        "list",
        "--json",
        "tagName,isDraft",
        "--repo",
        repo,
    ])
    if releases is None:
        return CheckResult(
            None, "Stale draft releases check: skipped (could not query API)."
        )

    stale_drafts = [
        r["tagName"]
        for r in releases
        if r.get("isDraft") and not r["tagName"].endswith(".dev0")
    ]
    if stale_drafts:
        tags = ", ".join(stale_drafts)
        msg = f"Stale draft releases found: {tags}. Delete these leftover drafts."
        return CheckResult(False, msg)
    return CheckResult(True, "No stale draft releases.")


def check_topics_subset_of_keywords(
    repo: str,
    keywords: list[str] | None = None,
) -> CheckResult:
    """Check that GitHub repo topics are a subset of pyproject.toml keywords.

    :param repo: Repository in 'owner/repo' format.
    :param keywords: Keywords from pyproject.toml. If `None`, check is skipped.
    :return: A `CheckResult`.
    """
    if not keywords:
        return CheckResult(
            None, "Topics check: skipped (no keywords in pyproject.toml)"
        )

    try:
        output = run_gh_command(["api", f"repos/{repo}", "--jq", ".topics[]"])
    except RuntimeError as e:
        logging.warning(f"Could not fetch GitHub topics: {e}")
        return CheckResult(
            None, "Topics check: skipped (could not fetch GitHub topics)"
        )

    topics = {t.strip() for t in output.splitlines() if t.strip()}
    if not topics:
        return CheckResult(None, "Topics check: skipped (no GitHub topics set)")

    extra = sorted(topics - set(keywords))
    if extra:
        msg = (
            f"GitHub topics not in pyproject.toml keywords: {', '.join(extra)}. "
            "Add them to [project] keywords or remove from repo topics."
        )
        return CheckResult(False, msg)
    return CheckResult(
        True, f"All {len(topics)} GitHub topics are in pyproject.toml keywords."
    )


def check_pat_repository_scope(repo: str) -> CheckResult:
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
    :return: A `CheckResult`.
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
            return CheckResult(False, msg)
        return CheckResult(
            True, "PAT scope: correctly limited to selected repositories."
        )

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
        return CheckResult(
            None, "PAT scope check: skipped (could not list owner repos)."
        )

    other_repos = [
        r.strip() for r in output.splitlines() if r.strip() and r.strip() != repo
    ]
    if not other_repos:
        return CheckResult(None, "PAT scope check: skipped (no other repos to probe).")

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
            return CheckResult(False, msg)
    except RuntimeError:
        return CheckResult(None, "PAT scope check: skipped (probe request failed).")

    return CheckResult(
        True, f"PAT scope: no push access to {probe_repo} (correctly scoped)."
    )


def check_pat_stale_statuses_permission(repo: str) -> CheckResult:
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
    :return: A `CheckResult`.
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
            return CheckResult(False, msg)
        if "HTTP 403" in stderr:
            return CheckResult(
                True, "Commit statuses: token correctly lacks the dropped scope."
            )
        return CheckResult(
            None, "Stale Commit statuses check: skipped (indeterminate response)."
        )
    # A 2xx is unreachable: NULL_SHA can never resolve to a commit.
    return CheckResult(
        None, "Stale Commit statuses check: skipped (unexpected success)."
    )


def check_fork_pr_approval_policy(repo: str) -> CheckResult:
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
    `GET /repos/{repo}/actions/permissions/fork-pr-contributor-approval`
    and returns `False` when the policy is weaker than
    `first_time_contributors`.

    ```{note}

    This endpoint requires the `Actions: read` permission. When the
    `REPOMATIC_PAT` lacks it (or the API call fails for any other
    reason), the check returns `None` to signal that the result is
    indeterminate rather than negative.
    ```

    :param repo: Repository in 'owner/repo' format.
    :return: A `CheckResult`. `passed` is `None` when the check could not run
        (API inaccessible, unparsable, or unknown policy).
    """
    data = gh_api_json([
        "api",
        f"repos/{repo}/actions/permissions/fork-pr-contributor-approval",
    ])
    if data is None:
        return CheckResult(
            None, "Fork PR approval policy check: skipped (could not query API)."
        )

    policy = data.get("approval_policy", "")
    if policy in {"first_time_contributors", "all_external_contributors"}:
        return CheckResult(True, f"Fork PR approval policy: {policy}.")

    if policy == "first_time_contributors_new_to_github":
        msg = (
            "Fork PR approval policy is 'first_time_contributors_new_to_github',"
            " which only catches brand-new GitHub accounts."
            " Set it to 'first_time_contributors' (or stricter) under"
            f" https://github.com/{repo}/settings/actions"
            " to require approval for any first-time contributor."
        )
        return CheckResult(False, msg)

    return CheckResult(
        None,
        f"Fork PR approval policy check: skipped (unknown policy '{policy}').",
    )


def check_sha_pinning_required(repo: str) -> CheckResult:
    """Check that GitHub Actions must be pinned to a full-length commit SHA.

    GitHub has a per-repository policy, `sha_pinning_required`, that makes
    the platform itself refuse to run any workflow referencing an action by a
    mutable tag or branch instead of a commit SHA. repomatic already pins
    every action it generates and checks unpinned refs with `zizmor`
    (`check_inline_pins_match_upstream` and the `lint-zizmor` job), but a
    `zizmor` finding can be silenced inline (`# zizmor: ignore[...]`), so a
    hand-edited workflow could still slip a mutable tag past review. This
    repo-level setting is the platform-enforced backstop.

    Queries `GET /repos/{repo}/actions/permissions` and returns `False`
    when `sha_pinning_required` is absent or `false`.

    ```{note}

    This endpoint requires the `Actions: read` permission. When the
    `REPOMATIC_PAT` lacks it (or the API call fails for any other
    reason), the check returns `None` to signal that the result is
    indeterminate rather than negative.
    ```

    :param repo: Repository in 'owner/repo' format.
    :return: A `CheckResult`. `passed` is `None` when the check could not run
        (API inaccessible or unparsable).
    """
    data = gh_api_json(["api", f"repos/{repo}/actions/permissions"])
    if data is None:
        return CheckResult(
            None, "SHA pinning required check: skipped (could not query API)."
        )

    if data.get("sha_pinning_required"):
        return CheckResult(True, "SHA pinning required: enabled.")

    msg = (
        "SHA pinning is not required for GitHub Actions in this repository."
        " Enable it under"
        f" https://github.com/{repo}/settings/actions"
        " (Actions permissions → Require actions to be pinned to a"
        " full-length commit SHA) so GitHub rejects any unpinned action"
        " reference, not just the ones zizmor happens to catch."
    )
    return CheckResult(False, msg)


def check_tag_protection_rules(repo: str) -> CheckResult:
    """Check that no tag rulesets could block the `create-tag` workflow job.

    Tag rulesets that restrict creation or require status checks can prevent
    `REPOMATIC_PAT` (or `GITHUB_TOKEN`) from pushing release tags. This
    check queries the repository rulesets API and warns when any ruleset
    targets tags.

    :param repo: Repository in 'owner/repo' format.
    :return: A `CheckResult`.
    """
    rulesets = _fetch_rulesets(repo)
    if rulesets is None:
        return CheckResult(
            None, "Tag protection check: skipped (could not query rulesets API)."
        )

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
        return CheckResult(False, msg)
    return CheckResult(True, "No active tag rulesets found.")


def check_branch_ruleset_on_default(repo: str) -> CheckResult:
    """Check that at least one active branch ruleset exists.

    Queries the same `GET /repos/{repo}/rulesets` endpoint as
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
    :return: A `CheckResult`. `passed` is `None` when the rulesets API could
        not be read, matching {func}`check_tag_protection_rules`, which reads
        the same payload.
    """
    rulesets = _fetch_rulesets(repo)
    if rulesets is None:
        return CheckResult(
            None, "Branch ruleset check: skipped (could not query rulesets API)."
        )

    branch_rulesets = [
        r["name"]
        for r in rulesets
        if isinstance(r, dict)
        and r.get("target") == "branch"
        and r.get("enforcement") == "active"
    ]
    if branch_rulesets:
        names = ", ".join(branch_rulesets)
        return CheckResult(True, f"Active branch rulesets found: {names}.")
    return CheckResult(
        False, "No active branch rulesets found protecting the default branch."
    )


def check_immutable_releases(repo: str) -> CheckResult:
    """Check that immutable releases are enabled for the repository.

    Queries `GET /repos/{repo}/immutable-releases` and inspects the
    `enabled` field in the response.

    ```{note}

    This endpoint requires the "Administration: Read-only" permission on
    fine-grained PATs. The `REPOMATIC_PAT` does not include this scope
    (too broad), so the check returns `None` when the API call fails,
    signaling that the result is indeterminate rather than negative.
    ```

    :param repo: Repository in 'owner/repo' format.
    :return: A `CheckResult`. `passed` is `None` when the check could not run
        (API inaccessible or unparsable).
    """
    data = gh_api_json(["api", f"repos/{repo}/immutable-releases"])
    if data is None:
        return CheckResult(
            None,
            "Immutable releases check: skipped (could not query API).",
        )

    if data.get("enabled"):
        return CheckResult(True, "Immutable releases are enabled.")
    return CheckResult(False, "Immutable releases are not enabled.")


def check_pages_deployment_source(repo: str) -> CheckResult:
    """Check that GitHub Pages is deployed via GitHub Actions, not a branch.

    The `docs.yaml` workflow uses `actions/upload-pages-artifact` and
    `actions/deploy-pages`, which require the Pages source to be set to
    **GitHub Actions** in the repository settings. Branch-based deployment
    (`legacy`) is incompatible.

    Queries `GET /repos/{repo}/pages` and inspects the `build_type`
    field in the response.

    ```{note}

    A 404 means Pages is not configured at all. This is treated as
    indeterminate (`None`) rather than a failure, because the repo
    may not have deployed docs yet.
    ```

    :param repo: Repository in 'owner/repo' format.
    :return: A `CheckResult`. `passed` is `None` when the check could not run
        (Pages not configured, or API inaccessible).
    """
    data = gh_api_json(["api", f"repos/{repo}/pages"])
    if data is None:
        return CheckResult(
            None,
            "Pages deployment source check: skipped (Pages not configured or API"
            " inaccessible).",
        )

    build_type = data.get("build_type")
    if build_type == "workflow":
        return CheckResult(
            True, "GitHub Pages deployment source is set to GitHub Actions."
        )
    if build_type == "legacy":
        msg = (
            "GitHub Pages deployment source is set to 'Deploy from a branch'."
            " Change it to 'GitHub Actions' under"
            f" https://github.com/{repo}/settings/pages"
            " so the docs.yaml workflow can deploy."
        )
        return CheckResult(False, msg)
    return CheckResult(
        None,
        f"Pages deployment source check: skipped (unknown build_type '{build_type}').",
    )


def check_pypi_trusted_publisher(
    repo: str,
    package_name: str | None,
) -> CheckResult:
    """Check that the PyPI Trusted Publisher entry is registered for this repo.

    PyPI's Trusted Publisher settings are owner-only at
    `/manage/project/<name>/settings/publishing/` and not exposed through
    any public API. The only public surface where the OIDC publisher is
    observable is the PEP 740 provenance attached to releases uploaded via
    OIDC: see {func}`repomatic.pypi.get_trusted_publishers`.
    This check probes the latest release's provenance and looks for a
    bundle whose `repository` matches `repo` and whose `workflow`
    is {data}`PYPI_TRUSTED_PUBLISHER_WORKFLOW`.
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

    :param repo: Repository in `"owner/repo"` format.
    :param package_name: PyPI package name. The check is skipped when not
        provided.
    :return: A `CheckResult`.
    """
    if not package_name:
        return CheckResult(
            None, ("PyPI Trusted Publisher check: skipped (no package name).")
        )

    latest = get_latest_release_file(package_name)
    if latest is None:
        return CheckResult(
            None,
            (
                f"PyPI Trusted Publisher check: skipped (no released version of"
                f" '{package_name}' on PyPI yet)."
            ),
        )
    version, filename = latest

    publishers = get_trusted_publishers(package_name, version, filename)
    if publishers is None:
        return CheckResult(
            None,
            (
                f"PyPI Trusted Publisher check: skipped (no provenance for"
                f" '{package_name}' {version}; previous release likely uploaded"
                f" via API token)."
            ),
        )

    if not publishers:
        return CheckResult(
            None,
            (
                f"PyPI Trusted Publisher check: skipped (provenance for"
                f" '{package_name}' {version} contains no publisher bundles)."
            ),
        )

    for publisher in publishers:
        if (
            publisher.kind == "GitHub"
            and publisher.repository == repo
            and publisher.workflow == PYPI_TRUSTED_PUBLISHER_WORKFLOW
        ):
            return CheckResult(
                True,
                (
                    f"PyPI Trusted Publisher matches: {publisher.repository}"
                    f" via {publisher.workflow}."
                ),
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
    return CheckResult(False, msg)


def check_stale_gh_pages_branch(repo: str) -> CheckResult:
    """Check for a leftover `gh-pages` branch after switching to GitHub Actions.

    When Pages is deployed via GitHub Actions, the `gh-pages` branch is no
    longer needed and should be deleted to avoid confusion.

    :param repo: Repository in 'owner/repo' format.
    :return: A `CheckResult`.
    """
    try:
        run_gh_command(["api", f"repos/{repo}/branches/gh-pages"])
    except RuntimeError:
        # 404: branch doesn't exist. That's the desired state.
        return CheckResult(True, "No stale gh-pages branch found.")

    msg = (
        "Stale `gh-pages` branch detected. Pages is deployed via GitHub"
        " Actions, so this branch is no longer needed. Delete it with:"
        f" `gh api --method DELETE repos/{repo}/git/refs/heads/gh-pages`"
    )
    return CheckResult(False, msg)


def check_workflow_permissions() -> list[CheckResult]:
    """Check workflow `permissions` declarations for least privilege.

    Two failure modes are flagged:

    1. A workflow that defines its own `steps:` should carry a top-level
       `permissions` key (`permissions: {}` for least privilege) so its
       jobs default to no scopes rather than the repository default.
    2. A job that calls a reusable workflow (a job-level `uses:`) hands its
       own permissions *down*, and the reusable workflow's jobs are capped by
       them: they cannot escalate beyond what the caller grants. So under a
       top-level `permissions: {}`, a reusable-call job with no
       `permissions:` block of its own passes `{}` to the called workflow,
       and GitHub aborts the run at startup the moment a nested job requests a
       scope the caller never granted. Such a job must name the union of the
       scopes its reusable workflow needs (mirror the reusable workflow's own
       top-level `{}` plus per-job grants).

    A thin caller with *no* top-level `permissions` key is fine: its jobs
    inherit the repository default, which the reusable workflow's own
    `permissions:` blocks then cap. The failure is specifically an empty
    top-level `permissions: {}` starving an unqualified reusable call.

    :return: A list of `CheckResult`.
    """
    results: list[CheckResult] = []
    if not WORKFLOW_DIR.is_dir():
        return [
            CheckResult(
                None,
                f"Workflow permissions check: skipped (no {WORKFLOW_DIR.as_posix()}/)",
            )
        ]

    for wf_path, data in _load_workflows().items():
        jobs = data["jobs"]
        has_custom_steps = any(
            "steps" in job for job in jobs.values() if isinstance(job, dict)
        )
        # Reusable-workflow-calling jobs (a job-level `uses:`) that would
        # inherit an empty top-level `permissions: {}` because they declare no
        # `permissions:` of their own. `data.get("permissions") == {}` matches
        # only the empty mapping, not an absent key (which yields the repo
        # default) nor a populated one.
        starved_calls = (
            sorted(
                name
                for name, job in jobs.items()
                if isinstance(job, dict)
                and job.get("uses")
                and "permissions" not in job
            )
            if data.get("permissions") == {}
            else []
        )

        # Only workflows subject to one of the two checks are reported on.
        if not has_custom_steps and not starved_calls:
            continue

        failures: list[str] = []
        if has_custom_steps and "permissions" not in data:
            failures.append(
                f"Workflow {wf_path.name} defines custom job steps but has no"
                f" top-level `permissions` key. Add `permissions: {{}}` for"
                f" least-privilege security."
            )
        if starved_calls:
            joined = ", ".join(f"`{name}`" for name in starved_calls)
            failures.append(
                f"Workflow {wf_path.name}: {joined} call a reusable workflow"
                f" under a top-level `permissions: {{}}` without their own"
                f" `permissions:` block, so GitHub rejects the run at startup"
                f" once a nested job requests a scope the caller never granted."
                f" Grant each job the union of the scopes its reusable workflow"
                f" needs."
            )

        if failures:
            results.extend(CheckResult(False, msg) for msg in failures)
        else:
            results.append(
                CheckResult(True, f"Workflow {wf_path.name}: permissions declared.")
            )

    if not results:
        results.append(
            CheckResult(
                None,
                "Workflow permissions check: no custom-step workflows found.",
            )
        )
    return results


def check_test_matrix_excludes() -> list[CheckResult]:
    """Flag `[tool.repomatic.test-matrix] exclude` entries that match no axis.

    An exclude naming a value absent from every matrix axis (like a renamed
    runner) can never match a combination, so `Matrix.prune()` drops it
    silently and its exclusion intent is lost. Reporting it as a warning makes
    the drift visible in CI instead of silently weakening the matrix.

    :return: A list of `CheckResult`.
    """
    metadata = Metadata()
    if not metadata.config.test_matrix.exclude:
        return [
            CheckResult(None, "Test matrix excludes check: skipped (none configured).")
        ]

    axes = metadata.test_matrix.all_variations()
    stale = metadata.stale_test_matrix_excludes
    if not stale:
        return [
            CheckResult(
                True, "Test matrix excludes check: all entries match a live axis."
            )
        ]

    results: list[CheckResult] = []
    for entry in stale:
        bad = stale_axis_values(entry, axes)
        msg = (
            f"Test matrix exclude {entry} references values absent from the "
            f"matrix axes ({bad}); it is silently dropped and never takes "
            "effect. Update it (for instance after an upstream runner rename) "
            "or remove it."
        )
        results.append(CheckResult(False, msg))
    return results


def _workflow_texts(workflow_dir: Path = WORKFLOW_DIR) -> dict[Path, str]:
    """Read every workflow in *workflow_dir*, skipping the unreadable ones.

    The raw-text half of the workflow walk, for the checks that match against
    the file as written rather than as parsed: an argument inside a folded
    scalar is one opaque string to a YAML parser, whichever way the file is
    loaded.

    :param workflow_dir: Directory holding the workflow files.
    :return: A mapping of path to file content, in sorted path order.
    """
    texts: dict[Path, str] = {}
    if not workflow_dir.is_dir():
        return texts
    for path in sorted(workflow_dir.glob("*.yaml")):
        try:
            texts[path] = path.read_text(encoding="UTF-8")
        except OSError as e:
            logging.warning(f"Could not read {path}: {e}")
    return texts


def _load_workflows(workflow_dir: Path = WORKFLOW_DIR) -> dict[Path, dict]:
    """Parse every workflow in *workflow_dir*, skipping the unreadable ones.

    The parsed half of the workflow walk. Documents that declare no `jobs:`
    mapping are dropped, so every consumer can index `data["jobs"]` directly.

    :param workflow_dir: Directory holding the workflow files.
    :return: A mapping of path to parsed document, for documents declaring jobs.
    """
    workflows: dict[Path, dict] = {}
    for path, text in _workflow_texts(workflow_dir).items():
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as e:
            logging.warning(f"Could not parse {path}: {e}")
            continue
        if isinstance(data, dict) and isinstance(data.get("jobs"), dict):
            workflows[path] = data
    return workflows


def _advertised_python_versions(metadata: Metadata) -> list[tuple[int, int]]:
    """`(major, minor)` pairs from the project's Python version classifiers.

    `3` and `3 :: Only` carry no minor version, and `Implementation :: CPython`
    is not a version at all, so only dotted numeric suffixes are kept.

    :param metadata: The repository metadata to read the classifiers from.
    :return: A sorted list of `(major, minor)` pairs, empty when none are set.
    """
    versions: set[tuple[int, int]] = set()
    for classifier in metadata.pyproject.classifiers if metadata.pyproject else []:
        suffix = classifier.removeprefix(PYTHON_CLASSIFIER_PREFIX)
        if suffix == classifier:
            continue
        major, _, minor = suffix.partition(".")
        if major.isdigit() and minor.isdigit():
            versions.add((int(major), int(minor)))
    return sorted(versions)


def _literal_python_axes(workflows: dict[Path, dict]) -> dict[str, list[str]]:
    """Test matrix `python-version` axes spelled out as a literal list.

    A matrix assembled at runtime, as repomatic's own
    `${{ fromJSON(...).test_matrix }}` is, reads back as an opaque string. Those
    are skipped: their axes come from {mod}`repomatic.matrix_axes` and are
    canonical by construction, so there is nothing left to reconcile.

    :param workflows: Parsed workflows, keyed by path.
    :return: A mapping of `<file>:<job>` to the versions that job names.
    """
    axes: dict[str, list[str]] = {}
    for path, data in workflows.items():
        for job_id, job in data["jobs"].items():
            if not isinstance(job, dict):
                continue
            matrix = job.get("strategy", {}).get("matrix")
            if not isinstance(matrix, dict):
                continue
            versions = matrix.get("python-version")
            if isinstance(versions, list) and versions:
                axes[f"{path.name}:{job_id}"] = [str(v) for v in versions]
    return axes


def check_python_version_consistency() -> list[CheckResult]:
    """Reconcile the Python versions a project requires, advertises and tests.

    The same fact is stated in up to three places, and nothing else holds them
    together: the `requires-python` lower bound, the
    `Programming Language :: Python :: X.Y` classifiers PyPI renders, and any
    test matrix naming its versions literally.

    Two failure modes are flagged:

    1. The lowest classifier disagrees with the `requires-python` floor. One of
       the two is then lying to resolvers about what installs.
    2. A literal test matrix does not reach both ends of the advertised range,
       or names a released version the classifiers never claim. Coverage of the
       *ends* is the invariant rather than of every version in between, so that
       a matrix testing the floor, the latest release and the development
       version stays conformant: skipping intermediate releases is a deliberate
       way to cut CI load, advertising an untested boundary is not.

    Versions in {data}`~repomatic.matrix_axes.UNSTABLE_PYTHON_VERSIONS` are
    exempt from the second rule, being tested precisely because they are not
    released yet and so cannot be advertised. Build flavors carrying a suffix
    (the free-threaded `3.14t`) count as their base version.

    :return: A list of `CheckResult`.
    """
    metadata = Metadata()
    advertised = _advertised_python_versions(metadata)
    if not advertised:
        return [
            CheckResult(
                None, "Python version consistency: skipped (no version classifiers)."
            )
        ]

    results: list[CheckResult] = []

    floor = min(advertised)
    required = (
        [s for s in metadata.pyproject.requires_python if s.operator in (">=", ">")]
        if metadata.pyproject and metadata.pyproject.requires_python
        else []
    )
    if not required:
        results.append(
            CheckResult(
                None, "Python floor check: skipped (no requires-python lower bound)."
            )
        )
    else:
        release = Version(required[0].version).release
        declared = (release[0], release[1])
        floor_str = ".".join(map(str, floor))
        if declared != floor:
            results.append(
                CheckResult(
                    False,
                    f"requires-python floor is"
                    f" {'.'.join(map(str, declared))} but the lowest"
                    f" `Programming Language :: Python` classifier is"
                    f" {floor_str}. Resolvers read the first and humans read"
                    f" the second, so they have to agree.",
                )
            )
        else:
            results.append(
                CheckResult(
                    True,
                    f"Python floor check: requires-python and classifiers"
                    f" agree on {floor_str}.",
                )
            )

    axes = _literal_python_axes(_load_workflows())
    if not axes:
        results.append(
            CheckResult(None, "Python test matrix: skipped (no literal axis found).")
        )
        return results

    advertised_labels = {".".join(map(str, v)) for v in advertised}
    boundaries = {
        ".".join(map(str, min(advertised))),
        ".".join(map(str, max(advertised))),
    }
    for location, versions in sorted(axes.items()):
        # A build flavor ("3.14t") exercises its base version's interpreter.
        tested = {v.rstrip("t") for v in versions}
        unadvertised = sorted(
            (tested - advertised_labels) - set(UNSTABLE_PYTHON_VERSIONS)
        )
        missing = sorted(boundaries - tested)
        if unadvertised:
            results.append(
                CheckResult(
                    False,
                    f"{location} tests Python {', '.join(unadvertised)}, which"
                    f" the classifiers do not advertise. Add the classifier, or"
                    f" drop the version from the matrix.",
                )
            )
        if missing:
            results.append(
                CheckResult(
                    False,
                    f"{location} never tests Python {', '.join(missing)}, at the"
                    f" edge of the advertised range. Testing between the ends is"
                    f" optional, reaching them is not.",
                )
            )
        if not unadvertised and not missing:
            results.append(
                CheckResult(True, f"{location}: matrix spans the advertised range.")
            )
    return results


def check_runner_images() -> list[CheckResult]:
    """Flag runner images that move on their own, or that no axis knows about.

    Neither Dependabot nor `sync-workflow-pins` touches a `runs-on:` value:
    the first only rewrites `uses:` references, the second only the
    `uvx '<pkg>==X.Y.Z'` and `npm install pkg@X.Y.Z` literals. So a runner is
    the one dependency in a workflow that nothing bumps, and the only defence
    is keeping the set small and named.

    Two failure modes are flagged:

    1. A `-latest` alias. GitHub repoints those to a new image on its own
       schedule, so the build changes underneath the repository with no commit
       to review, and a breakage arrives unattached to any change.
    2. An image outside the curated axes in {mod}`repomatic.matrix_axes`. Those
       carry measured guidance on speed and cost; an image picked outside them
       is one nobody has weighed, and is usually a leftover.

    Values built from an expression (`${{ matrix.os }}`) name no image here and
    are left alone: the axis they draw from is checked at its definition.

    :return: A list of `CheckResult`.
    """
    workflows = _load_workflows()
    if not workflows:
        return [CheckResult(None, "Runner images check: skipped (no workflows).")]

    seen: dict[str, list[str]] = {}
    for path, data in workflows.items():
        for job_id, job in data["jobs"].items():
            if not isinstance(job, dict) or "steps" not in job:
                # A thin caller's runner is the reusable workflow's business.
                continue
            runner = job.get("runs-on")
            if not isinstance(runner, str) or "${{" in runner:
                continue
            seen.setdefault(runner, []).append(f"{path.name}:{job_id}")

    if not seen:
        return [
            CheckResult(None, "Runner images check: skipped (none named literally).")
        ]

    results: list[CheckResult] = []
    for runner, locations in sorted(seen.items()):
        where = ", ".join(sorted(locations))
        if runner.endswith("-latest"):
            results.append(
                CheckResult(
                    False,
                    f"{where} run on `{runner}`, which GitHub repoints without"
                    f" a commit here. Pin the image instead.",
                )
            )
        elif runner not in KNOWN_RUNNERS:
            known = ", ".join(f"`{r}`" for r in sorted(KNOWN_RUNNERS))
            results.append(
                CheckResult(
                    False,
                    f"{where} run on `{runner}`, which is not one of the images"
                    f" the test matrix axes are drawn from ({known}). Nothing"
                    f" bumps a runner literal, so an image off that list is one"
                    f" nobody is tracking.",
                )
            )
        else:
            results.append(CheckResult(True, f"{where}: runner `{runner}` is known."))
    return results


class ReleaseGate(NamedTuple):
    """A release-only step or job, and the project capability it needs.

    `metadata_key` names the {class}`~repomatic.metadata.Metadata` field that
    both decides whether the step runs and supplies what it consumes. `None`
    marks a step that needs nothing beyond being on a release commit.
    """

    name: str
    workflow: str
    metadata_key: str | None
    needs: str


RELEASE_ONLY_GATES: tuple[ReleaseGate, ...] = (
    ReleaseGate(
        "Pre-bake tag SHA",
        "_release-build.yaml",
        "cli_scripts",
        "a `[project.scripts]` entry, since click-extra's prebake finds the"
        " module to stamp through it",
    ),
    ReleaseGate(
        "📌 Tag release",
        "_release-engine.yaml",
        None,
        "nothing beyond the release commit",
    ),
    ReleaseGate(
        # Lives in the caller, not the engine, and reaches its capability gate
        # transitively: the build lane only sets `package_built` when
        # `build-package` ran, and that job is the one gated on
        # `is_python_project`. So there is no key of its own to check.
        "🐍 Publish to PyPI",
        "release.yaml",
        None,
        "a wheel from the build lane",
    ),
    ReleaseGate(
        "🐙 Create GitHub release draft",
        "_release-engine.yaml",
        None,
        "nothing beyond the release commit",
    ),
    ReleaseGate(
        "📖 Man pages",
        "_release-engine.yaml",
        "manpages_script",
        "a configured man-page script",
    ),
    ReleaseGate(
        "📎 Extra release assets",
        "_release-engine.yaml",
        "release_assets",
        "at least one configured extra asset",
    ),
    ReleaseGate(
        "🎉 Publish GitHub release",
        "_release-engine.yaml",
        None,
        "nothing beyond the release commit",
    ),
)
"""Every step and job that runs only on a release commit.

These are invisible on an ordinary push: each is gated behind a condition that
holds open only for the one commit that tags, publishes and releases. A project
can therefore build green for its entire life and meet them for the first time
on release day, where a failure costs a reverted release rather than a red push.

`VirusTotal scan` is deliberately absent: it gates on a repository secret rather
than a project capability, so there is nothing in the tree to check it against.
"""


def check_release_path() -> list[CheckResult]:
    """Resolve the release path against this project, on an ordinary push.

    Two arms, because the two failure modes live in different repositories.

    The first runs everywhere, including downstream. It resolves each entry of
    {data}`RELEASE_ONLY_GATES` against the local project and reports which
    release-only steps a release commit would actually run. That turns a surface
    nothing exercises until release day into a line of output on every push, so
    the answer is known long before it is expensive.

    The second runs only where the reusable workflows live, since a downstream
    repository holds a thin caller and not the steps themselves. It asserts each
    gate's `if:` really does test the metadata key its step depends on. That
    invariant is what a release-only step gets wrong: the condition looks
    complete because it correctly waits for a release, while saying nothing
    about the capability the step consumes. `Pre-bake tag SHA` shipped that way,
    gated on the version alone, and every project with no `[project.scripts]`
    built green until the release commit ran prebake against a module that was
    not there.

    :return: A list of `CheckResult`.
    """
    metadata = Metadata()
    if not metadata.is_python_project:
        return [
            CheckResult(None, "Release path check: skipped (not a Python project).")
        ]

    # Resolved through `dump`, not `getattr`: several of these keys are assembled
    # by the dump factories or come from `Config`, so they are not attributes at
    # all and `getattr` would quietly return None for every one of them. Going
    # through `dump` also reads them exactly as the workflow's `fromJSON` does.
    wanted = tuple({g.metadata_key for g in RELEASE_ONLY_GATES if g.metadata_key})
    resolved = json.loads(metadata.dump(Dialect.json, keys=wanted))

    results: list[CheckResult] = []

    for gate in RELEASE_ONLY_GATES:
        if gate.metadata_key is None:
            results.append(CheckResult(True, f"Release path: `{gate.name}` will run."))
            continue
        if gate.metadata_key not in resolved:
            results.append(
                CheckResult(
                    False,
                    f"`{gate.name}` is registered against metadata key"
                    f" `{gate.metadata_key}`, which no longer exists. The"
                    f" workflow gate reading it resolves to empty, so the step"
                    f" silently never runs.",
                )
            )
            continue
        value = resolved[gate.metadata_key]
        verb = "will run" if value else "will be skipped"
        results.append(
            CheckResult(
                True,
                f"Release path: `{gate.name}` {verb} (`{gate.metadata_key}`"
                f" is {'set' if value else 'empty'}; it needs {gate.needs}).",
            )
        )

    # Second arm. Absent the reusable workflows there is nothing to read, which is
    # the normal downstream case rather than a failure.
    workflows = _load_workflows()
    engine = {
        path.name: data
        for path, data in workflows.items()
        if path.name in {gate.workflow for gate in RELEASE_ONLY_GATES}
    }
    if not engine:
        return results

    for gate in RELEASE_ONLY_GATES:
        if gate.metadata_key is None:
            continue
        data = engine.get(gate.workflow)
        if data is None:
            continue
        conditions = _conditions_for(data, gate.name)
        if not conditions:
            results.append(
                CheckResult(
                    False,
                    f"`{gate.name}` is registered as release-only but no step or"
                    f" job of that name carries an `if:` in {gate.workflow}."
                    f" Either the name drifted or the gate was dropped.",
                )
            )
        elif not any(gate.metadata_key in cond for cond in conditions):
            results.append(
                CheckResult(
                    False,
                    f"`{gate.name}` in {gate.workflow} needs {gate.needs}, but its"
                    f" `if:` never tests `{gate.metadata_key}`. It will fire on the"
                    f" release commit of a project that has none, which is the one"
                    f" run no ordinary push rehearses.",
                )
            )
        else:
            results.append(
                CheckResult(
                    True,
                    f"`{gate.name}` gates on `{gate.metadata_key}`.",
                )
            )

    return results


def _conditions_for(workflow: dict, name: str) -> list[str]:
    """Collect the `if:` expressions attached to a named job or step.

    A job is matched on its `name:`, a step on its `name:` too, so the registry
    can address both by the label that shows up in the Actions UI. A job's own
    condition is returned alongside its steps', since either can be where the
    gate lives.

    :param workflow: Parsed workflow document.
    :param name: Job or step name, matched on its rendered prefix so a name
        carrying a `${{ }}` suffix still matches.
    :return: Every `if:` expression found, as strings.
    """
    found: list[str] = []
    for job in workflow.get("jobs", {}).values():
        if not isinstance(job, dict):
            continue
        job_name = job.get("name", "")
        if isinstance(job_name, str) and job_name.startswith(name):
            found.append(str(job.get("if", "")))
        for step in job.get("steps", []) or ():
            if not isinstance(step, dict):
                continue
            if step.get("name") == name:
                found.append(str(step.get("if", "")))
    return [cond for cond in found if cond]


def check_inline_pins_match_upstream(
    workflow_dir: Path = WORKFLOW_DIR,
    upstream_repo: str = DEFAULT_REPO,
) -> CheckResult:
    """Check inline upstream pins match the workflow `uses:` ref version.

    A workflow that pins the upstream toolkit in a `run:` shell command (like
    `uvx 'repomatic==1.2.3' metadata`) must keep that version in lockstep with
    the SHA-pinned `uses:` refs. A manual workflow sync bumps the refs but not
    the inline pin, and `sync-workflow-pins` only realigns it on its next
    scheduled run, so the pin can lag in between. When the stale version drops
    a symbol the newer refs rely on, the metadata job fails and a release can
    publish to PyPI yet never tag (the toolkit chicken-and-egg). Flag the
    drift so the lint fails before a release does.

    :param workflow_dir: Directory holding the workflow YAML files.
    :param upstream_repo: Upstream `owner/repo`; its name is the inline
        package to match (like `repomatic`).
    :return: A `CheckResult`.
    """
    package = upstream_repo.rsplit("/", 1)[-1]
    if not workflow_dir.is_dir():
        return CheckResult(
            None, f"Inline {package} pin check: skipped (no {workflow_dir.as_posix()})."
        )

    pin_re = re.compile(rf"\b{re.escape(package)}==(?P<version>[0-9]+(?:\.[0-9]+)*)")

    upstream_versions: set[str] = set()
    pins_by_file: dict[str, set[str]] = {}
    for wf, content in _workflow_texts(workflow_dir).items():
        upstream_versions.update(find_upstream_ref_versions(content, upstream_repo))
        found = {m["version"] for m in pin_re.finditer(content)}
        if found:
            pins_by_file[wf.name] = found

    if not upstream_versions or not pins_by_file:
        return CheckResult(None, f"Inline {package} pin check: nothing to compare.")

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
        return CheckResult(False, msg)
    return CheckResult(
        True, f"Inline {package} pins match the uses: refs ({expected})."
    )


def check_pr_templates(
    workflow_dir: Path = WORKFLOW_DIR,
    template_dir: Path = PR_TEMPLATE_DIR,
) -> list[CheckResult]:
    """Check a repository's own `pr-body --template-file` templates.

    A repo with a custom PR-opening job ships the body as a file of its own
    rather than adding a template upstream. Three failure modes are flagged:

    1. The file sits outside *template_dir*. See {data}`PR_TEMPLATE_DIR`.
    2. A workflow references a path that does not exist, which the job only
       discovers when it runs and `pr-body` rejects the missing file.
    3. The frontmatter lacks a `title`, or does not set `footer` to the bare
       boolean `false`. Both `false` and the quoted `'false'` opt out, but an
       absent field, `'False'`, and every other value do not, and the failure
       is silent: the rendered body carries the attribution footer twice.

    A `docs` field is not required. It deep-links the hosted workflows
    reference, which documents upstream jobs only.

    :param workflow_dir: Directory holding the workflow YAML files.
    :param template_dir: Directory the templates are expected to live in.
    :return: A list of `CheckResult`.
    """
    if not workflow_dir.is_dir():
        return [
            CheckResult(
                None, f"PR template check: skipped (no {workflow_dir.as_posix()})."
            )
        ]

    # Map each referenced path to the workflows naming it, so a misplaced
    # template is reported against the file someone has to edit. Keys stay
    # POSIX throughout: a workflow always spells the path with `/`, so the
    # glob below must too, or on Windows one template lands in `candidates`
    # twice and the backslash spelling loses its referencing workflow.
    referenced: dict[str, set[str]] = {}
    for wf, content in _workflow_texts(workflow_dir).items():
        for match in TEMPLATE_FILE_ARG_RE.finditer(content):
            arg_path = match["path"].strip("\"'")
            referenced.setdefault(arg_path, set()).add(wf.name)

    # Templates already in the canonical directory are checked even when no
    # workflow names them, so a body built by a script is covered too.
    candidates = set(referenced)
    if template_dir.is_dir():
        candidates.update(found.as_posix() for found in template_dir.glob("*.md"))

    if not candidates:
        return [
            CheckResult(None, "PR template check: no repository-local templates found.")
        ]

    results: list[CheckResult] = []
    for raw_path in sorted(candidates):
        path = Path(raw_path)
        callers = ", ".join(
            f"`{name}`" for name in sorted(referenced.get(raw_path, ()))
        )
        origin = f" (referenced by {callers})" if callers else ""
        failures: list[str] = []

        if path.parent != template_dir:
            failures.append(
                f"PR template `{raw_path}`{origin} lives outside"
                f" `{template_dir.as_posix()}/`. Move it there and drop any"
                f" `pr-` prefix from its name, so the basename can match its"
                f" job ID and PR branch."
            )

        if not path.is_file():
            failures.append(
                f"PR template `{raw_path}`{origin} does not exist. The job"
                f" fails when it runs."
            )
        else:
            meta, _body = split_frontmatter(path.read_text(encoding="UTF-8"))
            if not meta.get("title"):
                failures.append(
                    f"PR template `{raw_path}` has no `title` in its"
                    f" frontmatter, so the PR and its commit are left unnamed."
                )
            if "footer" not in meta:
                failures.append(
                    f"PR template `{raw_path}` declares no `footer` field, so"
                    f" it opts in to an attribution footer the metadata block"
                    f" already appends. Add `footer: false`."
                )
            elif meta["footer"] is not False:
                failures.append(
                    f"PR template `{raw_path}` sets `footer:"
                    f" {meta['footer']!r}` instead of the bare boolean"
                    f" `false`. Only `false` and the quoted `'false'` opt out;"
                    f" every other value silently duplicates the attribution"
                    f" footer."
                )

        if failures:
            results.extend(CheckResult(False, msg) for msg in failures)
        else:
            results.append(CheckResult(True, f"PR template `{raw_path}`: conforms."))

    return results


def _report_result(
    result: CheckResult, level: AnnotationLevel = AnnotationLevel.WARNING
) -> bool:
    """Print one check line and emit its annotation; return True when it failed.

    :param result: The check outcome to render.
    :param level: Severity for a failure. `WARNING` prints `⚠` and emits a
        warning annotation; `ERROR` prints `✗` and emits an error annotation.
    :return: `True` when the check failed (`passed is False`), else `False`.
        A skipped check (`passed is None`) prints `ℹ` and emits no annotation.
    """
    if result.passed is None:
        echo(f"ℹ {result.message}")
        return False
    if result.passed:
        echo(f"✓ {result.message}")
        return False
    symbol = "✗" if level is AnnotationLevel.ERROR else "⚠"
    emit_annotation(level, result.message)
    echo(f"{symbol} {result.message}")
    return True


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

    # Fetch repo metadata once, for the checks that compare against it.
    repo_metadata: dict[str, str | None] | None = None
    if is_sphinx or project_description:
        if repo:
            repo_metadata = get_repo_metadata(repo)
        else:
            logging.warning("No repo specified, skipping API-based checks.")
            repo_metadata = {"homepageUrl": None, "description": None}

    # Check 1: Package name vs repo name.
    if package_name and repo_name:
        _report_result(check_package_name_vs_repo(package_name, repo_name))

    # Check 2: Website for Sphinx projects.
    if is_sphinx:
        homepage_url = repo_metadata.get("homepageUrl") if repo_metadata else None
        _report_result(check_website_for_sphinx(repo or "", is_sphinx, homepage_url))

    # Check 3: Pages deployment source (Sphinx projects only).
    if is_sphinx and repo:
        _report_result(check_pages_deployment_source(repo))

    # Check 3b: Stale gh-pages branch (Sphinx projects only).
    if is_sphinx and repo:
        _report_result(check_stale_gh_pages_branch(repo))

    # Check 4: Description matches (fatal).
    if project_description:
        repo_description = repo_metadata.get("description") if repo_metadata else None
        if _report_result(
            check_description_matches(
                repo or "", project_description, repo_description
            ),
            AnnotationLevel.ERROR,
        ):
            fatal_error = True

    # Check 5: GitHub topics are a subset of pyproject.toml keywords.
    if keywords and repo:
        _report_result(check_topics_subset_of_keywords(repo, keywords))

    # Check 6: Funding file present when owner has GitHub Sponsors.
    if repo:
        _report_result(check_funding_file(repo))

    # Check 7: Stale draft releases (warning).
    if repo:
        _report_result(check_stale_draft_releases(repo))

    # Check 8: Tag protection rules (warning).
    if repo:
        _report_result(check_tag_protection_rules(repo))

    # Check 9: Fork PR approval policy strict enough (warning).
    if repo:
        _report_result(check_fork_pr_approval_policy(repo))

    # Check 9a: SHA pinning required for Actions (warning).
    if repo:
        _report_result(check_sha_pinning_required(repo))

    # Check 9b: PyPI Trusted Publisher entry registered (warning).
    if repo and package_name:
        _report_result(check_pypi_trusted_publisher(repo, package_name))

    # Check 10: Workflow permissions declared on custom-step workflows.
    for result in check_workflow_permissions():
        _report_result(result)

    # Check 10b: Test matrix excludes reference values present in a live axis.
    for result in check_test_matrix_excludes():
        _report_result(result)

    # Check 10b-bis: Python versions required, advertised and tested agree.
    for result in check_python_version_consistency():
        _report_result(result)

    # Check 10b-ter: Runner images are pinned, and known to the matrix axes.
    for result in check_runner_images():
        _report_result(result)

    # Check 10b-quarter: Release-only steps resolved against this project, and
    # gated on the capability each one consumes.
    for result in check_release_path():
        _report_result(result)

    # Check 10c: Inline upstream pins match the workflow uses: ref version (error).
    if _report_result(check_inline_pins_match_upstream(), AnnotationLevel.ERROR):
        fatal_error = True

    # Check 10d: Repository-local PR body templates (warning).
    for result in check_pr_templates():
        _report_result(result)

    # Check 11: VIRUSTOTAL_API_KEY secret (warning, only when Nuitka builds are active).
    if nuitka_active:
        if has_virustotal_key:
            _report_result(
                CheckResult(True, "VIRUSTOTAL_API_KEY secret is configured.")
            )
        else:
            vt_msg = (
                "VIRUSTOTAL_API_KEY secret is not configured."
                " Release binaries will not be submitted to VirusTotal."
                " Get a free API key at https://www.virustotal.com/gui/my-apikey"
                " and add it as a repository secret."
            )
            _report_result(CheckResult(False, vt_msg))

    # Check 12: REPOMATIC_NOTIFICATIONS_PAT secret (warning, only when the
    # unsubscribe workflow is opted in via notification.unsubscribe).
    if unsubscribe_active:
        if has_notifications_pat:
            _report_result(
                CheckResult(True, "REPOMATIC_NOTIFICATIONS_PAT secret is configured.")
            )
        else:
            notif_msg = (
                "REPOMATIC_NOTIFICATIONS_PAT secret is not configured."
                " The unsubscribe workflow will skip silently."
                " Create a classic PAT with the notifications scope at"
                " https://github.com/settings/tokens/new"
                "?description=REPOMATIC_NOTIFICATIONS_PAT&scopes=notifications"
                " and add it as a repository secret."
            )
            _report_result(CheckResult(False, notif_msg))

    # PAT capability checks (only when REPOMATIC_PAT is configured).
    if not has_pat or not repo:
        if not has_pat:
            echo("ℹ PAT capability checks: skipped (no REPOMATIC_PAT)")
        return 1 if fatal_error else 0

    results = check_all_pat_permissions(repo)

    for passed, msg in results.iter_results():
        if _report_result(CheckResult(passed, msg), AnnotationLevel.ERROR):
            fatal_error = True

    # Check PAT repository scope (warning, not fatal).
    _report_result(check_pat_repository_scope(repo))

    # Check for the dropped Commit statuses permission (warning, not fatal).
    _report_result(check_pat_stale_statuses_permission(repo))

    return 1 if fatal_error else 0
