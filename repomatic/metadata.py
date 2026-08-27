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
`fromJSON()` to parse into a job matrix. See {meth}`Metadata.format_github_value`
for the encoding, and {class}`Dialect` for the other output formats.

The full key inventory is generated from this module rather than listed here, so
it cannot go stale: run `repomatic metadata --list-keys`, or read the rendered
table in [the workflows documentation](https://repomatic.net/workflows).
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable, Iterable
from dataclasses import fields
from functools import cached_property, partial
from pathlib import Path

from click_extra import field_docstrings
from extra_platforms import is_github_ci

from .binary import (
    FLAT_BUILD_TARGETS,
)
from .changelog import (
    GITHUB_RELEASE_URL,
    Changelog,
    build_release_admonition,
    resolved_changelog_path,
)
from .compat import StrEnum
from .config import (
    SUBCOMMAND_CONFIG_FIELDS,
    Config,
)
from .file_inventory import FileInventory
from .github.actions import (
    generate_delimiter,
    get_github_event,
)
from .github.matrix import Matrix
from .github.release_sync import build_expected_body
from .mailmap import MAILMAP_PATH
from .metadata_env import EnvironmentMetadata
from .metadata_git import GitMetadata
from .metadata_matrix import MatrixMetadata
from .metadata_project import ProjectMetadata
from .pypi import PYPI_PROJECT_URL
from .version_sync import min_release_age_days

TYPE_CHECKING = False
if TYPE_CHECKING:
    from typing import Any, Final

    from typing_extensions import Self


HEREDOC_FIELDS: Final[frozenset[str]] = frozenset((
    "release_notes",
    "release_notes_with_admonition",
))
"""Metadata fields that should always use heredoc format in GitHub Actions output.

Some fields may contain special characters (brackets, parentheses, emojis, or potential
newlines) that can break GitHub Actions parsing when using simple `key=value` format.
These fields will use the heredoc delimiter format regardless of whether they currently
contain multiple lines.
"""


class Dialect(StrEnum):
    """Output dialect for metadata serialization."""

    github = "github"
    github_json = "github-json"
    json = "json"

    def serialize(self, metadata: dict[str, Any]) -> str:
        """Render *metadata* in this dialect.

        :param metadata: Raw key-to-value mapping from {meth}`Metadata.dump`.
        :return: The serialized payload.
        """
        if self is Dialect.github:
            content = ""
            for env_name, value in metadata.items():
                env_value = Metadata.format_github_value(value)

                # Use heredoc format for multiline values or fields with
                # special chars.
                use_heredoc = (
                    len(env_value.splitlines()) > 1 or env_name in HEREDOC_FIELDS
                )
                if not use_heredoc:
                    content += f"{env_name}={env_value}\n"
                else:
                    # Use a random unique delimiter to encode multiline value.
                    delimiter = generate_delimiter()
                    content += f"{env_name}<<{delimiter}\n{env_value}\n{delimiter}\n"
            return content

        if self is Dialect.github_json:
            # Bundle all metadata into a single `metadata` output key as JSON.
            # Downstream jobs access values via
            # `fromJSON(needs.metadata.outputs.metadata).key`,
            # eliminating the need for per-key `outputs:` declarations.
            #
            # Pre-format list/tuple values via format_github_value(). GitHub
            # Actions stringifies JSON arrays as "Array" when interpolated in
            # ${{ }} expressions, so workflows that splice lists into `run:`
            # or `env:` contexts would receive the literal word "Array".
            # Matrix objects are excluded: they serialize to JSON objects via
            # JSONMetadata and are consumed directly in `strategy: matrix:`
            # blocks, which accept expression objects without string coercion.
            formatted = {}
            for k, v in metadata.items():
                if isinstance(v, (list, tuple)):
                    formatted[k] = Metadata.format_github_value(v)
                else:
                    formatted[k] = v
            json_str = json.dumps(formatted, cls=JSONMetadata, separators=(",", ":"))
            return f"metadata={json_str}\n"

        assert self is Dialect.json
        return json.dumps(metadata, cls=JSONMetadata, indent=2)


_METADATA_KEY_DESCRIPTIONS: Final[dict[str, str]] = {
    "is_bot": "Workflow was triggered by a bot or automated process.",
    "skip_binary_build": "Binary builds should be skipped for this event.",
    "yaml_changed": "Current event's commit range touches at least one YAML file.",
    "zsh_changed": "Current event's commit range touches at least one Zsh file.",
    "workflows_changed": (
        "Current event's commit range touches at least one GitHub workflow file."
    ),
    "new_commits": "Hashes of new commits in the push event.",
    "release_commits": "Hashes of release commits in the push event.",
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
    "test_matrix": "Full test matrix for non-PR events.",
    "test_matrix_pr": "Reduced test matrix for pull requests.",
    "minor_bump_allowed": "Minor version bump is allowed by commit history.",
    "major_bump_allowed": "Major version bump is allowed by commit history.",
    "npm_min_release_age_days": "npm min-release-age cooldown, in whole days.",
}
"""One-liner descriptions for each metadata key produced by {meth}`Metadata.dump`."""


METADATA_KEYS_HEADER_DEFS: tuple[tuple[str, str], ...] = (
    ("Key", "key"),
    ("Description", "description"),
)
"""Column definitions for the metadata keys reference table."""


def _metadata_config_fields() -> list[str]:
    """`Config` field names exposed as metadata outputs.

    One filter shared by {func}`metadata_keys_reference`,
    {func}`all_metadata_keys`, and {meth}`Metadata.dump`, so the three
    surfaces can never disagree on which config fields are metadata. Excluded
    are the Nuitka fields consumed through dedicated computed keys and every
    field in {data}`~repomatic.config.SUBCOMMAND_CONFIG_FIELDS` (read directly
    by its subcommand).
    """
    skipped = (
        "nuitka_dev_targets",
        "nuitka_entry_points",
        "nuitka_unstable_targets",
    )
    return [
        f.name
        for f in fields(Config)
        if f.name not in skipped and f.name not in SUBCOMMAND_CONFIG_FIELDS
    ]


def metadata_keys_reference() -> list[tuple[str, str]]:
    """Build the metadata keys reference as table rows.

    Returns a list of `(key, description)` tuples for all keys produced by
    {meth}`Metadata.dump`, including `[tool.repomatic]` config fields that are
    exposed as metadata outputs. Rows are unsorted: sorting is handled by the
    CLI's `SortByOption`.
    """
    rows = [(k, v) for k, v in _METADATA_KEY_DESCRIPTIONS.items()]

    # Add config fields exposed as metadata (same filter as dump()). Collapse
    # each attribute docstring to its first paragraph, single-spaced: the
    # metadata table wants the one-line summary, not the full prose.
    docstrings = {
        name: " ".join(text.split("\n\n")[0].split())
        for name, text in field_docstrings(Config).items()
    }
    for name in _metadata_config_fields():
        desc = docstrings.get(name, "").replace("``", "`")
        rows.append((name, desc))

    return rows


def all_metadata_keys() -> frozenset[str]:
    """Returns the set of all valid metadata key names."""
    return frozenset(_METADATA_KEY_DESCRIPTIONS) | frozenset(_metadata_config_fields())


METADATA_VALUE_OPTIONS: frozenset[str] = frozenset((
    "--format",
    "-o",
    "--output",
    "--sort-by",
))
"""Options on the `metadata` command consuming the token that follows them.

Needed by {func}`repomatic.lint_repo.check_metadata_keys` to tell a positional
key from an option's value while reading a workflow's `run:` line. The command
itself is not importable from there: {mod}`repomatic.cli` reads `sys.stdout.name`
at import time, so importing it under a test that has replaced stdout raises.

Listed here rather than derived, and pinned against the real command by
repomatic's own test suite, so an option added later cannot quietly turn its
value into a token the lint reports as an unknown key.
"""


# Silence overly verbose debug messages from py-walk logger.
logging.getLogger("py_walk").setLevel(logging.WARNING)


class JSONMetadata(json.JSONEncoder):
    """Custom JSON encoder for metadata serialization."""

    def default(self, o: Any) -> Any:
        if isinstance(o, Matrix):
            return o.matrix()

        if isinstance(o, Path):
            return str(o)

        return super().default(o)


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

    pyproject_path = Path() / "pyproject.toml"
    sphinx_conf_path = Path() / "docs" / "conf.py"

    @cached_property
    def github_event(self) -> dict[str, Any]:
        """Load the GitHub event payload from `GITHUB_EVENT_PATH`.

        GitHub Actions automatically sets `GITHUB_EVENT_PATH` to a JSON file
        containing the complete webhook event payload.

        Delegates to {func}`repomatic.github.actions.get_github_event`, the
        one loader of the payload: the two used to parse the file
        independently and disagree on whether a missing file was fatal. The
        tolerant contract wins (an unreadable payload degrades every consumer
        to its no-event behavior); only the CI-context warning lives here,
        since the shared loader serves non-CI callers too.
        """
        if is_github_ci() and not os.environ.get("GITHUB_EVENT_PATH"):
            logging.warning("GITHUB_EVENT_PATH not set in environment.")
        event = get_github_event()
        if event:
            logging.debug("--- GitHub event payload ---")
            logging.debug(json.dumps(event, indent=4))
        return event

    @cached_property
    def mailmap_exists(self) -> bool:
        return MAILMAP_PATH.is_file()

    @cached_property
    def files(self) -> FileInventory:
        """What this repository holds on disk, `.gitignore` applied.

        The inventory is its own concern ({mod}`repomatic.file_inventory`):
        answering "which Markdown files are there" needs no CI context, no git
        history and no `pyproject.toml`. The group properties below forward to
        it so every `metadata` output key keeps its name; a caller wanting the
        argument-taking lookups goes to the inventory itself.
        """
        return FileInventory()

    @property
    def gitignore_exists(self) -> bool:
        """Whether a `.gitignore` file is present."""
        return self.files.gitignore_exists

    @property
    def python_files(self) -> list[Path]:
        """Python sources, notebooks included."""
        return self.files.python_files

    @property
    def json_files(self) -> list[Path]:
        """JSON files Biome can format."""
        return self.files.json_files

    @property
    def yaml_files(self) -> list[Path]:
        """YAML files."""
        return self.files.yaml_files

    @property
    def pyproject_files(self) -> list[Path]:
        """Every `pyproject.toml` in the tree."""
        return self.files.pyproject_files

    @property
    def workflow_files(self) -> list[Path]:
        """GitHub workflow definitions."""
        return self.files.workflow_files

    @property
    def doc_files(self) -> list[Path]:
        """Documentation sources."""
        return self.files.doc_files

    @property
    def markdown_files(self) -> list[Path]:
        """Markdown files."""
        return self.files.markdown_files

    @property
    def image_files(self) -> list[Path]:
        """Images the optimizer can losslessly shrink."""
        return self.files.image_files

    @property
    def shfmt_files(self) -> list[Path]:
        """Shell scripts `shfmt` formats."""
        return self.files.shfmt_files

    @property
    def zsh_files(self) -> list[Path]:
        """Zsh scripts, by extension or shebang."""
        return self.files.zsh_files

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
        start. Doing it there (rather than editing the release from the
        caller's fast `publish-pypi` lane) removes the cross-lane race where the
        edit ran before `create-release` had created the release, and so silently
        dropped the admonition under `continue-on-error`. The bake is optimistic:
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

    @staticmethod
    def format_github_value(value: Any) -> str:
        """Transform Python value to GitHub-friendly, JSON-like, console string.

        Renders:

        - `str` as-is
        - `None` into empty string
        - `bool` into lower-cased string
        - `Matrix` into JSON string
        - `Iterable` of mixed strings and `Path` into a serialized space-separated
          string, where `Path` items are double-quoted
        - other `Iterable` into a JSON string

        ```{todo}
        Widen the JSON branch beyond an iterable of `dict[str, str]`, the only
        shape it asserts on today, when a metadata key needs a richer one.
        ```
        """
        # Structured metadata to be rendered as JSON.
        if isinstance(value, Matrix):
            return str(value)

        # Convert non-strings.
        if not isinstance(value, str):
            if value is None:
                value = ""

            elif isinstance(value, bool):
                value = str(value).lower()

            elif isinstance(value, int):
                value = str(value)

            elif isinstance(value, dict):
                raise NotImplementedError(
                    f"GitHub formatting for mapping: {value!r}. Wrap it in a "
                    "Matrix, or expose it to its subcommand directly through "
                    "SUBCOMMAND_CONFIG_FIELDS instead of as a metadata key."
                )

            elif isinstance(value, Iterable):
                # Cast all items to strings, wrapping Path items with double-quotes.
                if all(isinstance(i, (str, Path)) for i in value):
                    items = (
                        (f'"{i}"' if isinstance(i, Path) else str(i)) for i in value
                    )
                    value = " ".join(items)
                else:
                    assert all(
                        isinstance(i, dict)
                        and all(
                            isinstance(k, str) and isinstance(v, str)
                            for k, v in i.items()
                        )
                        for i in value
                    ), f"Unsupported iterable value: {value!r}"
                    value = json.dumps(value)

            else:
                raise NotImplementedError(
                    f"GitHub formatting for {type(value).__name__}: {value!r}. "
                    "A nested config dataclass belongs in "
                    "SUBCOMMAND_CONFIG_FIELDS, not in the metadata output."
                )

        return str(value)

    def dump_factories(self) -> dict[str, Callable[[], Any]]:
        """Lazy value factories for every metadata key, in output order.

        Each value is computed only when its key is included, so
        `keys=("is_python_project",)` skips {attr}`nuitka_matrix` and the git
        history walk it pulls in.

        Split out of {meth}`dump` so the key inventory is inspectable without
        computing anything: `tests/test_metadata.py` asserts these names match
        {data}`_METADATA_KEY_DESCRIPTIONS` plus
        {func}`_metadata_config_fields`, which is what keeps `--list-keys`,
        {func}`all_metadata_keys` and the emitted output from drifting apart.

        Derived from {data}`_METADATA_KEY_DESCRIPTIONS` rather than re-listing
        every key: most keys read the attribute of the same name, so only the
        handful whose value is not a plain attribute carry an explicit
        factory.

        :return: Key name to a zero-argument callable producing its value.
        """
        non_attribute: dict[str, Callable[[], Any]] = {
            "new_commits": lambda: self.new_commits_hash,
            "release_commits": lambda: self.release_commits_hash,
            "cli_scripts": lambda: [cli_id for cli_id, _, _ in self.script_entries],
            "build_targets": lambda: FLAT_BUILD_TARGETS,
            "npm_min_release_age_days": lambda: min_release_age_days(
                self.config.minimum_release_age
            ),
        }
        factories: dict[str, Callable[[], Any]] = {
            # `partial` binds `key` eagerly, dodging the late-binding pitfall.
            key: non_attribute.get(key, partial(getattr, self, key))
            for key in _METADATA_KEY_DESCRIPTIONS
        }

        # Add config from [tool.repomatic] in pyproject.toml.
        # Convert kebab-case config keys to snake_case metadata keys.
        # Exclude nuitka internal config (dedicated properties with validation logic)
        # and subcommand config fields (read directly by dep-graph).
        for name in _metadata_config_fields():
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
