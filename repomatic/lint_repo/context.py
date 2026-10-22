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

"""What the repository checks read, resolved once per run.

{class}`LintContext` is the one context that `lint-repo` and `setup-guide`
both read, so the two commands cannot disagree on a gate. It holds what the
command alone knows (the repository it names, which secrets exist), the facts
`pyproject.toml` states, and the `[tool.repomatic]` configuration.

Every remote read is a cached property: a check that never asks for one makes
no call, and two checks asking for the same one share a single call.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path

from packaging.utils import canonicalize_name

from ..changelog import resolved_changelog_path
from ..cloudflare.r2 import oversized_files
from ..config import Config, deploys_to
from ..github.gh import gh_json
from ..github.labels import LabelConfigError, declared_label_names
from ..github.token import check_all_pat_permissions, check_pat_repository_scope
from ..github.urls import split_slug
from ..github.workflow_files import parse_workflow_texts, read_workflow_texts
from ..http import FetchError
from ..metadata.core import Metadata
from ..pyproject import get_project_name
from ..upstream import is_awesome_repo

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Mapping
    from typing import Any

    from ..checks import CheckResult
    from ..file_inventory import FileInventory
    from ..github.token import PatPermissionResults


DOCS_URL_KEYS = ("documentation", "docs")
"""Keys in `[project.urls]` naming the published documentation site.

