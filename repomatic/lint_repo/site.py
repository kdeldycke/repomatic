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

"""Checks of the site deployment: GitHub Pages and Cloudflare Pages.

The GitHub Pages source and the redirect of its legacy URLs, the Cloudflare
credentials, the `_redirects` files as the Pages engine reads them, the
`wrangler.toml` file, and the files over the Pages size limit.
"""

from __future__ import annotations

from urllib.parse import urlsplit

import tomlrt

from ..checks import CheckResult
from ..cloudflare.r2 import (
    ACCESS_KEY_ID_ENV,
    OFFLOAD_DOCS_URL,
    PAGES_MAX_FILE_SIZE,
    SECRET_ACCESS_KEY_ENV,
)
from ..cloudflare.redirects import (
    MAX_DYNAMIC_RULES,
    MAX_STATIC_RULES,
    discarded_rules,
    evaluate,
    misordered_statics,
    parse_redirects,
    sample_path,
    shadowed_statics,
)
from ..github.gh import GhCommandError, gh_json, run_gh_command
from ..github.urls import split_slug
from ..humanize import format_file_size
from .context import WRANGLER_TOML
from .settings import RepoSettingProbe

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Iterator
    from typing import Final

    from ..cloudflare.redirects import ParseResult, Rule
    from .context import LintContext


MAX_REPORTED_DEAD_URLS = 5
"""How many abandoned redirect sources a failing `_redirects` check names.

Enough to recognize which part of the file fell off the end, short of dumping
a tail that can run to hundreds of rules into one lint message. The remainder
is counted rather than listed, and the fix is the same reorder either way.
"""

PAGES_LIMIT_LABEL: Final = f"{PAGES_MAX_FILE_SIZE // 2**20} MiB"
"""The Cloudflare Pages file size limit, the way the messages spell it."""

PAGES_DEPLOYMENT_SOURCE: Final = RepoSettingProbe(
    label="Pages deployment source check",
    endpoint="pages",
    field="build_type",
    pass_message="GitHub Pages deployment source is set to GitHub Actions.",
    # `{repo}` is filled by `RepoSettingProbe.check`, not an f-string.
    fail_message=(
        "GitHub Pages deployment source is set to 'Deploy from a branch'."
        " Change it to 'GitHub Actions' under"
        " https://github.com/{repo}/settings/pages"
        " so the docs.yaml workflow can deploy."
    ),
    passing=frozenset({"workflow"}),
    failing=frozenset({"legacy"}),
    unknown="unknown build_type {value!r}",
    unavailable="Pages not configured or API inaccessible",
)
"""The GitHub Pages `build_type`, read by {func}`check_pages_deployment_source`."""


def check_pages_deployment_source(ctx: LintContext) -> CheckResult:
    """Check that GitHub Pages is deployed through GitHub Actions, not a branch.

    The `docs.yaml` workflow uses `actions/upload-pages-artifact` and
    `actions/deploy-pages`, which require the Pages source to be set to
    **GitHub Actions** in the repository settings. Branch-based deployment
    (`legacy`) is incompatible.

    ```{note}
    A 404 means Pages is not configured at all. This is treated as
    indeterminate (`None`) rather than a failure, because the repository may
    not have deployed docs yet.
    ```
    """
    return PAGES_DEPLOYMENT_SOURCE.check(ctx.slug)


