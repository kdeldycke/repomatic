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

"""Extract metadata from repository and Python projects to be used by GitHub workflows.

This module solves a fundamental limitation of GitHub Actions: a workflow run is
triggered by a singular event, which might encapsulate **multiple commits**. GitHub only
exposes `github.event.head_commit` (the most recent commit), but workflows often need
to process all commits in the push event.

This is critical for releases, where two commits are pushed together:

1. `[changelog] Release vX.Y.Z` — the release commit to be tagged and published
2. `[changelog] Post-release bump vX.Y.Z → vX.Y.Z` — bumps version for the next dev cycle

Since `github.event.head_commit` only sees the post-release bump, this module extracts
the full commit range from the push event and identifies release commits that need
special handling (tagging, PyPI publishing, GitHub release creation).

```{rubric} Output shapes
```

Every key is [printed to the environment file](https://docs.github.com/en/free-pro-team@latest/actions/reference/workflow-commands-for-github-actions#environment-files)
as one `key=value` line. Values take three shapes:

```{code-block} text
is_python_project=true
doc_files="changelog.md" "readme.md" "docs/license.md"
new_commits_matrix={"commit": ["346ce66…", "6f27db4…"], "include": [{"commit": "346ce66…", "short_sha": "346ce66"}]}
```

A scalar prints bare. A list prints as space-joined, individually quoted items,
not a JSON array: workflow `if:` conditions test membership with a padded
`contains()` against that string. A matrix prints as inlined JSON for
`fromJSON()` to parse into a job matrix. See
{func}`~repomatic.metadata.output.format_github_value` for the encoding, and
{class}`~repomatic.metadata.output.Dialect` for the other output formats.

The full key inventory is generated from this module rather than listed here, so
it cannot go stale: run `repomatic show-metadata --list-keys`, or read the rendered
table in [the workflows documentation](https://repomatic.net/workflows).
"""

from __future__ import annotations

import logging
import re
import shlex
from collections.abc import Callable
from functools import cached_property, partial
from pathlib import Path
from typing import NamedTuple

from click_extra import ColumnSpec, field_docstrings

from ..changelog import (
    GITHUB_RELEASE_URL,
    Changelog,
    build_release_admonition,
    resolved_changelog_path,
)
from ..config import Config
from ..deps.pins import min_release_age_days
from ..file_inventory import FileInventory
from ..github.release_sync import build_expected_body
from ..github.workflow_files import iter_steps
from ..mailmap import MAILMAP_PATH
from ..pypi import PYPI_PROJECT_URL
from ..pyproject import get_current_version
from ..release.binary import (
    FLAT_BUILD_TARGETS,
)
from .env import EnvironmentMetadata
from .git import GitMetadata
from .matrix import MatrixMetadata
from .output import Dialect, format_github_value
from .project import ProjectMetadata

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping
    from typing import Any, Final, Self


FILE_GROUP_KEYS: Final[frozenset[str]] = frozenset((
    "doc_files",
    "gitignore_exists",
    "image_files",
    "json_files",
    "markdown_files",
    "pyproject_files",
    "python_files",
    "shfmt_files",
    "workflow_files",
    "yaml_files",
    "zsh_files",
))
"""Metadata keys read off {attr}`Metadata.files`, the attribute of the same name.

The inventory answers them with no CI context, no git history and no
`pyproject.toml`, so {meth}`Metadata.dump_factories` reads them there directly.
"""

METADATA_CONFIG_FIELDS: Final[tuple[str, ...]] = (
    "binaries_sync",
    "manpages_asset_name",
    "manpages_script",
    "nuitka_extras",
    "release_assets",
    "site_cloudflare_project",
    "site_deploy",
    "sphinx_builder",
)
"""`Config` fields a workflow reads through the metadata output, sorted.

Every other field stays out of the output: the subcommand that needs one reads
it from `[tool.repomatic]` directly. A new field joins this list only when a
workflow condition or step reads it, like the `binaries.sync` toggle a separate
`pr-sync` step depends on.
"""


