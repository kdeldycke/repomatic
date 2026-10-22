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

"""The `lint-repo` check roster, in report order.

Each {class}`RepoCheck` declares one check once: its name, the function that
runs it, when it applies, and whether its failure fails the command. The
`lint-repo --help` text and the `lint-repo` job docs render from the roster, so
neither can list fewer checks than the command runs.
"""

from __future__ import annotations

import textwrap
from dataclasses import dataclass

from ..checks import CheckResult
from ..cloudflare.r2 import PAGES_LIMIT_LABEL
from ..components import SyncMode
from ..github.outputs import AnnotationLevel
from ..github.token import PAT_PERMISSION_PROBES
from .pins import (
    check_inline_pins_match_upstream,
    check_manpages_toolchain,
    check_registry_pin_cooldown,
    check_self_pin_cooldown_exemption,
    check_setup_uv_checksum_coverage,
    check_setup_uv_version_pin,
)
from .repo import (
    check_description_matches,
    check_funding_file,
    check_install_guide_downloads,
    check_package_name_vs_repo,
    check_stale_draft_releases,
    check_topics_subset_of_keywords,
    check_undeclared_labels,
    check_website_for_sphinx,
)
from .settings import (
    check_branch_ruleset_on_default,
    check_classic_branch_protection,
    check_fork_pr_approval_policy,
    check_immutable_releases,
    check_notifications_secret,
    check_pat_permissions,
    check_pat_scope,
    check_pypi_trusted_publisher,
    check_sha_pinning_required,
    check_tag_protection_rules,
    check_virustotal_secret,
)
from .site import (
    check_cloudflare_r2_secrets,
    check_cloudflare_secrets,
    check_oversized_site_files,
    check_pages_deployment_source,
    check_pages_redirect_preserved,
    check_pages_redirects,
    check_stale_gh_pages_branch,
    check_wrangler_config,
)
from .tool_config import check_bootstrap_config_drift, check_superseded_local_excludes
from .workflows import (
    check_metadata_keys,
    check_pr_templates,
    check_python_version_consistency,
    check_release_path,
    check_runner_images,
    check_test_matrix_excludes,
    check_thin_caller_pins,
    check_thin_caller_secrets,
    check_thin_caller_triggers,
    check_workflow_dispatch,
    check_workflow_permissions,
)

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator

    from .context import LintContext


@dataclass(frozen=True)
class RepoCheck:
    """One entry of the `lint-repo` check sequence.

    Declaring each check once makes the roster the thing tests and readers
    walk: the order, the gate and the severity of every check sit in one table.
    """

    name: str
    """Stable identity, for tests and for grepping the roster."""

    run: Callable[[LintContext], CheckResult | Iterable[CheckResult]]
    """Perform the check. May answer one result or a stream of them."""

    summary: str
    """What a passing repository looks like, as one clause for `lint-repo --help`.

    Leave out the severity: {attr}`help_entry` derives it from {attr}`fatal`.
    """

    description: str
    """What the check reports and why, as its bullet in the `lint-repo` job docs.

    Markdown, rendered on `docs/workflows.md` by {func}`repo_check_reference`
    in roster order, so a link is relative to that page. It opens on `Fails`
    for a {attr}`fatal` check and on `Warns` or `Reports` for any other, and
    carries no closing period, like every job bullet on that page.
    """

    applies: Callable[[LintContext], bool] = lambda ctx: True
    """Whether this repository has anything for the check to look at.

    A check reading the GitHub API applies only when the context names a
    repository: it reads the slug through
    {attr}`~repomatic.lint_repo.context.LintContext.slug`.
    """

    fatal: bool = False
    """Whether a failure fails the command.

    A fatal check reports at {attr}`~repomatic.github.outputs.AnnotationLevel.ERROR`
    and sets the non-zero exit code; every other check is advisory. A red run
    for an advisory finding teaches its readers to ignore red runs, and that
    hides the failures that count.
    """

    def results(self, ctx: LintContext) -> tuple[CheckResult, ...]:
        """Run the check, normalizing one-or-many into a tuple."""
        produced = self.run(ctx)
        if isinstance(produced, CheckResult):
            return (produced,)
        return tuple(produced)

    def fails(self, result: CheckResult) -> bool:
        """Whether *result* fails the command, which only a fatal check can do."""
        return self.fatal and result.passed is False

    @property
    def level(self) -> AnnotationLevel:
        """The severity a failure reports at, which {attr}`fatal` selects."""
        return AnnotationLevel.ERROR if self.fatal else AnnotationLevel.WARNING

    @property
    def help_entry(self) -> str:
        """This check's item in the `lint-repo --help` list, wrapped for a terminal."""
        # Neither break may split a token: `--exclude-newer-package` and
        # `astral-sh/setup-uv` must stay whole to stay searchable.
        return textwrap.fill(
            f"{self.summary} ({self.level.value}).",
            width=76,
            initial_indent="  - ",
            subsequent_indent="    ",
            break_long_words=False,
            break_on_hyphens=False,
        )


