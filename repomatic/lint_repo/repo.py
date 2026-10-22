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

"""Checks of the repository's GitHub metadata against the project.

The description, the website field and the topics come from the one
repository object
{attr}`~repomatic.lint_repo.context.LintContext.repo_object` reads. The other
checks read the funding file, the labels, the draft releases and the release
assets the install guide links to.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from urllib.parse import urlsplit

from ..checks import CheckResult
from ..github.gh import gh_graphql, gh_json
from ..upstream import INSTALL_GUIDE_PATH

TYPE_CHECKING = False
if TYPE_CHECKING:
    from .context import LintContext


RELEASE_DOWNLOAD_RE = re.compile(
    r"/releases/(?:download/(?P<tag>[^/\s\")]+)|latest/download)"
    r"/(?P<filename>[^/\s\")]+)"
)
"""A GitHub release asset URL, capturing its tag and filename.

Matches both spellings a guide can use: the tag-pinned
`/releases/download/<tag>/<file>` the release freeze writes, and the
versionless `/releases/latest/download/<file>` alias the binary aliases exist
to serve, which names no tag and so leaves `tag` unset.

The alias is where a filename rots unnoticed: the freeze rewrites a pinned tag
every release and would surface a bad name, while nothing touches an alias URL
again after it is written. `meta-package-manager` renamed its binaries from
`mpm-*` to `meta-package-manager-*` in `7.0.0` and its install guide kept
advertising the old name for six weeks.

Matches any release download link, not only a binary one, so the install
guide's whole download surface is verified with a single pattern. Both groups
stop at a quote, whitespace or a closing parenthesis, covering an HTML `src`,
a Markdown link target and a bare URL in prose alike.
"""

SPONSORS_LISTING_QUERY = """
query($owner: String!) {
  repositoryOwner(login: $owner) {
    ... on Sponsorable { hasSponsorsListing }
  }
}"""
"""Reads whether a repository owner runs GitHub Sponsors.

GraphQL because the REST API does not expose `hasSponsorsListing`.
"""

DEV_PRERELEASE_SUFFIX = ".dev0"
"""Tag suffix of the rolling dev pre-releases `sync-dev-release` manages."""

RELEASE_LIST_LIMIT = 100
"""How many of the most recent releases {func}`check_stale_draft_releases` reads.