def check_pages_redirect_preserved(ctx: LintContext) -> CheckResult:
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

    :return: A `CheckResult`. `passed` is `None` when the repository has no
        legacy Pages URLs to preserve, or the declared URL is unreadable.
    """
    host = urlsplit(ctx.docs_url or "").netloc.lower()
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

    repo = ctx.slug
    owner, name = split_slug(repo)
    legacy_urls = f"https://{owner}.github.io/{name}/…"

    data = gh_json(["api", f"repos/{repo}/pages"], soft=True)
    if not isinstance(data, dict):
        # Pages answers `404` both for a repository that disabled it and for
        # one that never had it, and only the first is a problem. The
        # deployment history tells them apart: a `github-pages` environment
        # deployment means the old URLs were published and are now dead.
        history = gh_json(
            ["api", f"repos/{repo}/deployments?environment=github-pages&per_page=1"],
            soft=True,
        )
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
    return CheckResult(True, f"Legacy GitHub Pages URLs redirect to {host}.")


def check_stale_gh_pages_branch(ctx: LintContext) -> CheckResult:
    """Check for a leftover `gh-pages` branch after switching to GitHub Actions.

    When Pages is deployed through GitHub Actions, the `gh-pages` branch is no
    longer needed and should be deleted to avoid confusion.
    """
    try:
        run_gh_command(["api", f"repos/{ctx.slug}/branches/gh-pages"])
    except GhCommandError as exc:
        # 404: branch doesn't exist. That's the desired state. Any other
        # failure (network, auth, rate limit) means the branch could not be
        # read at all, which is a skip, never a pass.
        if exc.status == 404:
            return CheckResult(True, "No stale gh-pages branch found.")
        return CheckResult(
            None, "Stale gh-pages branch check: skipped (could not query API)."
        )

    msg = (
        "Stale `gh-pages` branch detected. Pages is deployed via GitHub"
        " Actions, so this branch is no longer needed. Delete it with:"
        f" `gh api --method DELETE repos/{ctx.slug}/git/refs/heads/gh-pages`"
    )
    return CheckResult(False, msg)


def check_cloudflare_secrets(ctx: LintContext) -> CheckResult:
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


def check_cloudflare_r2_secrets(ctx: LintContext) -> CheckResult:
    """Report whether the offload can upload to the declared R2 bucket.

    Without the key pair the deploy still publishes, but it drops every file
    over the Pages limit: the very files the bucket was declared to serve. The
    pair is limited to that bucket, which Cloudflare allows on the S3 API only,
    so `CLOUDFLARE_API_TOKEN` cannot stand in for it.
    """
    secrets = f"{ACCESS_KEY_ID_ENV} and {SECRET_ACCESS_KEY_ENV}"
    if ctx.has_cloudflare_r2_keys:
        return CheckResult(True, f"{secrets} are configured.")
    return CheckResult(
        False,
        f"{secrets} are not both configured, while site.cloudflare-r2-bucket"
        f" declares {ctx.config.site_cloudflare_r2_bucket!r}: the deploy drops"
        f" every file over {PAGES_LIMIT_LABEL} instead of serving it from R2."
        " Create an account API token with Object Read & Write, limited to"
        " that bucket, and store its Access Key ID and Secret Access Key as"
        f" those two repository secrets: see {OFFLOAD_DOCS_URL}",
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
    :param parsed: What {func}`~repomatic.cloudflare.redirects.parse_redirects`
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


def _shadowed_report(rules: list[Rule]) -> str:
    """Name the exact rules that never fire, and the pattern that answers for each.

    An exact rule below the first dynamic one is probed in file order, after
    every pattern above it. Where one of those patterns matches its source,
    the exact rule is dead today, and moving it up is what makes it fire: the
    reader must know that the reorder changes these answers, and only these.

    :param rules: The rules {func}`~repomatic.cloudflare.redirects.parse_redirects`
        kept.
    :return: A sentence to append to the failure, empty when no rule is dead.
    """
    shadowed = shadowed_statics(rules)
    if not shadowed:
        return ""
    verdicts = [
        f"{rule.source} (line {rule.line_number}) is answered by"
        f" {winner.source} on line {winner.line_number}"
        for rule, winner in shadowed[:MAX_REPORTED_DEAD_URLS]
    ]
    remainder = len(shadowed) - len(verdicts)
    tail = f", and {remainder} more" if remainder > 0 else ""
    return f" Dead where they sit: {'; '.join(verdicts)}{tail}."


def check_pages_redirects(ctx: LintContext) -> Iterator[CheckResult]:
    """Audit every committed `_redirects` file the way the Pages engine reads it.

    The engine's budget accounting is undocumented and its failures are silent:
    `wrangler pages deploy` prints nothing when the parser drops a rule or
    abandons the file, and a dead redirect looks exactly like a URL nobody
    visits. One site carried 18 dead rules for years this way. The replica in
    {mod}`repomatic.cloudflare.redirects` reports here what production would do.
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
                " `:placeholder` source: only those ride the"
                f" {MAX_STATIC_RULES}-rule static budget, and every rule after"
                " the first dynamic one burns the dynamic budget of"
                f" {MAX_DYNAMIC_RULES}."
                f"{_dead_url_report(text, parsed)}",
            )
        misordered = misordered_statics(parsed.rules)
        if misordered:
            problems += 1
            yield CheckResult(
                False,
                f"{path}: {len(misordered)} exact rule(s) sit below the first"
                f" dynamic rule (line {misordered[0].line_number} on), each"
                f" burning a slot of the {MAX_DYNAMIC_RULES}-rule dynamic"
                f" budget instead of the {MAX_STATIC_RULES}-rule static one."
                " Move them above it: the runtime reads an exact rule ahead of"
                " every pattern only from there."
                f"{_shadowed_report(parsed.rules)}",
            )
        for rule in parsed.rules:
            if not rule.compiles:
                problems += 1
                yield CheckResult(
                    False,
                    f"{path}:{rule.line_number}: the engine cannot compile the"
                    f" source {rule.source}, and drops the rule at run time"
                    " without a word. A source takes one `*`, never beside"
                    " `:splat`, and each placeholder name once.",
                )
            elif rule.unresolved:
                problems += 1
                yield CheckResult(
                    False,
                    f"{path}:{rule.line_number}: the destination"
                    f" {rule.destination} holds {', '.join(rule.unresolved)},"
                    f" which no capture of the source {rule.source} replaces,"
                    " so it reaches the visitor as literal text. Write a splat"
                    " as `:splat` there, and a literal colon as `%3A`.",
                )
        if not problems:
            yield CheckResult(
                True,
                f"{path}: {len(parsed.rules)} rules survive the Pages engine.",
            )