_METADATA_KEY_DESCRIPTIONS: Final[dict[str, str]] = {
    "is_bot": "Workflow was triggered by a bot or automated process.",
    "skip_binary_build": "Binary builds should be skipped for this event.",
    "yaml_changed": "Current event's commit range touches at least one YAML file.",
    "zsh_changed": "Current event's commit range touches at least one Zsh file.",
    "workflows_changed": (
        "Current event's commit range touches at least one GitHub workflow file."
    ),
    "mailmap_exists": "Whether a .mailmap file exists in the repository.",
    "gitignore_exists": "Whether a .gitignore file exists in the repository.",
    "python_files": "List of Python files in the repository.",
    "json_files": "List of JSON files in the repository.",
    "yaml_files": "List of YAML files in the repository.",
    "pyproject_files": "List of pyproject.toml files in the repository.",
    "workflow_files": "List of GitHub workflow files.",
    "doc_files": "List of documentation files.",
    "markdown_files": "List of Markdown files.",
    "image_files": "List of image files.",
    "shfmt_files": "List of shell files formattable by shfmt.",
    "zsh_files": "List of Zsh files.",
    "is_python_project": "Repository is a Python project with pyproject.toml.",
    "is_python_package": "Repository builds a distributable Python package, not a uv virtual project.",
    "package_name": "Package name as published on PyPI.",
    "cli_scripts": "CLI script entry points from pyproject.toml.",
    "project_description": "Project description from pyproject.toml.",
    "mypy_params": "Generated mypy command-line parameters.",
    "current_version": "Current version from pyproject.toml.",
    "released_version": "Version of the release commit, if any.",
    "is_sphinx": "Sphinx configuration file is present.",
    "active_autodoc": "Active Sphinx autodoc extensions detected.",
    "uses_myst": "MyST-Parser is active in Sphinx configuration.",
    "release_notes": "Release notes for the GitHub release.",
    "release_notes_with_admonition": "Release notes with PyPI availability admonition.",
    "new_commits_matrix": "Matrix of new commits with long and short SHA values.",
    "release_commits_matrix": "Matrix of release commits with long and short SHA values.",
    "build_targets": "List of Nuitka build targets for all platforms.",
    "nuitka_matrix": "Matrix for Nuitka compilation workflows.",
    "runner_arch": "Architecture of each runner image in the full test matrix.",
    "test_matrix": "Full test matrix for non-PR events.",
    "test_matrix_pr": "Reduced test matrix for pull requests.",
    "minor_bump_allowed": "Minor version bump is allowed by commit history.",
    "major_bump_allowed": "Major version bump is allowed by commit history.",
    "npm_min_release_age_days": "npm min-release-age cooldown, in whole days.",
}
"""One-liner descriptions for each metadata key produced by {meth}`Metadata.dump`."""


METADATA_KEYS_HEADER_DEFS: tuple[ColumnSpec, ...] = (
    ColumnSpec("key", "Key"),
    ColumnSpec("description", "Description"),
)
"""Column definitions for the metadata keys reference table."""


def metadata_keys_reference() -> list[tuple[str, str]]:
    """Build the metadata keys reference as table rows.

    Returns a list of `(key, description)` tuples for all keys produced by
    {meth}`Metadata.dump`, including the {data}`METADATA_CONFIG_FIELDS` of
    `[tool.repomatic]`. Rows are unsorted: sorting is handled by the CLI's
    `SortByOption`.
    """
    rows = list(_METADATA_KEY_DESCRIPTIONS.items())

    # Collapse each attribute docstring to its first paragraph, single-spaced:
    # the metadata table wants the one-line summary, not the full prose.
    docstrings = {
        name: " ".join(text.split("\n\n")[0].split())
        for name, text in field_docstrings(Config).items()
    }
    for name in METADATA_CONFIG_FIELDS:
        rows.append((name, docstrings.get(name, "").replace("``", "`")))

    return rows


def all_metadata_keys() -> frozenset[str]:
    """Returns the set of all valid metadata key names."""
    return frozenset(_METADATA_KEY_DESCRIPTIONS) | frozenset(METADATA_CONFIG_FIELDS)


METADATA_VALUE_OPTIONS: frozenset[str] = frozenset((
    "--format",
    "-o",
    "--output",
    "--output-format",
    "--sort-by",
))
"""Options on the `show-metadata` command consuming the token that follows them.

Needed by {func}`requested_metadata_keys` to tell a positional key from an
option's value while reading a workflow's `run:` line. `--format` is the
spelling of releases before `8.0.0`, kept so that a workflow pinned to one of
them still parses.

Listed here rather than derived, and pinned against the real command by
repomatic's own test suite, so an option added later cannot quietly turn its
value into a token the lint reports as an unknown key.
"""

METADATA_SUBCOMMAND: Final[str] = "show-metadata"
"""The subcommand that prints the metadata keys a workflow asks for."""

REMOVED_METADATA_ALIAS: Final[str] = "metadata"
"""The alias of {data}`METADATA_SUBCOMMAND` that `8.0.0` removed.

A workflow still calling it fails at its first job, so the readers below
recognize it in order to report it.
"""

