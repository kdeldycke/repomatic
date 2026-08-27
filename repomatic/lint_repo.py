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
import shlex
from dataclasses import dataclass
from functools import cache, cached_property
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlsplit

import tomlrt
import yaml
from click_extra import echo

from .config import Config, deploys_to
from .file_inventory import FileInventory
from .frontmatter import split_frontmatter
from .github.actions import NULL_SHA, AnnotationLevel, emit_annotation
from .github.gh import gh_api_json, gh_graphql, run_gh_command
from .github.token import check_all_pat_permissions
from .matrix_axes import (
    TEST_RUNNERS_FULL,
    TEST_RUNNERS_PR,
    UNSTABLE_PYTHON_VERSIONS,
)
from .metadata import (
    METADATA_VALUE_OPTIONS,
    Dialect,
    Metadata,
    all_metadata_keys,
)
from .pages_redirects import (
    discarded_rules,
    evaluate,
    misordered_statics,
    parse_redirects,
    sample_path,
)
from .prepare_release import SELF_PIN_COOLDOWN_EXEMPTION
from .pypi import (
    PYPI_TRUSTED_PUBLISHER_WORKFLOW,
    get_latest_release_file,
    get_trusted_publishers,
    pypi_trusted_publisher_settings_url,
)
from .pyproject import get_project_name
from .registry import (
    DEFAULT_REPO,
    INSTALL_GUIDE_PATH,
    WORKFLOW_TARGET_ROOT,
    package_of,
)
from .version_sync import (
    SETUP_UV_SLUG,
    find_upstream_ref_versions,
    self_pin_exemption_re,
    setup_uv_verified_versions,
)

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Mapping

    from .pages_redirects import ParseResult

DOCS_URL_KEYS = ("documentation", "docs")
"""Keys in `[project.urls]` naming the published documentation site.

Checked in priority order, and looked up in a lowercased index of the
project's own keys: PEP 621 leaves the spelling to the project, so
`Documentation`, `documentation` and `Docs` all occur in the wild. Mirrors the
same convention `_SOURCE_URL_KEYS` in {mod}`repomatic.pypi` applies to the PyPI
copy of the same mapping.
"""

WORKFLOW_DIR = Path(WORKFLOW_TARGET_ROOT)
"""Directory every workflow check walks.

Derived from the registry constant `repomatic init` deploys against, so the
checks and the generator can never disagree about where a workflow lives.
"""

RELEASE_DOWNLOAD_RE = re.compile(
    r"/releases/(?:download/(?P<tag>[^/\s\")]+)|latest/download)"
    r"/(?P<filename>[^/\s\")]+)"
)
"""A GitHub release asset URL, capturing its tag and filename.

Matches both spellings a guide can use: the tag-pinned
`/releases/download/<tag>/<file>` the release freeze writes, and the
versionless `/releases/latest/download/<file>` alias the binary aliases exist
to serve, which names no tag and so leaves `tag` unset.

Covering only the first made the check a no-op on a guide written entirely
against the alias, which is precisely where a filename rots unnoticed: the
freeze rewrites a pinned tag every release and would surface a bad name, while
an alias URL is never touched again after it is written. `meta-package-manager`
renamed its binaries from `mpm-*` to `meta-package-manager-*` in `7.0.0` and
its install guide kept advertising the old name for six weeks.

Matches any release download link, not only a binary one, so the install
guide's whole download surface is verified with a single pattern. Both groups
stop at a quote, whitespace or a closing parenthesis, covering an HTML `src`,
a Markdown link target and a bare URL in prose alike.
"""

MAX_REPORTED_DEAD_URLS = 5
"""How many abandoned redirect sources a failing `_redirects` check names.

Enough to recognize which part of the file fell off the end, short of dumping
a tail that can run to hundreds of rules into one lint message. The remainder
is counted rather than listed, and the fix is the same reorder either way.
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
"""Every runner image this project has deliberately chosen.

The closest thing to a curated list of images a project should be running on,
and the one place carrying measured guidance on their relative speed and cost.
A job naming something outside it has been picked without that guidance.

The test axes are the whole list: every job runs on an image the suite is also
validated against, so "where is the suite exercised" and "what may a job run on"
are one question. That is deliberate, since each extra image is one more to
track, pin and migrate. A job needing something else is a decision to make
explicitly, by widening the axes rather than by naming an image here.
"""

TEMPLATE_FILE_ARG_RE = re.compile(r"--template-file[=\s]+(?P<path>\S+)")
"""A `repomatic pr-body --template-file` argument inside a workflow `run:` block.

Matched against the raw YAML text rather than the parsed document: the argument
sits inside a folded scalar, so the surrounding `run:` value is one opaque
string whichever way the file is parsed.
"""

FUNDING_SPONSORS_QUERY = """
query($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) { isFork }
  repositoryOwner(login: $owner) {
    ... on Sponsorable { hasSponsorsListing }
  }
}"""
"""Reads whether a repository is a fork and whether its owner runs Sponsors.