def _enumerate(names: Iterable[str]) -> str:
    """Join *names* the way a sentence lists them: `a, b and c`."""
    items = list(names)
    if len(items) < 2:
        return "".join(items)
    return f"{', '.join(items[:-1])} and {items[-1]}"


PAT_PERMISSION_NAMES = _enumerate(probe.name for probe in PAT_PERMISSION_PROBES)
"""The permissions the PAT probes cover, listed for the `pat-permissions` entry."""

BOOTSTRAP_SECTIONS = ", ".join(
    f"`[{comp.tool_section}]`" for comp in SyncMode.BOOTSTRAP.components()
)
"""The tool sections `init` seeds once, for the `bootstrap-config-drift` entry."""


REPO_CHECKS: tuple[RepoCheck, ...] = (
    RepoCheck(
        "package-name-vs-repo",
        check_package_name_vs_repo,
        summary="Package name matches the repository name",
        description="Warns when the package name differs from the repository name",
        applies=lambda ctx: bool(ctx.package_name and ctx.repo_name),
    ),
    RepoCheck(
        "website-for-sphinx",
        check_website_for_sphinx,
        summary=(
            "A Sphinx project's website field names the documentation URL"
            " declared in [project.urls]"
        ),
        description=(
            "Warns when a Sphinx project's GitHub website field does not name the"
            " documentation URL it declares in `[project.urls]` (`Documentation`, then"
            " `Docs`). A trailing slash and the case of the scheme and host are"
            " ignored, since GitHub stores the website field with the slash a browser"
            " appends. Moving a documentation site to a new domain is what this"
            ' catches: Sphinx renders `<link rel="canonical">` from `html_baseurl`, so'
            " every published page names the new origin while the repository sidebar"
            " keeps sending visitors to the old one. A project declaring no"
            " documentation URL keeps the presence-only check"
        ),
        applies=lambda ctx: bool(ctx.is_sphinx and ctx.repo),
    ),
    RepoCheck(
        "pages-deployment-source",
        check_pages_deployment_source,
        summary=(
            "GitHub Pages publishes from GitHub Actions, when site.deploy"
            " targets GitHub Pages"
        ),
        description=(
            "Warns when a Sphinx project deploying to GitHub Pages (the `site.deploy`"
            " default) has the Pages source set to `Deploy from a branch`. The"
            " `docs.yaml` deploy job publishes with `actions/deploy-pages`, which"
            " needs the Pages source set to `GitHub Actions`"
        ),
        applies=lambda ctx: bool(ctx.repo and ctx.deploys_to("github-pages")),
    ),
    RepoCheck(
        # Needs no repository: both answers come from the workflow environment,
        # so a local run reports the same gap CI does. Gated on the declared
        # target alone, not on Sphinx: a repository building its site with its
        # own workflow needs the same credential.
        "cloudflare-pages-secrets",
        check_cloudflare_secrets,
        summary=(
            "CLOUDFLARE_API_TOKEN secret set, when site.deploy targets Cloudflare Pages"
        ),
        description=(
            "Warns about a missing `CLOUDFLARE_API_TOKEN` when `site.deploy` targets"
            " Cloudflare Pages. The token is the whole of the credential: the account"
            " is derived from it at run time"
        ),
        applies=lambda ctx: ctx.deploys_to("cloudflare-pages"),
    ),
    RepoCheck(
        # The key pair only matters to a site that declared a bucket: without
        # one, the deploy drops oversized files by design and reads no key.
        "cloudflare-r2-secrets",
        check_cloudflare_r2_secrets,
        summary=(
            "CLOUDFLARE_R2_ACCESS_KEY_ID and"
            " CLOUDFLARE_R2_SECRET_ACCESS_KEY secrets set, when"
            " site.cloudflare-r2-bucket is declared"
        ),
        description=(
            "Warns about a missing `CLOUDFLARE_R2_ACCESS_KEY_ID` or"
            " `CLOUDFLARE_R2_SECRET_ACCESS_KEY` when `site.deploy` targets Cloudflare"
            " Pages and `site.cloudflare-r2-bucket` is declared"
        ),
        applies=lambda ctx: bool(
            ctx.deploys_to("cloudflare-pages") and ctx.config.site_cloudflare_r2_bucket
        ),
    ),
    RepoCheck(
        # The mirror image of `pages-deployment-source` above: that one asks
        # the GitHub Pages host to publish, this one asks it to redirect. Only
        # a repository that moved away needs the second question answered.
        "pages-redirect-preserved",
        check_pages_redirect_preserved,
        summary=(
            "Legacy github.io URLs still redirect, for a site moved to Cloudflare Pages"
        ),
        description=(
            "Warns when a site that moved to Cloudflare Pages no longer keeps its old"
            " `github.io` URLs redirecting: `[project.urls]` still naming the"
            " `github.io` host, GitHub Pages disabled, or its custom domain unset or"
            " naming another host"
            " ([details](cloudflare.md#leaving-github-pages-behind-urls-intact))"
        ),
        applies=lambda ctx: bool(ctx.repo and ctx.deploys_to("cloudflare-pages")),
    ),
    RepoCheck(
        # Fatal: a rule the engine drops is already dead in production, not one
        # that might age badly, and the deploy pipeline reports nothing about
        # it. Gated on the file rather than on the deploy target, since a
        # `_redirects` only means anything to Cloudflare Pages anyway.
        "pages-redirects",
        check_pages_redirects,
        summary=(
            "Committed _redirects files survive the Cloudflare Pages"
            " engine: no dropped rules, no silent budget abort"
        ),
        description=(
            "Fails when a committed `_redirects` file would lose rules to the"
            " Cloudflare Pages engine's undocumented budget accounting, or holds"
            " a rule that can never apply as written, since such a rule is"
            " silently dead in production"
            " ([details](cloudflare.md#the-redirects-engine-as-it-actually-is))"
        ),
        applies=lambda ctx: bool(ctx.redirects_files),
        fatal=True,
    ),
    RepoCheck(
        "wrangler-toml",
        check_wrangler_config,
        summary=(
            "wrangler.toml agrees with the declared Cloudflare project"
            " name and compatibility date"
        ),
        description=(
            "Warns when a committed `wrangler.toml` contradicts the declared"
            " Cloudflare project name or compatibility date"
        ),
        applies=lambda ctx: bool(
            ctx.deploys_to("cloudflare-pages") and ctx.has_wrangler_toml
        ),
    ),
    RepoCheck(
        "oversized-site-files",
        check_oversized_site_files,
        summary=(
            f"No tracked file over the {PAGES_LIMIT_LABEL} Cloudflare Pages limit,"
            " unless an R2 bucket is declared to serve it"
        ),
        description=(
            f"Warns about a tracked file over the {PAGES_LIMIT_LABEL} Cloudflare Pages"
            " limit when `site.deploy` targets Cloudflare Pages and no R2 bucket is"
            " declared"
        ),
        applies=lambda ctx: ctx.deploys_to("cloudflare-pages"),
    ),
    RepoCheck(
        "stale-gh-pages-branch",
        check_stale_gh_pages_branch,
        summary="No leftover gh-pages branch in a Sphinx project",
        description=(
            "Warns when a Sphinx project still has a `gh-pages` branch. Pages deploys"
            " through GitHub Actions, so the branch is no longer needed"
        ),
        applies=lambda ctx: bool(ctx.is_sphinx and ctx.repo),
    ),
    RepoCheck(
        "description-matches",
        check_description_matches,
        summary="Repository description matches the project description",
        description=(
            "Fails when the repository description differs from the project description"
        ),
        applies=lambda ctx: bool(ctx.project_description and ctx.repo),
        fatal=True,
    ),
    RepoCheck(
        "topics-subset-of-keywords",
        check_topics_subset_of_keywords,
        summary="GitHub topics are a subset of the pyproject.toml keywords",
        description=(
            "Warns when a GitHub topic matches no `[project] keywords` entry. A"
            " keyword matches the topic GitHub would store for it: lowercased, with"
            " each run of whitespace replaced by a hyphen, so `CLI` declares the `cli`"
            " topic and `Weather forecast` declares `weather-forecast`"
        ),
        applies=lambda ctx: bool(ctx.keywords and ctx.repo),
    ),
    RepoCheck(
        "funding-file",
        check_funding_file,
        summary="Funding file present when the owner has GitHub Sponsors",
        description=(
            "Warns when the repository owner has a GitHub Sponsors listing but"
            " `.github/funding.yml` is missing, so the repository shows no Sponsor"
            " button. Forks are skipped"
        ),
        applies=lambda ctx: bool(ctx.repo),
    ),
    RepoCheck(
        "stale-draft-releases",
        check_stale_draft_releases,
        summary="No stale draft release, whose tag is not a .dev0",
        description=(
            "Warns about draft releases whose tag does not end in `.dev0`. Those are"
            " leftovers from an abandoned or failed release: the only expected drafts"
            " are the rolling dev pre-releases that `sync-dev-release` manages"
        ),
        applies=lambda ctx: bool(ctx.repo),
    ),
    RepoCheck(
        "undeclared-labels",
        check_undeclared_labels,
        summary="Every repository label is declared by a configured label source",
        description=(
            "Warns when the repository carries a label that no configured source (the"
            " labeller's bundled `labels.toml` profiles, a hand-written or downloaded"
            " `extra-labels/` file, the inline `labels.extra` block) declares."
            " `sync-labels` runs `labelmaker apply`, which creates, updates and"
            " renames labels but never deletes one, so a label dropped from a source"
            " keeps existing on GitHub with nothing left to mention it again. Advisory"
            " only: deleting a label detaches every issue and pull request carrying"
            " it, and only the count of what each orphan holds tells a hand-made label"
            " from one still in active use. Extra label files are read in every"
            " format `labelmaker` accepts (JSON, TOML, YAML, and JSON5 as far as its"
            " plain-JSON subset), and a file that cannot be fetched or read skips the"
            " check rather than reporting its labels as orphans"
        ),
        applies=lambda ctx: bool(ctx.repo),
    ),
    RepoCheck(
        "install-guide-downloads",
        check_install_guide_downloads,
        summary="Install guide download URLs resolve to real release assets",
        description=(
            "Warns when a release download URL in `docs/install.md` names a file its"
            " release does not carry. The release freeze pins those URLs before the"
            " binaries exist, so a failed build lane leaves the guide advertising 404s"
            " until the next release moves past it: this is the check that surfaces"
            " the gap instead of leaving it for a user to hit. Versionless"
            " `releases/latest/download` URLs are checked against the latest published"
            " release too, and rot longer: nothing rewrites them at release time, so"
            " a renamed asset leaves one pointing at a 404 indefinitely"
        ),
        applies=lambda ctx: bool(ctx.repo),
    ),
    RepoCheck(
        "tag-protection-rules",
        check_tag_protection_rules,
        summary="No active tag ruleset that can block a release tag push",
        description=(
            "Warns when an active ruleset targets tags, including one inherited from"
            " a parent level. Such a ruleset can stop the `create-tag` job from"
            " pushing release tags unless `REPOMATIC_PAT` is in its bypass list"
        ),
        applies=lambda ctx: bool(ctx.repo),
    ),
    RepoCheck(
        "branch-ruleset-on-default",
        check_branch_ruleset_on_default,
        summary="At least one active branch ruleset",
        description=(
            "Warns when the repository has no active branch ruleset, which the check"
            " reads as a default branch open to deletion and force pushes. It does not"
            " verify that a ruleset targets the default branch itself"
            " ([details](security.md#supply-chain-security))"
        ),
        applies=lambda ctx: bool(ctx.repo),
    ),
    RepoCheck(
        "classic-branch-protection",
        check_classic_branch_protection,
        summary="No classic branch protection rule beside the rulesets",
        description=(
            "Warns when a classic branch protection rule survives beside the"
            " rulesets. GitHub applies both together, so the branch policy splits"
            " across two settings pages ([details](security.md#supply-chain-security))"
        ),
        applies=lambda ctx: bool(ctx.repo),
    ),
    RepoCheck(
        # Gated on the changelog the setup guide gates the same step on: the
        # release workflow publishes a GitHub release for any project with one,
        # Python package or not.
        "immutable-releases",
        check_immutable_releases,
        summary="Immutable releases enabled, for a repository publishing releases",
        description=(
            "Warns when a repository that publishes GitHub releases, which means one"
            " holding a changelog, has immutable releases disabled. That setting locks"
            " the tag and assets of a published release (see [§ Immutable"
            " releases](#immutable-releases))"
        ),
        applies=lambda ctx: bool(ctx.repo and ctx.has_changelog),
    ),
    RepoCheck(
        "fork-pr-approval-policy",
        check_fork_pr_approval_policy,
        summary="Fork PR workflow approval policy strict enough",
        description=(
            "Warns when the fork PR workflow approval policy is weaker than"
            " `first_time_contributors`"
        ),
        applies=lambda ctx: bool(ctx.repo),
    ),
    RepoCheck(
        "sha-pinning-required",
        check_sha_pinning_required,
        summary="Actions required to be pinned to a full-length commit SHA",
        description=(
            "Warns when the repository does not require actions to be pinned to a"
            " full-length commit SHA (`sha_pinning_required`). With the setting on,"
            " GitHub itself refuses to run a workflow that names an action by a"
            " mutable tag or branch. This backs up `zizmor`, whose findings can be"
            " silenced inline ([details](security.md#supply-chain-security))"
        ),
        applies=lambda ctx: bool(ctx.repo),
    ),
    RepoCheck(
        # Gated on `is_package`, not on `package_name`: a uv virtual project
        # sets a `[project] name` to carry dependencies and never publishes it,
        # so matching that name against PyPI reports on a project someone else
        # owns.
        "pypi-trusted-publisher",
        check_pypi_trusted_publisher,
        summary=(
            "The latest PyPI release's provenance names this repository's release.yaml"
        ),
        description=(
            "Warns when the latest PyPI release of a package carries PEP 740"
            " provenance naming a repository or workflow other than this repository's"
            " `release.yaml`, which means the Trusted Publisher entry is registered"
            " wrongly. A package with no release yet, or no provenance, is reported as"
            " skipped ([details](security.md#supply-chain-security))"
        ),
        applies=lambda ctx: bool(ctx.repo and ctx.package_name and ctx.is_package),
    ),
    RepoCheck(
        "workflow-permissions",
        check_workflow_permissions,
        summary="Workflows and reusable-workflow callers declare their permissions",
        description=(
            "Warns when a workflow with steps of its own has no top-level"
            " `permissions` key. Also warns when a job calling a reusable workflow"
            " under `permissions: {}` declares no `permissions:` of its own: GitHub"
            " then aborts the run at startup once a nested job requests a scope the"
            " caller never granted"
        ),
    ),
    RepoCheck(
        "workflow-dispatch",
        check_workflow_dispatch,
        summary="Every standalone workflow can be started by hand",
        description=(
            "Warns when a workflow that neither calls an upstream reusable workflow"
            " nor is reusable itself has no `workflow_dispatch` trigger, so a"
            " maintainer cannot rerun it without a push"
        ),
    ),
    RepoCheck(
        "thin-caller-pins",
        check_thin_caller_pins,
        summary="Thin callers pin the upstream workflows to a release, not to @main",
        description=(
            "Warns when a job calling an upstream reusable workflow references it at"
            " `@main`, which runs whatever the upstream default branch holds when the"
            " job starts instead of a release"
        ),
    ),
    RepoCheck(
        "thin-caller-triggers",
        check_thin_caller_triggers,
        summary="Thin callers carry the triggers of the workflow they call",
        description=(
            "Warns when a thin caller lacks a trigger the upstream workflow it calls"
            " declares, or adds one it does not. An upstream workflow callable only"
            " through `workflow_call` leaves its caller free to pick its own"
        ),
    ),
    RepoCheck(
        "thin-caller-secrets",
        check_thin_caller_secrets,
        summary="Thin callers pass every secret the workflow they call declares",
        description=(
            "Warns when the job of a thin caller that calls its upstream workflow"
            " forwards neither `secrets: inherit` nor every secret that workflow"
            " declares. The secrets are read off that job alone, so another job of"
            " the same file, like the build lane of a downstream `release.yaml`,"
            " cannot vouch for it"
        ),
    ),
    RepoCheck(
        "test-matrix-excludes",
        check_test_matrix_excludes,
        summary="Every test-matrix exclude entry names a value on some matrix axis",
        description=(
            "Warns when a `[tool.repomatic.test-matrix] exclude` entry names a value"
            " found on no matrix axis, like a renamed runner. Such an entry never"
            " matches a combination, so the matrix drops it without a message"
            " ([details](test-matrix.md#guard-the-matrix-with-a-test))"
        ),
    ),
    RepoCheck(
        "python-version-consistency",
        check_python_version_consistency,
        summary=(
            "Python classifiers, requires-python and the workflow test matrices agree"
        ),
        description=(
            "Warns when the lowest `Programming Language :: Python :: X.Y` classifier"
            " differs from the `requires-python` floor. Also warns when a workflow"
            " test matrix listing `python-version` values literally omits either end"
            " of the advertised range, or tests a released version the classifiers do"
            " not advertise"
        ),
    ),
    RepoCheck(
        "runner-images",
        check_runner_images,
        summary=(
            "No job runs on a -latest alias or on an image outside the test matrices"
        ),
        description=(
            "Warns when a job runs on a `-latest` alias, which GitHub repoints"
            " without a commit to review, or on an image outside the test matrix axes."
            " A `runs-on:` built from an expression is left alone"
            " ([details](test-matrix.md#github-hosted-runner-inventory))"
        ),
    ),
    RepoCheck(
        "release-path",
        check_release_path,
        summary=(
            "Each release-only step tests the capability it needs, and"
            " runs on a release commit"
        ),
        description=(
            "Reports which release-only steps a release commit would run or skip for"
            " a Python project, so a missing capability shows on an ordinary push"
            " instead of on release day. Where the reusable workflows live, warns when"
            " a step's `if:` never tests the capability it needs"
        ),
    ),
    RepoCheck(
        "manpages-toolchain",
        check_manpages_toolchain,
        summary=(
            "The manpages.script lockfile pins a click-extra able to render man pages"
        ),
        description=(
            "Warns when a project setting `[tool.repomatic] manpages.script` locks a"
            " `click-extra` older than `9`. The `manpages` release job renders with"
            " `click-extra wrap --help-format man`, an invocation `9.0.0` introduced,"
            " and the job first runs on the commit that tags and publishes. Since a"
            " published release locks its asset list, a failure there costs that"
            " version its man pages for good rather than being repairable afterwards."
            " The subject is `uv.lock`, which is what `uv sync --frozen` installs; a"
            " project with no lockfile, or one whose lockfile never mentions"
            " `click-extra`, is reported as skipped"
        ),
        applies=lambda ctx: bool(ctx.config.manpages_script),
    ),
    RepoCheck(
        "bootstrap-config-drift",
        check_bootstrap_config_drift,
        summary=(
            "Seeded tool configs carry every bundled template entry, or decline it"
            " with a reason"
        ),
        description=(
            f"Warns when a tool config seeded once ({BOOTSTRAP_SECTIONS}) lacks an"
            " entry its bundled template carries. `init` writes those sections on the"
            " first run and hands them over, so a rule the template gains later"
            " reaches no repository and nothing names the gap. Only absence is"
            " reported, keys and list members alike: a value the repository tuned, a"
            " key it added and a comment it rewrote are all its own. A pytest section"
            " that keeps its keys under `[tool.pytest.ini_options]` is measured there,"
            " since pytest refuses a file holding both that table and the native"
            " keys. Advisory, since a template rule dropped on purpose looks identical"
            " to one never received, and only the maintainer can tell them apart. An"
            " entry named under `[tool.repomatic] lint-repo.declined` is reported with"
            " its reason in place of the warning, and an entry there that declines"
            " nothing is warned about in its turn"
        ),
        applies=lambda ctx: bool(ctx.tool_table),
    ),
    RepoCheck(
        "superseded-local-excludes",
        check_superseded_local_excludes,
        summary="No bare [tool.lychee] exclude entry the bundled template now anchors",
        description=(
            "Warns when a `[tool.lychee] exclude` entry is the bare form of one the"
            " bundled template has since anchored. An ongoing sync grafts a local-only"
            " array item back on and retires nothing, so a pattern the template"
            " *replaced* rather than added survives beside its own replacement: lychee"
            " matches these as substrings, and a bare `x\\.com` kept beside"
            " `^https://(www\\.)?x\\.com(/.*)?$` goes on excluding every domain ending"
            " in `x.com`, which is what the anchoring was written to stop. A section"
            " no sync reaches holds the bare form alone, so there the warning names"
            " the anchored form to write in its place rather than advising a drop."
            " Advisory, since only the maintainer can tell a superseded entry from"
            " one the repository narrowed on purpose"
        ),
        applies=lambda ctx: bool(ctx.tool_table),
    ),
    RepoCheck(
        "inline-pins-match-upstream",
        check_inline_pins_match_upstream,
        summary="Inline upstream pins match the version the uses: refs name",
        description=(
            "Fails when a workflow's inline upstream pin (like `uvx"
            " 'repomatic==X.Y.Z'`) names a different version than its `uses:` refs. A"
            " stale pin can lack a symbol the newer refs rely on: the `metadata` job"
            " then fails, and a release can reach PyPI without a tag"
        ),
        fatal=True,
    ),
    # Fatal for the same reason as the pin check above: both describe a
    # workflow that is already broken, not one that might age badly. The
    # exemption check is the sharper of the two, since the pin it guards takes
    # every `needs: metadata` job down with it.
    RepoCheck(
        "self-pin-cooldown-exemption",
        check_self_pin_cooldown_exemption,
        summary=(
            "Inline upstream pins resolving under a cooldown carry their"
            " --exclude-newer-package exemption"
        ),
        description=(
            "Fails when a workflow's inline upstream pin (like `uvx"
            " 'repomatic==X.Y.Z'`) resolves under a cooldown but carries no"
            " `--exclude-newer-package` exemption beside it, checked only in a"
            " workflow that sets `UV_EXCLUDE_NEWER` at all. `uvx` reads no project"
            " configuration, so the flag on the command line is the only place the"
            " bypass can live: without it, a pin naming a release younger than the"
            " window cannot resolve, and since the pin usually sits in the `metadata`"
            " job with every other job `needs: metadata`, the whole workflow fails at"
            " its first job while executing nothing. `sync-workflow-pins` backfills"
            " the flag on its next run, but a repository already pinned at the newest"
            " release never triggers that backfill on its own, which is what this"
            " check catches. The sharper of the two fatal pin checks, since the pin it"
            " guards takes every `needs: metadata` job down with it"
        ),
        fatal=True,
    ),
    # The odd one out in this run of checks, and deliberately not fatal: an
    # unpinned or split `setup-uv` resolves and runs, it just resolves through
    # a uv nobody chose. That ages badly rather than being already broken,
    # which is the line the checks around it sit on the other side of.
    RepoCheck(
        "setup-uv-version-pin",
        check_setup_uv_version_pin,
        summary="Every astral-sh/setup-uv step pins one uv version",
        description=(
            "Warns when an `astral-sh/setup-uv` step declares no `version:` input, or"
            " when steps across the repository pin more than one uv version."
            " `[tool.uv] required-version` is only a floor; left unpinned, `setup-uv`"
            " installs whatever uv release is newest the moment the job runs, seconds"
            " after publication, making the one tool that enforces every cooldown the"
            " one tool carrying none of its own"
        ),
    ),
    # The second half of the same pin: which uv CI runs, then whether it can
    # tell it got that uv. Non-fatal on the same reasoning, and indeterminate
    # rather than red when the table cannot be read.
    RepoCheck(
        "setup-uv-checksum-coverage",
        check_setup_uv_checksum_coverage,
        summary="The pinned uv carries a checksum in the pinned astral-sh/setup-uv",
        description=(
            "Warns when the pinned uv is absent from the checksum table bundled into"
            " the pinned `astral-sh/setup-uv` release. That table holds the only"
            " pinned hashes `setup-uv` verifies a download against, and a version"
            " missing from it is not refused. Before `v10.1.0` it installs unverified,"
            " on a debug line no CI log shows by default. From `v10.1.0` on it is"
            " checked against a hash the action fetches at run time, which no pin"
            " fixes. Since uv ships weekly against the action's monthly cadence, and"
            " `sync-action-pins` and `sync-workflow-pins` walk the two pins"
            " independently, a repository drifts into holding two perfectly good pins"
            " that together pin no hash. `sync-workflow-pins` repairs it by stepping"
            " the uv pin back onto the table, and a `sync-action-pins` bump lets the"
            " pin move forward again. Reported as skipped rather than failed when the"
            " table cannot be read"
        ),
    ),
    # Advisory like the two above: the finding clears on its own once the
    # release ages out of the window, and the pins ship with repomatic, so a
    # downstream repository can act only by adding an entry.
    RepoCheck(
        "registry-pin-cooldown",
        check_registry_pin_cooldown,
        summary=(
            "Every repomatic run tool pin is past the cooldown, or an"
            " exclude-newer-package entry admits it"
        ),
        description=(
            "Warns when a `repomatic run` tool is pinned at a release younger than"
            " [`minimum-release-age`](configuration.md#minimum-release-age), which"
            " `sync-tool-versions` never adopts but a hand bump can. A virtualenv"
            " tool like `nuitka` installs through uv commands that read the"
            " project's `[tool.uv] exclude-newer-package`, so the warning names the"
            " dated entry to add when none admits the release. A downstream"
            " repository that adopts such a pin before it clears the window needs"
            " the same entry. A `uvx` or npm tool reads no such entry, so the"
            " warning gives the date its install starts to work. Binary tools are"
            " not checked: their download applies no cooldown, so nothing holds a"
            " young pin back. A pin whose release date cannot be read is reported"
            " as skipped"
        ),
    ),
    # Fatal for the same reason again: a retired key fails the `metadata` job,
    # and every other job is `needs: metadata`.
    RepoCheck(
        "metadata-keys",
        check_metadata_keys,
        summary="Workflows only ask repomatic show-metadata for keys it still emits",
        description=(
            "Fails when a workflow's `run:` line asks `repomatic show-metadata` for a"
            " key that no longer exists, reading the invocation the way Click does so"
            " an option's value is never mistaken for a positional key. `repomatic"
            " init` syncs a header-only workflow's header and its `uses:` pins and"
            " leaves the job bodies to the repository, so a key retired upstream stays"
            " in a `run:` line nothing sweeps. The command answers an unknown key"
            " with a `UsageError`, and every job reaching the metadata job through"
            " `needs:` dies with it, which turns a retired key into a whole workflow"
            " failing at its first job on the next push. Fatal, like the inline-pin"
            " checks above: all three describe a workflow that is already broken"
            " rather than one that might age badly"
        ),
        fatal=True,
    ),
    RepoCheck(
        "pr-templates",
        check_pr_templates,
        summary=(
            "Repository-local PR body templates sit in .github/pr-templates/ and"
            " carry valid frontmatter"
        ),
        description=(
            "Warns when a repository-local `pr-body --template-file` template sits"
            " outside `.github/pr-templates/`, does not exist, or has frontmatter"
            " without a `title` or that keeps the attribution footer. Both `footer:"
            " false` and the quoted `footer: 'false'` opt out. A template that keeps"
            " the footer renders it twice"
            " ([details](operation-contracts.md#repository-local-templates))"
        ),
    ),
    RepoCheck(
        "virustotal-secret",
        check_virustotal_secret,
        summary="VIRUSTOTAL_API_KEY secret set, when Nuitka is active",
        description=(
            "Warns about a missing `VIRUSTOTAL_API_KEY` when Nuitka binary"
            " compilation is active"
        ),
        applies=lambda ctx: ctx.nuitka_active,
    ),
    RepoCheck(
        "notifications-pat-secret",
        check_notifications_secret,
        summary=(
            "REPOMATIC_NOTIFICATIONS_PAT secret set, when the unsubscribe"
            " workflow is enabled"
        ),
        description=(
            "Warns about a missing `REPOMATIC_NOTIFICATIONS_PAT` when the unsubscribe"
            " workflow is enabled"
        ),
        applies=lambda ctx: ctx.config.notification_unsubscribe,
    ),
    RepoCheck(
        "pat-permissions",
        check_pat_permissions,
        summary=(
            f"With a PAT, the {PAT_PERMISSION_NAMES} permissions of REPOMATIC_PAT"
        ),
        description=(
            "Fails when a configured `REPOMATIC_PAT` lacks one of the permissions the"
            f" automation needs: {PAT_PERMISSION_NAMES}"
        ),
        fatal=True,
    ),
    RepoCheck(
        "pat-repository-scope",
        check_pat_scope,
        summary="With a PAT, a REPOMATIC_PAT limited to this repository",
        description=(
            "Warns when a configured `REPOMATIC_PAT` also has access to another"
            " repository of the same owner. The check asks the owner's most"
            " recently pushed repositories for their Actions settings, which only a"
            " token covering them can read. The setup guide issue stays open on the"
            " same finding"
        ),
        applies=lambda ctx: bool(ctx.has_pat and ctx.repo),
    ),
)
"""Every check `lint-repo` runs, in report order."""