def check_wrangler_config(ctx: LintContext) -> Iterator[CheckResult]:
    """Check the committed `wrangler.toml` against the declared Cloudflare state.

    The file only matters to local `wrangler` commands on a Direct Upload
    project, but it can still contradict the repository's declared state in
    two places Cloudflare will not reconcile for anyone: the project name the
    CI deploy targets, and the compatibility date the live project honours
    server-side. One value sat three years behind the live one this way,
    because nothing read it.
    """
    path = WRANGLER_TOML
    try:
        data = tomlrt.loads(path.read_text(encoding="UTF-8"))
    except (tomlrt.TOMLParseError, OSError) as error:
        yield CheckResult(False, f"{path} does not parse: {error}")
        return

    project = ctx.config.site_cloudflare_project or ctx.repo_name
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

    declared_date = ctx.config.site_cloudflare_compatibility_date
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


def check_oversized_site_files(ctx: LintContext) -> CheckResult:
    """Warn about tracked files the Cloudflare Pages deploy cannot upload.

    Direct Upload rejects any file over the Pages limit. The deploy drops such
    a file unless an R2 bucket takes it, and a dropped file leaves a dead link.
    No setting names the site's sources, so the check reads every tracked
    file: a large file outside the site is a false alarm, which is why this
    warns and never fails.
    """
    files = ctx.oversized_files
    if not files:
        return CheckResult(
            True,
            f"No tracked file is over the {PAGES_LIMIT_LABEL} Cloudflare Pages limit.",
        )
    listed = ", ".join(
        f"{path} ({format_file_size(path.stat().st_size)})" for path in files
    )
    bucket = ctx.config.site_cloudflare_r2_bucket
    if bucket:
        return CheckResult(
            True,
            f"{len(files)} tracked file(s) over the {PAGES_LIMIT_LABEL} Cloudflare"
            f" Pages limit move to R2 bucket {bucket!r} if the site ships them:"
            f" {listed}.",
        )
    return CheckResult(
        False,
        f"{len(files)} tracked file(s) over the {PAGES_LIMIT_LABEL} Cloudflare"
        f" Pages limit: {listed}. The Cloudflare deploy drops any the site ships,"
        " then fails. Declare [tool.repomatic] site.cloudflare-r2-bucket and"
        " site.cloudflare-r2-domain to serve them from R2 instead: see"
        f" {OFFLOAD_DOCS_URL}",
    )