`gh release list` stops at 30 when no limit is given. A draft left by an
abandoned release sits among the recent ones, and one page of the API holds
100.
"""


def check_package_name_vs_repo(ctx: LintContext) -> CheckResult:
    """Check that the package name matches the repository name."""
    if ctx.package_name != ctx.repo_name:
        msg = (
            f"Package name '{ctx.package_name}' differs from repository name"
            f" '{ctx.repo_name}'."
        )
        return CheckResult(False, msg)
    return CheckResult(
        True, f"Package name '{ctx.package_name}' matches repository name."
    )


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


def check_website_for_sphinx(ctx: LintContext) -> CheckResult:
    """Check that a Sphinx project's website field names its documentation.

    GitHub renders the website field in the repository sidebar, and for a
    project publishing Sphinx documentation that is where a visitor expects to
    land. So the check has two halves: the field is set at all, and it names
    the site the project itself declares under
    {data}`~repomatic.lint_repo.context.DOCS_URL_KEYS`.

    The second half is what a documentation move leaves behind. Sphinx emits
    `<link rel="canonical">` from `html_baseurl`, and a `conf.py` commonly
    derives that from the same `[project.urls]` entry, so a project that moves
    to a new domain has every published page naming the new origin as canonical
    while the sidebar keeps sending visitors to the old one. Nothing but a
    reader noticing connects the two.

    ```{note}
    A project declaring no documentation URL gets the presence half only. The
    comparison needs the project to have named an expected answer, and nothing
    here invents one from the repository slug.
    ```
    """
    if ctx.repo_object is None:
        return CheckResult(None, "Website check: skipped (could not query API).")

    homepage_url = ctx.repo_object.get("homepage") or None
    if not homepage_url:
        msg = "Sphinx documentation detected but repository website field is not set."
        return CheckResult(False, msg)

    if not ctx.docs_url:
        return CheckResult(True, f"Website field is set: {homepage_url}")

    if _url_key(homepage_url) != _url_key(ctx.docs_url):
        msg = (
            f"Repository website field '{homepage_url}' differs from the"
            f" documentation URL '{ctx.docs_url}' declared in [project.urls]."
        )
        return CheckResult(False, msg)

    return CheckResult(
        True, f"Website field matches the documentation URL: {homepage_url}"
    )


def check_description_matches(ctx: LintContext) -> CheckResult:
    """Check that the repository description matches the project description."""
    if ctx.repo_object is None:
        return CheckResult(None, "Description check: skipped (could not query API).")

    repo_description = ctx.repo_object.get("description") or None
    if ctx.project_description != repo_description:
        msg = (
            f"Repo description '{repo_description}' != "
            f"project description '{ctx.project_description}'."
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


def check_funding_file(ctx: LintContext) -> CheckResult:
    """Check that a repository whose owner runs GitHub Sponsors has a `FUNDING.yml`.

    Skips forks, which inherit the parent's sponsor button, and owners without
    a Sponsors listing.
    """
    if _funding_file_exists():
        return CheckResult(True, "Funding file found.")

    if ctx.repo_object is None:
        return CheckResult(None, "Funding check: skipped (could not query GitHub API)")
    if ctx.repo_object.get("fork"):
        return CheckResult(None, "Funding check: skipped (repository is a fork)")

    try:
        data = gh_graphql(SPONSORS_LISTING_QUERY, owner=ctx.owner)
    except (RuntimeError, KeyError) as error:
        logging.warning(f"Could not query GitHub Sponsors state: {error}")
        return CheckResult(None, "Funding check: skipped (could not query GitHub API)")

    owner_data = (data or {}).get("repositoryOwner") or {}
    if not owner_data.get("hasSponsorsListing"):
        return CheckResult(
            None, "Funding check: skipped (owner has no GitHub Sponsors listing)"
        )

    msg = (
        "Owner has GitHub Sponsors enabled but .github/FUNDING.yml is missing."
        " Create it to display the Sponsor button on the repository."
    )
    return CheckResult(False, msg)


def check_stale_draft_releases(ctx: LintContext) -> CheckResult:
    """Check for draft releases that are not dev pre-releases.

    A draft release whose tag does not end with {data}`DEV_PRERELEASE_SUFFIX`
    is likely a leftover from an abandoned or failed release. The only expected
    drafts are the rolling dev pre-releases `sync-dev-release` manages.
    """
    releases = gh_json(
        [
            "release",
            "list",
            "--json",
            "tagName,isDraft",
            "--limit",
            str(RELEASE_LIST_LIMIT),
            "--repo",
            ctx.slug,
        ],
        soft=True,
    )
    if not isinstance(releases, list):
        return CheckResult(
            None, "Stale draft releases check: skipped (could not query API)."
        )

    stale_drafts = [
        r["tagName"]
        for r in releases
        if r.get("isDraft") and not r["tagName"].endswith(DEV_PRERELEASE_SUFFIX)
    ]
    if stale_drafts:
        tags = ", ".join(stale_drafts)
        msg = f"Stale draft releases found: {tags}. Delete these leftover drafts."
        return CheckResult(False, msg)
    return CheckResult(True, "No stale draft releases.")


def check_undeclared_labels(ctx: LintContext) -> CheckResult:
    """Report labels the repository carries that no configured source declares.

    `sync-labels` runs `labelmaker apply`, which creates, updates and renames
    but never deletes. So a label retired from
    {func}`~repomatic.github.labels.declared_label_names`' sources keeps existing on
    GitHub, and no other job will ever mention it again. This is the only
    reading that closes that loop.

    Advisory on purpose, and it must stay that way on both counts. A repository
    may legitimately carry a hand-made label nobody declared, so a red run would
    be wrong; and deleting a label detaches every issue and pull request
    carrying it, which is a judgement no check should push someone into making
    quickly. Of nine orphans `meta-package-manager` carried, eight held nothing
    and one held 864 items, and only the counts told them apart.
    """
    declared = ctx.declared_labels
    if declared is None:
        return CheckResult(
            None,
            "Undeclared labels check: skipped (could not read every label definition).",
        )

    # No `--jq` here: it is rejected alongside `--slurp`, and without `--slurp`
    # it would emit bare newline-separated names rather than JSON. Paginating
    # the plain endpoint merges the pages into one array on its own.
    payload = gh_json(
        ["api", "--paginate", f"repos/{ctx.slug}/labels?per_page=100"], soft=True
    )
    if not isinstance(payload, list):
        return CheckResult(
            None, "Undeclared labels check: skipped (could not query API)."
        )

    names = {str(entry["name"]) for entry in payload if entry.get("name")}
    orphans = sorted(names - set(declared))
    if orphans:
        listed = ", ".join(f"'{name}'" for name in orphans)
        msg = (
            f"Labels present on the repository but declared nowhere: {listed}. "
            "Count what each one carries before deleting it, with "
            f"`gh api 'search/issues?q=repo:{ctx.slug}+label:\"<name>\"' --jq "
            "'.total_count'`: a label holding items needs its items moved first, "
            "since deleting it detaches them."
        )
        return CheckResult(False, msg)
    return CheckResult(True, f"All {len(names)} labels are declared.")


def check_install_guide_downloads(ctx: LintContext) -> CheckResult:
    """Check the install guide's release download URLs still resolve.

    The release freeze pins those URLs to the version being released, but it
    runs *before* the binaries exist: the freeze commit is what triggers the
    build. So the pin is optimistic, and a release whose binary lane fails
    leaves the guide advertising files that 404 until the next release
    ratchets past it.

    A versionless `latest/download` alias fails a different way, and stays
    broken longer: nothing rewrites it at release time, so it silently outlives
    a renamed asset instead of being re-pinned every cycle. Both forms are
    checked, see {data}`RELEASE_DOWNLOAD_RE`.

    Nothing static can catch either: the URLs are well-formed and correct on
    disk, and only the release's actual asset list settles whether they
    resolve. Hence a lint check against the API rather than a conformance
    test.

    Reports rather than repairs: the fix is a one-liner
    ({meth}`~repomatic.release.prepare_release.PrepareRelease.freeze_install_download_urls`
    re-pointed at the last release that carries binaries), while an automated
    rewrite driven by one API read could downgrade a healthy install page on
    a flaky response.
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
        assets = gh_json(
            [
                "release",
                "view",
                *(() if tag is None else (tag,)),
                "--json",
                "assets",
                "--repo",
                ctx.slug,
            ],
            soft=True,
        )
        if not isinstance(assets, dict):
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