One query for both facts the funding check gates on. GraphQL because the REST
API does not expose `hasSponsorsListing`.
"""

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


class CheckResult(NamedTuple):
    """Outcome of one repository check.

    `passed` is tri-state: `True` on success, `False` on failure, `None`
    when the check could not run or does not apply (skipped). `message` is
    the human-readable line for both terminal output and annotations.
    """

    passed: bool | None
    message: str


@cache
def _fetch_rulesets(repo: str) -> list[dict] | None:
    """Fetch the repository's rulesets, parents included.

    One endpoint serves the ruleset checks ({func}`check_tag_protection_rules`
    and {func}`check_branch_ruleset_on_default`), which filter the same
    payload on different `target` values, and the setup guide reads it again
    through the latter. Memoized per repository so one run pays the endpoint
    once; each check still renders its own skip verdict off the shared
    payload. A failed fetch is memoized too, which is what sharing one fetch
    means: both checks report the same unreadable API instead of retrying it.

    :param repo: Repository in 'owner/repo' format.
    :return: The ruleset list, or `None` when the API could not be read.
    """
    rulesets = gh_api_json([
        "api",
        f"repos/{repo}/rulesets",
        "--method",
        "GET",
        "--field",
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


def _url_key(url: str) -> tuple[str, str, str, str, str]:
    """Reduce a URL to what two spellings of the same address share.

    Lowercases the scheme and host, which are case-insensitive per
    [RFC 3986](https://datatracker.ietf.org/doc/html/rfc3986#section-3.1), and
    drops a trailing slash. GitHub stores the website field with the trailing
    slash a browser appends, while `[project.urls]` is usually written without
    one, so comparing raw strings would report a correctly configured
    repository as a mismatch.

    Every other difference survives. A path's case is significant, and `http`
    and `https` really are two origins: those are the splits a comparison
    exists to surface, not noise to smooth over.
    """
    parts = urlsplit(url.strip())
    return (
        parts.scheme.lower(),
        parts.netloc.lower(),
        parts.path.rstrip("/"),
        parts.query,
        parts.fragment,
    )


def documentation_url(project_urls: Mapping[str, str] | None) -> str | None:
    """The documentation site a project declares in `[project.urls]`.

    :param project_urls: The `[project.urls]` mapping, keys untouched.
    :return: The first URL found per {data}`DOCS_URL_KEYS`, or `None` when the
        project declares none.
    """
    if not project_urls:
        return None
    by_key = {key.lower(): str(value).strip() for key, value in project_urls.items()}
    for key in DOCS_URL_KEYS:
        if candidate := by_key.get(key):
            return candidate
    return None


def check_website_for_sphinx(
    repo: str,
    is_sphinx: bool,
    homepage_url: str | None = None,
    docs_url: str | None = None,
) -> CheckResult:
    """Check that a Sphinx project's website field names its documentation.

    GitHub renders the website field in the repository sidebar, and for a
    project publishing Sphinx documentation that is where a visitor expects to
    land. So the check has two halves: the field is set at all, and it names
    the site the project itself declares under {data}`DOCS_URL_KEYS`.

    The second half is what a documentation move leaves behind. Sphinx emits
    `<link rel="canonical">` from `html_baseurl`, and a `conf.py` commonly
    derives that from the same `[project.urls]` entry, so a project that moves
    to a new domain has every published page naming the new origin as canonical
    while the sidebar keeps sending visitors to the one it replaced. Nothing
    but a reader noticing connects the two.

    ```{note}
    A project declaring no documentation URL gets the presence half only. The
    comparison needs the project to have named an expected answer, and nothing
    here invents one from the repository slug.
    ```

    :param repo: Repository in 'owner/repo' format.
    :param is_sphinx: Whether the project uses Sphinx documentation.
    :param homepage_url: The homepage URL from API (to avoid duplicate calls).
    :param docs_url: Documentation URL declared in `[project.urls]`.
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

    if not docs_url:
        return CheckResult(True, f"Website field is set: {homepage_url}")

    if _url_key(homepage_url) != _url_key(docs_url):
        msg = (
            f"Repository website field '{homepage_url}' differs from the"
            f" documentation URL '{docs_url}' declared in [project.urls]."
        )
        return CheckResult(False, msg)

    return CheckResult(
        True, f"Website field matches the documentation URL: {homepage_url}"
    )


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

    # One GraphQL query for both isFork and hasSponsorsListing, through the
    # shared runner: variables travel as GraphQL variables rather than being
    # interpolated into the query body, matching the sibling ruleset check.
    try:
        data = gh_graphql(FUNDING_SPONSORS_QUERY, owner=owner, name=name)
    except RuntimeError as error:
        logging.warning(f"Could not query GitHub Sponsors state: {error}")
        return CheckResult(None, "Funding check: skipped (could not query GitHub API)")

    repo_data = data.get("repository") or {}
    owner_data = data.get("repositoryOwner") or {}

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


def check_install_guide_downloads(repo: str) -> CheckResult:
    """Check the install guide's release download URLs still resolve.

    The release freeze pins those URLs to the version being released, but it
    runs *before* the binaries exist: the freeze commit is what triggers the
    build. So the pin is optimistic, and a release whose binary lane fails
    leaves the guide advertising files that 404 until the next release
    ratchets past it. `7.7.0` shipped that way, with all six links dead.

    A versionless `latest/download` alias fails a different way, and stays
    broken longer: nothing rewrites it at release time, so it silently outlives
    a renamed asset instead of being re-pinned every cycle. Both forms are
    checked, see {data}`RELEASE_DOWNLOAD_RE`.

    Nothing static can catch either: the URLs are well-formed and correct on
    disk, and only the release's actual asset list settles whether they
    resolve. Hence a lint check against the API rather than a conformance
    test.

    Reports rather than repairs, per `claude.md` § Skip and move forward:
    the fix is a one-liner
    ({meth}`~repomatic.prepare_release.PrepareRelease.freeze_install_download_urls`
    re-pointed at the last release that carries binaries), while an automated
    rewrite driven by one API read could downgrade a healthy install page on
    a flaky response.

    :param repo: Repository in 'owner/repo' format.
    :return: A `CheckResult`.
    """
    guide = Path(INSTALL_GUIDE_PATH)
    if not guide.is_file():
        return CheckResult(None, "Install guide downloads: skipped (no install guide).")

    # Group the referenced filenames by the release they are served from. The
    # versionless `latest/download` alias names no tag, so it keys on None and
    # resolves through `gh release view` with no tag argument, which reads the
    # same latest published release GitHub redirects that URL to.
    referenced: dict[str | None, set[str]] = {}
    for match in RELEASE_DOWNLOAD_RE.finditer(guide.read_text(encoding="UTF-8")):
        referenced.setdefault(match["tag"], set()).add(match["filename"])
    if not referenced:
        return CheckResult(
            None, "Install guide downloads: skipped (no release download URLs)."
        )

    missing: list[str] = []
    # None is not comparable to a tag string, so sort it to the front by hand.
    for tag, filenames in sorted(
        referenced.items(), key=lambda item: (item[0] is not None, item[0] or "")
    ):
        label = "latest" if tag is None else tag
        assets = gh_api_json([
            "release",
            "view",
            *(() if tag is None else (tag,)),
            "--json",
            "assets",
            "--repo",
            repo,
        ])
        if assets is None:
            # An unreadable release is indistinguishable from a missing one
            # here, and a false alarm on a transient API failure is worse
            # than a silent pass: the next run re-checks.
            return CheckResult(
                None, f"Install guide downloads: skipped (could not read {label})."
            )
        published = {asset["name"] for asset in assets.get("assets", [])}
        missing.extend(f"{label}/{name}" for name in sorted(filenames - published))

    if missing:
        listed = ", ".join(missing)
        return CheckResult(
            False,
            f"Install guide links {len(missing)} missing release file(s): "
            f"{listed}. Re-point a pinned tag at the last release carrying them "
            "(PrepareRelease.freeze_install_download_urls); fix a `latest` alias "
            "by hand, as the freeze never rewrites one.",
        )
    return CheckResult(True, "Install guide download URLs all resolve.")


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
            "--raw-field",
            "per_page=10",
            "--raw-field",
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
            "--raw-field",
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


def check_classic_branch_protection(repo: str) -> CheckResult:
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
    exposing it, so the finding is a cleanup, per `claude.md` § Defensive
    workflow design.
    ```

    :param repo: Repository in 'owner/repo' format.
    :return: A `CheckResult`. `passed` is `None` when the API could not be
        read, matching the ruleset checks. Reading the rules needs admin on
        the repository, so a token without it skips rather than passes.
    """
    owner, _, name = repo.partition("/")
    try:
        data = gh_graphql(BRANCH_PROTECTION_RULES_QUERY, owner=owner, name=name)
    except (RuntimeError, KeyError, json.JSONDecodeError) as error:
        logging.debug(f"Could not read branch protection rules for {repo}: {error}")
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
        f" https://github.com/{repo}/settings/rules, then delete them at"
        f" https://github.com/{repo}/settings/branches."
    )
    return CheckResult(False, msg)


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


def check_pages_redirect_preserved(repo: str, docs_url: str | None) -> CheckResult:
    """Check that the old `github.io` URLs still redirect to the live site.

    A repository whose site moved to Cloudflare Pages keeps its
    `<owner>.github.io/<repo>/…` URLs answering through a single field: the
    GitHub Pages custom domain. Set it, and GitHub redirects that whole space
    with a path-preserving `301`, for free, covering paths the site never even
    had. What it rescues is precisely the set of URLs nobody can rewrite:
    search indexes, other projects' readmes, and the `[project.urls]` metadata
    frozen into every release already published.

    Two ways to lose it, both invisible from inside the repository. Disabling
    Pages deletes the redirect along with the site, and every historical link
    starts answering `404` with nothing to show a maintainer why. Leaving the
    custom domain unset is quieter still: the old host keeps serving a *copy*
    of the documentation, which stops being rebuilt the moment the deploy job
    is gated off, so the two hosts disagree more with every release.

    :param repo: Repository in 'owner/repo' format.
    :param docs_url: Documentation URL declared in `[project.urls]`, whose
        host is what the custom domain must name.
    :return: A `CheckResult`. `passed` is `None` when the repository has no
        legacy Pages URLs to preserve, or the declared URL is unreadable.
    """
    host = urlsplit(docs_url or "").netloc.lower()
    if not host:
        return CheckResult(
            None,
            "Pages redirect check: skipped (no documentation URL declared in"
            " [project.urls]).",
        )
    if host.endswith(".github.io"):
        return CheckResult(
            False,
            f"[project.urls] still documents {host}, the host this project"
            " deploys away from. Point it at the site's own domain: until it"
            " moves, every new release publishes metadata naming the old host.",
        )

    owner, _, name = repo.partition("/")
    legacy_urls = f"https://{owner}.github.io/{name}/…"

    data = gh_api_json(["api", f"repos/{repo}/pages"])
    if data is None:
        # Pages answers `404` both for a repository that disabled it and for
        # one that never had it, and only the first is a problem. The
        # deployment history tells them apart: a `github-pages` environment
        # deployment means the old URLs were published and are now dead.
        history = gh_api_json([
            "api",
            f"repos/{repo}/deployments?environment=github-pages&per_page=1",
        ])
        if history:
            return CheckResult(
                False,
                f"GitHub Pages is disabled, but this repository published"
                f" there before: every {legacy_urls} URL now answers 404."
                f" Re-enable Pages and set its custom domain to {host}, which"
                " restores the redirect without redeploying anything.",
            )
        return CheckResult(
            None,
            "Pages redirect check: skipped (this repository never published to"
            " GitHub Pages, so it has no legacy URLs to preserve).",
        )

    cname = (data.get("cname") or "").lower()
    if not cname:
        return CheckResult(
            False,
            f"GitHub Pages carries no custom domain, so {legacy_urls}"
            f" still serves its own copy of the documentation instead of"
            f" redirecting to {host}. That copy stops being rebuilt once the"
            f" deploy job is gated off. Set it with: gh api --method PUT"
            f" repos/{repo}/pages --field cname={host}",
        )
    if cname != host:
        return CheckResult(
            False,
            f"GitHub Pages redirects to {cname} while the documentation is"
            f" published at {host}. One of the two is stale, and the old URLs"
            f" follow whichever the custom domain names.",
        )
    return CheckResult(
        True,
        f"Legacy GitHub Pages URLs redirect to {host}.",
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
    except RuntimeError as exc:
        # 404: branch doesn't exist. That's the desired state. Any other
        # failure (network, auth, rate limit) means the branch could not be
        # read at all, which is a skip, never a pass: this used to report
        # every API failure as green, the one false-green path in the lane.
        if "HTTP 404" in str(exc):
            return CheckResult(True, "No stale gh-pages branch found.")
        return CheckResult(
            None, "Stale gh-pages branch check: skipped (could not query API)."
        )

    msg = (
        "Stale `gh-pages` branch detected. Pages is deployed via GitHub"
        " Actions, so this branch is no longer needed. Delete it with:"
        f" `gh api --method DELETE repos/{repo}/git/refs/heads/gh-pages`"
    )
    return CheckResult(False, msg)


def check_workflow_permissions(
    workflows: Mapping[Path, dict] | None = None,
) -> list[CheckResult]:
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

    :param workflows: Pre-parsed workflows, read from disk when `None`.
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
    if workflows is None:
        workflows = _load_workflows()

    for wf_path, data in workflows.items():
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

    stale = metadata.stale_test_matrix_excludes
    if not stale:
        return [
            CheckResult(
                True, "Test matrix excludes check: all entries match a live axis."
            )
        ]

    results: list[CheckResult] = []
    for entry, bad in stale:
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
    # Both extensions, since GitHub accepts either: a downstream repository
    # may have picked the short one (see registry.GITHUB_YAML_PATTERNS).
    paths = [*workflow_dir.glob("*.yaml"), *workflow_dir.glob("*.yml")]
    for path in sorted(paths):
        try:
            texts[path] = path.read_text(encoding="UTF-8")
        except OSError as e:
            logging.warning(f"Could not read {path}: {e}")
    return texts


def _parse_workflow_texts(texts: Mapping[Path, str]) -> dict[Path, dict]:
    """Parse pre-read workflow texts, keeping the jobs-bearing documents.

    The parsed half of the workflow walk. Documents that declare no `jobs:`
    mapping are dropped, so every consumer can index `data["jobs"]` directly.

    :param texts: Raw file contents keyed by path, from {func}`_workflow_texts`.
    :return: A mapping of path to parsed document, for documents declaring jobs.
    """
    workflows: dict[Path, dict] = {}
    for path, text in texts.items():
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as e:
            logging.warning(f"Could not parse {path}: {e}")
            continue
        if isinstance(data, dict) and isinstance(data.get("jobs"), dict):
            workflows[path] = data
    return workflows


def _load_workflows(workflow_dir: Path = WORKFLOW_DIR) -> dict[Path, dict]:
    """Read and parse every workflow in *workflow_dir*.

    The from-disk convenience over {func}`_workflow_texts` and
    {func}`_parse_workflow_texts`, for a check invoked on its own. A full
    `lint-repo` run reads and parses once instead, through
    {attr}`LintContext.workflows`, and hands the result to each check.

    :param workflow_dir: Directory holding the workflow files.
    :return: A mapping of path to parsed document, for documents declaring jobs.
    """
    return _parse_workflow_texts(_workflow_texts(workflow_dir))


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


def _literal_python_axes(workflows: Mapping[Path, dict]) -> dict[str, list[str]]:
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


def check_python_version_consistency(
    workflows: Mapping[Path, dict] | None = None,
) -> list[CheckResult]:
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

    :param workflows: Pre-parsed workflows, read from disk when `None`.
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
    declared = metadata.requires_python_floor
    if declared is None:
        results.append(
            CheckResult(
                None, "Python floor check: skipped (no requires-python lower bound)."
            )
        )
    else:
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

    axes = _literal_python_axes(_load_workflows() if workflows is None else workflows)
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


def literal_runners(
    workflow_dir: Path = WORKFLOW_DIR,
    workflows: Mapping[Path, dict] | None = None,
) -> dict[str, list[str]]:
    """Every runner image this repository names outright, and where.

    Only literals: a value built from an expression (`${{ matrix.os }}`) names
    no image here, and the axis it draws from is checked at its definition. A
    thin caller declares no `steps:` and runs on whatever the reusable workflow
    chose, which is that workflow's business rather than this repository's.

    Separate from {data}`KNOWN_RUNNERS`, and deliberately so. That set is what
    this project has *chosen*; this function reports what it is *running*, and
    the two diverge exactly when something has been left behind. Callers wanting
    "an image this repository has a stake in" need the union of both.

    :param workflow_dir: Directory holding the workflow files. Ignored when
        *workflows* is supplied.
    :param workflows: Pre-parsed workflows, read from disk when `None`.
    :return: A mapping of runner label to the `file.yaml:job-id` locations
        naming it, empty when no job names one literally.
    """
    if workflows is None:
        workflows = _load_workflows(workflow_dir)
    seen: dict[str, list[str]] = {}
    for path, data in workflows.items():
        for job_id, job in data["jobs"].items():
            if not isinstance(job, dict) or "steps" not in job:
                continue
            runner = job.get("runs-on")
            if not isinstance(runner, str) or "${{" in runner:
                continue
            seen.setdefault(runner, []).append(f"{path.name}:{job_id}")
    return seen


def check_runner_images(
    workflows: Mapping[Path, dict] | None = None,
) -> list[CheckResult]:
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

    :param workflows: Pre-parsed workflows, read from disk when `None`.
    :return: A list of `CheckResult`.
    """
    if workflows is None:
        workflows = _load_workflows()
    seen = literal_runners(workflows=workflows)
    if not seen:
        # The two skips are worth telling apart: no workflows at all is a
        # different repository from one whose jobs all delegate.
        if not workflows:
            return [CheckResult(None, "Runner images check: skipped (no workflows).")]
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


def check_release_path(
    workflows: Mapping[Path, dict] | None = None,
) -> list[CheckResult]:
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

    :param workflows: Pre-parsed workflows, read from disk when `None`.
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
    if workflows is None:
        workflows = _load_workflows()
    engine = {
        path.name: data
        for path, data in workflows.items()
        if path.name in {gate.workflow for gate in RELEASE_ONLY_GATES}
    }
    if not engine:
        return results

    # One condition walk per workflow, however many gates read it.
    indexes: dict[str, tuple[list[tuple[str, str]], list[tuple[str, str]]]] = {}
    for gate in RELEASE_ONLY_GATES:
        if gate.metadata_key is None:
            continue
        data = engine.get(gate.workflow)
        if data is None:
            continue
        index = indexes.get(gate.workflow)
        if index is None:
            index = indexes[gate.workflow] = _collect_conditions(data)
        conditions = _conditions_for(index, gate.name)
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


def _collect_conditions(
    workflow: dict,
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Walk a workflow once, collecting every job's and step's `(name, if)`.

    The gate roster asks {func}`_conditions_for` about several names in the
    same document, and each question used to re-walk every job and step; one
    walk feeds them all.

    :param workflow: Parsed workflow document.
    :return: `(job_pairs, step_pairs)`, each a `(name, condition)` list with
        the empty-condition entries kept so the name filter decides.
    """
    job_pairs: list[tuple[str, str]] = []
    step_pairs: list[tuple[str, str]] = []
    for job in workflow.get("jobs", {}).values():
        if not isinstance(job, dict):
            continue
        job_name = job.get("name", "")
        if isinstance(job_name, str):
            job_pairs.append((job_name, str(job.get("if", ""))))
        for step in job.get("steps", []) or ():
            if not isinstance(step, dict):
                continue
            step_name = step.get("name")
            if isinstance(step_name, str):
                step_pairs.append((step_name, str(step.get("if", ""))))
    return job_pairs, step_pairs


def _conditions_for(
    index: tuple[list[tuple[str, str]], list[tuple[str, str]]],
    name: str,
) -> list[str]:
    """Collect the `if:` expressions attached to a named job or step.

    A job is matched on its `name:`, a step on its `name:` too, so the registry
    can address both by the label that shows up in the Actions UI. A job's own
    condition is returned alongside its steps', since either can be where the
    gate lives.

    :param index: A workflow's `(job_pairs, step_pairs)`, from
        {func}`_collect_conditions`.
    :param name: Job or step name; a job matches on its rendered prefix so a
        name carrying a `${{ }}` suffix still matches, a step on equality.
    :return: Every non-empty `if:` expression found, as strings.
    """
    job_pairs, step_pairs = index
    found = [cond for job_name, cond in job_pairs if job_name.startswith(name)]
    found.extend(cond for step_name, cond in step_pairs if step_name == name)
    return [cond for cond in found if cond]


def check_inline_pins_match_upstream(
    workflow_dir: Path = WORKFLOW_DIR,
    upstream_repo: str = DEFAULT_REPO,
    texts: Mapping[Path, str] | None = None,
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

    :param workflow_dir: Directory holding the workflow YAML files. Ignored
        when *texts* is supplied.
    :param upstream_repo: Upstream `owner/repo`; its name is the inline
        package to match (like `repomatic`).
    :param texts: Pre-read workflow texts, read from disk when `None`.
    :return: A `CheckResult`.
    """
    package = package_of(upstream_repo)
    if not workflow_dir.is_dir():
        return CheckResult(
            None, f"Inline {package} pin check: skipped (no {workflow_dir.as_posix()})."
        )
    if texts is None:
        texts = _workflow_texts(workflow_dir)

    pin_re = re.compile(rf"\b{re.escape(package)}==(?P<version>[0-9]+(?:\.[0-9]+)*)")

    upstream_versions: set[str] = set()
    pins_by_file: dict[str, set[str]] = {}
    for wf, content in texts.items():
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


def check_self_pin_cooldown_exemption(
    workflow_dir: Path = WORKFLOW_DIR,
    upstream_repo: str = DEFAULT_REPO,
    texts: Mapping[Path, str] | None = None,
) -> CheckResult:
    """Check every inline upstream pin carries its cooldown exemption.

    A workflow pinning the upstream toolkit in a `run:` command
    (`uvx 'repomatic==1.2.3' metadata`) resolves under the workflow-wide
    `UV_EXCLUDE_NEWER`, and that pin moves in lockstep with the `uses:` refs, so
    it routinely names a release published hours ago. Without
    {data}`~repomatic.prepare_release.SELF_PIN_COOLDOWN_EXEMPTION` on the
    command line, `uvx` cannot resolve it at all. `uvx` reads no project
    configuration, so there is nowhere else the bypass could live.

    The failure is total rather than partial, which is why this is worth a
    dedicated check: the pin usually sits in the `metadata` job, every other job
    is `needs: metadata`, and the whole workflow reports failure while executing
    nothing. Downstream repos are the exposed ones. `tests/test_workflows.py`
    pins the canonical workflows, `sync-workflow-pins` splices a missing flag in
    on any run that also moves the version, and a repo already pinned at the
    newest release falls through both.

    Only flags a pin under a workflow that actually sets a cooldown: a repo
    without one has nothing to exempt.

    :param workflow_dir: Directory holding the workflow YAML files. Ignored
        when *texts* is supplied.
    :param upstream_repo: Upstream `owner/repo`; its name is the inline
        package to match (like `repomatic`).
    :param texts: Pre-read workflow texts, read from disk when `None`.
    :return: A `CheckResult`.
    """
    package = package_of(upstream_repo)
    if not workflow_dir.is_dir():
        return CheckResult(
            None,
            f"{package} cooldown exemption: skipped (no {workflow_dir.as_posix()}).",
        )
    if texts is None:
        texts = _workflow_texts(workflow_dir)

    exemption_re = self_pin_exemption_re(package)
    unexempt: list[str] = []
    pinned = 0
    for wf, content in texts.items():
        # A workflow with no cooldown resolves the pin fine as-is.
        if "UV_EXCLUDE_NEWER" not in content:
            continue
        for match in exemption_re.finditer(content):
            pinned += 1
            if SELF_PIN_COOLDOWN_EXEMPTION not in match.group("flags"):
                unexempt.append(wf.name)
                break

    if not pinned:
        return CheckResult(None, f"{package} cooldown exemption: no inline pin found.")
    if unexempt:
        files = ", ".join(sorted(set(unexempt)))
        return CheckResult(
            False,
            f"Inline {package} pin resolves under a cooldown without"
            f" `{SELF_PIN_COOLDOWN_EXEMPTION}`, so it cannot resolve once the pin"
            f" names a release younger than the window: {files}.",
        )
    return CheckResult(
        True, f"Inline {package} pins ({pinned}) all carry the cooldown exemption."
    )


def _setup_uv_steps(
    workflows: Mapping[Path, dict],
) -> Iterator[tuple[Path, str, str | None]]:
    """Yield `(path, ref, version)` for every `astral-sh/setup-uv` step.

    The one definition of what counts as a `setup-uv` step, shared by the two
    checks reading them so neither can vouch for a step the other skips.

    *ref* is whatever follows the `@` (a commit SHA in a repository whose pins
    `sync-action-pins` walks, a tag in one pinning by tag), and *version* is
    the `with: version:` input, `None` when the step declares none.

    :param workflows: Parsed workflow documents, keyed by path.
    """
    for path, data in workflows.items():
        for job in data["jobs"].values():
            if not isinstance(job, dict):
                continue
            for step in job.get("steps", []) or ():
                if not isinstance(step, dict):
                    continue
                uses = str(step.get("uses", ""))
                if not uses.startswith(f"{SETUP_UV_SLUG}@"):
                    continue
                inputs = step.get("with")
                version = inputs.get("version") if isinstance(inputs, dict) else None
                yield (
                    path,
                    uses.partition("@")[2],
                    None if version is None else str(version),
                )


def check_setup_uv_version_pin(
    workflow_dir: Path = WORKFLOW_DIR,
    workflows: Mapping[Path, dict] | None = None,
) -> CheckResult:
    """Check every `astral-sh/setup-uv` step pins the uv version it installs.

    `[tool.uv] required-version` is a floor for everyone; what a runner
    downloads is a separate question, and left to `setup-uv` the answer is "the
    newest release satisfying the floor", installed seconds after it lands.
    That makes the tool enforcing every cooldown the one tool without one, so
    each step carries `with: version: "X.Y.Z"` and `sync-workflow-pins` walks it
    forward once a uv release clears `minimum-release-age`.

    Steps naming two different versions in one repository are flagged too: the
    pin exists so every job resolves through the same uv, and a split fleet
    silently tests two.

    Reads the parsed workflow rather than its text: a `with:` input is a plain
    mapping, so the step a pin belongs to is a fact the parser already knows.
    Matching the raw text instead means bounding a step's block by hand, and a
    body running past its own step lets one pinned step vouch for every
    unpinned one above it.

    :param workflow_dir: Directory holding the workflow YAML files. Ignored
        when *workflows* is supplied.
    :param workflows: Pre-parsed workflows, read from disk when `None`.
    :return: A `CheckResult`.
    """
    if workflows is None:
        workflows = _load_workflows(workflow_dir)
    if not workflows:
        return CheckResult(None, "setup-uv version pin: skipped (no workflows).")

    unpinned: list[str] = []
    versions: dict[str, set[str]] = {}
    steps = 0
    for path, _ref, version in _setup_uv_steps(workflows):
        steps += 1
        if version is None:
            unpinned.append(path.name)
            continue
        versions.setdefault(version, set()).add(path.name)

    if not steps:
        return CheckResult(None, "setup-uv version pin: no setup-uv step found.")
    if unpinned:
        files = ", ".join(sorted(set(unpinned)))
        return CheckResult(
            False,
            "astral-sh/setup-uv step installs whatever uv is newest, with no"
            ' `with: version: "X.Y.Z"` input, leaving the tool that enforces'
            f" every cooldown without one: {files}.",
        )
    if len(versions) > 1:
        detail = "; ".join(
            f"{version} in {', '.join(sorted(files))}"
            for version, files in sorted(versions.items())
        )
        return CheckResult(
            False, f"astral-sh/setup-uv steps pin more than one uv version: {detail}."
        )
    pinned_version = next(iter(versions))
    return CheckResult(
        True, f"astral-sh/setup-uv steps ({steps}) all pin uv {pinned_version}."
    )


def check_setup_uv_checksum_coverage(
    workflow_dir: Path = WORKFLOW_DIR,
    workflows: Mapping[Path, dict] | None = None,
) -> CheckResult:
    """Check the pinned uv is one the pinned `setup-uv` can checksum-verify.

    The pin check above settles that CI resolves through a uv somebody chose.
    Whether the bytes it downloads are the bytes that version shipped is a
    second question, and `setup-uv` answers it only for the versions listed in
    the checksum table its own release bundles: anything else installs with no
    verification and no warning. So a repository can hold two perfectly good
    pins that together verify nothing, which is what this reports.

    Advisory, and not fatal for the same reason the pin check is not: the
    download succeeds and the job runs, it just carries a cooldown where it
    could have carried a cooldown and a hash. The repair is a
    `sync-action-pins` bump, and `sync-workflow-pins` stops widening the gap on
    its own (see {func}`repomatic.sync_ops._gate_uv_on_checksums`).

    :param workflow_dir: Directory holding the workflow YAML files. Ignored
        when *workflows* is supplied.
    :param workflows: Pre-parsed workflows, read from disk when `None`.
    :return: A `CheckResult`, indeterminate when the table cannot be read.
    """
    if workflows is None:
        workflows = _load_workflows(workflow_dir)
    if not workflows:
        return CheckResult(None, "setup-uv checksum coverage: skipped (no workflows).")

    refs: set[str] = set()
    versions: set[str] = set()
    for _path, ref, version in _setup_uv_steps(workflows):
        refs.add(ref)
        if version is not None:
            versions.add(version)

    if not versions:
        return CheckResult(
            None, "setup-uv checksum coverage: skipped (no pinned uv version)."
        )

    verified = setup_uv_verified_versions(refs)
    if verified is None:
        return CheckResult(
            None, "setup-uv checksum coverage: skipped (checksum table unreadable)."
        )

    unverified = sorted(versions - verified)
    if unverified:
        return CheckResult(
            False,
            f"uv {', '.join(unverified)} carries no checksum in the pinned"
            f" {SETUP_UV_SLUG}, so CI installs uv unverified. Bump the"
            " action pin to one whose table covers it.",
        )
    return CheckResult(
        True,
        f"Pinned uv ({', '.join(sorted(versions))}) is checksum-verified by the"
        f" pinned {SETUP_UV_SLUG}.",
    )


_METADATA_KEY_TOKEN = re.compile(r"^[a-z][a-z0-9_]*$")
"""Shape of a metadata key, used to tell one from a neighbouring shell token.

Every key is a Python identifier, so a token carrying a hyphen (`github-json`),
a dollar (`$GITHUB_OUTPUT`) or a dot is something else on the command line and
is passed over rather than reported as unknown.
"""

_SHELL_OPERATORS = frozenset(("&&", "||", ";", "|", ">", ">>", "<", "&"))
"""Tokens that end the invocation and start unrelated words.

`repomatic metadata a b && echo done` must not report `echo` and `done` as
metadata keys.
"""


def requested_metadata_keys(command: str, package: str) -> list[str]:
    """Positional keys a shell command passes to `<package> metadata`.

    Reads the tail of the invocation the way Click would: options are dropped
    along with the value each one consumes, and what remains are the positional
    key arguments. Handles both spellings in use, the upstream
    `uv run -- repomatic metadata …` and the downstream
    `uvx 'repomatic==1.2.3' metadata …`, by looking for the subcommand after
    any token naming the package.

    Shared with {mod}`repomatic.init_project`, which asks the same question of
    a downstream checkout at sync time rather than of this repository at lint
    time. One parser, so the two verdicts cannot disagree about what a `run:`
    line requests.

    :param command: The step's `run:` script, folded or literal.
    :param package: Upstream package name (like `repomatic`).
    :return: The key names requested, in the order written.
    """
    keys: list[str] = []
    # A literal block scalar holds a whole script: read it a line at a time so
    # a later command's words cannot be mistaken for this one's arguments.
    # Backslash continuations are rejoined first, being one command still.
    for line in command.replace("\\\n", " ").splitlines():
        try:
            tokens = shlex.split(line, comments=True)
        except ValueError:
            # An unbalanced quote is a line this cannot read. A shell would
            # reject it too, so leave it to the shell to complain.
            continue
        seen_package = False
        tail: list[str] | None = None
        for token in tokens:
            if tail is not None:
                if token in _SHELL_OPERATORS:
                    break
                tail.append(token)
            elif token == "metadata" and seen_package:
                tail = []
            elif token == package or token.startswith(f"{package}=="):
                seen_package = True
        if not tail:
            continue

        skip_next = False
        for token in tail:
            if skip_next:
                skip_next = False
                continue
            if token.startswith("-"):
                # `--format=json` carries its value inline, consuming nothing.
                skip_next = "=" not in token and token in METADATA_VALUE_OPTIONS
                continue
            if _METADATA_KEY_TOKEN.match(token):
                keys.append(token)
    return keys


def check_metadata_keys(
    workflow_dir: Path = WORKFLOW_DIR,
    upstream_repo: str = DEFAULT_REPO,
    workflows: Mapping[Path, dict] | None = None,
) -> list[CheckResult]:
    """Check the metadata keys workflows request still exist.

    A downstream repository owns the job bodies of its header-only workflows:
    `repomatic init` syncs their `name`, `on` and `concurrency` blocks and the
    `uses:` pins, and never touches the steps below. So a key retired upstream
    keeps being asked for by a `run:` line nothing sweeps, and the `metadata`
    command answers a retired key with a `UsageError`. Since every other job in
    a test workflow reaches it through `needs:`, the whole run dies at the first
    job, on the next push, from a workflow file that looks freshly synced.

    That is not hypothetical: `coverage_cells` went away with the Codecov
    integration and took a downstream test workflow down with it. Failing here
    instead moves the report to lint time, where it names the file and the job.

    :param workflow_dir: Directory holding the workflow YAML files. Ignored
        when *workflows* is supplied.
    :param upstream_repo: Upstream `owner/repo`; its name is the package whose
        `metadata` invocations are read (like `repomatic`).
    :param workflows: Pre-parsed workflows, read from disk when `None`.
    :return: A list of `CheckResult`.
    """
    package = package_of(upstream_repo)
    if workflows is None:
        workflows = _load_workflows(workflow_dir)
    if not workflows:
        return [CheckResult(None, "Metadata keys check: skipped (no workflows).")]

    valid = all_metadata_keys()
    results: list[CheckResult] = []
    for path, data in workflows.items():
        for job_id, job in data["jobs"].items():
            if not isinstance(job, dict):
                continue
            for step in job.get("steps", []) or ():
                if not isinstance(step, dict):
                    continue
                command = step.get("run")
                if not isinstance(command, str):
                    continue
                requested = requested_metadata_keys(command, package)
                if not requested:
                    continue
                where = f"{path.name}:{job_id}"
                unknown = sorted(set(requested) - valid)
                if unknown:
                    names = ", ".join(f"`{key}`" for key in unknown)
                    results.append(
                        CheckResult(
                            False,
                            f"{where} asks `{package} metadata` for {names}, which"
                            f" no longer exists. The command rejects an unknown"
                            f" key outright, so this job fails on its next run,"
                            f" and every job gated on it through `needs:` with"
                            f" it. Run `{package} metadata --list-keys` for the"
                            f" current set.",
                        )
                    )
                else:
                    count = len(requested)
                    plural = "" if count == 1 else "s"
                    results.append(
                        CheckResult(
                            True,
                            f"{where}: {count} requested metadata key{plural}"
                            f" exist{'s' if count == 1 else ''}.",
                        )
                    )

    if not results:
        return [
            CheckResult(None, f"Metadata keys check: no `{package} metadata` calls.")
        ]
    return results


def check_pr_templates(
    workflow_dir: Path = WORKFLOW_DIR,
    template_dir: Path = PR_TEMPLATE_DIR,
    texts: Mapping[Path, str] | None = None,
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

    :param workflow_dir: Directory holding the workflow YAML files. Ignored
        when *texts* is supplied.
    :param template_dir: Directory the templates are expected to live in.
    :param texts: Pre-read workflow texts, read from disk when `None`.
    :return: A list of `CheckResult`.
    """
    if not workflow_dir.is_dir():
        return [
            CheckResult(
                None, f"PR template check: skipped (no {workflow_dir.as_posix()})."
            )
        ]
    if texts is None:
        texts = _workflow_texts(workflow_dir)

    # Map each referenced path to the workflows naming it, so a misplaced
    # template is reported against the file someone has to edit. Keys stay
    # POSIX throughout: a workflow always spells the path with `/`, so the
    # glob below must too, or on Windows one template lands in `candidates`
    # twice and the backslash spelling loses its referencing workflow.
    referenced: dict[str, set[str]] = {}
    for wf, content in texts.items():
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


@dataclass
class LintContext:
    """Everything the checks read, resolved once per `lint-repo` run."""

    package_name: str | None = None
    """The Python package name."""

    repo_name: str | None = None
    """The repository name."""

    is_package: bool = False
    """Whether the project builds a distributable package.

    Per {func}`repomatic.pyproject.is_python_package`. Gates the checks that
    only make sense for something actually published to PyPI.
    """

    is_sphinx: bool = False
    """Whether the project uses Sphinx documentation."""

    site_deploy: str = Config.site_deploy
    """Where the repository's built site publishes, per `site.deploy`.

    Each host has its own prerequisite, and exactly one of them applies: the
    GitHub Pages source check reads a `404` forever on a Cloudflare-hosted
    project, and the Cloudflare credential check has nothing to say about a
    project deploying with the repository's own OIDC identity. The credential
    check follows the declared target alone, Sphinx or not: a site built by
    the repository's own workflow needs the same secrets the Docs workflow
    would.
    """

    site_cloudflare_project: str = ""
    """Cloudflare Pages project name override, per `site.cloudflare-project`.

    Empty means the project is named after the repository, the deploy job's
    own fallback.
    """

    site_cloudflare_compatibility_date: str = ""
    """Declared Workers runtime date, per `site.cloudflare-compatibility-date`."""

    project_description: str | None = None
    """Description from `pyproject.toml`."""

    docs_url: str | None = None
    """Documentation site declared in `[project.urls]`, per {data}`DOCS_URL_KEYS`."""

    keywords: list[str] | None = None
    """Keywords list from `pyproject.toml`."""

    repo: str | None = None
    """Repository in `owner/repo` format."""

    has_pat: bool = False
    """Whether `GH_TOKEN` contains `REPOMATIC_PAT`."""

    has_virustotal_key: bool = False
    """Whether `VIRUSTOTAL_API_KEY` is configured."""

    has_cloudflare_api_token: bool = False
    """Whether `CLOUDFLARE_API_TOKEN` is configured."""

    nuitka_active: bool = False
    """Whether this project compiles binaries with Nuitka."""

    has_notifications_pat: bool = False
    """Whether `REPOMATIC_NOTIFICATIONS_PAT` is configured."""

    unsubscribe_active: bool = False
    """Whether the unsubscribe workflow is opted in."""

    @cached_property
    def repo_metadata(self) -> dict[str, str | None]:
        """The repository's GitHub-side description and homepage.

        Fetched once and shared by the checks that compare a `pyproject.toml`
        field against it. An absent repository answers empty rather than
        failing, so those checks report a miss instead of the run dying.
        """
        if not self.repo:
            logging.warning("No repo specified, skipping API-based checks.")
            return {"homepageUrl": None, "description": None}
        return get_repo_metadata(self.repo) or {
            "homepageUrl": None,
            "description": None,
        }

    @cached_property
    def redirects_files(self) -> list[Path]:
        """Committed Cloudflare Pages `_redirects` files, `.gitignore` honoured.

        The gitignore filter is what keeps a generated site tree (an `output/`
        or `docs/_build/` copy of the same file) out of the audit: the engine
        replica must read the source of truth, not a build artifact of it.
        """
        return FileInventory().glob_files("**/_redirects")

    @cached_property
    def has_wrangler_toml(self) -> bool:
        """Whether the repository commits a root-level `wrangler.toml`."""
        return Path("wrangler.toml").is_file()

    @cached_property
    def workflow_texts(self) -> dict[Path, str]:
        """Every workflow file's raw text, read once for the whole run.

        Half the roster walks `.github/workflows/`: three checks match the
        files as written and six parse them. Reading once here spares each
        its own directory walk, the same way {attr}`repo_metadata` pools the
        GitHub lookup.
        """
        return _workflow_texts()

    @cached_property
    def workflows(self) -> dict[Path, dict]:
        """The parsed jobs-bearing workflows, from {attr}`workflow_texts`."""
        return _parse_workflow_texts(self.workflow_texts)

    def deploys_to(self, target: str) -> bool:
        """Whether this repository publishes its site to *target*.

        Routes through {func}`repomatic.config.deploys_to`, the same predicate
        the setup guide reads, so the audit and the guide cannot disagree on
        which host a repository is on.
        """
        return deploys_to(self.site_deploy, target, is_sphinx=self.is_sphinx)

    @classmethod
    def from_project(
        cls,
        config: Config,
        *,
        repo: str | None = None,
        repo_name: str | None = None,
        has_pat: bool = False,
        has_virustotal_key: bool = False,
        has_cloudflare_api_token: bool = False,
        has_notifications_pat: bool = False,
    ) -> LintContext:
        """Resolve the project-shaped fields from the current checkout.

        The one derivation the CLI runs: everything `pyproject.toml`, the
        `Metadata` singleton and the `[tool.repomatic]` config can answer is
        read here, so the command hands over only what it alone knows (which
        secrets exist, which repository was named). The 17-field transcription
        this replaces lived in the CLI and cost four edits per new
        check-relevant fact.

        :param config: The resolved `[tool.repomatic]` configuration.
        :param repo: Repository in `owner/repo` format, or `None`.
        :param repo_name: Repository name; derived from *repo* when omitted.
        :param has_pat: Whether `REPOMATIC_PAT` is configured.
        :param has_virustotal_key: Whether `VIRUSTOTAL_API_KEY` is configured.
        :param has_cloudflare_api_token: Whether `CLOUDFLARE_API_TOKEN` is
            configured.
        :param has_notifications_pat: Whether `REPOMATIC_NOTIFICATIONS_PAT` is
            configured.
        """
        if repo_name is None and repo:
            repo_name = repo.split("/")[-1] if "/" in repo else repo
        metadata = Metadata()
        project_table = metadata.pyproject_toml.get("project", {})
        return cls(
            package_name=get_project_name(),
            repo_name=repo_name,
            is_package=metadata.is_python_package,
            is_sphinx=metadata.is_sphinx,
            site_deploy=config.site_deploy,
            site_cloudflare_project=config.site_cloudflare_project,
            site_cloudflare_compatibility_date=(
                config.site_cloudflare_compatibility_date
            ),
            project_description=metadata.project_description,
            docs_url=documentation_url(project_table.get("urls")),
            keywords=project_table.get("keywords"),
            repo=repo or None,
            has_pat=has_pat,
            has_virustotal_key=has_virustotal_key,
            has_cloudflare_api_token=has_cloudflare_api_token,
            nuitka_active=config.nuitka_enabled and bool(metadata.script_entries),
            has_notifications_pat=has_notifications_pat,
            unsubscribe_active=config.notification_unsubscribe,
        )


@dataclass(frozen=True)
class RepoCheck:
    """One entry of the `lint-repo` check sequence.

    The sequence used to be twenty-five hand-numbered `if` blocks whose
    comment numbering had degraded to `Check 10b-quater`, and two checks this
    module defines were never reached by it at all. Declaring each check once
    makes the roster the thing tests and readers walk.
    """

    name: str
    """Stable identity, for tests and for grepping the roster."""

    run: Callable[[LintContext], CheckResult | Iterable[CheckResult]]
    """Perform the check. May answer one result or a stream of them."""

    applies: Callable[[LintContext], bool] = lambda ctx: True
    """Whether this repository has anything for the check to look at."""

    fatal: bool = False
    """Whether a failure fails the command.

    A fatal check reports at {attr}`~repomatic.github.actions.AnnotationLevel.ERROR`
    and sets the non-zero exit code; every other check is advisory, per
    `claude.md` § Defensive workflow design.
    """

    def results(self, ctx: LintContext) -> tuple[CheckResult, ...]:
        """Run the check, normalizing one-or-many into a tuple."""
        produced = self.run(ctx)
        if isinstance(produced, CheckResult):
            return (produced,)
        return tuple(produced)


def _cloudflare_secrets(ctx: LintContext) -> CheckResult:
    """Report whether the Cloudflare Pages deploy has the credential it needs.

    One secret, and no identifier beside it: the account is derived from the
    token at run time, since an account-owned token holding `Cloudflare
    Pages: Edit` and nothing else still enumerates the account it belongs
    to, measured on 2026-08-16 against a `cfat_` token with exactly that
    scope. The token is the whole of the authentication surface.
    """
    if not ctx.has_cloudflare_api_token:
        return CheckResult(
            False,
            "CLOUDFLARE_API_TOKEN not configured, while site.deploy is"
            " 'cloudflare-pages': the deploy will fail. Create an"
            " account-owned token scoped to Account → Cloudflare Pages → Edit"
            " with the pre-filled form at"
            " https://dash.cloudflare.com/?to=/:account/api-tokens"
            "&permissionGroupKeys=%5B%7B%22key%22%3A%22page%22%2C%22type%22%3A%22edit%22%7D%5D"
            " and store it as a repository secret.",
        )
    return CheckResult(
        True,
        "CLOUDFLARE_API_TOKEN is configured, and the account resolves from it.",
    )


def _dead_url_report(text: str, parsed: ParseResult) -> str:
    """Say what the abandoned rules cost, one URL at a time.

    A count of dropped rules says a file is broken; this says which requests
    changed answer, which is the part a reader can check against their own
    traffic. Each abandoned source is run back through the rules that *did*
    survive, and the two outcomes differ in kind: a request that now matches
    nothing falls through to whatever the site serves, while one picked up by
    a surviving pattern is quietly redirected somewhere its author never
    chose. The second is the worse failure and the one nothing else surfaces.

    :param text: The full `_redirects` source.
    :param parsed: What {func}`~repomatic.pages_redirects.parse_redirects`
        made of it.
    :return: A sentence to append to the failure, empty when nothing was
        abandoned.
    """
    abandoned = discarded_rules(text, parsed)
    if not abandoned:
        return ""
    verdicts = []
    for rule in abandoned[:MAX_REPORTED_DEAD_URLS]:
        request = sample_path(rule.source)
        landing = evaluate(parsed.rules, request)
        if landing is None:
            verdicts.append(f"{request} no longer redirects")
        else:
            _matched, destination = landing
            verdicts.append(
                f"{request} now redirects to {destination} instead of"
                f" {rule.destination}"
            )
    remainder = len(abandoned) - len(verdicts)
    tail = f", and {remainder} more rule(s) below them" if remainder > 0 else ""
    return f" Lost from line {abandoned[0].line_number}: {'; '.join(verdicts)}{tail}."


def _pages_redirects(ctx: LintContext) -> Iterator[CheckResult]:
    """Audit every committed `_redirects` file the way the Pages engine reads it.

    The engine's budget accounting is undocumented and its failures are silent:
    `wrangler pages deploy` prints nothing when the parser drops a rule or
    abandons the file, and a dead redirect looks exactly like a URL nobody
    visits. One site carried 18 dead rules for years this way. The replica in
    {mod}`repomatic.pages_redirects` reports here what production would do.
    """
    for path in ctx.redirects_files:
        text = path.read_text(encoding="UTF-8")
        parsed = parse_redirects(text)
        problems = 0
        for entry in parsed.invalid:
            problems += 1
            location = f":{entry.line_number}" if entry.line_number else ""
            yield CheckResult(False, f"{path}{location}: {entry.message}")
        if parsed.aborted_at_line:
            problems += 1
            yield CheckResult(
                False,
                f"{path}: the engine stops reading at line"
                f" {parsed.aborted_at_line}, so every rule below it is dead in"
                " production. Move all exact rules above the first `*` or"
                " `:placeholder` source: only those ride the 2000-rule static"
                " budget, and every rule after the first dynamic one burns the"
                " dynamic budget of 100."
                f"{_dead_url_report(text, parsed)}",
            )
        misordered = misordered_statics(parsed.rules)
        if misordered:
            problems += 1
            yield CheckResult(
                False,
                f"{path}: {len(misordered)} exact rule(s) sit below the first"
                f" dynamic rule (line {misordered[0].line_number} on), each"
                " burning a slot of the 100-rule dynamic budget instead of the"
                " 2000-rule static one. Reordering exact rules first is"
                " behaviour-preserving: the runtime probes exact sources ahead"
                " of patterns wherever they sit in the file.",
            )
        if not problems:
            yield CheckResult(
                True,
                f"{path}: {len(parsed.rules)} rules survive the Pages engine.",
            )


def _wrangler_config(ctx: LintContext) -> Iterator[CheckResult]:
    """Check the committed `wrangler.toml` against the declared Cloudflare state.

    The file only matters to local `wrangler` commands on a Direct Upload
    project, but it can still contradict the repository's declared state in
    two places Cloudflare will not reconcile for anyone: the project name the
    CI deploy targets, and the compatibility date the live project honours
    server-side. One value sat three years behind the live one this way,
    because nothing read it.
    """
    path = Path("wrangler.toml")
    try:
        data = tomlrt.loads(path.read_text(encoding="UTF-8"))
    except (tomlrt.TOMLParseError, OSError) as error:
        yield CheckResult(False, f"{path} does not parse: {error}")
        return

    project = ctx.site_cloudflare_project or ctx.repo_name
    file_name = data.get("name")
    if project and file_name:
        if file_name == project:
            yield CheckResult(True, f"{path} names the deployed project {project!r}.")
        else:
            yield CheckResult(
                False,
                f"{path} names project {file_name!r} while the deploy targets"
                f" {project!r}: local wrangler commands and CI would address"
                " two different Pages projects.",
            )

    declared_date = ctx.site_cloudflare_compatibility_date
    file_date = data.get("compatibility_date")
    if declared_date and file_date is not None:
        if str(file_date) == declared_date:
            yield CheckResult(
                True,
                f"{path} compatibility date matches the declared {declared_date}.",
            )
        else:
            yield CheckResult(
                False,
                f"{path} says compatibility_date {file_date!s} while"
                f" site.cloudflare-compatibility-date declares {declared_date}:"
                " Cloudflare honours the server-side value, so the file is the"
                " one that lies. Keep both equal so the repository states one"
                " value.",
            )


def _virustotal_secret(ctx: LintContext) -> CheckResult:
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


def _notifications_secret(ctx: LintContext) -> CheckResult:
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


def _pat_permissions(ctx: LintContext) -> Iterator[CheckResult]:
    """Yield one result per PAT permission probe, or a skip note."""
    if not ctx.has_pat:
        yield CheckResult(None, "PAT capability checks: skipped (no REPOMATIC_PAT)")
        return
    if not ctx.repo:
        return
    for passed, message in check_all_pat_permissions(ctx.repo).iter_results():
        yield CheckResult(passed, message)


REPO_CHECKS: tuple[RepoCheck, ...] = (
    RepoCheck(
        "package-name-vs-repo",
        lambda ctx: check_package_name_vs_repo(
            ctx.package_name or "", ctx.repo_name or ""
        ),
        applies=lambda ctx: bool(ctx.package_name and ctx.repo_name),
    ),
    RepoCheck(
        "website-for-sphinx",
        lambda ctx: check_website_for_sphinx(
            ctx.repo or "",
            ctx.is_sphinx,
            ctx.repo_metadata.get("homepageUrl"),
            ctx.docs_url,
        ),
        applies=lambda ctx: ctx.is_sphinx,
    ),
    RepoCheck(
        "pages-deployment-source",
        lambda ctx: check_pages_deployment_source(ctx.repo or ""),
        applies=lambda ctx: bool(ctx.repo and ctx.deploys_to("github-pages")),
    ),
    RepoCheck(
        # Needs no repository: both answers come from the workflow environment,
        # so a local run reports the same gap CI does. Gated on the declared
        # target alone, not on Sphinx: a repository building its site with its
        # own workflow needs the same credential.
        "cloudflare-pages-secrets",
        _cloudflare_secrets,
        applies=lambda ctx: ctx.deploys_to("cloudflare-pages"),
    ),
    RepoCheck(
        # The mirror image of `pages-deployment-source` above: that one asks
        # the GitHub Pages host to publish, this one asks it to redirect. Only
        # a repository that moved away needs the second question answered.
        "pages-redirect-preserved",
        lambda ctx: check_pages_redirect_preserved(ctx.repo or "", ctx.docs_url),
        applies=lambda ctx: bool(ctx.repo and ctx.deploys_to("cloudflare-pages")),
    ),
    RepoCheck(
        # Fatal: a rule the engine drops is already dead in production, not one
        # that might age badly, and the deploy pipeline reports nothing about
        # it. Gated on the file rather than on the deploy target, since a
        # `_redirects` only means anything to Cloudflare Pages anyway.
        "pages-redirects",
        _pages_redirects,
        applies=lambda ctx: bool(ctx.redirects_files),
        fatal=True,
    ),
    RepoCheck(
        "wrangler-toml",
        _wrangler_config,
        applies=lambda ctx: bool(
            ctx.deploys_to("cloudflare-pages") and ctx.has_wrangler_toml
        ),
    ),
    RepoCheck(
        "stale-gh-pages-branch",
        lambda ctx: check_stale_gh_pages_branch(ctx.repo or ""),
        applies=lambda ctx: bool(ctx.is_sphinx and ctx.repo),
    ),
    RepoCheck(
        "description-matches",
        lambda ctx: check_description_matches(
            ctx.repo or "",
            ctx.project_description or "",
            ctx.repo_metadata.get("description"),
        ),
        applies=lambda ctx: bool(ctx.project_description),
        fatal=True,
    ),
    RepoCheck(
        "topics-subset-of-keywords",
        lambda ctx: check_topics_subset_of_keywords(ctx.repo or "", ctx.keywords or []),
        applies=lambda ctx: bool(ctx.keywords and ctx.repo),
    ),
    RepoCheck(
        "funding-file",
        lambda ctx: check_funding_file(ctx.repo or ""),
        applies=lambda ctx: bool(ctx.repo),
    ),
    RepoCheck(
        "stale-draft-releases",
        lambda ctx: check_stale_draft_releases(ctx.repo or ""),
        applies=lambda ctx: bool(ctx.repo),
    ),
    RepoCheck(
        "install-guide-downloads",
        lambda ctx: check_install_guide_downloads(ctx.repo or ""),
        applies=lambda ctx: bool(ctx.repo),
    ),
    RepoCheck(
        "tag-protection-rules",
        lambda ctx: check_tag_protection_rules(ctx.repo or ""),
        applies=lambda ctx: bool(ctx.repo),
    ),
    RepoCheck(
        "branch-ruleset-on-default",
        lambda ctx: check_branch_ruleset_on_default(ctx.repo or ""),
        applies=lambda ctx: bool(ctx.repo),
    ),
    RepoCheck(
        "classic-branch-protection",
        lambda ctx: check_classic_branch_protection(ctx.repo or ""),
        applies=lambda ctx: bool(ctx.repo),
    ),
    RepoCheck(
        "immutable-releases",
        lambda ctx: check_immutable_releases(ctx.repo or ""),
        applies=lambda ctx: bool(ctx.repo and ctx.is_package),
    ),
    RepoCheck(
        "fork-pr-approval-policy",
        lambda ctx: check_fork_pr_approval_policy(ctx.repo or ""),
        applies=lambda ctx: bool(ctx.repo),
    ),
    RepoCheck(
        "sha-pinning-required",
        lambda ctx: check_sha_pinning_required(ctx.repo or ""),
        applies=lambda ctx: bool(ctx.repo),
    ),
    RepoCheck(
        # Gated on `is_package`, not on `package_name`: a uv virtual project
        # sets a `[project] name` to carry dependencies and never publishes it,
        # so matching that name against PyPI reports on a project someone else
        # owns.
        "pypi-trusted-publisher",
        lambda ctx: check_pypi_trusted_publisher(
            ctx.repo or "", ctx.package_name or ""
        ),
        applies=lambda ctx: bool(ctx.repo and ctx.package_name and ctx.is_package),
    ),
    RepoCheck(
        "workflow-permissions", lambda ctx: check_workflow_permissions(ctx.workflows)
    ),
    RepoCheck("test-matrix-excludes", lambda ctx: check_test_matrix_excludes()),
    RepoCheck(
        "python-version-consistency",
        lambda ctx: check_python_version_consistency(ctx.workflows),
    ),
    RepoCheck("runner-images", lambda ctx: check_runner_images(ctx.workflows)),
    RepoCheck("release-path", lambda ctx: check_release_path(ctx.workflows)),
    RepoCheck(
        "inline-pins-match-upstream",
        lambda ctx: check_inline_pins_match_upstream(texts=ctx.workflow_texts),
        fatal=True,
    ),
    # Fatal for the same reason as the pin check above: both describe a
    # workflow that is already broken, not one that might age badly. The
    # exemption check is the sharper of the two, since the pin it guards takes
    # every `needs: metadata` job down with it.
    RepoCheck(
        "self-pin-cooldown-exemption",
        lambda ctx: check_self_pin_cooldown_exemption(texts=ctx.workflow_texts),
        fatal=True,
    ),
    # The odd one out in this run of checks, and deliberately not fatal: an
    # unpinned or split `setup-uv` resolves and runs, it just resolves through
    # a uv nobody chose. That ages badly rather than being already broken,
    # which is the line the checks around it sit on the other side of.
    RepoCheck(
        "setup-uv-version-pin",
        lambda ctx: check_setup_uv_version_pin(workflows=ctx.workflows),
    ),
    # The second half of the same pin: which uv CI runs, then whether it can
    # tell it got that uv. Non-fatal on the same reasoning, and indeterminate
    # rather than red when the table cannot be read, per `claude.md` § PAT-gated
    # checks degrade.
    RepoCheck(
        "setup-uv-checksum-coverage",
        lambda ctx: check_setup_uv_checksum_coverage(workflows=ctx.workflows),
    ),
    # Fatal for the same reason again: a retired key fails the `metadata` job,
    # and every other job is `needs: metadata`.
    RepoCheck(
        "metadata-keys",
        lambda ctx: check_metadata_keys(workflows=ctx.workflows),
        fatal=True,
    ),
    RepoCheck("pr-templates", lambda ctx: check_pr_templates(texts=ctx.workflow_texts)),
    RepoCheck(
        "virustotal-secret",
        _virustotal_secret,
        applies=lambda ctx: ctx.nuitka_active,
    ),
    RepoCheck(
        "notifications-pat-secret",
        _notifications_secret,
        applies=lambda ctx: ctx.unsubscribe_active,
    ),
    RepoCheck("pat-permissions", _pat_permissions, fatal=True),
    RepoCheck(
        "pat-repository-scope",
        lambda ctx: check_pat_repository_scope(ctx.repo or ""),
        applies=lambda ctx: bool(ctx.has_pat and ctx.repo),
    ),
    RepoCheck(
        "pat-stale-statuses-permission",
        lambda ctx: check_pat_stale_statuses_permission(ctx.repo or ""),
        applies=lambda ctx: bool(ctx.has_pat and ctx.repo),
    ),
)
"""Every check `lint-repo` runs, in report order.

Two of these (`branch-ruleset-on-default`, `immutable-releases`) were defined
in this module but reached only from {mod}`repomatic.setup_guide`, so
`lint-repo` silently skipped them until the roster made the omission visible.
"""


def run_repo_lint(ctx: LintContext) -> int:
    """Run all repository lint checks.

    Walks {data}`REPO_CHECKS`, printing each result and emitting its GitHub
    Actions annotation. Only a check declaring itself fatal can fail the
    command; everything else is advisory, so a scheduled run stays green on
    findings a maintainer merely needs to see.

    :param ctx: Everything the checks read. Build it with
        {meth}`LintContext.from_project` to derive the project-shaped fields
        the way the CLI does, or directly for a hand-assembled probe.
    :return: Exit code (0 for success, 1 for errors).
    """
    fatal_error = False
    for check in REPO_CHECKS:
        if not check.applies(ctx):
            continue
        level = AnnotationLevel.ERROR if check.fatal else AnnotationLevel.WARNING
        for result in check.results(ctx):
            if _report_result(result, level) and check.fatal:
                fatal_error = True

    return 1 if fatal_error else 0