Checked in priority order, and looked up in a lowercased index of the
project's own keys: PEP 621 leaves the spelling to the project, so
`Documentation`, `documentation` and `Docs` all occur in the wild. Mirrors the
same convention `_SOURCE_URL_KEYS` in {mod}`repomatic.pypi` applies to the PyPI
copy of the same mapping.
"""

WRANGLER_TOML = Path("wrangler.toml")
"""The root-level Wrangler configuration a Cloudflare Pages project may commit."""


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


def _resolve_declared_labels(
    config: Config, *, is_awesome: bool
) -> frozenset[str] | None:
    """Every label name the configured sources declare, or `None` if unknown.

    A label definition that cannot be fetched or read leaves the set unknown
    rather than short, so
    {func}`~repomatic.lint_repo.repo.check_undeclared_labels` reports skipped
    instead of calling that file's labels orphans.

    :param config: The resolved `[tool.repomatic]` configuration.
    :param is_awesome: Whether the repository is an `awesome-*` list.
    :return: The declared names, or `None` when a definition is unreachable
        or unreadable.
    """
    try:
        return frozenset(declared_label_names(config, is_awesome=is_awesome))
    except (FetchError, LabelConfigError) as exc:
        logging.warning(f"Could not read every label definition: {exc}")
        return None


@dataclass
class LintContext:
    """Everything the repository checks and the setup guide read.

    Build it with {meth}`from_project`, which reads the checkout the way the
    CLI does, or directly for a hand-assembled probe. The fields hold what is
    known before any check runs. The cached properties hold what a check reads
    on demand: a GitHub API call, a walk of the working tree, or a parse of the
    workflows.
    """

    config: Config = field(default_factory=Config)
    """The resolved `[tool.repomatic]` configuration."""

    repo: str | None = None
    """Repository in `owner/repo` format.

    `None` turns off every check that reads the GitHub API.
    """

    repo_name: str | None = None
    """The repository name, from `--repo-name` or the name part of {attr}`repo`."""

    package_name: str | None = None
    """The `[project] name` of `pyproject.toml`."""

    is_package: bool = False
    """Whether the project builds a distributable package.

    Per {func}`repomatic.pyproject.is_python_package`. Gates the checks that
    only make sense for something actually published to PyPI.
    """

    is_sphinx: bool = False
    """Whether the project uses Sphinx documentation."""

    uses_myst: bool = False
    """Whether the Sphinx configuration loads MyST-Parser."""

    has_changelog: bool = False
    """Whether the configured changelog exists.

    The release workflow publishes a GitHub release for each release commit,
    and only `prepare-release` writes one, from the changelog. So this is also
    whether the repository publishes GitHub releases, which is what the
    immutable releases setting protects.
    """

    project_description: str | None = None
    """Description from `pyproject.toml`."""

    docs_url: str | None = None
    """Documentation site declared in `[project.urls]`, per {data}`DOCS_URL_KEYS`."""

    keywords: list[str] | None = None
    """Keywords list from `pyproject.toml`."""

    tool_table: Mapping[str, Any] = field(default_factory=dict)
    """The `[tool]` table of `pyproject.toml`, parsed."""

    nuitka_active: bool = False
    """Whether this project compiles binaries with Nuitka."""

    has_pat: bool = False
    """Whether `REPOMATIC_PAT` is configured."""

    has_virustotal_key: bool = False
    """Whether `VIRUSTOTAL_API_KEY` is configured."""

    has_cloudflare_api_token: bool = False
    """Whether `CLOUDFLARE_API_TOKEN` is configured."""

    has_cloudflare_r2_keys: bool = False
    """Whether `CLOUDFLARE_R2_ACCESS_KEY_ID` and `CLOUDFLARE_R2_SECRET_ACCESS_KEY`
    are both configured."""

    has_notifications_pat: bool = False
    """Whether `REPOMATIC_NOTIFICATIONS_PAT` is configured."""

    @property
    def slug(self) -> str:
        """{attr}`repo`, for a check that runs only when it is set.

        :raises ValueError: When no repository is named. A check reading the
            GitHub API declares that it needs one, so reaching this error means
            the check ran without its gate.
        """
        if not self.repo:
            msg = "This check reads the GitHub API, and no repository is named."
            raise ValueError(msg)
        return self.repo

    @property
    def owner(self) -> str:
        """The owner part of {attr}`slug`."""
        return split_slug(self.slug)[0]

    @property
    def is_awesome(self) -> bool:
        """Whether the repository is an `awesome-*` curated list."""
        return bool(self.repo_name and is_awesome_repo(self.repo_name))

    @property
    def pypi_name(self) -> str | None:
        """The canonical form of {attr}`package_name`, the one PyPI files it under.

        PyPI normalizes a project name, so `Fig_Tree` is published as
        `fig-tree`, and its settings page and its pending publisher take that
        form.
        """
        return canonicalize_name(self.package_name) if self.package_name else None

    @property
    def md(self) -> Metadata:
        """CI and project context, which the checks and the setup guide read."""
        return Metadata()

    def deploys_to(self, target: str) -> bool:
        """Whether this repository publishes its site to *target*.

        Routes through {func}`repomatic.config.deploys_to`: one host's
        prerequisite is the other's noise, and exactly one of them applies.
        """
        return deploys_to(self.config.site_deploy, target, is_sphinx=self.is_sphinx)

    @cached_property
    def repo_object(self) -> dict[str, Any] | None:
        """The repository object of the GitHub REST API, read once.

        One ``GET /repos/{repo}`` answers the description, the website field, the
        topics, the fork flag and the owner type, which four checks and the
        setup guide read. `None` when no repository is named or the read
        failed, which each reader reports as skipped rather than failed.
        """
        if not self.repo:
            return None
        data = gh_json(["api", f"repos/{self.repo}"], soft=True)
        return data if isinstance(data, dict) else None

    @cached_property
    def rulesets(self) -> list[dict[str, Any]] | None:
        """The repository's rulesets, the ones inherited from a parent included.

        One endpoint serves the two ruleset checks, which filter the same
        payload on different `target` values. A failed read is kept too: both
        checks then report the same unreadable API instead of asking twice.

        :return: The ruleset list, or `None` when the API could not be read.
        """
        if not self.repo:
            return None
        data = gh_json(
            [
                "api",
                f"repos/{self.repo}/rulesets",
                "--method",
                "GET",
                "--field",
                "includes_parents=true",
            ],
            soft=True,
        )
        return data if isinstance(data, list) else None

    @cached_property
    def declared_labels(self) -> frozenset[str] | None:
        """Every label name the configured sources declare, per `sync-labels`.

        Read here rather than inside the check so the check reads its
        expectation from the same place the sync writes it. Reading it fetches
        the `labels.extra-files` definitions. `None` when a label definition
        could not be fetched or read: the set is unknown rather than short, and
        the check reports skipped.
        """
        return _resolve_declared_labels(self.config, is_awesome=self.is_awesome)

    @cached_property
    def pat_permissions(self) -> PatPermissionResults | None:
        """The results of the PAT permission probes, or `None` without a PAT.

        Both `lint-repo` and the setup guide read them, so they run once.
        """
        if not (self.has_pat and self.repo):
            return None
        return check_all_pat_permissions(self.repo)

    @cached_property
    def pat_scope(self) -> CheckResult | None:
        """The result of the PAT repository-scope probe, or `None` without a PAT."""
        if not (self.has_pat and self.repo):
            return None
        return check_pat_repository_scope(self.repo)

    @property
    def files(self) -> FileInventory:
        """What the repository holds on disk, `.gitignore` applied.

        The inventory of the {class}`~repomatic.metadata.core.Metadata`
        singleton, so the checks share one walk of the tree with every other
        reader of the run.
        """
        return self.md.files

    @cached_property
    def redirects_files(self) -> list[Path]:
        """Committed Cloudflare Pages `_redirects` files, `.gitignore` honoured.

        The gitignore filter is what keeps a generated site tree (an `output/`
        or `docs/_build/` copy of the same file) out of the audit: the engine
        replica must read the source of truth, not a build artifact of it.
        """
        return self.files.glob_files("**/_redirects")

    @cached_property
    def oversized_files(self) -> list[Path]:
        """Tracked files over the Cloudflare Pages limit, `.gitignore` honoured.

        Built trees stay out, like the `_redirects` inventory above: the deploy
        step reads those, and it names each file it moves or drops. The size
        rule is the one the deploy applies, from
        {func}`~repomatic.cloudflare.r2.oversized_files`.
        """
        return oversized_files(self.files.glob_files("**/*"))

    @property
    def has_wrangler_toml(self) -> bool:
        """Whether the repository commits a root-level `wrangler.toml`."""
        return WRANGLER_TOML.is_file()

    @cached_property
    def workflow_texts(self) -> dict[Path, str]:
        """Every workflow file's raw text, read once for the whole run.

        Some checks match the files as written, others parse them. Reading once
        here spares each check its own walk of `.github/workflows/`.
        """
        return read_workflow_texts()

    @cached_property
    def workflows(self) -> dict[Path, dict[str, Any]]:
        """The parsed workflows, from {attr}`workflow_texts`."""
        return parse_workflow_texts(self.workflow_texts)

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
        has_cloudflare_r2_keys: bool = False,
        has_notifications_pat: bool = False,
    ) -> LintContext:
        """Resolve the project facts from the current checkout.

        Everything `pyproject.toml`, the `Metadata` singleton and the
        configuration can answer is read here, so a command hands over only
        what it alone knows: which repository it names, and which secrets exist.

        :param config: The resolved `[tool.repomatic]` configuration.
        :param repo: Repository in `owner/repo` format, or `None`.
        :param repo_name: Repository name; derived from *repo* when omitted.
        :param has_pat: Whether `REPOMATIC_PAT` is configured.
        :param has_virustotal_key: Whether `VIRUSTOTAL_API_KEY` is configured.
        :param has_cloudflare_api_token: Whether `CLOUDFLARE_API_TOKEN` is
            configured.
        :param has_cloudflare_r2_keys: Whether both R2 upload secrets are
            configured.
        :param has_notifications_pat: Whether `REPOMATIC_NOTIFICATIONS_PAT` is
            configured.
        """
        if repo_name is None and repo:
            repo_name = split_slug(repo)[1]
        metadata = Metadata()
        pyproject = metadata.pyproject_toml
        project_table = pyproject.get("project", {})
        return cls(
            config=config,
            repo=repo or None,
            repo_name=repo_name,
            package_name=get_project_name(pyproject),
            is_package=metadata.is_python_package,
            is_sphinx=metadata.is_sphinx,
            uses_myst=metadata.uses_myst,
            has_changelog=resolved_changelog_path(config).exists(),
            project_description=metadata.project_description,
            docs_url=documentation_url(project_table.get("urls")),
            keywords=project_table.get("keywords"),
            tool_table=pyproject.get("tool", {}),
            nuitka_active=config.nuitka_enabled and bool(metadata.script_entries),
            has_pat=has_pat,
            has_virustotal_key=has_virustotal_key,
            has_cloudflare_api_token=has_cloudflare_api_token,
            has_cloudflare_r2_keys=has_cloudflare_r2_keys,
            has_notifications_pat=has_notifications_pat,
        )