_METADATA_KEY_TOKEN: Final = re.compile(r"^[a-z][a-z0-9_]*$")
"""Shape of a metadata key, used to tell one from a neighbouring shell token.

Every key is a Python identifier, so a token carrying a hyphen (`github-json`),
a dollar (`$GITHUB_OUTPUT`) or a dot is something else on the command line and
is passed over rather than reported as unknown.
"""

_SHELL_OPERATORS: Final[frozenset[str]] = frozenset((
    "&&",
    "||",
    ";",
    "|",
    ">",
    ">>",
    "<",
    "&",
))
"""Tokens that end the invocation and start unrelated words.

`repomatic show-metadata a b && echo done` must not report `echo` and `done` as
metadata keys.
"""


def _metadata_invocations(
    command: str, package: str
) -> Iterator[tuple[str, list[str]]]:
    """Yield each call of the metadata subcommand in a `run:` script.

    Handles both spellings in use, the upstream
    `uv run -- repomatic show-metadata …` and the downstream
    `uvx 'repomatic==1.2.3' show-metadata …`, by looking for the subcommand
    after any token naming the package. The removed {data}`REMOVED_METADATA_ALIAS`
    spelling is recognized too, so a caller can report it.

    :param command: The step's `run:` script, folded or literal.
    :param package: Upstream package name (like `repomatic`).
    :return: `(subcommand, arguments)` per call, the arguments stopping at the
        first shell operator.
    """
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
        for index, token in enumerate(tokens):
            if seen_package and token in (METADATA_SUBCOMMAND, REMOVED_METADATA_ALIAS):
                arguments: list[str] = []
                for argument in tokens[index + 1 :]:
                    if argument in _SHELL_OPERATORS:
                        break
                    arguments.append(argument)
                yield token, arguments
                break
            if token == package or token.startswith(f"{package}=="):
                seen_package = True


def _positional_keys(arguments: list[str]) -> list[str]:
    """Read the positional keys out of a metadata call's arguments.

    Reads them the way Click would: options are dropped along with the value
    each one consumes, and what remains are the positional key arguments.
    """
    keys: list[str] = []
    skip_next = False
    for token in arguments:
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


def requested_metadata_keys(command: str, package: str) -> list[str]:
    """Positional keys a shell command passes to the `show-metadata` subcommand.

    A call of the removed {data}`REMOVED_METADATA_ALIAS` requests nothing, since
    the command it names no longer exists: {func}`unknown_metadata_keys`
    reports it instead.

    :param command: The step's `run:` script, folded or literal.
    :param package: Upstream package name (like `repomatic`).
    :return: The key names requested, in the order written.
    """
    return [
        key
        for subcommand, arguments in _metadata_invocations(command, package)
        if subcommand == METADATA_SUBCOMMAND
        for key in _positional_keys(arguments)
    ]


class MetadataRequest(NamedTuple):
    """The metadata one workflow step asks for."""

    path: Path
    """The workflow file holding the step."""

    job_id: str
    """The job holding the step."""

    keys: tuple[str, ...]
    """Every key the step requests, in the order written."""

    unknown: tuple[str, ...]
    """The requested keys this version does not emit, sorted."""

    removed_alias: bool
    """Whether the step calls the {data}`REMOVED_METADATA_ALIAS` spelling."""


def unknown_metadata_keys(
    workflows: Mapping[Path, Mapping[str, Any]], package: str
) -> list[MetadataRequest]:
    """Read every metadata call of *workflows*, and the keys it gets wrong.

    The one scan behind the `lint-repo` check and the `repomatic init` warning,
    so the two verdicts cannot disagree about what a `run:` line requests.

    :param workflows: Parsed workflow documents, keyed by path.
    :param package: Upstream package name (like `repomatic`).
    :return: One entry per step that calls the metadata subcommand with keys,
        or that calls the removed alias at all, in workflow order. A step
        requesting only valid keys is listed with no unknown key.
    """
    valid = all_metadata_keys()
    requests: list[MetadataRequest] = []
    for path, job_id, step in iter_steps(workflows):
        command = step.get("run")
        if not isinstance(command, str):
            continue
        keys: list[str] = []
        removed_alias = False
        for subcommand, arguments in _metadata_invocations(command, package):
            removed_alias |= subcommand == REMOVED_METADATA_ALIAS
            keys.extend(_positional_keys(arguments))
        if not keys and not removed_alias:
            continue
        requests.append(
            MetadataRequest(
                path=path,
                job_id=job_id,
                keys=tuple(keys),
                unknown=tuple(sorted(set(keys) - valid)),
                removed_alias=removed_alias,
            )
        )
    return requests