def check_topics_subset_of_keywords(ctx: LintContext) -> CheckResult:
    """Check that the GitHub repository topics are a subset of the keywords.

    ```{note}
    A keyword matches the topic GitHub would store for it: lowercased, with
    each run of whitespace replaced by a hyphen. A topic holds only lowercase
    letters, numbers and hyphens, while `[project] keywords` carries the
    spelling the package publishes. A project keywording an acronym (`CLI`,
    `EML`, `MMDF`) or a phrase (`Weather forecast`) would otherwise be told to
    add topics it already declares, with no spelling of the keyword able to
    satisfy both sides.
    ```
    """
    if ctx.repo_object is None:
        return CheckResult(
            None, "Topics check: skipped (could not fetch GitHub topics)"
        )

    topics = {str(topic) for topic in ctx.repo_object.get("topics") or () if topic}
    if not topics:
        return CheckResult(None, "Topics check: skipped (no GitHub topics set)")

    declared = {"-".join(keyword.lower().split()) for keyword in ctx.keywords or ()}
    extra = sorted(topic for topic in topics if topic.lower() not in declared)
    if extra:
        msg = (
            f"GitHub topics not in pyproject.toml keywords: {', '.join(extra)}. "
            "Add them to [project] keywords or remove from repo topics."
        )
        return CheckResult(False, msg)
    return CheckResult(
        True, f"All {len(topics)} GitHub topics are in pyproject.toml keywords."
    )