REPO_CHECK_HELP_LIST: str = "\n".join(check.help_entry for check in REPO_CHECKS)
"""The `lint-repo --help` check list, one item per {data}`REPO_CHECKS` entry.

Rendered from the roster, so the help cannot name fewer checks than the command
runs.
"""


def repo_check_reference() -> str:
    """The `lint-repo` job's check list for `docs/workflows.md`, as Markdown.

    One bullet per {data}`REPO_CHECKS` entry, from its
    {attr}`~RepoCheck.description`, in report order. Rendered from the roster
    for the same reason as {data}`REPO_CHECK_HELP_LIST`: the page cannot
    describe fewer checks than the command runs.
    """
    return "\n".join(f"- {check.description}" for check in REPO_CHECKS)


def run_repo_lint(ctx: LintContext) -> Iterator[tuple[RepoCheck, CheckResult]]:
    """Run every check of {data}`REPO_CHECKS` that applies to the repository.

    Reports nothing itself: the caller renders each result at the
    {attr}`~RepoCheck.level` of its check, and fails when
    {meth}`~RepoCheck.fails` says so. Only a fatal check can fail the command;
    everything else is advisory, so a scheduled run stays green on findings a
    maintainer merely needs to see.

    :param ctx: Everything the checks read. Build it with
        {meth}`~repomatic.lint_repo.context.LintContext.from_project` to derive
        the project facts the way the CLI does, or directly for a hand-assembled
        probe.
    :return: Each result, with the check that produced it, in report order.
    """
    for check in REPO_CHECKS:
        if not check.applies(ctx):
            continue
        for result in check.results(ctx):
            yield check, result