# Silence overly verbose debug messages from py-walk logger.
logging.getLogger("py_walk").setLevel(logging.WARNING)


class Metadata(
    EnvironmentMetadata,
    GitMetadata,
    MatrixMetadata,
    ProjectMetadata,
):
    """Metadata class.

    Implemented as a singleton: every `Metadata()` call returns the same
    instance within a process. This is safe because env vars and project files
    do not change during a single CLI invocation. Use {meth}`reset` in test
    teardown to discard the cached instance between tests.
    """

    _instance: Metadata | None = None

    def __new__(cls) -> Self:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance  # type: ignore[return-value]

    @classmethod
    def reset(cls) -> None:
        """Discard the singleton so the next call creates a fresh instance.

        Intended for test teardown only. Production code should never call this.
        """
        cls._instance = None

    # 8.0.0 shim: re-export for callers not migrated yet.
    format_github_value = staticmethod(format_github_value)

    @cached_property
    def mailmap_exists(self) -> bool:
        return MAILMAP_PATH.is_file()

    @cached_property
    def files(self) -> FileInventory:
        """What this repository holds on disk, `.gitignore` applied.

        The inventory is its own concern ({mod}`repomatic.file_inventory`):
        answering "which Markdown files are there" needs no CI context, no git
        history and no `pyproject.toml`. The {data}`FILE_GROUP_KEYS` output
        keys read the inventory attribute of the same name.
        """
        return FileInventory()

    # 8.0.0 shim: re-export for callers not migrated yet.
    @property
    def image_files(self) -> list[Path]:
        """Images the optimizer can losslessly shrink."""
        return self.files.image_files

    # 8.0.0 shim: re-export for callers not migrated yet.
    @property
    def workflow_files(self) -> list[Path]:
        """GitHub workflow definitions."""
        return self.files.workflow_files

    # 8.0.0 shim: re-export for callers not migrated yet.
    @property
    def yaml_files(self) -> list[Path]:
        """YAML files."""
        return self.files.yaml_files

    # 8.0.0 shim: re-export for callers not migrated yet.
    @property
    def zsh_files(self) -> list[Path]:
        """Zsh scripts, by extension or shebang."""
        return self.files.zsh_files

    @cached_property
    def current_version(self) -> str | None:
        """Returns the current version.

        Current version is fetched from the `bump-my-version` configuration file.

        During a release, two commits are bundled into a single push event:

        1. `[changelog] Release vX.Y.Z` — freezes the version to the release number
        2. `[changelog] Post-release bump vX.Y.Z → vX.Y.Z` — bumps to the next dev version

        In this situation, the current version returned is the one from the most recent
        commit (the post-release bump), which represents the next development version.
        Use `released_version` to get the version from the release commit.

        The commit matrix lists the new commits oldest first, so the most recent
        one is its last entry.
        """
        version = None
        if self.new_commits_matrix:
            details = self.new_commits_matrix.include
            if details:
                version = details[-1].get("current_version")
        else:
            version = get_current_version()
        return version

    @cached_property
    def released_version(self) -> str | None:
        """Returns the version of the release commit.

        During a release push event, this extracts the version from the
        `[changelog] Release vX.Y.Z` commit, which is distinct from
        `current_version` (the post-release bump version). This is used for
        tagging, PyPI publishing, and GitHub release creation.

        Returns `None` if no release commit is found in the current event.
        """
        version = None
        if self.release_commits_matrix:
            details = self.release_commits_matrix.include
            if details:
                # This script is only designed for at most 1 release in the list of new
                # commits.
                assert len(details) == 1
                version = details[0].get("current_version")
        return version

    @cached_property
    def _release_changelog(self) -> tuple[str, Changelog] | None:
        """The version to write release notes for, and the parsed changelog.

        Shared by {attr}`release_notes` and
        {attr}`release_notes_with_admonition`, which need the same two inputs
        and would otherwise read and parse `changelog.md` twice per run.

        :return: `(version, changelog)`, or `None` when there is no version to
            release or the changelog file is missing.
        """
        version = self.released_version or self.current_version
        if not version:
            return None
        changelog_path = resolved_changelog_path(self.config)
        if not changelog_path.exists():
            return None
        return version, Changelog(changelog_path.read_text(encoding="UTF-8"))

    @cached_property
    def release_notes(self) -> str | None:
        """Generate notes to be attached to the GitHub release.

        Renders the `github-releases` template with changelog
        content for the version. The template is the single place
        that defines the release body layout.
        """
        if self._release_changelog is None:
            return None
        version, changelog = self._release_changelog
        notes = build_expected_body(changelog, version)
        return notes or None

    @cached_property
    def release_notes_with_admonition(self) -> str | None:
        """Generate release notes with a pre-computed availability admonition.

        Builds the same body as {attr}`release_notes`, but injects a
        `> [!NOTE]` admonition linking to PyPI and GitHub even before
        `fix-changelog` has a chance to update `changelog.md`.

        The engine's `create-release` job bakes this body into the GitHub
        release at draft-creation time, so the admonition is present from the
        start. Baking it there, rather than editing the release from the
        caller's fast `publish-pypi` lane, avoids a race between the lanes: that
        edit can run before `create-release` has created the release, and then
        drops the admonition under `continue-on-error`. The bake is optimistic:
        it assumes the parallel PyPI upload succeeds, which it does on the normal
        path; a failed upload surfaces as a red `publish-pypi` job, not as a
        wrong admonition the user must catch.

        Returns `None` when the project is not on PyPI, has no changelog, or
        has no version to release, in which case `create-release` falls back to
        the plain {attr}`release_notes`.
        """
        if self._release_changelog is None or not self.package_name:
            return None
        version, changelog = self._release_changelog

        repo_url = changelog.extract_repo_url()
        if not repo_url:
            return None

        pypi_url = PYPI_PROJECT_URL.format(
            package=self.package_name,
            version=version,
        )
        github_url = GITHUB_RELEASE_URL.format(
            repo_url=repo_url,
            version=version,
        )
        admonition = build_release_admonition(
            version,
            pypi_url=pypi_url,
            github_url=github_url,
        )
        notes = build_expected_body(
            changelog,
            version,
            admonition_override=admonition,
        )
        return notes or None

    def dump_factories(self) -> dict[str, Callable[[], Any]]:
        """Lazy value factories for every metadata key, in output order.

        Each value is computed only when its key is included, so
        `keys=("is_python_project",)` skips
        {attr}`~repomatic.metadata.matrix.MatrixMetadata.nuitka_matrix` and the
        git history walk it pulls in.

        Split out of {meth}`dump` so the key inventory is inspectable without
        computing anything: `tests/test_metadata.py` asserts these names match
        `_METADATA_KEY_DESCRIPTIONS` plus {data}`METADATA_CONFIG_FIELDS`, which
        is what keeps `--list-keys`, {func}`all_metadata_keys` and the emitted
        output from drifting apart.

        Derived from `_METADATA_KEY_DESCRIPTIONS` rather than re-listing
        every key: most keys read the attribute of the same name, on this
        instance or on {attr}`files` for the {data}`FILE_GROUP_KEYS`, so only
        the handful whose value is not a plain attribute carry an explicit
        factory.

        :return: Key name to a zero-argument callable producing its value.
        """
        non_attribute: dict[str, Callable[[], Any]] = {
            "cli_scripts": lambda: [cli_id for cli_id, _, _ in self.script_entries],
            "build_targets": lambda: FLAT_BUILD_TARGETS,
            "npm_min_release_age_days": lambda: min_release_age_days(
                self.config.minimum_release_age
            ),
        }
        factories: dict[str, Callable[[], Any]] = {}
        for key in _METADATA_KEY_DESCRIPTIONS:
            source = self.files if key in FILE_GROUP_KEYS else self
            # `partial` binds `key` eagerly, dodging the late-binding pitfall.
            factories[key] = non_attribute.get(key, partial(getattr, source, key))
        for name in METADATA_CONFIG_FIELDS:
            factories[name] = partial(getattr, self.config, name)
        return factories

    def dump(
        self,
        dialect: Dialect = Dialect.github,
        keys: tuple[str, ...] = (),
    ) -> str:
        """Returns metadata in the specified format.

        Defaults to GitHub dialect. When *keys* is non-empty, only the
        requested keys are computed and included in the output. Filtered-out
        keys are never accessed, so callers requesting a small subset avoid
        triggering expensive dependent computations (git history walks, file
        system scans, build matrix expansion). See {meth}`dump_factories`.
        """
        factories = self.dump_factories()

        keys_set = set(keys)
        metadata: dict[str, Any] = {
            name: factory()
            for name, factory in factories.items()
            if not keys_set or name in keys_set
        }

        logging.debug(f"Raw metadata: {metadata!r}")
        logging.debug(f"Format metadata into {dialect} format.")

        content = dialect.serialize(metadata)

        logging.debug(f"Formatted metadata:\n{content}")

        return content
